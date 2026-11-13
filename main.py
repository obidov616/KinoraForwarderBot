import asyncio
import json
import logging
import os
import random
import re
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from html import escape as _esc

from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from telethon import TelegramClient, functions, types, errors
from telethon.sessions import StringSession


# ================== SOZLAMALAR ==================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip().strip('"').strip("'")
OWNER_ID_RAW = os.getenv("OWNER_ID", "").strip()
OWNER_ID = int(OWNER_ID_RAW) if OWNER_ID_RAW.isascii() and OWNER_ID_RAW.isdecimal() else 0
DATA_FILE = Path(os.getenv("DATA_FILE", "forwarder_data.json"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("telethon").setLevel(logging.WARNING)

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN topilmadi. Environment bo'limida BOT_TOKEN variable yarating."
    )
if not OWNER_ID:
    raise RuntimeError(
        "OWNER_ID topilmadi. Environment bo'limida OWNER_ID ga Telegram ID (raqam) yozing."
    )

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


# ================== MA'LUMOTLAR ==================
def default_data():
    return {
        "userbot": {"session": "", "phone": ""},
        "api": {"api_id": 0, "api_hash": ""},
        "settings": {
            "source": "",
            "source_title": "",
            "dest": "",
            "dest_title": "",
            "min_size_mb": 100,
            "movies_topic": "Kinolar",
            "series_min_episodes": 2,
            "auto": False,
            "auto_interval_min": 30,
        },
        "stats": {},
        "copied": {},
    }


_DATA_CACHE = None
_DATA_CACHE_MTIME = None


def load_data():
    global _DATA_CACHE, _DATA_CACHE_MTIME
    try:
        current_mtime = DATA_FILE.stat().st_mtime_ns if DATA_FILE.exists() else None
    except OSError:
        current_mtime = None
    if _DATA_CACHE is not None and current_mtime == _DATA_CACHE_MTIME:
        return _DATA_CACHE
    if not DATA_FILE.exists():
        save_data(default_data())
    try:
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        data = default_data()
    base = default_data()
    for key, value in base.items():
        data.setdefault(key, value)
    for key, value in base["settings"].items():
        data["settings"].setdefault(key, value)
    data.setdefault("api", {})
    data.setdefault("userbot", {})
    data.setdefault("stats", {})
    data.setdefault("copied", {})
    _DATA_CACHE = data
    try:
        _DATA_CACHE_MTIME = DATA_FILE.stat().st_mtime_ns
    except OSError:
        _DATA_CACHE_MTIME = None
    return data


def save_data(data):
    global _DATA_CACHE, _DATA_CACHE_MTIME
    DATA_FILE.write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    _DATA_CACHE = data
    try:
        _DATA_CACHE_MTIME = DATA_FILE.stat().st_mtime_ns
    except OSError:
        _DATA_CACHE_MTIME = None


def now_ts() -> int:
    return int(time.time())


def fmt_date(timestamp) -> str:
    try:
        timestamp = int(timestamp or 0)
    except (TypeError, ValueError):
        timestamp = 0
    if not timestamp:
        return "—"
    return datetime.fromtimestamp(timestamp).strftime("%d.%m.%Y %H:%M")


def fmt_size(num_bytes: int) -> str:
    value = float(num_bytes or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit in ("B", "KB") else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def is_owner(user_id) -> bool:
    return bool(OWNER_ID) and int(user_id or 0) == OWNER_ID


def get_api():
    data = load_data()
    api = data.get("api", {}) or {}
    api_id = api.get("api_id") or os.getenv("API_ID", "")
    api_hash = api.get("api_hash") or os.getenv("API_HASH", "")
    try:
        api_id = int(str(api_id).strip())
    except (TypeError, ValueError):
        api_id = 0
    return api_id, str(api_hash or "").strip()


def get_session() -> str:
    data = load_data()
    return str((data.get("userbot", {}) or {}).get("session") or os.getenv("USERBOT_SESSION", "")).strip()


# ================== USERBOT (AKKAUNT) ==================
_client = None
_login_client = None
_run_lock = asyncio.Lock()
_client_lock = asyncio.Lock()
_background_tasks = set()


def spawn(coro):
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


async def close_client():
    global _client
    if _client is not None:
        try:
            await _client.disconnect()
        except Exception:
            pass
        _client = None


async def userbot_client():
    """Saqlangan session orqali akkauntga ulanadi; bo'lmasa None qaytaradi."""
    global _client
    if _client is not None and _client.is_connected():
        try:
            if await _client.is_user_authorized():
                return _client
        except Exception:
            pass
    async with _client_lock:
        if _client is not None and _client.is_connected():
            try:
                if await _client.is_user_authorized():
                    return _client
            except Exception:
                pass
        session = get_session()
        api_id, api_hash = get_api()
        if not session or not api_id or not api_hash:
            return None
        await close_client()
        _client = TelegramClient(
            StringSession(session), api_id, api_hash,
            connection_retries=5, retry_delay=2, auto_reconnect=True,
        )
        try:
            await _client.connect()
            if not await _client.is_user_authorized():
                await close_client()
                return None
        except Exception:
            logging.exception("Userbotga ulanishda xatolik")
            await close_client()
            return None
        return _client


async def login_abort():
    global _login_client
    if _login_client is not None:
        try:
            await _login_client.disconnect()
        except Exception:
            pass
        _login_client = None


async def login_start(phone: str):
    """Kod so'rovini yuboradi va phone_code_hash qaytaradi."""
    global _login_client
    api_id, api_hash = get_api()
    if not api_id or not api_hash:
        raise RuntimeError("API_ID va API_HASH kiritilmagan.")
    await login_abort()
    _login_client = TelegramClient(StringSession(), api_id, api_hash)
    try:
        await _login_client.connect()
        sent = await _login_client.send_code_request(phone)
    except Exception:
        await login_abort()
        raise
    return sent.phone_code_hash


async def login_code(phone: str, code: str, phone_code_hash: str):
    """Kodni tasdiqlaydi. 'ok' yoki 'password' qaytaradi."""
    if _login_client is None:
        raise RuntimeError("Kirish sessiyasi yo'qoldi. «🔐 Kirish» ni qaytadan bosing.")
    try:
        await _login_client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
    except errors.SessionPasswordNeededError:
        return "password"
    return "ok"


async def login_password(password: str):
    if _login_client is None:
        raise RuntimeError("Kirish sessiyasi yo'qoldi. «🔐 Kirish» ni qaytadan bosing.")
    await _login_client.sign_in(password=password)
    return "ok"


async def login_finish(phone: str):
    """Muvaffaqiyatli kirishdan keyin sessionni saqlaydi."""
    global _client, _login_client
    if _login_client is None:
        raise RuntimeError("Kirish sessiyasi yo'qoldi. «🔐 Kirish» ni qaytadan bosing.")
    await _login_client.connect()
    me = await _login_client.get_me()
    session_string = _login_client.session.save()
    data = load_data()
    data["userbot"] = {"session": session_string, "phone": phone}
    save_data(data)
    _client = _login_client
    _login_client = None
    return me


async def logout_userbot():
    global _client
    if _client is None:
        await userbot_client()
    if _client is not None:
        try:
            await _client.log_out()
        except Exception:
            logging.exception("Logout xatosi")
        try:
            await _client.disconnect()
        except Exception:
            pass
    _client = None
    data = load_data()
    data["userbot"] = {"session": "", "phone": ""}
    save_data(data)


# ================== KANAL VA MAVZULAR ==================
def parse_chat_ref(raw: str):
    value = str(raw or "").strip()
    if not value:
        return ""
    lowered = value.lower()
    for prefix in (
        "https://t.me/", "http://t.me/", "t.me/",
        "https://telegram.me/", "http://telegram.me/", "telegram.me/",
    ):
        if lowered.startswith(prefix):
            value = value[len(prefix):].split("?", 1)[0].split("#", 1)[0]
            parts = [part for part in value.split("/") if part]
            if not parts:
                return ""
            if parts[0].lower() in ("s", "c") and len(parts) > 1:
                # t.me/c/... — yopiq kanal xabari havolasi, u ID emas.
                # t.me/s/kanal — ochiq kanal ko'rinishi.
                if parts[0].lower() == "c":
                    return ""
                parts = parts[1:]
            if parts[0].startswith("+") or parts[0].lower() == "joinchat":
                return ""  # taklif havolasi: @username yoki ID kerak
            return "@" + parts[0]
    if value.startswith("@"):
        return value
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    if value.lower().startswith(("https://", "http://")):
        return ""
    if re.search(r"\s", value):
        return ""
    return "@" + value


async def resolve_chat(client, ref):
    parsed = parse_chat_ref(ref)
    if parsed == "":
        raise RuntimeError("Kanal havolasi noto'g'ri. @username yoki -100... ID yuboring.")
    try:
        return await client.get_entity(parsed)
    except Exception:
        # Kanal username o'zgargan bo'lishi mumkin: dialoglar orasidan ID bo'yicha izlaymiz.
        target = str(parsed).lstrip("@")
        if target.startswith("-100"):
            target = target[4:]
        elif target.startswith("-"):
            target = target[1:]
        async for dialog in client.iter_dialogs():
            entity = dialog.entity
            entity_id = str(getattr(entity, "id", ""))
            username = str(getattr(entity, "username", "") or "")
            if entity_id == target or (username and username.lower() == target.lower()):
                return entity
        raise


def channel_title(entity) -> str:
    title = getattr(entity, "title", "") or getattr(entity, "username", "") or ""
    return str(title)[:120]


async def check_topic_rights(client, entity):
    """Manzil kanalda mavzu ochish huquqini tekshiradi."""
    if not isinstance(entity, types.Channel):
        return False, "Manzil oddiy kanal emas."
    if not getattr(entity, "forum", False):
        return False, "Manzil kanalda «Mavzular» (Topics) yoqilmagan."
    try:
        res = await client(functions.channels.GetParticipantRequest(channel=entity, participant="me"))
    except Exception as exc:
        return False, f"Kanal a'zoligini tekshirib bo'lmadi: {exc}"
    participant = res.participant
    if isinstance(participant, types.ChannelParticipantCreator):
        return True, ""
    rights = getattr(participant, "admin_rights", None)
    if isinstance(participant, types.ChannelParticipantAdmin) and rights:
        if not getattr(rights, "manage_topics", False):
            return False, "Manzil kanalda «Mavzularni boshqarish» huquqi kerak."
        if not getattr(rights, "post_messages", False):
            return False, "Manzil kanalda xabar yuborish (post_messages) huquqi kerak."
        return True, ""
    return False, "Manzil kanalda admin bo'lishingiz kerak."


def _topic_title_key(title: str) -> str:
    return re.sub(r"\s+", " ", str(title or "").strip().casefold())


async def find_topic(client, dest, title: str):
    needle = _topic_title_key(title)
    variants = (
        {"offset_date": None, "offset_id": 0, "offset_topic": 0, "limit": 100, "q": str(title)[:100]},
        {"offset_date": None, "offset_id": 0, "offset_topic": 0, "limit": 100, "q": None},
    )
    for params in variants:
        try:
            res = await client(functions.messages.GetForumTopicsRequest(peer=dest, **params))
        except Exception:
            logging.warning("Mavzularni qidirishda xatolik", exc_info=True)
            continue
        for topic in (getattr(res, "topics", None) or []):
            if isinstance(topic, types.ForumTopic) and _topic_title_key(topic.title) == needle:
                return topic.id
    return None


async def create_topic(client, dest, title: str):
    clean = re.sub(r"\s+", " ", str(title or "")).strip()[:128] or "Mavzu"
    res = await client(functions.messages.CreateForumTopicRequest(
        peer=dest, title=clean, random_id=random.getrandbits(63),
    ))
    for update in (getattr(res, "updates", None) or []):
        message = getattr(update, "message", None)
        if message is None:
            continue
        if isinstance(getattr(message, "action", None), types.MessageActionTopicCreate):
            return message.id
    return await find_topic(client, dest, clean)


async def ensure_topic(client, dest, title: str):
    topic_id = await find_topic(client, dest, title)
    if topic_id:
        return topic_id, False
    topic_id = await create_topic(client, dest, title)
    if topic_id:
        return topic_id, True
    topic_id = await find_topic(client, dest, title)
    if topic_id:
        return topic_id, True
    raise RuntimeError(f"«{title}» mavzusini yaratib bo'lmadi")


# ================== NOMLARNI ANIQLASH ==================
_URL_RE = re.compile(r"(?:https?://\S+|t\.me/\S+|@[A-Za-z0-9_]{4,})", re.I)
_EXT_RE = re.compile(r"\.(?:mp4|mkv|avi|mov|wmv|flv|webm|m4v|ts|m2ts|rar|zip)\b", re.I)
_SE_RE = re.compile(r"\bS(\d{1,2})\s*[._-]?\s*E(\d{1,3})\b", re.I)
_EP_MARKER_RE = re.compile(
    r"(?<!\d)(\d{1,4})\s*[-–—_.]?\s*"
    r"(?:qism(?:i|da|dagi|lar|larida|ning)?|epizod|episode|ep|seriya|seria|"
    r"bo['’ʻ]lim(?:i)?|bolim(?:i)?|part)\b",
    re.I,
)
_SEASON_MARKER_RE = re.compile(
    r"(?<!\d)(\d{1,2})\s*[-–—_.]?\s*(?:fasl(?:i)?|sezon|season|mavsum)\b", re.I
)
_HASH_EP_RE = re.compile(r"(?:^|[\s|(])#(\d{1,3})(?=$|[\s|)])")
_E_RE = re.compile(r"\bE(?:P)?\s*[._-]?\s*(\d{1,3})\b", re.I)
_QUALITY_RE = re.compile(
    r"\b(?:\d{3,4}[pi]|4k|8k|uhd|fhd|full\s*hd|hdrip|web[- ]?dl|webrip|hdtv|bluray|brrip|dvdrip|camrip)\b",
    re.I,
)
_LANG_RE = re.compile(
    r"\b(?:uzbek|o['’ʻ]zbek|ozbek|узбек|rus|russian|ruscha|hind|hindi|turk|turkish|"
    r"tarjima|tarjimon|tilida|dublyaj|dublaj|subtitr(?:i)?)\b",
    re.I,
)


def parse_media_title(raw: str):
    """Sarlavhadan serial nomi, fasl va qism raqamini ajratadi.

    «Hovli 1 qism uzbek tilida» -> ("Hovli", None, 1)
    """
    text = unicodedata.normalize("NFKC", str(raw or ""))
    text = _URL_RE.sub(" ", text)
    text = _EXT_RE.sub(" ", text)
    text = text.replace("_", " ").replace("|", " ")

    season = episode = None
    match = _SE_RE.search(text)
    if match:
        season, episode = int(match.group(1)), int(match.group(2))
        text = _SE_RE.sub(" ", text)
    else:
        match = _EP_MARKER_RE.search(text)
        if match:
            episode = int(match.group(1))
            text = _EP_MARKER_RE.sub(" ", text)
        else:
            match = _HASH_EP_RE.search(text)
            if match:
                episode = int(match.group(1))
                text = _HASH_EP_RE.sub(" ", text)
            else:
                match = _E_RE.search(text)
                if match:
                    episode = int(match.group(1))
                    text = _E_RE.sub(" ", text)
        match = _SEASON_MARKER_RE.search(text)
        if match:
            season = int(match.group(1))
            text = _SEASON_MARKER_RE.sub(" ", text)

    text = _QUALITY_RE.sub(" ", text)
    text = _LANG_RE.sub(" ", text)
    text = re.sub(r"#\w+", " ", text)
    text = re.sub(r"(?<!\d)(?:19|20)\d{2}(?!\d)", " ", text)
    text = re.sub(r"[\[\](){}<>«»,;:•·/\\]+", " ", text)
    text = re.sub(r"\s*[-–—.]+\s*", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .-–—_")
    if not text or re.fullmatch(r"[\d\s.+-]+", text):
        return "", season, episode
    return text[:100], season, episode


def title_key(title: str) -> str:
    return re.sub(r"\s+", " ", str(title or "").casefold()).strip()


def video_document(message):
    doc = getattr(message, "document", None)
    if not doc:
        return None
    for attr in (doc.attributes or []):
        if isinstance(attr, (types.DocumentAttributeAnimated, types.DocumentAttributeSticker)):
            return None
        if isinstance(attr, types.DocumentAttributeVideo):
            if getattr(attr, "round_message", False):
                return None
            return doc
    if str(doc.mime_type or "").lower().startswith("video/"):
        return doc
    return None


def document_filename(doc) -> str:
    for attr in (doc.attributes or []):
        if isinstance(attr, types.DocumentAttributeFilename):
            return attr.file_name or ""
    return ""


# ================== SKANERLASH ==================
async def scan_source(client, source, min_size: int, on_progress=None):
    items = []
    seen_docs = set()
    scanned = 0
    async for message in client.iter_messages(source, reverse=True, wait_time=0.4):
        scanned += 1
        doc = video_document(message)
        if doc is None or int(doc.size or 0) <= min_size:
            continue
        if doc.id in seen_docs:
            continue
        seen_docs.add(doc.id)
        raw_title = (message.message or "").strip() or document_filename(doc)
        name, season, episode = parse_media_title(raw_title)
        items.append({
            "msg_id": message.id,
            "grouped_id": message.grouped_id,
            "doc_id": doc.id,
            "document": doc,
            "caption": (message.message or "")[:1024],
            "size": int(doc.size or 0),
            "name": name,
            "key": title_key(name),
            "season": season,
            "episode": episode,
            "date": int(message.date.timestamp()) if message.date else 0,
        })
        if on_progress and scanned % 500 == 0:
            await on_progress(scanned, len(items))
    return items, scanned


async def existing_doc_ids(client, dest):
    ids = set()
    async for message in client.iter_messages(dest, wait_time=0.4):
        doc = getattr(message, "document", None)
        if doc is not None:
            ids.add(doc.id)
    return ids


def chunk_albums(records):
    batches = []
    for record in records:
        if (
            batches
            and record["grouped_id"]
            and batches[-1][0]["grouped_id"] == record["grouped_id"]
        ):
            batches[-1].append(record)
        else:
            batches.append([record])
    return batches


async def forward_batch(client, source, dest, topic_id, batch):
    """'forward' — forward qilindi, 'copy' — nusxa yuborildi, '' — xato."""
    ids = [record["msg_id"] for record in batch]
    for _attempt in range(3):
        try:
            await client(functions.messages.ForwardMessagesRequest(
                from_peer=source, id=ids, to_peer=dest, top_msg_id=topic_id,
                random_id=[random.getrandbits(63) for _ in ids],
            ))
            return "forward"
        except errors.FloodWaitError as exc:
            await asyncio.sleep(min(int(exc.seconds) + 1, 900))
        except errors.ChatForwardsRestrictedError:
            break
        except Exception as exc:
            logging.warning("Forward xatosi (ids=%s): %s", ids, exc)
            break

    # Forward taqiqlangan bo'lsa, nusxa sifatida yuborishga urinamiz.
    for attempt in range(2):
        try:
            for record in batch:
                await client.send_file(
                    dest,
                    record["document"],
                    caption=record["caption"] or "",
                    reply_to=topic_id,
                )
            return "copy"
        except errors.FloodWaitError as exc:
            await asyncio.sleep(min(int(exc.seconds) + 1, 900))
        except Exception as exc:
            logging.warning("Nusxa yuborishda xatolik: %s", exc)
            if attempt == 1:
                return ""
    return ""


def remember_copies(records):
    """Nusxa (copy) yuborilganda fayl ID o'zgaradi — takrorlmasligi uchun manba ID sini saqlaymiz."""
    data = load_data()
    copied = data.setdefault("copied", {})
    stamp = now_ts()
    for record in records:
        copied[str(record["doc_id"])] = stamp
    if len(copied) > 50000:
        newest = sorted(copied.items(), key=lambda kv: kv[1], reverse=True)[:40000]
        data["copied"] = dict(newest)
    save_data(data)


# ================== FORWARD JARAYONI ==================
async def _notify(chat_id, text):
    if not chat_id:
        return
    try:
        await bot.send_message(chat_id, text)
    except Exception:
        logging.exception("Xabar yuborilmadi")


async def run_forward(notify_chat_id=None):
    if _run_lock.locked():
        await _notify(notify_chat_id, "⏳ Forward allaqachon ketmoqda. Tugashini kuting.")
        return None
    async with _run_lock:
        settings = load_data().get("settings", {})
        source_ref = str(settings.get("source") or "").strip()
        dest_ref = str(settings.get("dest") or "").strip()
        if not source_ref or not dest_ref:
            await _notify(notify_chat_id, "❌ Avval manba va manzil kanallarni tanlang.")
            return None

        client = await userbot_client()
        if client is None:
            await _notify(notify_chat_id, "❌ Akkaunt ulanmagan. «🔐 Akkauntga kirish» bo'limidan kiring.")
            return None

        status_msg = None
        if notify_chat_id:
            try:
                status_msg = await bot.send_message(notify_chat_id, "🔄 Forward boshlandi…")
            except Exception:
                status_msg = None

        async def set_status(text):
            if status_msg is not None:
                try:
                    await status_msg.edit_text(text)
                except Exception:
                    pass

        started = time.time()
        try:
            source = await resolve_chat(client, source_ref)
            dest = await resolve_chat(client, dest_ref)
        except Exception as exc:
            await set_status(f"❌ Kanal topilmadi: <code>{_esc(str(exc))}</code>")
            return None

        min_mb = max(0, int(settings.get("min_size_mb", 100) or 100))
        min_size = min_mb * 1024 * 1024
        movies_topic = str(settings.get("movies_topic") or "Kinolar").strip() or "Kinolar"
        min_eps = max(2, int(settings.get("series_min_episodes", 2) or 2))

        await set_status("🔎 Manzil kanaldagi mavjud fayllar tekshirilmoqda…")
        try:
            existing = await existing_doc_ids(client, dest)
        except Exception as exc:
            await set_status(f"❌ Manzil kanalni o'qib bo'lmadi: <code>{_esc(str(exc))}</code>")
            return None
        for doc_id in (load_data().get("copied", {}) or {}):
            try:
                existing.add(int(doc_id))
            except (TypeError, ValueError):
                continue

        async def scan_progress(scanned, found):
            await set_status(
                f"📤 Manba skanerlanmoqda…\n"
                f"Ko'rildi: <b>{scanned}</b> xabar\n"
                f"{min_mb} MB dan katta video: <b>{found}</b> ta"
            )

        await set_status("📤 Manba kanal skanerlanmoqda…")
        try:
            items, scanned_count = await scan_source(client, source, min_size, scan_progress)
        except Exception as exc:
            await set_status(f"❌ Manba kanalni o'qib bo'lmadi: <code>{_esc(str(exc))}</code>")
            return None

        skipped_existing = sum(1 for item in items if item["doc_id"] in existing)
        pending = [item for item in items if item["doc_id"] not in existing]

        # Serial ekanini butun manba bo'yicha aniqlaymiz (kanalda avvaldan bor qismlar ham hisobga olinadi).
        counts = Counter(
            item["key"] for item in items if item["key"] and item["episode"] is not None
        )
        groups = defaultdict(list)
        for item in pending:
            is_series = bool(item["key"]) and item["episode"] is not None and counts[item["key"]] >= min_eps
            topic_title = item["name"] if is_series else movies_topic
            groups[topic_title].append(item)

        for records in groups.values():
            records.sort(key=lambda r: (
                r["season"] or 0,
                r["episode"] if r["episode"] is not None else 10 ** 6,
                r["date"],
                r["msg_id"],
            ))

        forwarded = 0
        copied = 0
        failed = 0
        created_topics = 0
        for topic_title, records in groups.items():
            try:
                topic_id, was_created = await ensure_topic(client, dest, topic_title)
            except Exception as exc:
                logging.exception("Mavzu xatosi: %s", exc)
                failed += len(records)
                continue
            if was_created:
                created_topics += 1

            for batch in chunk_albums(records):
                result = await forward_batch(client, source, dest, topic_id, batch)
                if result == "forward":
                    forwarded += len(batch)
                    existing.update(record["doc_id"] for record in batch)
                elif result == "copy":
                    copied += len(batch)
                    existing.update(record["doc_id"] for record in batch)
                    remember_copies(batch)
                else:
                    failed += len(batch)
                done = forwarded + copied
                if done and done % 25 < len(batch):
                    await set_status(
                        f"📤 Yuborilmoqda…\n✅ Forward: <b>{forwarded}</b> ta\n"
                        f"📋 Nusxa: <b>{copied}</b> ta\n"
                        f"🏷 Mavzu: {_esc(topic_title)}"
                    )

        elapsed = int(time.time() - started)
        stats = {
            "last_run": now_ts(),
            "scanned": scanned_count,
            "found": len(items),
            "forwarded": forwarded,
            "copied": copied,
            "skipped_existing": skipped_existing,
            "failed": failed,
            "topics_created": created_topics,
            "elapsed": elapsed,
            "source_title": channel_title(source),
            "dest_title": channel_title(dest),
        }
        data = load_data()
        data["stats"] = stats
        save_data(data)

        summary = (
            f"🏁 <b>Forward tugadi</b>\n"
            f"📥 Ko'rilgan xabar: {scanned_count}\n"
            f"🎬 {min_mb} MB dan katta video: {len(items)}\n"
            f"✅ Yuborildi: <b>{forwarded}</b>\n"
            f"📋 Nusxa sifatida: {copied}\n"
            f"♻️ Allaqachon bor (o'tkazildi): {skipped_existing}\n"
            f"🆕 Yangi mavzular: {created_topics}\n"
            f"⚠️ Xatolar: {failed}\n"
            f"⏱ Vaqt: {elapsed} soniya"
        )
        if notify_chat_id:
            await set_status(summary)
        elif forwarded or failed:
            await _notify(OWNER_ID, "🔄 <b>Avto-forward</b>\n" + summary)
        return stats


async def auto_loop():
    await asyncio.sleep(10)
    while True:
        try:
            settings = load_data().get("settings", {})
            if settings.get("auto") and get_session():
                interval = max(5, int(settings.get("auto_interval_min", 30) or 30)) * 60
                last_run = int(load_data().get("stats", {}).get("last_run", 0) or 0)
                if now_ts() - last_run >= interval:
                    await run_forward(notify_chat_id=None)
        except Exception:
            logging.exception("Avto-forward xatosi")
        await asyncio.sleep(30)


# ================== UI ==================
class Form(StatesGroup):
    phone = State()
    code = State()
    password = State()
    api = State()
    source = State()
    dest = State()
    min_size = State()
    movies_topic = State()
    series_min = State()
    interval = State()


def panel_text():
    data = load_data()
    settings = data["settings"]
    stats = data.get("stats", {})
    api_id, api_hash = get_api()
    linked = "✅ ulangan" if get_session() else "❌ ulanmagan"
    auto = "🟢 yoqilgan" if settings.get("auto") else "🔴 o'chirilgan"
    lines = [
        "🤖 <b>Avto-Forward bot</b>",
        "",
        f"👤 Akkaunt: {linked}",
        f"🔑 API: {'✅ bor' if api_id and api_hash else '❌ kiritilmagan'}",
        f"📥 Manba: {_esc(settings.get('source_title') or settings.get('source') or 'tanlanmagan')}",
        f"📤 Manzil: {_esc(settings.get('dest_title') or settings.get('dest') or 'tanlanmagan')}",
        f"📦 Minimal hajm: {settings.get('min_size_mb', 100)} MB",
        f"🎬 Kinolar mavzusi: {_esc(settings.get('movies_topic') or 'Kinolar')}",
        f"🔁 Avto: {auto} (har {settings.get('auto_interval_min', 30)} daqiqa)",
    ]
    if stats:
        lines += [
            "",
            f"🕒 Oxirgi ishga tushish: {fmt_date(stats.get('last_run'))}",
            f"✅ Oxirgi forward: {stats.get('forwarded', 0)} ta",
        ]
    lines += ["", "Kanallar nomli mavzularga ajratib forward qilinadi."]
    return "\n".join(lines)


def panel_kb():
    settings = load_data()["settings"]
    linked = bool(get_session())
    auto = bool(settings.get("auto"))
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=("👤 Akkaunt: ulangan" if linked else "🔐 Akkauntga kirish"),
            callback_data="account",
        )],
        [
            InlineKeyboardButton(text="📥 Manba kanal", callback_data="set_source"),
            InlineKeyboardButton(text="📤 Manzil kanal", callback_data="set_dest"),
        ],
        [InlineKeyboardButton(text="⚙️ Sozlamalar", callback_data="cfg")],
        [InlineKeyboardButton(text="🚀 Forwardni boshlash", callback_data="run_now")],
        [InlineKeyboardButton(
            text=("🟢 Avto: YOQILGAN" if auto else "🔴 Avto: O'CHIRILGAN"),
            callback_data="toggle_auto",
        )],
        [InlineKeyboardButton(text="📊 Statistika", callback_data="stats")],
    ])


def account_kb(linked: bool):
    rows = []
    if linked:
        rows.append([InlineKeyboardButton(text="🚪 Akkauntdan chiqish", callback_data="logout")])
    else:
        rows.append([InlineKeyboardButton(text="🔐 Kirish (telefon + kod)", callback_data="login")])
    rows.append([InlineKeyboardButton(text="🔑 API_ID / API_HASH", callback_data="set_api")])
    rows.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cfg_kb():
    settings = load_data()["settings"]
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"📦 Minimal hajm: {settings.get('min_size_mb', 100)} MB", callback_data="cfg:min_size")],
        [InlineKeyboardButton(text=f"🎬 Kinolar mavzusi: {settings.get('movies_topic', 'Kinolar')}", callback_data="cfg:movies_topic")],
        [InlineKeyboardButton(text=f"🔢 Serial uchun minimal qism: {settings.get('series_min_episodes', 2)}", callback_data="cfg:series_min")],
        [InlineKeyboardButton(text=f"🔁 Avto oraliq: {settings.get('auto_interval_min', 30)} daqiqa", callback_data="cfg:interval")],
        [InlineKeyboardButton(text="🧹 Statistika va holatni tozalash", callback_data="reset_stats")],
        [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="panel")],
    ])


async def show_panel(target, edit=False):
    text = panel_text()
    markup = panel_kb()
    if edit:
        try:
            await target.edit_text(text, reply_markup=markup)
            return
        except Exception:
            pass
    await target.answer(text, reply_markup=markup)


# ================== HANDLERLAR ==================
@dp.message(CommandStart())
async def start_handler(message: Message, state: FSMContext):
    await state.clear()
    if not is_owner(message.from_user.id):
        return await message.answer(
            f"🔒 Bu bot shaxsiy.\nSizning ID: <code>{message.from_user.id}</code>"
        )
    await message.answer(panel_text(), reply_markup=panel_kb())


@dp.message(Command("panel"))
async def panel_command(message: Message, state: FSMContext):
    await state.clear()
    if not is_owner(message.from_user.id):
        return
    await message.answer(panel_text(), reply_markup=panel_kb())


@dp.message(Command("cancel"))
async def cancel_handler(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id):
        return
    await state.clear()
    await login_abort()
    await message.answer("✅ Bekor qilindi.", reply_markup=panel_kb())


@dp.callback_query(F.data == "panel")
async def panel_cb(call: CallbackQuery, state: FSMContext):
    if not is_owner(call.from_user.id):
        return await call.answer("⛔ Shaxsiy bot", show_alert=True)
    await state.clear()
    await show_panel(call.message, edit=True)
    await call.answer()


@dp.callback_query(F.data == "account")
async def account_cb(call: CallbackQuery, state: FSMContext):
    if not is_owner(call.from_user.id):
        return await call.answer("⛔ Shaxsiy bot", show_alert=True)
    await state.clear()
    data = load_data()
    phone = (data.get("userbot", {}) or {}).get("phone") or "—"
    linked = bool(get_session())
    text = (
        "👤 <b>Telegram akkaunt</b>\n\n"
        f"Holat: {'✅ ulangan' if linked else '❌ ulanmagan'}\n"
        f"Telefon: <code>{_esc(phone)}</code>\n\n"
        "Akkaunt orqali forward qilinadi. Kirishda kod va 2 bosqichli parol so'raladi."
    )
    try:
        await call.message.edit_text(text, reply_markup=account_kb(linked))
    except Exception:
        await call.message.answer(text, reply_markup=account_kb(linked))
    await call.answer()


@dp.callback_query(F.data == "login")
async def login_cb(call: CallbackQuery, state: FSMContext):
    if not is_owner(call.from_user.id):
        return await call.answer("⛔ Shaxsiy bot", show_alert=True)
    api_id, api_hash = get_api()
    if not api_id or not api_hash:
        return await call.answer("❌ Avval API_ID va API_HASH kiriting", show_alert=True)
    await state.set_state(Form.phone)
    await call.message.answer(
        "📱 <b>Telefon raqam</b>\nXalqaro formatda yuboring, masalan:\n"
        "<code>+998901234567</code>\n\nBekor qilish: /cancel"
    )
    await call.answer()


@dp.message(Form.phone)
async def phone_handler(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id):
        await state.clear()
        return
    phone = re.sub(r"\D", "", message.text or "")
    if phone.startswith("00"):
        phone = phone[2:]
    if len(phone) < 8 or len(phone) > 15:
        return await message.answer("⚠️ Raqam noto'g'ri. Masalan: <code>+998901234567</code>")
    phone = "+" + phone
    try:
        code_hash = await login_start(phone)
    except errors.FloodWaitError as exc:
        return await message.answer(f"⏳ Telegram limit: {exc.seconds} soniyadan keyin urinib ko'ring.")
    except errors.ApiIdInvalidError:
        await state.clear()
        return await message.answer("❌ API_ID yoki API_HASH noto'g'ri.", reply_markup=panel_kb())
    except Exception as exc:
        logging.exception("Kod yuborishda xatolik")
        return await message.answer(f"❌ Kod yuborilmadi: <code>{_esc(str(exc))}</code>")
    await state.update_data(phone=phone, phone_code_hash=code_hash)
    await state.set_state(Form.code)
    try:
        await message.delete()
    except Exception:
        pass
    await message.answer(
        "🔢 <b>Kod</b>\nTelegram yuborgan kodni kiriting (masalan <code>12345</code> "
        "yoki <code>1.2.3.4</code>).\n\nBekor qilish: /cancel"
    )


@dp.message(Form.code)
async def code_handler(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id):
        await state.clear()
        return
    code = re.sub(r"\D", "", message.text or "")
    ctx = await state.get_data()
    phone = ctx.get("phone")
    code_hash = ctx.get("phone_code_hash")
    if not code:
        return await message.answer("⚠️ Kod faqat raqamlardan iborat. Qayta yuboring:")
    if not phone or not code_hash:
        await state.clear()
        await login_abort()
        return await message.answer(
            "❌ Kirish sessiyasi topilmadi. «🔐 Akkauntga kirish» ni qaytadan bosing.",
            reply_markup=panel_kb(),
        )
    try:
        result = await login_code(phone, code, code_hash)
    except errors.PhoneCodeInvalidError:
        return await message.answer("❌ Kod xato. Qayta kiriting:")
    except errors.PhoneCodeExpiredError:
        await state.clear()
        await login_abort()
        return await message.answer("❌ Kod muddati tugagan. Qaytadan «🔐 Kirish» ni bosing.")
    except errors.FloodWaitError as exc:
        return await message.answer(f"⏳ Telegram limit: {exc.seconds} soniya kuting.")
    except Exception as exc:
        logging.exception("Kodni tasdiqlashda xatolik")
        return await message.answer(f"❌ Xatolik: <code>{_esc(str(exc))}</code>")

    try:
        await message.delete()
    except Exception:
        pass

    if result == "password":
        await state.set_state(Form.password)
        return await message.answer(
            "🔐 <b>2 bosqichli himoya</b>\nAkkaunt parolini yuboring.\n\nBekor qilish: /cancel"
        )

    try:
        me = await login_finish(phone)
    except Exception as exc:
        logging.exception("Kirishni yakunlashda xatolik")
        await state.clear()
        return await message.answer(
            f"❌ Kirishni yakunlab bo'lmadi: <code>{_esc(str(exc))}</code>",
            reply_markup=panel_kb(),
        )
    await state.clear()
    name = getattr(me, "username", None) or getattr(me, "first_name", "") or "akkaunt"
    await message.answer(
        f"✅ Akkaunt ulandi: <b>{_esc(str(name))}</b>",
        reply_markup=panel_kb(),
    )


@dp.message(Form.password)
async def password_handler(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id):
        await state.clear()
        return
    password = message.text or ""
    if not password:
        return await message.answer("⚠️ Parolni yuboring. Bekor qilish: /cancel")
    ctx = await state.get_data()
    phone = ctx.get("phone", "")
    try:
        await login_password(password)
    except errors.PasswordHashInvalidError:
        return await message.answer("❌ Parol xato. Qayta kiriting:")
    except errors.FloodWaitError as exc:
        return await message.answer(f"⏳ Telegram limit: {exc.seconds} soniya kuting.")
    except Exception as exc:
        logging.exception("Parolni tasdiqlashda xatolik")
        return await message.answer(f"❌ Xatolik: <code>{_esc(str(exc))}</code>")

    try:
        await message.delete()
    except Exception:
        pass

    try:
        me = await login_finish(phone)
    except Exception as exc:
        logging.exception("Kirishni yakunlashda xatolik")
        await state.clear()
        return await message.answer(
            f"❌ Kirishni yakunlab bo'lmadi: <code>{_esc(str(exc))}</code>",
            reply_markup=panel_kb(),
        )
    await state.clear()
    name = getattr(me, "username", None) or getattr(me, "first_name", "") or "akkaunt"
    await message.answer(
        f"✅ Akkaunt ulandi: <b>{_esc(str(name))}</b>\nEndi kanallarni tanlang.",
        reply_markup=panel_kb(),
    )


@dp.callback_query(F.data == "logout")
async def logout_cb(call: CallbackQuery):
    if not is_owner(call.from_user.id):
        return await call.answer("⛔ Shaxsiy bot", show_alert=True)
    await logout_userbot()
    await call.answer("✅ Akkauntdan chiqildi")
    await show_panel(call.message, edit=True)


@dp.callback_query(F.data == "set_api")
async def set_api_cb(call: CallbackQuery, state: FSMContext):
    if not is_owner(call.from_user.id):
        return await call.answer("⛔ Shaxsiy bot", show_alert=True)
    api_id, api_hash = get_api()
    await state.set_state(Form.api)
    await call.message.answer(
        "🔑 <b>API_ID va API_HASH</b>\n"
        "my.telegram.org → API development tools bo'limidan oling.\n\n"
        "Ikkalasini bitta xabarda yuboring:\n"
        "<code>1234567:0123456789abcdef0123456789abcdef</code>\n"
        f"Hozirgi API_ID: <code>{api_id or 'kiritilmagan'}</code>\n"
        "Bekor qilish: /cancel"
    )
    await call.answer()


@dp.message(Form.api)
async def api_handler(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id):
        await state.clear()
        return
    raw = (message.text or "").replace("\n", " ").strip()
    match = re.fullmatch(r"(\d{3,12})\s*[:;,\s]\s*([0-9a-fA-F]{20,40})", raw)
    if not match:
        return await message.answer(
            "⚠️ Format: <code>API_ID:API_HASH</code>. Masalan "
            "<code>1234567:0123456789abcdef0123456789abcdef</code>"
        )
    data = load_data()
    data["api"] = {"api_id": int(match.group(1)), "api_hash": match.group(2).lower()}
    save_data(data)
    await close_client()
    await state.clear()
    try:
        await message.delete()
    except Exception:
        pass
    await message.answer("✅ API ma'lumotlari saqlandi.", reply_markup=panel_kb())


@dp.callback_query(F.data == "set_source")
async def set_source_cb(call: CallbackQuery, state: FSMContext):
    if not is_owner(call.from_user.id):
        return await call.answer("⛔ Shaxsiy bot", show_alert=True)
    await state.set_state(Form.source)
    await call.message.answer(
        "📥 <b>Manba kanal</b>\n@username, -100... ID yoki https://t.me/kanal yuboring.\n"
        "Akkaunt bu kanalga a'zo bo'lishi kerak."
    )
    await call.answer()


@dp.message(Form.source)
async def source_handler(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id):
        await state.clear()
        return
    raw = (message.text or "").strip()
    parsed = parse_chat_ref(raw)
    if parsed == "":
        return await message.answer("⚠️ @username yoki -100... ID yuboring (taklif havolasi emas).")
    client = await userbot_client()
    if client is None:
        return await message.answer("❌ Avval akkauntga kiring.")
    try:
        entity = await resolve_chat(client, raw)
    except Exception as exc:
        return await message.answer(f"❌ Kanal topilmadi: <code>{_esc(str(exc))}</code>")
    username = getattr(entity, "username", None)
    stored = "@" + username if username else str(entity.id)
    data = load_data()
    data["settings"]["source"] = stored
    data["settings"]["source_title"] = channel_title(entity)
    save_data(data)
    await state.clear()
    await message.answer(
        f"✅ Manba saqlandi: <b>{_esc(channel_title(entity))}</b>\n<code>{_esc(stored)}</code>",
        reply_markup=panel_kb(),
    )


@dp.callback_query(F.data == "set_dest")
async def set_dest_cb(call: CallbackQuery, state: FSMContext):
    if not is_owner(call.from_user.id):
        return await call.answer("⛔ Shaxsiy bot", show_alert=True)
    await state.set_state(Form.dest)
    await call.message.answer(
        "📤 <b>Manzil kanal</b>\nMavzular (Topics) yoqilgan kanal bo'lishi kerak.\n"
        "Siz u yerda admin va «Mavzularni boshqarish» huquqiga ega bo'ling."
    )
    await call.answer()


@dp.message(Form.dest)
async def dest_handler(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id):
        await state.clear()
        return
    raw = (message.text or "").strip()
    if parse_chat_ref(raw) == "":
        return await message.answer("⚠️ @username yoki -100... ID yuboring.")
    client = await userbot_client()
    if client is None:
        return await message.answer("❌ Avval akkauntga kiring.")
    try:
        entity = await resolve_chat(client, raw)
        ok, problem = await check_topic_rights(client, entity)
    except Exception as exc:
        return await message.answer(f"❌ Kanal topilmadi: <code>{_esc(str(exc))}</code>")
    if not ok:
        return await message.answer(f"❌ {_esc(problem)}")
    username = getattr(entity, "username", None)
    stored = "@" + username if username else str(entity.id)
    data = load_data()
    data["settings"]["dest"] = stored
    data["settings"]["dest_title"] = channel_title(entity)
    save_data(data)
    await state.clear()
    await message.answer(
        f"✅ Manzil saqlandi: <b>{_esc(channel_title(entity))}</b>",
        reply_markup=panel_kb(),
    )


@dp.callback_query(F.data == "cfg")
async def cfg_cb(call: CallbackQuery, state: FSMContext):
    if not is_owner(call.from_user.id):
        return await call.answer("⛔ Shaxsiy bot", show_alert=True)
    await state.clear()
    try:
        await call.message.edit_text("⚙️ <b>Sozlamalar</b>", reply_markup=cfg_kb())
    except Exception:
        await call.message.answer("⚙️ <b>Sozlamalar</b>", reply_markup=cfg_kb())
    await call.answer()


@dp.callback_query(F.data.startswith("cfg:"))
async def cfg_prompt(call: CallbackQuery, state: FSMContext):
    if not is_owner(call.from_user.id):
        return await call.answer("⛔ Shaxsiy bot", show_alert=True)
    key = call.data.split(":", 1)[1]
    prompts = {
        "min_size": ("min_size", Form.min_size, "📦 Minimal video hajmini MB da yuboring (masalan 100):"),
        "movies_topic": ("movies_topic", Form.movies_topic, "🎬 Bir qismli kino va filmlar uchun mavzu nomini yuboring (masalan Kinolar):"),
        "series_min": ("series_min", Form.series_min, "🔢 Serial alohida mavzu ochilishi uchun minimal qism sonini yuboring (kamida 2):"),
        "interval": ("interval", Form.interval, "🔁 Avto tekshiruv oralig'ini daqiqada yuboring (kamida 5):"),
    }
    if key not in prompts:
        return await call.answer("⚠️ Noma'lum sozlama", show_alert=True)
    setting_key, state_value, prompt = prompts[key]
    await state.update_data(cfg_key=setting_key)
    await state.set_state(state_value)
    await call.message.answer(prompt + "\nBekor qilish: /cancel")
    await call.answer()


@dp.message(Form.min_size)
async def min_size_handler(message: Message, state: FSMContext):
    if await _save_numeric(message, state, "min_size_mb", 0, 4000):
        await message.answer("✅ Minimal hajm yangilandi.", reply_markup=cfg_kb())


@dp.message(Form.series_min)
async def series_min_handler(message: Message, state: FSMContext):
    if await _save_numeric(message, state, "series_min_episodes", 2, 100):
        await message.answer("✅ Minimal qism soni yangilandi.", reply_markup=cfg_kb())


@dp.message(Form.interval)
async def interval_handler(message: Message, state: FSMContext):
    if await _save_numeric(message, state, "auto_interval_min", 5, 1440):
        await message.answer("✅ Avto oraliq yangilandi.", reply_markup=cfg_kb())


async def _save_numeric(message: Message, state: FSMContext, key: str, low: int, high: int) -> bool:
    if not is_owner(message.from_user.id):
        await state.clear()
        return False
    raw = (message.text or "").strip()
    if raw == "/cancel":
        await state.clear()
        await message.answer("✅ Bekor qilindi.", reply_markup=panel_kb())
        return False
    try:
        value = int(raw)
    except ValueError:
        await message.answer("🔢 Faqat raqam yuboring:")
        return False
    if not low <= value <= high:
        await message.answer(f"⚠️ Qiymat {low} dan {high} gacha bo'lishi kerak. Qayta yuboring:")
        return False
    data = load_data()
    data["settings"][key] = value
    save_data(data)
    await state.clear()
    return True


@dp.message(Form.movies_topic)
async def movies_topic_handler(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id):
        await state.clear()
        return
    raw = (message.text or "").strip()
    if raw == "/cancel":
        await state.clear()
        return await message.answer("✅ Bekor qilindi.", reply_markup=panel_kb())
    if not raw or len(raw) > 100:
        return await message.answer("⚠️ 1–100 belgi kiriting:")
    data = load_data()
    data["settings"]["movies_topic"] = raw
    save_data(data)
    await state.clear()
    await message.answer(f"✅ Kinolar mavzusi: <b>{_esc(raw)}</b>", reply_markup=cfg_kb())


@dp.callback_query(F.data == "toggle_auto")
async def toggle_auto_cb(call: CallbackQuery):
    if not is_owner(call.from_user.id):
        return await call.answer("⛔ Shaxsiy bot", show_alert=True)
    data = load_data()
    data["settings"]["auto"] = not bool(data["settings"].get("auto"))
    save_data(data)
    state_text = "yoqildi" if data["settings"]["auto"] else "o'chirildi"
    await call.answer(f"✅ Avto rejim {state_text}")
    await show_panel(call.message, edit=True)


@dp.callback_query(F.data == "run_now")
async def run_now_cb(call: CallbackQuery):
    if not is_owner(call.from_user.id):
        return await call.answer("⛔ Shaxsiy bot", show_alert=True)
    settings = load_data()["settings"]
    if not settings.get("source") or not settings.get("dest"):
        return await call.answer("❌ Avval manba va manzil kanallarni tanlang", show_alert=True)
    if not get_session():
        return await call.answer("❌ Avval akkauntga kiring", show_alert=True)
    if _run_lock.locked():
        return await call.answer("⏳ Forward allaqachon ketmoqda", show_alert=True)
    await call.answer("🚀 Boshlandi. Natijani shu chatga yozaman.")
    spawn(run_forward(notify_chat_id=call.from_user.id))


@dp.callback_query(F.data == "reset_stats")
async def reset_stats_cb(call: CallbackQuery):
    if not is_owner(call.from_user.id):
        return await call.answer("⛔ Shaxsiy bot", show_alert=True)
    data = load_data()
    data["stats"] = {}
    save_data(data)
    await call.answer("🧹 Tozalandi")
    try:
        await call.message.edit_text("⚙️ <b>Sozlamalar</b>", reply_markup=cfg_kb())
    except Exception:
        await call.message.answer("⚙️ <b>Sozlamalar</b>", reply_markup=cfg_kb())


@dp.callback_query(F.data == "stats")
async def stats_cb(call: CallbackQuery):
    if not is_owner(call.from_user.id):
        return await call.answer("⛔ Shaxsiy bot", show_alert=True)
    stats = load_data().get("stats", {})
    if not stats:
        text = "📊 Hali statistika yo'q. «🚀 Forwardni boshlash» ni bosing."
    else:
        text = (
            "📊 <b>Oxirgi ish natijasi</b>\n\n"
            f"🕒 Vaqt: {fmt_date(stats.get('last_run'))}\n"
            f"📥 Manba: {_esc(stats.get('source_title') or '—')}\n"
            f"📤 Manzil: {_esc(stats.get('dest_title') or '—')}\n"
            f"🔎 Ko'rilgan xabar: {stats.get('scanned', 0)}\n"
            f"🎬 Topilgan video: {stats.get('found', 0)}\n"
            f"✅ Forward: {stats.get('forwarded', 0)}\n"
            f"📋 Nusxa: {stats.get('copied', 0)}\n"
            f"♻️ Allaqachon bor: {stats.get('skipped_existing', 0)}\n"
            f"🆕 Mavzular: {stats.get('topics_created', 0)}\n"
            f"⚠️ Xatolar: {stats.get('failed', 0)}\n"
            f"⏱ Davomiylik: {stats.get('elapsed', 0)} soniya"
        )
    try:
        await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="panel")],
        ]))
    except Exception:
        await call.message.answer(text)
    await call.answer()


@dp.message()
async def fallback_handler(message: Message):
    if not is_owner(message.from_user.id):
        return
    await message.answer("📋 Menyudan foydalaning.", reply_markup=panel_kb())


# ================== RENDER / ISHGA TUSHISH ==================
async def health_check(request: web.Request) -> web.Response:
    return web.Response(text="auto-forward bot is running", status=200)


async def start_web_server():
    port = int(os.getenv("PORT", "10000"))
    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    logging.info("Health server 0.0.0.0:%s portida ishga tushdi", port)
    return runner


async def main():
    runner = await start_web_server()
    auto_task = spawn(auto_loop())
    spawn(userbot_client())
    try:
        await dp.start_polling(bot)
    finally:
        auto_task.cancel()
        try:
            await auto_task
        except asyncio.CancelledError:
            pass
        await close_client()
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
