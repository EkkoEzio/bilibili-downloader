"""清晰度标注与 format 归一化。纯函数,无副作用,便于单测。"""
import re
from typing import Optional

# B 站 playurl 的 quality 编号:74 = 720P60、116 = 1080P60。
# 其余档位按高度标注即可(同高度多编码由码率去重)。
_BILI_FPS_QUALITY = {74: "720P60", 116: "1080P60"}


def height_to_label(height: Optional[int]) -> str:
    """像素高度转标准标签。非标准高度原样返回。"""
    if height is None:
        return "未知"
    return f"{height}P"


def _format_label(f: dict, height: Optional[int]) -> str:
    """B 站 60 帧档位用 quality 编号判定,其余按高度标注。

    不能只看 fps 字段:同一条 4K 流的 fps 可能被报成 120,按 fps 判定会把
    4K 误标成 60 帧档。quality 编号是 B 站自己给出的权威档位标识。
    """
    qid = f.get("quality")
    if isinstance(qid, int) and qid in _BILI_FPS_QUALITY:
        return _BILI_FPS_QUALITY[qid]
    return height_to_label(height)


def classify_formats(formats: list[dict]) -> list[dict]:
    """把 yt-dlp 的 formats 归一化为可选项。

    只有恰好包含一种媒体流时才归为纯音频/纯视频；vcodec、acodec 都为
    ``none`` 的 storyboard/占位格式不具备可播放媒体，直接忽略。
    """
    video_by_height: dict[int, dict] = {}
    av_by_height: dict[int, dict] = {}
    audio_best: Optional[dict] = None
    for f in formats:
        if not isinstance(f, dict):
            continue
        fid = f.get("format_id")
        if fid is None or not str(fid).strip():
            continue
        vcodec = str(f.get("vcodec") or "none").lower()
        acodec = str(f.get("acodec") or "none").lower()
        has_video = vcodec != "none"
        has_audio = acodec != "none"
        if not has_video and not has_audio:
            continue

        raw_height = f.get("height")
        try:
            height = int(raw_height) if raw_height is not None else None
        except (TypeError, ValueError):
            height = None
        try:
            tbr = float(f.get("tbr") or 0)
        except (TypeError, ValueError):
            tbr = 0.0

        if not has_video and has_audio:
            if audio_best is None or tbr > audio_best["tbr"]:
                audio_best = {"format_id": fid, "height": None, "label": "音频",
                              "kind": "audio", "tbr": tbr}
        elif has_video and not has_audio:
            label = _format_label(f, height)
            cur = video_by_height.get(label)
            if cur is None or tbr > cur["tbr"]:
                video_by_height[label] = {
                    "format_id": fid, "height": height,
                    "label": label, "kind": "video", "tbr": tbr,
                }
        else:
            label = _format_label(f, height)
            cur = av_by_height.get(label)
            if cur is None or tbr > cur["tbr"]:
                av_by_height[label] = {
                    "format_id": fid, "height": height,
                    "label": label, "kind": "av", "tbr": tbr,
                }

    def _order(entry):
        return (-(entry["height"] or 0), 0 if "P60" in entry["label"] else 1)

    result = []
    if audio_best:
        result.append(audio_best)
    result.extend(sorted(video_by_height.values(), key=_order))
    result.extend(sorted(av_by_height.values(), key=_order))
    return result


def _label_height(label: str) -> Optional[int]:
    """'1080P' / '1080P60' → 1080;非标准高度标签返回 None。"""
    m = re.match(r"(\d+)P", label or "")
    return int(m.group(1)) if m else None


def best_video_audio_choice(options: list[dict], preferred_label: str) -> str:
    """返回传给 yt-dlp 的 format 选择串，优先保证结果带声音。

    DASH 纯视频只有在存在独立音轨时才优先。没有独立音轨但存在单体 AV 时，
    选择同标签或不超过目标高度的最近 AV；裸视频仅作为没有任何有声候选时的
    最后兼容兜底。
    """
    videos = [o for o in options if o.get("kind") == "video"]
    audios = [o for o in options if o.get("kind") == "audio"]
    avs = [o for o in options if o.get("kind") == "av"]

    if preferred_label == "音频":
        if audios:
            return audios[0]["format_id"]
        if avs:
            return avs[0]["format_id"]
        if videos:
            return videos[0]["format_id"]
        return "best"

    target_video = next((v for v in videos if v.get("label") == preferred_label), None)
    if target_video and audios:
        return f"{target_video['format_id']}+{audios[0]['format_id']}"

    target_av = next((a for a in avs if a.get("label") == preferred_label), None)
    if target_av:
        return target_av["format_id"]

    h = _label_height(preferred_label)
    if h:
        if audios:
            below_v = [v for v in videos if (v.get("height") or 0) <= h]
            pick_v = below_v[0] if below_v else (videos[-1] if videos else None)
            if pick_v:
                return f"{pick_v['format_id']}+{audios[0]['format_id']}"

        below_av = [a for a in avs if (a.get("height") or 0) <= h]
        if below_av:
            return below_av[0]["format_id"]
        if avs:
            return avs[-1]["format_id"]

        # 完全没有可发声候选时才允许降级到裸视频。
        below_v = [v for v in videos if (v.get("height") or 0) <= h]
        pick_v = below_v[0] if below_v else (videos[-1] if videos else None)
        if pick_v:
            return pick_v["format_id"]

    if videos and audios:
        return f"{videos[0]['format_id']}+{audios[0]['format_id']}"
    if avs:
        return avs[0]["format_id"]
    if videos:
        return videos[0]["format_id"]
    if audios:
        return audios[0]["format_id"]
    return "best"


# 订阅档位(纯分辨率)→像素高度。与 up_list.QUALITY_CHOICES、前端
# subscriptions.js 的 UP_QUALITY_SEGMENTS 同一套档位(测试绑定两侧)。
QUALITY_TIERS = {"2160P": 2160, "2K": 1440, "1080": 1080}  # 720 档已退役(最低 1080)


def resolve_quality_label(options: list[dict], quality: str) -> str:
    """把订阅档位解析成实际可用的清晰度标签(2026-11-17 档位改版)。

    ''(最高)= 全场最高高度;具体分辨率先取该高度的第一项——classify_formats
    已按高度降序、同高度 P60 在前,所以第一项就是「同分辨率默认最高帧率」
    (144→60→30 由 B 站档位编号天然保证:116/74 这类 60 帧号在同高度里最大);
    该高度没有时降档到所选以下最高;连以下都没有(理论边缘)取全场最高。
    旧精确标签(1080P60/1080P/2160P/音频 等)不是纯分辨率档位,原样返回,
    继续走 best_video_audio_choice 的精确匹配路径。
    """
    q = (quality or "").strip()
    if q not in QUALITY_TIERS and q != "":
        return q
    tier_h = QUALITY_TIERS.get(q)  # q == ""(最高)时为 None:走全场最高分支
    if tier_h is None:
        entries_all = [o for o in options
                       if o.get("kind") in ("video", "av") and (o.get("height") or 0) > 0]
        if not entries_all:
            return ""
        entries_all.sort(key=lambda o: (-(o["height"]),
                                        0 if "P60" in o.get("label", "") else 1,
                                        0 if o.get("kind") == "video" else 1))
        return entries_all[0]["label"]
    entries = [o for o in options
               if o.get("kind") in ("video", "av") and (o.get("height") or 0) > 0]
    # video/av 两段各自有序,这里重排成跨 kind 的高度序;同高度 P60 在前,
    # 纯视频优先于单体 AV(挑流时纯视频还能搭独立音轨)
    entries.sort(key=lambda o: (-(o["height"]),
                                0 if "P60" in o.get("label", "") else 1,
                                0 if o.get("kind") == "video" else 1))
    hit = next((o for o in entries if o["height"] == tier_h), None)
    if hit:
        return hit["label"]
    below = next((o for o in entries if o["height"] < tier_h), None)
    if below:
        return below["label"]
    above = next((o for o in entries if o["height"] > tier_h), None)
    return above["label"] if above else ""
