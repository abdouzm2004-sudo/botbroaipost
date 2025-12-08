# main.py
import os
import io
import json
import time
import tempfile
import logging
from typing import Dict, Any, List, Optional
from datetime import datetime, time as dtime

import pytz
from dotenv import load_dotenv
from fastapi import FastAPI, Request
import uvicorn

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# Gemini (Google generative AI)
import google.generativeai as genai

# Google APIs
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload
from googleapiclient.errors import HttpError

# ---------------- Config / Globals ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
WEBHOOK_BASE = os.getenv("WEBHOOK_BASE", "").rstrip("/")  # e.g. https://your-app.onrender.com
PORT = int(os.getenv("PORT", "8000"))

if not TELEGRAM_BOT_TOKEN or not GOOGLE_API_KEY:
    raise RuntimeError("يرجى ضبط TELEGRAM_BOT_TOKEN و GOOGLE_API_KEY في Environment variables.")

genai.configure(api_key=GOOGLE_API_KEY)
GEMINI_MODEL = "gemini-2.5-flash"
TZ = pytz.timezone("Africa/Algiers")

STATE_FILE = "user_state.json"
USER_STATE: Dict[int, Dict[str, Any]] = {}

# Telegram Application will be created at startup
fastapp = FastAPI()
app_telegram = None  # type: ignore

# ---------------- State helpers ----------------
def load_state():
    global USER_STATE
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                USER_STATE = json.load(f)
            logging.info("✅ تم تحميل الحالة من user_state.json")
        except Exception as e:
            logging.warning(f"⚠️ فشل تحميل الحالة: {e}")
            USER_STATE = {}
    else:
        logging.info("📁 لا يوجد ملف حالة، سيتم البدء من جديد")
        USER_STATE = {}


def save_state():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(USER_STATE, f, ensure_ascii=False, indent=2)
        logging.info("💾 تم حفظ الحالة في user_state.json")
    except Exception as e:
        logging.error(f"❌ فشل حفظ الحالة: {e}")


def get_chat(chat_id: int) -> Dict[str, Any]:
    if chat_id not in USER_STATE:
        USER_STATE[chat_id] = {
            "next": "await_json",
            "oauth_json": None,
            "refresh_token": None,
            "drive_folder_id": None,
            "save_info": False,
            "setup_complete": False,
            "autopost_enabled": False,
            "autopost_count": 0,
            "autopost_times": []
        }
    return USER_STATE[chat_id]

# ---------------- Keyboards ----------------
def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("انشر الآن", callback_data="publish_now")],
         [InlineKeyboardButton("🔘 ضبط النشر التلقائي", callback_data="autopost_setup")],
         [InlineKeyboardButton("📋 عرض الإعدادات الحالية", callback_data="show_settings")],
         [InlineKeyboardButton("🔄 إعادة الإعداد من جديد", callback_data="reset_setup")]]
    )


def yes_no_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("نعم", callback_data="save_yes"),
                                  InlineKeyboardButton("لا", callback_data="save_no")]])


def after_publish_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("📋 عرض الإعدادات الحالية", callback_data="show_settings")],
                                 [InlineKeyboardButton("🔄 إعادة الإعداد من جديد", callback_data="reset_setup")]])


def autopost_control_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⛔ إيقاف النشر التلقائي", callback_data="autopost_stop")]])


# ---------------- Google / Drive helpers ----------------
def extract_oauth_fields(oauth_json: Dict[str, Any]) -> Dict[str, str]:
    block = oauth_json.get("installed") or oauth_json.get("web")
    if not block:
        raise RuntimeError("ملف JSON لا يحتوي على 'installed' أو 'web'.")
    for key in ("client_id", "client_secret", "token_uri"):
        if key not in block:
            raise RuntimeError(f"ملف JSON ناقص الحقل: {key}")
    return {"client_id": block["client_id"], "client_secret": block["client_secret"], "token_uri": block["token_uri"]}


def build_services(cfg):
    fields = extract_oauth_fields(cfg["oauth_json"])
    creds = Credentials(None,
                        refresh_token=cfg["refresh_token"],
                        token_uri=fields["token_uri"],
                        client_id=fields["client_id"],
                        client_secret=fields["client_secret"],
                        scopes=[
                            "https://www.googleapis.com/auth/youtube.upload",
                            "https://www.googleapis.com/auth/drive"
                        ])
    drive = build("drive", "v3", credentials=creds)
    youtube = build("youtube", "v3", credentials=creds)
    return drive, youtube


def list_first_video_in_folder(drive, folder_id: str) -> Optional[Dict[str, Any]]:
    query = f"'{folder_id}' in parents and mimeType contains 'video/'"
    resp = drive.files().list(q=query, orderBy="createdTime", pageSize=10, fields="files(id,name,createdTime)").execute()
    files = resp.get("files", [])
    return files[0] if files else None


def download_drive_file(drive, file_id: str, filename: str) -> str:
    request = drive.files().get_media(fileId=file_id)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        status, done = downloader.next_chunk()
    temp_dir = tempfile.mkdtemp(prefix="dl_")
    ext = os.path.splitext(filename)[1] or ".mp4"
    local_path = os.path.join(temp_dir, f"video{ext}")
    with open(local_path, "wb") as f:
        f.write(fh.getvalue())
    return local_path


def delete_drive_file(drive, file_id: str):
    try:
        drive.files().delete(fileId=file_id).execute()
    except Exception as e:
        logging.error(f"Delete error: {e}")


# ---------------- Gemini metadata generation (kept as-is) ----------------
TRENDING_TAGS = ["Trending", "Viral", "Shorts", "AI", "Creative", "YouTube", "Funny", "Tech", "Magic", "Surprise"]


def infer_context_tags(filename: str) -> List[str]:
    name = (filename or "").lower()
    tags: List[str] = []
    if any(k in name for k in ["slime", "glitter", "goo"]):
        tags += ["Slime", "Glitter"]
    if any(k in name for k in ["cat", "kitten"]):
        tags += ["Kitten", "Cats", "PetLovers"]
    if any(k in name for k in ["wave", "ocean", "sea", "surf"]):
        tags += ["Ocean", "Wave", "Nature"]
    if any(k in name for k in ["car", "auto", "vehicle"]):
        tags += ["Car", "Automotive"]
    if any(k in name for k in ["transform", "morph", "change"]):
        tags += ["Transformation"]
    seen = set()
    out = []
    for t in tags:
        if t not in seen:
            out.append(t)
            seen.add(t)
    return out


def format_hashtags(trending: List[str], contextual: List[str], max_total: int = 10) -> str:
    mixed = trending[:5] + contextual[:5]
    if len(mixed) < max_total:
        for r in trending[5:] + contextual[5:]:
            if len(mixed) >= max_total:
                break
            if r not in mixed:
                mixed.append(r)
    return " ".join(f"#{t.replace(' ', '')}" for t in mixed)


def generate_metadata_with_gemini(video_path: str, filename_hint: str = "") -> Dict[str, str]:
    contextual = infer_context_tags(filename_hint)
    trending = TRENDING_TAGS.copy()
    try:
        uploaded = genai.upload_file(video_path)
        file_info = genai.get_file(uploaded.name)
        for _ in range(60):
            if file_info.state.name == "ACTIVE":
                break
            time.sleep(0.5)
            file_info = genai.get_file(uploaded.name)
        if file_info.state.name != "ACTIVE":
            raise RuntimeError("الملف لم يصبح ACTIVE بعد رفعه إلى Gemini.")
        prompt = (
            "Analyze the video and generate:\n"
            "1) A catchy English YouTube title (max 70 characters).\n"
            "2) A short English description (3-4 sentences) explaining what the viewer sees and why it's engaging.\n"
            "Return exactly:\nTitle: <title>\nDescription: <description>"
        )
        model = genai.GenerativeModel(GEMINI_MODEL)
        response = model.generate_content([file_info, prompt])
        text = (getattr(response, "text", "") or "").strip()
        title, desc = None, None
        for line in text.splitlines():
            low = line.lower()
            if low.startswith("title:"):
                title = line.split(":", 1)[1].strip()
            elif low.startswith("description:"):
                desc = line.split(":", 1)[1].strip()
        if not title:
            title = "AI Magic: Surprising Transformation!"
        if not desc:
            desc = "A stunning AI-powered visual with an unexpected twist that keeps you watching."
        title = title[:70].strip()
        hashtags = format_hashtags(trending, contextual, max_total=10)
        return {"title": title, "description": f"{desc}\n{hashtags}"}
    except Exception as e:
        logging.error(f"Gemini error: {e}")
        hashtags = format_hashtags(trending, contextual, max_total=10)
        return {"title": "AI Magic: Surprising Transformation!", "description": "Fallback description.\n" + hashtags}


# ---------------- YouTube upload ----------------
def upload_to_youtube(youtube, video_path: str, meta: Dict[str, str]) -> Optional[str]:
    try:
        body = {
            "snippet": {"title": meta["title"], "description": meta["description"], "categoryId": "22"},
            "status": {"privacyStatus": "public"}
        }
        media = MediaFileUpload(video_path, chunksize=-1, resumable=True)
        request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
        response = None
        tries = 0
        while response is None:
            status, response = request.next_chunk()
            tries += 1
            if tries > 200:
                raise RuntimeError("YouTube upload did not complete.")
        video_id = response.get("id")
        return f"https://www.youtube.com/watch?v={video_id}" if video_id else None
    except HttpError as e:
        if "uploadLimitExceeded" in str(e):
            return "ERR:uploadLimitExceeded"
        return f"ERR:HttpError {getattr(e, 'status_code', '')}"
    except Exception as e:
        return f"ERR:Generic {str(e)}"


# ---------------- Publish flow (kept async) ----------------
async def publish_now(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    cfg = get_chat(chat_id)
    folder_id = cfg.get("drive_folder_id")
    if not folder_id:
        await context.bot.send_message(chat_id, "❌ لم يتم ضبط معرف مجلد درايف بعد.")
        return

    try:
        drive, youtube = build_services(cfg)
        file = list_first_video_in_folder(drive, folder_id)
        if not file:
            await context.bot.send_message(chat_id, "❌ لا يوجد فيديوهات في هذا المجلد.")
            return

        await context.bot.send_message(chat_id, f"🔍 جاري التحليل وتوليد البيانات للفيديو: {file['name']}")
        local_path = download_drive_file(drive, file["id"], file["name"])

        meta = generate_metadata_with_gemini(local_path, filename_hint=file["name"])
        await context.bot.send_message(chat_id, f"⬆️ جاري الرفع إلى يوتيوب...\nTitle: {meta['title']}")

        url_or_err = upload_to_youtube(youtube, local_path, meta)

        if isinstance(url_or_err, str) and url_or_err.startswith("ERR:"):
            reason = url_or_err.split(":", 1)[1]
            msg = "❌ فشل رفع الفيديو."
            if reason == "uploadLimitExceeded":
                msg += "\nالسبب: تجاوز حد الرفع المؤقت في يوتيوب."
            else:
                msg += f"\nالسبب: {reason}."
            await context.bot.send_message(chat_id, msg, reply_markup=after_publish_keyboard())
            if cfg.get("autopost_enabled") and cfg.get("autopost_times"):
                next_time = next_scheduled_time_text(cfg["autopost_times"])
                await context.bot.send_message(chat_id, f"🗓️ النشر التلقائي مفعّل. موعد النشر القادم: {next_time}")
            return

        delete_drive_file(drive, file["id"])
        remaining = len(list_videos(drive, folder_id))
        await context.bot.send_message(chat_id,
                                       f"✅ تم النشر!\nعدد الفيديوات المتبقية: {remaining}\nرابط الفيديو: {url_or_err}",
                                       reply_markup=after_publish_keyboard())
        if cfg.get("autopost_enabled") and cfg.get("autopost_times"):
            next_time = next_scheduled_time_text(cfg["autopost_times"])
            await context.bot.send_message(chat_id, f"🗓️ النشر التلقائي مفعّل. موعد النشر القادم: {next_time}")

    except HttpError as e:
        logging.error(f"publish_now HttpError: {e}")
        await context.bot.send_message(chat_id, f"❌ خطأ YouTube/Drive: {e}", reply_markup=after_publish_keyboard())
    except Exception as e:
        logging.error(f"publish_now error: {e}")
        await context.bot.send_message(chat_id, f"❌ حدث خطأ أثناء النشر: {e}", reply_markup=after_publish_keyboard())


# ---------------- Scheduling helpers ----------------
def next_scheduled_time_text(times: List[str]) -> str:
    if not times:
        return "غير محدد"
    now = datetime.now(TZ)
    today = now.date()
    candidates = []
    for t in times:
        try:
            hh, mm = map(int, t.split(":"))
            dt = datetime(today.year, today.month, today.day, hh, mm, tzinfo=TZ)
            if dt > now:
                candidates.append(dt)
        except Exception:
            continue
    if candidates:
        nxt = min(candidates)
        return nxt.strftime("%H:%M")
    try:
        hh, mm = map(int, times[0].split(":"))
        return f"{hh:02d}:{mm:02d} (غدًا)"
    except Exception:
        return "غير محدد"


async def scheduled_post(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data["chat_id"]
    await publish_now(chat_id, context)


def clear_chat_jobs(app, chat_id: int):
    try:
        jobs = list(app.job_queue._jobs) if hasattr(app.job_queue, "_jobs") else list(app.job_queue.jobs())
        for job in jobs:
            try:
                if getattr(job, "data", None) and job.data.get("chat_id") == chat_id:
                    job.schedule_removal()
            except Exception:
                continue
    except Exception:
        logging.exception("Error clearing jobs")


def schedule_daily_jobs(app, chat_id: int, times: List[str]):
    clear_chat_jobs(app, chat_id)
    for t in times:
        try:
            hh, mm = map(int, t.split(":"))
            run_time = dtime(hour=hh, minute=mm, tzinfo=TZ)
            app.job_queue.run_daily(scheduled_post, time=run_time, data={"chat_id": chat_id}, name=f"autopost_{chat_id}_{t}")
        except Exception as e:
            logging.error(f"Failed scheduling time {t}: {e}")
    logging.info(f"Scheduled {len(times)} jobs for chat {chat_id}.")


# ---------------- Handlers ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    cfg = get_chat(chat_id)
    if cfg.get("setup_complete") and cfg.get("oauth_json") and cfg.get("refresh_token") and cfg.get("drive_folder_id"):
        await update.message.reply_text("👋 مرحبًا من جديد!\nهل تريد النشر الآن أم ضبط النشر التلقائي؟", reply_markup=main_menu_keyboard())
        return
    cfg["next"] = "await_json"
    save_state()
    await update.message.reply_text("📄 أرسل الآن ملف JSON الخاص بـ OAuth (client_secret.json).")


# (handle_document, handle_text, on_button) —— use your prior implementations unchanged
# For brevity in this message I assume you keep the previously provided implementations
# Paste your handle_document, handle_text, on_button functions here (unchanged).

# ---------------- FastAPI routes & lifecycle ----------------

# simple health route for UptimeRobot
@fastapp.get("/")
async def home():
    return {"status": "Bot is running!"}


# Startup: create Application, register handlers, initialize & start it, set webhook
@fastapp.on_event("startup")
async def on_startup():
    global app_telegram
    load_state()
    logging.info("Starting Telegram Application...")

    # create telegram Application here (avoid creating at import-time)
    app_telegram = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # register handlers (ensure these functions are defined above in your file)
    app_telegram.add_handler(CommandHandler("start", start))
    app_telegram.add_handler(CallbackQueryHandler(on_button))
    app_telegram.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app_telegram.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text))

    # initialize & start so job_queue runs
    await app_telegram.initialize()
    await app_telegram.start()

    # set webhook if WEBHOOK_BASE available
    if WEBHOOK_BASE:
        webhook_url = f"{WEBHOOK_BASE}/{TELEGRAM_BOT_TOKEN}"
        try:
            await app_telegram.bot.set_webhook(webhook_url)
            logging.info(f"✅ Webhook set to {webhook_url}")
        except Exception as e:
            logging.error(f"❌ Failed to set webhook: {e}")
    else:
        logging.warning("WEBHOOK_BASE not set — webhook not configured automatically. Set env var WEBHOOK_BASE to your app base URL.")


@fastapp.on_event("shutdown")
async def on_shutdown():
    global app_telegram
    if app_telegram:
        try:
            await app_telegram.bot.delete_webhook()
        except Exception:
            pass
        await app_telegram.stop()
        await app_telegram.shutdown()


@fastapp.post("/{token}")
async def telegram_webhook(token: str, request: Request):
    if token != TELEGRAM_BOT_TOKEN:
        return {"ok": False, "error": "invalid token path"}
    data = await request.json()
    # app_telegram must exist because webhook will be called after startup
    update = Update.de_json(data, app_telegram.bot)
    await app_telegram.update_queue.put(update)
    return {"ok": True}


# ---------------- Entrypoint ----------------
if __name__ == "__main__":
    uvicorn.run("main:fastapp", host="0.0.0.0", port=PORT)
