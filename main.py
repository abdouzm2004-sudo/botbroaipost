import os
import io
import json
import time
import tempfile
import logging
from typing import Dict, Any, List, Optional
from datetime import time as dtime

import pytz
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (ApplicationBuilder, CommandHandler, MessageHandler,
                          CallbackQueryHandler, ContextTypes, filters)

# Gemini
import google.generativeai as genai

# Google APIs
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload
from googleapiclient.errors import HttpError

# FastAPI / Uvicorn for webhook
from fastapi import FastAPI, Request
import uvicorn

# ===================== إعداد عام =====================
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)s | %(message)s")
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
if not TELEGRAM_BOT_TOKEN or not GOOGLE_API_KEY:
    raise RuntimeError(
        "يرجى ضبط TELEGRAM_BOT_TOKEN و GOOGLE_API_KEY في Secrets.")

genai.configure(api_key=GOOGLE_API_KEY)
GEMINI_MODEL = "gemini-2.5-flash"
TZ = pytz.timezone("Africa/Algiers")

STATE_FILE = "user_state.json"
USER_STATE: Dict[int, Dict[str, Any]] = {}


# ===================== حفظ واسترجاع الحالة =====================
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
            "autopost_times": []  # قائمة أوقات بصيغة "HH:MM"
        }
    return USER_STATE[chat_id]


# ===================== لوحات الأزرار =====================
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
         ],
         [
             InlineKeyboardButton("🔄 إعادة الإعداد من جديد",
                                  callback_data="reset_setup")
         ]])


def yes_no_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("نعم", callback_data="save_yes"),
        InlineKeyboardButton("لا", callback_data="save_no")
    ]])


def after_publish_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📋 عرض الإعدادات الحالية",
                             callback_data="show_settings")
    ],
                                 [
                                     InlineKeyboardButton(
                                         "🔄 إعادة الإعداد من جديد",
                                         callback_data="reset_setup")
                                 ]])


def autopost_control_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⛔ إيقاف النشر التلقائي",
                             callback_data="autopost_stop")
    ]])


# ===================== بناء الاعتمادات =====================
def extract_oauth_fields(oauth_json: Dict[str, Any]) -> Dict[str, str]:
    block = oauth_json.get("installed") or oauth_json.get("web")
    if not block:
        raise RuntimeError("ملف JSON لا يحتوي على 'installed' أو 'web'.")
    for key in ("client_id", "client_secret", "token_uri"):
        if key not in block:
            raise RuntimeError(f"ملف JSON ناقص الحقل: {key}")
    return {
        "client_id": block["client_id"],
        "client_secret": block["client_secret"],
        "token_uri": block["token_uri"]
    }


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


# ===================== التعامل مع Google Drive =====================
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


# ===================== توليد العنوان/الوصف + الهاشتاغات =====================
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
    # إزالة التكرار
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
            if len(mixed) >= max_total: break
            if r not in mixed: mixed.append(r)
    return " ".join(f"#{t.replace(' ', '')}" for t in mixed)


def generate_metadata_with_gemini(video_path: str,
                                  filename_hint: str = "") -> Dict[str, str]:
    contextual = infer_context_tags(filename_hint)
    trending = TRENDING_TAGS.copy()
    try:
        uploaded = genai.upload_file(video_path)
        file_info = genai.get_file(uploaded.name)
        for _ in range(60):
            if file_info.state.name == "ACTIVE": break
            time.sleep(0.5)
            file_info = genai.get_file(uploaded.name)
        if file_info.state.name != "ACTIVE":
            raise RuntimeError("الملف لم يصبح ACTIVE بعد رفعه إلى Gemini.")
        prompt = (
            "Analyze the video and generate:\n"
            "1) A catchy English YouTube title (max 70 characters).\n"
            "2) A short English description (3-4 sentences) explaining what the viewer sees and why it's engaging.\n"
            "Return exactly:\nTitle: <title>\nDescription: <description>")
        model = genai.GenerativeModel(GEMINI_MODEL)
        response = model.generate_content([file_info, prompt])
        text = (getattr(response, "text", "") or "").strip()
        title, desc = None, None
        for line in text.splitlines():
            low = line.lower()
            if low.startswith("title:"): title = line.split(":", 1)[1].strip()
            elif low.startswith("description:"):
                desc = line.split(":", 1)[1].strip()
        if not title: title = "AI Magic: Surprising Transformation!"
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


# ===================== رفع الفيديو إلى يوتيوب مع رسائل فشل واضحة =====================
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
        # نُعيد سبب الفشل كنص لعرضه للمستخدم لاحقًا
        if "uploadLimitExceeded" in str(e):
            return "ERR:uploadLimitExceeded"
        return f"ERR:HttpError {e.status_code if hasattr(e, 'status_code') else ''}"
    except Exception as e:
        return f"ERR:Generic {str(e)}"


# ===================== نشر فوري =====================
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

        # في حالة الفشل: لا نحذف من درايف ونرسل سبب الفشل
        if isinstance(url_or_err, str) and url_or_err.startswith("ERR:"):
            reason = url_or_err.split(":", 1)[1]
            msg = "❌ فشل رفع الفيديو."
            if reason == "uploadLimitExceeded":
                msg += "\nالسبب: تجاوز حد الرفع المؤقت في يوتيوب."
            else:
                msg += f"\nالسبب: {reason}."
            await context.bot.send_message(
                chat_id, msg, reply_markup=after_publish_keyboard())

            # إذا كان النشر التلقائي مفعلاً، أعلن عن الوقت القادم
            if cfg.get("autopost_enabled") and cfg.get("autopost_times"):
                next_time = next_scheduled_time_text(cfg["autopost_times"])
                await context.bot.send_message(
                    chat_id,
                    f"🗓️ النشر التلقائي مفعّل. موعد النشر القادم: {next_time}")
            return

        # نجاح: نحذف من درايف ونبلغ المستخدم ونظهر لوحة بعد النشر
        delete_drive_file(drive, file["id"])
        remaining = len(list_videos(drive, folder_id))
        await context.bot.send_message(
            chat_id,
            f"✅ تم النشر!\nعدد الفيديوات المتبقية: {remaining}\nرابط الفيديو: {url_or_err}",
            reply_markup=after_publish_keyboard())

        # إذا كان النشر التلقائي مفعلاً، أعلن عن الوقت القادم
        if cfg.get("autopost_enabled") and cfg.get("autopost_times"):
            next_time = next_scheduled_time_text(cfg["autopost_times"])
            await context.bot.send_message(
                chat_id,
                f"🗓️ النشر التلقائي مفعّل. موعد النشر القادم: {next_time}")

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


# ===================== حساب نص وقت النشر القادم =====================
def next_scheduled_time_text(times: List[str]) -> str:
    """
    تُعيد أقرب وقت قادم اليوم أو غدًا بصيغة HH:MM بناءً على المنطقة الزمنية.
    """
    if not times:
        return "غير محدد"
    # تحويل إلى أرقام
    now = pytz.datetime.datetime.now(TZ)
    today = now.date()
    candidates = []
    for t in times:
        try:
            hh, mm = map(int, t.split(":"))
            dt = pytz.datetime.datetime(today.year,
                                        today.month,
                                        today.day,
                                        hh,
                                        mm,
                                        tzinfo=TZ)
            if dt > now:
                candidates.append(dt)
        except Exception:
            continue
    if candidates:
        nxt = min(candidates)
        return nxt.strftime("%H:%M")
    # إن لم يوجد وقت لاحق اليوم، اعرض أول وقت في قائمة الغد
    try:
        hh, mm = map(int, times[0].split(":"))
        return f"{hh:02d}:{mm:02d} (غدًا)"
    except Exception:
        return "غير محدد"


# ===================== جدولة يومية للنشر التلقائي =====================
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


# ===================== Handlers: تدفّق الإعداد =====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    cfg = get_chat(chat_id)

    # تخطي الإعداد إذا سبق حفظ المعلومات
    if cfg.get("setup_complete") and cfg.get("oauth_json") and cfg.get(
            "refresh_token") and cfg.get("drive_folder_id"):
        await update.message.reply_text(
            "👋 مرحبًا من جديد!\nهل تريد النشر الآن أم ضبط النشر التلقائي؟",
            reply_markup=main_menu_keyboard())
        return

    cfg["next"] = "await_json"
    save_state()
    await update.message.reply_text(
        "📄 أرسل الآن ملف JSON الخاص بـ OAuth (client_secret.json).")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    cfg = get_chat(chat_id)
    if cfg.get("next") != "await_json":
        await update.message.reply_text(
            "⚠️ لست في مرحلة رفع JSON. أرسل /start لإعادة التهيئة.")
        return

    doc = update.message.document
    if not doc or not doc.file_name.endswith(".json"):
        await update.message.reply_text("❌ أرسل ملف JSON فقط.")
        return

    file = await context.bot.get_file(doc.file_id)
    tmp_dir = tempfile.mkdtemp(prefix="json_")
    local_path = os.path.join(tmp_dir, doc.file_name)
    await file.download_to_drive(local_path)
    try:
        with open(local_path, "r", encoding="utf-8") as f:
            oauth_json = json.load(f)
        _ = extract_oauth_fields(oauth_json)
        cfg["oauth_json"] = oauth_json
        cfg["next"] = "await_refresh"
        save_state()
        await update.message.reply_text(
            "🔐 تم استلام JSON. أرسل الآن الـ Refresh Token (نص).")
    except Exception as e:
        await update.message.reply_text(f"❌ ملف JSON غير صالح: {e}")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()
    cfg = get_chat(chat_id)

    if cfg.get("next") == "await_refresh":
        cfg["refresh_token"] = text
        cfg["next"] = "await_folder"
        save_state()
        await update.message.reply_text(
            "📁 أرسل معرف مجلد الفيديوات في Google Drive (folder_id).")
        return

    if cfg.get("next") == "await_folder":
        cfg["drive_folder_id"] = text
        cfg["next"] = "ask_save"
        save_state()
        await update.message.reply_text("هل تريد حفظ معلوماتك؟",
                                        reply_markup=yes_no_keyboard())
        return

    # أوامر نصية اختصارية
    if text.lower() == "انشر الان":
        await publish_now(chat_id, context)
        return

    if text.strip() == "ضبط النشر التلقائي":
        cfg["next"] = "await_times_count"
        save_state()
        await update.message.reply_text(
            "📊 أرسل عدد الفيديوهات يومياً (من 1 إلى 7).")
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
            save_state()
            await update.message.reply_text(
                "⏰ أرسل وقت نشر الفيديو الأول بصيغة HH:MM (مثال 13:30).")
        except Exception:
            await update.message.reply_text(
                "❌ صيغة غير صحيحة. أرسل عددًا بين 1 و7.")
        return

    if cfg.get("next", "").startswith("await_time_"):
        try:
            hh, mm = map(int, text.split(":"))
            if not (0 <= hh <= 23 and 0 <= mm <= 59):
                raise ValueError("bad time")
            cfg["autopost_times"].append(f"{hh:02d}:{mm:02d}")
            save_state()
            if len(cfg["autopost_times"]) < cfg["autopost_count"]:
                next_idx = len(cfg["autopost_times"]) + 1
                cfg["next"] = f"await_time_{next_idx}"
                save_state()
                await update.message.reply_text(
                    f"⏰ أرسل وقت نشر الفيديو رقم {next_idx} بصيغة HH:MM.")
            else:
                cfg["next"] = "idle"
                cfg["autopost_enabled"] = True
                save_state()
                schedule_daily_jobs(context.application, chat_id,
                                    cfg["autopost_times"])
                await update.message.reply_text(
                    "✅ تم تفعيل النشر التلقائي يوميًا حسب الأوقات المضبوطة.",
                    reply_markup=autopost_control_keyboard())
        except Exception:
            await update.message.reply_text(
                "❌ وقت غير صالح. استخدم صيغة HH:MM (مثال 08:15).")
        return

    await update.message.reply_text(
        "الأوامر:\n"
        "- /start بدء الإعداد من جديد.\n"
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
        if cfg["save_info"] and cfg.get("oauth_json") and cfg.get(
                "refresh_token") and cfg.get("drive_folder_id"):
            cfg["setup_complete"] = True
        save_state()

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
        save_state()
        await query.edit_message_text(
            "📊 أرسل عدد الفيديوهات يومياً (من 1 إلى 7).")
        return

    if query.data == "autopost_stop":
        cfg["autopost_enabled"] = False
        cfg["autopost_times"] = []
        cfg["autopost_count"] = 0
        save_state()
        clear_chat_jobs(context.application, chat_id)
        await query.edit_message_text("⛔ تم إيقاف النشر التلقائي.",
                                      reply_markup=main_menu_keyboard())
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

    if query.data == "reset_setup":
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
        save_state()
        clear_chat_jobs(context.application, chat_id)
        await query.edit_message_text(
            "🔄 تمت إعادة الإعداد. أرسل الآن ملف JSON للبدء من جديد.")
        return

    await query.edit_message_text("خيار غير معروف.",
                                  reply_markup=main_menu_keyboard())


# ===================== FastAPI Webhook integration =====================
# Load previous state
load_state()

# FastAPI app and telegram Application (Dispatcher)
fastapp = FastAPI()
app_telegram = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

# Register handlers on the telegram application (same as before)
app_telegram.add_handler(CommandHandler("start", start))
app_telegram.add_handler(CallbackQueryHandler(on_button))
app_telegram.add_handler(MessageHandler(filters.Document.ALL, handle_document))
app_telegram.add_handler(
    MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text))

# WEBHOOK settings
WEBHOOK_BASE = os.getenv("WEBHOOK_URL") or ""
PORT = int(os.getenv("PORT", "8000"))
if not WEBHOOK_BASE:
    logging.warning(
        "WEBHOOK_URL not set. You must set WEBHOOK_URL env var to your Replit/host URL."
    )
WEBHOOK_PATH = f"/{TELEGRAM_BOT_TOKEN}"
WEBHOOK_URL = f"{WEBHOOK_BASE}{WEBHOOK_PATH}"


@fastapp.on_event("startup")
async def on_startup():
    # Initialize and start the telegram application so job_queue and dispatcher run
    await app_telegram.initialize()
    await app_telegram.start()
    # Set webhook on Telegram
    try:
        await app_telegram.bot.set_webhook(WEBHOOK_URL)
        logging.info(f"✅ Webhook set to {WEBHOOK_URL}")
    except Exception as e:
        logging.error(f"❌ Failed to set webhook: {e}")


@fastapp.on_event("shutdown")
async def on_shutdown():
    try:
        await app_telegram.bot.delete_webhook()
    except Exception:
        pass
    await app_telegram.stop()
    await app_telegram.shutdown()


@fastapp.post("/{token}")
async def telegram_webhook(token: str, request: Request):
    # basic check: ensure path token matches
    if token != TELEGRAM_BOT_TOKEN:
        return {"ok": False, "error": "invalid token path"}
    data = await request.json()
    update = Update.de_json(data, app_telegram.bot)
    # enqueue update for processing by python-telegram-bot
    await app_telegram.update_queue.put(update)
    return {"ok": True}


# ===================== Entrypoint (uvicorn) =====================
if __name__ == "__main__":
    # If your file name is different, change "main:fastapp" accordingly
    uvicorn.run("main:fastapp", host="0.0.0.0", port=PORT)
