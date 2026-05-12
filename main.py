"""
Pareeksha Gurukul - Unified Bot + Server v2.2
Key fixes:
  - Single gunicorn worker (--workers 1) so in-memory state is consistent
  - Every data read goes to disk fresh — no stale in-memory cache
  - Removed MarkdownV2 everywhere (was causing silent parse errors)
  - Webhook returns 200 instantly; all work runs in background thread
  - data_lock protects concurrent file access within the single worker
"""

import os
import json
import logging
import threading
from flask import Flask, request, jsonify, send_from_directory
import requests
from dotenv import load_dotenv

load_dotenv()

# ── LOGGING ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ── CONFIG ────────────────────────────────────────────────────────────────────
BOT_TOKEN     = os.getenv("BOT_TOKEN", "")
EVAL_GROUP_ID = os.getenv("EVAL_GROUP_ID", "")
WEBAPP_URL    = os.getenv("WEBAPP_URL", "").rstrip("/")
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")
PORT          = int(os.getenv("PORT", 8080))

ADMIN_IDS = [a.strip() for a in ADMIN_IDS_RAW.split(",") if a.strip()]
TG_API    = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ── DATA HELPERS — always read/write disk ─────────────────────────────────────
DATA_FILE = "data.json"
data_lock = threading.Lock()

def _load():
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
                d.setdefault("question", "")
                d.setdefault("submissions", {})
                d.setdefault("students", {})
                return d
    except Exception as e:
        log.error(f"_load error: {e}")
    return {"question": "", "submissions": {}, "students": {}}

def _save(d):
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error(f"_save error: {e}")

def get_question():
    with data_lock:
        return _load().get("question", "")

def set_question(q):
    with data_lock:
        d = _load()
        d["question"]    = q
        d["submissions"] = {}
        _save(d)

def clear_question():
    with data_lock:
        d = _load()
        d["question"] = ""
        _save(d)

def get_students():
    with data_lock:
        return dict(_load().get("students", {}))

def register_student(uid, name):
    with data_lock:
        d = _load()
        d.setdefault("students", {})[str(uid)] = name
        _save(d)

def has_submitted(uid):
    with data_lock:
        return str(uid) in _load().get("submissions", {})

def record_submission(uid, name, answer_type, msg_id=None):
    with data_lock:
        d = _load()
        d.setdefault("submissions", {})[str(uid)] = {
            "name":        name,
            "answer_type": answer_type,
            "msg_id":      msg_id,
        }
        _save(d)

def get_stats():
    with data_lock:
        d = _load()
    subs     = d.get("submissions", {})
    students = d.get("students", {})
    q        = d.get("question", "")
    by_type  = {"text": 0, "audio": 0, "video": 0}
    for s in subs.values():
        t = s.get("answer_type", "text")
        by_type[t] = by_type.get(t, 0) + 1
    return q, students, subs, by_type

def reset_submissions():
    with data_lock:
        d = _load()
        count = len(d.get("submissions", {}))
        d["submissions"] = {}
        _save(d)
    return count

# ── TELEGRAM HELPERS ──────────────────────────────────────────────────────────
def tg_call(method, payload=None, files=None, timeout=25):
    url = f"{TG_API}/{method}"
    try:
        if files:
            r = requests.post(url, data=payload, files=files, timeout=timeout)
        else:
            r = requests.post(url, json=payload, timeout=timeout)
        result = r.json()
        if not result.get("ok"):
            log.warning(f"TG [{method}] not ok: {result.get('description')}")
        return result
    except requests.exceptions.Timeout:
        log.error(f"TG [{method}] timed out")
        return {}
    except Exception as e:
        log.error(f"TG [{method}] exception: {e}")
        return {}

def send_msg(chat_id, text, markup=None):
    payload = {
        "chat_id":    str(chat_id),
        "text":       text,
        "parse_mode": "Markdown",
    }
    if markup:
        payload["reply_markup"] = markup
    return tg_call("sendMessage", payload)

def is_admin(uid):
    return str(uid) in ADMIN_IDS

def run_bg(fn, *args, **kwargs):
    threading.Thread(target=fn, args=args, kwargs=kwargs, daemon=True).start()

# ── FLASK APP ─────────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder="static")

@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/health")
def health():
    q = get_question()
    return jsonify({"ok": True, "status": "running", "question_active": bool(q)})

@app.route("/question")
def api_question():
    q = get_question()
    return jsonify({"ok": True, "question": q, "active": bool(q)})

# ── SUBMIT ────────────────────────────────────────────────────────────────────
@app.route("/submit", methods=["POST"])
def submit():
    student_id   = request.form.get("student_id",   "").strip()
    student_name = request.form.get("student_name", "Student").strip()
    answer_type  = request.form.get("answer_type",  "text").strip()
    text_answer  = request.form.get("text_answer",  "").strip()

    if not student_id:
        return jsonify({"ok": False, "error": "Missing student ID"}), 400
    if not BOT_TOKEN or not EVAL_GROUP_ID:
        return jsonify({"ok": False, "error": "Server not configured"}), 500

    q = get_question()
    if not q:
        return jsonify({"ok": False, "error": "No active question"}), 400
    if has_submitted(student_id):
        return jsonify({"ok": False, "error": "Already submitted"}), 400

    file_bytes, mime, fname = None, None, None
    if answer_type in ("audio", "video"):
        f = request.files.get("file")
        if not f:
            return jsonify({"ok": False, "error": "No file uploaded"}), 400
        file_bytes = f.read()
        mime       = f.content_type or "application/octet-stream"
        fname      = f.filename or f"answer.{answer_type}"

    # Pre-record to block duplicate submissions
    record_submission(student_id, student_name, answer_type)
    run_bg(_forward_submission, student_id, student_name, q,
           answer_type, text_answer, file_bytes, mime, fname)

    return jsonify({"ok": True, "message": "Submitted successfully!"})


def _forward_submission(student_id, student_name, question,
                        answer_type, text_answer, file_bytes, mime, fname):
    caption = (
        f"New Submission\n"
        f"Student: {student_name} ({student_id})\n"
        f"Question: {question}\n"
        f"Type: {answer_type.upper()}"
    )
    if answer_type == "text":
        caption += f"\n\nAnswer:\n{text_answer[:3500]}"

    rating_kb = {
        "inline_keyboard": [[
            {"text": "⭐ 1", "callback_data": f"rate|{student_id}|1"},
            {"text": "⭐ 2", "callback_data": f"rate|{student_id}|2"},
            {"text": "⭐ 3", "callback_data": f"rate|{student_id}|3"},
            {"text": "⭐ 4", "callback_data": f"rate|{student_id}|4"},
            {"text": "⭐ 5", "callback_data": f"rate|{student_id}|5"},
        ]]
    }

    if answer_type == "text":
        resp = tg_call("sendMessage", {
            "chat_id":      EVAL_GROUP_ID,
            "text":         caption,
            "reply_markup": rating_kb,
        })
    else:
        if len(caption) > 1024:
            caption = caption[:1020] + "..."
        tg_method = "sendAudio" if answer_type == "audio" else "sendVideo"
        field_key = "audio"    if answer_type == "audio" else "video"
        resp = tg_call(
            tg_method,
            payload={
                "chat_id":      EVAL_GROUP_ID,
                "caption":      caption,
                "reply_markup": json.dumps(rating_kb),
            },
            files={field_key: (fname, file_bytes, mime)},
            timeout=60,
        )

    result = resp.get("result", {})
    if result:
        record_submission(student_id, student_name, answer_type,
                          msg_id=result.get("message_id"))
        log.info(f"Forwarded: {student_name} ({student_id}) type={answer_type}")
    else:
        log.error(f"Forward failed for {student_id}: {resp}")


# ── WEBHOOK ───────────────────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json(silent=True)
    if update:
        run_bg(_process_update, update)
    return "ok", 200   # always return instantly


def _process_update(update):
    try:
        cb = update.get("callback_query")
        if cb:
            _handle_callback(cb)
            return
        msg = update.get("message", {})
        if msg:
            _handle_message(msg)
    except Exception as e:
        log.error(f"_process_update error: {e}", exc_info=True)


def _handle_callback(cb):
    cb_data    = cb.get("data", "")
    evaluator  = cb.get("from", {}).get("first_name", "Evaluator")
    msg        = cb.get("message", {})
    chat_id    = msg.get("chat", {}).get("id")
    message_id = msg.get("message_id")
    cb_id      = cb["id"]

    if cb_data.startswith("rate|"):
        parts = cb_data.split("|")
        if len(parts) != 3:
            return
        _, student_id, stars_str = parts
        stars    = int(stars_str)
        star_str = "⭐" * stars

        send_msg(student_id,
            f"Your Answer Has Been Evaluated!\n\n"
            f"Rating: {star_str} ({stars}/5)\n"
            f"Evaluated by: {evaluator}\n\n"
            f"Keep up the great work! Send /start to submit for the next question."
        )
        tg_call("editMessageReplyMarkup", {
            "chat_id":    chat_id,
            "message_id": message_id,
            "reply_markup": {
                "inline_keyboard": [[
                    {"text": f"Rated {star_str} by {evaluator}", "callback_data": "done"}
                ]]
            }
        })
        tg_call("answerCallbackQuery", {
            "callback_query_id": cb_id,
            "text": f"Rated {stars}/5 - Student notified!"
        })
        log.info(f"Rated {student_id}: {stars}/5 by {evaluator}")

    elif cb_data == "done":
        tg_call("answerCallbackQuery", {
            "callback_query_id": cb_id,
            "text": "Already rated."
        })


def _handle_message(msg):
    text    = (msg.get("text") or "").strip()
    user    = msg.get("from", {})
    user_id = str(user.get("id", ""))
    name    = (user.get("first_name") or "Student").strip()
    chat_id = msg.get("chat", {}).get("id")

    if not text or not user_id:
        return

    register_student(user_id, name)
    cmd = text.split()[0].split("@")[0].lower()

    # ── /start ────────────────────────────────────────────────────────────────
    if cmd == "/start":
        q = get_question()
        if is_admin(user_id):
            q_preview = (q[:80] + "...") if len(q) > 80 else q
            send_msg(chat_id,
                f"Welcome back, {name} (Admin)\n\n"
                f"Admin Commands:\n"
                f"/setquestion - Set today's question\n"
                f"/clearquestion - Clear current question\n"
                f"/viewquestion - See current question\n"
                f"/stats - Submission stats\n"
                f"/resetsubmissions - Allow re-submissions\n"
                f"/broadcast - Notify all students\n\n"
                f"Status: {'Question active' if q else 'No question set'}\n"
                + (f"{q_preview}" if q else "")
            )
        else:
            webapp_url = f"{WEBAPP_URL}?uid={user_id}&name={name}"
            markup = {
                "inline_keyboard": [[
                    {"text": "Submit Your Answer",
                     "web_app": {"url": webapp_url}}
                ]]
            } if q else None
            send_msg(chat_id,
                f"Pareeksha Gurukul - Mock Interview Platform\n\n"
                f"Namaste {name}!\n\n"
                + (
                    "Tap the button below to submit your answer for today's question.\n"
                    "Your score will be sent here after evaluation."
                    if q else
                    "No active question right now. Check back soon!"
                ),
                markup=markup
            )

    # ── /setquestion ──────────────────────────────────────────────────────────
    elif cmd == "/setquestion":
        if not is_admin(user_id):
            send_msg(chat_id, "You are not authorized.")
            return
        parts = text.split(None, 1)
        if len(parts) < 2 or not parts[1].strip():
            send_msg(chat_id,
                "Set Today's Question\n\n"
                "Usage: /setquestion Your question text here\n\n"
                "Example:\n/setquestion Tell us about yourself in 45-60 seconds"
            )
            return
        new_q = parts[1].strip()
        set_question(new_q)
        send_msg(chat_id,
            f"Question set!\n\n{new_q}\n\nUse /broadcast to notify all students."
        )
        log.info(f"Admin {name} ({user_id}) set question: {new_q[:60]}")

    # ── /clearquestion ────────────────────────────────────────────────────────
    elif cmd == "/clearquestion":
        if not is_admin(user_id):
            send_msg(chat_id, "You are not authorized.")
            return
        clear_question()
        send_msg(chat_id, "Question cleared. Submissions are now disabled.")

    # ── /viewquestion ─────────────────────────────────────────────────────────
    elif cmd == "/viewquestion":
        if not is_admin(user_id):
            send_msg(chat_id, "You are not authorized.")
            return
        q = get_question()
        send_msg(chat_id,
            f"Current Question:\n\n{q}" if q else "No active question."
        )

    # ── /stats ────────────────────────────────────────────────────────────────
    elif cmd == "/stats":
        if not is_admin(user_id):
            send_msg(chat_id, "You are not authorized.")
            return
        q, students, subs, by_type = get_stats()
        q_prev  = (q[:80] + "...") if len(q) > 80 else (q or "Not set")
        recent  = "\n".join([
            f"- {v.get('name','?')} ({v.get('answer_type','?').upper()})"
            for v in list(subs.values())[-10:]
        ]) or "None yet"
        send_msg(chat_id,
            f"Submission Stats\n\n"
            f"Question: {q_prev}\n\n"
            f"Registered students: {len(students)}\n"
            f"Total submissions: {len(subs)}\n"
            f"Text: {by_type['text']}\n"
            f"Audio: {by_type['audio']}\n"
            f"Video: {by_type['video']}\n\n"
            f"Recent submissions:\n{recent}"
        )

    # ── /resetsubmissions ─────────────────────────────────────────────────────
    elif cmd == "/resetsubmissions":
        if not is_admin(user_id):
            send_msg(chat_id, "You are not authorized.")
            return
        count = reset_submissions()
        send_msg(chat_id, f"Reset {count} submission(s). Students can now resubmit.")

    # ── /broadcast ────────────────────────────────────────────────────────────
    elif cmd == "/broadcast":
        if not is_admin(user_id):
            send_msg(chat_id, "You are not authorized.")
            return
        q        = get_question()
        students = get_students()
        if not q:
            send_msg(chat_id, "No active question. Use /setquestion first.")
            return
        if not students:
            send_msg(chat_id, "No students registered yet. They need to /start the bot first.")
            return
        send_msg(chat_id, f"Broadcasting to {len(students)} student(s)... please wait.")
        run_bg(_broadcast, students, q)

    # ── /help ─────────────────────────────────────────────────────────────────
    elif cmd == "/help":
        if is_admin(user_id):
            send_msg(chat_id,
                "Admin Commands:\n\n"
                "/setquestion <text> - Set today's question\n"
                "/clearquestion - Disable submissions\n"
                "/viewquestion - See active question\n"
                "/stats - Submission count and breakdown\n"
                "/resetsubmissions - Allow re-submissions\n"
                "/broadcast - Notify all students\n"
                "/start - Admin dashboard"
            )
        else:
            send_msg(chat_id,
                "How it works:\n\n"
                "1. Tap Submit Your Answer\n"
                "2. Choose Text, Audio, or Video\n"
                "3. Submit your answer\n"
                "4. Get your star rating here in DM\n\n"
                "Use /start to open the submission form."
            )

    else:
        if msg.get("chat", {}).get("type") == "private":
            send_msg(chat_id, "Use /start to get started or /help for commands.")


def _broadcast(students, question):
    success, fail = 0, 0
    for sid, sname in students.items():
        url    = f"{WEBAPP_URL}?uid={sid}&name={sname}"
        markup = {
            "inline_keyboard": [[
                {"text": "Submit Your Answer",
                 "web_app": {"url": url}}
            ]]
        }
        resp = send_msg(sid,
            f"New Question Available!\n\n{question}\n\nTap below to submit your answer.",
            markup=markup
        )
        if resp.get("ok"):
            success += 1
        else:
            fail += 1
    log.info(f"Broadcast done: {success} sent, {fail} failed")


# ── REGISTER WEBHOOK ──────────────────────────────────────────────────────────
def register_webhook():
    if not BOT_TOKEN or not WEBAPP_URL:
        log.warning("BOT_TOKEN or WEBAPP_URL not set - skipping webhook registration")
        return
    hook_url = f"{WEBAPP_URL}/webhook"
    resp = tg_call("setWebhook", {
        "url":                  hook_url,
        "drop_pending_updates": True,
        "max_connections":      10,
    })
    if resp.get("ok"):
        log.info(f"Webhook set: {hook_url}")
    else:
        log.error(f"Webhook failed: {resp}")


# ── ENTRY POINT ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    register_webhook()
    log.info(f"Starting on port {PORT}")
    app.run(host="0.0.0.0", port=PORT, threaded=True)
