import asyncio
import base64
import io
import logging
import os
import re
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Tuple

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.enums import ParseMode
from aiogram.types import (
    BotCommand,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from PIL import Image
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("telegram-auth-bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
API_SECRET = os.environ.get("API_SECRET", "change-me")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "")
PORT = int(os.environ.get("PORT", "8080"))
DB_PATH = Path(os.environ.get("DATABASE_PATH", "auth.db"))
CODE_TTL_MINUTES = int(os.environ.get("CODE_TTL_MINUTES", "10"))
LOGIN_CODE_TTL_MINUTES = int(os.environ.get("LOGIN_CODE_TTL_MINUTES", "5"))

CODE_PATTERN = re.compile(r"^[A-HJ-NP-Z2-9]{4,8}$")
ALLOWED_SKIN_SIZES = {(64, 32), (64, 64), (128, 64), (128, 128)}
SKIN_BUTTON_TEXT = "🎨 Сменить скин"
PREV_SKIN_BUTTON_TEXT = "↩️ Предыдущий скин"

bot = Bot(BOT_TOKEN)
dp = Dispatcher()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def init_db() -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS links (
                minecraft_uuid TEXT PRIMARY KEY,
                minecraft_name TEXT NOT NULL,
                telegram_id INTEGER NOT NULL UNIQUE,
                telegram_username TEXT,
                telegram_name TEXT,
                linked_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS pending_codes (
                code TEXT PRIMARY KEY,
                minecraft_uuid TEXT NOT NULL,
                minecraft_name TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS login_codes (
                minecraft_uuid TEXT PRIMARY KEY,
                code TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_pending_uuid ON pending_codes(minecraft_uuid);
            """
        )
        migrate_db(conn)
        conn.commit()


def migrate_db(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(links)")}
    if "telegram_username" not in cols:
        conn.execute("ALTER TABLE links ADD COLUMN telegram_username TEXT")
    if "telegram_name" not in cols:
        conn.execute("ALTER TABLE links ADD COLUMN telegram_name TEXT")

    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    if "login_codes" not in tables:
        conn.execute(
            """
            CREATE TABLE login_codes (
                minecraft_uuid TEXT PRIMARY KEY,
                code TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )
            """
        )
    if "custom_skins" not in tables:
        conn.execute(
            """
            CREATE TABLE custom_skins (
                minecraft_uuid TEXT PRIMARY KEY,
                minecraft_name TEXT NOT NULL,
                telegram_id INTEGER NOT NULL,
                skin_png_base64 TEXT NOT NULL,
                updated_ms INTEGER NOT NULL
            )
            """
        )
    if "skin_upload_wait" not in tables:
        conn.execute(
            """
            CREATE TABLE skin_upload_wait (
                telegram_id INTEGER PRIMARY KEY,
                started_at TEXT NOT NULL
            )
            """
        )
    if "custom_skins" in tables:
        skin_cols = {row[1] for row in conn.execute("PRAGMA table_info(custom_skins)")}
        if "skin_rgba_base64" not in skin_cols:
            conn.execute("ALTER TABLE custom_skins ADD COLUMN skin_rgba_base64 TEXT")
        if "prev_skin_png_base64" not in skin_cols:
            conn.execute("ALTER TABLE custom_skins ADD COLUMN prev_skin_png_base64 TEXT")
        if "prev_skin_rgba_base64" not in skin_cols:
            conn.execute("ALTER TABLE custom_skins ADD COLUMN prev_skin_rgba_base64 TEXT")


def json_secret_ok(data: dict) -> bool:
    return data.get("secret") == API_SECRET


async def api_check(request: web.Request) -> web.Response:
    if request.query.get("secret") != API_SECRET:
        return web.json_response({"error": "unauthorized"}, status=401)

    uuid = request.match_info["uuid"].lower()
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute(
            "SELECT 1 FROM links WHERE minecraft_uuid = ?",
            (uuid,),
        ).fetchone()

    return web.json_response({"authorized": row is not None})


async def api_session(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    if not json_secret_ok(data):
        return web.json_response({"error": "unauthorized"}, status=401)

    uuid = str(data.get("uuid", "")).lower()
    name = str(data.get("name", ""))[:16]
    code = str(data.get("code", "")).upper().strip()

    if not uuid or not name or not code:
        return web.json_response({"error": "missing fields"}, status=400)

    expires_at = (utcnow() + timedelta(minutes=CODE_TTL_MINUTES)).isoformat()

    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("DELETE FROM pending_codes WHERE minecraft_uuid = ?", (uuid,))
        conn.execute(
            "INSERT OR REPLACE INTO pending_codes (code, minecraft_uuid, minecraft_name, expires_at) "
            "VALUES (?, ?, ?, ?)",
            (code, uuid, name, expires_at),
        )
        conn.commit()

    log.info("Pending code %s for %s (%s)", code, name, uuid)
    return web.json_response({"ok": True})


async def api_health(_: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def api_login_session(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    if not json_secret_ok(data):
        return web.json_response({"error": "unauthorized"}, status=401)

    uuid = str(data.get("uuid", "")).lower()
    name = str(data.get("name", ""))[:16]
    code = str(data.get("code", "")).strip()

    if not uuid or not name or not code:
        return web.json_response({"error": "missing fields"}, status=400)

    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute(
            "SELECT telegram_id FROM links WHERE minecraft_uuid = ?",
            (uuid,),
        ).fetchone()

    if not row:
        return web.json_response({"ok": False, "error": "not_linked"}, status=404)

    telegram_id = row[0]
    expires_at = (utcnow() + timedelta(minutes=LOGIN_CODE_TTL_MINUTES)).isoformat()

    try:
        await bot.send_message(
            telegram_id,
            "🔐 <b>Код входа на сервер</b>\n\n"
            f"🎮 Игрок: <code>{name}</code>\n"
            f"🔢 Код: <code>{code}</code>\n\n"
            "Введите этот код в <b>чат Minecraft</b>.\n"
            f"<i>Действует {LOGIN_CODE_TTL_MINUTES} мин.</i>",
            parse_mode=ParseMode.HTML,
        )
    except Exception as exc:
        log.error("Failed to send login code to %s: %s", telegram_id, exc)
        return web.json_response({"ok": False, "error": "send_failed"}, status=502)

    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO login_codes (minecraft_uuid, code, expires_at) "
            "VALUES (?, ?, ?)",
            (uuid, code, expires_at),
        )
        conn.commit()

    log.info("Login code %s for %s (%s) -> telegram %s", code, name, uuid, telegram_id)
    return web.json_response({"ok": True})


async def api_login_verify(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    if not json_secret_ok(data):
        return web.json_response({"error": "unauthorized"}, status=401)

    uuid = str(data.get("uuid", "")).lower()
    code = str(data.get("code", "")).strip()

    if not uuid or not code:
        return web.json_response({"error": "missing fields"}, status=400)

    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute(
            "SELECT code, expires_at FROM login_codes WHERE minecraft_uuid = ?",
            (uuid,),
        ).fetchone()

        if not row:
            return web.json_response({"ok": False, "error": "no_code"})

        expected, expires_at = row
        if datetime.fromisoformat(expires_at) < utcnow():
            conn.execute("DELETE FROM login_codes WHERE minecraft_uuid = ?", (uuid,))
            conn.commit()
            return web.json_response({"ok": False, "error": "expired"})

        if code != expected:
            return web.json_response({"ok": False, "error": "wrong"})

        conn.execute("DELETE FROM login_codes WHERE minecraft_uuid = ?", (uuid,))
        conn.commit()

    log.info("Login verified for %s", uuid)
    return web.json_response({"ok": True})


def _find_link(conn: sqlite3.Connection, name: Optional[str], uuid: Optional[str]):
    if name:
        return conn.execute(
            "SELECT minecraft_uuid, minecraft_name, telegram_id, telegram_username, telegram_name, linked_at "
            "FROM links WHERE lower(minecraft_name) = lower(?) LIMIT 1",
            (name.strip(),),
        ).fetchone()
    if uuid:
        return conn.execute(
            "SELECT minecraft_uuid, minecraft_name, telegram_id, telegram_username, telegram_name, linked_at "
            "FROM links WHERE minecraft_uuid = ? LIMIT 1",
            (uuid.lower(),),
        ).fetchone()
    return None


def _link_payload(row) -> dict:
    mc_uuid, mc_name, telegram_id, telegram_username, telegram_name, linked_at = row
    return {
        "linked": True,
        "minecraft_uuid": mc_uuid,
        "minecraft_name": mc_name,
        "telegram_id": telegram_id,
        "telegram_username": telegram_username or "",
        "telegram_name": telegram_name or "",
        "linked_at": linked_at,
    }


async def api_lookup_name(request: web.Request) -> web.Response:
    if request.query.get("secret") != API_SECRET:
        return web.json_response({"error": "unauthorized"}, status=401)

    name = request.match_info.get("name", "").strip()
    if not name:
        return web.json_response({"error": "missing name"}, status=400)

    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = _find_link(conn, name, None)

    if not row:
        return web.json_response({"linked": False})

    return web.json_response(_link_payload(row))


async def api_unlink(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    if not json_secret_ok(data):
        return web.json_response({"error": "unauthorized"}, status=401)

    name = str(data.get("name", "")).strip()
    uuid = str(data.get("uuid", "")).lower().strip()
    if not name and not uuid:
        return web.json_response({"error": "missing fields"}, status=400)

    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = _find_link(conn, name or None, uuid or None)
        if not row:
            return web.json_response({"ok": False, "error": "not_linked"}, status=404)

        mc_uuid, mc_name, telegram_id, telegram_username, telegram_name, _linked_at = row
        conn.execute("DELETE FROM links WHERE minecraft_uuid = ?", (mc_uuid,))
        conn.execute("DELETE FROM pending_codes WHERE minecraft_uuid = ?", (mc_uuid,))
        conn.execute("DELETE FROM login_codes WHERE minecraft_uuid = ?", (mc_uuid,))
        conn.commit()

    log.info("Unlinked %s (%s) from telegram %s", mc_name, mc_uuid, telegram_id)
    return web.json_response(
        {
            "ok": True,
            "minecraft_uuid": mc_uuid,
            "minecraft_name": mc_name,
            "telegram_id": telegram_id,
            "telegram_username": telegram_username or "",
            "telegram_name": telegram_name or "",
        }
    )


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=SKIN_BUTTON_TEXT)],
            [KeyboardButton(text=PREV_SKIN_BUTTON_TEXT)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


async def setup_bot_commands() -> None:
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Главное меню"),
            BotCommand(command="skin", description="Сменить скин Minecraft"),
            BotCommand(command="prevskin", description="Вернуть предыдущий скин"),
        ]
    )


def get_link_by_telegram(conn: sqlite3.Connection, telegram_id: int):
    return conn.execute(
        "SELECT minecraft_uuid, minecraft_name FROM links WHERE telegram_id = ? LIMIT 1",
        (telegram_id,),
    ).fetchone()


def set_skin_wait(conn: sqlite3.Connection, telegram_id: int) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO skin_upload_wait (telegram_id, started_at) VALUES (?, ?)",
        (telegram_id, utcnow().isoformat()),
    )


def clear_skin_wait(conn: sqlite3.Connection, telegram_id: int) -> None:
    conn.execute("DELETE FROM skin_upload_wait WHERE telegram_id = ?", (telegram_id,))


def is_skin_wait(conn: sqlite3.Connection, telegram_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM skin_upload_wait WHERE telegram_id = ? LIMIT 1",
        (telegram_id,),
    ).fetchone()
    return row is not None


def png_to_legacy_rgba(image: Image.Image) -> bytes:
    width, height = image.size
    out = bytearray(width * height * 4)
    index = 0
    for y in range(height):
        for x in range(width):
            red, green, blue, alpha = image.getpixel((x, y))
            out[index] = red
            out[index + 1] = green
            out[index + 2] = blue
            out[index + 3] = alpha
            index += 4
    return bytes(out)


def process_skin_png(raw_bytes: bytes) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    try:
        image = Image.open(io.BytesIO(raw_bytes)).convert("RGBA")
    except Exception:
        return None, None, "Не удалось прочитать изображение. Отправьте PNG-файл скина."

    width, height = image.size
    if (width, height) not in ALLOWED_SKIN_SIZES:
        allowed = ", ".join(f"{w}x{h}" for w, h in sorted(ALLOWED_SKIN_SIZES))
        return None, None, f"Неверный размер {width}x{height}. Нужен один из: {allowed}."

    rgba_b64 = base64.b64encode(png_to_legacy_rgba(image)).decode("ascii")
    output = io.BytesIO()
    image.save(output, format="PNG")
    png_b64 = base64.b64encode(output.getvalue()).decode("ascii")
    return png_b64, rgba_b64, None


def save_custom_skin(
    conn: sqlite3.Connection,
    mc_uuid: str,
    mc_name: str,
    telegram_id: int,
    png_b64: str,
    rgba_b64: str,
) -> int:
    updated_ms = int(time.time() * 1000)
    conn.execute(
        "INSERT INTO custom_skins ("
        "minecraft_uuid, minecraft_name, telegram_id, "
        "skin_png_base64, skin_rgba_base64, updated_ms"
        ") VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(minecraft_uuid) DO UPDATE SET "
        "minecraft_name=excluded.minecraft_name, "
        "telegram_id=excluded.telegram_id, "
        "prev_skin_png_base64=custom_skins.skin_png_base64, "
        "prev_skin_rgba_base64=custom_skins.skin_rgba_base64, "
        "skin_png_base64=excluded.skin_png_base64, "
        "skin_rgba_base64=excluded.skin_rgba_base64, "
        "updated_ms=excluded.updated_ms",
        (mc_uuid, mc_name, telegram_id, png_b64, rgba_b64, updated_ms),
    )
    return updated_ms


def restore_previous_skin(conn: sqlite3.Connection, mc_uuid: str) -> Tuple[Optional[int], Optional[str]]:
    row = conn.execute(
        "SELECT prev_skin_png_base64, prev_skin_rgba_base64, "
        "skin_png_base64, skin_rgba_base64 "
        "FROM custom_skins WHERE minecraft_uuid = ?",
        (mc_uuid,),
    ).fetchone()
    if not row or not row[0]:
        return None, "Нет сохранённого предыдущего скина."
    prev_png, prev_rgba, cur_png, cur_rgba = row
    updated_ms = int(time.time() * 1000)
    conn.execute(
        "UPDATE custom_skins SET "
        "skin_png_base64=?, skin_rgba_base64=?, "
        "prev_skin_png_base64=?, prev_skin_rgba_base64=?, updated_ms=? "
        "WHERE minecraft_uuid=?",
        (prev_png, prev_rgba, cur_png, cur_rgba, updated_ms, mc_uuid),
    )
    return updated_ms, None


async def save_skin_upload(message: Message, raw_bytes: bytes) -> None:
    if not message.from_user:
        return

    telegram_id = message.from_user.id
    png_b64, rgba_b64, error = process_skin_png(raw_bytes)
    if error:
        await message.answer(error, parse_mode=ParseMode.HTML)
        return

    with closing(sqlite3.connect(DB_PATH)) as conn:
        link = get_link_by_telegram(conn, telegram_id)
        if not link:
            clear_skin_wait(conn, telegram_id)
            conn.commit()
            await message.answer(
                "❌ <b>Аккаунт не привязан</b>\n\nСначала авторизуйтесь на сервере.",
                parse_mode=ParseMode.HTML,
            )
            return

        mc_uuid, mc_name = link
        save_custom_skin(conn, mc_uuid, mc_name, telegram_id, png_b64, rgba_b64)
        clear_skin_wait(conn, telegram_id)
        conn.commit()

    await message.answer(
        "✅ <b>Скин сохранён!</b>\n\n"
        f"🎮 Игрок: <code>{mc_name}</code>\n"
        "Если вы на сервере — скин обновится через несколько секунд.",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(),
    )
    log.info("Skin updated for %s (%s) via telegram %s", mc_name, mc_uuid, telegram_id)


async def api_skin_get(request: web.Request) -> web.Response:
    if request.query.get("secret") != API_SECRET:
        return web.json_response({"error": "unauthorized"}, status=401)

    uuid = request.match_info.get("uuid", "").lower().strip()
    if not uuid:
        return web.json_response({"error": "missing uuid"}, status=400)

    try:
        since = int(request.query.get("since", "0"))
    except ValueError:
        since = 0

    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute(
            "SELECT skin_png_base64, skin_rgba_base64, updated_ms, minecraft_name FROM custom_skins WHERE minecraft_uuid = ?",
            (uuid,),
        ).fetchone()

    if not row:
        return web.json_response({"pending": False})

    skin_png_base64, skin_rgba_base64, updated_ms, minecraft_name = row
    if updated_ms <= since:
        return web.json_response({"pending": False})

    payload = {
        "pending": True,
        "updated_ms": updated_ms,
        "minecraft_name": minecraft_name,
        "skin_png_base64": skin_png_base64,
    }
    if skin_rgba_base64:
        payload["skin_rgba_base64"] = skin_rgba_base64
    return web.json_response(payload)


async def api_skin_texture(request: web.Request) -> web.Response:
    uuid = request.match_info.get("uuid", "").lower().strip()
    if not uuid:
        return web.Response(status=404, text="missing uuid")

    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute(
            "SELECT skin_png_base64 FROM custom_skins WHERE minecraft_uuid = ?",
            (uuid,),
        ).fetchone()

    if not row or not row[0]:
        return web.Response(status=404, text="skin not found")

    try:
        png_bytes = base64.b64decode(row[0])
    except Exception:
        return web.Response(status=500, text="invalid skin data")

    if not png_bytes:
        return web.Response(status=404, text="skin not found")

    return web.Response(
        body=png_bytes,
        content_type="image/png",
        headers={"Cache-Control": "public, max-age=300"},
    )


async def start_skin_upload(message: Message) -> None:
    if not message.from_user:
        return

    telegram_id = message.from_user.id
    with closing(sqlite3.connect(DB_PATH)) as conn:
        link = get_link_by_telegram(conn, telegram_id)
        if not link:
            await message.answer(
                "❌ <b>Аккаунт не привязан</b>\n\nСначала авторизуйтесь на сервере.",
                parse_mode=ParseMode.HTML,
                reply_markup=ReplyKeyboardRemove(),
            )
            return
        _mc_uuid, mc_name = link
        set_skin_wait(conn, telegram_id)
        conn.commit()

    await message.answer(
        "🎨 <b>Смена скина</b>\n\n"
        f"🎮 Аккаунт: <code>{mc_name}</code>\n\n"
        "Отправьте <b>PNG</b> как <b>документ</b> (скрепка → Файл).\n"
        "<b>Не как фото</b> — Telegram сжимает картинку.\n\n"
        "<i>Можно отправить PNG в любой момент — кнопку нажимать не обязательно.</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(),
    )


@dp.message(Command("skin"))
async def cmd_skin(message: Message) -> None:
    await start_skin_upload(message)


@dp.message(F.text == SKIN_BUTTON_TEXT)
async def on_skin_button_text(message: Message) -> None:
    await start_skin_upload(message)


async def apply_previous_skin(message: Message) -> None:
    if not message.from_user:
        return

    telegram_id = message.from_user.id
    with closing(sqlite3.connect(DB_PATH)) as conn:
        link = get_link_by_telegram(conn, telegram_id)
        if not link:
            await message.answer(
                "❌ <b>Аккаунт не привязан</b>\n\nСначала авторизуйтесь на сервере.",
                parse_mode=ParseMode.HTML,
            )
            return

        mc_uuid, mc_name = link
        _updated_ms, error = restore_previous_skin(conn, mc_uuid)
        if error:
            await message.answer(f"❌ {error}", parse_mode=ParseMode.HTML)
            return
        conn.commit()

    await message.answer(
        "✅ <b>Предыдущий скин восстановлен!</b>\n\n"
        f"🎮 Игрок: <code>{mc_name}</code>\n"
        "Если вы на сервере — скин обновится через несколько секунд.",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(),
    )
    log.info("Previous skin restored for %s (%s) via telegram %s", mc_name, mc_uuid, telegram_id)


@dp.message(F.text == PREV_SKIN_BUTTON_TEXT)
async def on_prev_skin_button(message: Message) -> None:
    await apply_previous_skin(message)


@dp.message(Command("prevskin"))
async def cmd_prevskin(message: Message) -> None:
    await apply_previous_skin(message)


@dp.message(F.photo)
async def on_photo(message: Message) -> None:
    if not message.from_user:
        return

    with closing(sqlite3.connect(DB_PATH)) as conn:
        link = get_link_by_telegram(conn, message.from_user.id)
    if not link:
        return

    await message.answer(
        "❌ <b>Не отправляйте скин как фото</b>\n\n"
        "Telegram сжимает фото.\n"
        "Отправьте PNG как <b>документ</b>: скрепка → Файл → .png",
        parse_mode=ParseMode.HTML,
    )


@dp.message(F.document)
async def on_document(message: Message) -> None:
    if not message.from_user or not message.document:
        return

    with closing(sqlite3.connect(DB_PATH)) as conn:
        link = get_link_by_telegram(conn, message.from_user.id)
    if not link:
        return

    document = message.document
    file_name = (document.file_name or "").lower()
    mime = (document.mime_type or "").lower()
    if not (file_name.endswith(".png") or mime == "image/png"):
        await message.answer("❌ Нужен PNG-файл скина (.png).")
        return

    file = await bot.get_file(document.file_id)
    if not file.file_path:
        await message.answer("❌ Не удалось скачать файл.")
        return

    downloaded = await bot.download_file(file.file_path)
    raw_bytes = downloaded.read()
    await save_skin_upload(message, raw_bytes)


async def process_auth_code(message: Message, code: str) -> None:
    if not message.from_user:
        return

    code = code.upper().strip()
    telegram_id = message.from_user.id
    telegram_username = message.from_user.username
    telegram_name = message.from_user.full_name
    now = utcnow().isoformat()

    with closing(sqlite3.connect(DB_PATH)) as conn:
        pending = conn.execute(
            "SELECT minecraft_uuid, minecraft_name, expires_at FROM pending_codes WHERE code = ?",
            (code,),
        ).fetchone()

        if not pending:
            await message.answer(
                "❌ <b>Код не найден</b>\n\n"
                "Такого кода нет — возможно, он уже использован или введён неверно.\n"
                "Зайдите на сервер заново и получите новый код.",
                parse_mode=ParseMode.HTML,
            )
            return

        mc_uuid, mc_name, expires_at = pending
        if datetime.fromisoformat(expires_at) < utcnow():
            conn.execute("DELETE FROM pending_codes WHERE code = ?", (code,))
            conn.commit()
            await message.answer(
                "⏰ <b>Код истёк</b>\n\n"
                "Зайдите на сервер снова — вам выдадут новый код.",
                parse_mode=ParseMode.HTML,
            )
            return

        existing_tg = conn.execute(
            "SELECT minecraft_name FROM links WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
        if existing_tg and existing_tg[0] != mc_name:
            await message.answer(
                "🚫 <b>Привязка недоступна</b>\n\n"
                f"Ваш Telegram уже привязан к аккаунту <code>{existing_tg[0]}</code>.\n\n"
                "<i>Один Telegram — один Minecraft-аккаунт.</i>",
                parse_mode=ParseMode.HTML,
            )
            return

        existing_uuid = conn.execute(
            "SELECT telegram_id FROM links WHERE minecraft_uuid = ?",
            (mc_uuid,),
        ).fetchone()
        if existing_uuid and existing_uuid[0] != telegram_id:
            await message.answer(
                "🚫 <b>Привязка недоступна</b>\n\n"
                f"Аккаунт <code>{mc_name}</code> уже привязан к другому Telegram.",
                parse_mode=ParseMode.HTML,
            )
            return

        conn.execute(
            "INSERT INTO links (minecraft_uuid, minecraft_name, telegram_id, telegram_username, telegram_name, linked_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(minecraft_uuid) DO UPDATE SET "
            "minecraft_name=excluded.minecraft_name, "
            "telegram_id=excluded.telegram_id, "
            "telegram_username=excluded.telegram_username, "
            "telegram_name=excluded.telegram_name, "
            "linked_at=excluded.linked_at",
            (mc_uuid, mc_name, telegram_id, telegram_username, telegram_name, now),
        )
        conn.execute("DELETE FROM pending_codes WHERE code = ?", (code,))
        conn.commit()

    tg_label = f"@{telegram_username}" if telegram_username else telegram_name
    await message.answer(
        "✅ <b>Авторизация успешна!</b>\n\n"
        f"🎮 Игрок: <code>{mc_name}</code>\n"
        f"📱 Telegram: {tg_label}\n\n"
        "Вернитесь в Minecraft — доступ откроется автоматически.\n"
        "Приятной игры! ⛏️",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(),
    )
    log.info(
        "Linked %s (%s) to telegram %s (@%s)",
        mc_name, mc_uuid, telegram_id, telegram_username or "-",
    )


@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    if not message.from_user:
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            link = get_link_by_telegram(conn, message.from_user.id)
        if link:
            _mc_uuid, mc_name = link
            await message.answer(
                "🎮 <b>Меню Minecraft</b>\n\n"
                f"Аккаунт: <code>{mc_name}</code>\n\n"
                "Выберите действие:",
                parse_mode=ParseMode.HTML,
                reply_markup=main_menu_keyboard(),
            )
            return

        await message.answer(
            "🎮 <b>Авторизация Minecraft</b>\n\n"
            "Зайдите на сервер — в чате появится ссылка или код для привязки.\n"
            "Отправьте код сюда в чат или нажмите ссылку из игры.\n\n"
            "<i>Один Telegram = один игровой аккаунт.</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    await process_auth_code(message, parts[1])


@dp.message(F.text & ~F.text.startswith("/"))
async def on_plain_code(message: Message) -> None:
    code = (message.text or "").strip().upper()
    if not CODE_PATTERN.match(code):
        return
    await process_auth_code(message, code)


async def start_web(app: web.Application) -> None:
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("HTTP API listening on port %s", PORT)


async def main() -> None:
    if API_SECRET == "change-me":
        log.warning("API_SECRET is default! Set a strong random secret.")

    init_db()

    app = web.Application()
    app.router.add_get("/health", api_health)
    app.router.add_get("/api/check/{uuid}", api_check)
    app.router.add_post("/api/session", api_session)
    app.router.add_post("/api/login-session", api_login_session)
    app.router.add_post("/api/login-verify", api_login_verify)
    app.router.add_get("/api/lookup/name/{name}", api_lookup_name)
    app.router.add_post("/api/unlink", api_unlink)
    app.router.add_get("/api/skin/{uuid}", api_skin_get)
    app.router.add_get("/api/skin/{uuid}/texture.png", api_skin_texture)

    await start_web(app)
    await setup_bot_commands()

    if not BOT_USERNAME:
        me = await bot.get_me()
        log.info("Bot username: @%s (set BOT_USERNAME env for deep links)", me.username)
    else:
        log.info("Bot username: @%s", BOT_USERNAME)

    log.info("Starting Telegram polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
