import io
import os
import sys
import zipfile

from flask import Flask, jsonify, render_template, request, send_file

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import airvault_core as av
import log_store
import telegram as tg

TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "templates")
app = Flask(__name__, template_folder=TEMPLATES_DIR)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024


def _human_size(b: int) -> str:
    b = float(b)
    for u in ["B", "KB", "MB", "GB", "TB"]:
        if b < 1024:
            return f"{b:.0f} {u}" if u == "B" else f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} PB"


@app.context_processor
def _ctx():
    return {
        "human": _human_size,
        "log_dir": os.environ.get("LOG_DIR", "/tmp/airvault_logs"),
        "tg_configured": tg.configured(),
    }


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/encode", methods=["POST"])
def encode_route():
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "No file uploaded"}), 400

    data = f.read()
    filename = os.path.basename(f.filename)
    password = request.form.get("password") or None
    if len(data) == 0:
        return jsonify({"error": "Uploaded file is empty"}), 400

    try:
        parts = av.encode(data, filename, password=password)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    encrypted = bool(password)
    try:
        log_store.log_encode(filename, data, parts,
                             encrypted=encrypted,
                             save_files=not tg.configured())
    except Exception:
        pass
    tg.send_encode(filename, data, parts, encrypted=encrypted)

    if len(parts) == 1:
        out_name, out_bytes = parts[0]
        mimetype = "application/octet-stream"
    else:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
            for png_name, png_bytes in parts:
                zf.writestr(png_name, png_bytes)
        out_bytes = buf.getvalue()
        out_name = f"{filename}.airvault.zip"
        mimetype = "application/zip"

    slug = None
    try:
        slug = log_store.create_share(out_name, out_bytes)
    except Exception:
        pass

    resp = send_file(io.BytesIO(out_bytes), mimetype=mimetype,
                     as_attachment=True, download_name=out_name)
    if slug:
        resp.headers["X-Share-Slug"] = slug
        resp.headers["X-Share-Name"] = out_name
    return resp


def _decode_shared_bytes(data: bytes, filename: str, password: str = None):
    """Decode the exact encoded bytes stored behind a share link."""
    if filename.lower().endswith(".zip") or data[:4] == b"PK\x03\x04":
        try:
            with zipfile.ZipFile(io.BytesIO(data), "r") as zf:
                names = [n for n in zf.namelist()
                         if not n.endswith("/") and n.lower().endswith(".avlt")]
                if not names:
                    raise ValueError("Share archive contains no AirVault parts")
                png_list = [zf.read(n) for n in sorted(names)]
        except zipfile.BadZipFile:
            raise ValueError("Shared archive is corrupted")
    else:
        png_list = [data]
    return av.decode_from_pngs(png_list, password=password)


@app.route("/decode", methods=["POST"])
def decode_route():
    files = request.files.getlist("files")
    password = request.form.get("password") or None
    png_list = [f.read() for f in files if f.filename]
    if not png_list:
        return jsonify({"error": "No PNG files uploaded"}), 400

    try:
        filename, file_bytes = av.decode_from_pngs(png_list, password=password)
    except ValueError as e:
        msg = str(e)
        return jsonify({"error": msg, "password_required": "password" in msg.lower()}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    try:
        log_store.log_decode(filename, file_bytes,
                             save_files=not tg.configured())
    except Exception:
        pass
    tg.send_decode(filename, file_bytes)
    return send_file(io.BytesIO(file_bytes), mimetype=_guess_mime(filename),
                     as_attachment=True, download_name=filename)


# ── share links ─────────────────────────────────────────────────────────────

@app.route("/s/<slug>")
def share_download(slug):
    filename, data = log_store.get_share(slug)
    if data is None:
        return ("This link has expired or doesn't exist. Share links last 7 days "
                "(and only survive a server restart if LOG_DIR points to persistent disk).", 404)

    # ?download=1 preserves the old behaviour for clients that need the raw
    # encoded .avlt/.zip. The normal copied share link opens the decoder UI.
    if request.args.get("download") == "1":
        mimetype = "application/zip" if filename.endswith(".zip") else "application/octet-stream"
        return send_file(io.BytesIO(data), mimetype=mimetype,
                         as_attachment=True, download_name=filename)

    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AirVault — Decode shared file</title>
<style>
*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;background:#08081a;color:#e0e0ff;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;display:grid;place-items:center;padding:20px}}
.card{{width:min(560px,100%);background:#16163a;border:1px solid #2a2a55;border-radius:22px;padding:30px;box-shadow:0 20px 60px #0006}}
h1{{margin:0 0 8px;font-size:1.65rem}}p{{color:#8888b8;line-height:1.55}}.file{{margin:20px 0;padding:15px;background:#111128;border:1px solid #2a2a55;border-radius:12px;word-break:break-word}}
input{{width:100%;padding:13px 14px;background:#111128;color:#e0e0ff;border:1px solid #2a2a55;border-radius:10px;font-size:1rem;outline:none}}input:focus{{border-color:#6366f1}}
button{{width:100%;margin-top:14px;padding:14px;border:0;border-radius:11px;background:linear-gradient(135deg,#06b6d4,#0284c7);color:white;font-weight:800;font-size:1rem;cursor:pointer}}button:disabled{{opacity:.55;cursor:wait}}
.msg{{margin-top:14px;padding:12px;border-radius:10px;display:none;line-height:1.45}}.err{{display:block;background:#ef44441a;border:1px solid #ef444455;color:#ff7777}}.ok{{display:block;background:#22c55e1a;border:1px solid #22c55e55;color:#5ee68a}}
a{{color:#22d3ee}}
</style></head><body><main class="card">
<h1>🔓 Decode shared AirVault file</h1>
<p>This link contains the AirVault encoded data. Enter the password if required, then decode and download the verified original file.</p>
<div class="file">📦 <strong>{_html_escape(filename)}</strong><br><small>Share link expires after 7 days.</small></div>
<form id="f"><input id="pw" type="password" placeholder="Password (only if encrypted)" autocomplete="current-password"><button id="b">🔓 Decode & download original</button></form>
<div id="m" class="msg"></div>
<p><a href="/">← Back to AirVault</a></p>
<script>
const form=document.getElementById('f'),pw=document.getElementById('pw'),b=document.getElementById('b'),m=document.getElementById('m');
form.addEventListener('submit',async e=>{{e.preventDefault();b.disabled=true;m.className='msg';m.textContent='Decoding and verifying…';m.style.display='block';
const fd=new FormData();if(pw.value)fd.append('password',pw.value);
try{{const r=await fetch(location.pathname+'/decode',{{method:'POST',body:fd}});if(!r.ok){{const j=await r.json().catch(()=>({{error:'Server error'}}));m.className='msg err';m.textContent='✗ '+j.error;return;}}
const blob=await r.blob();const cd=r.headers.get('Content-Disposition')||'';const mm=cd.match(/filename="?([^";]+)"?/i);const name=mm?mm[1]:'decoded_file';const u=URL.createObjectURL(blob);const a=document.createElement('a');a.href=u;a.download=name;document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(u),60000);m.className='msg ok';m.textContent='✓ Decoded, SHA-256 verified, and download started.';}}catch(x){{m.className='msg err';m.textContent='✗ Network error: '+x.message;}}finally{{b.disabled=false;}}}});
</script></main></body></html>'''


@app.route("/s/<slug>/decode", methods=["POST"])
def decode_share(slug):
    filename, data = log_store.get_share(slug)
    if data is None:
        return jsonify({"error": "This share link has expired or doesn't exist."}), 404

    password = request.form.get("password") or None
    try:
        original_name, file_bytes = _decode_shared_bytes(data, filename, password=password)
    except ValueError as e:
        msg = str(e)
        return jsonify({"error": msg, "password_required": "password" in msg.lower()}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    try:
        log_store.log_decode(original_name, file_bytes,
                             save_files=not tg.configured())
    except Exception:
        pass
    tg.send_decode(original_name, file_bytes)
    return send_file(io.BytesIO(file_bytes), mimetype=_guess_mime(original_name),
                     as_attachment=True, download_name=original_name)


def _html_escape(value: str) -> str:
    return (value.replace("&", "&amp;").replace("<", "&lt;")
                 .replace(">", "&gt;").replace('"', "&quot;")
                 .replace("'", "&#39;"))


@app.route("/logs")
def logs_page():
    return render_template("logs.html", entries=log_store.get_entries(), stats=log_store.stats())


@app.route("/logs/download/<int:entry_id>/<subdir>/<filename>")
def logs_download(entry_id, subdir, filename):
    if subdir not in ("originals", "encoded", "decoded"):
        return "Not found", 404
    filename = os.path.basename(filename)
    p = log_store.get_file(entry_id, subdir, filename)
    if p is None:
        return ("File not found — server may have restarted. Set LOG_DIR to a persistent path to keep files.", 404)
    return send_file(str(p), mimetype=_guess_mime(filename), as_attachment=True, download_name=filename)


@app.route("/logs/clear", methods=["POST"])
def logs_clear():
    try:
        log_store.clear_all()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── helpers ───────────────────────────────────────────────────────────────────
def _guess_mime(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return {
        "pdf": "application/pdf",
        "zip": "application/zip",
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "gif": "image/gif",
        "mp4": "video/mp4",
        "mp3": "audio/mpeg",
        "txt": "text/plain",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }.get(ext, "application/octet-stream")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
