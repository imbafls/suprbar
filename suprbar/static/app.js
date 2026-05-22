// supr.bar — flyout client logic
//
// Data source: /api/today returns the aggregator response shape
// ({today, sources, active, last_session_seen}). Re-rendered every 5s and on
// focus. Keyboard: Esc closes, F5 refreshes, Alt+Q quits, ? help, Ctrl+L
// focuses key field, Ctrl+E exports CSV, Ctrl+W closes.

// Idempotency guard — multiple script injections should be safe.
if (window.__suprbar_inited) {
  // already booted
} else {
  window.__suprbar_inited = true;

const $ = (id) => document.getElementById(id);

const POLL_MS_ACTIVE = 5000;
const POLL_MS_HIDDEN = 60000;
let pollTimer = null;
let pollInterval = POLL_MS_ACTIVE;

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
let lastCost = 0;
let costAnimFrame = null;
let firstRenderDone = false;

// ───────────────────────── Toast system (#2) ─────────────────────────

let _toastTimer = null;
function toast(msg, kind = 'ok', ms = 2400) {
  if (!msg) return;
  let el = document.getElementById('toast');
  if (!el) {
    el = document.createElement('div');
    el.id = 'toast';
    el.style.cssText = [
      'position:fixed', 'bottom:14px', 'left:50%', 'transform:translateX(-50%)',
      'background:rgba(28,30,38,0.96)', 'color:#f4f5f7',
      'font-family:Geist Mono,ui-monospace,monospace', 'font-size:11px',
      'padding:7px 12px', 'border-radius:6px',
      'border:1px solid rgba(255,255,255,0.1)',
      'box-shadow:0 6px 20px rgba(0,0,0,0.5)',
      'opacity:0', 'transition:opacity 160ms ease', 'pointer-events:none',
      'z-index:9999', 'max-width:80%', 'text-align:center',
    ].join(';');
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.dataset.kind = kind;
  // colour based on kind
  if (kind === 'err')   el.style.borderColor = 'rgba(248,113,113,0.55)';
  else if (kind === 'warn') el.style.borderColor = 'rgba(251,191,36,0.55)';
  else                  el.style.borderColor = 'rgba(74,222,128,0.45)';
  el.classList.add('show');
  el.style.opacity = '1';
  if (_toastTimer) clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => {
    el.style.opacity = '0';
    el.classList.remove('show');
  }, ms);
}

// ───────────────────────── Count-up animation (#1) ─────────────────────────

function animateCostTo(target) {
  const start = lastCost;
  const delta = target - start;
  if (Math.abs(delta) < 0.001) {
    paintCost(target);
    lastCost = target;
    return;
  }
  if (costAnimFrame) cancelAnimationFrame(costAnimFrame);
  const t0 = performance.now();
  const dur = 400;
  function step(now) {
    const p = Math.min(1, (now - t0) / dur);
    // ease-out cubic
    const eased = 1 - Math.pow(1 - p, 3);
    const cur = start + delta * eased;
    paintCost(cur);
    if (p < 1) {
      costAnimFrame = requestAnimationFrame(step);
    } else {
      lastCost = target;
      costAnimFrame = null;
    }
  }
  costAnimFrame = requestAnimationFrame(step);
}

function paintCost(v) {
  const { whole, cents } = fmtCost(v);
  const wEl = $('costWhole'); if (wEl) wEl.textContent = whole;
  const cEl = $('costCents'); if (cEl) cEl.textContent = cents;
}

// ───────────────────────── Render ─────────────────────────

function render(d) {
  lastData = d;
  const active = d.active;
  const today = d.today || {};
  const sources = d.sources || [];

  // Cost number — animate change (#1)
  const newCost = Number(today.cost || 0);
  if (!firstRenderDone) {
    paintCost(newCost);
    lastCost = newCost;
  } else if (Math.abs(newCost - lastCost) > 0.001) {
    animateCostTo(newCost);
  }
  $('costNum')?.classList.toggle('idle', !newCost && !active);

  // Label adapts to which sources are active
  const enabledSourceLabels = sources
    .filter(s => s.ok)
    .map(s => {
      if (s.id === 'local') return 'Claude Code';
      if (s.id === 'anthropic_api') return 'API';
      return s.label;
    });
  const lblEl = $('costLabel');
  if (lblEl) lblEl.textContent = 'Today · ' + (enabledSourceLabels.join(' + ') || 'Claude Code');

  // Per-source breakdown line
  const sourceLine = $('sourceLine');
  if (sourceLine) {
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
  }

  // Token mix
  const inT = (today.input || 0);
  const outT = (today.output || 0);
  const cache5 = (today.cache_5m || 0);
  const cache1 = (today.cache_1h || 0);
  const cacheR = (today.cache_read || 0);
  const cacheT = cache5 + cache1 + cacheR;
  const totalT = inT + outT + cacheT;
  const pct = (n) => totalT > 0 ? (n / totalT * 100) : 0;
  const setW = (id, v) => { const el = $(id); if (el) el.style.width = v.toFixed(2) + '%'; };
  setW('tbIn', pct(inT));
  setW('tbOut', pct(outT));
  setW('tbCache', pct(cacheT));
  const setT = (id, v) => { const el = $(id); if (el) el.textContent = v; };
  setT('legIn', fmtTokens(inT));
  setT('legOut', fmtTokens(outT));
  setT('legCache', fmtTokens(cacheT));

  // Optional metrics (HTML agent may add #mBurn, #cacheHit)
  const burnEl = document.getElementById('mBurn');
  if (burnEl) {
    // burn-rate $/hr from today.cost over first-message → now (rough).
    // Use active session start if available, otherwise today's window.
    let rate = 0;
    if (active && active.started_at && newCost > 0) {
      const elapsed = (Date.now() - new Date(active.started_at).getTime()) / 3600000;
      if (elapsed > 0.01) rate = newCost / elapsed;
    }
    burnEl.textContent = rate > 0 ? `$${rate.toFixed(2)}/h` : '—';
  }
  const cacheHitEl = document.getElementById('cacheHit');
  if (cacheHitEl) {
    const totalIn = inT + cacheT;
    const hit = totalIn > 0 ? (cacheR / totalIn) * 100 : 0;
    cacheHitEl.textContent = totalIn > 0 ? hit.toFixed(0) + '%' : '—';
  }

  // Active vs Idle
  const live = $('liveIndicator');
  if (active) {
    if (live) {
      live.hidden = false;
      live.classList.remove('dim');
      live.innerHTML = '<span class="pulse-dot"></span>session live';
    }
    const mr = $('metricRow'); if (mr) mr.hidden = false;
    const es = $('emptyState'); if (es) es.hidden = true;

    setT('mMessages', (active.messages_today ?? 0).toLocaleString());
    setT('mModel', shortModel(active.model));
    startedAt = active.started_at ? new Date(active.started_at) : null;
    updateStartedDisplay();

    const proj = active.project || '~/.claude';
    setT('footMeta', proj.length > 36 ? proj.slice(0, 35) + '…' : proj);
  } else {
    if (live) {
      live.hidden = false;
      live.classList.add('dim');
      live.textContent = 'idle';
    }
    const mr = $('metricRow'); if (mr) mr.hidden = true;
    const es = $('emptyState'); if (es) es.hidden = false;
    startedAt = null;
    if (d.last_session_seen) {
      const last = new Date(d.last_session_seen.last_activity);
      const ago = (Date.now() - last.getTime()) / 1000;
      setT('emptySub', `last seen ${fmtAgo(ago)}`);
    } else {
      setT('emptySub', 'watching ~/.claude');
    }
    setT('footMeta', 'watching ~/.claude');
  }

  // Optional projects list (#projectsList / #projectsListItems may be added)
  const pl = document.getElementById('projectsListItems');
  if (pl && Array.isArray(d.projects)) {
    pl.innerHTML = d.projects.slice(0, 6).map(pr =>
      `<li>${escape(pr.name || pr.path || 'project')} <span class="pill">$${(pr.cost || 0).toFixed(2)}</span></li>`
    ).join('');
  }

  // #20 — remove .loading after first successful render
  if (!firstRenderDone) {
    firstRenderDone = true;
    document.body.classList.remove('loading');
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

// ───────────────────────── Fetch w/ retry + abort (#15 #16 #18) ─────────────────────────

let inflightController = null;
let consecutiveFailures = 0;
let nextBackoff = 1000;
const MAX_BACKOFF = 30000;
let retryTimer = null;

function statusMessage(status, errMsg) {
  if (status === 503) return 'service starting…';
  if (status === 500) return 'server error';
  if (status === 401 || status === 403) return 'auth issue';
  if (status === 0 || /network|failed to fetch|aborted/i.test(errMsg || '')) return 'offline?';
  if (status) return 'http ' + status;
  return errMsg || 'error';
}

async function load({ refresh = false } = {}) {
  // Cancel any in-flight request (#18)
  if (inflightController) {
    try { inflightController.abort(); } catch (_) { /* ignore */ }
  }
  inflightController = new AbortController();
  const signal = inflightController.signal;

  try {
    const res = await fetch(
      refresh ? '/api/today?refresh=1' : '/api/today',
      { cache: 'no-store', signal },
    );
    if (!res.ok) {
      const msg = statusMessage(res.status);
      // surface obvious problems
      if (res.status === 401 || res.status === 403) toast(msg, 'err');
      throw new Error('http ' + res.status);
    }
    const data = await res.json();
    render(data);
    // success — reset backoff
    consecutiveFailures = 0;
    nextBackoff = 1000;
    if (retryTimer) { clearTimeout(retryTimer); retryTimer = null; }
  } catch (e) {
    if (e?.name === 'AbortError') return;
    consecutiveFailures++;
    const msg = statusMessage(0, String(e?.message || e));
    if (consecutiveFailures === 3) {
      toast('connection lost — ' + msg, 'err', 3200);
    }
    // schedule retry with exponential backoff
    if (retryTimer) clearTimeout(retryTimer);
    const delay = Math.min(nextBackoff, MAX_BACKOFF);
    nextBackoff = Math.min(nextBackoff * 2, MAX_BACKOFF);
    retryTimer = setTimeout(() => load(), delay);
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
    const adm = $('adminKeyInput');
    if (adm) {
      adm.value = '';
      adm.placeholder = anth.has_key
        ? `saved: ${anth.key_fingerprint || '••••'}`
        : 'sk-ant-admin01-…';
    }

    const ui = c.ui || {};
    setToggle($('pinnedToggle'), !!ui.pinned);
    setToggle($('startupToggle'), !!ui.start_on_login);
    syncPinButton(!!ui.pinned);

    // #13 — per-source view (read from /api/today response cached in lastData)
    renderSourcesPanel();
  } catch (e) { /* swallow */ }
}

function renderSourcesPanel() {
  const host = document.getElementById('sourcesPanel');
  if (!host || !lastData) return;
  const sources = lastData.sources || [];
  host.innerHTML = sources.map(s => {
    const status = s.ok
      ? `<span class="status-line ok">ok</span>`
      : `<span class="status-line err">${escape(s.error || 'error')}</span>`;
    const cost = s.ok ? `$${(s.cost_today || 0).toFixed(2)}` : '—';
    const updated = s.last_updated ? fmtAgo((Date.now() - new Date(s.last_updated).getTime())/1000) : '';
    return `<div class="settings-row"><div class="settings-row-main">
      <div class="lbl">${escape(s.label || s.id)} <span class="pill">${escape(s.id)}</span></div>
      <div class="desc">${cost} ${updated ? '· ' + escape(updated) : ''}</div>
      ${status}
    </div></div>`;
  }).join('');
}

function setToggle(el, on) {
  if (!el) return;
  el.classList.toggle('on', !!on);
  el.dataset.on = on ? '1' : '0';
}
function toggleValue(el) { return el?.classList.contains('on'); }

async function patchConfig(body) {
  try {
    const res = await fetch('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      let msg = 'http ' + res.status;
      try {
        const j = await res.json();
        if (j?.error) msg = j.error;
      } catch (_) { /* ignore */ }
      toast('save failed: ' + msg, 'err');           // #19
      return null;
    }
    const j = await res.json();
    toast('saved', 'ok', 1400);                       // #19
    return j;
  } catch (e) {
    toast('save failed: ' + (e?.message || e), 'err'); // #19
    return null;
  }
}

function applyTabOrder() {                            // #9
  const order = [
    'adminKeyInput', 'testKeyBtn', 'clearKeyBtn',
    'anthropicToggle', 'pinnedToggle', 'startupToggle',
    'settingsCloseBtn',
  ];
  order.forEach((id, i) => {
    const el = document.getElementById(id);
    if (el) el.setAttribute('tabindex', String(i + 1));
  });
}

function openSettings() {
  if (!overlay) return;
  overlay.hidden = false;
  loadConfig();
  applyTabOrder();                                     // #9
  // #10 — auto-focus admin key after overlay shown
  setTimeout(() => {
    const k = document.getElementById('adminKeyInput');
    if (k) try { k.focus(); } catch (_) { /* ignore */ }
  }, 50);
}
function closeSettings() { if (overlay) overlay.hidden = true; }

function syncPinButton(on) {
  $('pinBtn')?.classList.toggle('on', !!on);
}

$('settingsBtn')?.addEventListener('click', openSettings);
$('settingsCloseBtn')?.addEventListener('click', closeSettings);

$('pinBtn')?.addEventListener('click', async () => {
  const next = !$('pinBtn').classList.contains('on');
  syncPinButton(next);
  await patchConfig({ pinned: next });
});

$('anthropicToggle')?.addEventListener('click', async () => {
  const next = !toggleValue($('anthropicToggle'));
  setToggle($('anthropicToggle'), next);
  await patchConfig({ anthropic_api_enabled: next });
  load({ refresh: true });
});

$('pinnedToggle')?.addEventListener('click', async () => {
  const next = !toggleValue($('pinnedToggle'));
  setToggle($('pinnedToggle'), next);
  await patchConfig({ pinned: next });
  syncPinButton(next);
});

$('startupToggle')?.addEventListener('click', async () => {
  const next = !toggleValue($('startupToggle'));
  setToggle($('startupToggle'), next);
  const r = await patchConfig({ start_on_login: next });
  if (!r) {
    setToggle($('startupToggle'), !next);
  }
});

async function runTestKey() {                          // factored for #11
  const key = $('adminKeyInput')?.value.trim();
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
}

$('testKeyBtn')?.addEventListener('click', runTestKey);

$('clearKeyBtn')?.addEventListener('click', async () => {
  await patchConfig({ anthropic_api_key: '', anthropic_api_enabled: false });
  const adm = $('adminKeyInput'); if (adm) adm.value = '';
  setKeyStatus('cleared', 'ok');
  loadConfig();
  load({ refresh: true });
});

// #11 — Enter in admin key triggers Test
$('adminKeyInput')?.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') {
    e.preventDefault();
    $('testKeyBtn')?.click();
  }
});

function setKeyStatus(msg, kind) {
  const el = $('keyStatus');
  if (!el) return;
  el.textContent = msg;
  el.classList.remove('ok', 'err');
  if (kind) el.classList.add(kind);
}

// ───────────────────────── Footer buttons ─────────────────────────

$('openLogsBtn')?.addEventListener('click', () => {
  fetch('/api/today').then(r => r.json()).then(d => {
    const target = (d.active && d.active.path) || '~/.claude/projects';
    fetch('/api/open-path', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ p: target }),
    });
  });
});

// ───────────────────────── CSV export (#5) ─────────────────────────

function exportTodayCSV() {
  const d = lastData || {};
  const today = d.today || {};
  const date = new Date().toISOString().slice(0, 10);
  const sessions = (d.last_session_seen ? 1 : 0) + (d.active ? 1 : 0);
  const headers = ['date','cost','messages','input_tokens','output_tokens','cache_5m','cache_1h','cache_read','sessions'];
  const row = [
    date,
    (today.cost || 0).toFixed(4),
    today.messages || (d.active?.messages_today ?? 0),
    today.input || 0,
    today.output || 0,
    today.cache_5m || 0,
    today.cache_1h || 0,
    today.cache_read || 0,
    sessions,
  ];
  const csv = headers.join(',') + '\n' + row.join(',') + '\n';
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `suprbar-today-${date}.csv`;
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { document.body.removeChild(a); URL.revokeObjectURL(url); }, 200);
  toast('CSV downloaded');
}

// ───────────────────────── Click cost to copy (#8) ─────────────────────────

$('costNum')?.addEventListener('click', async () => {
  const today = (lastData && lastData.today) || {};
  const v = Number(today.cost || 0);
  const txt = '$' + v.toFixed(2);
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(txt);
      toast('copied ' + txt);
    } else {
      // fallback
      const ta = document.createElement('textarea');
      ta.value = txt;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
      toast('copied ' + txt);
    }
  } catch (e) {
    toast('copy failed', 'err');
  }
});
// pointer affordance
if ($('costNum')) $('costNum').style.cursor = 'pointer';

// ───────────────────────── Custom context menu (#12) ─────────────────────────

let _ctxMenu = null;
function closeCtxMenu() {
  if (_ctxMenu) { _ctxMenu.remove(); _ctxMenu = null; }
}
function openCtxMenu(x, y) {
  closeCtxMenu();
  const m = document.createElement('div');
  _ctxMenu = m;
  m.style.cssText = [
    'position:fixed', `left:${x}px`, `top:${y}px`,
    'background:rgba(28,30,38,0.98)',
    'border:1px solid rgba(255,255,255,0.1)',
    'border-radius:6px', 'padding:4px',
    'box-shadow:0 8px 24px rgba(0,0,0,0.5)',
    'font-family:Geist Mono,ui-monospace,monospace', 'font-size:11px',
    'color:#f4f5f7', 'min-width:130px', 'z-index:9998',
  ].join(';');
  const pinned = $('pinBtn')?.classList.contains('on');
  const items = [
    { label: 'Settings',       fn: openSettings },
    { label: 'Refresh',        fn: () => { load({ refresh: true }); toast('refreshing…', 'ok', 900); } },
    { label: pinned ? 'Unpin' : 'Pin', fn: () => $('pinBtn')?.click() },
    { label: 'Quit',           fn: () => fetch('/api/quit', { method: 'POST' }) },
  ];
  items.forEach(it => {
    const b = document.createElement('button');
    b.textContent = it.label;
    b.style.cssText = [
      'display:block', 'width:100%', 'text-align:left',
      'background:transparent', 'border:none', 'color:inherit',
      'padding:6px 10px', 'font:inherit', 'cursor:pointer',
      'border-radius:4px',
    ].join(';');
    b.addEventListener('mouseenter', () => b.style.background = 'rgba(255,255,255,0.07)');
    b.addEventListener('mouseleave', () => b.style.background = 'transparent');
    b.addEventListener('click', (e) => { e.stopPropagation(); closeCtxMenu(); it.fn(); });
    m.appendChild(b);
  });
  document.body.appendChild(m);
  // keep menu within viewport
  const r = m.getBoundingClientRect();
  if (r.right > window.innerWidth) m.style.left = (window.innerWidth - r.width - 4) + 'px';
  if (r.bottom > window.innerHeight) m.style.top = (window.innerHeight - r.height - 4) + 'px';
}

document.addEventListener('contextmenu', (e) => {
  e.preventDefault();
  openCtxMenu(e.clientX, e.clientY);
});
document.addEventListener('click', (e) => {
  if (_ctxMenu && !_ctxMenu.contains(e.target)) closeCtxMenu();
});

// ───────────────────────── Shortcuts help (#3) ─────────────────────────

function toggleShortcutsHelp() {
  const help = document.getElementById('shortcutsHelp');
  if (!help) {
    // fallback toast
    toast('? ESC F5 Ctrl+E Ctrl+L Ctrl+W Ctrl+,  Alt+Q', 'ok', 3200);
    return;
  }
  // <dialog> if available, otherwise toggle hidden
  if (typeof help.showModal === 'function') {
    if (help.open) help.close();
    else try { help.showModal(); } catch (_) { help.hidden = !help.hidden; }
  } else {
    help.hidden = !help.hidden;
  }
}

// ───────────────────────── Keyboard shortcuts ─────────────────────────

document.addEventListener('keydown', (e) => {
  // Ctrl+W → close popup (#6)
  if (e.ctrlKey && (e.key === 'w' || e.key === 'W')) {
    e.preventDefault();
    if (window.pywebview?.api?.hide) window.pywebview.api.hide();
    return;
  }
  // Ctrl+R or F5 → refresh (#7)
  if (e.key === 'F5' || (e.ctrlKey && (e.key === 'r' || e.key === 'R'))) {
    e.preventDefault();
    toast('refreshing…', 'ok', 900);
    load({ refresh: true });
    return;
  }
  // Ctrl+E → CSV export (#5)
  if (e.ctrlKey && (e.key === 'e' || e.key === 'E')) {
    e.preventDefault();
    exportTodayCSV();
    return;
  }
  // Ctrl+L → focus admin key (#4)
  if (e.ctrlKey && (e.key === 'l' || e.key === 'L')) {
    e.preventDefault();
    if (overlay && overlay.hidden) openSettings();
    setTimeout(() => $('adminKeyInput')?.focus(), 80);
    return;
  }
  // Ctrl+, opens settings
  if (e.ctrlKey && e.key === ',') {
    e.preventDefault();
    overlay && (overlay.hidden ? openSettings() : closeSettings());
    return;
  }
  // ? toggles shortcuts help (#3)
  if (e.key === '?' && !e.ctrlKey && !e.metaKey) {
    // ignore when typing in inputs
    const t = e.target;
    if (t && /^(INPUT|TEXTAREA)$/.test(t.tagName)) return;
    e.preventDefault();
    toggleShortcutsHelp();
    return;
  }
  // Alt+Q quits
  if (e.altKey && (e.key === 'q' || e.key === 'Q')) {
    e.preventDefault();
    fetch('/api/quit', { method: 'POST' });
    return;
  }
  // Esc — close menu, then settings, then hide
  if (e.key === 'Escape') {
    if (_ctxMenu) { closeCtxMenu(); e.preventDefault(); return; }
    if (overlay && !overlay.hidden) {
      closeSettings();
      e.preventDefault();
      return;
    }
    if (window.pywebview?.api?.hide) window.pywebview.api.hide();
    return;
  }
});

// ───────────────────────── Auto-hide on blur ─────────────────────────

window.addEventListener('blur', () => {
  try {
    if (window.pywebview?.api?.hide) window.pywebview.api.hide();
  } catch (e) { /* swallow */ }
});

// ───────────────────────── Polling + visibility (#14) ─────────────────────────

function setPollInterval(ms) {
  if (pollInterval === ms && pollTimer) return;
  pollInterval = ms;
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(load, ms);
}

document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    setPollInterval(POLL_MS_HIDDEN);
  } else {
    setPollInterval(POLL_MS_ACTIVE);
    load({ refresh: true });
  }
});
window.addEventListener('focus', () => load({ refresh: true }));

// ───────────────────────── Initial boot ─────────────────────────

document.body.classList.add('loading');                // matches CSS skeleton, removed after #20
load({ refresh: true });
loadConfig();
setPollInterval(POLL_MS_ACTIVE);
setInterval(updateStartedDisplay, 1000);

// Expose a tiny debug surface — useful in DevTools.
window.suprbar = { load, loadConfig, toast, exportTodayCSV };

} // end idempotency guard
