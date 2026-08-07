"""Flask 路由层。薄,业务委托给 bilibili_auth / downloader。"""
import io
import time
import json
import subprocess
from flask import Flask, request, jsonify, render_template, Response, stream_with_context

import qrcode
import bilibili_auth as auth
import downloader
from config import PORT, COOKIE_FILE, DOWNLOAD_DIR, BROWSER_COOKIE_SOURCE

app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html")


# ---------- 登录 ----------
@app.route("/api/login/status")
def login_status():
    return jsonify(auth.get_login_status())


@app.route("/api/login/qrcode")
def login_qrcode():
    try:
        return jsonify({"ok": True, **auth.qrcode_generate()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/login/qrcode-image")
def login_qrcode_image():
    """本地用 qrcode 库把扫码 URL 画成 PNG,不依赖第三方服务。"""
    data = auth.qrcode_generate()
    img = qrcode.make(data["url"])
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return Response(buf.getvalue(), mimetype="image/png",
                    headers={"Cache-Control": "no-store"})


@app.route("/api/login/qrcode/poll")
def login_qrcode_poll():
    key = request.args.get("key", "")
    r = auth.qrcode_poll(key)
    if r["status"] == "success" and r["sessdata"]:
        auth.save_session(r["sessdata"], r.get("dede_userid", ""), r.get("bili_jct", ""))
    return jsonify(r)


@app.route("/api/login/use-browser", methods=["POST"])
def login_use_browser():
    auth.clear_session()
    return jsonify({"ok": True, "mode": BROWSER_COOKIE_SOURCE})


@app.route("/api/login/cookie-txt", methods=["POST"])
def login_cookie_txt():
    text = request.get_data(as_text=True)
    if "SESSDATA" not in text:
        return jsonify({"ok": False, "error": "未找到 SESSDATA"}), 400
    COOKIE_FILE.write_text(text, encoding="utf-8")
    auth.clear_session()  # 切到 cookies.txt 模式
    return jsonify({"ok": True})


@app.route("/api/login/logout", methods=["POST"])
def login_logout():
    auth.clear_session()
    if COOKIE_FILE.exists():
        COOKIE_FILE.unlink()
    return jsonify({"ok": True})


# ---------- 解析/下载 ----------
@app.route("/api/parse", methods=["POST"])
def parse():
    url = (request.get_json(silent=True) or {}).get("url", "").strip()
    if not url:
        return jsonify({"ok": False, "error": "请输入 URL"}), 400
    try:
        info = downloader.parse_video(url)
        # 附带登录态信息,前端据此提示"未登录仅公开画质"
        status = auth.get_login_status()
        return jsonify({"ok": True, "info": info, "logged_in": status.get("logged_in", False)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/parse-part", methods=["POST"])
def parse_part():
    """多 P 视频选了具体分 P 后,拉该 P 的清晰度。"""
    body = request.get_json(silent=True) or {}
    url = body.get("url", "").strip()
    page = int(body.get("page", 1))
    if not url:
        return jsonify({"ok": False, "error": "请输入 URL"}), 400
    try:
        return jsonify({"ok": True, "info": downloader.parse_part(url, page)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/download", methods=["POST"])
def download():
    body = request.get_json(silent=True) or {}
    url = body.get("url", "").strip()
    label = body.get("label", "")
    page = int(body.get("page", 1))
    if not url:
        return jsonify({"ok": False, "error": "请输入 URL"}), 400
    # 重复下载拦截:同 URL+清晰度 且仍在跑
    existing = downloader.find_active_by_url(url, label)
    if existing:
        return jsonify({"ok": False, "error": "该视频相同清晰度正在下载中", "task_id": existing}), 409
    try:
        tid = downloader.start_download(url, label, page)
        return jsonify({"ok": True, "task_id": tid})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/cancel/<task_id>", methods=["POST"])
def cancel(task_id):
    ok = downloader.cancel_download(task_id)
    return jsonify({"ok": ok})


@app.route("/api/progress/<task_id>")
def progress(task_id):
    """SSE 推送单个任务进度,完成/失败后关闭。"""
    def gen():
        while True:
            t = downloader.get_task(task_id)
            if not t:
                yield "event: end\ndata: {}\n\n"
                break
            yield f"data: {json.dumps(t, ensure_ascii=False)}\n\n"
            if t["status"] in ("done", "failed"):
                break
            time.sleep(1)
    return Response(stream_with_context(gen()), mimetype="text/event-stream")


@app.route("/api/tasks")
def tasks():
    return jsonify({"ok": True, "tasks": downloader.list_tasks()})


@app.route("/api/config")
def get_config():
    return jsonify({"ok": True, "download_dir": str(DOWNLOAD_DIR)})


@app.route("/api/open-folder", methods=["POST"])
def open_folder():
    """在 Finder 中打开下载目录。"""
    try:
        subprocess.run(["open", str(DOWNLOAD_DIR)], check=False)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    print(f"\n  浏览器打开: http://localhost:{PORT}\n")
    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True)
