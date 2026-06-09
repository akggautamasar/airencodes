"""
Telegram MTProto integration via Pyrogram.
Supports files up to 2 GB (vs 50 MB with the plain Bot API).

Pyrogram is imported lazily inside the background thread (after the event
loop is created) to avoid the "no current event loop" crash on Python 3.10+.

Required env vars:
  TELEGRAM_API_ID      — integer,  from https://my.telegram.org/apps
  TELEGRAM_API_HASH    — string,   from https://my.telegram.org/apps
  TELEGRAM_BOT_TOKEN   — string,   from @BotFather
  TELEGRAM_CHANNEL_ID  — @username  or  -100xxxx numeric ID

Session is stored at LOG_DIR/tg.session (persists across restarts when
LOG_DIR points to a Render Disk — avoids the "Peer id invalid" error on
numeric channel IDs by caching the access_hash between connections).
"""

import asyncio
import hashlib
import io
import os
import threading


def configured() -> bool:
    return bool(
        os.environ.get("TELEGRAM_API_ID")
        and os.environ.get("TELEGRAM_API_HASH")
        and os.environ.get("TELEGRAM_BOT_TOKEN")
        and os.environ.get("TELEGRAM_CHANNEL_ID")
    )


def _chat():
    v = os.environ.get("TELEGRAM_CHANNEL_ID", "")
    try:
        return int(v)
    except ValueError:
        return v


def _session_path() -> str:
    d = os.environ.get("LOG_DIR", "/tmp/airvault_logs")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "tg")   # Pyrogram appends .session automatically


def _make_client():
    from pyrogram import Client
    return Client(
        name=_session_path(),           # persistent file session (not in_memory)
        api_id=int(os.environ["TELEGRAM_API_ID"]),
        api_hash=os.environ["TELEGRAM_API_HASH"],
        bot_token=os.environ["TELEGRAM_BOT_TOKEN"],
    )


async def _warm_and_resolve(app):
    """
    Resolve the configured channel peer.

    • @username → always works via contacts.ResolveUsername
    • numeric ID → check peer cache first; if missing, invoke channels.GetChannels
      with access_hash=0 (Telegram accepts this for channels the bot is an admin of),
      manually store the real access_hash, then return the peer.

    get_dialogs() is a user-only method and raises BOT_METHOD_INVALID for bots.
    """
    from types import SimpleNamespace

    chat_id = _chat()

    # Username always resolves without peer cache
    if isinstance(chat_id, str):
        return await app.get_chat(chat_id)

    # Fast path: peer already cached (persistent session or second call this session)
    try:
        return await app.get_chat(chat_id)
    except Exception:
        pass

    # Numeric channel ID: use channels.GetChannels with access_hash=0.
    # Telegram accepts this for channels the bot is already a member/admin of.
    # Pyrogram's invoke() does NOT auto-cache raw responses, so we call
    # storage.update_peers() manually with the real access_hash from the response.
    from pyrogram import raw as raw_api

    channel_id_str = str(abs(chat_id))
    mtproto_id = int(channel_id_str[3:]) if channel_id_str.startswith("100") else abs(chat_id)

    result = await app.invoke(
        raw_api.functions.channels.GetChannels(
            id=[raw_api.types.InputChannel(channel_id=mtproto_id, access_hash=0)]
        )
    )

    if not result.chats:
        raise RuntimeError(
            f"Channel {chat_id} not found — make sure the bot is a channel admin."
        )

    ch = result.chats[0]
    ch_type = "channel" if getattr(ch, "broadcast", False) else "supergroup"

    await app.storage.update_peers([
        (ch.id, ch.access_hash, ch_type, getattr(ch, "username", None), None)
    ])

    return SimpleNamespace(id=ch.id)


async def _send_doc(app, peer_id, data: bytes, filename: str, caption: str = ""):
    from pyrogram.enums import ParseMode
    bio = io.BytesIO(data)
    bio.name = filename
    await app.send_document(
        chat_id=peer_id,
        document=bio,
        caption=caption[:1024],
        parse_mode=ParseMode.HTML,
    )


def _fire(coro) -> None:
    """Run *coro* in a daemon thread that owns its own event loop."""
    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)   # must be set BEFORE pyrogram is imported
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
        chat = await _warm_and_resolve(app)
        await _send_doc(app, chat.id, original, filename, caption)
        for png_name, png_bytes in parts:
            await _send_doc(app, chat.id, png_bytes, png_name,
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
        chat = await _warm_and_resolve(app)
        await _send_doc(app, chat.id, data, filename, caption)


def _human(b: float) -> str:
    for u in ["B", "KB", "MB", "GB"]:
        if b < 1024:
            return f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} TB"
