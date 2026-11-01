/* 清晰度选项(前端唯一来源;与后端 up_list.QUALITY_CHOICES 保持一致)。
   [value, 显示文案];value 空串 = 最高画质自动。 */
export const QUALITY_OPTIONS = [
  ['', '最高画质(自动)'], ['2160P', '4K'],
  ['1080P60', '1080P60帧'], ['1080P', '1080P'],
  ['720P60', '720P60帧'], ['720P', '720P'], ['音频', '仅音频'],
];

export function qualityOptionsHtml(cur) {
  return QUALITY_OPTIONS.map(([v, l]) =>
    `<option value="${v}" ${v === cur ? 'selected' : ''}>${l}</option>`).join('');
}

/* 卡片窄下拉用的短标签(订阅页/合集页/收藏页共用)。从 QUALITY_OPTIONS
   派生:value 集合与 test_quality_choices_single_source 的三处一致性检查
   联动——往 OPTIONS 加档位而忘了这里的短标签,测试会因 value 集合对不上而红。 */
export const QUALITY_CARD_LABELS = new Map([
  ['', '最高画质'], ['2160P', '4K'],
  ['1080P60', '1080P60'], ['1080P', '1080P'],
  ['720P60', '720P60'], ['720P', '720P'], ['音频', '仅音频'],
]);
