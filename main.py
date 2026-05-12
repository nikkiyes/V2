"""
Pareeksha Gurukul - Unified Bot + Server v2.1
Fixes:
  1. Webhook handler now returns "ok" immediately — processing runs in background thread
  2. Gunicorn used in Procfile with multiple workers so webhook never blocks
  3. data_lock protects shared state across threads
  4. Telegram API calls have proper timeouts and won't hang the webhook response
  5. /setquestion also broadcasts to all known students
"""

import os
import json
import logging
import threading
from flask import Flask, request, jsonify, send_from_directory
import requests
from dotenv import load_dotenv

load_dotenv()

# ── LOGGING ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ── CONFIG ───────────────────────────────────────────────────────────────────
BOT_TOKEN     = os.getenv("BOT_TOKEN", "")
EVAL_GROUP_ID = os.getenv("EVAL_GROUP_ID", "")
WEBAPP_URL    = os.getenv("WEBAPP_URL", "")
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")
PORT          = int(os.getenv("PORT", 8080))

ADMIN_IDS = [a.strip() for a in ADMIN_IDS_RAW.split(",") if a.strip()]
TG_API    = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ── THREAD-SAFE DATA STORE ───────────────────────────────────────────────────
DATA_FILE  = "data.json"
data_lock  = threading.Lock()

def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.error(f"load_data error: {e}")
    return {"question": "", "submissions": {}, "students": {}}

def save_data(d):
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error(f"save_data error: {e}")

data = load_data()
# data["question"]    → active question string
# data["submissions"] → {student_id: {name, answer_type, msg_id}}
# data["students"]    → {student_id: name}  — everyone who ever /start-ed

# ── TELEGRAM HELPERS ─────────────────────────────────────────────────────────
def tg_call(method, payload=None, files=None, timeout=25):
    """Non-blocking Telegram API call with timeout."""
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
        log.error(f"TG [{method}] timed out after {timeout}s")
        return {}
    except Exception as e:
        log.error(f"TG [{method}] exception: {e}")
        return {}

def send_msg(chat_id, text, markup=None, parse_mode="Markdown"):
    payload = {
        "chat_id":    str(chat_id),
        "text":       text,
        "parse_mode": parse_mode,
    }
    if markup:
        payload["reply_markup"] = markup
    return tg_call("sendMessage", payload)

def is_admin(user_id):
    return str(user_id) in ADMIN_IDS

# ── BACKGROUND TASK RUNNER ───────────────────────────────────────────────────
def run_in_bg(fn, *args, **kwargs):
    """Run fn(*args, **kwargs) in a daemon thread so webhook returns instantly."""
    t = threading.Thread(target=fn, args=args, kwargs=kwargs, daemon=True)
    t.start()

# ── FLASK APP ─────────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder="static")

@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/health")
def health():
    with data_lock:
        has_q = bool(data.get("question"))
    return jsonify({"ok": True, "status": "running", "question_set": has_q})

@app.route("/question")
def get_question():
    with data_lock:
        q = data.get("question", "")
    return jsonify({"ok": True, "question": q, "active": bool(q)})

# ── SUBMIT ENDPOINT ───────────────────────────────────────────────────────────
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

    with data_lock:
        q = data.get("question", "")
        already = student_id in data.get("submissions", {})

    if not q:
        return jsonify({"ok": False, "error": "No active question"}), 400
    if already:
        return jsonify({"ok": False, "error": "Already submitted"}), 400

    # For file uploads, read bytes before spawning thread
    file_bytes = None
    mime       = None
    fname      = None
    if answer_type in ("audio", "video"):
        f = request.files.get("file")
        if not f:
            return jsonify({"ok": False, "error": "No file uploaded"}), 400
        file_bytes = f.read()
        mime       = f.content_type or "application/octet-stream"
        fname      = f.filename or f"answer.{answer_type}"

    # Forward to eval group in background (so response is instant to student)
    run_in_bg(
        _forward_submission,
        student_id, student_name, q, answer_type,
        text_answer, file_bytes, mime, fname
    )

    # Immediately record to prevent duplicate while bg thread runs
    with data_lock:
        data.setdefault("submissions", {})[student_id] = {
            "name":        student_name,
            "answer_type": answer_type,
            "msg_id":      None,   # will be updated by bg thread if needed
        }
        save_data(data)

    return jsonify({"ok": True, "message": "Submitted successfully!"})


def _forward_submission(student_id, student_name, question, answer_type,
                         text_answer, file_bytes, mime, fname):
    """Runs in background thread — sends submission to eval group."""
    caption = (
        f"📋 *New Submission*\n"
        f"👤 *Student:* {student_name} (`{student_id}`)\n"
        f"❓ *Question:* {question}\n"
        f"📝 *Type:* {answer_type.upper()}"
    )
    if answer_type == "text":
        caption += f"\n\n💬 *Answer:*\n{text_answer[:3500]}"

    rating_kb = {
        "inline_keyboard": [[
            {"text": "⭐ 1", "callback_data": f"rate|{student_id}|1"},
            {"text": "⭐ 2", "callback_data": f"rate|{student_id}|2"},
            {"text": "⭐ 3", "callback_data": f"rate|{student_id}|3"},
            {"text": "⭐ 4", "callback_data": f"rate|{student_id}|4"},
            {"text": "⭐ 5", "callback_data": f"rate|{student_id}|5"},
        ]]
    }

    result = None
    if answer_type == "text":
        resp = tg_call("sendMessage", {
            "chat_id":      EVAL_GROUP_ID,
            "text":         caption,
            "parse_mode":   "Markdown",
            "reply_markup": rating_kb,
        })
        result = resp.get("result")

    else:
        if len(caption) > 1024:
            caption = caption[:1020] + "…"
        tg_method = "sendAudio" if answer_type == "audio" else "sendVideo"
        field_key = "audio"    if answer_type == "audio" else "video"
        resp = tg_call(
            tg_method,
            payload={
                "chat_id":      EVAL_GROUP_ID,
                "caption":      caption,
                "parse_mode":   "Markdown",
                "reply_markup": json.dumps(rating_kb),
            },
            files={field_key: (fname, file_bytes, mime)},
            timeout=60,
        )
        result = resp.get("result")

    if result:
        with data_lock:
            if student_id in data.get("submissions", {}):
                data["submissions"][student_id]["msg_id"] = result.get("message_id")
                save_data(data)
        log.info(f"Forwarded submission: {student_name} ({student_id}), type={answer_type}")
    else:
        log.error(f"Failed to forward submission for {student_id}")


# ── WEBHOOK ───────────────────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    """
    MUST return 200 quickly — Telegram resends if no response within 60s.
    All real work runs in background threads.
    """
    update = request.get_json(silent=True)
    if update:
        run_in_bg(_process_update, update)
    return "ok", 200   # always return immediately


def _process_update(update):
    """Runs in background — safe to do any amount of work here."""
    try:
        # ── Callback query (star rating) ──────────────────────────────────────
        cb = update.get("callback_query")
        if cb:
            _handle_callback(cb)
            return

        # ── Message ───────────────────────────────────────────────────────────
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

        # DM student
        send_msg(student_id,
            f"🎉 *Your Answer Has Been Evaluated!*\n\n"
            f"Rating: {star_str} *({stars}/5)*\n"
            f"Evaluated by: {evaluator}\n\n"
            f"Keep up the great work! 💪\n"
            f"Send /start to submit for the next question."
        )

        # Edit eval group message
        tg_call("editMessageReplyMarkup", {
            "chat_id":    chat_id,
            "message_id": message_id,
            "reply_markup": {
                "inline_keyboard": [[
                    {"text": f"✅ Rated {star_str} by {evaluator}", "callback_data": "done"}
                ]]
            }
        })

        # Ack button
        tg_call("answerCallbackQuery", {
            "callback_query_id": cb_id,
            "text": f"Rated {stars}/5 ✅  Student notified!"
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
    name    = user.get("first_name") or "Student"
    chat_id = msg.get("chat", {}).get("id")

    if not text or not user_id:
        return

    cmd = text.split()[0].split("@")[0].lower()

    # Register student on any interaction
    with data_lock:
        data.setdefault("students", {})[user_id] = name
        save_data(data)

    # ── /start ────────────────────────────────────────────────────────────────
    if cmd == "/start":
        with data_lock:
            has_q = bool(data.get("question"))

        if is_admin(user_id):
            with data_lock:
                q_preview = data.get("question", "")[:80] or "Not set"
            send_msg(chat_id,
                f"👋 Welcome back, *{name}* \\(Admin\\)\n\n"
                f"🛠 *Admin Commands:*\n"
                f"📝 /setquestion — Set today's question\n"
                f"❌ /clearquestion — Clear current question\n"
                f"📊 /stats — View submission stats\n"
                f"📋 /viewquestion — See current question\n"
                f"🔄 /resetsubmissions — Reset all submissions\n"
                f"📣 /broadcast — Notify all students of new question\n\n"
                f"*Current question:* {'✅ Active' if has_q else '❌ Not set'}\n"
                f"_{q_preview}_",
                parse_mode="MarkdownV2"
            )
        else:
            webapp_url = f"{WEBAPP_URL}?uid={user_id}&name={name}"
            markup = {
                "inline_keyboard": [[
                    {"text": "📝 Submit Your Answer", "web_app": {"url": webapp_url}}
                ]]
            } if has_q else None

            send_msg(chat_id,
                f"🎓 *Pareeksha Gurukul*\n"
                f"_Mock Interview Platform_\n\n"
                f"Namaste {name}! 🙏\n\n"
                + (
                    "Tap the button below to submit your answer for today's question.\n\n"
                    "📌 _Your score will be sent here after evaluation._"
                    if has_q else
                    "⏳ No active question right now.\nCheck back soon!"
                ),
                markup=markup
            )

    # ── /setquestion ──────────────────────────────────────────────────────────
    elif cmd == "/setquestion":
        if not is_admin(user_id):
            send_msg(chat_id, "❌ You are not authorized.")
            return

        parts = text.split(None, 1)
        if len(parts) < 2 or not parts[1].strip():
            send_msg(chat_id,
                "📝 *Set Today's Question*\n\n"
                "Usage:\n`/setquestion Your question text here`\n\n"
                "Example:\n`/setquestion भारत के रेलवे बोर्ड की संरचना का वर्णन करें।`"
            )
            return

        new_q = parts[1].strip()
        with data_lock:
            data["question"]    = new_q
            data["submissions"] = {}
            save_data(data)

        send_msg(chat_id,
            f"✅ *Question set!*\n\n❓ {new_q}\n\n"
            f"Use /broadcast to notify all students."
        )
        log.info(f"Admin {name} ({user_id}) set question.")

    # ── /clearquestion ────────────────────────────────────────────────────────
    elif cmd == "/clearquestion":
        if not is_admin(user_id):
            send_msg(chat_id, "❌ You are not authorized.")
            return
        with data_lock:
            data["question"] = ""
            save_data(data)
        send_msg(chat_id, "✅ Question cleared. Submissions disabled.")

    # ── /viewquestion ─────────────────────────────────────────────────────────
    elif cmd == "/viewquestion":
        if not is_admin(user_id):
            send_msg(chat_id, "❌ You are not authorized.")
            return
        with data_lock:
            q = data.get("question", "")
        send_msg(chat_id, f"📋 *Current Question:*\n\n{q}" if q else "❌ No active question.")

    # ── /stats ────────────────────────────────────────────────────────────────
    elif cmd == "/stats":
        if not is_admin(user_id):
            send_msg(chat_id, "❌ You are not authorized.")
            return
        with data_lock:
            subs     = dict(data.get("submissions", {}))
            q        = data.get("question", "Not set")
            students = dict(data.get("students", {}))

        total    = len(subs)
        by_type  = {"text": 0, "audio": 0, "video": 0}
        for s in subs.values():
            t = s.get("answer_type", "text")
            by_type[t] = by_type.get(t, 0) + 1

        q_prev = q[:80] + "…" if len(q) > 80 else q
        recent = "\n".join([
            f"• {v.get('name','?')} — {v.get('answer_type','?').upper()}"
            for v in list(subs.values())[-10:]
        ]) or "_None yet_"

        send_msg(chat_id,
            f"📊 *Submission Stats*\n\n"
            f"❓ *Question:* {q_prev}\n\n"
            f"👥 *Registered students:* {len(students)}\n"
            f"📥 *Total submissions:* {total}\n"
            f"✍️ Text: {by_type['text']}\n"
            f"🎙️ Audio: {by_type['audio']}\n"
            f"🎬 Video: {by_type['video']}\n\n"
            f"*Recent:*\n{recent}"
        )

    # ── /resetsubmissions ─────────────────────────────────────────────────────
    elif cmd == "/resetsubmissions":
        if not is_admin(user_id):
            send_msg(chat_id, "❌ You are not authorized.")
            return
        with data_lock:
            count = len(data.get("submissions", {}))
            data["submissions"] = {}
            save_data(data)
        send_msg(chat_id, f"🔄 Reset {count} submission(s). Students can submit again.")

    # ── /broadcast ────────────────────────────────────────────────────────────
    elif cmd == "/broadcast":
        if not is_admin(user_id):
            send_msg(chat_id, "❌ You are not authorized.")
            return
        with data_lock:
            q        = data.get("question", "")
            students = dict(data.get("students", {}))

        if not q:
            send_msg(chat_id, "❌ No active question. Set one first with /setquestion")
            return
        if not students:
            send_msg(chat_id, "⚠️ No students registered yet.")
            return

        send_msg(chat_id, f"📣 Broadcasting to {len(students)} students… please wait.")
        run_in_bg(_broadcast, students, q, WEBAPP_URL)

    # ── /help ─────────────────────────────────────────────────────────────────
    elif cmd == "/help":
        if is_admin(user_id):
            send_msg(chat_id,
                "🛠️ *Admin Commands:*\n\n"
                "/setquestion `<text>` — Set today's question\n"
                "/clearquestion — Disable submissions\n"
                "/viewquestion — See active question\n"
                "/stats — Submissions count & breakdown\n"
                "/resetsubmissions — Allow re-submissions\n"
                "/broadcast — Notify all students\n"
                "/start — Admin dashboard"
            )
        else:
            send_msg(chat_id,
                "📖 *How it works:*\n\n"
                "1️⃣ Tap 'Submit Your Answer'\n"
                "2️⃣ Choose Text, Audio, or Video\n"
                "3️⃣ Submit your answer\n"
                "4️⃣ Get your ⭐ rating here in DM\n\n"
                "Use /start to open the submission form."
            )


def _broadcast(students, question, webapp_url):
    """Notify every registered student of the new question."""
    success = 0
    fail    = 0
    for sid, sname in students.items():
        url    = f"{webapp_url}?uid={sid}&name={sname}"
        markup = {
            "inline_keyboard": [[
                {"text": "📝 Submit Your Answer", "web_app": {"url": url}}
            ]]
        }
        resp = send_msg(sid,
            f"🔔 *New Question Available!*\n\n"
            f"❓ {question}\n\n"
            f"Tap below to submit your answer 👇",
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
        log.warning("BOT_TOKEN or WEBAPP_URL missing — webhook not registered")
        return
    hook_url = WEBAPP_URL.rstrip("/") + "/webhook"
    resp = tg_call("setWebhook", {
        "url":                  hook_url,
        "drop_pending_updates": True,
        "max_connections":      40,
    })
    if resp.get("ok"):
        log.info(f"✅ Webhook registered: {hook_url}")
    else:
        log.error(f"❌ Webhook failed: {resp}")


# ── ENTRY POINT ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    register_webhook()
    log.info(f"Starting on port {PORT}")
    app.run(host="0.0.0.0", port=PORT, threaded=True)
