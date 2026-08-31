"""B 站读接口客户端:历史/稍后再看/收藏夹/合集/关注/UP 投稿,及条目归一化。

依赖 bilibili_auth 的会话(get_cookiejar / BROWSER_HEADERS / _session_lock);
数据域缓存(_followings_cache/_follow_cache)归本模块,登出时由
bilibili_auth.clear_session 经 reset_data_caches 清空。
"""
import logging
import threading
import time
from typing import Optional

import requests

import up_list
from bilibili_auth import (BROWSER_HEADERS, get_cookiejar,
                           get_ytdlp_cookie_arg, _session_lock)
from request_governor import (UpstreamRateLimitError, governed_get, governor)

NAV_API = "https://api.bilibili.com/x/web-interface/nav"
log = logging.getLogger("bd.bilibili_api")


def _get(url: str, *, kind: str = "default", **kwargs):
    """通过统一请求治理器调用当前 requests.get（保留测试替换能力）。"""
    return governed_get(requests.get, url, kind=kind, **kwargs)


def _safe_json(resp):
    try:
        data = resp.json()
        if isinstance(data, dict):
            governor.note_api_code(data.get("code"),
                                   endpoint=getattr(resp, "url", ""))
        return data
    except UpstreamRateLimitError:
        raise
    except Exception:
        return None


def _my_mid(cj) -> Optional[int]:
    try:
        resp = _get(NAV_API, cookies=cj, headers=BROWSER_HEADERS, timeout=10)
        data = _safe_json(resp)
        return (data or {}).get("data", {}).get("mid")
    except UpstreamRateLimitError:
        raise
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
        resp = _get(url, cookies=cj, headers=BROWSER_HEADERS,
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
            resp = _get("https://api.bilibili.com/x/v2/history",
                                cookies=cj, headers=BROWSER_HEADERS,
                                params={"pn": page, "ps": min(size, 100)}, timeout=12)
            data = resp.json()
            governor.note_api_code(data.get("code"),
                                   endpoint="https://api.bilibili.com/x/v2/history")
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
    resp = _get("https://api.bilibili.com/x/v2/history/toview",
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
    resp = _get("https://api.bilibili.com/x/v3/fav/folder/created/list-all",
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
# 冷缓存/强制刷新 single-flight：同一数据源同一时刻只允许一个真实上游抓取。
_followings_refresh_lock = threading.Lock()
_follow_refresh_lock = threading.Lock()


def fetch_followings_all(force: bool = False, max_pages: int = 10) -> dict:
    """拉取我关注的全部 UP(上限 max_pages*50)。10 分钟缓存。

    relation 接口轻量,翻页间隔 0.2s 即可;与 arc/search 的账号级风控不是一类。
    并发冷启动/强制刷新通过 single-flight 合并，等待者复用先完成的结果。
    """
    started_at = time.time()
    with _fetch_lock:
        if (not force and _followings_cache["items"]
                and started_at - _followings_cache["ts"] < 600):
            return {"items": _followings_cache["items"], "total": _followings_cache["total"]}

    with _followings_refresh_lock:
        # 等锁期间若已有请求刷新成功，即使本次原本 force 也复用该新结果。
        with _fetch_lock:
            if (_followings_cache["items"]
                    and (_followings_cache["ts"] >= started_at
                         or (not force and time.time() - _followings_cache["ts"] < 600))):
                return {"items": _followings_cache["items"],
                        "total": _followings_cache["total"]}
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
    """公开的用户卡片接口:mid → {"uname", "face"}。普通失败返回 None。

    明确限流异常必须向上传播，避免添加 UP 时把“被风控”误判成“没有数据”。
    """
    try:
        r = _get("https://api.bilibili.com/x/web-interface/card",
                 params={"mid": mid}, headers=BROWSER_HEADERS, timeout=10)
        data = _safe_json(r)
        if not data or data.get("code") != 0:
            return None
        card = (data.get("data") or {}).get("card") or {}
        return {"uname": card.get("name", ""), "face": card.get("face", "")}
    except UpstreamRateLimitError:
        raise
    except Exception:
        return None


def fetch_up_videos(mid: int, ps: int = 10, pn: int = 1) -> list[dict]:
    """WBI 签名调 arc/search,返回某 UP 最近投稿(按 pubdate 倒序)。

    arc/search 走敏感接口队列。限流、非 JSON 和业务错误不再伪装成空列表，
    让上层保留 last-good cache 和 last_seen；调用方可按单 UP 隔离普通失败。
    """
    try:
        ie = _get_wbi_ie()
        signed = ie._sign_wbi({"mid": str(mid), "ps": str(ps), "pn": str(pn),
                               "order": "pubdate"}, str(mid))
        endpoint = "https://api.bilibili.com/x/space/wbi/arc/search"
        resp = _get(endpoint, kind="sensitive", params=signed,
                    cookies=get_cookiejar(),
                    headers={**BROWSER_HEADERS,
                             "Referer": f"https://space.bilibili.com/{mid}"},
                    timeout=12)
        data = _safe_json(resp)
        if not data:
            raise RuntimeError("UP 投稿接口返回非 JSON 数据")
        if data.get("code") != 0:
            raise RuntimeError(data.get("message") or "获取 UP 投稿失败")
    except UpstreamRateLimitError:
        raise
    except Exception as e:
        raise RuntimeError(f"获取 UP {mid} 投稿失败: {e}") from e

    items = []
    for v in (data.get("data", {}) or {}).get("list", {}).get("vlist", []) or []:
        items.append(_video_item(v, owner_key=None, name_key="author",
                                 view_at_key="created", uploader_mid=mid))
    return items


_follow_cache = {"items": None, "ts": 0.0}


def fetch_follow_updates(max_ups: int = 30, ps: int = 10, force: bool = False) -> list[dict]:
    """拉取前 max_ups 个关注 UP 的最近投稿,合并按时间倒序。带 5 分钟缓存。

    arc/search 串行且由统一治理器做最小间隔/账号冷却。并发刷新通过
    single-flight 合并；明确限流时整轮失败并保留 last-good cache，普通单 UP
    错误只隔离该 UP，避免一人异常拖垮整页。
    """
    started_at = time.time()
    with _fetch_lock:
        if (not force and _follow_cache["items"] is not None
                and started_at - _follow_cache["ts"] < 300):
            return _follow_cache["items"]

    with _follow_refresh_lock:
        with _fetch_lock:
            if (_follow_cache["items"] is not None
                    and (_follow_cache["ts"] >= started_at
                         or (not force and time.time() - _follow_cache["ts"] < 300))):
                return _follow_cache["items"]

        follows = fetch_followings(1, max_ups)
        results = []
        succeeded = 0
        for i, up in enumerate(follows):
            try:
                results.extend(fetch_up_videos(up["mid"], ps))
                succeeded += 1
            except UpstreamRateLimitError:
                raise
            except Exception as e:
                log.warning("关注动态:UP %s 拉取失败:%s", up.get("mid"), e)
            if i < len(follows) - 1:
                time.sleep(1.5)

        if follows and succeeded == 0:
            raise RuntimeError("关注动态全部拉取失败，已保留上次缓存")
        results.sort(key=lambda x: x.get("view_at", 0), reverse=True)
        with _fetch_lock:
            _follow_cache.update(items=results, ts=time.time())
        return results


def reset_data_caches() -> None:
    """登录世代变化时清身份相关数据、WBI 签名器与接口间隔。

    会话锁和数据锁分开获取、不嵌套，避免与 get_cookiejar/_get_wbi_ie 形成
    锁顺序反转。账号级 412/429 冷却刻意保留，切换登录态不能绕过熔断。
    """
    global _wbi_ie
    with _fetch_lock:
        _followings_cache.update(items=[], total=0, ts=0.0)
        _follow_cache.update(items=None, ts=0.0)
    with _session_lock:
        _wbi_ie = None
    governor.reset_spacing()


def register_up(mid: int) -> dict:
    """取 UP 身份与最新投稿，并把清单项、初始游标一次性持久化。"""
    uname, face = "", ""
    card = fetch_up_card(mid)
    if card:
        uname, face = card.get("uname", ""), card.get("face", "")

    now = int(time.time())
    # 普通接口错误和明确限流都必须向上返回；只有真实空列表才使用“当前时刻”
    # 基线。否则一次网络故障会伪装成无投稿，并永久掩盖故障前的存量视频。
    page = fetch_up_videos(mid, ps=1)
    newest = page[0] if page else None
    if not uname and newest:
        uname = newest.get("uploader", "")
    if newest:
        bvid = newest.get("bvid", "")
        initial_seen = {
            "bvid": bvid,
            "ts": newest.get("view_at", 0),
            "title": newest.get("title", ""),
            "fetched_at": now,
            "same_ts_bvids": [bvid] if bvid else [],
        }
    else:
        initial_seen = {"bvid": "", "ts": now, "title": "", "fetched_at": now,
                        "same_ts_bvids": []}
    return up_list.add_up(mid, uname, face, initial_seen=initial_seen)
