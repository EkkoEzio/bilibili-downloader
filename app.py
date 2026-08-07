"""Flask 路由层。薄,业务委托给 bilibili_auth / downloader。"""
import time
import json
from flask import Flask, request, jsonify, render_template, Response, stream_with_context

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


@app.route("/api/login/qrcode/poll")
def login_qrcode_poll():
    key = request.args.get("key", "")
    r = auth.qrcode_poll(key)
    if r["status"] == "success" and r["sessdata"]:
        auth.save_session(r["sessdata"])
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
        return jsonify({"ok": True, "info": downloader.parse_video(url)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/download", methods=["POST"])
def download():
    body = request.get_json(silent=True) or {}
    url = body.get("url", "").strip()
    label = body.get("label", "")
    if not url:
        return jsonify({"ok": False, "error": "请输入 URL"}), 400
    try:
        tid = downloader.start_download(url, label)
        return jsonify({"ok": True, "task_id": tid})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


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


if __name__ == "__main__":
    print(f"\n  浏览器打开: http://localhost:{PORT}\n")
    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True)
