/* 遗留项 27 试点:在 Node 里以最小 DOM 桩真实执行 player.js / search.js 的
   模块代码,验证两类「延迟响应落地」的会话归属行为(2026-11-23 审查):

     · player.js(遗留项 24):旧会话的解析响应落地时,若会话已关闭,
       不得设置视频源;正常会话的响应必须设置 /online/stream/<token>;
       关闭播放器必须中止在途请求并摘掉 loadedmetadata 监听。
     · search.js(遗留项 25):搜索响应落地时人已切到别的页,结果不得铺进
       目标 apply;还在发起页时正常落地。

   由 pytest(test_app.py::test_pilot_deferred_session_behavior)以
   node 子进程驱动:退出码 0 = 全部断言通过,stdout 逐行打印场景结果。
   桩只有这一个文件,不得依赖 npm 包(项目无 node_modules)。
*/
import { pathToFileURL } from 'node:url';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const jsRoot = path.resolve(here, '..', '..', 'static', 'js');

/* ---------- 最小 DOM 桩 ---------- */

const noop = () => {};

function makeClassList(props) {
  const set = new Set();
  return {
    add: (...names) => names.forEach(n => set.add(n)),
    remove: (...names) => names.forEach(n => set.delete(n)),
    contains: n => set.has(n),
    toggle: (n, force) => {
      const on = force === undefined ? !set.has(n) : !!force;
      if (on) set.add(n); else set.delete(n);
      return on;
    },
  };
}

/* 宽容元素桩:已知属性/方法照实,未知读取给 undefined,未知调用给 noop。
   events 记录 add/removeEventListener 供断言监听清理。 */
function makeElement(id) {
  const target = {
    id,
    props: {},                 // 显式赋值记录(src 等)
    events: { added: [], removed: [] },
    listeners: {},             // 自身事件(addEventListener 后 dispatch 触发)
    dataset: {},
    classList: makeClassList(),
    style: {},
    attributes: {},
    _textContent: '',
    children: [],
    setAttribute(name, value) { this.attributes[name] = value; },
    getAttribute(name) { return this.attributes[name]; },
    removeAttribute(name) { delete this.attributes[name]; },
    hasAttribute(name) { return name in this.attributes; },
    addEventListener(type, handler) { this.events.added.push({ type, handler }); },
    removeEventListener(type, handler) { this.events.removed.push({ type, handler }); },
    dispatchEvent() { return true; },
    focus() {},
    blur() {},
    remove() {},
    appendChild(child) { this.children.push(child); },
    insertBefore() {},
    replaceChildren() { this.children.length = 0; },
    closest() { return null; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    load() { this.props._loadCalled = (this.props._loadCalled || 0) + 1; },
    play() { return Promise.resolve(); },
    pause() {},
    scrollIntoView() {},
    click() {},
  };
  return new Proxy(target, {
    get(obj, key) {
      if (key in obj) return obj[key];
      if (key === 'textContent') return obj._textContent;
      if (key === 'innerHTML' || key === 'value') return obj.props[key] ?? '';
      return obj.props[key];
    },
    set(obj, key, value) {
      if (key === 'textContent') { obj._textContent = String(value); return true; }
      obj.props[key] = value;
      return true;
    },
  });
}

function makeDocumentStub() {
  const elements = new Map();     // id -> element
  const docListeners = [];
  let activePage = '';
  const state = {
    get activePage() { return activePage; },
    set activePage(v) { activePage = v; },
  };
  const doc = {
    state,
    _listeners: docListeners,
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, makeElement(id));
      return elements.get(id);
    },
    querySelector(sel) {
      if (sel === '.page.active') {
        return activePage ? { dataset: { page: activePage } } : null;
      }
      return null;
    },
    querySelectorAll() { return []; },
    addEventListener(type, handler) { docListeners.push({ type, handler }); },
    removeEventListener() {},
    createElement(tag) {
      const el = makeElement('created-' + tag);
      el.textContentSet = text => { el.props.textContent = text; };
      return el;
    },
    body: makeElement('body'),
  };
  return doc;
}

function makeWindowStub() {
  return { addEventListener: noop, removeEventListener: noop, dispatchEvent: noop };
}

function makeLocalStorageStub() {
  const store = new Map();
  return {
    getItem: k => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => { store.set(k, String(v)); },
    removeItem: k => { store.delete(k); },
    clear: () => store.clear(),
  };
}

/* fetch 桩:每次调用挂起,保留 defers 数组,测试手动放行 payload。
   记录每次调用的 signal,供断言「关闭会话时在途请求被中止」。 */
function makeFetchStub() {
  const calls = [];
  const fetchStub = (url, opts) => {
    const call = { url, opts: opts || {}, aborted: false };
    if (opts && opts.signal) {
      if (opts.signal.aborted) call.aborted = true;
      opts.signal.addEventListener('abort', () => { call.aborted = true; });
    }
    let resolveFetch, rejectFetch;
    const promise = new Promise((resolve, reject) => {
      resolveFetch = resolve;
      rejectFetch = reject;
    });
    call.resolveWithJson = payload => resolveFetch({
      ok: true,
      status: 200,
      json: () => Promise.resolve(payload),
    });
    call.reject = reason => rejectFetch(reason);
    calls.push(call);
    return promise;
  };
  return { fetchStub, calls };
}

/* ---------- 场景执行 ---------- */

const results = [];
function record(name, pass, detail) {
  results.push({ name, pass, detail });
  console.log('[case] ' + name + ' -> ' + (pass ? 'PASS' : 'FAIL') + (pass ? '' : ' :: ' + detail));
}

async function flushMicrotasks() {
  for (let i = 0; i < 6; i++) {
    await Promise.resolve();
    await new Promise(r => setTimeout(r, 5));
  }
}

/* 场景 1(遗留项 24):player.js 在线会话的响应归属 */
async function casePlayerSession() {
  const doc = makeDocumentStub();
  const win = makeWindowStub();
  const ls = makeLocalStorageStub();
  const { fetchStub, calls } = makeFetchStub();
  globalThis.document = doc;
  globalThis.window = win;
  globalThis.localStorage = ls;
  globalThis.fetch = fetchStub;
  globalThis.AbortController = AbortController;
  globalThis.requestAnimationFrame = noop;
  globalThis.matchMedia = () => ({ matches: false, addListener: noop, removeListener: noop });
  try {
    const playerUrl = pathToFileURL(path.join(jsRoot, 'player.js')).href;
    const mod = await import(playerUrl);
    const videoEl = doc.getElementById('player-video');
    const overlay = doc.getElementById('player');
    const url = 'https://www.bilibili.com/video/BV1xx411c7mD';
    const payload = {
      ok: true, duration: 120, title: 'A',
      qualities: [{ label: '1080P', height: 1080, token: 'tokB' }],
      audio: 'aTok',
    };

    // 打开会话 1(fetch 挂起),随后关闭播放器
    mod.openOnlinePlayer(url, { title: 'A' });
    const firstCall = calls[0];
    if (!firstCall) return record('player_session_guards', false, '第一次 openOnlinePlayer 没有发起解析请求');
    mod.closeLocalPlayer();
    // 关闭必须中止在途解析请求
    if (!(firstCall.opts.signal && firstCall.opts.signal.aborted)) {
      return record('player_session_guards', false, 'closeLocalPlayer 未中止在途解析请求');
    }
    // 旧会话的迟到响应不得落地:视频源不得被设置
    firstCall.resolveWithJson(payload);
    await flushMicrotasks();
    const staleSrc = videoEl.props.src;
    if (staleSrc) {
      return record('player_session_guards', false, '旧会话迟到响应设置了视频源: ' + staleSrc);
    }
    // 正常会话:重新打开,响应落地必须设置 /online/stream/<token>
    mod.openOnlinePlayer(url, { title: 'A' });
    const secondCall = calls[calls.length - 1];
    secondCall.resolveWithJson(payload);
    await flushMicrotasks();
    const src = videoEl.props.src;
    if (src !== '/online/stream/tokB') {
      return record('player_session_guards', false, '正常会话未设置视频源,实际 src=' + src);
    }
    // 起播挂载了 loadedmetadata 监听;关闭必须摘掉
    const added = videoEl.events.added.filter(ev => ev.type === 'loadedmetadata');
    const removed = videoEl.events.removed.filter(ev => ev.type === 'loadedmetadata');
    if (!added.length) {
      return record('player_session_guards', false, '起播未挂 loadedmetadata 监听');
    }
    mod.closeLocalPlayer();
    const removedAfter = videoEl.events.removed.filter(ev => ev.type === 'loadedmetadata');
    if (removedAfter.length < added.length) {
      return record('player_session_guards', false,
        '关闭后未摘净 loadedmetadata 监听: added=' + added.length + ' removed=' + removedAfter.length);
    }
    record('player_session_guards', true, '旧响应不落地/正常落地设源/关闭中止+摘监听 全部成立');
  } finally {
    delete globalThis.document; delete globalThis.window;
    delete globalThis.localStorage; delete globalThis.fetch;
    delete globalThis.matchMedia; delete globalThis.requestAnimationFrame;
  }
}

/* 场景 2(遗留项 25):search.js 搜索响应的页面归属 */
async function caseSearchPageOwnership() {
  const doc = makeDocumentStub();
  globalThis.document = doc;
  globalThis.window = makeWindowStub();
  globalThis.localStorage = makeLocalStorageStub();
  const { fetchStub, calls } = makeFetchStub();
  globalThis.fetch = fetchStub;
  try {
    const searchUrl = pathToFileURL(path.join(jsRoot, 'search.js')).href;
    const mod = await import(searchUrl);

    const applied = [];
    let exited = 0;
    mod.registerSearchTarget('home', {
      kind: 'video',
      apply: items => applied.push(items),
      exit: () => { exited += 1; },
    });

    // 通过真实的键盘事件路径触发搜索(模块导入时注册的 keydown 监听)
    const input = doc.getElementById('tb-search');
    input.props.value = '歌';
    const enterEvent = {
      key: 'Enter',
      preventDefault: noop,
      target: { closest: () => ({ value: input.props.value, dispatchEvent: noop }) },
    };
    doc.state.activePage = 'home';
    doc._listeners.filter(l => l.type === 'keydown').forEach(l => l.handler(enterEvent));
    const firstCall = calls[calls.length - 1];
    if (!firstCall) return record('search_page_ownership', false, '回车没有触发 /api/search 请求');

    // 响应落地前人已经切到别的页:结果不得铺进目标 apply
    doc.state.activePage = 'downloads';
    firstCall.resolveWithJson({ ok: true, items: [{ title: 'x' }] });
    await flushMicrotasks();
    if (applied.length !== 0) {
      return record('search_page_ownership', false, '跨页迟到的搜索结果被铺进了目标 apply');
    }
    if (mod.isSearchMode('home')) {
      return record('search_page_ownership', false, '跨页落地后 home 仍处于搜索态');
    }

    // 人还在发起页:响应正常落地
    doc.state.activePage = 'home';
    doc._listeners.filter(l => l.type === 'keydown').forEach(l => l.handler(enterEvent));
    const secondCall = calls[calls.length - 1];
    secondCall.resolveWithJson({ ok: true, items: [{ title: 'x' }] });
    await flushMicrotasks();
    if (applied.length !== 1) {
      return record('search_page_ownership', false, '同页响应未落地,applied=' + applied.length);
    }
    if (!mod.isSearchMode('home')) {
      return record('search_page_ownership', false, '同页落地后 home 未进入搜索态');
    }
    // 退出搜索态:在途请求被中止(换一个词再发一轮挂着,然后退出;
    // 同词第三次回车会命中会话内缓存,不会发起请求)
    input.props.value = '歌2';
    doc._listeners.filter(l => l.type === 'keydown').forEach(l => l.handler(enterEvent));
    const thirdCall = calls[calls.length - 1];
    mod.exitSearch('home');
    if (!(thirdCall.opts.signal && thirdCall.opts.signal.aborted)) {
      return record('search_page_ownership', false, '退出搜索态未中止在途请求');
    }
    thirdCall.resolveWithJson({ ok: true, items: [{ title: 'late' }] });
    await flushMicrotasks();
    if (applied.length !== 1) {
      return record('search_page_ownership', false, '退出搜索态后的迟到结果被铺进 apply');
    }
    record('search_page_ownership', true, '跨页不落地/同页落地/退出中止且迟到不铺 全部成立');
  } finally {
    delete globalThis.document; delete globalThis.window;
    delete globalThis.localStorage; delete globalThis.fetch;
  }
}

const cases = [casePlayerSession, caseSearchPageOwnership];
for (const run of cases) {
  try { await run(); } catch (error) {
    record(run.name, false, '异常: ' + (error && error.stack ? error.stack.split('\n').slice(0, 4).join(' | ') : String(error)));
  }
}

const failed = results.filter(r => !r.pass);
console.log(failed.length ? 'RESULT: FAIL ' + failed.length : 'RESULT: ALL PASS');
process.exit(failed.length ? 1 : 0);
