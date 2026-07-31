const SERVER_URL = 'http://127.0.0.1:8765';

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.action === 'checkout') {
    const params = new URLSearchParams({
      branch: message.branch,
      commit_odoo: message.commit_odoo,
      commit_enterprise: message.commit_enterprise,
    });
    fetch(`${SERVER_URL}/checkout?${params}`)
      .then(res => res.json())
      .then(data => {
        if (data.url) {
          chrome.tabs.create({ url: data.url });
          sendResponse({ success: true, data });
        } else {
          sendResponse({ success: false, error: data.error || 'Unknown error' });
        }
      })
      .catch(err => {
        sendResponse({ success: false, error: `Cannot reach local server: ${err.message}` });
      });
    return true;
  }

  if (message.action === 'status') {
    fetch(`${SERVER_URL}/status`)
      .then(res => res.json())
      .then(data => sendResponse({ ...data, _reachable: true }))
      .catch(() => sendResponse({ running: false, _reachable: false }));
    return true;
  }

  if (message.action === 'stop') {
    fetch(`${SERVER_URL}/stop`)
      .then(res => res.json())
      .then(data => sendResponse(data))
      .catch(err => sendResponse({ error: err.message }));
    return true;
  }

  if (message.action === 'openBatch') {
    chrome.tabs.create({ url: message.url });
    sendResponse({ success: true });
  }
});
