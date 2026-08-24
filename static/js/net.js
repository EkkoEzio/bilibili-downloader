/* 网络层:统一 fetch + JSON。同源部署,URL 直通。 */
export const api = (p) => p;

export async function getJSON(url) {
  return (await fetch(api(url))).json();
}

export async function postJSON(url, body) {
  return (await fetch(api(url), {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
  })).json();
}

export async function postText(url, text) {
  return (await fetch(api(url), { method: 'POST', body: text })).json();
}

export async function postEmpty(url) {
  return (await fetch(api(url), { method: 'POST' })).json();
}
