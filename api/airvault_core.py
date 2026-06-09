"""
AirVault core logic — adapted for in-memory (bytes ↔ BytesIO) use.
All public functions accept/return bytes, never touch the filesystem.
"""

import io, struct, hashlib, math
import numpy as np
from PIL import Image, ImageDraw, ImageFont

MAGIC = b"AVLT"
VERSION = 2
DEFAULT_CHUNK_MB = 33


# ── header ─────────────────────────────────────────────────────────────────

def build_header(mode, filename, part_no, part_total, chunk_bytes, file_sha):
    name = filename.encode("utf-8")
    chunk_sha = hashlib.sha256(chunk_bytes).digest()
    h = bytearray()
    h += MAGIC
    h += struct.pack(">B", VERSION)
    h += struct.pack(">B", mode)
    h += struct.pack(">H", len(name))
    h += name
    h += struct.pack(">I", part_no)
    h += struct.pack(">I", part_total)
    h += struct.pack(">Q", len(chunk_bytes))
    h += chunk_sha
    h += file_sha
    return bytes(h)


def parse_header(data):
    if len(data) < 8 or data[:4] != MAGIC:
        raise ValueError("Not an AirVault image (bad magic)")
    try:
        off = 4
        version = data[off]; off += 1
        mode    = data[off]; off += 1
        name_len = struct.unpack(">H", data[off:off+2])[0]; off += 2
        filename = data[off:off+name_len].decode("utf-8"); off += name_len
        part_no    = struct.unpack(">I", data[off:off+4])[0]; off += 4
        part_total = struct.unpack(">I", data[off:off+4])[0]; off += 4
        chunk_len  = struct.unpack(">Q", data[off:off+8])[0]; off += 8
        chunk_sha  = data[off:off+32]; off += 32
        file_sha   = data[off:off+32]; off += 32
        payload    = data[off:off+chunk_len]
    except (struct.error, UnicodeDecodeError, IndexError) as e:
        raise ValueError(f"corrupted header ({e})")
    return {
        "version": version, "mode": mode, "filename": filename,
        "part_no": part_no, "part_total": part_total,
        "chunk_len": chunk_len, "chunk_sha": chunk_sha,
        "file_sha": file_sha, "payload": payload,
    }


# ── noise mode ─────────────────────────────────────────────────────────────

def bytes_to_noise_png(blob: bytes) -> bytes:
    n = len(blob)
    px = math.ceil(n / 3)
    side = math.ceil(math.sqrt(px))
    total = side * side * 3
    padded = blob + b"\x00" * (total - n)
    img = Image.frombytes("RGB", (side, side), padded)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=False)
    return buf.getvalue()


def noise_png_to_bytes(png_bytes: bytes) -> bytes:
    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    return img.tobytes()


# ── label mode ─────────────────────────────────────────────────────────────

def bytes_to_label_png(blob: bytes, label_text: str) -> bytes:
    """Hide blob in LSBs of a gradient image, then draw label_text on top."""
    need_bits = len(blob) * 8
    min_px = math.ceil(need_bits / 3)
    side = max(640, math.ceil(math.sqrt(min_px)) + 1)

    # gradient cover — vectorised via numpy
    y = np.arange(side, dtype=np.float32)[:, None]
    x = np.arange(side, dtype=np.float32)[None, :]
    r = np.clip(20 + 60 * y / side, 0, 255).astype(np.uint8)
    g = ((15 + 30 * y / side + x / 8) % 256).astype(np.uint8)
    b = np.clip(40 + 80 * y / side, 0, 255).astype(np.uint8)
    pixels = np.stack([np.broadcast_to(r, (side, side)),
                       g,
                       np.broadcast_to(b, (side, side))], axis=-1).copy()

    # embed bits into LSBs — vectorised
    bits = np.unpackbits(np.frombuffer(blob, dtype=np.uint8))
    flat = pixels.ravel()
    flat[:len(bits)] = (flat[:len(bits)] & np.uint8(0xFE)) | bits
    pixels = flat.reshape(side, side, 3)
    img = Image.fromarray(pixels, "RGB")

    # draw visible label without touching data LSBs
    label_layer = img.copy()
    draw = ImageDraw.Draw(label_layer)

    def _font(sz):
        for fp in ("DejaVuSans-Bold.ttf", "/system/fonts/Roboto-Bold.ttf"):
            try:
                return ImageFont.truetype(fp, sz)
            except Exception:
                pass
        return ImageFont.load_default()

    def _tw(font):
        try:
            b = draw.textbbox((0, 0), label_text, font=font)
            return b[2] - b[0], b[3] - b[1]
        except Exception:
            return int(draw.textlength(label_text, font=font)), font.size

    size = max(28, side // 12)
    font = _font(size)
    tw, th = _tw(font)
    while tw > int(side * 0.85) and size > 12:
        size -= 2
        font = _font(size)
        tw, th = _tw(font)

    cx, cy = (side - tw) // 2, (side - th) // 2
    pad = size // 2
    draw.rectangle([cx - pad, cy - pad, cx + tw + pad, cy + th + pad], fill=(0, 0, 0))
    draw.text((cx, cy), label_text, fill=(255, 255, 255), font=font)

    # merge: keep LSB (data) from img, take high 7 bits from label layer
    base = np.frombuffer(img.tobytes(), dtype=np.uint8).copy()
    lab  = np.frombuffer(label_layer.tobytes(), dtype=np.uint8)
    merged = (lab & np.uint8(0xFE)) | (base & np.uint8(1))
    img = Image.fromarray(merged.reshape(side, side, 3), "RGB")

    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=False)
    return buf.getvalue()


def label_png_to_bits(png_bytes: bytes, n_bytes: int) -> bytes:
    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    arr = np.frombuffer(img.tobytes(), dtype=np.uint8)
    need = n_bytes * 8
    bits = (arr[:need] & np.uint8(1)).astype(np.uint8)
    return np.packbits(bits).tobytes()[:n_bytes]


# ── read one PNG ────────────────────────────────────────────────────────────

def read_one(png_bytes: bytes) -> dict:
    """Parse a single AirVault PNG. Raises ValueError on any problem."""
    raw = noise_png_to_bytes(png_bytes)
    if raw[:4] == MAGIC:
        info = parse_header(raw)
        if hashlib.sha256(info["payload"]).digest() != info["chunk_sha"]:
            raise ValueError("chunk checksum FAILED — image is corrupted")
        return info

    # try label mode
    peek = label_png_to_bits(png_bytes, 4096)
    if peek[:4] != MAGIC:
        raise ValueError("not an AirVault image")
    tmp = parse_header(peek + b"\x00" * (1 << 20))
    name_len = len(tmp["filename"].encode("utf-8"))
    header_size = 4 + 1 + 1 + 2 + name_len + 4 + 4 + 8 + 32 + 32
    total_needed = header_size + tmp["chunk_len"]
    full = label_png_to_bits(png_bytes, total_needed)
    info = parse_header(full)
    if hashlib.sha256(info["payload"]).digest() != info["chunk_sha"]:
        raise ValueError("chunk checksum FAILED — image is corrupted")
    return info


# ── public API ──────────────────────────────────────────────────────────────

def encode(data: bytes, filename: str,
           chunk_mb: int = DEFAULT_CHUNK_MB,
           force_noise: bool = False,
           label_threshold_mb: int = 4) -> list:
    """
    Encode file data into one or more PNG images.
    Returns list of (png_filename, png_bytes).
    """
    file_sha = hashlib.sha256(data).digest()
    total_len = len(data)
    chunk_size = chunk_mb * 1024 * 1024
    part_total = max(1, math.ceil(total_len / chunk_size))

    use_label = (not force_noise and
                 part_total == 1 and
                 total_len <= label_threshold_mb * 1024 * 1024)
    mode = 1 if use_label else 0

    outputs = []
    for i in range(part_total):
        chunk = data[i * chunk_size:(i + 1) * chunk_size]
        header = build_header(mode, filename, i + 1, part_total, chunk, file_sha)
        blob = header + chunk

        out_name = (f"{filename}.avlt.png" if part_total == 1
                    else f"{filename}.part{i+1}of{part_total}.avlt.png")

        if mode == 1:
            png = bytes_to_label_png(blob, f"{filename}  ·  {_human(total_len)}")
        else:
            png = bytes_to_noise_png(blob)

        outputs.append((out_name, png))

    return outputs


def decode_from_pngs(png_list: list) -> tuple:
    """
    Decode one or more AirVault PNGs back to the original file.
    Returns (original_filename, file_bytes).
    Raises ValueError on any problem.
    """
    groups = {}
    for png_bytes in png_list:
        info = read_one(png_bytes)
        key = (info["filename"], bytes(info["file_sha"]))
        g = groups.setdefault(key, {
            "parts": {}, "total": info["part_total"], "sha": info["file_sha"]
        })
        g["parts"][info["part_no"]] = info["payload"]

    if not groups:
        raise ValueError("No valid AirVault images found")

    for (filename, _), g in groups.items():
        missing = [i for i in range(1, g["total"] + 1) if i not in g["parts"]]
        if missing:
            raise ValueError(
                f"Missing part(s) {missing} of {g['total']} for '{filename}'. "
                "Upload all parts together."
            )
        data = b"".join(g["parts"][i] for i in range(1, g["total"] + 1))
        if hashlib.sha256(data).digest() != bytes(g["sha"]):
            raise ValueError(f"Full-file checksum mismatch for '{filename}' — file is corrupted")
        return filename, data

    raise ValueError("No complete file found in the provided images")


def _human(b):
    b = float(b)
    for u in ["B", "KB", "MB", "GB", "TB"]:
        if b < 1024:
            return f"{b:.0f}{u}" if u == "B" else f"{b:.2f}{u}"
        b /= 1024
    return f"{b:.2f}PB"
