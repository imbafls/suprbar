// supr.bar flyout — v2.
//
// One job: show what Claude Code (and the other enabled sources) cost, for
// today or the last 7 / 30 / 90 days. Polls only while the window is visible:
// popup.py pushes window.__suprbarVisible(bool) on show/hide, because a
// hidden WinForms host never flips document.hidden.
'use strict';

const $ = (id) => document.getElementById(id);
const POLL_LIVE_MS = 5000;
const POLL_IDLE_MS = 30000;
const BACKOFF_MAX_MS = 30000;
// [id, label, has an API key]
const SOURCES = [
  ['local', 'Claude Code', false],
  ['opencode', 'opencode', false],
  ['hermes', 'Hermes', false],
  ['anthropic_api', 'Anthropic API', true],
  ['openrouter', 'OpenRouter', true],
  ['openai', 'OpenAI', true],
];

let range = 'today';
let visible = true;
let timer = 0;
let backoff = 2000;
let loading = false;
let settings = null;
// Monthly subscription prices; mirrors config.PLAN_PRICES.
const PLAN_USD = { pro: 20, max5: 100, max20: 200 };
const PAID_API = new Set(['anthropic_api', 'openrouter', 'openai']);

// ───────────── formatting ─────────────

function money(v) {
  const n = Number(v) || 0;
  if (n >= 1000) return '$' + Math.round(n).toLocaleString('en-US');
  return '$' + n.toFixed(2);
}

function compact(v) {
  const n = Number(v) || 0;
  if (n >= 1e9) return (n / 1e9).toFixed(1) + 'B';
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k';
  return String(Math.round(n));
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// Claude Code folders encode the path ("C--depot--projects-app"): show the
// last path segment.
function projectName(p) {
  const s = String(p || '—');
  const i = s.lastIndexOf('--');
  return i >= 0 ? s.slice(i + 2) || s : s;
}

function modelName(m) {
  return String(m || '—').replace(/^claude-/, '');
}

function tokensOf(t) {
  if (!t) return 0;
  if (t.tokens != null) return t.tokens;
  return (t.input || 0) + (t.output || 0) + (t.cache_5m || 0)
    + (t.cache_1h || 0) + (t.cache_read || 0);
}

// ───────────── data ─────────────

async function getJSON(url, timeoutMs = 15000) {
  const r = await fetch(url, { cache: 'no-store', signal: AbortSignal.timeout(timeoutMs) });
  if (!r.ok) throw new Error(url + ' → ' + r.status);
  return r.json();
}

async function postJSON(url, body) {
  const r = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
    signal: AbortSignal.timeout(20000),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data?.error?.message || ('http ' + r.status));
  return data;
}

function schedule(ms) {
  clearTimeout(timer);
  timer = visible ? setTimeout(() => load(false), ms) : 0;
}

async function load(refresh) {
  if (!visible || loading) return;
  loading = true;
  const q = refresh ? '?refresh=1' : '';
  try {
    const todayP = getJSON('/api/today' + q);
    const rangeP = range === 'today' ? null
      : getJSON(`/api/range?key=${range}${refresh ? '&refresh=1' : ''}`, 60000);
    const [today, rng] = await Promise.all([todayP, rangeP]);
    render(today, rng);
    backoff = 2000;
    schedule(today.active ? POLL_LIVE_MS : POLL_IDLE_MS);
  } catch (e) {
    setStatus('offline');
    $('updated').textContent = 'can’t reach supr.bar — retrying';
    schedule(backoff);
    backoff = Math.min(backoff * 2, BACKOFF_MAX_MS);
  } finally {
    loading = false;
  }
}

// ───────────── render ─────────────

function setStatus(state) {
  $('status').dataset.state = state;
  $('statusText').textContent = state;
}

function render(today, rng) {
  setStatus(today.active ? 'live' : 'idle');
  const t = rng ? rng.totals : today.today;
  $('cost').textContent = money(t.cost);
  $('sub').textContent = `${Number(t.messages || 0).toLocaleString('en-US')} messages · `
    + `${compact(tokensOf(t))} tokens` + (rng ? ` · ${rng.range.label}` : '');

  const burn = $('burn');
  const a = today.active;
  burn.hidden = !(range === 'today' && a);
  if (a) burn.textContent = `${money(a.burn_rate_usd_per_hour)}/h · ${projectName(a.project)}`;

  renderSavings(today, rng);
  renderSources(today.sources || [], rng);
  renderBars(rng ? rng.by_day : today.hourly, !rng);
  renderList('projects', (rng || today).by_project, (p) => projectName(p.project));
  renderList('models', (rng || today).by_model, (m) => modelName(m.model));

  $('updated').textContent = 'updated ' + new Date().toLocaleTimeString([], {
    hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

// Spent = the plan's share of this range + metered API spend.
// Saved = what Claude Code would have cost at API rates, minus that share.
function renderSavings(today, rng) {
  const el = $('save');
  const price = PLAN_USD[settings?.settings?.['plan.tier']] || 0;
  el.hidden = !price;
  if (!price) return;
  const srcs = today.sources || [];
  const days = rng ? Number(rng.range.days) || 1 : 1;
  const planShare = price * 12 / 365 * days;
  const local = srcs.find((x) => x.id === 'local');
  const covered = rng ? Number(rng.totals.cost) || 0 : Number(local?.cost_today) || 0;
  const metered = rng ? 0 : srcs.filter((x) => x.ok && PAID_API.has(x.id))
    .reduce((n, x) => n + (Number(x.cost_today) || 0), 0);
  const saved = covered - planShare;
  el.dataset.state = saved >= 0 ? 'up' : 'down';
  el.innerHTML = `spent <b>${money(planShare + metered)}</b> · `
    + (saved >= 0 ? `saved <b>${money(saved)}</b>` : `plan ahead <b>${money(-saved)}</b>`);
  el.title = `Plan share ${money(planShare)} for ${days} day${days === 1 ? '' : 's'}`
    + (metered ? ` + ${money(metered)} metered API` : '')
    + ` · Claude Code at API rates ${money(covered)}`;
}

function renderSources(sources, rng) {
  // A failed source shows "!" with its error — never a silent $0.00.
  $('sources').innerHTML = sources.map((s) => {
    const label = esc(String(s.label || s.id).split('·')[0].trim());
    if (!s.ok) {
      return `<span class="chip bad" title="${esc(s.error || 'failed')}">${label} !</span>`;
    }
    let val;
    if (!rng) val = money(s.cost_today);
    else if (s.id === 'local') val = money(rng.totals.cost);
    else val = 'today only';
    return `<span class="chip"><i data-src="${esc(s.id)}"></i>${label} <b class="num">${val}</b></span>`;
  }).join('');
}

function renderBars(rows, hourly) {
  const list = Array.isArray(rows) ? rows : [];
  const max = Math.max(0.0001, ...list.map((r) => Number(r.cost) || 0));
  const nowHour = new Date().getHours();
  $('bars').innerHTML = list.map((r, i) => {
    const pct = Math.max(2, ((Number(r.cost) || 0) / max) * 100);
    const label = hourly ? `${r.hour}:00` : r.date;
    const on = hourly ? r.hour === nowHour : i === list.length - 1;
    return `<span class="${on ? 'on' : ''}" style="height:${pct.toFixed(1)}%"`
      + ` title="${esc(label)} · ${money(r.cost)}"></span>`;
  }).join('');
}

function renderList(id, rows, name) {
  const list = (Array.isArray(rows) ? rows : []).filter((r) => r.cost > 0).slice(0, 5);
  const total = list.reduce((s, r) => s + r.cost, 0) || 1;
  $(id).innerHTML = list.length ? list.map((r) => `
    <li><span class="nm" title="${esc(name(r))}">${esc(name(r))}</span>
      <b class="num">${money(r.cost)}</b>
      <i style="width:${((r.cost / total) * 100).toFixed(1)}%"></i></li>`).join('')
    : '<li class="empty">nothing yet</li>';
}

// ───────────── header, tabs, footer ─────────────

function setRange(key) {
  range = key;
  document.querySelectorAll('#tabs button').forEach((b) => {
    b.setAttribute('aria-selected', String(b.dataset.range === key));
  });
  $('cost').textContent = '…';
  load(false);
}

$('tabs').addEventListener('click', (e) => {
  const key = e.target?.dataset?.range;
  if (key && key !== range) setRange(key);
});

$('refreshBtn').addEventListener('click', () => {
  $('refreshBtn').classList.add('spin');
  setTimeout(() => $('refreshBtn').classList.remove('spin'), 600);
  load(true);
});

$('pinBtn').addEventListener('click', async () => {
  const next = $('pinBtn').getAttribute('aria-pressed') !== 'true';
  $('pinBtn').setAttribute('aria-pressed', String(next));
  try {
    await saveSettings({ settings: { 'ui.pinned': next } });
  } catch (_) {
    $('pinBtn').setAttribute('aria-pressed', String(!next));
  }
});

$('reportBtn').addEventListener('click', () => {
  postJSON('/api/open-report').catch(() => {});
});

async function checkUpdate() {
  try {
    const st = await getJSON('/api/update/status');
    const btn = $('updateBtn');
    btn.hidden = !st?.available;
    if (st?.available) btn.textContent = `Update to v${st.latest}`;
  } catch (_) { /* no update info is fine */ }
}

$('updateBtn').addEventListener('click', async () => {
  $('updateBtn').textContent = 'Updating…';
  try { await postJSON('/api/update/apply'); } catch (_) {
    $('updateBtn').textContent = 'Update failed';
  }
});

// ───────────── settings sheet ─────────────

async function saveSettings(body) {
  settings = await postJSON('/api/settings', body);
  applySettings();
  return settings;
}

function applySettings() {
  if (!settings) return;
  const s = settings.settings;
  $('pinBtn').setAttribute('aria-pressed', String(!!s['ui.pinned']));
  document.querySelectorAll('#sheet input[data-path]').forEach((el) => {
    el.checked = !!s[el.dataset.path];
  });
  document.querySelectorAll('#sheet select[data-path]').forEach((el) => {
    el.value = s[el.dataset.path] || '';
  });
  $('version').textContent = 'supr.bar v' + settings.version;
}

function renderSourceRows() {
  const s = settings.settings;
  $('srcRows').innerHTML = SOURCES.map(([id, label, hasKey]) => `
    <label class="row"><span>${label}</span>
      <input type="checkbox" data-path="sources.${id}.enabled"
        ${s[`sources.${id}.enabled`] ? 'checked' : ''} /></label>
    ${hasKey ? `<div class="key" data-src="${id}">
      <input type="password" placeholder="${settings.keys[id] ? esc(settings.keys[id]) : 'API key'}"
        autocomplete="off" spellcheck="false" />
      <button class="tbtn" data-act="save">Save</button>
      <button class="tbtn" data-act="test">Test</button>
      ${settings.keys[id] ? '<button class="tbtn" data-act="clear">Clear</button>' : ''}
      <small class="msg"></small></div>` : ''}`).join('');
}

async function openSheet() {
  $('sheet').hidden = false;
  try {
    settings = await getJSON('/api/settings');
    renderSourceRows();
    applySettings();
  } catch (_) {
    $('srcRows').innerHTML = '<p class="empty">Can’t load settings.</p>';
  }
}

function closeSheet() {
  $('sheet').hidden = true;
  load(true);  // sources may have changed what is counted
}

$('settingsBtn').addEventListener('click', openSheet);
$('sheetClose').addEventListener('click', closeSheet);

$('sheet').addEventListener('change', async (e) => {
  const el = e.target;
  if (!el?.dataset?.path) return;
  try {
    const value = el.tagName === 'SELECT' ? el.value : el.checked;
    await saveSettings({ settings: { [el.dataset.path]: value } });
  } catch (_) {
    applySettings();  // didn't stick: show the real state
  }
});

$('sheet').addEventListener('click', async (e) => {
  const act = e.target?.dataset?.act;
  const box = e.target?.closest?.('.key');
  if (!act || !box) return;
  e.preventDefault();
  const source = box.dataset.src;
  const input = box.querySelector('input');
  const msg = box.querySelector('.msg');
  msg.className = 'msg';
  try {
    if (act === 'test') {
      const r = await postJSON('/api/settings/test-key', { source, key: input.value });
      msg.textContent = r.ok ? 'key works' : (r.message || 'invalid key');
      msg.classList.add(r.ok ? 'good' : 'bad');
      return;
    }
    const key = act === 'clear' ? '' : input.value.trim();
    if (act === 'save' && !key) return;
    await saveSettings({ keys: { [source]: key } });
    renderSourceRows();
  } catch (err) {
    msg.textContent = String(err.message || err);
    msg.classList.add('bad');
  }
});

// ───────────── window integration ─────────────

window.__suprbarVisible = (on) => {
  visible = !!on;
  if (visible) {
    load(true);
    checkUpdate();
  } else {
    clearTimeout(timer);
  }
};
window.__suprbarOpenSettings = openSheet;

function api() {
  return window.pywebview?.api;
}

window.addEventListener('blur', () => {
  // popup.py ignores this while pinned or right after showing.
  api()?.hide?.();
});

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    if (!$('sheet').hidden) closeSheet();
    else api()?.hide?.();
  } else if (e.key === 'q' && e.altKey) {
    api()?.quit?.();
  }
});

window.addEventListener('pywebviewready', async () => {
  try {
    if (await api()?.consume_pending_open?.() === 'settings') openSheet();
  } catch (_) { /* not in the app window */ }
});

checkUpdate();
// Settings first: the savings line needs the plan on the first render.
getJSON('/api/settings').then((s) => { settings = s; applySettings(); })
  .catch(() => {}).finally(() => load(false));
