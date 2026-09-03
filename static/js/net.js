/* 网络层：统一 fetch / HTTP / JSON 边界。同源部署，URL 直通。 */
export const api = (p) => p;

export class HttpError extends Error {
  constructor(message, options = {}) {
    super(message);
    this.name = 'HttpError';
    this.status = Number(options.status) || 0;
    this.code = options.code || 'http_error';
    this.field = options.field || null;
    this.payload = options.payload ?? null;
    this.url = options.url || '';
    this.retryAfter = options.retryAfter || null;
  }
}

function responseMessage(response, payload) {
  if (payload && typeof payload === 'object') {
    const message = payload.error || payload.message;
    if (typeof message === 'string' && message.trim()) return message.trim();
  }
  return response.statusText || `请求失败（HTTP ${response.status}）`;
}

function responseCode(payload, fallback) {
  return payload && typeof payload === 'object' && typeof payload.code === 'string'
    ? payload.code : fallback;
}

export async function requestJSON(url, options = {}) {
  const {
    method = 'GET', body, headers: inputHeaders = {}, signal,
  } = options;
  const headers = new Headers(inputHeaders);
  if (!headers.has('Accept')) headers.set('Accept', 'application/json');

  const init = { method, headers, signal };
  if (body !== undefined) {
    const rawBody = typeof body === 'string'
      || body instanceof Blob
      || body instanceof FormData
      || body instanceof URLSearchParams;
    if (rawBody) {
      init.body = body;
    } else {
      if (!headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
      init.body = JSON.stringify(body);
    }
  }

  const response = await fetch(api(url), init);
  const text = await response.text();
  let payload = null;
  if (text.trim()) {
    try {
      payload = JSON.parse(text);
    } catch (error) {
      throw new HttpError(
        response.ok ? '服务器返回了无法识别的数据' : responseMessage(response, null),
        {
          status: response.status,
          code: response.ok ? 'invalid_json_response' : 'http_error',
          payload: text.slice(0, 512),
          url: response.url || String(url),
          retryAfter: response.headers.get('Retry-After'),
        },
      );
    }
  }

  if (!response.ok) {
    throw new HttpError(responseMessage(response, payload), {
      status: response.status,
      code: responseCode(payload, 'http_error'),
      field: payload && typeof payload === 'object' ? payload.field : null,
      payload,
      url: response.url || String(url),
      retryAfter: response.headers.get('Retry-After'),
    });
  }

  // 204/空响应统一成空对象；200 {ok:false} 保持业务响应，不在网络层抛错。
  return payload === null ? {} : payload;
}

export function getJSON(url, options = {}) {
  return requestJSON(url, { ...options, method: 'GET' });
}

export function postJSON(url, body, options = {}) {
  return requestJSON(url, { ...options, method: 'POST', body });
}

export function postText(url, text, options = {}) {
  const headers = new Headers(options.headers || {});
  if (!headers.has('Content-Type')) headers.set('Content-Type', 'text/plain;charset=UTF-8');
  return requestJSON(url, { ...options, method: 'POST', headers, body: text });
}

export function postEmpty(url, options = {}) {
  return requestJSON(url, { ...options, method: 'POST' });
}

export function isAbortError(error) {
  return !!error && (error.name === 'AbortError' || error.code === 20);
}
