"""
Pareeksha Gurukul - Unified Bot + Server
Single process: Flask server handles webhook + student submissions
Admin commands built into the bot
Deploy on Railway with: python main.py
"""

import os
import json
import logging
import threading
from flask import Flask, request, jsonify, send_from_directory
import requests
from dotenv import load_dotenv

load_dotenv()

# ── LOGGING ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ── CONFIG ───────────────────────────────────────────────────────────────────
BOT_TOKEN     = os.getenv("BOT_TOKEN", "")
EVAL_GROUP_ID = os.getenv("EVAL_GROUP_ID", "")   # e.g. -1001234567890
WEBAPP_URL    = os.getenv("WEBAPP_URL", "")       # https://your-app.up.railway.app
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")       # comma-separated Telegram user IDs
PORT          = int(os.getenv("PORT", 8080))

ADMIN_IDS = [a.strip() for a in ADMIN_IDS_RAW.split(",") if a.strip()]

TG = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ── STATE (in-memory, survives restarts via question.txt) ────────────────────
DATA_FILE = "data.json"

def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"question": "", "submissions": {}}

def save_data(d):
    with open(DATA_FILE, "w") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)

data = load_data()
# data.question      → today's active question string
# data.submissions   → {student_id: {name, type, answered: True}}

# ── TELEGRAM HELPERS ─────────────────────────────────────────────────────────
def tg(method, payload=None, files=None):
    url = f"{TG}/{method}"
    try:
        if files:
            r = requests.post(url, data=payload, files=files, timeout=30)
        else:
            r = requests.post(url, json=payload, timeout=30)
        return r.json()
    except Exception as e:
        log.error(f"Telegram API error [{method}]: {e}")
        return {}

def send(chat_id, text, markup=None, parse_mode="Markdown"):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if markup:
        payload["reply_markup"] = markup
    return tg("sendMessage", payload)

def is_admin(user_id):
    return str(user_id) in ADMIN_IDS

# ── FLASK APP ────────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder="static")

# Serve index.html and static files
@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/health")
def health():
    return jsonify({"ok": True, "status": "running", "question_set": bool(data.get("question"))})

@app.route("/question")
def get_question():
    q = data.get("question", "")
    return jsonify({"ok": True, "question": q, "active": bool(q)})

@app.route("/submit", methods=["POST"])
def submit():
    student_id   = request.form.get("student_id", "").strip()
    student_name = request.form.get("student_name", "Student").strip()
    answer_type  = request.form.get("answer_type", "text").strip()
    text_answer  = request.form.get("text_answer", "").strip()
    question     = request.form.get("question", data.get("question", "N/A")).strip()

    if not student_id:
        return jsonify({"ok": False, "error": "Missing student ID"}), 400
    if not BOT_TOKEN or not EVAL_GROUP_ID:
        return jsonify({"ok": False, "error": "Server not configured"}), 500
    if not data.get("question"):
        return jsonify({"ok": False, "error": "No active question"}), 400

    # Prevent duplicate submissions
    if student_id in data.get("submissions", {}):
        return jsonify({"ok": False, "error": "Already submitted"}), 400

    # Build eval group caption
    caption = (
        f"📋 *New Submission*\n"
        f"👤 *Student:* {student_name} (`{student_id}`)\n"
        f"❓ *Question:* {question}\n"
        f"📝 *Type:* {answer_type.upper()}"
    )
    if answer_type == "text":
        # Telegram caption limit is 1024, message limit 4096
        answer_preview = text_answer[:3000]
        caption += f"\n\n💬 *Answer:*\n{answer_preview}"

    rating_markup = json.dumps({
        "inline_keyboard": [[
            {"text": "⭐ 1", "callback_data": f"rate|{student_id}|1"},
            {"text": "⭐ 2", "callback_data": f"rate|{student_id}|2"},
            {"text": "⭐ 3", "callback_data": f"rate|{student_id}|3"},
            {"text": "⭐ 4", "callback_data": f"rate|{student_id}|4"},
            {"text": "⭐ 5", "callback_data": f"rate|{student_id}|5"},
        ]]
    })

    result = None

    if answer_type == "text":
        resp = tg("sendMessage", {
            "chat_id": EVAL_GROUP_ID,
            "text": caption,
            "parse_mode": "Markdown",
            "reply_markup": json.loads(rating_markup)
        })
        result = resp.get("result")

    else:
        file = request.files.get("file")
        if not file:
            return jsonify({"ok": False, "error": "No file uploaded"}), 400

        file_bytes = file.read()
        mime       = file.content_type or "application/octet-stream"
        fname      = file.filename or f"answer.{answer_type}"

        tg_method = "sendAudio" if answer_type == "audio" else "sendVideo"
        field_key  = "audio"    if answer_type == "audio" else "video"

        # Caption for media is max 1024 chars
        if len(caption) > 1024:
            caption = caption[:1020] + "…"

        resp = tg(
            tg_method,
            payload={
                "chat_id":      EVAL_GROUP_ID,
                "caption":      caption,
                "parse_mode":   "Markdown",
                "reply_markup": rating_markup,
            },
            files={field_key: (fname, file_bytes, mime)}
        )
        result = resp.get("result")

    if not result:
        log.error(f"Failed to forward to eval group: {resp}")
        return jsonify({"ok": False, "error": "Failed to forward to evaluators"}), 500

    # Record submission
    subs = data.setdefault("submissions", {})
    subs[student_id] = {
        "name":        student_name,
        "answer_type": answer_type,
        "msg_id":      result.get("message_id"),
    }
    save_data(data)

    log.info(f"Submission recorded: {student_name} ({student_id}), type={answer_type}")
    return jsonify({"ok": True, "message": "Submitted successfully!"})


# ── TELEGRAM WEBHOOK ─────────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json(silent=True)
    if not update:
        return "ok"

    # ── Callback query (star rating) ─────────────────────────────────────────
    cb = update.get("callback_query")
    if cb:
        cb_data    = cb.get("data", "")
        evaluator  = cb.get("from", {}).get("first_name", "Evaluator")
        msg        = cb.get("message", {})
        chat_id    = msg.get("chat", {}).get("id")
        message_id = msg.get("message_id")

        if cb_data.startswith("rate|"):
            parts = cb_data.split("|")
            if len(parts) == 3:
                _, student_id, stars_str = parts
                stars    = int(stars_str)
                star_str = "⭐" * stars

                # DM the student
                tg("sendMessage", {
                    "chat_id":    student_id,
                    "text": (
                        f"🎉 *Your Answer Has Been Evaluated!*\n\n"
                        f"Rating: {star_str} *({stars}/5)*\n"
                        f"Evaluated by: {evaluator}\n\n"
                        f"Keep up the great work! 💪\n"
                        f"Send /start to submit tomorrow's answer."
                    ),
                    "parse_mode": "Markdown"
                })

                # Update group message buttons
                tg("editMessageReplyMarkup", {
                    "chat_id":    chat_id,
                    "message_id": message_id,
                    "reply_markup": {
                        "inline_keyboard": [[
                            {"text": f"✅ Rated {star_str} by {evaluator}", "callback_data": "done"}
                        ]]
                    }
                })

                # Ack the button press
                tg("answerCallbackQuery", {
                    "callback_query_id": cb["id"],
                    "text": f"Rated {stars}/5 ✅  Student notified!"
                })

                log.info(f"Rated student {student_id}: {stars}/5 by {evaluator}")

        elif cb_data == "done":
            tg("answerCallbackQuery", {"callback_query_id": cb["id"], "text": "Already rated."})

        return "ok"

    # ── Message commands ─────────────────────────────────────────────────────
    msg  = update.get("message", {})
    if not msg:
        return "ok"

    text    = msg.get("text", "").strip()
    user    = msg.get("from", {})
    user_id = str(user.get("id", ""))
    name    = user.get("first_name", "there")
    chat_id = msg.get("chat", {}).get("id")

    if not text:
        return "ok"

    cmd = text.split()[0].split("@")[0].lower()

    # ── /start ───────────────────────────────────────────────────────────────
    if cmd == "/start":
        webapp_url = f"{WEBAPP_URL}?uid={user_id}&name={name}"
        has_q      = bool(data.get("question"))

        if is_admin(user_id):
            # Admin gets a different menu
            send(chat_id,
                f"👋 Welcome back, *{name}* (Admin)\n\n"
                f"*Admin Commands:*\n"
                f"📝 /setquestion — Set today's question\n"
                f"❌ /clearquestion — Clear current question\n"
                f"📊 /stats — View submission stats\n"
                f"📋 /viewquestion — See current question\n"
                f"🔄 /resetsubmissions — Reset all submissions\n\n"
                f"*Current question:* {'✅ Set' if has_q else '❌ Not set'}"
            )
        else:
            markup = {
                "inline_keyboard": [[
                    {
                        "text": "📝 Submit Your Answer",
                        "web_app": {"url": webapp_url}
                    }
                ]]
            } if has_q else None

            send(chat_id,
                f"🎓 *Pareeksha Gurukul*\n"
                f"_Mock Interview Platform_\n\n"
                f"Namaste {name}! 🙏\n\n"
                + (
                    "Tap below to submit your answer for today's question.\n\n"
                    "📌 _Your score will be sent here after evaluation._"
                    if has_q else
                    "⏳ No active question right now.\nCheck back soon!"
                ),
                markup=markup
            )

    # ── /setquestion ─────────────────────────────────────────────────────────
    elif cmd == "/setquestion":
        if not is_admin(user_id):
            send(chat_id, "❌ You are not authorized to use this command.")
            return "ok"

        parts = text.split(None, 1)
        if len(parts) < 2 or not parts[1].strip():
            send(chat_id,
                "📝 *Set Today's Question*\n\n"
                "Usage:\n`/setquestion Your question text here`\n\n"
                "Example:\n`/setquestion भारत के रेलवे बोर्ड की संरचना का वर्णन करें।`"
            )
            return "ok"

        new_q = parts[1].strip()
        data["question"] = new_q
        # Reset submissions when question changes
        data["submissions"] = {}
        save_data(data)

        send(chat_id,
            f"✅ *Question set successfully!*\n\n"
            f"❓ *Question:*\n{new_q}\n\n"
            f"Students can now submit answers via /start"
        )
        log.info(f"Admin {name} ({user_id}) set question: {new_q[:60]}")

    # ── /clearquestion ───────────────────────────────────────────────────────
    elif cmd == "/clearquestion":
        if not is_admin(user_id):
            send(chat_id, "❌ You are not authorized to use this command.")
            return "ok"

        data["question"] = ""
        save_data(data)
        send(chat_id, "✅ Question cleared. No new submissions will be accepted.")

    # ── /viewquestion ────────────────────────────────────────────────────────
    elif cmd == "/viewquestion":
        if not is_admin(user_id):
            send(chat_id, "❌ You are not authorized to use this command.")
            return "ok"

        q = data.get("question", "")
        if q:
            send(chat_id, f"📋 *Current Question:*\n\n{q}")
        else:
            send(chat_id, "❌ No active question set.")

    # ── /stats ───────────────────────────────────────────────────────────────
    elif cmd == "/stats":
        if not is_admin(user_id):
            send(chat_id, "❌ You are not authorized to use this command.")
            return "ok"

        subs  = data.get("submissions", {})
        total = len(subs)
        by_type = {"text": 0, "audio": 0, "video": 0}
        for s in subs.values():
            t = s.get("answer_type", "text")
            by_type[t] = by_type.get(t, 0) + 1

        q = data.get("question", "Not set")
        q_preview = q[:80] + "…" if len(q) > 80 else q

        send(chat_id,
            f"📊 *Submission Stats*\n\n"
            f"❓ *Question:* {q_preview}\n\n"
            f"📥 *Total Submissions:* {total}\n"
            f"✍️ Text: {by_type['text']}\n"
            f"🎙️ Audio: {by_type['audio']}\n"
            f"🎬 Video: {by_type['video']}\n\n"
            + (
                "*Recent submissions:*\n" +
                "\n".join([
                    f"• {v.get('name', '?')} ({k}) — {v.get('answer_type','?').upper()}"
                    for k, v in list(subs.items())[-10:]
                ])
                if subs else "_No submissions yet._"
            )
        )

    # ── /resetsubmissions ────────────────────────────────────────────────────
    elif cmd == "/resetsubmissions":
        if not is_admin(user_id):
            send(chat_id, "❌ You are not authorized to use this command.")
            return "ok"

        count = len(data.get("submissions", {}))
        data["submissions"] = {}
        save_data(data)
        send(chat_id, f"🔄 Reset {count} submission record(s). Students can submit again.")

    # ── /help ────────────────────────────────────────────────────────────────
    elif cmd == "/help":
        if is_admin(user_id):
            send(chat_id,
                "🛠️ *Admin Commands:*\n\n"
                "/setquestion `<text>` — Set today's question\n"
                "/clearquestion — Disable submissions\n"
                "/viewquestion — See active question\n"
                "/stats — Submission count & breakdown\n"
                "/resetsubmissions — Allow re-submissions\n"
                "/start — Admin dashboard"
            )
        else:
            send(chat_id,
                "📖 *How it works:*\n\n"
                "1. Click 'Submit Your Answer'\n"
                "2. Choose Text, Audio or Video\n"
                "3. Submit your answer\n"
                "4. Get your rating here in DM ⭐\n\n"
                "Use /start to open the submission form."
            )

    return "ok"


# ── REGISTER WEBHOOK ─────────────────────────────────────────────────────────
def register_webhook():
    if not BOT_TOKEN or not WEBAPP_URL:
        log.warning("BOT_TOKEN or WEBAPP_URL not set — skipping webhook registration")
        return
    base = WEBAPP_URL.rstrip("/")
    hook_url = f"{base}/webhook"
    resp = tg("setWebhook", {"url": hook_url, "drop_pending_updates": True})
    if resp.get("ok"):
        log.info(f"Webhook registered: {hook_url}")
    else:
        log.error(f"Webhook registration failed: {resp}")


# ── ENTRY POINT ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    register_webhook()
    log.info(f"Starting server on port {PORT}")
    # Use threaded=True so file uploads don't block webhook
    app.run(host="0.0.0.0", port=PORT, threaded=True)
