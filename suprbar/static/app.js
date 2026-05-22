// supr.bar — flyout client logic
//
// Data source: /api/today returns the aggregator response shape
// ({today, sources, active, last_session_seen}). Re-rendered every 5s and on
// focus. Keyboard: Esc closes, F5 refreshes, Alt+Q quits.

const $ = (id) => document.getElementById(id);

const POLL_MS = 5000;

function fmtTokens(n) {
  n = Number(n || 0);
  if (n >= 1e9) return (n / 1e9).toFixed(2) + 'B';
  if (n >= 1e6) return (n / 1e6).toFixed(2) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k';
  return String(n);
}

function fmtCost(n) {
  n = Number(n || 0);
  const whole = Math.floor(n);
  const cents = (n - whole).toFixed(2).slice(1);
  return { whole: whole.toLocaleString(), cents };
}

function fmtDuration(seconds) {
  seconds = Math.max(0, Math.floor(seconds));
  if (seconds < 60) return seconds + 's';
  const m = Math.floor(seconds / 60);
  if (m < 60) return m + 'm';
  const h = Math.floor(m / 60);
  const mm = m % 60;
  if (h < 24) return `${h}h ${mm}m`;
  const d = Math.floor(h / 24);
  const hh = h % 24;
  return `${d}d ${hh}h`;
}

function fmtAgo(seconds) { return fmtDuration(seconds) + ' ago'; }

function shortModel(model) {
  if (!model) return '—';
  const m = model.match(/^claude-(opus|sonnet|haiku)-(\d+)-(\d+)(?:\[(\d+m)\])?/i);
  if (m) {
    const ctx = m[4] ? ` ${m[4]}` : '';
    return `${m[1]}-${m[2]}.${m[3]}${ctx}`;
  }
  return model.replace(/^claude-/, '');
}

let startedAt = null;
let lastData = null;

function render(d) {
  lastData = d;
  const active = d.active;
  const today = d.today || {};
  const sources = d.sources || [];

  // Cost number
  const { whole, cents } = fmtCost(today.cost || 0);
  $('costWhole').textContent = whole;
  $('costCents').textContent = cents;
  $('costNum').classList.toggle('idle', !(today.cost || 0) && !active);

  // Label adapts to which sources are active
  const enabledSourceLabels = sources
    .filter(s => s.ok)
    .map(s => {
      if (s.id === 'local') return 'Claude Code';
      if (s.id === 'anthropic_api') return 'API';
      return s.label;
    });
  $('costLabel').textContent = 'Today · ' + (enabledSourceLabels.join(' + ') || 'Claude Code');

  // Per-source breakdown line — shown when more than one source is active OR
  // the only source has a failure to report.
  const sourceLine = $('sourceLine');
  if (sources.length > 1 || sources.some(s => !s.ok && s.error && s.error !== 'disabled')) {
    sourceLine.hidden = false;
    sourceLine.innerHTML = sources.map(s => {
      if (!s.ok) {
        if (s.error === 'disabled' || s.error === 'no admin key configured') {
          return '';
        }
        return `<span class="pill" title="${escapeAttr(s.error || '')}">${escape(srcLabel(s.id))} · err</span>`;
      }
      return `<span class="pill">${escape(srcLabel(s.id))} $${s.cost_today.toFixed(2)}</span>`;
    }).filter(Boolean).join('');
  } else {
    sourceLine.hidden = true;
    sourceLine.innerHTML = '';
  }

  // Token mix
  const inT = (today.input || 0);
  const outT = (today.output || 0);
  const cacheT = (today.cache_5m || 0) + (today.cache_1h || 0) + (today.cache_read || 0);
  const totalT = inT + outT + cacheT;
  const pct = (n) => totalT > 0 ? (n / totalT * 100) : 0;
  $('tbIn').style.width    = pct(inT).toFixed(2) + '%';
  $('tbOut').style.width   = pct(outT).toFixed(2) + '%';
  $('tbCache').style.width = pct(cacheT).toFixed(2) + '%';
  $('legIn').textContent    = fmtTokens(inT);
  $('legOut').textContent   = fmtTokens(outT);
  $('legCache').textContent = fmtTokens(cacheT);

  // Active vs Idle
  if (active) {
    $('liveIndicator').hidden = false;
    $('liveIndicator').classList.remove('dim');
    $('liveIndicator').innerHTML = '<span class="pulse-dot"></span>session live';
    $('metricRow').hidden = false;
    $('emptyState').hidden = true;

    $('mMessages').textContent = (active.messages_today ?? 0).toLocaleString();
    $('mModel').textContent = shortModel(active.model);
    startedAt = active.started_at ? new Date(active.started_at) : null;
    updateStartedDisplay();

    const proj = active.project || '~/.claude';
    $('footMeta').textContent = proj.length > 36 ? proj.slice(0, 35) + '…' : proj;
  } else {
    $('liveIndicator').hidden = false;
    $('liveIndicator').classList.add('dim');
    $('liveIndicator').textContent = 'idle';
    $('metricRow').hidden = true;
    $('emptyState').hidden = false;
    startedAt = null;
    if (d.last_session_seen) {
      const last = new Date(d.last_session_seen.last_activity);
      const ago = (Date.now() - last.getTime()) / 1000;
      $('emptySub').textContent = `last seen ${fmtAgo(ago)}`;
    } else {
      $('emptySub').textContent = 'watching ~/.claude';
    }
    $('footMeta').textContent = 'watching ~/.claude';
  }
}

function srcLabel(id) {
  return id === 'local' ? 'local' : id === 'anthropic_api' ? 'api' : id;
}
function escape(s) {
  return String(s).replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}
function escapeAttr(s) { return escape(s); }

function updateStartedDisplay() {
  if (!startedAt) return;
  const el = $('mStarted');
  if (el) el.textContent = fmtDuration((Date.now() - startedAt.getTime()) / 1000);
}

async function load({ refresh = false } = {}) {
  try {
    const res = await fetch(refresh ? '/api/today?refresh=1' : '/api/today',
                            { cache: 'no-store' });
    if (!res.ok) throw new Error('http ' + res.status);
    render(await res.json());
  } catch (e) {
    console.warn('today fetch failed', e);
  }
}

// ───────────────────────── Settings overlay ─────────────────────────

const overlay = $('settingsOverlay');

async function loadConfig() {
  try {
    const res = await fetch('/api/config', { cache: 'no-store' });
    if (!res.ok) return;
    const c = await res.json();
    const anth = c.sources?.anthropic_api || {};
    setToggle($('anthropicToggle'), !!anth.enabled);
    $('adminKeyInput').value = '';
    $('adminKeyInput').placeholder = anth.has_key
      ? `saved: ${anth.key_fingerprint || '••••'}`
      : 'sk-ant-admin01-…';

    const ui = c.ui || {};
    setToggle($('pinnedToggle'), !!ui.pinned);
    setToggle($('startupToggle'), !!ui.start_on_login);
    syncPinButton(!!ui.pinned);
  } catch (e) { /* swallow */ }
}

function setToggle(el, on) {
  if (!el) return;
  el.classList.toggle('on', !!on);
  el.dataset.on = on ? '1' : '0';
}
function toggleValue(el) { return el?.classList.contains('on'); }

async function patchConfig(body) {
  const res = await fetch('/api/config', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return res.ok ? res.json() : null;
}

function openSettings() {
  overlay.hidden = false;
  loadConfig();
}
function closeSettings() { overlay.hidden = true; }

function syncPinButton(on) {
  $('pinBtn').classList.toggle('on', !!on);
}

$('settingsBtn').addEventListener('click', openSettings);
$('settingsCloseBtn').addEventListener('click', closeSettings);

$('pinBtn').addEventListener('click', async () => {
  const next = !$('pinBtn').classList.contains('on');
  syncPinButton(next);
  await patchConfig({ pinned: next });
});

$('anthropicToggle').addEventListener('click', async () => {
  const next = !toggleValue($('anthropicToggle'));
  setToggle($('anthropicToggle'), next);
  await patchConfig({ anthropic_api_enabled: next });
  load({ refresh: true });
});

$('pinnedToggle').addEventListener('click', async () => {
  const next = !toggleValue($('pinnedToggle'));
  setToggle($('pinnedToggle'), next);
  await patchConfig({ pinned: next });
  syncPinButton(next);
});

$('startupToggle').addEventListener('click', async () => {
  const next = !toggleValue($('startupToggle'));
  setToggle($('startupToggle'), next);
  const r = await patchConfig({ start_on_login: next });
  if (!r) {
    setToggle($('startupToggle'), !next);
  }
});

$('testKeyBtn').addEventListener('click', async () => {
  const key = $('adminKeyInput').value.trim();
  if (!key) {
    setKeyStatus('paste a key first', 'err');
    return;
  }
  setKeyStatus('testing…');
  try {
    const res = await fetch('/api/config/test-key', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key }),
    });
    const j = await res.json();
    if (j.ok) {
      // Save automatically on a successful test
      await patchConfig({ anthropic_api_key: key, anthropic_api_enabled: true });
      setKeyStatus('saved & connected', 'ok');
      setToggle($('anthropicToggle'), true);
      $('adminKeyInput').value = '';
      loadConfig();
      load({ refresh: true });
    } else {
      setKeyStatus(j.message || 'connection failed', 'err');
    }
  } catch (e) {
    setKeyStatus('request failed: ' + e, 'err');
  }
});

$('clearKeyBtn').addEventListener('click', async () => {
  await patchConfig({ anthropic_api_key: '', anthropic_api_enabled: false });
  $('adminKeyInput').value = '';
  setKeyStatus('cleared', 'ok');
  loadConfig();
  load({ refresh: true });
});

function setKeyStatus(msg, kind) {
  const el = $('keyStatus');
  el.textContent = msg;
  el.classList.remove('ok', 'err');
  if (kind) el.classList.add(kind);
}

// ───────────────────────── Footer buttons ─────────────────────────

$('openLogsBtn').addEventListener('click', () => {
  fetch('/api/today').then(r => r.json()).then(d => {
    const target = (d.active && d.active.path) || '~/.claude/projects';
    fetch('/api/open-path', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ p: target }),
    });
  });
});

// ───────────────────────── Keyboard shortcuts ─────────────────────────

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    if (!overlay.hidden) {
      closeSettings();
      e.preventDefault();
      return;
    }
    if (window.pywebview?.api?.hide) window.pywebview.api.hide();
    return;
  }
  if (e.key === 'F5') {
    e.preventDefault();
    load({ refresh: true });
    return;
  }
  // Alt+Q quits
  if (e.altKey && (e.key === 'q' || e.key === 'Q')) {
    e.preventDefault();
    fetch('/api/quit', { method: 'POST' });
    return;
  }
  // Ctrl+, opens settings
  if (e.ctrlKey && e.key === ',') {
    e.preventDefault();
    overlay.hidden ? openSettings() : closeSettings();
    return;
  }
});

// ───────────────────────── Auto-hide on blur ─────────────────────────

window.addEventListener('blur', () => {
  try {
    if (window.pywebview?.api?.hide) window.pywebview.api.hide();
  } catch (e) { /* swallow */ }
});

document.addEventListener('visibilitychange', () => {
  if (!document.hidden) load({ refresh: true });
});
window.addEventListener('focus', () => load({ refresh: true }));

// Initial
load({ refresh: true });
loadConfig();
setInterval(load, POLL_MS);
setInterval(updateStartedDisplay, 1000);
