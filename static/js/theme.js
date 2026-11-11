/* 主题:light / dark / auto(跟随系统)。localStorage 持久化,设置里可改。 */

export function applyTheme(theme) {
  if (theme === 'auto') {
    document.body.classList.remove('dark');
    const sysDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
    if (sysDark) document.body.classList.add('dark');
  } else if (theme === 'dark') {
    document.body.classList.add('dark');
  } else {
    document.body.classList.remove('dark');
  }
}
