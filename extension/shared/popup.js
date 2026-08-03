// Kept in a separate file: MV3's extension-page CSP (script-src 'self')
// silently blocks inline <script>, which is why the popup used to stay blank.
const statusEl = document.getElementById('status');
const detailEl = document.getElementById('detail');
const actionsEl = document.getElementById('actions');

function send(action) {
  return new Promise((resolve) => chrome.runtime.sendMessage({ action }, resolve));
}

function render(className, text, detail) {
  statusEl.className = `status ${className}`;
  statusEl.textContent = text;
  detailEl.textContent = detail || '';
}

async function showUnreachable() {
  render('err', '● Server unreachable', 'Restart it: systemctl --user restart odoo-runbot-local');
}

async function showProblems() {
  // The server is up but cannot work — say exactly why instead of "unreachable".
  const health = await send('health');
  if (health && Array.isArray(health.problems) && health.problems.length) {
    render('err', '● Not ready', health.problems[0]);
    return true;
  }
  return false;
}

async function refresh() {
  const data = await send('status');

  if (!data || data._reachable === false) {
    await showUnreachable();
    return;
  }

  if (data.running && data.alive) {
    const mins = Math.floor((data.uptime_seconds || 0) / 60);
    render('ok', '● Running',
      `Branch: ${data.branch} | DB: ${data.db_name} | Uptime: ${mins}m`);

    const port = data.port || 8072;
    actionsEl.style.display = 'block';
    actionsEl.textContent = '';

    const open = document.createElement('a');
    open.className = 'btn-open';
    open.href = `http://127.0.0.1:${port}`;
    open.target = '_blank';
    open.textContent = 'Open Odoo';

    const stop = document.createElement('a');
    stop.className = 'btn-stop';
    stop.textContent = 'Stop';
    stop.addEventListener('click', async () => {
      stop.textContent = 'Stopping...';
      const res = await send('stop');
      if (res && res.status === 'stopped') {
        render('idle', '○ Stopped', 'Instance stopped and database dropped');
        actionsEl.style.display = 'none';
      } else {
        render('err', '● Stop failed', (res && res.error) || 'Unknown error');
      }
    });

    actionsEl.append(open, stop);
    return;
  }

  actionsEl.style.display = 'none';
  if (data.phase) {
    render('loading', '● Working', data.phase);
    setTimeout(refresh, 2000);
    return;
  }
  if (await showProblems()) return;
  render('idle', '○ Idle', 'Click "Run locally" on a runbot bundle page');
}

refresh();
