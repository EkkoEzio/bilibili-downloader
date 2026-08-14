"""Flask 路由层。薄,业务委托给 bilibili_auth / downloader。"""
import io
import logging
import re
import time
import json
import subprocess
from typing import Optional

from flask import Flask, request, jsonify, render_template, Response, stream_with_context

import qrcode
import auto_refresh
import up_list
import bilibili_auth as auth
import downloader
from config import PORT, COOKIE_FILE, BROWSER_COOKIE_SOURCE
import config  # DOWNLOAD_DIR 运行时可变,用 config.DOWNLOAD_DIR 实时读

# 日志:控制台 + data/app.log(排 B 站 412/风控/下载失败要看这里)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(config.DATA_DIR / "app.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("bd.app")

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
    # 重复下载拦截:同 URL+清晰度+分P 且仍在跑
    existing = downloader.find_active_by_url(url, label, page)
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


@app.route("/api/task/delete", methods=["POST"])
def task_delete():
    """删除任务。{ids:[...], delete_file:bool}"""
    body = request.get_json(silent=True) or {}
    ids = body.get("ids") or []
    delete_file = bool(body.get("delete_file"))
    if not ids:
        return jsonify({"ok": False, "error": "未选择任务"}), 400
    n = downloader.delete_tasks(ids, delete_file)
    return jsonify({"ok": True, "count": n})


@app.route("/api/tasks/clear", methods=["POST"])
def tasks_clear():
    """清除所有已完成/失败记录。{delete_file:bool}"""
    body = request.get_json(silent=True) or {}
    delete_file = bool(body.get("delete_file"))
    n = downloader.clear_tasks(delete_file)
    return jsonify({"ok": True, "count": n})


# ---------- 批量 ----------
@app.route("/api/batch/parse", methods=["POST"])
def batch_parse():
    """批量解析。接受:
    - urls: [多个视频链接]  → 逐个返回标题/时长
    - url: 单个收藏夹/合集/UP主页链接 → 返回里面所有视频
    返回统一的 {items:[{title,url,duration}]}
    """
    body = request.get_json(silent=True) or {}
    urls = body.get("urls")  # 多链接模式
    url = body.get("url", "").strip()  # 收藏夹/合集模式

    items = []
    if urls and isinstance(urls, list):
        # 多链接模式:逐个 flat 解析
        for u in urls:
            u = (u or "").strip()
            if not u:
                continue
            try:
                r = downloader.resolve_playlist(u)
                items.extend(r["items"])
            except Exception as e:
                items.append({"title": f"(解析失败: {str(e)[:40]})",
                              "url": u, "duration": 0, "error": str(e)[:80]})
    elif url:
        try:
            r = downloader.resolve_playlist(url)
            items = r["items"]
            title = r.get("title", "")
            return jsonify({"ok": True, "items": items, "title": title})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
    else:
        return jsonify({"ok": False, "error": "请提供 url 或 urls"}), 400

    return jsonify({"ok": True, "items": items})


@app.route("/api/batch/download", methods=["POST"])
def batch_download():
    """批量启动下载。{items:[{url,title,page}], label}"""
    body = request.get_json(silent=True) or {}
    items = body.get("items") or []
    label = body.get("label", "")
    if not items:
        return jsonify({"ok": False, "error": "没有待下载项"}), 400
    try:
        tids = downloader.start_batch(items, label)
        return jsonify({"ok": True, "task_ids": tids, "count": len(tids)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ---------- 内容浏览 ----------
@app.route("/api/history")
def history():
    """观看历史。?page=1&size=50"""
    page = int(request.args.get("page", 1))
    size = int(request.args.get("size", 50))
    try:
        items = auth.fetch_history(page, size)
        return jsonify({"ok": True, "items": items})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/follow-updates")
def follow_updates():
    """关注 UP 主最近投稿,合并按时间倒序。?force=true 跳过缓存强制刷新。"""
    try:
        items = auth.fetch_follow_updates(force=request.args.get("force") == "true")
        return jsonify({"ok": True, "items": items})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ---------- A3: 自定义 UP 清单 ----------

def _parse_mid_from_input(s: str) -> Optional[int]:
    """从主页 URL 或纯 mid 数字中提取 mid。支持:
       - https://space.bilibili.com/12345
       - 12345
    """
    s = (s or "").strip()
    m = re.search(r"space\.bilibili\.com/(\d+)", s)
    if m:
        return int(m.group(1))
    if s.isdigit():
        return int(s)
    return None


@app.route("/api/up-list")
def up_list_get():
    return jsonify({"ok": True, "ups": up_list.list_ups()})


@app.route("/api/up-list/add", methods=["POST"])
def up_list_add():
    """添加 UP。{input: 'https://space.bilibili.com/12345' 或 '12345'}"""
    body = request.get_json(silent=True) or {}
    mid = _parse_mid_from_input(body.get("input", ""))
    if not mid:
        return jsonify({"ok": False, "error": "请输入 B 站主页链接或 UP 的 mid"}), 400
    # 尝试从 arc/search 拿 uname/face 显示更友好
    items = auth.fetch_up_videos(mid, ps=1)
    uname = items[0]["uploader"] if items else ""
    face = items[0]["thumbnail"] if items else ""
    u = up_list.add_up(mid, uname, face)
    return jsonify({"ok": True, "up": u})


@app.route("/api/up-list/remove", methods=["POST"])
def up_list_remove():
    body = request.get_json(silent=True) or {}
    mid = body.get("mid")
    if mid is None:
        return jsonify({"ok": False, "error": "缺少 mid"}), 400
    ok = up_list.remove_up(int(mid))
    return jsonify({"ok": ok})


@app.route("/api/up-list/set-auto", methods=["POST"])
def up_list_set_auto():
    """开关某 UP 的订阅追更。{mid, auto}"""
    body = request.get_json(silent=True) or {}
    mid = body.get("mid")
    if mid is None or "auto" not in body:
        return jsonify({"ok": False, "error": "缺少 mid 或 auto"}), 400
    ok = up_list.set_auto(int(mid), bool(body["auto"]))
    return jsonify({"ok": ok})


@app.route("/api/up-list/reset-seen", methods=["POST"])
def up_list_reset_seen():
    body = request.get_json(silent=True) or {}
    mid = body.get("mid")
    if mid is None:
        return jsonify({"ok": False, "error": "缺少 mid"}), 400
    up_list.reset_seen(int(mid))
    return jsonify({"ok": True})


@app.route("/api/up-updates")
def up_updates():
    """拉清单中所有 UP 的新增视频。?include_seen=true 把已见的一起返回(否则只返回增量)。

    风控:与 fetch_follow_updates 同一结论——arc/search 并发会触发 B 站账号级
    412,必须串行 + UP 间 1.5 秒间隔。mark_seen 只用本次实际拿到的数据推进;
    某 UP 被限流返回空时保持原 last_seen,否则用户没看到的视频会被误标为已见。
    """
    include_seen = request.args.get("include_seen") == "true"
    ups = up_list.list_ups()
    if not ups:
        return jsonify({"ok": True, "items": [], "ups": []})

    results = []
    for i, up in enumerate(ups):
        mid = int(up["mid"])
        fetched = auth.fetch_up_videos(mid, ps=20)
        if fetched:
            newest = fetched[0]  # arc/search 按 pubdate 倒序,第一条即最新
            last = up_list.get_last_seen(mid)
            if not include_seen and last:
                seen_ts = last.get("ts", 0)
                seen_bvid = last.get("bvid", "")
                fetched = [v for v in fetched
                           if v.get("view_at", 0) > seen_ts and v.get("bvid") != seen_bvid]
            for v in fetched:
                v["uploader_mid"] = mid
            results.extend(fetched)
            up_list.mark_seen(mid, newest["bvid"], newest.get("view_at", 0),
                              newest.get("title", ""))
        if i < len(ups) - 1:
            time.sleep(1.5)  # 防 arc/search 账号级限流
    results.sort(key=lambda x: x.get("view_at", 0), reverse=True)
    return jsonify({"ok": True, "items": results, "ups": ups})


# ---------- A4: 收藏夹浏览 ----------
@app.route("/api/fav-folders")
def fav_folders():
    try:
        folders = auth.fetch_fav_folders()
        return jsonify({"ok": True, "folders": folders})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/fav-content")
def fav_content():
    fid = int(request.args.get("fid", 0))
    if not fid:
        return jsonify({"ok": False, "error": "缺少 fid"}), 400
    try:
        items = auth.fetch_fav_content(fid)
        return jsonify({"ok": True, "items": items})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/watchlater")
def watchlater():
    """「稍后再看」列表。"""
    try:
        items = auth.fetch_watchlater()
        return jsonify({"ok": True, "items": items})
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


@app.route("/api/stats")
def stats():
    """统计面板: 总下载数、总字节数、本月/今日 数。本月=自然月。"""
    import datetime as _dt
    now = int(time.time())
    today = _dt.date.today()
    month_start = int(time.mktime(_dt.date(today.year, today.month, 1).timetuple()))
    day_start = now - 86400

    total_count = 0
    total_bytes = 0
    month_count = 0
    day_count = 0
    for t in downloader.list_tasks():
        if t.get("status") != "done":
            continue
        total_count += 1
        total_bytes += t.get("downloaded_bytes") or 0
        finished = t.get("finished_at")
        if finished:
            if finished >= day_start:
                day_count += 1
            if finished >= month_start:
                month_count += 1

    # 也扫一下磁盘真实文件大小兜底(历史任务没有 downloaded_bytes 字段时)
    try:
        disk_bytes = sum(f.stat().st_size for f in config.DOWNLOAD_DIR.iterdir() if f.is_file())
    except Exception:
        disk_bytes = 0

    return jsonify({
        "ok": True,
        "total_count": total_count,
        "total_bytes": total_bytes if total_bytes else disk_bytes,  # 用磁盘实测值兜底
        "month_count": month_count,
        "day_count": day_count,
        "disk_bytes": disk_bytes,
    })


@app.route("/api/config")
def get_config():
    return jsonify({"ok": True, **config.get_config()})


@app.route("/api/config", methods=["POST"])
def set_config():
    """部分更新配置。body 可含 download_dir / max_concurrent / ratelimit_mbps 任意字段。"""
    body = request.get_json(silent=True) or {}
    if not body:
        return jsonify({"ok": False, "error": "无更新字段"}), 400
    r = config.set_config(body)
    if not r.get("ok"):
        return jsonify(r), 400
    return jsonify(r)


@app.route("/api/open-folder", methods=["POST"])
def open_folder():
    """在 Finder 中打开下载目录。"""
    try:
        subprocess.run(["open", str(config.DOWNLOAD_DIR)], check=False)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    print(f"\n  浏览器打开: http://localhost:{PORT}\n")
    auto_refresh.start_loop()  # 订阅追更轮询(间隔见 config.AUTO_REFRESH_MINUTES)
    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True)
