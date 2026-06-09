"""
Persistent activity log + file store for AirVault web.

Directory layout under LOG_DIR (default /tmp/airvault_logs):
  log.json               — ordered list of all operations (newest first)
  originals/<id>/        — original file uploaded for encoding
  encoded/<id>/          — PNG(s) produced by encoding
  decoded/<id>/          — file recovered by decoding

Set the LOG_DIR environment variable to a Render Disk mount path (e.g. /data)
to make files survive server restarts.
"""

import hashlib
import json
import os
import shutil
import time
from pathlib import Path


def _root() -> Path:
    d = Path(os.environ.get("LOG_DIR", "/tmp/airvault_logs"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _log_path() -> Path:
    return _root() / "log.json"


def _read() -> list:
    lf = _log_path()
    if lf.exists():
        try:
            return json.loads(lf.read_text("utf-8"))
        except Exception:
            pass
    return []


def _write(entries: list) -> None:
    _log_path().write_text(json.dumps(entries, indent=2), encoding="utf-8")


# ── public write API ────────────────────────────────────────────────────────

def log_encode(filename: str, original_bytes: bytes,
               png_parts: list, encrypted: bool = False,
               save_files: bool = True) -> None:
    """Log an encode operation.
    save_files=False records metadata only (no bytes written to disk).
    """
    ts   = int(time.time() * 1000)
    root = _root()

    enc_names = [name for name, _ in png_parts]

    if save_files:
        orig_dir = root / "originals" / str(ts)
        orig_dir.mkdir(parents=True, exist_ok=True)
        (orig_dir / filename).write_bytes(original_bytes)

        enc_dir = root / "encoded" / str(ts)
        enc_dir.mkdir(parents=True, exist_ok=True)
        for png_name, png_bytes in png_parts:
            (enc_dir / png_name).write_bytes(png_bytes)

    total_png_size = sum(len(pb) for _, pb in png_parts)

    entries = _read()
    entries.insert(0, {
        "id":            ts,
        "ts":            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts / 1000)),
        "op":            "encode",
        "filename":      filename,
        "original_size": len(original_bytes),
        "png_size":      total_png_size,
        "parts":         len(png_parts),
        "encoded_names": enc_names,
        "encrypted":     encrypted,
        "sha256":        hashlib.sha256(original_bytes).hexdigest(),
        "files_saved":   save_files,
    })
    _write(entries)


def log_decode(filename: str, file_bytes: bytes,
               save_files: bool = True) -> None:
    """Log a decode operation.
    save_files=False records metadata only (no bytes written to disk).
    """
    ts   = int(time.time() * 1000)
    root = _root()

    if save_files:
        dec_dir = root / "decoded" / str(ts)
        dec_dir.mkdir(parents=True, exist_ok=True)
        (dec_dir / filename).write_bytes(file_bytes)

    entries = _read()
    entries.insert(0, {
        "id":          ts,
        "ts":          time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts / 1000)),
        "op":          "decode",
        "filename":    filename,
        "file_size":   len(file_bytes),
        "sha256":      hashlib.sha256(file_bytes).hexdigest(),
        "files_saved": save_files,
    })
    _write(entries)


# ── public read API ─────────────────────────────────────────────────────────

def get_entries() -> list:
    return _read()


def get_file(entry_id: int, subdir: str, filename: str):
    """Return a Path to the stored file, or None if not found."""
    p = _root() / subdir / str(entry_id) / os.path.basename(filename)
    return p if p.exists() else None


def stats() -> dict:
    entries = _read()
    enc  = [e for e in entries if e["op"] == "encode"]
    dec  = [e for e in entries if e["op"] == "decode"]
    used = sum(e.get("original_size", 0) + e.get("png_size", 0)
               for e in enc)
    used += sum(e.get("file_size", 0) for e in dec)
    return {
        "total":    len(entries),
        "encodes":  len(enc),
        "decodes":  len(dec),
        "bytes":    used,
    }


def clear_all() -> None:
    """Delete every saved file and the log."""
    root = _root()
    for sub in ("originals", "encoded", "decoded"):
        d = root / sub
        if d.exists():
            shutil.rmtree(d)
    lf = _log_path()
    if lf.exists():
        lf.unlink()
