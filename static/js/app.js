/* 入口:装配模块、建全局桥、启动初始化。

   页面拆成 ES modules 后函数不再天然是全局,而 index.html 与动态生成的
   HTML 里还有内联 onclick="xxx()"。这里统一把所有被内联引用的函数挂到
   window——桥只用于内联事件兼容,模块之间一律 import,不要新代码再走 window。 */
import * as lib from './lib.js';
import * as net from './net.js';
import * as theme from './theme.js';
import * as settings from './settings.js';
import * as drawer from './drawer.js';
import * as login from './login.js';

/* 页面模块:import 即向 lib 注册页控制器(顺序即侧边栏顺序,仅可读性) */
import * as downloads from './pages/downloads.js';
import * as history from './pages/history.js';
import * as feed from './pages/feed.js';
import * as seasons from './pages/seasons.js';
import * as fav from './pages/fav.js';
import * as later from './pages/later.js';

import * as newdl from './newdl.js';

/* ---------- 全局桥(内联 onclick 兼容) ---------- */
Object.assign(window, {
  // 框架/列表公共
  goPage: lib.goPage,
  refreshLib: lib.refreshLib,
  applyLibFilter: lib.applyLibFilter,
  toggleLibView: lib.toggleLibView,
  libToggleAll: lib.libToggleAll,
  toggleLibCheck: lib.toggleLibCheck,
  scrollToTop: lib.scrollToTop,
  applyFresh: lib.applyFresh,
  dismissFresh: lib.dismissFresh,
  // 抽屉 / 登录 / 设置
  openDrawer: drawer.openDrawer,
  closeDrawer: drawer.closeDrawer,
  logout: login.logout,
  showQrcode: login.showQrcode,
  useBrowser: login.useBrowser,
  uploadCookieTxt: login.uploadCookieTxt,
  saveConfigField: settings.saveConfigField,
  saveChk: settings.saveChk,
  saveDir: settings.saveDir,
  saveTheme: settings.saveTheme,
  resetConfigDir: settings.resetConfigDir,
  // 新建下载
  smartParse: newdl.smartParse,
  startDownload: newdl.startDownload,
  batchToggleAll: newdl.batchToggleAll,
  batchDownload: newdl.batchDownload,
  // 观看历史 / 稍后再看
  loadHistory: history.loadHistory,
  loadWatchlater: later.loadWatchlater,
  // 我的收藏
  favLoadFolders: fav.favLoadFolders,
  favEnter: fav.favEnter,
  favBack: fav.favBack,
  favRefresh: fav.favRefresh,
  // 追更合集
  loadSeasons: seasons.loadSeasons,
  seasonEnter: seasons.seasonEnter,
  seasonBack: seasons.seasonBack,
  seasonRefresh: seasons.seasonRefresh,
  // 关注动态(三源 + UP 清单管理)
  setFeedSource: feed.setFeedSource,
  loadFollowBili: feed.loadFollowBili,
  loadFollowAll: feed.loadFollowAll,
  loadMyUps: feed.loadMyUps,
  upAdd: feed.upAdd,
  upSetQuality: feed.upSetQuality,
  upRemove: feed.upRemove,
  upCheckNow: feed.upCheckNow,
  upAddLocal: feed.upAddLocal,
  // 下载记录
  loadDownloads: downloads.loadDownloads,
  libDownload: downloads.libDownload,
  delSelected: downloads.delSelected,
  clearAllRecords: downloads.clearAllRecords,
  clearInvalidRecords: downloads.clearInvalidRecords,
  cancelTask: downloads.cancelTask,
  revealTask: downloads.revealTask,
  resolveTaskConflict: downloads.resolveTaskConflict,
});

/* ---------- 启动 ---------- */
(async function init() {
  await settings.loadConfig();
  const stored = localStorage.getItem('bd_theme');
  const cfg = await net.getJSON('/api/config').catch(() => null);
  const t = stored || (cfg && cfg.theme) || 'auto';
  theme.applyTheme(t);
  lib.updateLibViewBtn();
  feed.syncSeg();
  await login.refreshLogin();
  lib.goPage('downloads');
  // 窗口宽度变化 → 每页条数自适应重算
  let _resizeT = null;
  window.addEventListener('resize', () => {
    clearTimeout(_resizeT);
    _resizeT = setTimeout(() => { if (lib.LIB_PAGES.includes(lib.state.mode)) lib.renderPage(); }, 200);
  });
  // 内容区独立滚动:滚过 500 显示回顶按钮;接近底部触发无限流追加
  const _scroller = document.querySelector('.content');
  _scroller.addEventListener('scroll', () => {
    document.getElementById('back-top').classList.toggle('show', _scroller.scrollTop > 500);
    if (_scroller.scrollTop + _scroller.clientHeight < _scroller.scrollHeight - 600) return;
    lib.maybeLoadMore();
  }, { passive: true });
  // 后台自动下载入队靠轮询刷新角标,与 SSE 互为兜底
  setInterval(() => { downloads.pollTasks(); }, 30000);
})();
