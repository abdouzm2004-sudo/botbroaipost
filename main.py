# main.py
import os
import json
import logging
import tempfile
import io
import time
import threading
from typing import Dict, Any, List, Optional
from datetime import time as dtime

import pytz
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (ApplicationBuilder, CommandHandler, MessageHandler,
                          CallbackQueryHandler, ContextTypes, filters)

# Google / Gemini imports (كما في كودك الأصلي)
import google.generativeai as genai
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload
from googleapiclient.errors import HttpError

# ==== logging و env ====
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)s | %(message)s")
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
WEBHOOK_BASE = os.getenv("WEBHOOK_BASE")  # مثال: https://your-service.onrender.com
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("يرجى ضبط TELEGRAM_BOT_TOKEN في متغيرات البيئة.")

if GOOGLE_API_KEY:
    genai.configure(api_key=GOOGLE_API_KEY)

GEMINI_MODEL = "gemini-2.5-flash"
TZ = pytz.timezone("Africa/Algiers")

USERS_DIR = "users"
os.makedirs(USERS_DIR, exist_ok=True)

STATE_FILE = "user_state.json"
USER_STATE: Dict[int, Dict[str, Any]] = {}

OAUTH_TOKEN_URI_DEFAULT = "https://oauth2.googleapis.com/token"

# ---------- مساعدات حفظ/تحميل المستخدمين ----------
def user_filepath(chat_id: int) -> str:
    return os.path.join(USERS_DIR, f"{chat_id}.json")


def load_user_file(chat_id: int) -> Optional[Dict[str, Any]]:
    path = user_filepath(chat_id)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"Failed to load user file {path}: {e}")
    return None


def save_user_file(chat_id: int, data: Dict[str, Any]):
    path = user_filepath(chat_id)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error(f"Failed to save user file {path}: {e}")


# ---------- حالة في الذاكرة ----------
def get_chat(chat_id: int) -> Dict[str, Any]:
    if chat_id in USER_STATE:
        return USER_STATE[chat_id]
    data = load_user_file(chat_id)
    if data:
        USER_STATE[chat_id] = data
        return USER_STATE[chat_id]
    USER_STATE[chat_id] = {
        "next": "await_json",
        "oauth_json": None,
        "refresh_token": None,
        "drive_folder_id": None,
        "oauth_client_id": None,
        "oauth_client_secret": None,
        "oauth_token_uri": OAUTH_TOKEN_URI_DEFAULT,
        "save_info": True,
        "setup_complete": False,
        "autopost_enabled": False,
        "autopost_count": 0,
        "autopost_times": []
    }
    return USER_STATE[chat_id]


# ---------- لوحات الأزرار ----------
def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("انشر الآن", callback_data="publish_now")],
         [
             InlineKeyboardButton("🔘 ضبط النشر التلقائي",
                                  callback_data="autopost_setup")
         ],
         [
             InlineKeyboardButton("📋 عرض الإعدادات الحالية",
                                  callback_data="show_settings")
         ]])


def after_publish_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("📋 عرض الإعدادات الحالية",
                                                     callback_data="show_settings")]])


def autopost_control_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⛔ إيقاف النشر التلقائي",
                                                     callback_data="autopost_stop")]])


# ---------- OAuth helpers ----------
def extract_oauth_fields(oauth_json: Dict[str, Any]) -> Dict[str, str]:
    block = oauth_json.get("installed") or oauth_json.get("web")
    if not block:
        raise RuntimeError("ملف JSON لا يحتوي على 'installed' أو 'web'.")
    for key in ("client_id", "client_secret"):
        if key not in block:
            raise RuntimeError(f"ملف JSON ناقص الحقل: {key}")
    token_uri = block.get("token_uri", OAUTH_TOKEN_URI_DEFAULT)
    return {
        "client_id": block["client_id"],
        "client_secret": block["client_secret"],
        "token_uri": token_uri
    }


def build_services(cfg: Dict[str, Any]):
    client_id = cfg.get("oauth_client_id")
    client_secret = cfg.get("oauth_client_secret")
    token_uri = cfg.get("oauth_token_uri") or OAUTH_TOKEN_URI_DEFAULT
    refresh_token = cfg.get("refresh_token")

    if not client_id or not client_secret:
        oauth_json = cfg.get("oauth_json")
        if oauth_json:
            fields = extract_oauth_fields(oauth_json)
            client_id = fields["client_id"]
            client_secret = fields["client_secret"]
            token_uri = fields.get("token_uri", token_uri)
            cfg["oauth_client_id"] = client_id
            cfg["oauth_client_secret"] = client_secret
            cfg["oauth_token_uri"] = token_uri

    if not (client_id and client_secret and refresh_token):
        raise RuntimeError(
            "بيانات OAuth ناقصة. أرسل JSON ثم REFRESH_TOKEN ثم DRIVE_FOLDER_ID."
        )

    creds = Credentials(None,
                        refresh_token=refresh_token,
                        token_uri=token_uri,
                        client_id=client_id,
                        client_secret=client_secret,
                        scopes=[
                            "https://www.googleapis.com/auth/youtube.upload",
                            "https://www.googleapis.com/auth/drive"
                        ])
    drive = build("drive", "v3", credentials=creds)
    youtube = build("youtube", "v3", credentials=creds)
    return drive, youtube


# ---------- Drive helpers ----------
def list_videos(drive, folder_id: str) -> List[Dict[str, Any]]:
    query = f"'{folder_id}' in parents and mimeType contains 'video/'"
    resp = drive.files().list(q=query, fields="files(id,name)").execute()
    return resp.get("files", [])


def list_first_video_in_folder(drive,
                               folder_id: str) -> Optional[Dict[str, Any]]:
    query = f"'{folder_id}' in parents and mimeType contains 'video/'"
    resp = drive.files().list(q=query,
                              orderBy="createdTime",
                              pageSize=10,
                              fields="files(id,name,createdTime)").execute()
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


# ---------- Gemini metadata (fallback safe) ----------
TRENDING_TAGS = [
    "Trending", "Viral", "Shorts", "AI", "Creative", "YouTube", "Funny",
    "Tech", "Magic", "Surprise"
]


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


def format_hashtags(trending: List[str],
                    contextual: List[str],
                    max_total: int = 10) -> str:
    mixed = trending[:5] + contextual[:5]
    if len(mixed) < max_total:
        for r in trending[5:] + contextual[5:]:
            if len(mixed) >= max_total:
                break
            if r not in mixed:
                mixed.append(r)
    return " ".join(f"#{t.replace(' ', '')}" for t in mixed)


def generate_metadata_with_gemini(video_path: str,
                                  filename_hint: str = "") -> Dict[str, str]:
    contextual = infer_context_tags(filename_hint)
    trending = TRENDING_TAGS.copy()
    try:
        if not GOOGLE_API_KEY:
            raise RuntimeError("GOOGLE_API_KEY not set for Gemini.")
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
            "Return exactly:\n"
            "Title: <title>\n"
            "Description: <description>")
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
        return {
            "title": "AI Magic: Surprising Transformation!",
            "description": "Fallback description.\n" + hashtags
        }


# ========== YouTube upload helper ==========
def upload_to_youtube(youtube, video_path: str,
                      meta: Dict[str, str]) -> Optional[str]:
    try:
        body = {
            "snippet": {
                "title": meta["title"],
                "description": meta["description"],
                "categoryId": "22"
            },
            "status": {
                "privacyStatus": "public"
            }
        }
        media = MediaFileUpload(video_path, chunksize=-1, resumable=True)
        request = youtube.videos().insert(part="snippet,status",
                                          body=body,
                                          media_body=media)
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
        return f"ERR:HttpError {e.status_code if hasattr(e, 'status_code') else ''}"
    except Exception as e:
        return f"ERR:Generic {str(e)}"


# ========== Publish flow ==========
async def publish_now(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    cfg = get_chat(chat_id)
    folder_id = cfg.get("drive_folder_id")
    if not folder_id:
        await context.bot.send_message(chat_id,
                                       "❌ لم يتم ضبط معرف مجلد درايف بعد.")
        return
    try:
        drive, youtube = build_services(cfg)
        file = list_first_video_in_folder(drive, folder_id)
        if not file:
            await context.bot.send_message(
                chat_id, "❌ لا يوجد فيديوهات في هذا المجلد.")
            return
        await context.bot.send_message(
            chat_id, f"🔍 جاري التحليل وتوليد البيانات للفيديو: {file['name']}")
        local_path = download_drive_file(drive, file["id"], file["name"])
        meta = generate_metadata_with_gemini(local_path,
                                             filename_hint=file["name"])
        await context.bot.send_message(
            chat_id, f"⬆️ جاري الرفع إلى يوتيوب...\nTitle: {meta['title']}")
        url_or_err = upload_to_youtube(youtube, local_path, meta)
        if isinstance(url_or_err, str) and url_or_err.startswith("ERR:"):
            reason = url_or_err.split(":", 1)[1]
            msg = "❌ فشل رفع الفيديو."
            if reason == "uploadLimitExceeded":
                msg += "\nالسبب: تجاوز حد الرفع المؤقت في يوتيوب."
            else:
                msg += f"\nالسبب: {reason}."
            await context.bot.send_message(
                chat_id, msg, reply_markup=after_publish_keyboard())
            return
        delete_drive_file(drive, file["id"])
        remaining = len(list_videos(drive, folder_id))
        await context.bot.send_message(
            chat_id,
            f"✅ تم النشر!\nعدد الفيديوات المتبقية: {remaining}\nرابط الفيديو: {url_or_err}",
            reply_markup=after_publish_keyboard())
    except HttpError as e:
        logging.error(f"publish_now HttpError: {e}")
        await context.bot.send_message(chat_id,
                                       f"❌ خطأ YouTube/Drive: {e}",
                                       reply_markup=after_publish_keyboard())
    except Exception as e:
        logging.error(f"publish_now error: {e}")
        await context.bot.send_message(chat_id,
                                       f"❌ حدث خطأ أثناء النشر: {e}",
                                       reply_markup=after_publish_keyboard())


# ========== Scheduling helpers ==========
async def scheduled_post(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data["chat_id"]
    await publish_now(chat_id, context)


def clear_chat_jobs(app, chat_id: int):
    for job in app.job_queue.jobs():
        try:
            if job.data and job.data.get("chat_id") == chat_id:
                job.schedule_removal()
        except Exception:
            continue


def schedule_daily_jobs(app, chat_id: int, times: List[str]):
    clear_chat_jobs(app, chat_id)
    for t in times:
        try:
            hh, mm = map(int, t.split(":"))
            run_time = dtime(hour=hh, minute=mm, tzinfo=TZ)
            app.job_queue.run_daily(scheduled_post,
                                    time=run_time,
                                    data={"chat_id": chat_id},
                                    name=f"autopost_{chat_id}_{t}")
        except Exception as e:
            logging.error(f"Failed scheduling time {t}: {e}")
    logging.info(f"Scheduled {len(times)} jobs for chat {chat_id}.")


# ========== Handlers ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    cfg = get_chat(chat_id)
    if cfg.get("setup_complete") and cfg.get("refresh_token") and cfg.get("drive_folder_id"):
        count_text = "غير قابل للقراءة"
        error = None
        try:
            drive, _ = build_services(cfg)
            files = list_videos(drive, cfg.get("drive_folder_id"))
            count_text = str(len(files))
        except Exception as e:
            error = str(e)
            logging.error(f"Drive read failed for {chat_id}: {error}")
        if error:
            await update.message.reply_text(
                f"👋 مرحبًا! الإعدادات محفوظة.\n⚠️ لكن قراءة المجلد فشلت: {error}\nعدد الفيديوهات (محاولة): {count_text}",
                reply_markup=main_menu_keyboard())
        else:
            await update.message.reply_text(
                f"👋 مرحبًا! الإعدادات محفوظة.\nعدد الفيديوهات في المجلد: {count_text}",
                reply_markup=main_menu_keyboard())
        return
    if cfg.get("next") == "await_json":
        await update.message.reply_text(
            "📄 أرسل الآن ملف JSON الخاص بـ OAuth (client_secret.json).")
        return
    else:
        await update.message.reply_text(
            "⚠️ إكمال الإعداد مطلوب. أرسل ملف JSON للبدء (client_secret.json).")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    cfg = get_chat(chat_id)
    if cfg.get("next") != "await_json":
        await update.message.reply_text(
            "⚠️ لست في مرحلة رفع JSON الآن. استخدم /start لإعادة التهيئة.")
        return
    doc = update.message.document
    if not doc or not doc.file_name.endswith(".json"):
        await update.message.reply_text("❌ أرسل ملف JSON فقط (client_secret.json).")
        return
    file = await context.bot.get_file(doc.file_id)
    tmp_dir = tempfile.mkdtemp(prefix="json_")
    local_path = os.path.join(tmp_dir, doc.file_name)
    await file.download_to_drive(local_path)
    try:
        with open(local_path, "r", encoding="utf-8") as f:
            oauth_json = json.load(f)
        fields = extract_oauth_fields(oauth_json)
        cfg["oauth_json"] = oauth_json
        cfg["oauth_client_id"] = fields["client_id"]
        cfg["oauth_client_secret"] = fields["client_secret"]
        cfg["oauth_token_uri"] = fields.get("token_uri", OAUTH_TOKEN_URI_DEFAULT)
        cfg["next"] = "await_refresh"
        save_user_file(chat_id, cfg)
        await update.message.reply_text("🔐 تم استلام JSON. أرسل الآن الـ Refresh Token (نص).")
    except Exception as e:
        logging.error(f"Invalid JSON upload from {chat_id}: {e}")
        await update.message.reply_text(f"❌ ملف JSON غير صالح: {e}")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()
    cfg = get_chat(chat_id)
    if cfg.get("next") == "await_refresh":
        cfg["refresh_token"] = text
        cfg["next"] = "await_folder"
        save_user_file(chat_id, cfg)
        await update.message.reply_text("📁 الآن أرسل معرف مجلد الفيديوات في Google Drive (folder_id).")
        return
    if cfg.get("next") == "await_folder":
        cfg["drive_folder_id"] = text
        cfg["next"] = "idle"
        cfg["setup_complete"] = True
        save_user_file(chat_id, cfg)
        try:
            drive, _ = build_services(cfg)
            files = list_videos(drive, cfg["drive_folder_id"])
            count = len(files)
            await update.message.reply_text(
                f"✅ تم حفظ الإعدادات. عدد الفيديوهات في المجلد: {count}",
                reply_markup=main_menu_keyboard())
        except Exception as e:
            await update.message.reply_text(
                f"⚠️ تم حفظ الإعدادات لكن فشل الوصول إلى Drive: {e}\nستظل البيانات محفوظة يمكنك تعديلها لاحقًا.",
                reply_markup=main_menu_keyboard())
        return
    if text.lower() in ("انشر الان", "/publish", "publish now"):
        await publish_now(chat_id, context)
        return
    if text.strip() in ("ضبط النشر التلقائي", "/autopost"):
        cfg["next"] = "await_times_count"
        save_user_file(chat_id, cfg)
        await update.message.reply_text("📊 أرسل عدد الفيديوهات يومياً (من 1 إلى 7).")
        return
    if cfg.get("next") == "await_times_count":
        try:
            n = int(text)
            if n < 1 or n > 7:
                await update.message.reply_text("❌ أرسل عددًا بين 1 و7.")
                return
            cfg["autopost_count"] = n
            cfg["autopost_times"] = []
            cfg["next"] = "await_time_1"
            save_user_file(chat_id, cfg)
            await update.message.reply_text("⏰ أرسل وقت نشر الفيديو الأول بصيغة HH:MM (مثال 13:30).")
        except Exception:
            await update.message.reply_text("❌ صيغة غير صحيحة. أرسل عددًا بين 1 و7.")
        return
    if cfg.get("next", "").startswith("await_time_"):
        try:
            hh, mm = map(int, text.split(":"))
            if not (0 <= hh <= 23 and 0 <= mm <= 59):
                raise ValueError("bad time")
            cfg["autopost_times"].append(f"{hh:02d}:{mm:02d}")
            save_user_file(chat_id, cfg)
            if len(cfg["autopost_times"]) < cfg["autopost_count"]:
                next_idx = len(cfg["autopost_times"]) + 1
                cfg["next"] = f"await_time_{next_idx}"
                save_user_file(chat_id, cfg)
                await update.message.reply_text(f"⏰ أرسل وقت نشر الفيديو رقم {next_idx} بصيغة HH:MM.")
            else:
                cfg["next"] = "idle"
                cfg["autopost_enabled"] = True
                save_user_file(chat_id, cfg)
                schedule_daily_jobs(context.application, chat_id, cfg["autopost_times"])
                await update.message.reply_text(
                    "✅ تم تفعيل النشر التلقائي يوميًا حسب الأوقات المضبوطة.",
                    reply_markup=autopost_control_keyboard())
        except Exception:
            await update.message.reply_text("❌ وقت غير صالح. استخدم صيغة HH:MM (مثال 08:15).")
        return
    await update.message.reply_text(
        "الأوامر:\n"
        "- /start بدء أو إظهار الحالة.\n"
        "- انشر الان للنشر الفوري.\n"
        "- ضبط النشر التلقائي لضبط الأوقات اليومية.")


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    cfg = get_chat(chat_id)
    if query.data in ("save_yes", "save_no"):
        cfg["save_info"] = (query.data == "save_yes")
        cfg["next"] = "idle"
        if cfg["save_info"] and cfg.get("refresh_token") and cfg.get("drive_folder_id"):
            cfg["setup_complete"] = True
        save_user_file(chat_id, cfg)
        count = 0
        try:
            drive, _ = build_services(cfg)
            count = len(list_videos(drive, cfg["drive_folder_id"]))
        except Exception as e:
            logging.error(f"Count videos error: {e}")
        await query.edit_message_text(
            f"✅ تم الإعداد.\nعدد الفيديوات في المجلد: {count}\nاختر طريقة النشر:",
            reply_markup=main_menu_keyboard())
        return
    if query.data == "publish_now":
        await query.edit_message_text("⏳ جاري النشر الآن...")
        await publish_now(chat_id, context)
        return
    if query.data == "autopost_setup":
        cfg["next"] = "await_times_count"
        save_user_file(chat_id, cfg)
        await query.edit_message_text("📊 أرسل عدد الفيديوهات يومياً (من 1 إلى 7).")
        return
    if query.data == "autopost_stop":
        cfg["autopost_enabled"] = False
        cfg["autopost_times"] = []
        cfg["autopost_count"] = 0
        save_user_file(chat_id, cfg)
        clear_chat_jobs(context.application, chat_id)
        await query.edit_message_text("⛔ تم إيقاف النشر التلقائي.", reply_markup=main_menu_keyboard())
        return
    if query.data == "show_settings":
        msg = (
            f"📋 إعداداتك الحالية:\n"
            f"- حفظ المعلومات: {'✅' if cfg.get('setup_complete') else '❌'}\n"
            f"- معرف المجلد: {cfg.get('drive_folder_id') or 'غير مضبوط'}\n"
            f"- النشر التلقائي: {'✅' if cfg.get('autopost_enabled') else '❌'}\n"
            f"- الأوقات: {', '.join(cfg.get('autopost_times', [])) or 'لا يوجد'}"
        )
        await query.edit_message_text(msg, reply_markup=main_menu_keyboard())
        return
    await query.edit_message_text("خيار غير معروف.", reply_markup=main_menu_keyboard())


# ========== إعداد تطبيق التليجرام ==========
application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
application.add_handler(CommandHandler("start", start))
application.add_handler(CallbackQueryHandler(on_button))
application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text))


# ========== FastAPI app ====
fastapp = FastAPI(title="BotBroAIPost")

@fastapp.get("/")
async def index():
    return {"ok": True, "bot": "BotBroAIPost"}

# Webhook endpoint (سيتم استدعاؤه من Telegram إذا ضبطت WEBHOOK_BASE)
@fastapp.post("/webhook/{token}")
async def telegram_webhook(token: str, request: Request):
    # لمزيد من الأمان نتحقق إن المسار يحتوي على التوكين الفعلي للبوت
    if token != TELEGRAM_BOT_TOKEN:
        logging.warning("Received webhook with invalid token in path.")
        raise HTTPException(status_code=403, detail="Invalid token")
    body = await request.json()
    try:
        update = Update.de_json(body, application.bot)
        # ضع التحديث في طابور التطبيق ليعالجه handlers
        await application.update_queue.put(update)
    except Exception as e:
        logging.exception("Failed to process incoming webhook update.")
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


# ========== startup / shutdown hooks ==========
def _run_polling_in_thread():
    """تشغيل polling في thread منفصل (يستخدم إن لم يكن WEBHOOK_BASE مضبوط)."""
    try:
        logging.info("Starting polling (background thread).")
        # run_polling سيبقى يعمل في هذا الخيط
        application.run_polling(allowed_updates=Update.ALL_TYPES,
                                drop_pending_updates=True)
    except Exception:
        logging.exception("Polling thread crashed.")


@fastapp.on_event("startup")
async def on_startup():
    logging.info("FastAPI startup: initializing Telegram application.")
    # ensure application.initialize/startup run to setup internal components
    await application.initialize()
    # إذا ضبطت WEBHOOK_BASE فسنعين webhook إلى URL الخاص بك
    if WEBHOOK_BASE:
        webhook_url = f"{WEBHOOK_BASE.rstrip('/')}/webhook/{TELEGRAM_BOT_TOKEN}"
        try:
            await application.bot.set_webhook(webhook_url)
            logging.info(f"Webhook set to {webhook_url}")
        except Exception:
            logging.exception("Failed to set webhook.")
        # ثم شغّل معالجة التحديثات داخليًا (application.start)
        await application.start()
        logging.info("Telegram application started in webhook mode.")
    else:
        # نستخدم polling في thread لأن uvicorn/ASGI يستخدم الخيوط/الـ event loop الخاصة به
        t = threading.Thread(target=_run_polling_in_thread, daemon=True)
        t.start()
        logging.info("Polling thread started.")


@fastapp.on_event("shutdown")
async def on_shutdown():
    logging.info("FastAPI shutdown: stopping Telegram application.")
    try:
        if WEBHOOK_BASE:
            try:
                await application.bot.delete_webhook()
                logging.info("Webhook deleted.")
            except Exception:
                logging.exception("Failed to delete webhook on shutdown.")
        # طلب إيقاف التطبيق (سيوقف polling أو غيره)
        await application.stop()
        await application.shutdown()
    except Exception:
        logging.exception("Error during Telegram application shutdown.")


# ========== مساعدة تحميل حالة عامة (اختياري) ==========
def load_state():
    global USER_STATE
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                USER_STATE = json.load(f)
            logging.info("Loaded global state file.")
        except Exception:
            logging.warning("Failed to load global state file; continuing.")


load_state()
