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
  // Range routing: if user picked a non-"today" tab, fetch /api/range instead.
  // Using a window property so the let-binding (further down) is irrelevant.
  if (window.__suprbar_range && window.__suprbar_range !== 'today') {
    return loadRange({ refresh });
  }
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

// ════════════════════════════════════════════════════════════════════════
//  Range tabs + budgets + dynamic settings (50+ prefs)
// ════════════════════════════════════════════════════════════════════════

let prefsCache = null;
let schemaCache = null;
let currentRange = 'today';

const SECTION_TITLES = {
  range:    'Time range',
  display:  'Display',
  budgets:  'Budgets & alerts',
  behavior: 'Behavior',
  projects: 'Projects',
  sources:  'Sources',
  keyboard: 'Keyboard',
  data:     'Data & privacy',
  window:   'Window',
  ui:       'Legacy UI',
};

const SECTION_ORDER = ['range','display','budgets','behavior','projects',
                       'sources','keyboard','data','window'];

const LABELS = {
  // range
  'range.default':          { label: 'Default range',         desc: 'Time range applied when popup opens.' },
  'range.custom_start':     { label: 'Custom start date',     desc: 'Used when range is "custom".' },
  'range.custom_end':       { label: 'Custom end date',       desc: 'Used when range is "custom".' },
  'range.week_starts_on':   { label: 'Week starts on',        desc: 'Affects the "Wk" range tab.' },
  'range.day_boundary':     { label: 'Day boundary',          desc: 'Compute "today" by local time or UTC.' },
  'range.rolling_24h':      { label: 'Rolling 24h "today"',   desc: 'Use last 24 hours instead of calendar day.' },
  'range.include_weekends': { label: 'Include weekends',      desc: 'Uncheck to exclude Sat/Sun from totals.' },
  'range.compare_previous': { label: 'Compare to previous',   desc: 'Show delta vs prior period (planned).' },
  // display
  'display.theme':          { label: 'Theme',                 desc: 'Dark, light, or follow OS.' },
  'display.accent':         { label: 'Accent color',          desc: 'Tints highlights and pin.' },
  'display.density':        { label: 'Density',               desc: 'Compact, normal, or spacious padding.' },
  'display.font_scale':     { label: 'Font scale',            desc: '0.85× to 1.25× the base size.' },
  'display.currency':       { label: 'Currency symbol',       desc: 'Cosmetic — does not convert (USD only).' },
  'display.cost_format':    { label: 'Cost format',           desc: 'Show cents or round to whole dollars.' },
  'display.token_format':   { label: 'Token format',          desc: '"1.2k" compact or "1,234" full.' },
  'display.locale':         { label: 'Number locale',         desc: 'BCP-47 tag (e.g. en-US, de-DE).' },
  'display.show_token_bar':     { label: 'Show token mix bar',     desc: 'Input / output / cache ratio bar.' },
  'display.show_cache_info':    { label: 'Show cache info',        desc: 'Cache-hit % + cache token count.' },
  'display.show_burn_rate':     { label: 'Show burn rate',         desc: 'Live $/hour for the active session.' },
  'display.show_model':         { label: 'Show model name',        desc: 'Current model in the metric trio.' },
  'display.show_project':       { label: 'Show project name',      desc: 'Project shown in the footer.' },
  'display.show_sessions_today': { label: 'Show session count',    desc: 'Distinct sessions today.' },
  'display.animations':         { label: 'Animations',             desc: 'Disable for reduced motion.' },
  // budgets
  'budgets.daily_limit':    { label: 'Daily limit ($)',       desc: 'Per-day cap. 0 = no limit.' },
  'budgets.weekly_limit':   { label: 'Weekly limit ($)',      desc: 'Per-week cap. 0 = no limit.' },
  'budgets.monthly_limit':  { label: 'Monthly limit ($)',     desc: 'Per-month cap. 0 = no limit.' },
  'budgets.alert_at_pct':   { label: 'Alert threshold (%)',   desc: 'Warn when % of any limit is reached.' },
  'budgets.notify':         { label: 'Toast on warning',      desc: 'Show in-popup notification.' },
  'budgets.tray_warn_color': { label: 'Tint tray icon',       desc: 'Tray icon turns red when over budget.' },
  'budgets.audio_alert':    { label: 'Audio alert',           desc: 'System sound on budget exceeded.' },
  'budgets.quiet_hours':    { label: 'Quiet hours',           desc: 'Suppress alerts during a time window.' },
  'budgets.quiet_start':    { label: 'Quiet start hour',      desc: '0-23, used when quiet_hours = custom.' },
  'budgets.quiet_end':      { label: 'Quiet end hour',        desc: '0-23, used when quiet_hours = custom.' },
  // behavior
  'behavior.refresh_seconds':      { label: 'Refresh interval',      desc: 'Seconds between auto-refreshes. 0 = manual only.' },
  'behavior.auto_hide':            { label: 'Auto-hide on blur',     desc: 'Hide popup when focus moves away.' },
  'behavior.auto_hide_delay_ms':   { label: 'Auto-hide delay (ms)',  desc: 'Grace period before hiding.' },
  'behavior.always_on_top':        { label: 'Always on top',         desc: 'Popup stays above other windows.' },
  'behavior.show_in_taskbar':      { label: 'Show in taskbar',       desc: 'Add a taskbar entry while open.' },
  'behavior.live_threshold_seconds': { label: 'Live session window', desc: 'Sessions touched in last N seconds are "live".' },
  'behavior.start_minimized':      { label: 'Start minimized',       desc: 'Boot to tray-only (no popup).' },
  'behavior.confirm_quit':         { label: 'Confirm before quit',   desc: 'Prompt before Alt+Q closes the app.' },
  'behavior.click_through':        { label: 'Click-through mode',    desc: 'Popup ignores mouse clicks (header still draggable).' },
  'behavior.single_instance':      { label: 'Single instance',       desc: 'Prevent multiple suprbar processes.' },
  'behavior.open_dashboard_on_click': { label: 'Left-click opens',   desc: 'Tray left-click toggles popup.' },
  // projects
  'projects.allowlist':     { label: 'Allowlist',             desc: 'Comma-separated. If non-empty, only these are shown.' },
  'projects.denylist':      { label: 'Denylist',              desc: 'Always hidden. Useful for personal/secret repos.' },
  'projects.anonymize':     { label: 'Anonymize names',       desc: 'Replace with "project-1/2/3" in UI.' },
  'projects.top_n':         { label: 'Top N',                 desc: 'Number of projects in the "Top projects" list.' },
  // keyboard
  'keyboard.enable_global':    { label: 'Global hotkeys',     desc: 'OS-wide shortcuts (planned).' },
  'keyboard.hotkey_toggle':    { label: 'Show / hide hotkey', desc: 'e.g. Ctrl+Alt+S.' },
  'keyboard.hotkey_refresh':   { label: 'Refresh hotkey',     desc: 'In-popup refresh key.' },
  'keyboard.hotkey_settings':  { label: 'Settings hotkey',    desc: 'Opens this panel.' },
  'keyboard.hotkey_quit':      { label: 'Quit hotkey',        desc: 'Closes the app.' },
  'keyboard.hotkey_export':    { label: 'Export CSV hotkey',  desc: 'Saves today as CSV.' },
  'keyboard.hotkey_copy_cost': { label: 'Copy cost hotkey',   desc: 'Copies "$X.XX" to clipboard.' },
  'keyboard.vim_keys':         { label: 'Vim-style navigation', desc: 'j / k navigation in lists.' },
  // data
  'data.log_level':          { label: 'Log level',            desc: 'Verbosity of suprbar.log.' },
  'data.log_retention_days': { label: 'Log retention (days)', desc: 'How long to keep log files.' },
  'data.anonymize_logs':     { label: 'Anonymize project names in logs', desc: 'Replace project paths with hashes.' },
  'data.cache_ttl_seconds':  { label: 'API cache TTL (s)',    desc: 'How long /api/today caches between requests.' },
  'data.telemetry':          { label: 'Anonymous telemetry',  desc: 'Currently a no-op. Reserved.' },
  // window
  'window.anchor':            { label: 'Default anchor',      desc: 'Corner the popup snaps to on first launch.' },
  'window.margin_px':         { label: 'Edge margin (px)',    desc: 'Gap between popup and screen edge.' },
  'window.preferred_monitor': { label: 'Preferred monitor',   desc: '0 = monitor with cursor.' },
  'window.remember_position': { label: 'Remember position',   desc: 'Save where you drag it.' },
  'window.width':             { label: 'Width (px)',          desc: 'Popup width.' },
  'window.height':            { label: 'Height (px)',         desc: 'Popup height.' },
  'window.opacity':           { label: 'Opacity',             desc: 'Window transparency (0.5–1.0).' },
  // sources
  'sources.local.enabled':              { label: 'Local source',            desc: 'Reads ~/.claude/projects/**/*.jsonl.' },
  'sources.anthropic_api.enabled':      { label: 'Anthropic API source',    desc: 'Org-wide spend via Admin API. Requires key above.' },
  'sources.anthropic_api.poll_seconds': { label: 'Admin API poll (s)',      desc: 'How often to query the Admin API.' },
  'sources.cost_mode':                  { label: 'Cost mode',                desc: 'equivalent = JSONL-derived, actual_api = API only, both = sum.' },
  // ui
  'ui.pinned':         { label: 'Pinned',           desc: 'Popup does not auto-hide.' },
  'ui.start_on_login': { label: 'Start on Windows sign-in', desc: 'Auto-launch when you log in.' },
};

// ──── Range tab handlers ────

window.__suprbar_range = window.__suprbar_range || 'today';

// Client-side cache: key → last successful payload. Lets a tab click paint
// instantly while a fresh request runs in the background.
const _rangeCache = new Map();
const _rangeInflight = new Map();

function setRange(key) {
  if (!key) return;
  currentRange = key;
  window.__suprbar_range = key;
  document.querySelectorAll('.range-tabs .rt').forEach(b => {
    b.classList.toggle('active', b.dataset.range === key);
    if (b.dataset.range === key) b.setAttribute('aria-selected', 'true');
    else b.removeAttribute('aria-selected');
  });
  // 1) Paint cached immediately if we have it (snappy).
  const cached = _rangeCache.get(key);
  if (cached) {
    if (key === 'today') {
      // today is fetched via /api/today; cached shape differs, just render() if exists
      if (typeof render === 'function') render(cached);
    } else {
      renderRangeData(cached);
    }
  } else {
    // Show a tiny "loading…" hint by dimming the cost number briefly.
    document.getElementById('costNum')?.classList.add('loading');
  }
  // 2) Fetch fresh in the background; replaces the paint when it arrives.
  load({ refresh: true }).then(() => {
    document.getElementById('costNum')?.classList.remove('loading');
  });
}

document.querySelectorAll('.range-tabs .rt').forEach(btn => {
  btn.addEventListener('click', () => setRange(btn.dataset.range));
});

async function loadRange({ refresh = false } = {}) {
  const key = currentRange;
  // De-dupe concurrent fetches of the same key.
  if (_rangeInflight.has(key)) {
    try { return await _rangeInflight.get(key); }
    catch (_) { /* fall through to a fresh attempt */ }
  }
  const params = new URLSearchParams({ key });
  if (refresh) params.set('refresh', '1');
  const p = fetch('/api/range?' + params, { cache: 'no-store' })
    .then(res => { if (!res.ok) throw new Error('http ' + res.status); return res.json(); })
    .then(d => {
      _rangeCache.set(key, d);
      if (currentRange === key) renderRangeData(d);
      return d;
    })
    .catch(e => { console.warn('range fetch failed', e); throw e; })
    .finally(() => { _rangeInflight.delete(key); });
  _rangeInflight.set(key, p);
  return p;
}

// Prefetch common ranges in the background once the popup boots, so the very
// first click on any tab is instant.
function prefetchRanges() {
  const keys = ['24h', '7d', 'week', 'month', '30d', '90d'];
  const fire = (k) => fetch('/api/range?key=' + k, { cache: 'no-store' })
    .then(r => r.ok ? r.json() : null)
    .then(d => { if (d) _rangeCache.set(k, d); })
    .catch(() => {});
  // Stagger by ~80ms each so we don't spike the local server.
  keys.forEach((k, i) => setTimeout(() => fire(k), 80 * i));
}
// Kick off after the page has had a chance to render today first.
setTimeout(prefetchRanges, 250);

// Also cache the original /api/today payload for the "today" tab so swapping
// back from another range is instant too.
(function hookTodayCache() {
  if (window.__suprbar_today_hook) return;
  window.__suprbar_today_hook = true;
  const origRender = window.render || render;
  if (typeof origRender === 'function') {
    window.render = function(d) {
      _rangeCache.set('today', d);
      return origRender(d);
    };
  }
})();

function renderRangeData(d) {
  const t = d.totals || {};
  // cost number
  const newCost = Number(t.cost || 0);
  if (window.lastCost !== undefined && Math.abs(newCost - window.lastCost) > 0.001) {
    if (typeof animateCostTo === 'function') animateCostTo(newCost);
  }
  const wEl = document.getElementById('costWhole');
  const cEl = document.getElementById('costCents');
  const whole = Math.floor(newCost);
  const cents = (newCost - whole).toFixed(2).slice(1);
  if (wEl) wEl.textContent = whole.toLocaleString();
  if (cEl) cEl.textContent = cents;

  document.getElementById('costNum')?.classList.toggle('idle', newCost === 0);

  // label
  const lbl = document.getElementById('costLabel');
  if (lbl) lbl.textContent = (d.range?.label ? d.range.label[0].toUpperCase() + d.range.label.slice(1) : currentRange) + ' · Claude Code';

  // hide live indicator, hide active metric row, show summary metrics in its place
  const live = document.getElementById('liveIndicator');
  if (live) { live.hidden = false; live.classList.add('dim'); live.textContent = `${t.sessions} sess · ${t.projects} proj`; }

  const metricRow = document.getElementById('metricRow');
  if (metricRow) {
    metricRow.hidden = false;
    const set = (id, v) => { const e = document.getElementById(id); if (e) e.textContent = v; };
    set('mMessages', (t.messages || 0).toLocaleString());
    set('mModel', d.by_model?.[0]?.model ? d.by_model[0].model.replace(/^claude-/, '') : '—');
    set('mStarted', (d.range?.days || 1) + 'd');
    const burn = document.getElementById('mBurn');
    if (burn) burn.textContent = '—';
  }
  document.getElementById('emptyState') && (document.getElementById('emptyState').hidden = true);

  // token bar
  const total = (t.input||0) + (t.output||0) + (t.cache_5m||0) + (t.cache_1h||0) + (t.cache_read||0);
  const cacheT = (t.cache_5m||0) + (t.cache_1h||0) + (t.cache_read||0);
  const setW = (id, n) => { const e = document.getElementById(id); if (e) e.style.width = (total > 0 ? n/total*100 : 0).toFixed(2) + '%'; };
  setW('tbIn', t.input||0);
  setW('tbOut', t.output||0);
  setW('tbCache', cacheT);
  const setT = (id, n) => { const e = document.getElementById(id); if (e) e.textContent = (typeof fmtTokens === 'function') ? fmtTokens(n) : n; };
  setT('legIn', t.input||0); setT('legOut', t.output||0); setT('legCache', cacheT);

  // cache-hit indicator
  const chitEl = document.getElementById('cacheHit');
  if (chitEl) chitEl.textContent = total > 0 ? `${Math.round((t.cache_hit_ratio||0)*100)}%` : '—';

  // footer
  const footMeta = document.getElementById('footMeta');
  if (footMeta) footMeta.textContent = `${d.files_scanned||0} files · ${d.scan_ms||0}ms · ${d.range?.label||currentRange}`;

  // projects list
  const list = document.getElementById('projectsListItems');
  if (list && Array.isArray(d.by_project)) {
    list.innerHTML = d.by_project.slice(0, 10).map(p => `
      <li><span class="name">${escape(p.project)}</span>
          <span class="val">$${p.cost.toFixed(2)} · ${p.messages} msgs</span></li>
    `).join('');
  }
}

// ──── Budgets polling + UI ────

async function loadBudgets() {
  try {
    const r = await fetch('/api/budgets', { cache: 'no-store' });
    if (!r.ok) return;
    const b = await r.json();
    renderBudget(b);
    return b;
  } catch (e) { /* swallow */ }
}

function renderBudget(b) {
  // Show the bar for the most-relevant window: daily if set, else weekly, else monthly
  const order = ['daily','weekly','monthly'];
  let active = null;
  for (const k of order) {
    if (b[k]?.limit > 0) { active = { key: k, ...b[k] }; break; }
  }
  const strip = document.getElementById('budgetStrip');
  if (!strip) return;
  if (!active) { strip.hidden = true; return; }
  strip.hidden = false;
  const fill = document.getElementById('bsFill');
  const pct  = document.getElementById('bsPct');
  if (!fill || !pct) return;
  const pctVal = Math.min(100, Math.max(0, active.pct));
  fill.style.width = pctVal.toFixed(1) + '%';
  fill.classList.remove('warn', 'over');
  if (active.pct >= 100) fill.classList.add('over');
  else if (active.alerting) fill.classList.add('warn');
  pct.textContent = active.pct >= 1000 ? '>999%' : active.pct.toFixed(0) + '%';
  pct.title = `${active.key}: $${active.spent.toFixed(2)} / $${active.limit.toFixed(2)}`;
}

setInterval(loadBudgets, 30_000);
loadBudgets();

// ════════════════════════════════════════════════════════════════════════
//  Coach: hero observation card
// ════════════════════════════════════════════════════════════════════════

let _lastCoachId = null;
let _coachMore = [];

async function loadCoach() {
  try {
    const r = await fetch('/api/coach', { cache: 'no-store' });
    if (!r.ok) return;
    renderCoach(await r.json());
  } catch (e) { /* swallow */ }
}

function renderCoach(data) {
  const hero = data?.hero;
  const host = document.getElementById('coachHero');
  if (!host) return;
  if (!hero) {
    host.hidden = true;
    document.body.classList.remove('coach-mode');
    return;
  }
  // Toggle "coach-mode" on body so the cost number demotes to a chip.
  document.body.classList.add('coach-mode');
  host.hidden = false;
  host.classList.remove('sev-info','sev-nudge','sev-warn');
  host.classList.add('sev-' + (hero.severity || 'info'));

  const $set = (id, v) => { const e = document.getElementById(id); if (e) e.textContent = v; };
  $set('chId',    hero.id);
  $set('chConf',  Math.round(hero.confidence * 100) + '% confidence');
  $set('chTitle', hero.title);
  $set('chBody',  hero.body);

  const tip = document.getElementById('chTip');
  if (tip) {
    if (hero.tip) { tip.textContent = 'Tip: ' + hero.tip; tip.hidden = false; }
    else          { tip.hidden = true; tip.textContent = ''; }
  }

  // More-notes button (visible only if there are minor observations)
  _coachMore = Array.isArray(data.more) ? data.more : [];
  const moreWrap = document.getElementById('chMoreNotes');
  if (moreWrap) {
    moreWrap.hidden = _coachMore.length === 0;
  }

  // Toast on new observation id (only when it changes, never on first paint).
  if (_lastCoachId !== null && hero.id !== _lastCoachId
      && hero.severity !== 'info'
      && typeof toast === 'function') {
    toast(hero.title, hero.severity === 'warn' ? 'err' : 'warn', 3200);
  }
  _lastCoachId = hero.id;
}

// More-notes drawer (lightweight render — no permanent DOM)
document.getElementById('chMoreBtn')?.addEventListener('click', () => {
  let drawer = document.getElementById('coachMore');
  if (drawer) { drawer.remove(); return; }
  drawer = document.createElement('div');
  drawer.id = 'coachMore';
  drawer.className = 'more-notes';
  drawer.innerHTML = _coachMore.map(o => `
    <div class="mn-item">
      <span class="mn-id">${o.id}</span>
      <span class="mn-title">${o.title}</span>
    </div>`).join('');
  document.getElementById('coachHero').after(drawer);
});

// Poll the coach alongside the data poll. Coach refreshes are cheap because
// they reuse the server-side today cache.
setInterval(loadCoach, 5000);
loadCoach();

// ──── Apply display prefs to the DOM ────

function applyDisplayPrefs(prefs) {
  if (!prefs) return;
  const d = prefs.display || {};
  const b = prefs.behavior || {};
  const body = document.body;

  // theme
  body.dataset.theme = (d.theme === 'light') ? 'light' :
                       (d.theme === 'auto')  ? (matchMedia?.('(prefers-color-scheme: light)').matches ? 'light' : 'dark')
                                             : '';
  // accent
  body.dataset.accent = d.accent || 'violet';
  // density
  body.classList.toggle('compact',  d.density === 'compact');
  body.classList.toggle('spacious', d.density === 'spacious');
  // font scale
  body.style.setProperty('--font-scale', String(d.font_scale || 1));
  // animations
  body.classList.toggle('no-animations', d.animations === false);
  // click-through
  body.classList.toggle('click-through', b.click_through === true);

  // visibility of each metric tile / chip
  const setHidden = (sel, hide) => document.querySelectorAll(sel).forEach(e => e.hidden = !!hide);
  setHidden('.token-bar, .token-legend', d.show_token_bar === false);
  setHidden('#cacheHit', d.show_cache_info === false);
  setHidden('#mBurnCell, #mBurn', d.show_burn_rate === false);

  // refresh interval
  const refresh = Math.max(0, Number(b.refresh_seconds ?? 5));
  if (typeof setPollInterval === 'function') {
    if (refresh === 0) setPollInterval(0);  // manual
    else setPollInterval(refresh * 1000);
  }
}

// ──── Settings: schema + prefs load ────

async function loadPrefs(force = false) {
  if (prefsCache && !force) return prefsCache;
  const [a, b] = await Promise.all([
    fetch('/api/prefs').then(r => r.json()),
    fetch('/api/prefs/schema').then(r => r.json()),
  ]);
  prefsCache  = a.prefs;
  schemaCache = b.settings;
  applyDisplayPrefs(prefsCache);
  return prefsCache;
}

// ──── Settings: render dynamic sections ────

function getNested(obj, path) {
  let cur = obj;
  for (const p of path.split('.')) {
    if (cur == null || typeof cur !== 'object') return undefined;
    cur = cur[p];
  }
  return cur;
}

function buildControl(setting, value) {
  const ctl = document.createElement('div');
  ctl.className = 'ctl-wrap';
  const path = setting.path;

  const commit = (val) => {
    fetch('/api/prefs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ updates: { [path]: val } }),
    }).then(r => r.json()).then(d => {
      if (d.error) {
        if (typeof toast === 'function') toast(d.error.message || 'invalid', 'err');
        return;
      }
      prefsCache = d.prefs;
      applyDisplayPrefs(prefsCache);
      // refresh data when range or filter prefs change
      if (path.startsWith('range.') || path.startsWith('projects.')) {
        load({ refresh: true });
        loadBudgets();
      }
      if (path.startsWith('budgets.')) loadBudgets();
      if (typeof toast === 'function') toast('saved', 'ok', 900);
    });
  };

  if (setting.type === 'bool') {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'toggle' + (value ? ' on' : '');
    btn.setAttribute('aria-pressed', value ? 'true' : 'false');
    btn.setAttribute('aria-label', LABELS[path]?.label || path);
    btn.addEventListener('click', () => {
      const next = !btn.classList.contains('on');
      btn.classList.toggle('on', next);
      btn.setAttribute('aria-pressed', next);
      commit(next);
    });
    ctl.appendChild(btn);
  } else if (setting.type === 'enum') {
    // For accent specifically, use color swatches; otherwise dropdown
    if (path === 'display.accent') {
      const wrap = document.createElement('div');
      wrap.className = 'accent-swatches';
      setting.options.forEach(opt => {
        const sw = document.createElement('button');
        sw.type = 'button';
        sw.className = 'sw' + (opt === value ? ' on' : '');
        sw.dataset.v = opt;
        sw.title = opt;
        sw.setAttribute('aria-label', 'Accent ' + opt);
        sw.addEventListener('click', () => {
          wrap.querySelectorAll('.sw').forEach(x => x.classList.remove('on'));
          sw.classList.add('on');
          commit(opt);
        });
        wrap.appendChild(sw);
      });
      ctl.appendChild(wrap);
    } else {
      const sel = document.createElement('select');
      sel.className = 'input-fld';
      setting.options.forEach(opt => {
        const o = document.createElement('option');
        o.value = String(opt); o.textContent = String(opt);
        if (String(opt) === String(value)) o.selected = true;
        sel.appendChild(o);
      });
      sel.addEventListener('change', () => commit(sel.value));
      ctl.appendChild(sel);
    }
  } else if (setting.type === 'int' || setting.type === 'float') {
    const lo = setting.min, hi = setting.max;
    const useRange = setting.type === 'float'
      || (lo != null && hi != null && (hi - lo) <= 100);
    const input = document.createElement('input');
    input.className = 'input-fld';
    if (useRange && lo != null && hi != null) {
      input.type = 'range';
      input.min = lo; input.max = hi;
      input.step = setting.type === 'float' ? 0.05 : 1;
    } else {
      input.type = 'number';
      if (lo != null) input.min = lo;
      if (hi != null) input.max = hi;
    }
    input.value = (value == null) ? '' : String(value);
    const label = document.createElement('span');
    label.className = 'ctl-range-val';
    label.textContent = setting.type === 'float'
      ? Number(value).toFixed(2)
      : String(value ?? '');
    input.addEventListener('input', () => {
      label.textContent = setting.type === 'float'
        ? Number(input.value).toFixed(2)
        : input.value;
    });
    input.addEventListener('change', () => {
      const v = setting.type === 'float' ? parseFloat(input.value) : parseInt(input.value, 10);
      commit(v);
    });
    ctl.appendChild(input);
    if (useRange) ctl.appendChild(label);
  } else if (setting.type === 'str') {
    const input = document.createElement('input');
    input.type = 'text'; input.className = 'input-fld';
    input.value = value || '';
    input.placeholder = '';
    input.addEventListener('change', () => commit(input.value));
    ctl.appendChild(input);
  } else if (setting.type === 'date_or_null') {
    const input = document.createElement('input');
    input.type = 'date'; input.className = 'input-fld';
    if (value) input.value = value;
    input.addEventListener('change', () => commit(input.value || null));
    ctl.appendChild(input);
  } else if (setting.type === 'list_str') {
    const arr = Array.isArray(value) ? [...value] : [];
    const wrap = document.createElement('div');
    wrap.className = 'taginput';
    const render = () => {
      wrap.innerHTML = '';
      arr.forEach((tag, i) => {
        const t = document.createElement('span');
        t.className = 'tag';
        t.innerHTML = `${escape(tag)}<span class="x" data-i="${i}" role="button" aria-label="Remove">×</span>`;
        wrap.appendChild(t);
      });
      const inp = document.createElement('input');
      inp.placeholder = 'add… (Enter)';
      inp.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && inp.value.trim()) {
          arr.push(inp.value.trim());
          commit([...arr]); render();
        } else if (e.key === 'Backspace' && !inp.value && arr.length) {
          arr.pop(); commit([...arr]); render();
        }
      });
      wrap.appendChild(inp);
      wrap.querySelectorAll('.x').forEach(x => {
        x.addEventListener('click', () => {
          arr.splice(parseInt(x.dataset.i, 10), 1);
          commit([...arr]); render();
        });
      });
    };
    render();
    ctl.appendChild(wrap);
  } else {
    const span = document.createElement('span');
    span.textContent = JSON.stringify(value);
    span.style.cssText = 'font-family:var(--mono);font-size:11px;color:rgba(255,255,255,0.6)';
    ctl.appendChild(span);
  }
  return ctl;
}

function renderSettings() {
  const host = document.getElementById('settingsSections');
  if (!host) return;
  host.innerHTML = '';

  // group schema entries by section
  const bySection = {};
  for (const s of schemaCache) {
    const section = s.path.split('.')[0];
    (bySection[section] ||= []).push(s);
  }

  for (const section of SECTION_ORDER) {
    const items = bySection[section];
    if (!items || items.length === 0) continue;
    const sec = document.createElement('div');
    sec.className = 'settings-section';
    sec.dataset.section = section;

    const title = document.createElement('div');
    title.className = 'settings-section-title';
    title.textContent = SECTION_TITLES[section] || section;
    sec.appendChild(title);

    for (const setting of items) {
      const row = document.createElement('div');
      row.className = 'settings-row dense';
      row.dataset.path = setting.path;
      const main = document.createElement('div');
      main.className = 'settings-row-main';
      const meta = LABELS[setting.path] || { label: setting.path, desc: '' };
      main.innerHTML = `<div class="lbl">${escape(meta.label)}</div><div class="desc">${meta.desc || ''}</div>`;
      row.appendChild(main);

      const cur = getNested(prefsCache, setting.path);
      row.appendChild(buildControl(setting, cur ?? setting.default));
      sec.appendChild(row);
    }
    host.appendChild(sec);
  }
}

// search filter for settings
document.getElementById('settingsSearch')?.addEventListener('input', (e) => {
  const q = e.target.value.trim().toLowerCase();
  document.querySelectorAll('.settings-section[data-section]').forEach(sec => {
    const rows = sec.querySelectorAll('.settings-row[data-path]');
    let any = false;
    rows.forEach(row => {
      const path = row.dataset.path.toLowerCase();
      const lbl = row.querySelector('.lbl')?.textContent.toLowerCase() || '';
      const desc = row.querySelector('.desc')?.textContent.toLowerCase() || '';
      const match = !q || path.includes(q) || lbl.includes(q) || desc.includes(q);
      row.classList.toggle('filtered', !match);
      if (match) any = true;
    });
    sec.classList.toggle('empty-section', !any);
  });
});

// rebind settings open to pull schema + prefs
const _origOpenSettings = window.openSettings;
window.openSettings = async function patchedOpenSettings() {
  if (typeof _origOpenSettings === 'function') _origOpenSettings();
  await loadPrefs(true);
  if (schemaCache && schemaCache.length) renderSettings();
};

// Reset / Export / Import buttons
document.getElementById('resetConfigBtn')?.addEventListener('click', async () => {
  if (!confirm('Reset all settings to defaults? (your API key is preserved)')) return;
  const r = await fetch('/api/config/reset', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reset_key: false }),
  });
  if (r.ok) {
    if (typeof toast === 'function') toast('settings reset', 'ok');
    await loadPrefs(true);
    renderSettings();
    load({ refresh: true });
    loadBudgets();
  }
});

document.getElementById('exportConfigBtn')?.addEventListener('click', async () => {
  const r = await fetch('/api/config/export');
  if (!r.ok) return;
  const blob = new Blob([await r.text()], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'suprbar-config.json';
  a.click();
  URL.revokeObjectURL(a.href);
  if (typeof toast === 'function') toast('config exported', 'ok');
});

document.getElementById('importConfigBtn')?.addEventListener('click', () => {
  document.getElementById('importFileInput')?.click();
});
document.getElementById('importFileInput')?.addEventListener('change', async (e) => {
  const f = e.target.files?.[0];
  if (!f) return;
  try {
    const text = await f.text();
    const payload = JSON.parse(text);
    const r = await fetch('/api/config/import', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (r.ok) {
      if (typeof toast === 'function') toast('imported', 'ok');
      await loadPrefs(true); renderSettings();
      load({ refresh: true });
    } else {
      const j = await r.json().catch(() => ({}));
      if (typeof toast === 'function') toast(j.error?.message || 'import failed', 'err');
    }
  } catch (err) {
    if (typeof toast === 'function') toast('invalid file: ' + err.message, 'err');
  }
  e.target.value = '';
});

// Boot prefs apply ASAP (before settings panel opens) so theme/density takes effect
loadPrefs().then(() => {
  if (document.getElementById('settingsOverlay') && !document.getElementById('settingsOverlay').hidden) {
    renderSettings();
  }
});

window.suprbar.setRange = setRange;
window.suprbar.loadPrefs = loadPrefs;
window.suprbar.loadBudgets = loadBudgets;

} // end idempotency guard
