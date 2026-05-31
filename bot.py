import asyncio
import logging
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.enums import ParseMode
from aiogram.types import Message

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("telegram-auth-bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
API_SECRET = os.environ.get("API_SECRET", "change-me")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "")
PORT = int(os.environ.get("PORT", "8080"))
DB_PATH = Path(os.environ.get("DATABASE_PATH", "auth.db"))
CODE_TTL_MINUTES = int(os.environ.get("CODE_TTL_MINUTES", "10"))

CODE_PATTERN = re.compile(r"^[A-HJ-NP-Z2-9]{4,8}$")

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
                linked_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS pending_codes (
                code TEXT PRIMARY KEY,
                minecraft_uuid TEXT NOT NULL,
                minecraft_name TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_pending_uuid ON pending_codes(minecraft_uuid);
            """
        )
        conn.commit()


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


async def process_auth_code(message: Message, code: str) -> None:
    if not message.from_user:
        return

    code = code.upper().strip()
    telegram_id = message.from_user.id
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
            "INSERT INTO links (minecraft_uuid, minecraft_name, telegram_id, linked_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(minecraft_uuid) DO UPDATE SET "
            "minecraft_name=excluded.minecraft_name, telegram_id=excluded.telegram_id, linked_at=excluded.linked_at",
            (mc_uuid, mc_name, telegram_id, now),
        )
        conn.execute("DELETE FROM pending_codes WHERE code = ?", (code,))
        conn.commit()

    await message.answer(
        "✅ <b>Авторизация успешна!</b>\n\n"
        f"🎮 Игрок: <code>{mc_name}</code>\n"
        f"📱 Telegram: {message.from_user.full_name}\n\n"
        "Вернитесь в Minecraft — доступ откроется автоматически.\n"
        "Приятной игры! ⛏️",
        parse_mode=ParseMode.HTML,
    )
    log.info("Linked %s (%s) to telegram %s", mc_name, mc_uuid, telegram_id)


@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    if not message.from_user:
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "🎮 <b>Авторизация Minecraft</b>\n\n"
            "Зайдите на сервер — в чате появится ссылка или код для привязки.\n"
            "Отправьте код сюда в чат или нажмите ссылку из игры.\n\n"
            "<i>Один Telegram = один игровой аккаунт.</i>",
            parse_mode=ParseMode.HTML,
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

    await start_web(app)

    if not BOT_USERNAME:
        me = await bot.get_me()
        log.info("Bot username: @%s (set BOT_USERNAME env for deep links)", me.username)
    else:
        log.info("Bot username: @%s", BOT_USERNAME)

    log.info("Starting Telegram polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
