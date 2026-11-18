/* 播放器:封面正中那颗播放按钮点了就播,一个弹层两种源。

   · 已下载 → 本地文件(/api/stream/<task_id>,Range 可拖进度)。
   · 没下载 → 联网播放:后端用应用已登录的 Cookie 解析出 B 站 DASH 直链,
     前端通过 /online/stream/<token> 拿视频轨与音频轨。

   在线为什么不用 B 站 iframe:iframe 是第三方环境,浏览器默认拦跨站 Cookie,
   B 站认不出登录态,只给 360P/480P。改成自己播之后,用的就是应用里那个
   已登录账号的档位(实测能到 1080P),顺带拿到了 Range,进度条能拖。

   DASH 的音视频是两条独立流,浏览器 <video> 不会自己合起来,所以这里用
   <video> 打画面、<audio> 出声,并按同一时间轴同步。控制条自己搭一条:
   原生控件的音量键对一个没有音轨的 <video> 是无效的,留着只会让人以为坏了。 */

import { formatBytes, formatDuration, bvFromUrl, pageFromUrl } from './util.js';

const POS_PREFIX = 'bd_play_pos:';
const SPEED_KEY = 'bd_play_speed';
const VOLUME_KEY = 'bd_play_volume';
const SAVE_INTERVAL_MS = 5000;
const RESUME_MIN_SECONDS = 15;
// 倍速档位:平铺成胶囊,点一下就切(不用滑条来回挪)
const SPEEDS = { steps: [0.5, 0.75, 1, 1.25, 1.5, 2], fallback: 1 };
const DRIFT_TOLERANCE = 0.25;   // 秒;超过就纠正音轨,再大就听得出不同步
const VOLUME_ICON = '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M3 10v4h4l5 5V5L7 10H3z"/><path d="M14 8.5a4 4 0 0 1 0 7" fill="none" stroke="currentColor" stroke-width="2"/></svg>';
const MUTE_ICON = '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M3 10v4h4l5 5V5L7 10H3z"/><path d="M16 9l5 5M21 9l-5 5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>';

let currentId = '';
let currentMode = '';
let lastFocus = null;
let lastSaveAt = 0;
let onlineState = { qualities: [], index: 0, audioToken: '' };
let onlineUrl = '';          // 在线播放的原始链接:断流自恢复时重新解析用
let onlineMeta = {};         // 打开播放器时的卡片元信息,恢复时还原标题
let onlineRecoverLeft = 0;   // 剩余自动恢复次数,起播成功即回满

function byId(id) { return document.getElementById(id); }

function isOpen() {
  const overlay = byId('player');
  return !!overlay && overlay.classList.contains('open');
}

function video() { return byId('player-video'); }
function audio() { return byId('player-audio'); }

function readNumber(key, fallback) {
  try {
    const value = Number(localStorage.getItem(key));
    return Number.isFinite(value) && value > 0 ? value : fallback;
  } catch (error) {
    return fallback;
  }
}

function writeNumber(key, value) {
  try { localStorage.setItem(key, String(value)); } catch (error) { /* 隐私模式忽略 */ }
}

function readPosition(taskId) {
  try {
    const value = Number(localStorage.getItem(POS_PREFIX + taskId));
    return Number.isFinite(value) && value > 0 ? value : 0;
  } catch (error) {
    return 0;
  }
}

function savePosition(taskId, seconds) {
  if (!taskId || !Number.isFinite(seconds) || seconds < 3) return;
  writeNumber(POS_PREFIX + taskId, Math.floor(seconds));
}

function setTip(text) {
  const tip = byId('player-tip');
  if (!tip) return;
  tip.textContent = text || '';
  tip.hidden = !text;
}

/* ---------- 控制条 ---------- */

function setPlayIcon(playing) {
  const button = byId('pl-toggle');
  if (!button) return;
  button.dataset.state = playing ? 'playing' : 'paused';
  button.setAttribute('aria-label', playing ? '暂停' : '播放');
  button.innerHTML = playing
    ? '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/></svg>'
    : '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><polygon points="7 4 19 12 7 20 7 4"/></svg>';
}

function setTimeLabel(current, total) {
  const label = byId('pl-time');
  if (!label) return;
  const fmt = (value) => (Number.isFinite(value) && value > 0 ? formatDuration(value) : '0:00');
  label.textContent = `${fmt(current)} / ${fmt(total)}`;
}

function syncSeekBar(current, total) {
  const bar = byId('pl-seek');
  if (!bar) return;
  const ratio = total > 0 ? current / total : 0;
  bar.value = String(Math.round(Math.min(1, Math.max(0, ratio)) * 1000));
  bar.style.setProperty('--played', `${(ratio * 100).toFixed(2)}%`);
}

function formatSpeed(value) {
  const rounded = Math.round(value * 100) / 100;
  return `${String(rounded).replace(/\.0$/, '')}×`;
}

/* 倍速:一排胶囊,点一下直接切到那一档。
   以前是滑条,想切回 1× 得把滑块慢慢挪回中间(用户明确说难用)。 */
function buildSpeedSegs() {
  const box = byId('pl-speed-segs');
  if (!box || box.children.length) return;
  for (const speed of SPEEDS.steps) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'pl-seg';
    button.dataset.speed = String(speed);
    button.textContent = formatSpeed(speed);
    button.setAttribute('aria-pressed', 'false');
    box.appendChild(button);
  }
}

function markSpeedSegs(speed) {
  document.querySelectorAll('#pl-speed-segs .pl-seg').forEach((button) => {
    const on = Number(button.dataset.speed) === speed;
    button.classList.toggle('on', on);
    button.setAttribute('aria-pressed', on ? 'true' : 'false');
  });
}

function applySpeed(value) {
  const speed = Number(value) || 1;
  const v = video();
  const a = audio();
  if (v) v.playbackRate = speed;
  if (a && !a.hidden) a.playbackRate = speed;
  markSpeedSegs(speed);
  writeNumber(SPEED_KEY, speed);
}

function applyVolume(value) {
  const volume = Math.min(1, Math.max(0, value));
  const a = audio();
  const v = video();
  // 在线时 <video> 没有音轨,声音全在 <audio> 上;本地则直接是 <video>。
  if (a && !a.hidden) a.volume = volume;
  else if (v) v.volume = volume;
  const slider = byId('pl-volume');
  if (slider) slider.value = String(Math.round(volume * 100));
  writeNumber(VOLUME_KEY, volume);
}

/* 静音状态与音量分开:静音不改变音量值,取消静音恢复原音量。
   以前 mute 按钮调 applyVolume(0) 会把音量也存成 0,下次取消时读到 0
   又回到静音,永远打不开声音。 */
function setMuted(muted) {
  const a = audio();
  const v = video();
  if (a && !a.hidden) a.muted = muted;
  if (v) v.muted = muted;
  const button = byId('pl-mute');
  if (button) {
    button.dataset.state = muted ? 'muted' : 'volume';
    button.setAttribute('aria-label', muted ? '取消静音' : '静音');
    button.innerHTML = muted ? MUTE_ICON : VOLUME_ICON;
  }
}

function isMuted() {
  const a = audio();
  const v = video();
  if (a && !a.hidden) return a.muted;
  return v ? v.muted : false;
}

function setMode(mode) {
  const isLocal = mode === 'local';
  const a = audio();
  const local = byId('pl-local-controls');
  const quality = byId('pl-quality-wrap');
  if (a) {
    a.hidden = isLocal;   // 本地声音在 <video> 里,不需要外挂音轨
    if (isLocal) {
      a.pause();
      a.removeAttribute('src');
    }
  }
  if (local) local.hidden = false;
  if (quality) quality.hidden = isLocal;
}

function setHeader(meta) {
  const title = byId('player-title');
  const sub = byId('player-sub');
  if (title) title.textContent = meta.title || '播放';
  const parts = [];
  if (meta.uploader) parts.push(meta.uploader);
  if (meta.duration) parts.push(formatDuration(meta.duration));
  if (meta.size) parts.push(formatBytes(meta.size));
  if (sub) sub.textContent = parts.join(' · ');
}

function setReveal(taskId) {
  const reveal = byId('player-reveal');
  if (!reveal) return;
  reveal.hidden = !taskId;
  reveal.onclick = () => {
    if (typeof window.revealTask === 'function') window.revealTask(taskId);
  };
}

function setExternal(url) {
  const external = byId('player-external');
  if (!external) return;
  external.hidden = !url;
  external.onclick = () => { if (url) window.open(url, '_blank', 'noopener'); };
}

function openOverlay() {
  const overlay = byId('player');
  if (!overlay) return;
  if (!isOpen()) lastFocus = document.activeElement;
  overlay.classList.add('open');
  overlay.setAttribute('aria-hidden', 'false');
  document.body.classList.add('player-open');
}

/* ---------- 音画同步(仅在线) ---------- */

function syncAudioToVideo() {
  const v = video();
  const a = audio();
  if (!v || !a || a.hidden || !a.src) return;
  if (Math.abs(a.currentTime - v.currentTime) > DRIFT_TOLERANCE) {
    try { a.currentTime = v.currentTime; } catch (error) { /* 忽略 */ }
  }
}

function wireVideoEvents() {
  const v = video();
  if (!v) return;
  v.onplay = () => {
    onlineRecoverLeft = 2;   // 真正播起来,恢复额度回满
    setPlayIcon(true);
    const a = audio();
    if (a && !a.hidden && a.src) {
      a.currentTime = v.currentTime;
      const started = a.play();
      if (started && started.catch) started.catch(() => {});
    }
  };
  v.onpause = () => {
    setPlayIcon(false);
    const a = audio();
    if (a && !a.hidden) a.pause();
  };
  v.onerror = () => {
    // 在线流断了(token 过期/上游断开)时浏览器只会无声停住,看起来像「自动暂停」;
    // 这里自动重新解析并从当前进度续播,重复失败才提示手动重开。
    if (currentMode === 'online' && isOpen()) recoverOnlineStream();
  };
  v.onseeked = () => {
    syncAudioToVideo();
    syncSeekBar(v.currentTime, v.duration);
  };
  v.ontimeupdate = () => {
    syncAudioToVideo();
    syncSeekBar(v.currentTime, v.duration);
    setTimeLabel(v.currentTime, v.duration);
    const now = Date.now();
    if (now - lastSaveAt < SAVE_INTERVAL_MS) return;
    lastSaveAt = now;
    if (currentMode === 'local') savePosition(currentId, v.currentTime);
  };
  v.onloadedmetadata = () => {
    setTimeLabel(v.currentTime, v.duration);
    syncSeekBar(v.currentTime, v.duration);
  };
  v.ondurationchange = () => setTimeLabel(v.currentTime, v.duration);
}

/* ---------- 本地播放 ---------- */

export function openLocalPlayer(taskId, meta = {}) {
  const id = String(taskId || '');
  const v = video();
  if (!id || !v) return;

  currentId = id;
  currentMode = 'local';
  buildSpeedSegs();
  setMode('local');
  setHeader(meta);
  setTip('');
  setReveal(id);
  setExternal(meta.url || '');
  openOverlay();

  const resumeAt = readPosition(id);
  const onMeta = () => {
    v.removeEventListener('loadedmetadata', onMeta);
    const total = Number(v.duration);
    if (resumeAt >= RESUME_MIN_SECONDS
        && (!Number.isFinite(total) || resumeAt < total - RESUME_MIN_SECONDS)) {
      try {
        v.currentTime = resumeAt;
        setTip(`从 ${formatDuration(resumeAt)} 继续播放`);
      } catch (error) { /* seek 失败就当从头播 */ }
    }
  };
  v.addEventListener('loadedmetadata', onMeta);
  v.addEventListener('error', () => {
    setTip('浏览器播不了这个封装(常见于 mkv),点右上角「Finder」用系统播放器打开');
  }, { once: true });

  applySpeed(readNumber(SPEED_KEY, 1));
  applyVolume(readNumber(VOLUME_KEY, 1));
  setMuted(false);

  v.src = `/api/stream/${encodeURIComponent(id)}`;
  v.load();
  const started = v.play();
  if (started && started.catch) started.catch(() => {});

  const focusTarget = byId('player-close');
  if (focusTarget) focusTarget.focus();
}

/* ---------- 在线播放 ---------- */

/* 清晰度:档位摊成一排胶囊(0 = 最高),点哪档切哪档。 */
function fillQualities(payload, meta) {
  const wrap = byId('pl-quality-wrap');
  const slider = byId('pl-quality-segs');
  if (!wrap || !slider) return;
  onlineState = { qualities: payload.qualities || [], index: 0,
                  audioToken: payload.audio || '' };
  const total = onlineState.qualities.length;
  // 清晰度也摊成一排胶囊:档位就那几个,直接点比拖准
  slider.innerHTML = '';
  onlineState.qualities.forEach((item, index) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'pl-seg';
    button.dataset.index = String(index);
    button.textContent = item.label;
    button.setAttribute('aria-pressed', 'false');
    slider.appendChild(button);
  });
  // 默认最高档(qualities 按高度降序,0 = 最高):桌面宽带不人为压档,
  // 网络卡顿交给断流自恢复兜底,再卡就手动降一档(2026-11-18 用户要求)
  markQualitySegs(0);
  wrap.hidden = total === 0;
  if (payload.duration && !meta.duration) {
    setHeader({ ...meta, duration: payload.duration });
  }
}

/* 真正起播:视频和音轨一起 play。
   解析是异步的,常常已经出了用户手势窗口,浏览器会以 NotAllowedError 拒绝——
   这时退一步先静音播起来(静音自动播放永远允许),等用户下一次点/按键再放开声音,
   而不是甩一个「请点播放」的闸门让人多点一次。 */
function armUnmuteOnGesture() {
  if (armUnmuteOnGesture.armed) return;
  armUnmuteOnGesture.armed = true;
  setTip('已静音播放:点一下画面或按任意键开声音');
  const unmute = () => {
    const v = video();
    const a = audio();
    const volume = readNumber(VOLUME_KEY, 1);
    if (v) { v.muted = false; v.volume = volume; }
    if (a) { a.muted = false; a.volume = volume; }
    setMuted(false);
    setTip('');
    document.removeEventListener('pointerdown', unmute, true);
    document.removeEventListener('keydown', unmute, true);
    armUnmuteOnGesture.armed = false;
  };
  document.addEventListener('pointerdown', unmute, true);
  document.addEventListener('keydown', unmute, true);
}

function attemptOnlinePlay() {
  const v = video();
  const a = audio();
  if (!v) return;
  const volume = readNumber(VOLUME_KEY, 1);
  const muted = isMuted();
  v.muted = muted;
  v.volume = currentMode === 'local' ? volume : 0;
  if (a && a.src) {
    a.muted = muted;
    a.volume = volume;
    try { a.currentTime = v.currentTime || 0; } catch (error) { /* 忽略 */ }
  }
  const startedVideo = v.play();
  const startedAudio = a && a.src ? a.play() : null;
  // 音轨可能还没到 canplay,play() 会被静默拒绝:等它就绪再补一次,
  // 否则会出现「画面在动但没有声音」而进度条看着正常的怪状态。
  if (a && a.src && a.paused) {
    const kick = () => {
      a.removeEventListener('canplay', kick);
      if (a.paused && !v.paused) {
        a.currentTime = v.currentTime || 0;
        const retry = a.play();
        if (retry && retry.catch) retry.catch(() => {});
      }
    };
    a.addEventListener('canplay', kick);
  }
  Promise.allSettled([startedVideo, startedAudio].filter(Boolean))
    .then((results) => {
      const blocked = results.some(r => r.status === 'rejected');
      if (!blocked) return;
      if (v) { v.muted = true; v.play().catch(() => {}); }
      if (a && a.src) { a.muted = true; a.play().catch(() => {}); }
      setMuted(true);
      armUnmuteOnGesture();
    });
}

function markQualitySegs(index) {
  onlineState.index = Number(index) || 0;
  document.querySelectorAll('#pl-quality-segs .pl-seg').forEach((button) => {
    const on = Number(button.dataset.index) === Number(index);
    button.classList.toggle('on', on);
    button.setAttribute('aria-pressed', on ? 'true' : 'false');
  });
}

function playOnlineAt(index, { keepTime = true, autoplay = true } = {}) {
  const v = video();
  const a = audio();
  const item = onlineState.qualities[index];
  if (!v || !item) return;
  const resumeAt = keepTime ? (Number(v.currentTime) || 0) : 0;
  const speed = readNumber(SPEED_KEY, 1);

  v.src = `/online/stream/${encodeURIComponent(item.token)}`;
  if (a) {
    a.hidden = false;
    a.src = onlineState.audioToken
      ? `/online/stream/${encodeURIComponent(onlineState.audioToken)}` : '';
    a.playbackRate = speed;
    a.volume = readNumber(VOLUME_KEY, 1);
  }
  applySpeed(speed);

  const onReady = () => {
    v.removeEventListener('loadedmetadata', onReady);
    if (resumeAt > 0.5) {
      try { v.currentTime = resumeAt; } catch (error) { /* 忽略 */ }
    }
    syncAudioToVideo();
    if (autoplay) attemptOnlinePlay();
  };
  v.addEventListener('loadedmetadata', onReady);
  v.load();
  markQualitySegs(index);
}

/* 断流自恢复:重新解析拿新 token,再从断点续播(playOnlineAt 的 keepTime
   会先读 v.currentTime 再换 src)。额度 2 次,防死循环;用尽就明说。 */
function recoverOnlineStream() {
  const v = video();
  if (!v || currentMode !== 'online' || !isOpen()) return;
  if (!onlineUrl || onlineRecoverLeft <= 0) {
    setTip('播放流断开了,请重新打开这个视频');
    return;
  }
  onlineRecoverLeft -= 1;
  const url = onlineUrl;
  setTip('播放中断,正在自动恢复…');
  fetch(`/api/online/qualities?url=${encodeURIComponent(url)}`,
         { headers: { Accept: 'application/json' } })
    .then(r => r.json())
    .then((payload) => {
      if (currentMode !== 'online') return;
      if (!payload || !payload.ok || !payload.qualities || !payload.qualities.length) {
        setTip('自动恢复失败:解析不到可播放的清晰度,请重新打开');
        return;
      }
      // 断流前在哪一档,恢复后就还在哪一档(fillQualities 会重置回默认最高)
      const prevIndex = Math.max(0, Math.min(Number(onlineState.index) || 0,
        (payload.qualities || []).length - 1));
      fillQualities(payload, onlineMeta);
      markQualitySegs(prevIndex);
      playOnlineAt(prevIndex, { keepTime: true, autoplay: true });
    })
    .catch(() => { if (currentMode === 'online') setTip('自动恢复失败,请重新打开这个视频'); });
}

export function openOnlinePlayer(url, meta = {}) {
  const bv = bvFromUrl(url);
  const v = video();
  if (!bv || !v) return;

  currentId = '';
  currentMode = 'online';
  onlineUrl = url;
  onlineMeta = meta;
  onlineRecoverLeft = 2;
  buildSpeedSegs();
  setMode('online');
  setHeader(meta);
  setReveal('');
  setExternal(url);
  openOverlay();
  setTip('正在按你的登录态解析清晰度…');
  setMuted(false);

  // 悬停时可能已经预解析过,这里优先复用,省掉等待
  const pendingTask = prefetched.get(url);
  const request = pendingTask
    || fetch(`/api/online/qualities?url=${encodeURIComponent(url)}`,
             { headers: { Accept: 'application/json' } }).then(r => r.json());
  prefetched.set(url, request);

  request
    .then((payload) => {
      if (currentMode !== 'online') return;
      if (!payload || !payload.ok || !payload.qualities || !payload.qualities.length) {
        setTip('没能解析出可播放的清晰度,可以点右上角「B 站」在原站观看');
        return;
      }
      fillQualities(payload, meta);
      const index = onlineState.index;
      // 顶部提示当前档;档位不再跨会话记忆,每次起播默认最高(2026-11-18 用户要求)
      const chosen = onlineState.qualities[index];
      const label = chosen ? chosen.label : '';
      setTip(label ? `${label} · 按你的登录态播放` : '');
      // 直接起播:手势还在就带声音,过期了就先静音播、下一次点击开声音
      playOnlineAt(index, { keepTime: false, autoplay: true });
    })
    .catch(() => {
      if (currentMode === 'online') setTip('解析失败,可以点右上角「B 站」在原站观看');
    });

  const focusTarget = byId('player-close');
  if (focusTarget) focusTarget.focus();
}

export function closeLocalPlayer() {
  const overlay = byId('player');
  const v = video();
  const a = audio();
  if (!overlay || !isOpen()) return;
  if (v) {
    if (currentMode === 'local') savePosition(currentId, v.currentTime);
    v.pause();
    v.removeAttribute('src');
    v.load();            // 断开流,别让浏览器继续占着连接
  }
  if (a) {
    a.pause();
    a.removeAttribute('src');
    a.hidden = true;
  }
  setMuted(false);
  overlay.classList.remove('open');
  overlay.setAttribute('aria-hidden', 'true');
  document.body.classList.remove('player-open');
  setTip('');
  currentId = '';
  currentMode = '';
  onlineState = { qualities: [], index: 0, audioToken: '' };
  onlineUrl = '';
  onlineMeta = {};
  onlineRecoverLeft = 0;
  if (lastFocus && typeof lastFocus.focus === 'function') lastFocus.focus();
  lastFocus = null;
}

/* ---------- 入口:封面播放按钮 / 标题 ---------- */

function playFromButton(button) {
  const meta = {
    title: button.dataset.playTitle || '',
    uploader: button.dataset.playUp || '',
    duration: Number(button.dataset.playDuration) || 0,
    size: Number(button.dataset.playSize) || 0,
    url: button.dataset.playUrl || '',
  };
  const taskId = button.dataset.playTask || '';
  if (taskId) { openLocalPlayer(taskId, meta); return; }
  const online = button.dataset.playOnline || '';
  if (online) openOnlinePlayer(online, meta);
}

/* ---------- 预解析:让「点下去就有」成为常态 ----------

   浏览器的自动播放策略只认用户手势,而手势有效期只有几秒;等解析完再
   play() 基本已经被拒。所以真正要让「点一下就播(还带声音)」成立,
   必须在点击**之前**就把档位备好。

   两层预热:
     1) 悬停即解析——鼠标移到播放键上就开始;
     2) 渲染完静默预热首屏若干张卡——用户没悬停、直接点也不会等。
   前端去重 + 后端 10 分钟解析缓存,重复点击不会重复解析。
   串行 + 间隔,避免把 B 站打成风控(412)。 */
const prefetched = new Map();

function prefetchOnline(url) {
  if (!url) return null;
  if (prefetched.has(url)) return prefetched.get(url);
  const task = fetch(`/api/online/qualities?url=${encodeURIComponent(url)}`,
    { headers: { Accept: 'application/json' } })
    .then((response) => response.json())
    .catch(() => null);
  prefetched.set(url, task);
  return task;
}

document.addEventListener('pointerenter', (event) => {
  const button = event.target.closest?.('.vi-play-main[data-play-online]');
  if (button) prefetchOnline(button.dataset.playOnline || '');
}, true);

/* 静默预热:一页只做前几张(首屏可见的那些),串行、间隔 400ms。
   每次解析是一次完整 yt-dlp 提取(秒级 CPU 大户),并发多了会把整个
   Flask 进程拖慢——实测 6 个预热并发能把一次搜索堵到 23s(2026-11-17)。
   所以同一时间只放行 1 个在途解析,上一个完成(或失败)才发下一个。 */
const WARM_LIMIT = 6;
const WARM_GAP_MS = 400;
const MAX_INFLIGHT_WARM = 1;
let warmTimer = null;
let warmInflight = 0;

export function warmVisibleCovers() {
  if (warmTimer) return;
  const buttons = [...document.querySelectorAll('.page.active .vi-play-main[data-play-online]')]
    .filter(button => button.offsetParent !== null)
    .slice(0, WARM_LIMIT);
  const queue = buttons
    .map(button => button.dataset.playOnline || '')
    .filter(url => url && !prefetched.has(url));
  if (!queue.length) return;
  const step = () => {
    if (warmInflight >= MAX_INFLIGHT_WARM) { warmTimer = setTimeout(step, WARM_GAP_MS); return; }
    const url = queue.shift();
    if (!url) { warmTimer = null; return; }
    warmInflight += 1;
    const task = prefetchOnline(url);
    if (task && typeof task.finally === 'function') {
      task.finally(() => { warmInflight -= 1; });
    } else {
      warmInflight -= 1;
    }
    warmTimer = setTimeout(step, WARM_GAP_MS);
  };
  warmTimer = setTimeout(step, WARM_GAP_MS);
}

/* 页面渲染完(切页/首屏)就预热一次;渲染是异步补进来的,多做两次兜底。 */
['load', 'pageshow'].forEach(name => window.addEventListener(name, () => {
  setTimeout(warmVisibleCovers, 1200);
}));

document.addEventListener('click', (event) => {
  // 封面播放键:一个按钮走两条路(已下载 → 本地,未下载 → 在线)
  const center = event.target.closest('.vi-play-main[data-play-task], .vi-play-main[data-play-online]');
  if (center) {
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    event.stopPropagation();
    playFromButton(center);
    return;
  }
  const link = event.target.closest('[data-play-task]');
  if (!link) return;
  if (event.defaultPrevented || event.button !== 0
      || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  openLocalPlayer(link.dataset.playTask, {
    title: (link.dataset.playTitle || link.textContent || '').trim(),
    uploader: link.dataset.playUp || '',
    duration: Number(link.dataset.playDuration) || 0,
    size: Number(link.dataset.playSize) || 0,
    url: link.getAttribute('href') || '',
  });
});

/* 关闭:按钮 / 点空白 / Esc */
document.addEventListener('click', (event) => {
  if (event.target.closest('#player-close')) { closeLocalPlayer(); return; }
  if (event.target.id === 'player') closeLocalPlayer();
});

document.addEventListener('keydown', (event) => {
  if (!isOpen()) return;
  if (event.key === 'Escape') {
    event.preventDefault();
    closeLocalPlayer();
    return;
  }
  if (event.key === ' ' || event.key === 'k') {
    // 焦点在控件上时不要抢空格
    if (event.target.closest('select, input, textarea, button')) return;
    event.preventDefault();
    const v = video();
    if (v) { if (v.paused) v.play().catch(() => {}); else v.pause(); }
  }
});

/* 控制条交互 */
document.addEventListener('click', (event) => {
  if (!event.target.closest('#pl-toggle')) return;
  const v = video();
  if (!v) return;
  if (v.paused) {
    const started = v.play();
    if (started && started.catch) started.catch(() => {});
  } else {
    v.pause();
  }
});

document.addEventListener('input', (event) => {
  const target = event.target;
  if (target.id === 'pl-seek') {
    const v = video();
    if (!v || !Number.isFinite(v.duration)) return;
    const ratio = Number(target.value) / 1000;
    v.currentTime = ratio * v.duration;
    const a = audio();
    if (a && !a.hidden) {
      try { a.currentTime = v.currentTime; } catch (error) { /* 忽略 */ }
    }
    syncSeekBar(v.currentTime, v.duration);
    setTimeLabel(v.currentTime, v.duration);
    return;
  }
  if (target.id === 'pl-volume') {
    const volume = Number(target.value) / 100;
    applyVolume(volume);
    if (volume > 0) setMuted(false);
    else setMuted(true);
  }
});

/* 倍速 / 清晰度胶囊:点一下即切,不再有滑条 */
document.addEventListener('click', (event) => {
  const seg = event.target.closest('#pl-speed-segs .pl-seg');
  if (seg) {
    applySpeed(Number(seg.dataset.speed));
    return;
  }
  const quality = event.target.closest('#pl-quality-segs .pl-seg');
  if (quality && currentMode === 'online') {
    const index = Number(quality.dataset.index) || 0;
    markQualitySegs(index);
    playOnlineAt(index);
  }
});

document.addEventListener('click', (event) => {
  if (event.target.closest('#pl-mute')) {
    setMuted(!isMuted());
    return;
  }
  if (event.target.closest('#pl-full')) {
    const stage = document.querySelector('.pl-stage');
    if (!stage) return;
    if (document.fullscreenElement) document.exitFullscreen();
    else if (stage.requestFullscreen) stage.requestFullscreen();
  }
});

wireVideoEvents();
setPlayIcon(false);

if (typeof window !== 'undefined') {
  window.closeLocalPlayer = closeLocalPlayer;
}
