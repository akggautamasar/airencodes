"""
Telegram MTProto integration via Pyrogram.
Supports files up to 2 GB (vs 50 MB with the plain Bot API).

Required env vars:
  TELEGRAM_API_ID      — integer,  from https://my.telegram.org/apps
  TELEGRAM_API_HASH    — string,   from https://my.telegram.org/apps
  TELEGRAM_BOT_TOKEN   — string,   from @BotFather
  TELEGRAM_CHANNEL_ID  — @username  or  -100xxxx numeric ID

All uploads run in daemon threads so they never block the HTTP response.
"""

import asyncio
import hashlib
import io
import os
import threading

try:
    from pyrogram import Client
    from pyrogram.enums import ParseMode
    _HAS_PYROGRAM = True
except ImportError:
    _HAS_PYROGRAM = False


# ── config ────────────────────────────────────────────────────────────────────

def configured() -> bool:
    return bool(
        _HAS_PYROGRAM
        and os.environ.get("TELEGRAM_API_ID")
        and os.environ.get("TELEGRAM_API_HASH")
        and os.environ.get("TELEGRAM_BOT_TOKEN")
        and os.environ.get("TELEGRAM_CHANNEL_ID")
    )


def _make_client() -> "Client":
    return Client(
        name="airvault",
        api_id=int(os.environ["TELEGRAM_API_ID"]),
        api_hash=os.environ["TELEGRAM_API_HASH"],
        bot_token=os.environ["TELEGRAM_BOT_TOKEN"],
        in_memory=True,      # no session file written to disk
    )


def _chat():
    v = os.environ.get("TELEGRAM_CHANNEL_ID", "")
    try:
        return int(v)        # numeric  -100xxxxxxxxxx
    except ValueError:
        return v             # username @mychannel


# ── low-level sender ──────────────────────────────────────────────────────────

async def _send_doc(app: "Client", data: bytes, filename: str, caption: str = ""):
    bio = io.BytesIO(data)
    bio.name = filename      # Pyrogram reads .name to set the filename
    await app.send_document(
        chat_id=_chat(),
        document=bio,
        caption=caption[:1024],
        parse_mode=ParseMode.HTML,
    )


# ── thread helper ─────────────────────────────────────────────────────────────

def _fire(coro):
    """Run *coro* in a fresh daemon thread with its own event loop."""
    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(coro)
        except Exception as exc:
            print(f"[telegram] upload error: {exc}", flush=True)
        finally:
            loop.close()
    threading.Thread(target=_run, daemon=True).start()


# ── public API ────────────────────────────────────────────────────────────────

def send_encode(filename: str, original: bytes,
                parts: list, encrypted: bool) -> None:
    if configured():
        _fire(_async_encode(filename, original, parts, encrypted))


def send_decode(filename: str, data: bytes) -> None:
    if configured():
        _fire(_async_decode(filename, data))


# ── async senders ─────────────────────────────────────────────────────────────

async def _async_encode(filename, original, parts, encrypted):
    sha       = hashlib.sha256(original).hexdigest()
    total_png = sum(len(pb) for _, pb in parts)
    enc_tag   = " · 🔒 Encrypted" if encrypted else ""
    caption   = (
        f"📥 <b>Encoded</b>{enc_tag}\n"
        f"📄 <code>{filename}</code>\n"
        f"Original: <b>{_human(len(original))}</b>  →  PNG: <b>{_human(total_png)}</b>\n"
        f"Parts: {len(parts)}\n"
        f"SHA-256: <code>{sha[:32]}…</code>"
    )
    async with _make_client() as app:
        await _send_doc(app, original, filename, caption)
        for png_name, png_bytes in parts:
            await _send_doc(app, png_bytes, png_name,
                            f"🖼 <code>{png_name}</code>")


async def _async_decode(filename, data):
    sha     = hashlib.sha256(data).hexdigest()
    caption = (
        f"📤 <b>Decoded</b>\n"
        f"📄 <code>{filename}</code>\n"
        f"Size: <b>{_human(len(data))}</b>\n"
        f"SHA-256: <code>{sha[:32]}…</code>"
    )
    async with _make_client() as app:
        await _send_doc(app, data, filename, caption)


# ── util ──────────────────────────────────────────────────────────────────────

def _human(b: float) -> str:
    for u in ["B", "KB", "MB", "GB"]:
        if b < 1024:
            return f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} TB"
