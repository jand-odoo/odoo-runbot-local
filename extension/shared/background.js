// Sole owner of network access to the local server. Content scripts and the
// popup talk to it through runtime messages.
const SERVER_URL = 'http://127.0.0.1:8765';
// The server rejects mutating calls without this header, which a drive-by page
// cannot set without triggering a preflight the server refuses.
const GUARD_HEADER = { 'X-Runbot-Local': '1', 'Content-Type': 'application/json' };

async function readError(res) {
  try {
    const data = await res.json();
    return data.detail ? `${data.error} — ${data.detail}` : (data.error || `HTTP ${res.status}`);
  } catch (e) {
    return `HTTP ${res.status}`;
  }
}

async function postJson(path, body) {
  const res = await fetch(`${SERVER_URL}${path}`, {
    method: 'POST',
    headers: GUARD_HEADER,
    body: JSON.stringify(body || {}),
  });
  if (!res.ok) throw new Error(await readError(res));
  return res.json();
}

async function getJson(path) {
  const res = await fetch(`${SERVER_URL}${path}`);
  return res.json();
}

const handlers = {
  async checkout(message) {
    const data = await postJson('/checkout', {
      branch: message.branch,
      commit_odoo: message.commit_odoo,
      commit_enterprise: message.commit_enterprise,
    });
    chrome.tabs.create({ url: data.url });
    return { success: true, data };
  },

  async status() {
    return { ...(await getJson('/status')), _reachable: true };
  },

  async health() {
    return { ...(await getJson('/health')), _reachable: true };
  },

  async stop() {
    return postJson('/stop');
  },
};

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  const handler = handlers[message.action];
  if (!handler) return false;

  handler(message)
    .then(sendResponse)
    .catch((err) => {
      const unreachable = err instanceof TypeError; // fetch could not connect
      sendResponse({
        success: false,
        running: false,
        _reachable: !unreachable,
        error: unreachable
          ? 'Cannot reach the local server — is odoo-runbot-local running?'
          : err.message,
      });
    });
  return true; // response is asynchronous
});
