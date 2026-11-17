import asyncio
import contextvars
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
# OWNER_ID ixtiyoriy: faqat yangi foydalanuvchi haqida xabar olish uchun ishlatiladi.

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


# ================== MA'LUMOTLAR ==================
def default_user():
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
            "clean_captions": True,
            "auto": False,
            "auto_interval_min": 30,
            "send_delay_sec": 10,
        },
        "stats": {},
        "copied": {},
        "created_at": 0,
        "last_seen": 0,
    }


def default_data():
    return {"users": {}}


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _norm_user(rec) -> dict:
    """Yozuvni to'liq shaklga keltiradi (eski/yetishmagan maydonlar to'ldiriladi)."""
    base = default_user()
    if not isinstance(rec, dict):
        return base
    for key in ("userbot", "api", "settings"):
        current = rec.get(key)
        if isinstance(current, dict):
            base[key].update(current)
    for key in ("stats", "copied"):
        current = rec.get(key)
        if isinstance(current, dict):
            base[key] = current
    base["created_at"] = _safe_int(rec.get("created_at"), 0)
    base["last_seen"] = _safe_int(rec.get("last_seen"), 0)
    return base


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
        raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}

    users = raw.get("users")
    if not isinstance(users, dict):
        users = {}
    changed = False
    legacy_keys = ("userbot", "api", "settings", "stats", "copied")
    if any(key in raw for key in legacy_keys):
        # Eski bir foydalanuvchili format: hammasi OWNER_ID hisobiga ko'chadi.
        legacy_key = str(OWNER_ID or 0)
        target = users.get(legacy_key)
        if not isinstance(target, dict):
            target = {}
        for key in legacy_keys:
            if key in raw and key not in target:
                target[key] = raw[key]
        users[legacy_key] = target
        changed = True

    data = {"users": {}}
    for key, rec in users.items():
        text = str(key).strip()
        if not text.lstrip("-").isdigit():
            changed = True
            continue
        data["users"][str(int(text))] = _norm_user(rec)

    _DATA_CACHE = data
    try:
        _DATA_CACHE_MTIME = DATA_FILE.stat().st_mtime_ns
    except OSError:
        _DATA_CACHE_MTIME = None
    if changed:
        save_data(data)
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
    return data


def now_ts() -> int:
    return int(time.time())


def user_key(user_id) -> str:
    return str(_safe_int(user_id, 0))


def get_user(user_id, create: bool = True) -> dict:
    """Foydalanuvchi yozuvi (cache ichida). O'zgartirgach save_data() chaqiring."""
    data = load_data()
    users = data.setdefault("users", {})
    key = user_key(user_id)
    rec = users.get(key)
    if not isinstance(rec, dict):
        rec = _norm_user({})
        if not create:
            return rec
        rec["created_at"] = now_ts()
        users[key] = rec
        save_data(data)
    return rec


def all_users() -> dict:
    return load_data().get("users", {})


def user_settings(user_id) -> dict:
    return get_user(user_id)["settings"]


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
    return bool(OWNER_ID) and _safe_int(user_id, 0) == OWNER_ID


def get_api(user_id):
    api = get_user(user_id).get("api") or {}
    api_id = api.get("api_id") or os.getenv("API_ID", "")
    api_hash = api.get("api_hash") or os.getenv("API_HASH", "")
    try:
        api_id = int(str(api_id).strip())
    except (TypeError, ValueError):
        api_id = 0
    return api_id, str(api_hash or "").strip()


def get_session(user_id) -> str:
    userbot = get_user(user_id, create=False).get("userbot") or {}
    return str(userbot.get("session") or "").strip()


# ================== USERBOT (HAR BIR FOYDALANUVCHI UCHUN) ==================
_clients = {}
_login_clients = {}
_client_locks = {}
_run_locks = {}
_stop_events = {}
_background_tasks = set()
# Har bir forward o'z taskida ishlaydi; stop signali shu context orqali uzatiladi.
_stop_ctx = contextvars.ContextVar("forward_stop", default=None)


class ForwardStopped(Exception):
    pass


def run_lock(user_id) -> asyncio.Lock:
    return _run_locks.setdefault(user_key(user_id), asyncio.Lock())


def stop_event(user_id) -> asyncio.Event:
    return _stop_events.setdefault(user_key(user_id), asyncio.Event())


def is_stopped() -> bool:
    event = _stop_ctx.get()
    return bool(event is not None and event.is_set())


def request_stop(user_id) -> bool:
    """Foydalanuvchining forwardi ketayotgan bo'lsa to'xtatishni so'raydi."""
    running = run_lock(user_id).locked()
    stop_event(user_id).set()
    return running


async def _sleep_or_stop(seconds: float) -> bool:
    """Kutish davomida stop so'ralsa True qaytaradi."""
    delay = max(0.1, float(seconds))
    event = _stop_ctx.get()
    if event is None:
        await asyncio.sleep(delay)
        return False
    try:
        await asyncio.wait_for(event.wait(), timeout=delay)
        return True
    except asyncio.TimeoutError:
        return False


def spawn(coro):
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


async def close_client(user_id):
    client = _clients.pop(user_key(user_id), None)
    if client is not None:
        try:
            await client.disconnect()
        except Exception:
            pass


async def close_all_clients():
    for key in list(_clients.keys()):
        client = _clients.pop(key, None)
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass


async def userbot_client(user_id):
    """Foydalanuvchining saqlangan sessioni orqali ulanadi; bo'lmasa None."""
    key = user_key(user_id)
    client = _clients.get(key)
    if client is not None and client.is_connected():
        try:
            if await client.is_user_authorized():
                return client
        except Exception:
            pass
    lock = _client_locks.setdefault(key, asyncio.Lock())
    async with lock:
        client = _clients.get(key)
        if client is not None and client.is_connected():
            try:
                if await client.is_user_authorized():
                    return client
            except Exception:
                pass
        session = get_session(user_id)
        api_id, api_hash = get_api(user_id)
        if not session or not api_id or not api_hash:
            return None
        await close_client(user_id)
        client = TelegramClient(
            StringSession(session), api_id, api_hash,
            connection_retries=5, retry_delay=2, auto_reconnect=True,
        )
        try:
            await client.connect()
            if not await client.is_user_authorized():
                try:
                    await client.disconnect()
                except Exception:
                    pass
                return None
        except Exception:
            logging.exception("Userbotga ulanishda xatolik (uid=%s)", key)
            try:
                await client.disconnect()
            except Exception:
                pass
            return None
        _clients[key] = client
        return client


async def login_abort(user_id):
    client = _login_clients.pop(user_key(user_id), None)
    if client is not None:
        try:
            await client.disconnect()
        except Exception:
            pass


async def login_start(user_id, phone: str):
    """Kod so'rovini yuboradi va phone_code_hash qaytaradi."""
    api_id, api_hash = get_api(user_id)
    if not api_id or not api_hash:
        raise RuntimeError("API_ID va API_HASH kiritilmagan.")
    await login_abort(user_id)
    client = TelegramClient(StringSession(), api_id, api_hash)
    _login_clients[user_key(user_id)] = client
    try:
        await client.connect()
        sent = await client.send_code_request(phone)
    except Exception:
        await login_abort(user_id)
        raise
    return sent.phone_code_hash


async def login_code(user_id, phone: str, code: str, phone_code_hash: str):
    """Kodni tasdiqlaydi. 'ok' yoki 'password' qaytaradi."""
    client = _login_clients.get(user_key(user_id))
    if client is None:
        raise RuntimeError("Kirish sessiyasi yo'qoldi. «🔐 Kirish» ni qaytadan bosing.")
    try:
        await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
    except errors.SessionPasswordNeededError:
        return "password"
    return "ok"


async def login_password(user_id, password: str):
    client = _login_clients.get(user_key(user_id))
    if client is None:
        raise RuntimeError("Kirish sessiyasi yo'qoldi. «🔐 Kirish» ni qaytadan bosing.")
    await client.sign_in(password=password)
    return "ok"


async def login_finish(user_id, phone: str):
    """Muvaffaqiyatli kirishdan keyin sessionni foydalanuvchi yozuviga saqlaydi."""
    key = user_key(user_id)
    client = _login_clients.pop(key, None)
    if client is None:
        raise RuntimeError("Kirish sessiyasi yo'qoldi. «🔐 Kirish» ni qaytadan bosing.")
    await client.connect()
    me = await client.get_me()
    session_string = client.session.save()
    data = load_data()
    rec = get_user(user_id)
    rec["userbot"] = {"session": session_string, "phone": phone}
    save_data(data)
    old = _clients.pop(key, None)
    if old is not None and old is not client:
        try:
            await old.disconnect()
        except Exception:
            pass
    _clients[key] = client
    return me


async def logout_userbot(user_id):
    key = user_key(user_id)
    client = _clients.get(key)
    if client is None:
        client = await userbot_client(user_id)
    if client is not None:
        try:
            await client.log_out()
        except Exception:
            logging.exception("Logout xatosi")
        try:
            await client.disconnect()
        except Exception:
            pass
    _clients.pop(key, None)
    data = load_data()
    rec = get_user(user_id)
    rec["userbot"] = {"session": "", "phone": ""}
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


_TOPIC_ICON_EMOJI = "📁"
_topic_icon_state = {"id": 0, "checked": 0.0}


def topic_title_text(title: str) -> str:
    """Mavzu nomi: «Qashqirlar Makoni p mp4 hls 2591» -> «Qashqirlar Makoni»."""
    clean, _, _ = parse_media_title(str(title or ""))
    clean = clean or str(title or "").strip()
    return re.sub(r"\s+", " ", clean).strip()[:120] or "Mavzu"


def _topic_title_key(title: str) -> str:
    """Solishtirish kaliti: emoji, fayl so'zlari va qism raqamlari e'tiborsiz."""
    return title_key(topic_title_text(title))


async def topic_icon_id(client):
    """Telegramning mavzu ikonkalari to'plamidan 📁 ni topadi; topilmasa 0."""
    if _topic_icon_state["id"]:
        return _topic_icon_state["id"]
    now = time.time()
    if _topic_icon_state["checked"] and now - _topic_icon_state["checked"] < 300:
        return 0
    _topic_icon_state["checked"] = now
    try:
        res = await client(functions.messages.GetStickerSetRequest(
            stickerset=types.InputStickerSetEmojiDefaultTopicIcons(), hash=0,
        ))
        for doc in (getattr(res, "documents", None) or []):
            for attr in (getattr(doc, "attributes", None) or []):
                if isinstance(attr, types.DocumentAttributeCustomEmoji):
                    if (getattr(attr, "alt", "") or "").strip() == _TOPIC_ICON_EMOJI:
                        _topic_icon_state["id"] = int(doc.id)
                        return int(doc.id)
        logging.info("📁 mavzu ikonkasi topilmadi — nom boshiga emoji qo'shiladi")
    except Exception:
        logging.warning("Mavzu ikonkalari to'plamini olib bo'lmadi", exc_info=True)
    return 0


def topic_display_title(title: str, icon_id: int = 0) -> str:
    text = topic_title_text(title)
    if not icon_id:
        text = f"{_TOPIC_ICON_EMOJI} {text}"
    return text[:128]


def _plain_topic_title(title: str) -> str:
    return re.sub(r"^[^\w]+", "", str(title or ""), flags=re.UNICODE).strip().casefold()


async def find_topic(client, dest, title: str):
    """(topic_id, mavjud nomi) yoki None.

    Bir xil kino uchun eski nomli mavzular ko'p bo'lsa, avval toza nomli,
    keyin eng eski mavzu tanlanadi — har safar boshqa mavzuga sakramaydi.
    """
    needle = _topic_title_key(title)
    plain = _plain_topic_title(topic_title_text(title))
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
        matches = [
            topic for topic in (getattr(res, "topics", None) or [])
            if isinstance(topic, types.ForumTopic) and _topic_title_key(topic.title) == needle
        ]
        if matches:
            matches.sort(key=lambda t: (0 if _plain_topic_title(t.title) == plain else 1, int(t.id)))
            topic = matches[0]
            return topic.id, str(topic.title or ""), int(getattr(topic, "icon_emoji_id", 0) or 0)
    return None


async def create_topic(client, dest, title: str, icon_id: int = 0):
    for icon in ([icon_id] if icon_id else []) + [0]:
        payload = {"peer": dest, "title": topic_display_title(title, icon),
                   "random_id": random.getrandbits(63)}
        if icon:
            payload["icon_emoji_id"] = icon
        try:
            res = await client(functions.messages.CreateForumTopicRequest(**payload))
        except errors.FloodWaitError:
            raise
        except Exception as exc:
            if icon:
                logging.warning("Mavzu ikonkasi bilan yaratilmadi, oddiy urinamiz: %s", exc)
                continue
            raise
        for update in (getattr(res, "updates", None) or []):
            message = getattr(update, "message", None)
            if message is None:
                continue
            if isinstance(getattr(message, "action", None), types.MessageActionTopicCreate):
                return message.id
        found = await find_topic(client, dest, title)
        return found[0] if found else None
    return None


async def edit_topic(client, dest, topic_id, title: str, icon_id: int = 0):
    """Eski nomli mavzuni (masalan «... mp4 hls 2591») toza nomga o'zgartiradi."""
    payload = {"peer": dest, "topic_id": topic_id,
               "title": topic_display_title(title, icon_id)}
    if icon_id:
        payload["icon_emoji_id"] = icon_id
    try:
        await client(functions.messages.EditForumTopicRequest(**payload))
    except Exception:
        logging.warning("Mavzu nomini yangilab bo'lmadi (topic_id=%s)", topic_id, exc_info=True)


async def ensure_topic(client, dest, title: str):
    icon_id = await topic_icon_id(client)
    display = topic_display_title(title, icon_id)
    for _attempt in range(4):
        if is_stopped():
            raise ForwardStopped()
        found = await find_topic(client, dest, title)
        if found:
            topic_id, current, current_icon = found
            need_edit = (current or "").strip() != display
            if icon_id and int(current_icon or 0) != int(icon_id):
                need_edit = True
            if need_edit:
                await edit_topic(client, dest, topic_id, title, icon_id)
            return topic_id, False
        try:
            topic_id = await create_topic(client, dest, title, icon_id)
        except errors.FloodWaitError as exc:
            logging.warning("Mavzu yaratishda limit: %s soniya", exc.seconds)
            if await _sleep_or_stop(int(exc.seconds) + 1):
                raise ForwardStopped()
            continue
        if topic_id:
            return topic_id, True
        found = await find_topic(client, dest, title)
        if found:
            return found[0], True
    raise RuntimeError(f"«{title}» mavzusini yaratib bo'lmadi")


# ================== NOMLARNI ANIQLASH ==================
_URL_RE = re.compile(r"(?:https?://\S+|t\.me/\S+|@[A-Za-z0-9_]{4,})", re.I)
_EXT_RE = re.compile(r"\.(?:mp4|mkv|avi|mov|wmv|flv|webm|m4v|ts|m2ts|rar|zip)\b", re.I)
# «Video: 1080p | Audio: uzb 👁 | Subtitr: ×» kabi maydonlar nomga qo'shilmaydi.
_FIELD_RE = re.compile(
    r"\b(?:video|audio|subtitr(?:i)?|sifat(?:i)?|tili?|janr(?:i)?|yil(?:i)?|"
    r"davlat(?:i)?|davomiylik|format|kodek)\s*[:：]\s*[^/\n|]*",
    re.I,
)
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
_EP_LABEL_RE = re.compile(
    r"\b(?:qism|epizod|episode|seriya|seria|part)\s*[:\-–—]?\s*(\d{1,3})\b", re.I
)
_SEASON_LABEL_RE = re.compile(
    r"\b(?:fasl|sezon|season|mavsum)\s*[:\-–—]?\s*(\d{1,2})\b", re.I
)
_LABEL_RE = re.compile(r"\b(?:kino|film|serial|multfilm|multik)\s*nomi\s*[:\-–—]?\s*", re.I)
_QUALITY_RE = re.compile(
    r"\b(?:\d{3,4}[pi]|4k|8k|uhd|fhd|full\s*hd|hdrip|web[- ]?dl|webrip|hdtv|bluray|brrip|dvdrip|camrip)\b",
    re.I,
)
_LANG_RE = re.compile(
    r"\b(?:uzbekcha|o['’ʻ]zbekcha|ozbekcha|uzbek|o['’ʻ]zbek|ozbek|узбек|"
    r"ruscha|russian|rus|hindcha|hindi|hind|turkcha|turkish|turk|turkiy|"
    r"tarjima|tarjimon|dublyaj|dublaj|subtitr(?:i)?|"
    r"tilidagi|tilida|tili|til|"
    r"sifatli|sifati|sifat|yuklab|olish|premyera|premerasi)\b",
    re.I,
)
_RES_RE = re.compile(r"(?<!\d)(?:360|480|720|1080|1440|2160)(?!\d)")
_HD_RE = re.compile(r"\b(?:hd|fhd|uhd|sd)\b", re.I)
_FILE_RE = re.compile(
    r"\b(?:mp4|mkv|avi|mov|wmv|flv|webm|m4v|m2ts|mts|ts|m3u8|hls|"
    r"h26[45]|x26[45]|hevc|avc|aac|ac3|eac3|dts|ddp|hdrip|bdrip|brrip)\b",
    re.I,
)
_PART_WORD_RE = re.compile(r"\b(?:oxirgi|yakuniy|final|so['’ʻ]nggi)\s+qism\b", re.I)


def clean_name_text(text: str) -> str:
    """Nom atrofidagi bezak, tinish belgilari va emojilarni olib tashlaydi."""
    text = re.sub(r"^[^\w]+", "", str(text or ""), flags=re.UNICODE)
    text = re.sub(r"[^\w\s]+$", "", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def parse_media_title(raw: str):
    """Sarlavhadan kino/serial nomi, fasl va qism raqamini ajratadi.

    «Merlin 1 qism til uzbek sifati 1080p» -> ("Merlin", None, 1)
    """
    text = unicodedata.normalize("NFKC", str(raw or ""))
    text = _URL_RE.sub(" ", text)
    text = _EXT_RE.sub(" ", text)
    text = _FIELD_RE.sub(" ", text)
    text = _LABEL_RE.sub(" ", text)
    text = text.replace("_", " ").replace("|", " ")

    season = episode = None
    match = _SE_RE.search(text)
    if match:
        season, episode = int(match.group(1)), int(match.group(2))
        text = _SE_RE.sub(" ", text)
    else:
        match = _EP_LABEL_RE.search(text)
        if match:
            episode = int(match.group(1))
            text = _EP_LABEL_RE.sub(" ", text)
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
        match = _SEASON_LABEL_RE.search(text)
        if match:
            season = int(match.group(1))
            text = _SEASON_LABEL_RE.sub(" ", text)
        else:
            match = _SEASON_MARKER_RE.search(text)
            if match:
                season = int(match.group(1))
                text = _SEASON_MARKER_RE.sub(" ", text)

    text = _QUALITY_RE.sub(" ", text)
    text = _RES_RE.sub(" ", text)
    text = _HD_RE.sub(" ", text)
    text = _FILE_RE.sub(" ", text)
    text = _PART_WORD_RE.sub(" ", text)
    text = _LANG_RE.sub(" ", text)
    text = re.sub(r"#\w+", " ", text)
    text = re.sub(r"(?<!\d)(?:19|20)\d{2}(?!\d)", " ", text)
    text = re.sub(r"[\[\](){}<>«»,;:•·/\\]+", " ", text)
    text = re.sub(r"\s*[-–—.]+\s*", " ", text)
    text = re.sub(r"(?<!\w)p(?!\w)", " ", text)
    # Qism/bo'lim hisoblagichlari: «... Makoni 2591», «Forsaj 2» -> nomdan tashlanadi.
    previous = None
    while previous != text:
        previous = text
        text = re.sub(r"(?<=\s)\d{1,4}\s*$", "", text)
    text = clean_name_text(text)
    if text and text.isupper() and text.isascii():
        text = text.title()
    if text and text[0].islower():
        text = text[0].upper() + text[1:]
    if not text or re.fullmatch(r"[\d\s.+-]+", text):
        return "", season, episode
    return text[:100], season, episode


def title_key(title: str) -> str:
    text = unicodedata.normalize("NFKC", str(title or "")).casefold()
    text = re.sub(r"[^\w\s]+", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


_CAPTION_LINK_RE = re.compile(
    r"(?:https?://\S+|(?:www\.)?t(?:elegram)?\.me/\S+|@[A-Za-z0-9_]{4,})", re.I
)
_AD_LINE_RE = re.compile(
    r"(?:reklama|obuna|subscribe|kanalga|kanalimiz|hamkorlik|murojaat|\bjoin\b|"
    r"yuklab\s*olish|yuklab\s*ol\b|skachat|download)",
    re.I,
)


def _clean_caption_text(text: str) -> str:
    text = _CAPTION_LINK_RE.sub(" ", str(text or ""))
    lines = []
    for line in text.splitlines():
        if _AD_LINE_RE.search(line):
            continue
        line = re.sub(r"[ \t]+", " ", line).strip(" -–—|•·")
        if line:
            lines.append(line)
    return "\n".join(lines).strip()


def clean_caption(caption: str, fallback: str = "") -> str:
    """Tavsifdan reklama havolalari, @username va reklama qatorlarini olib tashlaydi."""
    text = _clean_caption_text(caption)
    if not text:
        text = _clean_caption_text(fallback)
    return text[:1024]


def looks_random_name(text: str) -> bool:
    """«JUSTaFiLesxmtujyec» kabi avtomatik yaratilgan fayl nomlarini aniqlaydi."""
    t = str(text or "").strip()
    if not t or " " in t:
        return False
    if len(re.sub(r"[^\w]", "", t)) < 14:
        return False
    if len(re.findall(r"[a-z][A-Z]", t)) >= 2:
        return True
    return bool(re.search(r"\d", t))


def fallback_name(caption: str, filename: str = "") -> str:
    candidates = _clean_caption_text(caption).splitlines()
    candidates.append(str(filename or ""))
    for line in candidates:
        parsed, _, _ = parse_media_title(line)
        if looks_random_name(parsed):
            continue
        if len(re.findall(r"[^\W\d_]", parsed, flags=re.UNICODE)) >= 2:
            return parsed[:60]
    return ""


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
def message_topic_id(message):
    """Forum xabarining mavzu ID si (General mavzuda None)."""
    header = getattr(message, "reply_to", None)
    if header is None:
        return None
    top = getattr(header, "reply_to_top_id", None)
    if top:
        return int(top)
    if getattr(header, "forum_topic", False):
        return int(getattr(header, "reply_to_msg_id", 0) or 0) or None
    return None


async def fetch_source_topics(client, peer):
    """Manba forumning mavzu nomlari: {topic_id: title}."""
    titles = {}
    offset_topic = offset_id = 0
    offset_date = None
    for _page in range(30):
        if is_stopped():
            raise ForwardStopped()
        res = await client(functions.messages.GetForumTopicsRequest(
            peer=peer, offset_date=offset_date, offset_id=offset_id,
            offset_topic=offset_topic, limit=100, q=None,
        ))
        topics = getattr(res, "topics", None) or []
        added = 0
        for topic in topics:
            if not isinstance(topic, types.ForumTopic):
                continue
            if topic.id in titles:
                continue
            titles[topic.id] = str(topic.title or "").strip()
            added += 1
        if added == 0 or len(topics) < 100:
            break
        last = topics[-1]
        offset_topic = int(last.id)
        offset_id = int(getattr(last, "top_message", 0) or 0)
        offset_date = getattr(last, "date", None)
    titles.pop(1, None)  # General mavzu — nomi bo'yicha emas, umumiy mavzuga
    return titles


async def scan_source(client, source, min_size: int, on_progress=None, topic_titles=None):
    items = []
    seen_docs = set()
    scanned = 0
    async for message in client.iter_messages(source, reverse=True, wait_time=0.4):
        scanned += 1
        if is_stopped():
            raise ForwardStopped()
        doc = video_document(message)
        if doc is None or int(doc.size or 0) <= min_size:
            continue
        if doc.id in seen_docs:
            continue
        seen_docs.add(doc.id)
        caption = (message.message or "").strip()
        lines = [line.strip() for line in caption.splitlines() if line.strip()]
        first_line = lines[0] if lines else ""
        header = " ".join(lines[:3])
        name, _, _ = parse_media_title(first_line or document_filename(doc))
        _, season, episode = parse_media_title(header or first_line or document_filename(doc))
        if topic_titles is not None:
            # Manba mavzuli: nom caption'dan emas, manbadagi mavzu nomidan olinadi.
            name = str(topic_titles.get(message_topic_id(message)) or "").strip()[:120]
        else:
            if not name:
                name = fallback_name(caption, document_filename(doc))
            if looks_random_name(name):
                name = ""
        items.append({
            "msg_id": message.id,
            "grouped_id": message.grouped_id,
            "doc_id": doc.id,
            "document": doc,
            "caption": caption[:1024],
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
        if is_stopped():
            raise ForwardStopped()
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


async def send_batch(client, source, dest, topic_id, batch, clean: bool):
    """'copy' — akkaunt o'zi yuborgandek nusxa, 'forward' — muallif yashirilgan forward.

    Nusxa rejimida tavsif tozalanadi va «Forwarded from» yozuvi bo'lmaydi.
    """
    captions = [clean_caption(r["caption"], r["name"]) if clean else (r["caption"] or "") for r in batch]
    files = [record["document"] for record in batch]

    for _attempt in range(3):
        try:
            if len(files) == 1:
                await client.send_file(dest, files[0], caption=captions[0], reply_to=topic_id)
            else:
                await client.send_file(dest, files, caption=captions, reply_to=topic_id)
            return "copy"
        except errors.FloodWaitError as exc:
            if await _sleep_or_stop(min(int(exc.seconds) + 1, 900)):
                return "stopped"
        except errors.ChatForwardsRestrictedError:
            break  # media himoyalangan: nusxa ham, forward ham mumkin emas
        except Exception as exc:
            logging.warning("Nusxa yuborishda xatolik: %s", exc)
            break

    # Nusxa ishlamasa, muallifni yashirib forward qilamiz (tavsif o'zgarmaydi).
    ids = [record["msg_id"] for record in batch]
    for _attempt in range(3):
        try:
            await client(functions.messages.ForwardMessagesRequest(
                from_peer=source, id=ids, to_peer=dest, top_msg_id=topic_id,
                drop_author=True,
                random_id=[random.getrandbits(63) for _ in ids],
            ))
            return "forward"
        except errors.FloodWaitError as exc:
            if await _sleep_or_stop(min(int(exc.seconds) + 1, 900)):
                return "stopped"
        except errors.ChatForwardsRestrictedError:
            break
        except Exception as exc:
            logging.warning("Forward xatosi (ids=%s): %s", ids, exc)
            break
    return ""


def remember_copies(user_id, records):
    """Nusxa (copy) yuborilganda fayl ID o'zgaradi — takrorlmasligi uchun manba ID sini saqlaymiz."""
    data = load_data()
    rec = get_user(user_id)
    copied = rec.setdefault("copied", {})
    stamp = now_ts()
    for record in records:
        copied[str(record["doc_id"])] = stamp
    if len(copied) > 50000:
        newest = sorted(copied.items(), key=lambda kv: kv[1], reverse=True)[:40000]
        rec["copied"] = dict(newest)
    save_data(data)


# ================== FORWARD JARAYONI ==================
async def _notify(chat_id, text):
    if not chat_id:
        return
    try:
        await bot.send_message(chat_id, text)
    except Exception:
        logging.exception("Xabar yuborilmadi")


async def run_forward(user_id, notify_chat_id=None):
    lock = run_lock(user_id)
    if lock.locked():
        await _notify(notify_chat_id, "⏳ Forward allaqachon ketmoqda. Tugashini kuting.")
        return None
    async with lock:
        event = stop_event(user_id)
        event.clear()
        # Stop signali context orqali chuqur yordamchi funksiyalarga uzatiladi.
        _stop_ctx.set(event)
        settings = user_settings(user_id)
        source_ref = str(settings.get("source") or "").strip()
        dest_ref = str(settings.get("dest") or "").strip()
        if not source_ref or not dest_ref:
            await _notify(notify_chat_id, "❌ Avval manba va manzil kanallarni tanlang.")
            return None

        client = await userbot_client(user_id)
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
        clean_captions = bool(settings.get("clean_captions", True))
        send_delay = max(0, int(settings.get("send_delay_sec", 10) or 0))

        items = []
        scanned_count = 0
        existing = set()
        skipped_existing = 0
        forwarded = copied = failed = created_topics = 0
        stopped = False

        try:
            await set_status("🔎 Manzil kanaldagi mavjud fayllar tekshirilmoqda…")
            try:
                existing = await existing_doc_ids(client, dest)
            except ForwardStopped:
                raise
            except Exception as exc:
                await set_status(f"❌ Manzil kanalni o'qib bo'lmadi: <code>{_esc(str(exc))}</code>")
                return None
            for doc_id in (get_user(user_id).get("copied") or {}):
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

            # Manba mavzuli (forum) bo'lsa, manzilda ham o'sha mavzu nomlari ochiladi.
            topic_titles = None
            if bool(getattr(source, "forum", False)):
                await set_status("🗂 Manba mavzulari olinmoqda…")
                try:
                    topic_titles = await fetch_source_topics(client, source)
                except ForwardStopped:
                    raise
                except Exception:
                    logging.warning("Manba mavzularini olib bo'lmadi", exc_info=True)
                    topic_titles = {}
                if not topic_titles:
                    topic_titles = None

            await set_status("📤 Manba kanal skanerlanmoqda…")
            try:
                items, scanned_count = await scan_source(
                    client, source, min_size, scan_progress, topic_titles=topic_titles,
                )
            except ForwardStopped:
                raise
            except Exception as exc:
                await set_status(f"❌ Manba kanalni o'qib bo'lmadi: <code>{_esc(str(exc))}</code>")
                return None

            skipped_existing = sum(1 for item in items if item["doc_id"] in existing)
            pending = [item for item in items if item["doc_id"] not in existing]

            # Har bir kino/serial nomi — alohida mavzu. Faqat nomi aniqlanmaganlar «Kinolar» ga tushadi.
            groups = defaultdict(list)
            group_titles = defaultdict(Counter)
            for item in pending:
                title = item["name"] or movies_topic
                key = title_key(title)
                groups[key].append(item)
                group_titles[key][title] += 1

            for records in groups.values():
                records.sort(key=lambda r: (
                    r["season"] or 0,
                    r["episode"] if r["episode"] is not None else 10 ** 6,
                    r["date"],
                    r["msg_id"],
                ))

            group_list = list(groups.items())
            for gi, (key, records) in enumerate(group_list):
                if is_stopped():
                    stopped = True
                    break
                topic_title = group_titles[key].most_common(1)[0][0] or movies_topic
                try:
                    topic_id, was_created = await ensure_topic(client, dest, topic_title)
                except ForwardStopped:
                    raise
                except Exception as exc:
                    logging.exception("Mavzu xatosi: %s", exc)
                    failed += len(records)
                    continue
                if was_created:
                    created_topics += 1

                batches = chunk_albums(records)
                for bi, batch in enumerate(batches):
                    if is_stopped():
                        stopped = True
                        break
                    result = await send_batch(client, source, dest, topic_id, batch, clean_captions)
                    if result == "stopped":
                        stopped = True
                        break
                    if result == "copy":
                        copied += len(batch)
                        existing.update(record["doc_id"] for record in batch)
                        remember_copies(user_id, batch)
                    elif result == "forward":
                        forwarded += len(batch)
                        existing.update(record["doc_id"] for record in batch)
                    else:
                        failed += len(batch)
                    done = forwarded + copied
                    if done and done % 25 < len(batch):
                        await set_status(
                            f"📤 Yuborilmoqda…\n✅ Yuborildi: <b>{done}</b> ta\n"
                            f"🏷 Mavzu: {_esc(topic_title)}"
                        )
                    # Akkaunt spam blokiga tushmasligi uchun har media orasida tanaffus.
                    has_more = bi < len(batches) - 1 or gi < len(group_list) - 1
                    if has_more and send_delay > 0:
                        await set_status(
                            f"⏳ Spam bo'lmasligi uchun {send_delay} soniya kutilyapti…\n"
                            f"✅ Yuborildi: <b>{done}</b> ta\n"
                            f"🏷 Mavzu: {_esc(topic_title)}"
                        )
                        if await _sleep_or_stop(send_delay):
                            stopped = True
                            break
                if stopped:
                    break
        except ForwardStopped:
            stopped = True

        elapsed = int(time.time() - started)
        total_sent = forwarded + copied
        stats = {
            "last_run": now_ts(),
            "scanned": scanned_count,
            "found": len(items),
            "forwarded": forwarded,
            "copied": copied,
            "skipped_existing": skipped_existing,
            "failed": failed,
            "topics_created": created_topics,
            "stopped": stopped,
            "elapsed": elapsed,
            "source_title": channel_title(source),
            "dest_title": channel_title(dest),
        }
        data = load_data()
        rec = get_user(user_id)
        rec["stats"] = stats
        save_data(data)

        summary = (
            f"{'⏹ <b>Forward to‘xtatildi</b>' if stopped else '🏁 <b>Forward tugadi</b>'}\n"
            f"📥 Ko'rilgan xabar: {scanned_count}\n"
            f"🎬 {min_mb} MB dan katta video: {len(items)}\n"
            f"✅ Yuborildi: <b>{total_sent}</b>\n"
            f"♻️ Allaqachon bor (o'tkazildi): {skipped_existing}\n"
            f"🆕 Yangi mavzular: {created_topics}\n"
            f"⚠️ Xatolar: {failed}\n"
            f"⏱ Vaqt: {elapsed} soniya"
        )
        if stopped:
            summary += "\n\n▶️ Qayta boshlaganda yuborilganlar o'tkazib yuboriladi."
        if notify_chat_id:
            await set_status(summary)
        elif total_sent or failed or stopped:
            await _notify(user_id, "🔄 <b>Avto-forward</b>\n" + summary)
        return stats


async def auto_loop():
    await asyncio.sleep(10)
    while True:
        try:
            # Har bir foydalanuvchi jadvali alohida tekshiriladi; ketma-ket ishlaydi.
            for key, rec in list(all_users().items()):
                try:
                    uid = int(key)
                except (TypeError, ValueError):
                    continue
                settings = rec.get("settings") or {}
                if not settings.get("auto"):
                    continue
                if not (rec.get("userbot") or {}).get("session"):
                    continue
                interval = max(5, int(settings.get("auto_interval_min", 30) or 30)) * 60
                last_run = int((rec.get("stats") or {}).get("last_run", 0) or 0)
                if now_ts() - last_run < interval:
                    continue
                if run_lock(uid).locked():
                    continue
                try:
                    await run_forward(uid, notify_chat_id=None)
                except Exception:
                    logging.exception("Avto-forward xatosi (uid=%s)", uid)
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
    interval = State()
    delay = State()


def panel_text(user_id):
    rec = get_user(user_id)
    settings = rec["settings"]
    stats = rec.get("stats", {})
    api_id, api_hash = get_api(user_id)
    linked = "✅ ulangan" if get_session(user_id) else "❌ ulanmagan"
    auto = "🟢 yoqilgan" if settings.get("auto") else "🔴 o'chirilgan"
    cleaning = "🟢 yoqilgan" if settings.get("clean_captions", True) else "🔴 o'chirilgan"
    lines = [
        "🤖 <b>Avto-Forward bot</b>",
        "",
        f"👤 Akkaunt: {linked}",
        f"🔑 API: {'✅ bor' if api_id and api_hash else '❌ kiritilmagan'}",
        f"📥 Manba: {_esc(settings.get('source_title') or settings.get('source') or 'tanlanmagan')}",
        f"📤 Manzil: {_esc(settings.get('dest_title') or settings.get('dest') or 'tanlanmagan')}",
        f"📦 Minimal hajm: {settings.get('min_size_mb', 100)} MB",
        f"⏱ Media oralig'i: {settings.get('send_delay_sec', 10)} soniya",
        f"🧽 Reklamani tozalash: {cleaning}",
        f"🔁 Avto: {auto} (har {settings.get('auto_interval_min', 30)} daqiqa)",
    ]
    if stats:
        total = int(stats.get("forwarded", 0) or 0) + int(stats.get("copied", 0) or 0)
        lines += [
            "",
            f"🕒 Oxirgi ishga tushish: {fmt_date(stats.get('last_run'))}",
            f"✅ Oxirgi yuborilgan: {total} ta"
            + (" (⏹ to'xtatilgan)" if stats.get("stopped") else ""),
        ]
    lines += [
        "",
        "Manba mavzuli (forum) bo'lsa — mavzular o'sha nomlari bilan manzilga ko'chiriladi.",
        "Manba mavzusiz bo'lsa — har bir kino nomi bo'yicha alohida mavzu ochiladi; o'sha nomdagi barcha qismlar shu mavzuga tushadi.",
        "Xabarlar akkauntning o'zi yuborgandek (forward yozuvisiz) ketadi.",
    ]
    return "\n".join(lines)


def panel_kb(user_id):
    settings = get_user(user_id)["settings"]
    linked = bool(get_session(user_id))
    auto = bool(settings.get("auto"))
    rows = [
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
        [InlineKeyboardButton(text="⏹ To'xtatish", callback_data="stop_run")],
        [InlineKeyboardButton(
            text=("🟢 Avto: YOQILGAN" if auto else "🔴 Avto: O'CHIRILGAN"),
            callback_data="toggle_auto",
        )],
        [InlineKeyboardButton(text="📊 Statistika", callback_data="stats")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def account_kb(linked: bool):
    rows = []
    if linked:
        rows.append([InlineKeyboardButton(text="🚪 Akkauntdan chiqish", callback_data="logout")])
    else:
        rows.append([InlineKeyboardButton(text="🔐 Kirish (telefon + kod)", callback_data="login")])
    rows.append([InlineKeyboardButton(text="🔑 API_ID / API_HASH", callback_data="set_api")])
    rows.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cfg_kb(user_id):
    settings = get_user(user_id)["settings"]
    cleaning = "🟢 yoqilgan" if settings.get("clean_captions", True) else "🔴 o'chirilgan"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"📦 Minimal hajm: {settings.get('min_size_mb', 100)} MB", callback_data="cfg:min_size")],
        [InlineKeyboardButton(text=f"⏱ Media oralig'i: {settings.get('send_delay_sec', 10)} s", callback_data="cfg:delay")],
        [InlineKeyboardButton(text=f"🎬 Bo'sh nom uchun mavzu: {settings.get('movies_topic', 'Kinolar')}", callback_data="cfg:movies_topic")],
        [InlineKeyboardButton(text=f"🧽 Reklamani tozalash: {cleaning}", callback_data="cfg:clean")],
        [InlineKeyboardButton(text=f"🔁 Avto oraliq: {settings.get('auto_interval_min', 30)} daqiqa", callback_data="cfg:interval")],
        [InlineKeyboardButton(text="🧹 Statistika va holatni tozalash", callback_data="reset_stats")],
        [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="panel")],
    ])


async def show_panel(target, user_id, edit=False):
    text = panel_text(user_id)
    markup = panel_kb(user_id)
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
    uid = message.from_user.id
    data = load_data()
    users = data.setdefault("users", {})
    is_new = user_key(uid) not in users
    rec = get_user(uid)
    rec["last_seen"] = now_ts()
    if is_new:
        rec["created_at"] = now_ts()
    save_data(data)
    if is_new and OWNER_ID and not is_owner(uid):
        who = message.from_user.full_name or "—"
        uname = f"@{message.from_user.username}" if message.from_user.username else "—"
        await _notify(
            OWNER_ID,
            "🆕 <b>Yangi foydalanuvchi</b>\n"
            f"🆔 <code>{uid}</code>\n"
            f"👤 {_esc(who)}\n"
            f"🔗 {_esc(uname)}",
        )
    await message.answer(
        "👋 Salom! Manba kanaldagi kinolarni manzil kanalga mavzular bo'yicha "
        "avtomatik ko'chiruvchi bot.\n\n" + panel_text(uid),
        reply_markup=panel_kb(uid),
    )


@dp.message(Command("panel"))
async def panel_command(message: Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    get_user(uid)["last_seen"] = now_ts()
    await message.answer(panel_text(uid), reply_markup=panel_kb(uid))


@dp.message(Command("cancel"))
async def cancel_handler(message: Message, state: FSMContext):
    uid = message.from_user.id
    await state.clear()
    await login_abort(uid)
    await message.answer("✅ Bekor qilindi.", reply_markup=panel_kb(uid))


@dp.message(Command("stop"))
async def stop_command(message: Message, state: FSMContext):
    uid = message.from_user.id
    if request_stop(uid):
        await message.answer(
            "⏹ To'xtatilmoqda… Joriy fayl yuborilgach to'xtaydi.",
            reply_markup=panel_kb(uid),
        )
    else:
        await message.answer("Hozir forward ketmayapti.", reply_markup=panel_kb(uid))


@dp.callback_query(F.data == "stop_run")
async def stop_run_cb(call: CallbackQuery):
    if request_stop(call.from_user.id):
        await call.answer("⏹ To'xtatilmoqda…", show_alert=True)
    else:
        await call.answer("Hozir forward ketmayapti", show_alert=True)


@dp.callback_query(F.data == "panel")
async def panel_cb(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_panel(call.message, call.from_user.id, edit=True)
    await call.answer()


@dp.callback_query(F.data == "account")
async def account_cb(call: CallbackQuery, state: FSMContext):
    await state.clear()
    uid = call.from_user.id
    rec = get_user(uid)
    phone = (rec.get("userbot") or {}).get("phone") or "—"
    linked = bool(get_session(uid))
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
    api_id, api_hash = get_api(call.from_user.id)
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
    uid = message.from_user.id
    phone = re.sub(r"\D", "", message.text or "")
    if phone.startswith("00"):
        phone = phone[2:]
    if len(phone) < 8 or len(phone) > 15:
        return await message.answer("⚠️ Raqam noto'g'ri. Masalan: <code>+998901234567</code>")
    phone = "+" + phone
    try:
        code_hash = await login_start(uid, phone)
    except errors.FloodWaitError as exc:
        return await message.answer(f"⏳ Telegram limit: {exc.seconds} soniyadan keyin urinib ko'ring.")
    except errors.ApiIdInvalidError:
        await state.clear()
        return await message.answer("❌ API_ID yoki API_HASH noto'g'ri.", reply_markup=panel_kb(uid))
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
    uid = message.from_user.id
    code = re.sub(r"\D", "", message.text or "")
    ctx = await state.get_data()
    phone = ctx.get("phone")
    code_hash = ctx.get("phone_code_hash")
    if not code:
        return await message.answer("⚠️ Kod faqat raqamlardan iborat. Qayta yuboring:")
    if not phone or not code_hash:
        await state.clear()
        await login_abort(uid)
        return await message.answer(
            "❌ Kirish sessiyasi topilmadi. «🔐 Akkauntga kirish» ni qaytadan bosing.",
            reply_markup=panel_kb(uid),
        )
    try:
        result = await login_code(uid, phone, code, code_hash)
    except errors.PhoneCodeInvalidError:
        return await message.answer("❌ Kod xato. Qayta kiriting:")
    except errors.PhoneCodeExpiredError:
        await state.clear()
        await login_abort(uid)
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
        me = await login_finish(uid, phone)
    except Exception as exc:
        logging.exception("Kirishni yakunlashda xatolik")
        await state.clear()
        return await message.answer(
            f"❌ Kirishni yakunlab bo'lmadi: <code>{_esc(str(exc))}</code>",
            reply_markup=panel_kb(uid),
        )
    await state.clear()
    name = getattr(me, "username", None) or getattr(me, "first_name", "") or "akkaunt"
    await message.answer(
        f"✅ Akkaunt ulandi: <b>{_esc(str(name))}</b>",
        reply_markup=panel_kb(uid),
    )


@dp.message(Form.password)
async def password_handler(message: Message, state: FSMContext):
    uid = message.from_user.id
    password = message.text or ""
    if not password:
        return await message.answer("⚠️ Parolni yuboring. Bekor qilish: /cancel")
    ctx = await state.get_data()
    phone = ctx.get("phone", "")
    try:
        await login_password(uid, password)
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
        me = await login_finish(uid, phone)
    except Exception as exc:
        logging.exception("Kirishni yakunlashda xatolik")
        await state.clear()
        return await message.answer(
            f"❌ Kirishni yakunlab bo'lmadi: <code>{_esc(str(exc))}</code>",
            reply_markup=panel_kb(uid),
        )
    await state.clear()
    name = getattr(me, "username", None) or getattr(me, "first_name", "") or "akkaunt"
    await message.answer(
        f"✅ Akkaunt ulandi: <b>{_esc(str(name))}</b>\nEndi kanallarni tanlang.",
        reply_markup=panel_kb(uid),
    )


@dp.callback_query(F.data == "logout")
async def logout_cb(call: CallbackQuery):
    uid = call.from_user.id
    await logout_userbot(uid)
    await call.answer("✅ Akkauntdan chiqildi")
    await show_panel(call.message, uid, edit=True)


@dp.callback_query(F.data == "set_api")
async def set_api_cb(call: CallbackQuery, state: FSMContext):
    api_id, api_hash = get_api(call.from_user.id)
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
    uid = message.from_user.id
    raw = (message.text or "").replace("\n", " ").strip()
    match = re.fullmatch(r"(\d{3,12})\s*[:;,\s]\s*([0-9a-fA-F]{20,40})", raw)
    if not match:
        return await message.answer(
            "⚠️ Format: <code>API_ID:API_HASH</code>. Masalan "
            "<code>1234567:0123456789abcdef0123456789abcdef</code>"
        )
    data = load_data()
    rec = get_user(uid)
    rec["api"] = {"api_id": int(match.group(1)), "api_hash": match.group(2).lower()}
    save_data(data)
    await close_client(uid)
    await state.clear()
    try:
        await message.delete()
    except Exception:
        pass
    await message.answer("✅ API ma'lumotlari saqlandi.", reply_markup=panel_kb(uid))


@dp.callback_query(F.data == "set_source")
async def set_source_cb(call: CallbackQuery, state: FSMContext):
    await state.set_state(Form.source)
    await call.message.answer(
        "📥 <b>Manba kanal</b>\n@username, -100... ID yoki https://t.me/kanal yuboring.\n"
        "Akkaunt bu kanalga a'zo bo'lishi kerak."
    )
    await call.answer()


@dp.message(Form.source)
async def source_handler(message: Message, state: FSMContext):
    uid = message.from_user.id
    raw = (message.text or "").strip()
    parsed = parse_chat_ref(raw)
    if parsed == "":
        return await message.answer("⚠️ @username yoki -100... ID yuboring (taklif havolasi emas).")
    client = await userbot_client(uid)
    if client is None:
        return await message.answer("❌ Avval akkauntga kiring.")
    try:
        entity = await resolve_chat(client, raw)
    except Exception as exc:
        return await message.answer(f"❌ Kanal topilmadi: <code>{_esc(str(exc))}</code>")
    username = getattr(entity, "username", None)
    stored = "@" + username if username else str(entity.id)
    data = load_data()
    rec = get_user(uid)
    rec["settings"]["source"] = stored
    rec["settings"]["source_title"] = channel_title(entity)
    save_data(data)
    await state.clear()
    await message.answer(
        f"✅ Manba saqlandi: <b>{_esc(channel_title(entity))}</b>\n<code>{_esc(stored)}</code>",
        reply_markup=panel_kb(uid),
    )


@dp.callback_query(F.data == "set_dest")
async def set_dest_cb(call: CallbackQuery, state: FSMContext):
    await state.set_state(Form.dest)
    await call.message.answer(
        "📤 <b>Manzil kanal</b>\nMavzular (Topics) yoqilgan kanal bo'lishi kerak.\n"
        "Siz u yerda admin va «Mavzularni boshqarish» huquqiga ega bo'ling."
    )
    await call.answer()


@dp.message(Form.dest)
async def dest_handler(message: Message, state: FSMContext):
    uid = message.from_user.id
    raw = (message.text or "").strip()
    if parse_chat_ref(raw) == "":
        return await message.answer("⚠️ @username yoki -100... ID yuboring.")
    client = await userbot_client(uid)
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
    rec = get_user(uid)
    rec["settings"]["dest"] = stored
    rec["settings"]["dest_title"] = channel_title(entity)
    save_data(data)
    await state.clear()
    await message.answer(
        f"✅ Manzil saqlandi: <b>{_esc(channel_title(entity))}</b>",
        reply_markup=panel_kb(uid),
    )


@dp.callback_query(F.data == "cfg")
async def cfg_cb(call: CallbackQuery, state: FSMContext):
    uid = call.from_user.id
    await state.clear()
    try:
        await call.message.edit_text("⚙️ <b>Sozlamalar</b>", reply_markup=cfg_kb(uid))
    except Exception:
        await call.message.answer("⚙️ <b>Sozlamalar</b>", reply_markup=cfg_kb(uid))
    await call.answer()


@dp.callback_query(F.data.startswith("cfg:"))
async def cfg_prompt(call: CallbackQuery, state: FSMContext):
    uid = call.from_user.id
    key = call.data.split(":", 1)[1]
    if key == "clean":
        data = load_data()
        rec = get_user(uid)
        rec["settings"]["clean_captions"] = not bool(rec["settings"].get("clean_captions", True))
        save_data(data)
        enabled = "yoqildi" if rec["settings"]["clean_captions"] else "o'chirildi"
        await call.answer(f"🧽 Reklamani tozalash {enabled}")
        try:
            await call.message.edit_text("⚙️ <b>Sozlamalar</b>", reply_markup=cfg_kb(uid))
        except Exception:
            await call.message.answer("⚙️ <b>Sozlamalar</b>", reply_markup=cfg_kb(uid))
        return
    prompts = {
        "min_size": ("min_size", Form.min_size, "📦 Minimal video hajmini MB da yuboring (masalan 100):"),
        "delay": ("send_delay_sec", Form.delay, "⏱ Har bir media orasidagi tanaffusni soniyada yuboring (0–120, masalan 10):"),
        "movies_topic": ("movies_topic", Form.movies_topic, "🎬 Nomi aniqlanmagan fayllar uchun mavzu nomini yuboring (masalan Kinolar):"),
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
        await message.answer("✅ Minimal hajm yangilandi.", reply_markup=cfg_kb(message.from_user.id))


@dp.message(Form.delay)
async def delay_handler(message: Message, state: FSMContext):
    if await _save_numeric(message, state, "send_delay_sec", 0, 120):
        await message.answer("✅ Media oralig'i yangilandi.", reply_markup=cfg_kb(message.from_user.id))


@dp.message(Form.interval)
async def interval_handler(message: Message, state: FSMContext):
    if await _save_numeric(message, state, "auto_interval_min", 5, 1440):
        await message.answer("✅ Avto oraliq yangilandi.", reply_markup=cfg_kb(message.from_user.id))


async def _save_numeric(message: Message, state: FSMContext, key: str, low: int, high: int) -> bool:
    uid = message.from_user.id
    raw = (message.text or "").strip()
    if raw == "/cancel":
        await state.clear()
        await message.answer("✅ Bekor qilindi.", reply_markup=panel_kb(uid))
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
    rec = get_user(uid)
    rec["settings"][key] = value
    save_data(data)
    await state.clear()
    return True


@dp.message(Form.movies_topic)
async def movies_topic_handler(message: Message, state: FSMContext):
    uid = message.from_user.id
    raw = (message.text or "").strip()
    if raw == "/cancel":
        await state.clear()
        return await message.answer("✅ Bekor qilindi.", reply_markup=panel_kb(uid))
    if not raw or len(raw) > 100:
        return await message.answer("⚠️ 1–100 belgi kiriting:")
    data = load_data()
    rec = get_user(uid)
    rec["settings"]["movies_topic"] = raw
    save_data(data)
    await state.clear()
    await message.answer(f"✅ Kinolar mavzusi: <b>{_esc(raw)}</b>", reply_markup=cfg_kb(uid))


@dp.callback_query(F.data == "toggle_auto")
async def toggle_auto_cb(call: CallbackQuery):
    uid = call.from_user.id
    data = load_data()
    rec = get_user(uid)
    rec["settings"]["auto"] = not bool(rec["settings"].get("auto"))
    save_data(data)
    state_text = "yoqildi" if rec["settings"]["auto"] else "o'chirildi"
    await call.answer(f"✅ Avto rejim {state_text}")
    await show_panel(call.message, uid, edit=True)


@dp.callback_query(F.data == "run_now")
async def run_now_cb(call: CallbackQuery):
    uid = call.from_user.id
    settings = user_settings(uid)
    if not settings.get("source") or not settings.get("dest"):
        return await call.answer("❌ Avval manba va manzil kanallarni tanlang", show_alert=True)
    if not get_session(uid):
        return await call.answer("❌ Avval akkauntga kiring", show_alert=True)
    if run_lock(uid).locked():
        return await call.answer("⏳ Forward allaqachon ketmoqda", show_alert=True)
    await call.answer("🚀 Boshlandi. Natijani shu chatga yozaman.")
    spawn(run_forward(uid, notify_chat_id=uid))


@dp.callback_query(F.data == "reset_stats")
async def reset_stats_cb(call: CallbackQuery):
    uid = call.from_user.id
    data = load_data()
    rec = get_user(uid)
    rec["stats"] = {}
    save_data(data)
    await call.answer("🧹 Tozalandi")
    try:
        await call.message.edit_text("⚙️ <b>Sozlamalar</b>", reply_markup=cfg_kb(uid))
    except Exception:
        await call.message.answer("⚙️ <b>Sozlamalar</b>", reply_markup=cfg_kb(uid))


@dp.callback_query(F.data == "stats")
async def stats_cb(call: CallbackQuery):
    stats = get_user(call.from_user.id).get("stats", {})
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
            f"✅ Yuborildi: {int(stats.get('forwarded', 0) or 0) + int(stats.get('copied', 0) or 0)}\n"
            f"📋 Akkaunt nomidan: {stats.get('copied', 0)}\n"
            f"♻️ Allaqachon bor: {stats.get('skipped_existing', 0)}\n"
            f"🆕 Mavzular: {stats.get('topics_created', 0)}\n"
            f"⚠️ Xatolar: {stats.get('failed', 0)}\n"
            f"⏹ To'xtatilgan: {'ha' if stats.get('stopped') else 'yo‘q'}\n"
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
    await message.answer("📋 Menyudan foydalaning.", reply_markup=panel_kb(message.from_user.id))


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


async def restore_clients():
    """Ishga tushganda barcha saqlangan userbot sessionlarini tiklaydi."""
    await asyncio.sleep(2)
    for key, rec in list(all_users().items()):
        if not (rec.get("userbot") or {}).get("session"):
            continue
        try:
            client = await userbot_client(int(key))
        except Exception:
            logging.exception("Sessionni tiklab bo'lmadi (uid=%s)", key)
            continue
        if client is not None:
            logging.info("Akkaunt ulandi (uid=%s)", key)


async def main():
    runner = await start_web_server()
    auto_task = spawn(auto_loop())
    spawn(restore_clients())
    try:
        await dp.start_polling(bot)
    finally:
        auto_task.cancel()
        try:
            await auto_task
        except asyncio.CancelledError:
            pass
        await close_all_clients()
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
