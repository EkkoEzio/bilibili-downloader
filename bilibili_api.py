"""B 站读接口客户端:历史/稍后再看/收藏夹/合集/关注/UP 投稿,及条目归一化。

依赖 bilibili_auth 的会话(get_cookiejar / BROWSER_HEADERS / _session_lock);
数据域缓存(_followings_cache/_follow_cache)归本模块,登出时由
bilibili_auth.clear_session 经 reset_data_caches 清空。
"""
import threading
import time
from typing import Optional

import requests

import up_list
from bilibili_auth import (BROWSER_HEADERS, get_cookiejar,
                           get_ytdlp_cookie_arg, _session_lock)

NAV_API = "https://api.bilibili.com/x/web-interface/nav"


def _safe_json(resp):
    try:
        return resp.json()
    except Exception:
        return None


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


def fetch_history(page: int = 1, size: int = 50) -> list[dict]:
    """拉取观看历史。返回归一化的视频列表(最新在前)。

    需要 cookiejar(扫码/Edge/cookies.txt)。size 上限 100/页。
    """
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
            time.sleep(1 + attempt)
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
    for f in (data.get("data", {}) or {}).get("list", []) or []:
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
    with _fetch_lock:
        if not force and _followings_cache["items"] and time.time() - _followings_cache["ts"] < 600:
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
        _followings_cache.update(items=all_items, total=box["total"], ts=time.time())
    return {"items": all_items, "total": box["total"]}


_wbi_ie = None


def _get_wbi_ie():
    """复用 yt-dlp 的 WBI 签名器(它内部处理 key 缓存)。锁内懒初始化,
    防并发双重构造(旧 cookie 实例胜出会长期驻留)。锁与会话域共用
    bilibili_auth._session_lock:签名器绑定当前 cookie,登出竞态下不得残留。"""
    global _wbi_ie
    with _session_lock:
        if _wbi_ie is not None:
            return _wbi_ie
        import yt_dlp
        from yt_dlp.extractor.bilibili import BiliBiliIE
        ydl = yt_dlp.YoutubeDL({"quiet": True, **get_ytdlp_cookie_arg()})
        _wbi_ie = BiliBiliIE(ydl)
        return _wbi_ie


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
    with _fetch_lock:
        if (not force and _follow_cache["items"] is not None
                and time.time() - _follow_cache["ts"] < 300):  # 5 分钟
            return _follow_cache["items"]
    follows = fetch_followings(1, max_ups)
    results = []
    # 串行拉取,每个 UP 间间隔 1.5 秒
    for up in follows:
        items = fetch_up_videos(up["mid"], ps)
        results.extend(items)
        time.sleep(1.5)
    results.sort(key=lambda x: x.get("view_at", 0), reverse=True)
    with _fetch_lock:
        _follow_cache.update(items=results, ts=time.time())
    return results


def reset_data_caches() -> None:
    """登出/切登录态时清数据域缓存(由 bilibili_auth.clear_session 调用)。

    与旧 clear_session 内联逻辑同锁纪律:持 _fetch_lock、不嵌套会话域锁。
    """
    with _fetch_lock:
        _followings_cache.update(items=[], total=0, ts=0.0)
        _follow_cache.update(items=None, ts=0.0)


def register_up(mid: int) -> dict:
    """把 UP 加入自动下载清单:取名称/头像(用户卡片接口,投稿作者名兜底),
    并以该 UP 当前最新投稿打「已见」基线——加入之后只有新投稿才自动下载。
    拿不到最新投稿时用当前时刻兜底(时间上早于现在的都不会算新增)。
    返回 up_list.add_up 的清单条目。"""
    uname, face = "", ""
    card = fetch_up_card(mid)
    if card:
        uname, face = card.get("uname", ""), card.get("face", "")
    if not uname:
        items = fetch_up_videos(mid, ps=1)
        if items:
            uname = items[0].get("uploader", "")
    u = up_list.add_up(mid, uname, face)
    now = int(time.time())
    try:
        page = fetch_up_videos(mid, ps=1)
        newest = page[0] if page else None
    except Exception:
        newest = None
    if newest:
        up_list.mark_seen(mid, newest.get("bvid", ""), newest.get("view_at", 0),
                          newest.get("title", ""), fetched_at=now)
    else:
        up_list.mark_seen(mid, "", now, "", fetched_at=now)
    return u
