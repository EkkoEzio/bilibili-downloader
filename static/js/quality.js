/* 清晰度选项(前端唯一来源;与后端 up_list.QUALITY_CHOICES 保持一致)。
   [value, 显示文案];value 空串 = 最高画质自动。 */
export const QUALITY_OPTIONS = [
  ['', '最高画质(自动)'], ['2160P', '4K'],
  ['1080P60', '1080P60帧'], ['1080P', '1080P'],
  ['720P60', '720P60帧'], ['720P', '720P'], ['音频', '仅音频'],
];

/* ---------- 自动下载分段档位(关注 UP / 追更合集 / 我的收藏 三处共用) ----------
   [value, 胶囊文案];__off=不自动下载,''=最高(同分辨率默认最高帧率)。
   2026-11-17 定稿:纯分辨率 + 仅音频,60 帧独立档移除,「最高画质」更名「最高」。
   2026-11-22 定稿:720 档退役,最低可选 1080——视频最高不足 1080 时按所选
   以下最高可用档下载(quality.resolve_quality_label 落实)。
   与后端 up_list.QUALITY_CHOICES 一致(测试绑定两侧)。 */
export const AUTO_QUALITY_SEGMENTS = [
  ["__off", "不自动下载"], ["", "最高"], ["2160P", "4K"], ["2K", "2K"], ["1080", "1080"], ["音频", "仅音频"],
];
/* 存量旧档位(精确标签/已下线档)归一:与后端 up_list._norm_quality 同一套映射;
   旧 720 选择就近归一到新的最低档 1080 */
const AUTO_LEGACY_QUALITY = { "1080P60": "1080", "1080P": "1080", "720P60": "1080", "720P": "1080", "720": "1080" };
export function normAutoQuality(q) {
  q = String(q || "");
  return AUTO_QUALITY_SEGMENTS.some(([v]) => v === q) ? q : (AUTO_LEGACY_QUALITY[q] ?? "");
}
