import io
import os
import sys
import zipfile

from flask import Flask, jsonify, render_template, request, send_file

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import airvault_core as av
import log_store

TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "templates")
app = Flask(__name__, template_folder=TEMPLATES_DIR)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500 MB hard cap


def _human_size(b: int) -> str:
    b = float(b)
    for u in ["B", "KB", "MB", "GB", "TB"]:
        if b < 1024:
            return f"{b:.0f} {u}" if u == "B" else f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} PB"


# make human() available in every Jinja2 template
app.jinja_env.globals["human"]   = _human_size
app.jinja_env.globals["log_dir"] = os.environ.get("LOG_DIR", "/tmp/airvault_logs")


# ── main app ─────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/encode", methods=["POST"])
def encode_route():
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "No file uploaded"}), 400

    data     = f.read()
    filename = os.path.basename(f.filename)
    password = request.form.get("password") or None

    if len(data) == 0:
        return jsonify({"error": "Uploaded file is empty"}), 400

    try:
        parts = av.encode(data, filename, password=password)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    # save to log
    try:
        log_store.log_encode(filename, data, parts, encrypted=bool(password))
    except Exception:
        pass  # logging failure must never break the encode response

    if len(parts) == 1:
        png_name, png_bytes = parts[0]
        return send_file(
            io.BytesIO(png_bytes),
            mimetype="image/png",
            as_attachment=True,
            download_name=png_name,
        )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for png_name, png_bytes in parts:
            zf.writestr(png_name, png_bytes)
    buf.seek(0)
    return send_file(
        buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"{filename}.airvault.zip",
    )


@app.route("/decode", methods=["POST"])
def decode_route():
    files    = request.files.getlist("files")
    password = request.form.get("password") or None
    png_list = [f.read() for f in files if f.filename]

    if not png_list:
        return jsonify({"error": "No PNG files uploaded"}), 400

    try:
        filename, file_bytes = av.decode_from_pngs(png_list, password=password)
    except ValueError as e:
        msg      = str(e)
        pw_issue = "password" in msg.lower()
        return jsonify({"error": msg, "password_required": pw_issue}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    try:
        log_store.log_decode(filename, file_bytes)
    except Exception:
        pass

    return send_file(
        io.BytesIO(file_bytes),
        mimetype=_guess_mime(filename),
        as_attachment=True,
        download_name=filename,
    )


# ── logs ──────────────────────────────────────────────────────────────────────

@app.route("/logs")
def logs_page():
    return render_template("logs.html",
                           entries=log_store.get_entries(),
                           stats=log_store.stats())


@app.route("/logs/download/<int:entry_id>/<subdir>/<filename>")
def logs_download(entry_id, subdir, filename):
    if subdir not in ("originals", "encoded", "decoded"):
        return "Not found", 404
    filename = os.path.basename(filename)   # prevent path traversal
    p = log_store.get_file(entry_id, subdir, filename)
    if p is None:
        return "File not found (server may have restarted — set LOG_DIR to a persistent path)", 404
    mime = _guess_mime(filename)
    return send_file(str(p), mimetype=mime, as_attachment=True, download_name=filename)


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
        "pdf":  "application/pdf",
        "zip":  "application/zip",
        "png":  "image/png",
        "jpg":  "image/jpeg",
        "jpeg": "image/jpeg",
        "gif":  "image/gif",
        "mp4":  "video/mp4",
        "mp3":  "audio/mpeg",
        "txt":  "text/plain",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }.get(ext, "application/octet-stream")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
