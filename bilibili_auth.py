"""B 站扫码登录 + cookie 策略。

登录方式三选一,优先级:扫码 cookie > Edge 浏览器 > cookies.txt。
"""
import json
import re
import threading
import time
from typing import Optional

import requests
import yt_dlp

import storage
from config import SESSION_COOKIE_FILE, COOKIE_FILE, BROWSER_COOKIE_SOURCE

QR_GENERATE = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QR_POLL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
NAV_API = "https://api.bilibili.com/x/web-interface/nav"

# B 站 passport 接口有风控,裸请求会 412。必须带浏览器 headers。
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.bilibili.com/",
    "Origin": "https://www.bilibili.com",
}

POLL_CODES = {"86101": "waiting", "86090": "scanned", "86038": "expired", "0": "success"}
POLL_MESSAGES = {"86101": "未扫码", "86090": "已扫码,请在手机上确认", "86038": "二维码已失效", "0": "登录成功"}

# 网络函数可被测试替换
_http_get = requests.get


def qrcode_generate() -> dict:
    """生成二维码。返回 {'url','qrcode_key'}。"""
    resp = _http_get(QR_GENERATE, headers=BROWSER_HEADERS, timeout=10)
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"生成二维码失败: {data.get('message')}")
    d = data["data"]
    return {"url": d["url"], "qrcode_key": d["qrcode_key"]}


def qrcode_poll(qrcode_key: str) -> dict:
    """轮询扫码状态。返回 parse_poll_response 结果。"""
    resp = _http_get(QR_POLL, params={"qrcode_key": qrcode_key},
                     headers=BROWSER_HEADERS, timeout=10)
    set_cookie = resp.headers.get("Set-Cookie", "")
    return parse_poll_response(resp.json(), set_cookie)


def parse_poll_response(data: dict, set_cookie_header: str) -> dict:
    """把 poll 响应解析成统一状态。

    SESSDATA 优先从 data.url 的 query 参数解析(社区文档验证最可靠),
    Set-Cookie 头作为兜底。同时提取 DedeUserID/bili_jct。
    """
    code = str(data.get("data", {}).get("code", -1))
    status = POLL_CODES.get(code, "unknown")
    sessdata = ""
    dede_userid = ""
    bili_jct = ""
    if status == "success":
        from urllib.parse import urlparse, parse_qs
        url = data.get("data", {}).get("url", "")
        qs = parse_qs(urlparse(url).query)
        # 优先 url query
        if qs.get("SESSDATA"):
            sessdata = qs["SESSDATA"][0]
        dede_userid = (qs.get("DedeUserID") or [""])[0]
        bili_jct = (qs.get("bili_jct") or [""])[0]
        # 兜底:Set-Cookie
        if not sessdata:
            m = re.search(r"SESSDATA=([^;]+)", set_cookie_header or "")
            sessdata = m.group(1) if m else ""
        if not bili_jct:
            m = re.search(r"bili_jct=([^;]+)", set_cookie_header or "")
            bili_jct = m.group(1) if m else ""
    return {"status": status, "sessdata": sessdata, "dede_userid": dede_userid,
            "bili_jct": bili_jct, "message": POLL_MESSAGES.get(code, "未知状态")}


def save_session(sessdata: str, dede_userid: str = "", bili_jct: str = "") -> None:
    storage.write_json(SESSION_COOKIE_FILE, {
        "SESSDATA": sessdata, "DedeUserID": dede_userid, "bili_jct": bili_jct
    })


def load_session() -> Optional[dict]:
    return storage.read_json(SESSION_COOKIE_FILE, default=None)


def clear_session() -> None:
    """退出扫码登录:删 sess.json + 清会话缓存。不动 cookies.txt(那是
    用户手工导入的登录态,归 import_cookies_txt/logout 管)。"""
    if SESSION_COOKIE_FILE.exists():
        SESSION_COOKIE_FILE.unlink()
    # 同时清掉 cookiejar 缓存,避免退出后还能访问接口(5 分钟缓存)。
    # 各拿各的锁、不嵌套:清会话域期间不阻塞数据域(反之亦然)。
    global _cj_cache, _follow_cache
    with _session_lock:
        _cj_cache = {"cj": None, "ts": 0.0}
    with _fetch_lock:
        _follow_cache = {"items": None, "ts": 0.0}


def import_cookies_txt(text: str) -> None:
    """用户粘贴的 Netscape cookies.txt 落盘,并切到该登录态。"""
    COOKIE_FILE.write_text(text, encoding="utf-8")
    clear_session()


def logout() -> None:
    """完全退出:扫码会话 + 手工导入的 cookies.txt 一起清。"""
    clear_session()
    if COOKIE_FILE.exists():
        COOKIE_FILE.unlink()


def has_session() -> bool:
    s = load_session()
    return bool(s and s.get("SESSDATA"))


def write_cookies_txt(sessdata: str, dede_userid: str = "", bili_jct: str = "") -> None:
    """把扫码 cookie 写成 Netscape cookies.txt,供 yt-dlp 用。"""
    jar_lines = [
        "# Netscape HTTP Cookie File",
        "# https://curl.se/docs/http-cookies.html",
        ".bilibili.com\tTRUE\t/\tFALSE\t0\tSESSDATA\t" + sessdata,
    ]
    if dede_userid:
        jar_lines.append(f".bilibili.com\tTRUE\t/\tFALSE\t0\tDedeUserID\t{dede_userid}")
    if bili_jct:
        jar_lines.append(f".bilibili.com\tTRUE\t/\tFALSE\t0\tbili_jct\t{bili_jct}")
    COOKIE_FILE.write_text("\n".join(jar_lines) + "\n", encoding="utf-8")


def get_ytdlp_cookie_arg() -> dict:
    """返回传给 yt-dlp YoutubeDL 的 cookie 选项。优先级:扫码>Edge>cookies.txt。"""
    sess = load_session()
    if sess and sess.get("SESSDATA"):
        write_cookies_txt(sess["SESSDATA"], sess.get("DedeUserID", ""), sess.get("bili_jct", ""))
        return {"cookiefile": str(COOKIE_FILE)}
    if COOKIE_FILE.exists() and COOKIE_FILE.stat().st_size > 50:
        return {"cookiefile": str(COOKIE_FILE)}
    return {"cookiesfrombrowser": (BROWSER_COOKIE_SOURCE,)}


_cj_cache = {"cj": None, "ts": 0.0}
# 会话域锁:罩 _cj_cache/_wbi_ie。get_cookiejar 整体入锁(含 Edge 慢提取),
# 与 clear_session 严格排序——登出期间并发的提取结果不会写回缓存,
# 也顺带避免并发重复提取;缓存命中时锁内只有一次微秒级检查。
_session_lock = threading.Lock()


def get_cookiejar():
    """返回当前生效的 cookiejar(扫码 > cookies.txt > Edge)。失败返回 None。

    缓存 5 分钟:Edge 读 cookie 较耗时,避免每次请求重建。
    """
    import time as _time
    with _session_lock:
        if _cj_cache["cj"] is not None and _time.time() - _cj_cache["ts"] < 300:
            return _cj_cache["cj"]
        cookie_arg = get_ytdlp_cookie_arg()
        if "cookiefile" in cookie_arg:
            try:
                import http.cookiejar
                cj = http.cookiejar.MozillaCookieJar(cookie_arg["cookiefile"])
                cj.load(ignore_discard=True, ignore_expires=True)
                _cj_cache.update(cj=cj, ts=_time.time())
                return cj
            except Exception:
                pass
        if "cookiesfrombrowser" in cookie_arg:
            try:
                import yt_dlp
                with yt_dlp.YoutubeDL({"quiet": True, **cookie_arg}) as ydl:
                    _cj_cache.update(cj=ydl.cookiejar, ts=_time.time())
                    return ydl.cookiejar
            except Exception:
                pass
        return None


def get_login_status() -> dict:
    """用当前生效的 cookie(扫码 > cookies.txt > Edge)调 nav 接口,返回登录信息。"""
    cookies = get_cookiejar()
    if cookies is None:
        return {"logged_in": False}
    try:
        resp = requests.get(NAV_API, cookies=cookies,
                            headers={"User-Agent": BROWSER_HEADERS["User-Agent"]}, timeout=10)
        d = resp.json().get("data", {})
        return {
            "logged_in": d.get("isLogin", False),
            "uname": d.get("uname", ""),
            "vip": d.get("vipStatus", 0) == 1,
            "vip_type": d.get("vipType", 0),
        }
    except Exception as e:
        return {"logged_in": False, "error": str(e)}


def fetch_history(page: int = 1, size: int = 50) -> list[dict]:
    """拉取观看历史。返回归一化的视频列表(最新在前)。

    需要 cookiejar(扫码/Edge/cookies.txt)。size 上限 100/页。
    """
    import time as _time
    cj = get_cookiejar()
    if cj is None:
        raise RuntimeError("未登录,无法获取观看历史")
    # B 站接口偶发 SSL 断连,自动重试
    last = None
    for attempt in range(3):
        try:
            resp = requests.get("https://api.bilibili.com/x/v2/history",
                                cookies=cj, headers=BROWSER_HEADERS,
                                params={"pn": page, "ps": min(size, 100)}, timeout=12)
            data = resp.json()
            break
        except requests.RequestException as e:
            last = e
            _time.sleep(1 + attempt)
    else:
        raise RuntimeError(f"网络错误(已重试 3 次):{last}")
    if data.get("code") != 0:
        raise RuntimeError(data.get("message", "获取失败"))
    items = []
    for it in data.get("data", []) or []:
        dur = it.get("duration", 0)
        watched = max(0, int(it.get("progress") or 0))
        pct = round(watched / dur * 100, 1) if dur else 0
        items.append(_video_item(it, watched_seconds=watched, watched_pct=pct))
    return items


def _my_mid(cj) -> Optional[int]:
    try:
        resp = requests.get(NAV_API, cookies=cj, headers=BROWSER_HEADERS, timeout=10)
        return resp.json().get("data", {}).get("mid")
    except Exception:
        return None


def _video_item(it: dict, *, owner_key="owner", name_key="name", pic_key="pic",
                view_at_key="view_at", mark_invalid=False, **extra) -> dict:
    """把 B 站各接口的原始条目归一化成前端统一的视频卡字段。

    各接口字段名不一:作者在 owner/upper 容器或顶层(arc 的 author),
    封面 pic/cover,时间戳 view_at/tview_at/created/无——用参数对齐;
    extra 直接覆盖/追加字段(如 watched_pct、uploader_mid)。
    """
    owner = (it.get(owner_key) or {}) if owner_key else {}
    bvid = it.get("bvid") or ""
    title = it.get("title") or ""
    if mark_invalid and it.get("attr"):
        title = title or "已失效视频"
    item = {
        "bvid": bvid,
        "title": title,
        "uploader": (owner.get(name_key) or "") if owner_key else (it.get(name_key) or ""),
        "uploader_mid": owner.get("mid") if owner_key else None,
        "thumbnail": (it.get(pic_key) or "").replace("http:", "https:"),
        "duration": it.get("duration") or 0,
        "url": f"https://www.bilibili.com/video/{bvid}" if bvid else "",
    }
    if view_at_key:
        item["view_at"] = it.get(view_at_key, 0) or 0
    if extra:
        item.update(extra)
    return item


def _paged_get(url: str, base_params: dict, *, what: str, max_pages: int, ps: int,
               gap: float = 0.3, mid_param: str = "", parse) -> list:
    """通用分页拉取:会话获取、code 判错、翻页限速。

    parse(data_dict, fetched_count) -> (本页条目, 是否还有下一页)。
    mid_param 给定时只取一次 _my_mid 塞进参数(旧行为每页重复打 nav 接口)。
    """
    cj = get_cookiejar()
    if cj is None:
        raise RuntimeError(f"未登录,无法获取{what}")
    if mid_param:
        mid = _my_mid(cj)
        if not mid:
            raise RuntimeError("无法获取用户 ID")
        base_params = {**base_params, mid_param: mid}
    out = []
    for pn in range(1, max_pages + 1):
        resp = requests.get(url, cookies=cj, headers=BROWSER_HEADERS,
                            params={**base_params, "pn": pn, "ps": ps}, timeout=12)
        data = _safe_json(resp)
        if not data or data.get("code") != 0:
            raise RuntimeError((data or {}).get("message",
                             f"获取{what}失败(可能被风控,稍后再试)"))
        items, has_more = parse(data.get("data") or {}, len(out))
        out.extend(items)
        if not has_more:
            break
        if gap:
            time.sleep(gap)
    return out


def fetch_watchlater() -> list[dict]:
    """拉取「稍后再看」列表。直接用 B 站 toview 接口(比 yt-dlp 快且字段丰富)。
    返回归一化项 [{bvid,title,uploader,thumbnail,duration,view_at,url}],
    其中 view_at 是加入稍后再看的时间(tview_at)。
    """
    cj = get_cookiejar()
    if cj is None:
        raise RuntimeError("未登录,无法获取稍后再看")
    resp = requests.get("https://api.bilibili.com/x/v2/history/toview",
                        cookies=cj, headers=BROWSER_HEADERS, timeout=12)
    data = _safe_json(resp)
    if not data or data.get("code") != 0:
        raise RuntimeError((data or {}).get("message", "获取稍后再看失败"))
    out = []
    for it in (data.get("data", {}) or {}).get("list", []) or []:
        out.append(_video_item(it, view_at_key="tview_at"))
    return out


def fetch_fav_folders() -> list[dict]:
    """列出我的收藏夹列表。"""
    cj = get_cookiejar()
    if cj is None:
        raise RuntimeError("未登录,无法获取收藏夹")
    mid = _my_mid(cj)
    if not mid:
        raise RuntimeError("无法获取用户 ID")
    resp = requests.get("https://api.bilibili.com/x/v3/fav/folder/created/list-all",
                        cookies=cj, headers=BROWSER_HEADERS,
                        params={"up_mid": mid}, timeout=12)
    data = _safe_json(resp)
    if not data or data.get("code") != 0:
        raise RuntimeError((data or {}).get("message", "获取收藏夹失败"))
    out = []
    for f in (data.get("data") or {}).get("list", []) or []:
        out.append({
            "id": f.get("id"),
            "fid": f.get("fid"),
            "title": f.get("title", ""),
            "media_count": f.get("media_count", 0),
            "cover": (f.get("cover") or "").replace("http:", "https:"),
        })
    return out


def fetch_collected_seasons(max_pages: int = 5) -> list[dict]:
    """「我追的合集/收藏夹」列表(B 站收藏页左侧栏的合集区)。

    官网调用 /x/v3/fav/folder/collected/list,必须带 platform=web,
    否则 code=0 但永远返回空列表。item.id 就是 season_id。
    """
    def parse(d, _fetched):
        items = [{"id": f.get("id"),
                  "title": f.get("title", ""),
                  "media_count": f.get("media_count", 0),
                  "cover": (f.get("cover") or "").replace("http:", "https:")}
                 for f in d.get("list") or []]
        return items, bool(d.get("has_more"))

    return _paged_get("https://api.bilibili.com/x/v3/fav/folder/collected/list",
                      {"platform": "web"}, what="合集列表",
                      max_pages=max_pages, ps=20, mid_param="up_mid", parse=parse)


def fetch_season_content(season_id: int, ps: int = 20, max_pages: int = 30) -> list[dict]:
    """拉取合集(我追的合集)内的视频。/x/space/fav/season/list,pn 翻页。"""
    def parse(d, _fetched):
        items = [_video_item(m, owner_key="upper", pic_key="cover",
                             view_at_key=None, mark_invalid=True)
                 for m in d.get("medias") or []]
        return items, bool(d.get("has_more"))

    return _paged_get("https://api.bilibili.com/x/space/fav/season/list",
                      {"season_id": season_id}, what="合集内容",
                      max_pages=max_pages, ps=ps, parse=parse)


def fetch_fav_content(media_id: int, ps: int = 20, max_pages: int = 20) -> list[dict]:
    """拉取某个收藏夹内的视频。走 B 站原生 /x/v3/fav/resource/list。

    media_id 是收藏夹的媒体列表 id(文件夹接口里的 `id` 字段,如 64425947),
    不是 `fid`(如 644259)——传 fid 接口会返回 code=0 但 medias 为空。
    旧实现用 yt-dlp 解析 medialist 页面,被风控时静默返回 [],已弃用。
    """
    def parse(d, _fetched):
        items = [_video_item(m, owner_key="upper", pic_key="cover",
                             view_at_key=None, mark_invalid=True)
                 for m in d.get("medias") or []]
        return items, bool(d.get("has_more"))

    return _paged_get("https://api.bilibili.com/x/v3/fav/resource/list",
                      {"media_id": media_id, "order": "mtime"}, what="收藏夹内容",
                      max_pages=max_pages, ps=ps, parse=parse)


def fetch_followings(page: int = 1, size: int = 50) -> list[dict]:
    """拉取我关注的 UP 主列表。"""
    def parse(d, _fetched):
        items = [{"mid": f.get("mid"), "uname": f.get("uname", ""),
                  "face": (f.get("face") or "").replace("http:", "https:")}
                 for f in d.get("list") or []]
        return items, False

    return _paged_get("https://api.bilibili.com/x/relation/followings",
                      {"order": "desc"}, what="关注列表",
                      max_pages=1, ps=min(size, 50), mid_param="vmid", parse=parse)


_followings_cache = {"items": [], "total": 0, "ts": 0}
# 数据域锁:罩 _followings_cache/_follow_cache。check/act 各自短锁,
# 翻页等慢网络请求一律锁外,不给 B 站请求排队。
_fetch_lock = threading.Lock()


def fetch_followings_all(force: bool = False, max_pages: int = 10) -> dict:
    """拉取我关注的全部 UP(上限 max_pages*50)。10 分钟缓存。

    relation 接口轻量,翻页间隔 0.2s 即可;与 arc/search 的账号级风控不是一类。
    """
    import time as _time
    with _fetch_lock:
        if not force and _followings_cache["items"] and _time.time() - _followings_cache["ts"] < 600:
            return {"items": _followings_cache["items"], "total": _followings_cache["total"]}
    box = {"total": 0}

    def parse(d, fetched):
        box["total"] = d.get("total", 0)
        items = [{"mid": f.get("mid"), "uname": f.get("uname", ""),
                  "face": (f.get("face") or "").replace("http:", "https:")}
                 for f in d.get("list") or []]
        has_more = not (fetched + len(items) >= box["total"] or not items)
        return items, has_more

    all_items = _paged_get("https://api.bilibili.com/x/relation/followings",
                           {"order": "desc"}, what="关注列表",
                           max_pages=max_pages, ps=50, gap=0.2,
                           mid_param="vmid", parse=parse)
    with _fetch_lock:
        _followings_cache.update(items=all_items, total=box["total"], ts=_time.time())
    return {"items": all_items, "total": box["total"]}


_wbi_ie = None


def _get_wbi_ie():
    """复用 yt-dlp 的 WBI 签名器(它内部处理 key 缓存)。锁内懒初始化,
    防并发双重构造(旧 cookie 实例胜出会长期驻留)。"""
    global _wbi_ie
    with _session_lock:
        if _wbi_ie is not None:
            return _wbi_ie
        import yt_dlp
        from yt_dlp.extractor.bilibili import BiliBiliIE
        ydl = yt_dlp.YoutubeDL({"quiet": True, **get_ytdlp_cookie_arg()})
        _wbi_ie = BiliBiliIE(ydl)
        return _wbi_ie


def _safe_json(resp):
    try:
        return resp.json()
    except Exception:
        return None


def fetch_up_card(mid: int) -> Optional[dict]:
    """公开的用户卡片接口:mid → {"uname", "face"}。失败返回 None。

    添加 UP 时用它拿名称/头像(arc/search 的视频列表里没有 UP 头像)。
    """
    try:
        r = requests.get("https://api.bilibili.com/x/web-interface/card",
                         params={"mid": mid}, headers=BROWSER_HEADERS, timeout=10)
        data = r.json()
        if data.get("code") != 0:
            return None
        card = (data.get("data") or {}).get("card") or {}
        return {"uname": card.get("name", ""), "face": card.get("face", "")}
    except Exception:
        return None


def fetch_up_videos(mid: int, ps: int = 10, pn: int = 1) -> list[dict]:
    """WBI 签名调 arc/search,返回某 UP 最近投稿(按 pubdate 倒序)。单个失败不影响整体。

    ps=页大小,pn=页码(1 起)。遇到 412/风控返回 [],不抛异常。
    """
    try:
        ie = _get_wbi_ie()
        signed = ie._sign_wbi({"mid": str(mid), "ps": str(ps), "pn": str(pn), "order": "pubdate"}, str(mid))
        resp = requests.get("https://api.bilibili.com/x/space/wbi/arc/search",
                            params=signed, cookies=get_cookiejar(),
                            headers={**BROWSER_HEADERS, "Referer": f"https://space.bilibili.com/{mid}"},
                            timeout=12)
        # 412/风控时返回 HTML 不是 JSON,_safe_json 返回 None
        if resp.status_code == 412:
            return []
        data = _safe_json(resp)
        if not data or data.get("code") != 0:
            return []
    except Exception:
        return []
    items = []
    for v in (data.get("data", {}) or {}).get("list", {}).get("vlist", []) or []:
        items.append(_video_item(v, owner_key=None, name_key="author",
                                 view_at_key="created", uploader_mid=mid))
    return items


_follow_cache = {"items": None, "ts": 0.0}


def fetch_follow_updates(max_ups: int = 30, ps: int = 10, force: bool = False) -> list[dict]:
    """拉取前 max_ups 个关注 UP 的最近投稿,合并按时间倒序。带 5 分钟缓存。

    重要: B 站 arc/search 接口对**账号级别**限流敏感,6 并发 + 错峰 0-0.5s
    仍会触发 412 限流(实测 30/30 全失败,账号风控持续 5+ 分钟)。
    改为**串行 + 1.5 秒间隔**,牺牲速度换稳定性。30 UP = 45 秒。
    """
    import time as _time
    with _fetch_lock:
        if (not force and _follow_cache["items"] is not None
                and _time.time() - _follow_cache["ts"] < 300):  # 5 分钟
            return _follow_cache["items"]
    follows = fetch_followings(1, max_ups)
    results = []
    # 串行拉取,每个 UP 间间隔 1.5 秒
    for up in follows:
        items = fetch_up_videos(up["mid"], ps)
        results.extend(items)
        _time.sleep(1.5)
    results.sort(key=lambda x: x.get("view_at", 0), reverse=True)
    with _fetch_lock:
        _follow_cache.update(items=results, ts=_time.time())
    return results
