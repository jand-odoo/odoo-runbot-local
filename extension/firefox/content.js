const COMMIT_LABEL_RE = /(odoo|enterprise):([a-f0-9]{7,40})/;
const GITHUB_COMMIT_RE = /github\.com\/(?:odoo|odoo-dev)\/(odoo|enterprise)\/commit\/([a-f0-9]{7,40})/;

let pollInterval = null;
let isBusy = false;

function showToast(message, isError = true) {
  const existing = document.getElementById('odoo-runbot-local-toast');
  if (existing) existing.remove();

  const toast = document.createElement('div');
  toast.id = 'odoo-runbot-local-toast';
  toast.style.cssText = `
    position: fixed; top: 20px; right: 20px; z-index: 999999;
    padding: 12px 20px; border-radius: 6px; font-size: 14px;
    font-family: sans-serif; max-width: 400px;
    color: #fff; box-shadow: 0 4px 12px rgba(0,0,0,0.15);
    background: ${isError ? '#e74c3c' : '#27ae60'};
  `;
  toast.textContent = message;
  document.body.appendChild(toast);
  setTimeout(() => toast.remove(), 6000);
}

async function fetchLatestBatchCommits(batchUrl) {
  try {
    const res = await fetch(batchUrl);
    const html = await res.text();
    const doc = new DOMParser().parseFromString(html, 'text/html');

    let commitOdoo = null, commitEnterprise = null;

    doc.querySelectorAll('a[title="View Commit on Github"]').forEach(link => {
      const m = (link.getAttribute('href') || '').match(GITHUB_COMMIT_RE);
      if (m) {
        if (m[1] === 'odoo' && !commitOdoo) commitOdoo = m[2];
        if (m[1] === 'enterprise' && !commitEnterprise) commitEnterprise = m[2];
      }
    });

    if (!commitOdoo || !commitEnterprise) {
      doc.querySelectorAll('span.label').forEach(label => {
        const m = label.textContent.trim().match(COMMIT_LABEL_RE);
        if (m) {
          if (m[1] === 'odoo') commitOdoo = m[2];
          if (m[1] === 'enterprise') commitEnterprise = m[2];
        }
      });
    }

    return { commitOdoo, commitEnterprise };
  } catch (e) {
    return { commitOdoo: null, commitEnterprise: null, error: e.message };
  }
}

function getStatus() {
  return new Promise(resolve => {
    chrome.runtime.sendMessage({ action: 'status' }, resolve);
  });
}

function stopInstance() {
  return new Promise(resolve => {
    chrome.runtime.sendMessage({ action: 'stop' }, resolve);
  });
}

function startPolling(h5BtnGroup, bundleName, fullBatchUrl) {
  if (pollInterval) clearInterval(pollInterval);
  pollInterval = setInterval(() => {
    if (!isBusy) updateButton(h5BtnGroup, bundleName, fullBatchUrl);
  }, 15000);
}

function stopPolling() {
  if (pollInterval) {
    clearInterval(pollInterval);
    pollInterval = null;
  }
}

async function updateButton(h5BtnGroup, bundleName, fullBatchUrl) {
  const existing = h5BtnGroup.querySelector('#odoo-runbot-local-btn');
  if (existing) existing.remove();

  const data = await getStatus();
  const isRunning = data && data.running && data.alive;

  const btn = document.createElement('a');
  btn.id = 'odoo-runbot-local-btn';
  btn.href = '#';
  btn.className = 'btn btn-default';

  if (isRunning) {
    btn.title = 'Stop running instance';
    btn.style.cssText = 'color: #c0392b; font-weight: 600;';
    btn.innerHTML = '<i class="fa fa-stop"></i> Stop';
    btn.addEventListener('click', async (e) => {
      e.preventDefault();
      e.stopPropagation();
      isBusy = true;
      btn.innerHTML = '<i class="fa fa-stop"></i> Stopping...';
      btn.style.opacity = '0.6';
      const res = await stopInstance();
      showToast(res.status === 'stopped' ? 'Instance stopped' : 'Failed to stop', res.status !== 'stopped');
      isBusy = false;
      updateButton(h5BtnGroup, bundleName, fullBatchUrl);
    });
  } else {
    btn.title = 'Run locally';
    btn.style.cssText = 'color: #875a7b; font-weight: 600;';
    btn.innerHTML = '<i class="fa fa-play"></i> Run locally';
    btn.addEventListener('click', async (e) => {
      e.preventDefault();
      e.stopPropagation();
      isBusy = true;
      btn.innerHTML = '<i class="fa fa-play"></i> Fetching commits...';
      btn.style.opacity = '0.6';
      const { commitOdoo, commitEnterprise, error } = await fetchLatestBatchCommits(fullBatchUrl);
      if (!commitOdoo || !commitEnterprise) {
        showToast(error ? 'Fetch failed: ' + error : 'Could not find commits in latest batch', true);
        isBusy = false;
        updateButton(h5BtnGroup, bundleName, fullBatchUrl);
        return;
      }
      btn.innerHTML = '<i class="fa fa-play"></i> Starting...';
      chrome.runtime.sendMessage({
        action: 'checkout',
        branch: bundleName,
        commit_odoo: commitOdoo,
        commit_enterprise: commitEnterprise,
      }, (response) => {
        if (response && response.success) {
          showToast('Odoo starting at ' + response.data.url, false);
        } else {
          showToast(response?.error || 'Failed to start', true);
        }
        isBusy = false;
        updateButton(h5BtnGroup, bundleName, fullBatchUrl);
      });
    });
  }

  h5BtnGroup.appendChild(btn);
  startPolling(h5BtnGroup, bundleName, fullBatchUrl);
}

async function handleBundlePage() {
  try {
    const h5 = Array.from(document.querySelectorAll('h5')).find(h => h.querySelector('.btn-group'));
    if (!h5) { console.log('odoo-runbot-local: no bundle h5 found'); return; }

    let bundleName = '';
    for (const node of h5.childNodes) {
      if (node.nodeType === Node.TEXT_NODE && node.textContent.trim()) {
        bundleName = node.textContent.trim();
        break;
      }
    }
    if (!bundleName) {
      bundleName = h5.textContent.replace(/\s+/g, ' ').trim()
        .replace(/\s*Copy.*$|\s*Stats.*$|\s*Run.*$/i, '');
    }
    console.log('odoo-runbot-local: bundleName =', bundleName);

    let latestBatchHref = null;
    document.querySelectorAll('a[href*="/runbot/batch/"]').forEach(link => {
      if (latestBatchHref) return;
      const href = link.getAttribute('href') || '';
      const m = href.match(/\/runbot\/batch\/(\d+)$/);
      if (!m) return;
      if (!latestBatchHref) latestBatchHref = href;
    });
    if (!latestBatchHref) {
      console.log('odoo-runbot-local: no batch link found');
      return;
    }
    console.log('odoo-runbot-local: latest batch =', latestBatchHref);

    const fullBatchUrl = window.location.origin + latestBatchHref;

    const h5BtnGroup = h5.querySelector('.btn-group');
    if (!h5BtnGroup) {
      console.log('odoo-runbot-local: no btn-group in h5');
      return;
    }

    await updateButton(h5BtnGroup, bundleName, fullBatchUrl);
    console.log('odoo-runbot-local: button added');
  } catch (e) {
    console.log('odoo-runbot-local error:', e.message);
  }
}

function processMainPage() {
  try {
    const bundleLinks = document.querySelectorAll('a[href*="/runbot/bundle/"]');
    const seen = new Set();
    bundleLinks.forEach(link => {
      const href = link.getAttribute('href');
      if (seen.has(href)) return;
      seen.add(href);

      const runLink = document.createElement('a');
      runLink.href = '#';
      runLink.textContent = '▶';
      runLink.title = 'Open bundle to run locally';
      runLink.style.cssText = 'margin-left:4px;font-size:13px;color:#875a7b;text-decoration:none;cursor:pointer;';
      runLink.addEventListener('click', (e) => {
        e.preventDefault();
        e.stopPropagation();
        chrome.runtime.sendMessage({
          action: 'openBatch',
          url: window.location.origin + href,
        });
      });
      link.parentNode.insertBefore(runLink, link.nextSibling);
    });
  } catch (e) {
    console.log('odoo-runbot-local main page error:', e.message);
  }
}

function init() {
  const path = window.location.pathname;
  console.log('odoo-runbot-local: path =', path);
  if (path.includes('/bundle/')) {
    handleBundlePage();
  } else if (!path.includes('/batch/')) {
    stopPolling();
    processMainPage();
  } else {
    stopPolling();
  }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
