"""Flask 路由层。薄,业务委托给 bilibili_auth / downloader。"""
import io
import logging
import re
import threading
import time
import json
from typing import Optional

from flask import Flask, request, jsonify, render_template, Response, stream_with_context

import qrcode
import auto_refresh
import pagecache
import up_list
import bilibili_auth as auth
import bilibili_api as bapi
import downloader
import taskstore
from config import PORT, BROWSER_COOKIE_SOURCE
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


def api_errors(f):
    """统一异常出口:意外异常 → 500 JSON。路由体只写正常路径。
    400 类参数错误仍由路由自行返回(它们是预期分支,不是异常)。"""
    import functools
    @functools.wraps(f)
    def wrapper(*a, **kw):
        try:
            return f(*a, **kw)
        except Exception as e:
            log.exception("路由 %s 异常", f.__name__)
            return jsonify({"ok": False, "error": str(e)}), 500
    return wrapper



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
    """本地用 qrcode 库把扫码 URL 画成 PNG,不依赖第三方服务。
    必须传 ?url=(与前端轮询 key 配对的登录 URL):自行 generate 会产生新 key,
    图与轮询 key 不一致,扫了也一直「未扫码」。域名白名单防钓鱼码。"""
    url = request.args.get("url", "")
    if not url.startswith("https://passport.bilibili.com/"):
        return jsonify({"ok": False, "error": "缺少/非法 url"}), 400
    img = qrcode.make(url)
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
    auth.import_cookies_txt(text)  # 写盘归 bilibili_auth 管,路由层不碰文件
    return jsonify({"ok": True})


@app.route("/api/login/logout", methods=["POST"])
def login_logout():
    auth.logout()
    return jsonify({"ok": True})


# ---------- 解析/下载 ----------
@app.route("/api/parse", methods=["POST"])
@api_errors
def parse():
    url = (request.get_json(silent=True) or {}).get("url", "").strip()
    if not url:
        return jsonify({"ok": False, "error": "请输入 URL"}), 400
    info = downloader.parse_video(url)
    # 附带登录态信息,前端据此提示"未登录仅公开画质"
    status = auth.get_login_status()
    return jsonify({"ok": True, "info": info, "logged_in": status.get("logged_in", False)})


@app.route("/api/parse-part", methods=["POST"])
@api_errors
def parse_part():
    """多 P 视频选了具体分 P 后,拉该 P 的清晰度。"""
    body = request.get_json(silent=True) or {}
    url = body.get("url", "").strip()
    page = int(body.get("page", 1))
    if not url:
        return jsonify({"ok": False, "error": "请输入 URL"}), 400
    return jsonify({"ok": True, "info": downloader.parse_part(url, page)})


@app.route("/api/download", methods=["POST"])
@api_errors
def download():
    body = request.get_json(silent=True) or {}
    url = body.get("url", "").strip()
    label = body.get("label", "")
    page = int(body.get("page", 1))
    uploader_mid = int(body.get("uploader_mid") or 0)  # 前端解析时拿到就带上
    if not url:
        return jsonify({"ok": False, "error": "请输入 URL"}), 400
    # 重复下载拦截:同 URL+清晰度+分P 且仍在跑
    existing = downloader.find_active_by_url(url, label, page)
    if existing:
        return jsonify({"ok": False, "error": "该视频相同清晰度正在下载中", "task_id": existing}), 409
    tid = downloader.start_download(url, label, page, uploader_mid=uploader_mid)
    return jsonify({"ok": True, "task_id": tid})


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


@app.route("/api/tasks/clean-invalid", methods=["POST"])
@api_errors
def tasks_clean_invalid():
    """删除失败/失效记录:下载失败的任务 + 已完成但本地文件已不存在(被删/改名)的任务。
    只删记录,不碰文件(失效的本就没有文件)。"""
    return jsonify({"ok": True, **downloader.clean_invalid_records()})


# ---------- 批量 ----------
@app.route("/api/batch/parse", methods=["POST"])
@api_errors
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
        r = downloader.resolve_playlist(url)
        return jsonify({"ok": True, "items": r["items"], "title": r.get("title", "")})
    else:
        return jsonify({"ok": False, "error": "请提供 url 或 urls"}), 400

    return jsonify({"ok": True, "items": items})


@app.route("/api/batch/download", methods=["POST"])
@api_errors
def batch_download():
    """批量启动下载。{items:[{url,title,page}], label}"""
    body = request.get_json(silent=True) or {}
    items = body.get("items") or []
    label = body.get("label", "")
    if not items:
        return jsonify({"ok": False, "error": "没有待下载项"}), 400
    tids = downloader.start_batch(items, label)
    return jsonify({"ok": True, "task_ids": tids, "count": len(tids)})


# ---------- 内容浏览(慢数据源统一「缓存优先 + 后台拉新」) ----------

@app.route("/api/cache/<key>")
def cache_get(key):
    """读某数据源的上次结果(前端秒显用)。纯本地文件,不打 B 站接口。"""
    entry = pagecache.load(key)
    if not entry:
        return jsonify({"ok": False})
    return jsonify({"ok": True, "saved_at": entry.get("saved_at"),
                    "data": entry.get("data")})


@app.route("/api/history")
@api_errors
def history():
    """观看历史。?page=1&size=50"""
    page = int(request.args.get("page", 1))
    size = int(request.args.get("size", 50))
    items = bapi.fetch_history(page, size)
    return jsonify({"ok": True, "items": items})


@app.route("/api/follow-updates")
@api_errors
def follow_updates():
    """关注 UP 主最近投稿,合并按时间倒序。?force=true 跳过缓存强制刷新。"""
    items = bapi.fetch_follow_updates(force=request.args.get("force") == "true")
    pagecache.save("follow_updates", {"ok": True, "items": items})
    return jsonify({"ok": True, "items": items})


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
    ups = up_list.list_ups()
    for u in ups:
        last = up_list.get_last_seen(int(u["mid"]))
        u["fetched_at"] = (last or {}).get("fetched_at")
    return jsonify({"ok": True, "ups": ups})


@app.route("/api/up-list/add", methods=["POST"])
@api_errors
def up_list_add():
    """添加 UP(加入清单 = 之后新投稿自动下载)。{input: 'https://space.bilibili.com/12345' 或 '12345'}"""
    body = request.get_json(silent=True) or {}
    mid = _parse_mid_from_input(body.get("input", ""))
    if not mid:
        return jsonify({"ok": False, "error": "请输入 B 站主页链接或 UP 的 mid"}), 400
    # 取名/头像、写清单、打「已见」基线的业务编排在 bilibili_api.register_up,
    # 路由层只做输入解析
    u = bapi.register_up(mid)
    return jsonify({"ok": True, "up": u})


@app.route("/api/up-list/remove", methods=["POST"])
def up_list_remove():
    body = request.get_json(silent=True) or {}
    mid = body.get("mid")
    if mid is None:
        return jsonify({"ok": False, "error": "缺少 mid"}), 400
    ok = up_list.remove_up(int(mid))
    return jsonify({"ok": ok})


@app.route("/api/up-list/set-quality", methods=["POST"])
def up_list_set_quality():
    """设置某 UP 新视频自动下载的默认清晰度。{mid, quality};空串=最高画质自动。"""
    body = request.get_json(silent=True) or {}
    mid = body.get("mid")
    if mid is None:
        return jsonify({"ok": False, "error": "缺少 mid"}), 400
    ok = up_list.set_quality(int(mid), str(body.get("quality", "")))
    return jsonify({"ok": ok})


@app.route("/api/up-list/reset-seen", methods=["POST"])
def up_list_reset_seen():
    body = request.get_json(silent=True) or {}
    mid = body.get("mid")
    if mid is None:
        return jsonify({"ok": False, "error": "缺少 mid"}), 400
    up_list.reset_seen(int(mid))
    return jsonify({"ok": True})


@app.route("/api/up-list/check", methods=["POST"])
def up_list_check():
    """手动触发一轮检查(与后台自动下载同一逻辑):清单 UP 的新投稿直接入队下载。
    串行 + UP 间隔,耗时数十秒,放后台线程跑,前端靠队列轮询看结果。"""
    def _run():
        try:
            n = auto_refresh.refresh_once()
            log.info("手动检查完成,入队 %d 条", n)
        except Exception:
            log.exception("手动检查异常")
    threading.Thread(target=_run, daemon=True, name="bd-manual-check").start()
    return jsonify({"ok": True})


# ---------- A4: 收藏夹浏览 ----------
@app.route("/api/fav-folders")
@api_errors
def fav_folders():
    folders = bapi.fetch_fav_folders()
    pagecache.save("fav_folders", {"ok": True, "folders": folders})
    return jsonify({"ok": True, "folders": folders})


@app.route("/api/fav-seasons")
@api_errors
def fav_seasons():
    """「我追的合集」列表(B 站收藏页左侧的合集区)。"""
    seasons = bapi.fetch_collected_seasons()
    pagecache.save("fav_seasons", {"ok": True, "seasons": seasons})
    return jsonify({"ok": True, "seasons": seasons})


@app.route("/api/season-content")
@api_errors
def season_content():
    """合集内的视频列表。?season_id="""
    season_id = int(request.args.get("season_id", 0) or 0)
    if not season_id:
        return jsonify({"ok": False, "error": "缺少 season_id"}), 400
    items = bapi.fetch_season_content(season_id)
    return jsonify({"ok": True, "items": items})


@app.route("/api/fav-content")
@api_errors
def fav_content():
    # media_id 是文件夹接口里的 id(长 id);旧前端只传 fid 会拿到空列表
    media_id = int(request.args.get("media_id", 0) or request.args.get("fid", 0))
    if not media_id:
        return jsonify({"ok": False, "error": "缺少 media_id"}), 400
    items = bapi.fetch_fav_content(media_id)
    return jsonify({"ok": True, "items": items})


@app.route("/api/followings")
@api_errors
def followings():
    """我关注的全部 UP(用于「站点关注」→「关注 UP」视图),附带本地清单已有 mid。"""
    r = bapi.fetch_followings_all(force=request.args.get("force") == "true")
    local = sorted(int(u["mid"]) for u in up_list.list_ups())
    body = {"ok": True, "items": r["items"], "total": r["total"],
            "local_mids": local}
    pagecache.save("followings", body)
    return jsonify(body)


@app.route("/api/watchlater")
@api_errors
def watchlater():
    """「稍后再看」列表。"""
    items = bapi.fetch_watchlater()
    pagecache.save("watchlater", {"ok": True, "items": items})
    return jsonify({"ok": True, "items": items})


@app.route("/api/progress/<task_id>")
def progress(task_id):
    """SSE 推送单个任务进度,任务进终态后收流。

    终态取 taskstore.TERMINAL(done/failed/cancelled/interrupted)——早先只判
    done/failed,取消或中断的任务服务端会一直 1 秒一推,靠前端主动 close 才停。
    """
    def gen():
        while True:
            t = downloader.get_task(task_id)
            if not t:
                yield "event: end\ndata: {}\n\n"
                break
            yield f"data: {json.dumps(t, ensure_ascii=False)}\n\n"
            if t["status"] in taskstore.TERMINAL:
                break
            time.sleep(1)
    return Response(stream_with_context(gen()), mimetype="text/event-stream")


@app.route("/api/tasks")
def tasks():
    return jsonify({"ok": True, "tasks": downloader.list_tasks()})


@app.route("/api/stats")
@api_errors
def stats():
    """统计面板:总下载数、总字节数、本月/今日(自然月)。计算在 taskstore.compute_stats。"""
    return jsonify({"ok": True, **taskstore.compute_stats()})


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


@app.route("/api/task/<task_id>/reveal", methods=["POST"])
@api_errors
def task_reveal(task_id):
    """在文件管理器中定位该任务下载到的文件(按 UP 归档时是子目录里的那个)。
    找不到文件时退回打开下载目录。跨平台细节在 files.reveal_path/open_dir。"""
    files = downloader.find_task_files(task_id)
    if files:
        downloader.reveal_path(files[0])
        return jsonify({"ok": True, "found": True})
    downloader.open_dir(config.DOWNLOAD_DIR)
    return jsonify({"ok": True, "found": False})


@app.route("/api/task/<task_id>/resolve-conflict", methods=["POST"])
def task_resolve_conflict(task_id):
    """处理下载文件名冲突。{action: 'rename_with_index' | 'cancel' | 'open_existing'}
    - rename_with_index: 期望名后加 ` (1)` 递增改名(到 (9) 封顶)
    - cancel: 忽略冲突,清掉标记
    - open_existing: 在 Finder 中定位占用期望名的现有文件
    """
    action = (request.get_json(silent=True) or {}).get("action", "")
    r = downloader.resolve_rename_conflict(task_id, action)
    if not r.get("ok"):
        return jsonify(r), 400
    if action == "open_existing" and r.get("path"):
        downloader.reveal_path(r["path"])
    return jsonify(r)


if __name__ == "__main__":
    print(f"\n  浏览器打开: http://localhost:{PORT}\n")
    pagecache.purge_stale()  # 清掉超 30 天没更新的缓存文件
    auto_refresh.start_loop()  # 订阅追更轮询(间隔见 config.AUTO_REFRESH_MINUTES)
    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True)
