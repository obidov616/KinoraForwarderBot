import asyncio
import json
import logging
import os
import re
import threading
from html import escape as he

from aiohttp import web

# ================== SOZLAMALAR (Render Environment Variables) ==================
# API_ID, API_HASH  -> my.telegram.org dan olinadi
# BOT_TOKEN         -> BotFather tokeni
# OWNER_ID          -> bot egasining Telegram ID si (faqat owner ishlatadi)
# SESSION_STRING    -> ixtiyoriy: tayyor user session (bo'lmasa /login orqali olinadi)
# SESSION_FILE      -> session saqlanadigan fayl (default: user_session.txt)
# SCAN_LIMIT        -> kanaldan skanerlanadigan maksimal postlar soni (default: 2000)
# PORT              -> Render health-check porti

def _env_int(name, default=0):
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"XATO: {name} raqam bo'lishi kerak, lekin '{raw}' berildi.", flush=True)
        raise SystemExit(1)


def _fail(msg):
    print(f"XATO: {msg}", flush=True)
    raise SystemExit(1)


API_ID = _env_int("API_ID")
API_HASH = os.getenv("API_HASH", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = _env_int("OWNER_ID")
SESSION_STRING = os.getenv("SESSION_STRING", "").strip()
SESSION_FILE = os.getenv("SESSION_FILE", "user_session.txt").strip()
DATA_FILE = os.getenv("DATA_FILE", "forward_data.json").strip()
SCAN_LIMIT = _env_int("SCAN_LIMIT", 2000)
PORT = _env_int("PORT", 10000)

if not BOT_TOKEN:
    _fail("BOT_TOKEN env kiritilmagan. BotFather tokenini yozing.")
if not API_ID or not API_HASH:
    _fail("API_ID va API_HASH env kiritilmagan. my.telegram.org dan oling.")
if not OWNER_ID:
    _fail("OWNER_ID env kiritilmagan. O'z Telegram ID ingizni yozing.")


# ================== RENDER HEALTH-SERVER (darhol, alohida oqimda) ==================

def _health_page(request):
    return web.Response(text="ok")


def _run_health():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app = web.Application()
    app.router.add_get("/", _health_page)
    runner = web.AppRunner(app)
    loop.run_until_complete(runner.setup())
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    loop.run_until_complete(site.start())
    loop.run_forever()


threading.Thread(target=_run_health, daemon=True).start()
print(f"[health] Port {PORT} ochildi.", flush=True)

# ================== KUTUBXONALAR ==================

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart, Filter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, KeyboardButton, Message,
                           ReplyKeyboardMarkup)
from pyrogram import Client
from pyrogram import errors as pg_errors

logging.basicConfig(level=logging.INFO)

BTN_FORWARD = "🔄 Avto forward"
BTN_DOWNLOAD = "📥 Video yuklash"

# Reklama/tozalash kerak bo'lgan satr qoliplari
AD_LINE_PATTERNS = [
    r"kanalidan\s*olindi",
    r"@\w+",
    r"t\.me/",
    r"obuna\s*bo",
    r"reklama",
    r"kanalimiz",
    r"bizning\s*kanal",
    r"homiy",
]
NAME_RE = re.compile(r"\b(?:nomi|name|название|nom)\b\s*[:\-–]?\s*(.+)", re.IGNORECASE)


# ================== MA'LUMOTLAR ==================

DATA = {}


def load_data():
    global DATA
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, encoding="utf-8") as f:
                DATA = json.load(f)
    except Exception:
        DATA = {}
    if not isinstance(DATA, dict):
        DATA = {}
    DATA.setdefault("topics", {})


def save_data():
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(DATA, f, ensure_ascii=False)
    except Exception:
        pass


load_data()


# ================== YORDAMCHILAR ==================

def resolve_chat(ref):
    """Havola/username/ID dan (chat_id yoki username, message_id) ajratadi."""
    ref = (ref or "").strip()
    m = re.search(r"(?:https?://)?t\.me/c/(\d+)(?:/(\d+))?", ref)
    if m:
        cid = int("-100" + m.group(1))
        return cid, (int(m.group(2)) if m.group(2) else None)
    m = re.search(r"(?:https?://)?t\.me/([A-Za-z0-9_]{4,})(?:/(\d+))?", ref)
    if m:
        return m.group(1), (int(m.group(2)) if m.group(2) else None)
    if ref.startswith("@"):
        return ref[1:], None
    if ref.lstrip("-").isdigit():
        return int(ref), None
    return ref, None


def extract_name(text):
    """Izohdan kino nomini ajratadi: 'Nomi: ...' / 'nomi ...' yoki birinchi satr."""
    if not text:
        return ""
    text = text.strip()
    m = NAME_RE.search(text)
    if m:
        cand = m.group(1).strip().splitlines()[0].strip()
        cand = cand.strip("🎬🎥📺🍥🎞⭐•-— ")
        if cand:
            return cand[:128]
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if any(re.search(p, line, re.IGNORECASE) for p in AD_LINE_PATTERNS):
            continue
        line = line.strip("🎬🎥📺🍥🎞⭐•-— ")
        if line:
            return line[:128]
    return ""


def norm_name(name):
    return re.sub(r"\s+", " ", (name or "").strip().casefold())


def clean_caption(text):
    """Izohdagi kanal reklamasi (@username, t.me, 'kanalidan olindi') satrlarini olib tashlaydi."""
    if not text:
        return ""
    out = []
    for line in text.splitlines():
        if any(re.search(p, line, re.IGNORECASE) for p in AD_LINE_PATTERNS):
            continue
        out.append(line)
    cleaned = "\n".join(out)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


# ================== PYROGRAM USER CLIENT ==================

user_client = None
LOGIN = {}


def read_saved_session():
    if SESSION_STRING:
        return SESSION_STRING
    try:
        if os.path.exists(SESSION_FILE):
            with open(SESSION_FILE, encoding="utf-8") as f:
                return f.read().strip()
    except Exception:
        pass
    return ""


async def start_user_client(session_string):
    global user_client
    if user_client is not None:
        return user_client
    client = Client("kino_user", api_id=API_ID, api_hash=API_HASH,
                    session_string=session_string)
    await client.start()
    user_client = client
    return client


def get_user():
    return user_client


async def login_cleanup(uid):
    rec = LOGIN.pop(uid, None)
    if rec:
        try:
            await rec["client"].disconnect()
        except Exception:
            pass


# ================== AIOGRAM BOT ==================

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())


class OwnerFilter(Filter):
    async def __call__(self, obj):
        u = getattr(obj, "from_user", None)
        return u is not None and u.id == OWNER_ID


dp.message.filter(OwnerFilter())
dp.callback_query.filter(OwnerFilter())


class ForwardFlow(StatesGroup):
    source = State()
    dest = State()
    confirm = State()


class DownloadFlow(StatesGroup):
    link = State()


class LoginFlow(StatesGroup):
    phone = State()
    code = State()
    password = State()


def main_menu():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_FORWARD)], [KeyboardButton(text=BTN_DOWNLOAD)]],
        resize_keyboard=True)


# ================== ASOSIY MENYU ==================

@dp.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    holat = "✅ userbot faol" if get_user() is not None else "⚠️ session yo'q (/login qiling)"
    await m.answer(
        "🎬 <b>Kino ko'chiruvchi bot</b>\n\n"
        f"Holat: {holat}\n\nQuyidagi bo'limni tanlang:",
        reply_markup=main_menu())


@dp.message(Command("help"))
async def cmd_help(m: Message):
    await m.answer(
        "🎬 <b>Yordam</b>\n\n"
        "🔄 <b>Avto forward</b> — manba kanaldagi kinolarni manzil kanalda yo'qlarini "
        "aniqlab, nom bo'yicha mavzu yaratib ko'chiradi (reklama tozalanadi, yuboruvchi yashiriladi).\n"
        "📥 <b>Video yuklash</b> — post havolasini yuborsangiz, media Saved Messages ga tez ko'chiriladi.\n\n"
        "Buyruqlar:\n"
        "/login — user session ulash\n"
        "/cancel — joriy amalni bekor qilish\n"
        "/status — holatni ko'rish",
        reply_markup=main_menu())


@dp.message(Command("status"))
async def cmd_status(m: Message):
    holat = "✅ faol" if get_user() is not None else "❌ yo'q"
    await m.answer(
        f"📊 <b>Holat</b>\n\nUser session: {holat}\n"
        f"Owner ID: <code>{OWNER_ID}</code>\nSkaner limiti: {SCAN_LIMIT} post",
        reply_markup=main_menu())


@dp.message(Command("cancel"))
async def cancel_cmd(m: Message, state: FSMContext):
    await login_cleanup(m.from_user.id)
    await state.clear()
    await m.answer("❌ Bekor qilindi.", reply_markup=main_menu())


# ================== AVTO FORWARD ==================

@dp.message(F.text == BTN_FORWARD)
async def forward_entry(m: Message, state: FSMContext):
    await state.clear()
    if get_user() is None:
        return await m.answer("⚠️ User session yo'q. Avval /login qiling.", reply_markup=main_menu())
    await state.set_state(ForwardFlow.source)
    await m.answer(
        "🔄 <b>Avto forward</b>\n\n"
        "1️⃣ Manba kanal (1-kanal) havolasini yuboring:\n"
        "Masalan: @kanal yoki https://t.me/kanal\n\nBekor qilish: /cancel")


@dp.message(ForwardFlow.source, F.text)
async def forward_source(m: Message, state: FSMContext):
    await state.update_data(source=m.text.strip())
    await state.set_state(ForwardFlow.dest)
    await m.answer(
        "2️⃣ Manzil kanal (2-kanal) havolasini yuboring:\n"
        "Masalan: @kanal2 yoki https://t.me/kanal2")


@dp.message(ForwardFlow.dest, F.text)
async def forward_dest(m: Message, state: FSMContext):
    await state.update_data(dest=m.text.strip())
    data = await state.get_data()
    await state.set_state(ForwardFlow.confirm)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Boshlash", callback_data="fw:go"),
         InlineKeyboardButton(text="❌ Bekor", callback_data="fw:cancel")]])
    await m.answer(
        f"🔄 <b>Tayyor</b>\n\n📤 Manba: {he(data['source'])}\n📥 Manzil: {he(m.text.strip())}\n\n"
        "Bot manbadagi kinolarni manzilda yo'qlarini aniqlab, nom bo'yicha mavzu yaratib "
        "ko'chiradi. Boshlaymizmi?", reply_markup=kb)


@dp.message(ForwardFlow.confirm)
async def forward_confirm_wait(m: Message):
    await m.answer("👆 «✅ Boshlash» yoki «❌ Bekor» tugmasini bosing.")


@dp.callback_query(F.data == "fw:cancel")
async def forward_cancel(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    await cq.answer("Bekor qilindi")
    await cq.message.answer("❌ Bekor qilindi.", reply_markup=main_menu())


@dp.callback_query(F.data == "fw:go")
async def forward_go(cq: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    src = data.get("source")
    dst = data.get("dest")
    await state.clear()
    await cq.answer("⏳ Boshlandi")
    user = get_user()
    if user is None:
        return await cq.message.answer("⚠️ User session yo'q. Avval /login qiling.", reply_markup=main_menu())
    prog = await cq.message.answer("⏳ Skanerlanmoqda...")
    try:
        copied, skipped, topics = await run_forward(user, src, dst, prog)
        await prog.edit_text(
            f"✅ <b>Tayyor!</b>\n\n📥 Ko'chirildi: {copied} ta\n"
            f"⏭ O'tkazib yuborildi (mavjud): {skipped} ta\n🗂 Yaratilgan mavzular: {topics} ta")
    except Exception as exc:
        await prog.edit_text(f"❌ Xatolik: {type(exc).__name__}: {str(exc)[:200]}")
    await cq.message.answer("🏠 Bosh menyu", reply_markup=main_menu())


async def run_forward(user, source_ref, dest_ref, prog):
    src_id, _ = resolve_chat(source_ref)
    dst_id, _ = resolve_chat(dest_ref)
    src_chat = await user.get_chat(src_id)
    dst_chat = await user.get_chat(dst_id)
    dst_real = dst_chat.id
    is_forum = bool(getattr(dst_chat, "is_forum", False))

    await prog.edit_text(
        f"📖 Manba: {he(src_chat.title or str(src_id))}\n"
        f"📥 Manzil: {he(dst_chat.title or str(dst_real))}\n"
        f"🔎 Manzildagi mavjud kinolar o'rganilmoqda...")

    existing = set()
    async for m in user.get_chat_history(dst_real, limit=SCAN_LIMIT):
        nm = extract_name(m.caption or m.text or "")
        if nm:
            existing.add(norm_name(nm))

    topics_map = DATA.setdefault("topics", {}).setdefault(str(dst_real), {})
    copied = 0
    skipped = 0
    made = 0
    await prog.edit_text("⏳ Kinolar ko'chirilmoqda...")

    async for m in user.get_chat_history(src_chat.id, limit=SCAN_LIMIT):
        if not m.media:
            continue
        name = extract_name(m.caption or "")
        key = norm_name(name)
        if key and key in existing:
            skipped += 1
            continue
        thread_id = None
        if is_forum and name:
            if key not in topics_map:
                try:
                    created = await user.create_forum_topic(dst_real, name[:128])
                    tid = getattr(created, "message_thread_id", None) or created.id
                    topics_map[key] = tid
                    made += 1
                    save_data()
                except Exception:
                    topics_map[key] = None
            thread_id = topics_map.get(key)
        kw = {}
        if m.caption is not None:
            kw["caption"] = clean_caption(m.caption)
        if thread_id:
            kw["message_thread_id"] = thread_id
        try:
            await user.copy_message(dst_real, src_chat.id, m.id, **kw)
            copied += 1
            if key:
                existing.add(key)
        except pg_errors.FloodWait as e:
            await asyncio.sleep(int(e.value) + 1)
            try:
                await user.copy_message(dst_real, src_chat.id, m.id, **kw)
                copied += 1
            except Exception:
                pass
        except Exception:
            pass
        if copied and copied % 10 == 0:
            try:
                await prog.edit_text(f"⏳ Ko'chirilmoqda... {copied} ta")
            except Exception:
                pass
        await asyncio.sleep(0.4)

    save_data()
    return copied, skipped, made


# ================== VIDEO YUKLASH ==================

@dp.message(F.text == BTN_DOWNLOAD)
async def download_entry(m: Message, state: FSMContext):
    await state.clear()
    if get_user() is None:
        return await m.answer("⚠️ User session yo'q. Avval /login qiling.", reply_markup=main_menu())
    await state.set_state(DownloadFlow.link)
    await m.answer(
        "📥 <b>Video yuklash</b>\n\n"
        "Kerakli post havolasini yuboring:\n"
        "Masalan: https://t.me/c/1234567890/123 yoki https://t.me/kanal/456\n\n"
        "Media Saved Messages ga tez ko'chiriladi. Bekor: /cancel")


@dp.message(DownloadFlow.link, F.text)
async def download_link(m: Message, state: FSMContext):
    ref = m.text.strip()
    user = get_user()
    await state.clear()
    if user is None:
        return await m.answer("⚠️ User session yo'q. Avval /login qiling.", reply_markup=main_menu())
    chat_ref, msg_id = resolve_chat(ref)
    prog = await m.answer("⏳ Media ko'chirilmoqda...")
    try:
        chat = await user.get_chat(chat_ref)
        if not msg_id:
            await prog.edit_text(
                "⚠️ Havolada post ID yo'q. Aniq post havolasini yuboring (masalan t.me/c/.../123).")
            return await m.answer("🏠 Bosh menyu", reply_markup=main_menu())
        await user.copy_message("me", chat.id, msg_id)
        await prog.edit_text("✅ <b>Tayyor!</b> Media Saved Messages ga ko'chirildi.")
    except Exception as exc:
        await prog.edit_text(
            f"❌ Xatolik: {type(exc).__name__}: {str(exc)[:200]}\n"
            "User ushbu kanal/guruhga a'zo ekanini tekshiring.")
    await m.answer("🏠 Bosh menyu", reply_markup=main_menu())


# ================== USER SESSION LOGIN ==================

@dp.message(Command("login"))
async def login_start(m: Message, state: FSMContext):
    if get_user() is not None:
        return await m.answer("✅ User session allaqachon ishlayapti.", reply_markup=main_menu())
    await state.clear()
    await state.set_state(LoginFlow.phone)
    await m.answer(
        "🔐 <b>User session login</b>\n\n"
        "Telefon raqamingizni xalqaro formatda yuboring:\n"
        "Masalan: <code>+998901234567</code>\n\nBekor: /cancel")


@dp.message(LoginFlow.phone, F.text)
async def login_phone(m: Message, state: FSMContext):
    phone = m.text.strip()
    if not re.fullmatch(r"\+\d{7,15}", phone):
        return await m.answer("❌ Format noto'g'ri. Masalan: +998901234567")
    try:
        client = Client(f"login_{m.from_user.id}", api_id=API_ID, api_hash=API_HASH, in_memory=True)
        await client.connect()
        sent = await client.send_code(phone)
        LOGIN[m.from_user.id] = {"client": client, "phone": phone, "hash": sent.phone_code_hash}
        await state.set_state(LoginFlow.code)
        await m.answer("📩 Telegram yuborgan <b>kodni</b> kiriting:")
    except Exception as exc:
        await login_cleanup(m.from_user.id)
        await m.answer(f"❌ Kod yuborishda xato: {type(exc).__name__}: {str(exc)[:160]}")


@dp.message(LoginFlow.code, F.text)
async def login_code(m: Message, state: FSMContext):
    code = m.text.strip()
    rec = LOGIN.get(m.from_user.id)
    if not rec:
        await state.clear()
        return await m.answer("⚠️ Login sessiya topilmadi. /login qayta boshlang.", reply_markup=main_menu())
    try:
        await rec["client"].sign_in(rec["phone"], rec["hash"], code)
        await login_finish(m, state)
    except pg_errors.SessionPasswordNeeded:
        await state.set_state(LoginFlow.password)
        await m.answer("🔒 Ikki bosqichli parol (2FA) yoqilgan. <b>Parolni</b> kiriting:")
    except (pg_errors.PhoneCodeInvalid, pg_errors.PhoneCodeExpired) as e:
        await m.answer(f"❌ Kod xato yoki muddati o'tgan ({type(e).__name__}). Qayta kiriting yoki /login.")
    except Exception as exc:
        await m.answer(f"❌ Xato: {type(exc).__name__}: {str(exc)[:160]}")


@dp.message(LoginFlow.password, F.text)
async def login_password(m: Message, state: FSMContext):
    pwd = m.text.strip()
    rec = LOGIN.get(m.from_user.id)
    if not rec:
        await state.clear()
        return await m.answer("⚠️ Sessiya topilmadi. /login qiling.", reply_markup=main_menu())
    try:
        await rec["client"].check_password(pwd)
        await login_finish(m, state)
    except pg_errors.PasswordHashInvalid:
        await m.answer("❌ Parol xato. Qayta kiriting:")
    except Exception as exc:
        await m.answer(f"❌ Xato: {type(exc).__name__}: {str(exc)[:160]}")


async def login_finish(m: Message, state: FSMContext):
    rec = LOGIN.pop(m.from_user.id, None)
    await state.clear()
    if not rec:
        return await m.answer("⚠️ Sessiya topilmadi.", reply_markup=main_menu())
    try:
        session_string = await rec["client"].export_session_string()
    finally:
        try:
            await rec["client"].disconnect()
        except Exception:
            pass
    try:
        with open(SESSION_FILE, "w", encoding="utf-8") as f:
            f.write(session_string)
    except Exception:
        pass
    try:
        await start_user_client(session_string)
        await m.answer(
            "✅ <b>Session saqlandi va userbot ishga tushdi!</b>\n\n"
            "Endi Avto forward va Video yuklash ishlaydi.", reply_markup=main_menu())
    except Exception as exc:
        await m.answer(
            f"⚠️ Session saqlandi, lekin ishga tushirishda xato: {type(exc).__name__}: {str(exc)[:160]}",
            reply_markup=main_menu())


# ================== ISHGA TUSHIRISH ==================

async def main():
    print("[1/3] User session tekshirilmoqda...", flush=True)
    ss = read_saved_session()
    if ss:
        try:
            await start_user_client(ss)
            print("[2/3] Userbot ishga tushdi.", flush=True)
        except Exception as exc:
            print(f"[!] Userbot ishga tushmadi: {type(exc).__name__}: {exc}", flush=True)
    else:
        print("[2/3] Session yo'q — botda /login orqali kiriting.", flush=True)
    print("[3/3] Bot polling boshlanmoqda...", flush=True)
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass
    try:
        await dp.start_polling(bot)
    finally:
        if user_client is not None:
            try:
                await user_client.stop()
            except Exception:
                pass


if __name__ == "__main__":
    asyncio.run(main())