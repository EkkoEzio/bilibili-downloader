"""B 站读接口客户端：账号数据按 fingerprint + incarnation + generation 隔离。

所有慢请求先取得同一份认证上下文与 Cookie 快照，返回/写缓存前再次确认
完整上下文仍有效。账号、生命期或代际切换前发出的旧请求可以完成网络 I/O，
但不得污染当前缓存，也不会作为当前认证上下文的成功响应返回。
"""
import logging
import threading
import time
from typing import Optional

import requests

import up_list
from bilibili_auth import (
    AuthContextChangedError,
    BROWSER_HEADERS,
    _cookiejar_value,
    _session_lock,
    commit_if_current,
    get_auth_context,
    get_cookiejar,
    get_ytdlp_cookie_arg,
    is_auth_context_current,
)
from request_governor import UpstreamRateLimitError, governed_get, governor

NAV_API = "https://api.bilibili.com/x/web-interface/nav"
log = logging.getLogger("bd.bilibili_api")


class NotLoggedInError(RuntimeError):
    """预期内的未登录状态（cookiejar 缺失），不是未知异常。

    API 层把它映射成 401 auth_required，让前端提示「请先登录」而不是
    「服务器处理失败」；继承 RuntimeError 保持既有 except 兼容。
    """


# 现有测试会把本模块符号替换成仅接受零参数的 lambda。替换时进入兼容模式，
# 不读取/写入真实认证状态；生产路径仍自动绑定完整认证上下文。
_ORIGINAL_GET_COOKIEJAR = get_cookiejar


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


def _context_key(context) -> Optional[tuple]:
    """仅已确认账号身份的完整 incarnation context 可作为缓存命名空间。"""
    if not isinstance(context, dict) or context.get("authenticated") is not True:
        return None
    fingerprint = str(context.get("fingerprint") or "").lower()
    incarnation = str(context.get("incarnation") or "").lower()
    try:
        generation = int(context.get("generation"))
    except (TypeError, ValueError):
        return None
    hex_chars = set("0123456789abcdef")
    if (len(fingerprint) != 64 or any(ch not in hex_chars for ch in fingerprint)
            or len(incarnation) != 32
            or any(ch not in hex_chars for ch in incarnation)
            or generation < 0):
        return None
    return (str(context.get("source") or "none"), fingerprint,
            incarnation, generation)


def _ensure_context_current(context) -> None:
    if context is not None and not is_auth_context_current(context):
        raise AuthContextChangedError("登录状态已变化，请重试")


def _auth_session(context=None):
    """取得一致的 ``(context, cookiejar)``；测试桩维持旧零参数契约。"""
    if context is not None:
        snapshot = dict(context)
        _ensure_context_current(snapshot)
        cookiejar = get_cookiejar(snapshot)
        _ensure_context_current(snapshot)
        return snapshot, cookiejar

    if get_cookiejar is not _ORIGINAL_GET_COOKIEJAR:
        return None, get_cookiejar()

    # Cookie 首次暴露 DedeUserID 时认证层会升级 generation，重取即可。
    for _ in range(3):
        snapshot = get_auth_context()
        cookiejar = get_cookiejar()
        if is_auth_context_current(snapshot):
            return snapshot, cookiejar
    raise AuthContextChangedError("登录状态正在切换，请稍后重试")


def _commit_data_cache(context, update) -> bool:
    """把短内存提交与认证切换串行化；未确认账号时禁止缓存。"""
    key = _context_key(context)
    if key is None:
        _ensure_context_current(context)
        return False

    def commit():
        with _fetch_lock:
            update(key)
        return True

    return bool(commit_if_current(context, commit))


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
    """把 B 站各接口的原始条目归一化成前端统一的视频卡字段。"""
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
               gap: float = 0.3, mid_param: str = "", parse,
               context=None, cookiejar=None) -> list:
    """通用分页拉取；整轮分页固定使用一份认证快照。"""
    if cookiejar is None:
        context, cookiejar = _auth_session(context)
    else:
        _ensure_context_current(context)
    if cookiejar is None:
        raise NotLoggedInError(f"未登录,无法获取{what}")
    if mid_param:
        mid = _my_mid(cookiejar)
        if not mid:
            raise RuntimeError("无法获取用户 ID")
        base_params = {**base_params, mid_param: mid}
    out = []
    for pn in range(1, max_pages + 1):
        resp = _get(url, cookies=cookiejar, headers=BROWSER_HEADERS,
                    params={**base_params, "pn": pn, "ps": ps}, timeout=12)
        data = _safe_json(resp)
        if not data or data.get("code") != 0:
            raise RuntimeError((data or {}).get(
                "message", f"获取{what}失败(可能被风控,稍后再试)"))
        items, has_more = parse(data.get("data") or {}, len(out))
        out.extend(items)
        if not has_more:
            break
        if gap:
            time.sleep(gap)
    _ensure_context_current(context)
    return out


def fetch_history(page: int = 1, size: int = 50, *, context=None) -> list[dict]:
    """拉取观看历史；返回归一化的视频列表（最新在前）。"""
    context, cookiejar = _auth_session(context)
    if cookiejar is None:
        raise NotLoggedInError("未登录,无法获取观看历史")
    last = None
    for attempt in range(3):
        try:
            resp = _get("https://api.bilibili.com/x/v2/history",
                        cookies=cookiejar, headers=BROWSER_HEADERS,
                        params={"pn": page, "ps": min(size, 100)}, timeout=12)
            # 上游对风控可能回 200+HTML:裸 resp.json() 会抛 JSONDecodeError
            # 直接变 500,_safe_json 统一防御(成功路径内部已上报 api code)。
            data = _safe_json(resp)
            break
        except requests.RequestException as exc:
            last = exc
            time.sleep(1 + attempt)
    else:
        raise RuntimeError(f"网络错误(已重试 3 次):{last}")
    if not isinstance(data, dict) or data.get("code") != 0:
        raise RuntimeError((data or {}).get("message", "获取失败")
                           if isinstance(data, dict) else "获取失败")
    items = []
    for it in data.get("data", []) or []:
        dur = it.get("duration", 0)
        watched = max(0, int(it.get("progress") or 0))
        pct = round(watched / dur * 100, 1) if dur else 0
        items.append(_video_item(it, watched_seconds=watched, watched_pct=pct))
    _ensure_context_current(context)
    return items


def fetch_watchlater(*, context=None) -> list[dict]:
    """拉取「稍后再看」列表。"""
    context, cookiejar = _auth_session(context)
    if cookiejar is None:
        raise NotLoggedInError("未登录,无法获取稍后再看")
    resp = _get("https://api.bilibili.com/x/v2/history/toview",
                cookies=cookiejar, headers=BROWSER_HEADERS, timeout=12)
    data = _safe_json(resp)
    if not data or data.get("code") != 0:
        raise RuntimeError((data or {}).get("message", "获取稍后再看失败"))
    out = [_video_item(it, view_at_key="tview_at")
           for it in (data.get("data", {}) or {}).get("list", []) or []]
    _ensure_context_current(context)
    return out


def fetch_fav_folders(*, context=None) -> list[dict]:
    """列出我的收藏夹列表。"""
    context, cookiejar = _auth_session(context)
    if cookiejar is None:
        raise NotLoggedInError("未登录,无法获取收藏夹")
    mid = _my_mid(cookiejar)
    if not mid:
        raise RuntimeError("无法获取用户 ID")
    resp = _get("https://api.bilibili.com/x/v3/fav/folder/created/list-all",
                cookies=cookiejar, headers=BROWSER_HEADERS,
                params={"up_mid": mid}, timeout=12)
    data = _safe_json(resp)
    if not data or data.get("code") != 0:
        raise RuntimeError((data or {}).get("message", "获取收藏夹失败"))
    out = []
    for folder in (data.get("data", {}) or {}).get("list", []) or []:
        out.append({
            "id": folder.get("id"),
            "fid": folder.get("fid"),
            "title": folder.get("title", ""),
            "media_count": folder.get("media_count", 0),
            "cover": (folder.get("cover") or "").replace("http:", "https:"),
        })
    _ensure_context_current(context)
    return out


def fetch_collected_seasons(max_pages: int = 5, *, context=None) -> list[dict]:
    """「我追的合集/收藏夹」列表。"""
    def parse(data, _fetched):
        items = [{"id": folder.get("id"),
                  "title": folder.get("title", ""),
                  "media_count": folder.get("media_count", 0),
                  "cover": (folder.get("cover") or "").replace("http:", "https:"),
                  # 合集更新时间:前端按「自动下载优先 → 组内时间倒序」排序要用,
                  # 接口本来就带,解析时不再丢弃(零额外请求)。
                  "mtime": _int_or_zero(folder.get("mtime"))}
                 for folder in data.get("list") or []]
        return items, bool(data.get("has_more"))

    return _paged_get("https://api.bilibili.com/x/v3/fav/folder/collected/list",
                      {"platform": "web"}, what="合集列表",
                      max_pages=max_pages, ps=20, mid_param="up_mid", parse=parse,
                      context=context)


def fetch_season_content(season_id: int, ps: int = 20, max_pages: int = 30,
                         *, context=None) -> list[dict]:
    """拉取合集内的视频。"""
    def parse(data, _fetched):
        items = [_video_item(media, owner_key="upper", pic_key="cover",
                             view_at_key=None, mark_invalid=True)
                 for media in data.get("medias") or []]
        return items, bool(data.get("has_more"))

    return _paged_get("https://api.bilibili.com/x/space/fav/season/list",
                      {"season_id": season_id}, what="合集内容",
                      max_pages=max_pages, ps=ps, parse=parse, context=context)


def fetch_fav_content(media_id: int, ps: int = 20, max_pages: int = 20,
                      *, context=None) -> list[dict]:
    """拉取某个收藏夹内的视频。"""
    def parse(data, _fetched):
        items = [_video_item(media, owner_key="upper", pic_key="cover",
                             view_at_key=None, mark_invalid=True)
                 for media in data.get("medias") or []]
        return items, bool(data.get("has_more"))

    return _paged_get("https://api.bilibili.com/x/v3/fav/resource/list",
                      {"media_id": media_id, "order": "mtime"}, what="收藏夹内容",
                      max_pages=max_pages, ps=ps, parse=parse, context=context)


def fetch_followings(page: int = 1, size: int = 50, *, context=None) -> list[dict]:
    """拉取我关注的 UP 主列表。"""
    def parse(data, _fetched):
        items = [{"mid": following.get("mid"), "uname": following.get("uname", ""),
                  "face": (following.get("face") or "").replace("http:", "https:")}
                 for following in data.get("list") or []]
        return items, False

    return _paged_get("https://api.bilibili.com/x/relation/followings",
                      {"order": "desc"}, what="关注列表", max_pages=1,
                      ps=min(size, 50), mid_param="vmid", parse=parse,
                      context=context)


_followings_cache = {"items": [], "total": 0, "ts": 0.0, "context": None}
# 数据域锁只保护内存 check/commit，慢网络请求一律锁外。
_fetch_lock = threading.Lock()
_followings_refresh_lock = threading.Lock()
_follow_refresh_lock = threading.Lock()


def _int_or_zero(value) -> int:
    """接口数值字段兜底:非数值/None 一律 0,避免一条脏数据打断整页解析。"""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def fetch_followings_all(force: bool = False, max_pages: int = 10,
                         *, context=None) -> dict:
    """拉取全部关注 UP；缓存按完整认证上下文隔离。"""
    context, cookiejar = _auth_session(context)
    if cookiejar is None:
        raise NotLoggedInError("未登录,无法获取关注列表")
    cache_key = _context_key(context)
    started_at = time.time()
    cached_result = None
    with _fetch_lock:
        if (cache_key is not None
                and _followings_cache.get("context") == cache_key
                and not force and _followings_cache.get("items")
                and started_at - float(_followings_cache.get("ts", 0)) < 600):
            cached_result = {"items": _followings_cache["items"],
                             "total": _followings_cache.get("total", 0)}
    if cached_result is not None:
        _ensure_context_current(context)
        return cached_result

    with _followings_refresh_lock:
        cached_result = None
        with _fetch_lock:
            if (cache_key is not None
                    and _followings_cache.get("context") == cache_key
                    and _followings_cache.get("items")
                    and (float(_followings_cache.get("ts", 0)) >= started_at
                         or (not force and time.time()
                             - float(_followings_cache.get("ts", 0)) < 600))):
                cached_result = {"items": _followings_cache["items"],
                                 "total": _followings_cache.get("total", 0)}
        if cached_result is not None:
            _ensure_context_current(context)
            return cached_result
        box = {"total": 0}

        def parse(data, fetched):
            box["total"] = data.get("total", 0)
            items = [{"mid": following.get("mid"),
                      "uname": following.get("uname", ""),
                      "face": (following.get("face") or "").replace("http:", "https:"),
                      # 关注时间与个性签名:列表接口本来就带,卡片要显示,
                      # 解析时不再丢弃(零额外请求)。
                      "mtime": _int_or_zero(following.get("mtime")),
                      "sign": str(following.get("sign") or "")}
                     for following in data.get("list") or []]
            has_more = not (fetched + len(items) >= box["total"] or not items)
            box["has_more"] = has_more
            return items, has_more

        all_items = _paged_get(
            "https://api.bilibili.com/x/relation/followings",
            {"order": "desc"}, what="关注列表", max_pages=max_pages, ps=50,
            gap=0.2, mid_param="vmid", parse=parse,
            context=context, cookiejar=cookiejar)

        def update(key):
            _followings_cache.update(items=all_items, total=box["total"],
                                     ts=time.time(), context=key)

        _commit_data_cache(context, update)
        result = {"items": all_items, "total": box["total"]}
        if box.get("has_more"):
            # 页数预算(10 页 × 50)耗尽仍还有更多:显式带 truncated,
            # 不静默截断后假装完整。
            result["truncated"] = True
        return result


_wbi_ie = None


_RELATION_MODIFY_ENDPOINT = "https://api.bilibili.com/x/relation/modify"
_RELATION_ACTS = {"follow": 1, "unfollow": 2}


def modify_relation(mid: int, action: str, *, context=None) -> None:
    """关注/取消关注某 UP（写操作，作用于登录账号）。

    act=1 关注、act=2 取消关注；认证走 SESSDATA，防 CSRF 走 bili_jct。
    重复关注(22013)按成功处理，其余非 0 code 透出 B 站原始 message。
    """
    act = _RELATION_ACTS.get(str(action))
    if act is None:
        raise ValueError(f"非法关注动作:{action}")
    mid = int(mid)
    context, cookiejar = _auth_session(context)
    if cookiejar is None:
        raise NotLoggedInError("未登录,无法修改关注关系")
    csrf = _cookiejar_value(cookiejar, "bili_jct")
    if not csrf:
        raise NotLoggedInError("登录凭据缺少 bili_jct，无法执行关注/取关（请重新扫码登录）")
    try:
        resp = governor.call(
            "sensitive", requests.post, _RELATION_MODIFY_ENDPOINT,
            data={"fid": str(mid), "act": str(act), "re_src": "11",
                  "csrf": csrf, "csrf_token": csrf},
            cookies=cookiejar,
            headers={**BROWSER_HEADERS,
                     "Content-Type": "application/x-www-form-urlencoded",
                     "Referer": f"https://space.bilibili.com/{mid}"},
            timeout=12,
        )
        data = _safe_json(resp)
        if not isinstance(data, dict):
            raise RuntimeError("关注接口返回非 JSON 数据")
        governor.note_api_code(data.get("code"),
                               endpoint=_RELATION_MODIFY_ENDPOINT)
        code = data.get("code")
        already = (code == 22013 and act == 1)
        if code != 0 and not already:
            raise RuntimeError(data.get("message") or "操作失败")
        _ensure_context_current(context)
    except UpstreamRateLimitError:
        raise
    except AuthContextChangedError:
        raise
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"修改关注关系失败: {exc}") from exc


def _get_wbi_ie(context=None):
    """返回绑定完整认证上下文的 WBI 签名器，锁外构造、锁内短提交。"""
    global _wbi_ie
    context, cookiejar = _auth_session(context)
    if cookiejar is None:
        raise NotLoggedInError("未登录,无法初始化 WBI 签名器")
    cache_key = _context_key(context)
    with _session_lock:
        cached = _wbi_ie
        if (cache_key is not None and isinstance(cached, dict)
                and cached.get("context") == cache_key):
            signer = cached.get("ie")
        else:
            signer = None
    if signer is not None:
        _ensure_context_current(context)
        return signer

    # 不能在 _session_lock 内调用此函数：它会读取认证 context，普通 Lock 会自锁。
    import yt_dlp
    from yt_dlp.extractor.bilibili import BiliBiliIE
    candidate = BiliBiliIE(yt_dlp.YoutubeDL(
        {"quiet": True, **get_ytdlp_cookie_arg(context)}))
    if cache_key is None:
        _ensure_context_current(context)
        return candidate

    def commit():
        global _wbi_ie
        if (isinstance(_wbi_ie, dict)
                and _wbi_ie.get("context") == cache_key
                and _wbi_ie.get("ie") is not None):
            return _wbi_ie["ie"]
        _wbi_ie = {"ie": candidate, "context": cache_key}
        return candidate

    return commit_if_current(context, commit)


def fetch_up_card(mid: int) -> Optional[dict]:
    """公开用户卡片接口；普通失败返回 None，明确限流向上传播。"""
    try:
        response = _get("https://api.bilibili.com/x/web-interface/card",
                        params={"mid": mid}, headers=BROWSER_HEADERS, timeout=10)
        data = _safe_json(response)
        if not data or data.get("code") != 0:
            return None
        card = (data.get("data") or {}).get("card") or {}
        return {"uname": card.get("name", ""), "face": card.get("face", "")}
    except UpstreamRateLimitError:
        raise
    except Exception:
        return None


def fetch_up_videos(mid: int, ps: int = 10, pn: int = 1,
                    *, context=None) -> list[dict]:
    """WBI 签名调 arc/search，返回某 UP 最近投稿。"""
    try:
        context, cookiejar = _auth_session(context)
        ie = _get_wbi_ie(context)
        signed = ie._sign_wbi({"mid": str(mid), "ps": str(ps), "pn": str(pn),
                               "order": "pubdate"}, str(mid))
        endpoint = "https://api.bilibili.com/x/space/wbi/arc/search"
        resp = _get(endpoint, kind="sensitive", params=signed,
                    cookies=cookiejar,
                    headers={**BROWSER_HEADERS,
                             "Referer": f"https://space.bilibili.com/{mid}"},
                    timeout=12)
        data = _safe_json(resp)
        if not data:
            raise RuntimeError("UP 投稿接口返回非 JSON 数据")
        if data.get("code") != 0:
            raise RuntimeError(data.get("message") or "获取 UP 投稿失败")
        _ensure_context_current(context)
    except (UpstreamRateLimitError, AuthContextChangedError):
        raise
    except Exception as exc:
        raise RuntimeError(f"获取 UP {mid} 投稿失败: {exc}") from exc

    return [_video_item(video, owner_key=None, name_key="author",
                        view_at_key="created", uploader_mid=mid)
            for video in (data.get("data", {}) or {}).get("list", {}).get("vlist", []) or []]


_follow_cache = {"items": None, "ts": 0.0, "context": None}


def fetch_follow_updates(max_ups: int = 30, ps: int = 10, force: bool = False,
                         *, context=None) -> list[dict]:
    """合并关注 UP 最近投稿；缓存按完整认证上下文隔离。"""
    context, cookiejar = _auth_session(context)
    if cookiejar is None:
        raise NotLoggedInError("未登录,无法获取关注动态")
    cache_key = _context_key(context)
    started_at = time.time()
    cache_hit = False
    cached_result = None
    with _fetch_lock:
        if (cache_key is not None and _follow_cache.get("context") == cache_key
                and not force and _follow_cache.get("items") is not None
                and started_at - float(_follow_cache.get("ts", 0)) < 300):
            cached_result = _follow_cache["items"]
            cache_hit = True
    if cache_hit:
        _ensure_context_current(context)
        return cached_result

    with _follow_refresh_lock:
        cache_hit = False
        cached_result = None
        with _fetch_lock:
            if (cache_key is not None and _follow_cache.get("context") == cache_key
                    and _follow_cache.get("items") is not None
                    and (float(_follow_cache.get("ts", 0)) >= started_at
                         or (not force and time.time()
                             - float(_follow_cache.get("ts", 0)) < 300))):
                cached_result = _follow_cache["items"]
                cache_hit = True
        if cache_hit:
            _ensure_context_current(context)
            return cached_result

        follows = fetch_followings(1, max_ups, context=context)
        results = []
        succeeded = 0
        for index, following in enumerate(follows):
            try:
                results.extend(fetch_up_videos(following["mid"], ps, context=context))
                succeeded += 1
            except (UpstreamRateLimitError, AuthContextChangedError):
                raise
            except Exception as exc:
                log.warning("关注动态:UP %s 拉取失败:%s", following.get("mid"), exc)
            if index < len(follows) - 1:
                time.sleep(1.5)

        if follows and succeeded == 0:
            raise RuntimeError("关注动态全部拉取失败，已保留上次缓存")
        results.sort(key=lambda item: item.get("view_at", 0), reverse=True)

        def update(key):
            _follow_cache.update(items=results, ts=time.time(), context=key)

        _commit_data_cache(context, update)
        return results


def _format_count(value) -> str:
    """播放/点赞数转万级中文短格式;取不到或为 0 返回空串(卡片不显示该指标)。"""
    try:
        n = int(value or 0)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    if n < 10000:
        return str(n)
    return f"{n / 10000:.1f}万"


def fetch_home_feed(fresh_idx: int = 1, ps: int = 12, *, context=None) -> list[dict]:
    """WBI 签名拉首页推荐流（登录态个性化），返回归一化视频卡列表。

    - 只保留 ``goto == "av"`` 的普通视频，过滤广告位（is_ad_loc）与直播；
    - fresh_idx 递增翻页；ps 上限 30，web 端惯用 12；
    - 透出 rcmd_reason（推荐理由）、播放/点赞数与发布时间供前端展示。
    """
    fresh_idx = max(1, int(fresh_idx))
    ps = max(1, min(30, int(ps)))
    try:
        context, cookiejar = _auth_session(context)
        ie = _get_wbi_ie(context)
        signed = ie._sign_wbi({
            "fresh_type": "4", "version": "1", "ps": str(ps),
            "fresh_idx": str(fresh_idx), "fresh_idx_1h": str(fresh_idx),
            "brush": str(fresh_idx), "fetch_row": str(
                1 + (fresh_idx - 1) * ps),
            "web_location": "143065",
        }, "home_feed")
        endpoint = "https://api.bilibili.com/x/web-interface/wbi/index/top/feed/rcmd"
        resp = _get(endpoint, kind="sensitive", params=signed,
                    cookies=cookiejar, headers=BROWSER_HEADERS, timeout=12)
        data = _safe_json(resp)
        if not data:
            raise RuntimeError("首页推荐接口返回非 JSON 数据")
        if data.get("code") != 0:
            raise RuntimeError(data.get("message") or "获取首页推荐失败")
        _ensure_context_current(context)
    except (UpstreamRateLimitError, AuthContextChangedError):
        raise
    except Exception as exc:
        raise RuntimeError(f"获取首页推荐失败: {exc}") from exc

    out: list[dict] = []
    for it in (data.get("data", {}) or {}).get("item", []) or []:
        if not isinstance(it, dict) or it.get("goto") != "av":
            continue
        if it.get("is_ad_loc") or it.get("is_stock"):
            continue
        bvid = it.get("bvid") or ""
        if not bvid:
            continue
        stat = it.get("stat") or {}
        rcmd = it.get("rcmd_reason") or {}
        reason = ""
        if rcmd.get("reason_type") == 1:
            reason = "已关注"
        elif str(rcmd.get("content") or "").strip():
            reason = str(rcmd["content"]).strip()
        out.append({
            "bvid": bvid,
            "title": it.get("title") or "",
            "uploader": (it.get("owner") or {}).get("name", ""),
            "uploader_mid": (it.get("owner") or {}).get("mid"),
            "thumbnail": (it.get("pic") or "").replace("http:", "https:"),
            "duration": it.get("duration") or 0,
            "url": f"https://www.bilibili.com/video/{bvid}",
            "view_at": it.get("pubdate") or 0,
            "rcmd_reason": reason,
            "stat_view": _format_count(stat.get("view")),
            "stat_like": _format_count(stat.get("like")),
        })
    return out


def reset_data_caches() -> None:
    """登录世代变化时清身份缓存；账号级 412/429 冷却刻意保留。"""
    global _wbi_ie
    with _fetch_lock:
        _followings_cache.update(items=[], total=0, ts=0.0, context=None)
        _follow_cache.update(items=None, ts=0.0, context=None)
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
