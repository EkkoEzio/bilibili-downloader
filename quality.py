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


def label_to_label_cn(label: str) -> str:
    """英文标签转中文友好显示。"""
    mapping = {
        "2160P": "4K 超清",
        "1440P": "2K 超清",
        "1080P": "1080P 高清",
        "720P": "720P",
        "480P": "480P",
        "360P": "360P 流畅",
    }
    return mapping.get(label, label)


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
