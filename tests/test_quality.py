from quality import height_to_label, label_to_label_cn, classify_formats


def test_height_to_label():
    assert height_to_label(360) == "360P"
    assert height_to_label(720) == "720P"
    assert height_to_label(1080) == "1080P"
    assert height_to_label(1440) == "1440P"
    assert height_to_label(2160) == "2160P"
    assert height_to_label(None) == "未知"
    assert height_to_label(480) == "480P"


def test_label_cn():
    assert label_to_label_cn("2160P") == "4K 超清"
    assert label_to_label_cn("1080P") == "1080P 高清"
    assert label_to_label_cn("720P") == "720P"
    assert label_to_label_cn("360P") == "360P 流畅"


def test_classify_formats_separates_av():
    formats = [
        {"format_id": "30280", "vcodec": "none", "acodec": "mp4a", "height": None, "tbr": 200},  # 纯音频
        {"format_id": "30016", "vcodec": "avc1", "acodec": "none", "height": 360, "tbr": 350},   # 纯视频
        {"format_id": "mp4-1", "vcodec": "avc1", "acodec": "mp4a", "height": 720, "tbr": 800},    # 单体音视频
    ]
    out = classify_formats(formats)
    by_id = {x["format_id"]: x for x in out}
    assert by_id["30280"]["kind"] == "audio"
    assert by_id["30280"]["label"] == "音频"
    assert by_id["30016"]["kind"] == "video"
    assert by_id["30016"]["label"] == "360P"
    assert by_id["mp4-1"]["kind"] == "av"
    assert by_id["mp4-1"]["label"] == "720P"


def test_classify_formats_dedup_by_height():
    # B 站同高度常有 h264/h265/av1 多编码,只保留最高码率的一条
    formats = [
        {"format_id": "a", "vcodec": "avc1", "acodec": "none", "height": 1080, "tbr": 1000},
        {"format_id": "b", "vcodec": "hev1", "acodec": "none", "height": 1080, "tbr": 1500},
    ]
    out = [x for x in classify_formats(formats) if x["kind"] == "video"]
    assert len(out) == 1
    assert out[0]["format_id"] == "b"


def test_choice_dash():
    from quality import best_video_audio_choice
    opts = [
        {"format_id": "30280", "kind": "audio", "label": "音频", "tbr": 200},
        {"format_id": "v1080", "kind": "video", "label": "1080P", "tbr": 3000},
        {"format_id": "v720", "kind": "video", "label": "720P", "tbr": 1500},
    ]
    assert best_video_audio_choice(opts, "1080P") == "v1080+30280"


def test_choice_av_only():
    from quality import best_video_audio_choice
    opts = [{"format_id": "mp4-1", "kind": "av", "label": "720P", "tbr": 800}]
    assert best_video_audio_choice(opts, "720P") == "mp4-1"
