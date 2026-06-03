import asyncio
import base64
import hashlib
import io
import json
import logging
import os
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

import aiohttp
from PIL import Image

log = logging.getLogger("telegram-auth-bot")

PUBLIC_BASE_URL = (
    os.environ.get("PUBLIC_BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL", "")
).rstrip("/")
MINESKIN_UPLOAD_URL = os.environ.get(
    "MINESKIN_UPLOAD_URL", "https://api.mineskin.org/generate/upload"
)
MINESKIN_URL_ENDPOINT = os.environ.get(
    "MINESKIN_URL_ENDPOINT", "https://api.mineskin.org/generate/url"
)
MINESKIN_USER_AGENT = os.environ.get("MINESKIN_USER_AGENT", "TelegramAuthBot/1.0")
MINESKIN_TIMEOUT_SEC = int(os.environ.get("MINESKIN_TIMEOUT_SEC", "120"))
MINESKIN_MAX_RETRIES = int(os.environ.get("MINESKIN_MAX_RETRIES", "8"))
MINESKIN_RETRY_BUFFER_SEC = float(os.environ.get("MINESKIN_RETRY_BUFFER_SEC", "1.5"))

_mineskin_lock = asyncio.Lock()


def build_skin_name(mc_uuid: str, updated_ms: int, png_bytes: Optional[bytes] = None) -> str:
    compact = mc_uuid.replace("-", "").lower()[:16]
    suffix = ""
    if png_bytes:
        suffix = "-" + hashlib.sha256(png_bytes).hexdigest()[:10]
    return f"tg-{compact}-{updated_ms}{suffix}"


async def sign_skin_png(
    png_bytes: bytes, mc_uuid: str, updated_ms: int
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Sign skin after PNG is saved on bot API (serialized + retries on rate limit)."""
    async with _mineskin_lock:
        attempts: List[Tuple[str, Any]] = []
        if PUBLIC_BASE_URL:
            attempts.append(("url", _sign_from_url))
        attempts.append(("upload", _sign_from_upload))

        last_error: Optional[str] = None
        for label, signer in attempts:
            if signer is _sign_from_url:
                value, signature, error = await signer(mc_uuid, updated_ms, png_bytes)
            else:
                value, signature, error = await signer(png_bytes, mc_uuid, updated_ms)

            if error and _is_rate_limit_message(error):
                return None, None, error
            if error:
                last_error = error
                log.warning("MineSkin %s sign for %s: %s", label, mc_uuid, error)
                continue
            if not value or not signature:
                last_error = f"MineSkin {label}: пустая подпись"
                continue
            if await _verify_signed_matches_png(value, png_bytes):
                return value, signature, None
            log.warning("MineSkin %s returned wrong skin for %s, trying next method", label, mc_uuid)
            last_error = "MineSkin подписал чужой скин (кэш), повторите загрузку"

        return None, None, last_error or "Не удалось подписать скин"


async def _sign_from_url(
    mc_uuid: str, updated_ms: int, png_bytes: bytes
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    fp = hashlib.sha256(png_bytes).hexdigest()[:12]
    skin_url = (
        f"{PUBLIC_BASE_URL}/api/skin/{mc_uuid.lower()}/texture.png"
        f"?v={updated_ms}&h={fp}"
    )
    skin_name = build_skin_name(mc_uuid, updated_ms, png_bytes)
    body, error = await _post_form(
        MINESKIN_URL_ENDPOINT,
        {
            "url": skin_url,
            "visibility": "1",
            "name": skin_name,
            "model": "",
        },
    )
    if error:
        return None, None, error
    if body and _is_duplicate(body) and not _parse_texture(body or "")[0]:
        body, error = await _post_form(
            MINESKIN_URL_ENDPOINT,
            {
                "url": skin_url + "&retry=1",
                "visibility": "1",
                "name": f"{skin_name}-r1",
                "model": "",
            },
        )
        if error:
            return None, None, error

    value, signature = _parse_texture(body or "")
    if value and signature and _texture_looks_valid(value):
        return value, signature, None
    return None, None, "MineSkin URL: не удалось получить подписанную текстуру"


async def _sign_from_upload(
    png_bytes: bytes, mc_uuid: str, updated_ms: int
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    skin_name = build_skin_name(mc_uuid, updated_ms, png_bytes)
    body, error = await _upload_bytes(png_bytes, skin_name)
    if error:
        return None, None, error

    value, signature = _parse_texture(body or "")
    if value and signature and _texture_looks_valid(value):
        return value, signature, None

    if body and _is_duplicate(body) and not (value and signature):
        log.warning("MineSkin upload duplicate for %s, retrying", skin_name)
        body, error = await _upload_bytes(png_bytes, f"{skin_name}-u2")
        if error:
            return None, None, error
        value, signature = _parse_texture(body or "")
        if value and signature and _texture_looks_valid(value):
            return value, signature, None

    return None, None, "MineSkin upload: не удалось получить подписанную текстуру"


async def _post_form(
    endpoint: str, fields: Dict[str, str]
) -> Tuple[Optional[str], Optional[str]]:
    def build_form() -> aiohttp.FormData:
        form = aiohttp.FormData()
        for key, value in fields.items():
            form.add_field(key, value)
        return form

    return await _mineskin_request(endpoint, build_form)


async def _upload_bytes(png_bytes: bytes, skin_name: str) -> Tuple[Optional[str], Optional[str]]:
    def build_form() -> aiohttp.FormData:
        form = aiohttp.FormData()
        form.add_field("file", png_bytes, filename="skin.png", content_type="image/png")
        form.add_field("visibility", "1")
        form.add_field("name", skin_name)
        form.add_field("model", "")
        return form

    return await _mineskin_request(MINESKIN_UPLOAD_URL, build_form)


async def _mineskin_request(
    endpoint: str, build_form: Callable[[], aiohttp.FormData]
) -> Tuple[Optional[str], Optional[str]]:
    timeout = aiohttp.ClientTimeout(total=MINESKIN_TIMEOUT_SEC)
    headers = {"User-Agent": MINESKIN_USER_AGENT, "Accept": "application/json"}

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for attempt in range(1, MINESKIN_MAX_RETRIES + 1):
                async with session.post(endpoint, data=build_form(), headers=headers) as resp:
                    text = await resp.text()
                    if resp.status in (429, 503):
                        delay = _retry_delay_seconds(text, resp.headers)
                        log.info(
                            "MineSkin rate limit HTTP %s, attempt %s/%s, wait %.1fs",
                            resp.status,
                            attempt,
                            MINESKIN_MAX_RETRIES,
                            delay,
                        )
                        if attempt >= MINESKIN_MAX_RETRIES:
                            return None, _rate_limit_user_message(resp.status)
                        await asyncio.sleep(delay)
                        continue
                    if resp.status < 200 or resp.status >= 300:
                        return None, f"MineSkin HTTP {resp.status}: {text[:200]}"
                    return text, None
    except aiohttp.ClientError as exc:
        return None, f"MineSkin недоступен: {exc}"

    return None, _rate_limit_user_message(429)


def _retry_delay_seconds(body: str, headers: Any) -> float:
    try:
        data = json.loads(body)
        delay_info = data.get("delayInfo")
        if isinstance(delay_info, dict):
            millis = delay_info.get("millis")
            if millis is not None:
                return max(float(millis) / 1000.0, 1.0) + MINESKIN_RETRY_BUFFER_SEC
            seconds = delay_info.get("seconds")
            if seconds is not None:
                return max(float(seconds), 1.0) + MINESKIN_RETRY_BUFFER_SEC
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    retry_after = None
    if hasattr(headers, "get"):
        retry_after = headers.get("Retry-After")
    if retry_after:
        try:
            return max(float(retry_after), 1.0) + MINESKIN_RETRY_BUFFER_SEC
        except ValueError:
            pass

    return 6.0 + MINESKIN_RETRY_BUFFER_SEC


def _rate_limit_user_message(status: int) -> str:
    return (
        f"MineSkin временно ограничил запросы (HTTP {status}). "
        "Бот уже ждал повтор — попробуйте загрузить скин ещё раз через 1–2 минуты."
    )


def _is_rate_limit_message(error: str) -> bool:
    return "429" in error or "503" in error or "ограничил запросы" in error


def _parse_texture(json_text: str) -> Tuple[Optional[str], Optional[str]]:
    try:
        data = json.loads(json_text)
    except json.JSONDecodeError:
        return _parse_texture_regex(json_text)

    value, signature = _extract_from_obj(data)
    if value and signature:
        return value, signature
    return _parse_texture_regex(json_text)


def _extract_from_obj(data: Any) -> Tuple[Optional[str], Optional[str]]:
    if not isinstance(data, dict):
        return None, None

    for key in ("data", "skin"):
        block = data.get(key)
        if not isinstance(block, dict):
            continue
        texture = block.get("texture")
        if isinstance(texture, dict):
            if "value" in texture and "signature" in texture:
                return texture.get("value"), texture.get("signature")
            inner = texture.get("data")
            if isinstance(inner, dict):
                return inner.get("value"), inner.get("signature")

    texture = data.get("texture")
    if isinstance(texture, dict) and "value" in texture:
        return texture.get("value"), texture.get("signature")

    return None, None


def _parse_texture_regex(json_text: str) -> Tuple[Optional[str], Optional[str]]:
    block = None
    for anchor in ('"data"', '"skin"'):
        idx = json_text.find(anchor)
        if idx >= 0:
            tex_idx = json_text.find('"texture"', idx)
            if tex_idx >= 0:
                block = json_text[tex_idx : tex_idx + 4000]
                break
    if not block:
        block = json_text

    values = re.findall(r'"value"\s*:\s*"([^"]+)"', block)
    signatures = re.findall(r'"signature"\s*:\s*"([^"]+)"', block)

    for value in values:
        if not _texture_looks_valid(value):
            continue
        for signature in signatures:
            if signature and len(signature) > 32:
                return value, signature
    return None, None


def _texture_looks_valid(value: str) -> bool:
    if not value or len(value) < 40:
        return False
    try:
        decoded = base64.b64decode(value).decode("utf-8", errors="ignore")
        return "textures.minecraft.net" in decoded
    except Exception:
        return False


def _is_duplicate(json_text: str) -> bool:
    return '"duplicate":true' in json_text or '"duplicate": true' in json_text


async def _verify_signed_matches_png(texture_value: str, png_bytes: bytes) -> bool:
    try:
        decoded = json.loads(base64.b64decode(texture_value).decode("utf-8"))
        skin_url = decoded.get("textures", {}).get("SKIN", {}).get("url")
        if not skin_url:
            return False
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(skin_url) as resp:
                if resp.status != 200:
                    return False
                remote_png = await resp.read()
        return _png_pixels_equal(png_bytes, remote_png)
    except Exception as exc:
        log.warning("Skin verify failed: %s", exc)
        return False


def _png_pixels_equal(expected: bytes, actual: bytes) -> bool:
    try:
        a = Image.open(io.BytesIO(expected)).convert("RGBA")
        b = Image.open(io.BytesIO(actual)).convert("RGBA")
        if a.size != b.size:
            b = b.resize(a.size, Image.Resampling.NEAREST)
        return list(a.getdata()) == list(b.getdata())
    except Exception:
        return hashlib.sha256(expected).digest() == hashlib.sha256(actual).digest()
