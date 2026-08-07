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


def best_video_audio_choice(options: list[dict], preferred_label: str) -> str:
    """给定选项和用户选的清晰度标签,返回传给 yt-dlp 的 format 选择串。

    策略:若存在 DASH(纯视频轨),用 {视频format_id}+{最佳音频format_id};
    否则用单体 av 的 format_id。
    """
    videos = [o for o in options if o["kind"] == "video"]
    audios = [o for o in options if o["kind"] == "audio"]
    avs = [o for o in options if o["kind"] == "av"]

    target_video = next((v for v in videos if v["label"] == preferred_label), None)
    if target_video and audios:
        return f"{target_video['format_id']}+{audios[0]['format_id']}"

    target_av = next((a for a in avs if a["label"] == preferred_label), None)
    if target_av:
        return target_av["format_id"]

    # 兜底:最高视频 + 最高音频,或第一个 av
    if videos and audios:
        return f"{videos[0]['format_id']}+{audios[0]['format_id']}"
    if avs:
        return avs[0]["format_id"]
    return "best"
