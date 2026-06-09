import io
import os
import sys
import zipfile

from flask import Flask, jsonify, render_template, request, send_file

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import airvault_core as av

TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "templates")
app = Flask(__name__, template_folder=TEMPLATES_DIR)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500 MB hard cap


# ── routes ──────────────────────────────────────────────────────────────────

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
    password = request.form.get("password") or None   # None when field is empty

    if len(data) == 0:
        return jsonify({"error": "Uploaded file is empty"}), 400

    try:
        parts = av.encode(data, filename, password=password)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    if len(parts) == 1:
        png_name, png_bytes = parts[0]
        return send_file(
            io.BytesIO(png_bytes),
            mimetype="image/png",
            as_attachment=True,
            download_name=png_name,
        )

    # multiple parts → zip
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
        msg = str(e)
        # tell the client whether it was specifically a password issue
        pw_issue = "password" in msg.lower() or "wrong password" in msg.lower()
        return jsonify({"error": msg, "password_required": pw_issue}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return send_file(
        io.BytesIO(file_bytes),
        mimetype=_guess_mime(filename),
        as_attachment=True,
        download_name=filename,
    )


# ── helpers ─────────────────────────────────────────────────────────────────

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
