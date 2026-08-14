"""清晰度标注与 format 归一化。纯函数,无副作用,便于单测。"""
from typing import Optional


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


def classify_formats(formats: list[dict]) -> list[dict]:
    """把 yt-dlp 的 formats 归一化为可选项。

    - 纯音频(vcodec=none)→ kind=audio, label=音频
    - 纯视频(acodec=none)→ kind=video, 按 height 去重保留最高码率
    - 单体音视频 → kind=av, 按 height 去重保留最高码率
    """
    video_by_height: dict[int, dict] = {}
    av_by_height: dict[int, dict] = {}
    audio_best: Optional[dict] = None
    for f in formats:
        vcodec = (f.get("vcodec") or "none").lower()
        acodec = (f.get("acodec") or "none").lower()
        height = f.get("height")
        tbr = f.get("tbr") or 0
        fid = f.get("format_id")
        if vcodec == "none":  # 纯音频
            if audio_best is None or tbr > audio_best["tbr"]:
                audio_best = {"format_id": fid, "height": None, "label": "音频",
                              "kind": "audio", "tbr": tbr}
        elif acodec == "none":  # 纯视频
            h = height or 0
            cur = video_by_height.get(h)
            if cur is None or tbr > cur["tbr"]:
                video_by_height[h] = {"format_id": fid, "height": height,
                                      "label": height_to_label(height), "kind": "video", "tbr": tbr}
        else:  # 单体音视频
            h = height or 0
            cur = av_by_height.get(h)
            if cur is None or tbr > cur["tbr"]:
                av_by_height[h] = {"format_id": fid, "height": height,
                                   "label": height_to_label(height), "kind": "av", "tbr": tbr}

    result = []
    if audio_best:
        result.append(audio_best)
    # 视频按高度降序
    for h in sorted(video_by_height, reverse=True):
        result.append(video_by_height[h])
    for h in sorted(av_by_height, reverse=True):
        result.append(av_by_height[h])
    return result


def _label_height(label: str) -> Optional[int]:
    """'1080P' → 1080;非标准高度标签返回 None。"""
    if label and label.endswith("P") and label[:-1].isdigit():
        return int(label[:-1])
    return None


def best_video_audio_choice(options: list[dict], preferred_label: str) -> str:
    """给定选项和用户选的清晰度标签,返回传给 yt-dlp 的 format 选择串。

    策略:
    - 「音频」→ 直接用最佳音频轨(不合并视频)
    - 精确匹配 DASH 视频轨 → {视频format_id}+{最佳音频format_id}
    - 精确匹配单体 av → format_id
    - 标签没匹配上(如选 1080P 源只有 720P)→ 就近降档:
      取不超过所选高度的最高档;一档都没有则取最低档(宁可低不超高)
    - 空标签(自动)→ 最高画质
    """
    videos = [o for o in options if o["kind"] == "video"]
    audios = [o for o in options if o["kind"] == "audio"]
    avs = [o for o in options if o["kind"] == "av"]

    if preferred_label == "音频" and audios:
        return audios[0]["format_id"]

    target_video = next((v for v in videos if v["label"] == preferred_label), None)
    if target_video and audios:
        return f"{target_video['format_id']}+{audios[0]['format_id']}"
    if target_video:  # 无独立音频轨的罕见情况
        return target_video["format_id"]

    target_av = next((a for a in avs if a["label"] == preferred_label), None)
    if target_av:
        return target_av["format_id"]

    # 用户指定了具体清晰度但源没有 → 就近降档(videos/avs 均按高度降序)
    h = _label_height(preferred_label)
    if h:
        below_v = [v for v in videos if (v.get("height") or 0) <= h]
        pick_v = below_v[0] if below_v else (videos[-1] if videos else None)
        if pick_v and audios:
            return f"{pick_v['format_id']}+{audios[0]['format_id']}"
        if pick_v:
            return pick_v["format_id"]
        below_av = [a for a in avs if (a.get("height") or 0) <= h]
        if below_av:
            return below_av[0]["format_id"]
        if avs:
            return avs[-1]["format_id"]

    # 兜底:最高视频 + 最高音频,或第一个 av
    if videos and audios:
        return f"{videos[0]['format_id']}+{audios[0]['format_id']}"
    if videos:
        return videos[0]["format_id"]
    if avs:
        return avs[0]["format_id"]
    return "best"
