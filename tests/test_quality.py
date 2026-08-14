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


# ---------- 仅音频 / 就近降档 ----------

def _dash_opts():
    return [
        {"format_id": "30280", "kind": "audio", "label": "音频", "tbr": 200},
        {"format_id": "v1080", "kind": "video", "label": "1080P", "height": 1080, "tbr": 3000},
        {"format_id": "v720", "kind": "video", "label": "720P", "height": 720, "tbr": 1500},
        {"format_id": "v480", "kind": "video", "label": "480P", "height": 480, "tbr": 800},
    ]


def test_choice_audio_only():
    """选「音频」直接返回音频轨 format_id,不与视频合并。"""
    from quality import best_video_audio_choice
    assert best_video_audio_choice(_dash_opts(), "音频") == "30280"


def test_choice_audio_but_no_audio_track_falls_back():
    """源没有独立音频轨时选「音频」,兜底到最佳视频(不崩溃)。"""
    from quality import best_video_audio_choice
    opts = [o for o in _dash_opts() if o["kind"] != "audio"]
    assert best_video_audio_choice(opts, "音频") == "v1080"


def test_choice_missing_label_downgrades_to_nearest_below():
    """选 1080P 但源最高 720P:降到 720P,而不是现在的「兜底最高=720」歧义;
    关键场景:选 720P 而源只有 1080/480 → 选 480(不超过所选清晰度)。"""
    from quality import best_video_audio_choice
    opts = [o for o in _dash_opts() if o["format_id"] != "v720"]
    assert best_video_audio_choice(opts, "720P") == "v480+30280"


def test_choice_missing_label_no_lower_takes_lowest():
    """选 360P 但源最低 480P:取最低档 480(宁可低也别超)。"""
    from quality import best_video_audio_choice
    assert best_video_audio_choice(_dash_opts(), "360P") == "v480+30280"


def test_choice_missing_label_downgrades_av():
    from quality import best_video_audio_choice
    opts = [{"format_id": "mp4-720", "kind": "av", "label": "720P", "height": 720, "tbr": 800}]
    assert best_video_audio_choice(opts, "1080P") == "mp4-720"


def test_choice_empty_label_still_best():
    """空标签(自动)仍是最高画质,行为不变。"""
    from quality import best_video_audio_choice
    assert best_video_audio_choice(_dash_opts(), "") == "v1080+30280"
