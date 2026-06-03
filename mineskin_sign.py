import base64
import hashlib
import json
import logging
import os
import re
from typing import Any, Dict, Optional, Tuple

import aiohttp

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


def build_skin_name(mc_uuid: str, updated_ms: int, png_bytes: Optional[bytes] = None) -> str:
    compact = mc_uuid.replace("-", "").lower()[:16]
    suffix = ""
    if png_bytes:
        suffix = "-" + hashlib.sha256(png_bytes).hexdigest()[:10]
    return f"tg-{compact}-{updated_ms}{suffix}"


async def sign_skin_png(
    png_bytes: bytes, mc_uuid: str, updated_ms: int
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Sign skin after PNG is saved on bot API (URL method uses hosted texture.png)."""
    if PUBLIC_BASE_URL:
        value, signature, error = await _sign_from_url(mc_uuid, updated_ms, png_bytes)
        if value and signature:
            if _texture_looks_valid(value):
                return value, signature, None
            error = error or "подпись не содержит textures.minecraft.net"
        log.warning("MineSkin URL sign for %s failed (%s), trying upload", mc_uuid, error)
    else:
        log.warning("PUBLIC_BASE_URL / RENDER_EXTERNAL_URL not set — using MineSkin upload")

    return await _sign_from_upload(png_bytes, mc_uuid, updated_ms)


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

    if _is_duplicate(body):
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

    value, signature = _parse_texture(body)
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

    value, signature = _parse_texture(body)
    if value and signature and _texture_looks_valid(value):
        return value, signature, None

    if _is_duplicate(body):
        log.warning("MineSkin upload duplicate for %s, retrying", skin_name)
        body, error = await _upload_bytes(png_bytes, f"{skin_name}-u2")
        if error:
            return None, None, error
        value, signature = _parse_texture(body)
        if value and signature and _texture_looks_valid(value):
            return value, signature, None

    return None, None, "MineSkin upload: не удалось получить подписанную текстуру"


async def _post_form(
    endpoint: str, fields: Dict[str, str]
) -> Tuple[Optional[str], Optional[str]]:
    form = aiohttp.FormData()
    for key, value in fields.items():
        form.add_field(key, value)

    timeout = aiohttp.ClientTimeout(total=MINESKIN_TIMEOUT_SEC)
    headers = {"User-Agent": MINESKIN_USER_AGENT, "Accept": "application/json"}

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(endpoint, data=form, headers=headers) as resp:
                text = await resp.text()
                if resp.status < 200 or resp.status >= 300:
                    return None, f"MineSkin HTTP {resp.status}: {text[:200]}"
                return text, None
    except aiohttp.ClientError as exc:
        return None, f"MineSkin недоступен: {exc}"


async def _upload_bytes(png_bytes: bytes, skin_name: str) -> Tuple[Optional[str], Optional[str]]:
    form = aiohttp.FormData()
    form.add_field("file", png_bytes, filename="skin.png", content_type="image/png")
    form.add_field("visibility", "1")
    form.add_field("name", skin_name)
    form.add_field("model", "")
    return await _post_multipart(MINESKIN_UPLOAD_URL, form)


async def _post_multipart(
    endpoint: str, form: aiohttp.FormData
) -> Tuple[Optional[str], Optional[str]]:
    timeout = aiohttp.ClientTimeout(total=MINESKIN_TIMEOUT_SEC)
    headers = {"User-Agent": MINESKIN_USER_AGENT, "Accept": "application/json"}

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(endpoint, data=form, headers=headers) as resp:
                text = await resp.text()
                if resp.status < 200 or resp.status >= 300:
                    return None, f"MineSkin HTTP {resp.status}: {text[:200]}"
                return text, None
    except aiohttp.ClientError as exc:
        return None, f"MineSkin недоступен: {exc}"


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
