/* 清晰度选项(前端唯一来源;与后端 up_list.QUALITY_CHOICES 保持一致)。
   [value, 显示文案];value 空串 = 最高画质自动。 */
export const QUALITY_OPTIONS = [
  ['', '最高画质(自动)'], ['2160P', '4K'],
  ['1080P60', '1080P60帧'], ['1080P', '1080P'],
  ['720P60', '720P60帧'], ['720P', '720P'], ['音频', '仅音频'],
];

/* 卡片窄下拉用的短标签(订阅页/合集页/收藏页共用)。从 QUALITY_OPTIONS
   派生:value 集合与 test_quality_choices_single_source 的三处一致性检查
   联动——往 OPTIONS 加档位而忘了这里的短标签,测试会因 value 集合对不上而红。 */
export const QUALITY_CARD_LABELS = new Map([
  ['', '最高画质'], ['2160P', '4K'],
  ['1080P60', '1080P60'], ['1080P', '1080P'],
  ['720P60', '720P60'], ['720P', '720P'], ['音频', '仅音频'],
]);

/* ---------- 自动下载分段档位(关注 UP / 追更合集 / 我的收藏 三处共用) ----------
   [value, 胶囊文案];__off=不自动下载,''=最高(同分辨率默认最高帧率)。
   2026-11-17 定稿:纯分辨率 + 仅音频,60 帧独立档移除,「最高画质」更名「最高」。
   与后端 up_list.QUALITY_CHOICES 一致(测试绑定两侧);没有所选档位时自动降档到
   所选以下最高,由 quality.resolve_quality_label 在挑流时落实。 */
export const AUTO_QUALITY_SEGMENTS = [
  ["__off", "不自动下载"], ["", "最高"], ["2160P", "4K"], ["2K", "2K"], ["1080", "1080"], ["720", "720"], ["音频", "仅音频"],
];
/* 存量旧档位(精确标签/已下线档)归一:与后端 up_list._norm_quality 同一套映射 */
const AUTO_LEGACY_QUALITY = { "1080P60": "1080", "1080P": "1080", "720P60": "720", "720P": "720" };
export function normAutoQuality(q) {
  q = String(q || "");
  return AUTO_QUALITY_SEGMENTS.some(([v]) => v === q) ? q : (AUTO_LEGACY_QUALITY[q] ?? "");
}
