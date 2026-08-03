const GITHUB_COMMIT_RE = /github\.com\/(?:odoo|odoo-dev)\/(odoo|enterprise)\/commit\/([a-f0-9]{7,40})/;
const COMMIT_LABEL_RE = /(odoo|enterprise):([a-f0-9]{7,40})/;
const BATCH_HREF_RE = /\/runbot\/batch\/(\d+)$/;

const BUTTON_ID = 'odoo-runbot-local-btn';
const POLL_MS = 10000;

let pollTimer = null;
let isBusy = false;
let isInjecting = false;
let lastPath = null;

function showToast(message, isError = true) {
  const existing = document.getElementById('odoo-runbot-local-toast');
  if (existing) existing.remove();

  const toast = document.createElement('div');
  toast.id = 'odoo-runbot-local-toast';
  toast.style.cssText = `
    position: fixed; top: 20px; right: 20px; z-index: 999999;
    padding: 12px 20px; border-radius: 6px; font-size: 14px;
    font-family: sans-serif; max-width: 420px; white-space: pre-wrap;
    color: #fff; box-shadow: 0 4px 12px rgba(0,0,0,0.15);
    background: ${isError ? '#e74c3c' : '#27ae60'};
  `;
  toast.textContent = message;
  document.body.appendChild(toast);
  setTimeout(() => toast.remove(), isError ? 12000 : 6000);
}

// Built from DOM nodes rather than innerHTML: AMO flags innerHTML assignment on
// every upload, and one caller interpolates a server-supplied string.
function setButtonLabel(btn, icon, text) {
  btn.textContent = '';
  const glyph = document.createElement('i');
  glyph.className = `fa fa-${icon}`;
  btn.append(glyph, ` ${text}`);
}

function send(payload) {
  return new Promise((resolve) => chrome.runtime.sendMessage(payload, resolve));
}

async function fetchLatestBatchCommits(batchUrl) {
  try {
    const res = await fetch(batchUrl, { credentials: 'include' });
    if (!res.ok) return { error: `runbot returned HTTP ${res.status} for the batch page` };
    const doc = new DOMParser().parseFromString(await res.text(), 'text/html');

    let commitOdoo = null;
    let commitEnterprise = null;

    doc.querySelectorAll('a[href*="/commit/"]').forEach((link) => {
      const m = (link.getAttribute('href') || '').match(GITHUB_COMMIT_RE);
      if (!m) return;
      if (m[1] === 'odoo' && !commitOdoo) commitOdoo = m[2];
      if (m[1] === 'enterprise' && !commitEnterprise) commitEnterprise = m[2];
    });

    if (!commitOdoo || !commitEnterprise) {
      doc.querySelectorAll('span.label, .badge, code').forEach((el) => {
        const m = el.textContent.trim().match(COMMIT_LABEL_RE);
        if (!m) return;
        if (m[1] === 'odoo' && !commitOdoo) commitOdoo = m[2];
        if (m[1] === 'enterprise' && !commitEnterprise) commitEnterprise = m[2];
      });
    }

    return { commitOdoo, commitEnterprise };
  } catch (e) {
    return { error: e.message };
  }
}

// Batch links appear in arbitrary DOM order, so pick the highest id rather than
// whichever happens to come first.
function findLatestBatchHref() {
  let best = null;
  let bestId = -1;
  document.querySelectorAll('a[href*="/runbot/batch/"]').forEach((link) => {
    const href = link.getAttribute('href') || '';
    const m = href.match(BATCH_HREF_RE);
    if (!m) return;
    const id = parseInt(m[1], 10);
    if (id > bestId) {
      bestId = id;
      best = href;
    }
  });
  return best;
}

function findBundleName(h5) {
  for (const node of h5.childNodes) {
    if (node.nodeType === Node.TEXT_NODE && node.textContent.trim()) {
      return node.textContent.trim();
    }
  }
  return h5.textContent.replace(/\s+/g, ' ').trim()
    .replace(/\s*Copy.*$|\s*Stats.*$|\s*Run.*$/i, '');
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

function startPolling(ctx) {
  stopPolling();
  pollTimer = setInterval(() => {
    if (!isBusy && document.getElementById(BUTTON_ID)) updateButton(ctx);
  }, POLL_MS);
}

async function handleRun(btn, ctx) {
  isBusy = true;
  setButtonLabel(btn, 'play', 'Fetching commits...');
  btn.style.opacity = '0.6';

  const { commitOdoo, commitEnterprise, error } = await fetchLatestBatchCommits(ctx.batchUrl);
  if (!commitOdoo || !commitEnterprise) {
    showToast(error
      ? `Could not read the batch page: ${error}`
      : 'Could not find odoo and enterprise commits in the latest batch');
    isBusy = false;
    updateButton(ctx);
    return;
  }

  setButtonLabel(btn, 'play', 'Starting...');
  // Reflect server-side progress: fetching a cold commit can take minutes.
  const progress = setInterval(async () => {
    const state = await send({ action: 'status' });
    if (state && state.phase) {
      setButtonLabel(btn, 'play', `${state.phase}...`);
    }
  }, 2000);

  const response = await send({
    action: 'checkout',
    branch: ctx.bundleName,
    commit_odoo: commitOdoo,
    commit_enterprise: commitEnterprise,
  });
  clearInterval(progress);

  if (response && response.success) {
    showToast(`Odoo starting at ${response.data.url}`, false);
  } else {
    // Chrome can terminate the MV3 service worker during a long checkout, which
    // loses the reply even though the server finished. Confirm the real state
    // before reporting a failure that did not happen.
    const state = await send({ action: 'status' });
    if (state && state.running && state.alive) {
      showToast(`Odoo is running at http://127.0.0.1:${state.port || 8072}`, false);
    } else {
      showToast((response && response.error) || 'Failed to start Odoo');
    }
  }
  isBusy = false;
  updateButton(ctx);
}

async function handleStop(btn, ctx) {
  isBusy = true;
  setButtonLabel(btn, 'stop', 'Stopping...');
  btn.style.opacity = '0.6';
  const res = await send({ action: 'stop' });
  if (res && res.status === 'stopped') {
    showToast('Instance stopped and database dropped', false);
  } else {
    showToast((res && res.error) || 'Failed to stop the instance');
  }
  isBusy = false;
  updateButton(ctx);
}

async function updateButton(ctx) {
  const data = await send({ action: 'status' });
  const btn = document.createElement('a');
  btn.id = BUTTON_ID;
  btn.href = '#';
  btn.className = 'btn btn-default';

  if (data && data._reachable === false) {
    btn.title = 'The local server is not running';
    btn.style.cssText = 'color: #999; font-weight: 600;';
    setButtonLabel(btn, 'play', 'Server offline');
    btn.addEventListener('click', (e) => {
      e.preventDefault();
      showToast('Cannot reach the local server.\nStart it with: systemctl --user restart odoo-runbot-local');
    });
  } else if (data && data.running && data.alive) {
    btn.title = 'Stop the running instance';
    btn.style.cssText = 'color: #c0392b; font-weight: 600;';
    setButtonLabel(btn, 'stop', 'Stop');
    btn.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      handleStop(btn, ctx);
    });
  } else {
    btn.title = 'Run this bundle locally';
    btn.style.cssText = 'color: #875a7b; font-weight: 600;';
    setButtonLabel(btn, 'play', 'Run locally');
    btn.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      handleRun(btn, ctx);
    });
  }

  // Clear immediately before appending, never before the await above: any gap
  // between the check and the append lets a concurrent pass inject a second
  // button. querySelectorAll (not querySelector) also collapses duplicates
  // already on the page — including one left by another copy of this extension.
  document.querySelectorAll(`#${BUTTON_ID}`).forEach((el) => el.remove());
  ctx.btnGroup.appendChild(btn);
  startPolling(ctx);
}

async function handleBundlePage() {
  // Set synchronously before any await, so a second call triggered by the DOM
  // mutations of the first cannot start its own injection.
  if (isInjecting || document.getElementById(BUTTON_ID)) return true;

  const h5 = Array.from(document.querySelectorAll('h5')).find((h) => h.querySelector('.btn-group'));
  if (!h5) return false;

  const btnGroup = h5.querySelector('.btn-group');
  const batchHref = findLatestBatchHref();
  if (!batchHref) return false;

  isInjecting = true;
  try {
    await updateButton({
      btnGroup,
      bundleName: findBundleName(h5),
      batchUrl: window.location.origin + batchHref,
    });
  } finally {
    isInjecting = false;
  }
  return true;
}

// The button is injected on bundle pages only — listing pages are left alone.
function init() {
  if (window.location.pathname.includes('/bundle/')) {
    handleBundlePage();
  } else {
    stopPolling();
  }
}

// Runbot rewrites the page without a full navigation, so re-run on DOM changes
// and on history navigation instead of only at document load.
function watch() {
  lastPath = window.location.pathname;
  const observer = new MutationObserver(() => {
    if (window.location.pathname !== lastPath) {
      lastPath = window.location.pathname;
      stopPolling();
      init();
    } else if (window.location.pathname.includes('/bundle/')
               && !document.getElementById(BUTTON_ID)) {
      init();
    }
  });
  observer.observe(document.body, { childList: true, subtree: true });
  window.addEventListener('popstate', init);
}

function start() {
  init();
  watch();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', start);
} else {
  start();
}
