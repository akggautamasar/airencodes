"""
Telegram channel integration for AirVault.
Set these two environment variables to activate:
  TELEGRAM_BOT_TOKEN   — token from @BotFather
  TELEGRAM_CHANNEL_ID  — e.g. @mychannel  or  -100xxxxxxxxxx

Files > 49 MB cannot be sent via the Bot API; a text notice is posted instead.
All sends run in daemon threads so they never delay the HTTP response.
"""

import hashlib
import os
import threading

import requests

_MAX_BYTES = 49 * 1024 * 1024   # Telegram Bot API hard limit


def configured() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and
                os.environ.get("TELEGRAM_CHANNEL_ID"))


def _token() -> str:
    return os.environ.get("TELEGRAM_BOT_TOKEN", "")

def _chat() -> str:
    return os.environ.get("TELEGRAM_CHANNEL_ID", "")


def _api(method: str, **kwargs):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{_token()}/{method}",
            timeout=60,
            **kwargs,
        )
        j = r.json()
        if not j.get("ok"):
            print(f"[telegram] {method}: {j.get('description')}", flush=True)
    except Exception as exc:
        print(f"[telegram] {method} exception: {exc}", flush=True)


def _msg(text: str):
    _api("sendMessage", data={
        "chat_id":    _chat(),
        "text":       text[:4096],
        "parse_mode": "HTML",
    })


def _doc(data: bytes, filename: str, caption: str = ""):
    if len(data) > _MAX_BYTES:
        _msg(caption + f"\n\n⚠️ File too large for Telegram "
             f"({_human(len(data))} &gt; 49 MB) — not uploaded.")
        return
    _api("sendDocument", data={
        "chat_id":    _chat(),
        "caption":    caption[:1024],
        "parse_mode": "HTML",
    }, files={
        "document": (filename, data, "application/octet-stream"),
    })


def _bg(fn, *args, **kwargs):
    """Fire-and-forget in a daemon thread."""
    threading.Thread(target=fn, args=args, kwargs=kwargs, daemon=True).start()


# ── public API ────────────────────────────────────────────────────────────────

def send_encode(filename: str, original: bytes,
                parts: list, encrypted: bool) -> None:
    if configured():
        _bg(_do_encode, filename, original, parts, encrypted)


def send_decode(filename: str, data: bytes) -> None:
    if configured():
        _bg(_do_decode, filename, data)


# ── internal senders (run in background threads) ──────────────────────────────

def _do_encode(filename, original, parts, encrypted):
    sha       = hashlib.sha256(original).hexdigest()
    total_png = sum(len(pb) for _, pb in parts)
    enc_tag   = " · 🔒 Encrypted" if encrypted else ""

    caption = (
        f"📥 <b>Encoded</b>{enc_tag}\n"
        f"📄 <code>{filename}</code>\n"
        f"Original: <b>{_human(len(original))}</b>  →  PNG: <b>{_human(total_png)}</b>\n"
        f"Parts: {len(parts)}\n"
        f"SHA-256: <code>{sha[:32]}…</code>"
    )
    _doc(original, filename, caption)

    for png_name, png_bytes in parts:
        _doc(png_bytes, png_name,
             caption=f"🖼 <code>{png_name}</code>")


def _do_decode(filename, data):
    sha = hashlib.sha256(data).hexdigest()
    caption = (
        f"📤 <b>Decoded</b>\n"
        f"📄 <code>{filename}</code>\n"
        f"Size: <b>{_human(len(data))}</b>\n"
        f"SHA-256: <code>{sha[:32]}…</code>"
    )
    _doc(data, filename, caption)


def _human(b: float) -> str:
    for u in ["B", "KB", "MB", "GB"]:
        if b < 1024:
            return f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} TB"
