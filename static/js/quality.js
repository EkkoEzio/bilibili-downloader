/* 清晰度选项(前端唯一来源;与后端 up_list.QUALITY_CHOICES 保持一致)。
   [value, 显示文案];value 空串 = 最高画质自动。 */
export const QUALITY_OPTIONS = [
  ['', '最高画质(自动)'], ['1080P', '1080P'], ['720P', '720P'],
  ['480P', '480P'], ['360P', '360P'], ['音频', '仅音频'],
];

export function qualityOptionsHtml(cur) {
  return QUALITY_OPTIONS.map(([v, l]) =>
    `<option value="${v}" ${v === cur ? 'selected' : ''}>${l}</option>`).join('');
}
