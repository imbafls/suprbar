/* supr.bar — mini overlay renderer (v2).

   Compact chip: live pip · cost · range tag · burn · ×
   Hover expands the native window into a detail card (range chips, live
   session, messages, budget bar).

   Range defaults to 24h (rolling) and can be switched to Today; the choice
   persists in localStorage. /api/range is server-cached for 30s, so polling
   it only fetches when stale. Fast cadence while a session is live, slow
   while idle. */

'use strict';

const $ = (id) => document.getElementById(id);
let prefs = null;
let range = localStorage.getItem('suprbar.mini.range') || '24h';
let todayData = null;
let rangeData = null;
let budgetData = null;
let lastRangeFetch = 0;
let lastBudgetFetch = 0;
let timer = null;
let tickCount = 0;
let leaveTimer = null;

const RANGE_STALE_MS = 25_000;
const BUDGET_STALE_MS = 30_000;

function fmtMoney(n) {
  n = Number(n || 0);
  if (n >= 1000) return '$' + Math.round(n).toLocaleString();
  if (n >= 10) return '$' + n.toFixed(1);
  return '$' + n.toFixed(2);
}

function shortProject(p) {
  if (!p) return '';
  const parts = String(p).split(/[\\/]/).filter(Boolean);
  return parts[parts.length - 1] || p;
}

function applyPrefs(p) {
  prefs = p || {};
  const d = prefs.display || {};
  const theme = d.theme === 'light'
    ? 'light'
    : d.theme === 'auto'
      ? (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : '')
      : '';
  document.body.dataset.theme = theme;
  document.body.dataset.accent = d.accent || 'blue';
  document.body.classList.toggle('no-animations', d.animations === false);
}

async function loadPrefs() {
  try {
    const r = await fetch('/api/prefs', { cache: 'no-store' });
    if (!r.ok) return;
    applyPrefs((await r.json()).prefs);
  } catch (_) { /* keep last known prefs */ }
}

function liveInfo() {
  const d = todayData || {};
  const active = d.active || null;
  const lives = Array.isArray(d.live_sessions) ? d.live_sessions.length : 0;
  return { live: !!active || lives > 0, active, lives };
}

function currentCost() {
  if (range === '24h') return rangeData?.totals?.cost ?? 0;
  return todayData?.today?.cost ?? 0;
}

function currentMsgs() {
  if (range === '24h') return rangeData?.totals?.messages ?? 0;
  return todayData?.today?.messages ?? 0;
}

function render() {
  const { live, active, lives } = liveInfo();
  document.body.classList.toggle('live', live);
  document.body.dataset.range = range;

  const cost = currentCost();
  $('cCost').textContent = fmtMoney(cost);
  $('fCost').textContent = fmtMoney(cost);

  document.querySelectorAll('.range').forEach((el) => {
    if (el.dataset.range) {
      el.classList.toggle('on', el.dataset.range === range);
    } else {
      el.textContent = range === '24h' ? '24h' : 'Today';
    }
  });

  const burn = active?.burn_rate_usd_per_hour;
  const showBurn = !prefs || !prefs.mini || prefs.mini.show_burn !== false;
  const burnTxt = (live && burn != null && burn > 0 && showBurn)
    ? fmtMoney(burn) + '/h' : '';
  $('cBurn').textContent = burnTxt;
  $('cBurn').hidden = !burnTxt;
  $('fBurn').textContent = burnTxt || (live ? 'live' : 'idle');

  const msgs = currentMsgs();
  $('fMsgs').textContent = msgs ? msgs.toLocaleString() + ' msgs' : '';
  $('fSep').hidden = !(burnTxt && msgs);

  const proj = active?.project ? shortProject(active.project) : '';
  $('fLive').textContent = live
    ? (proj || `${lives} live`)
    : 'no live session';

  renderBudget();
}

function renderBudget() {
  const wrap = $('fBudget');
  const b = budgetData;
  if (!b) { wrap.hidden = true; return; }
  const key = ['daily', 'weekly', 'monthly'].find((k) => b[k]?.limit > 0);
  if (!key) { wrap.hidden = true; return; }
  const e = b[key];
  wrap.hidden = false;
  const bar = $('fBudgetBar');
  bar.style.width = Math.min(100, Math.max(0, e.pct)) + '%';
  bar.classList.toggle('warn', !e.over && !!e.alerting);
  bar.classList.toggle('over', !!e.over);
  $('fBudgetTxt').textContent = `${fmtMoney(e.spent)} / ${fmtMoney(e.limit)} ${key}`;
}

async function fetchToday() {
  try {
    const r = await fetch('/api/today', { cache: 'no-store' });
    if (!r.ok) throw new Error('http ' + r.status);
    todayData = await r.json();
    document.body.classList.remove('offline', 'loading');
  } catch (_) {
    document.body.classList.add('offline');
  }
}

async function fetchRange(force = false) {
  if (range !== '24h') return;
  if (!force && Date.now() - lastRangeFetch < RANGE_STALE_MS) return;
  try {
    const r = await fetch('/api/range?key=24h', { cache: 'no-store' });
    if (!r.ok) return;
    rangeData = await r.json();
    lastRangeFetch = Date.now();
  } catch (_) { /* keep last known */ }
}

async function fetchBudgets() {
  if (Date.now() - lastBudgetFetch < BUDGET_STALE_MS) return;
  try {
    const r = await fetch('/api/budgets', { cache: 'no-store' });
    if (!r.ok) return;
    budgetData = await r.json();
    lastBudgetFetch = Date.now();
  } catch (_) { /* keep last known */ }
}

async function tick() {
  await fetchToday();
  await fetchRange(false);
  if (document.body.classList.contains('expanded')) await fetchBudgets();
  render();
  tickCount += 1;
  if (tickCount % 6 === 0) loadPrefs();
  schedule();
}

function schedule() {
  if (timer) clearTimeout(timer);
  const live = document.body.classList.contains('live');
  timer = setTimeout(tick, live ? 5000 : 30000);
}

function setRange(next) {
  range = next === 'today' ? 'today' : '24h';
  try { localStorage.setItem('suprbar.mini.range', range); } catch (_) { /* ignore */ }
  render();
  fetchRange(true).then(render);
}

function expand(on) {
  try { window.pywebview?.api?.expand(!!on); } catch (_) { /* ignore */ }
}

document.body.addEventListener('mouseenter', () => {
  clearTimeout(leaveTimer);
  document.body.classList.add('expanded');
  expand(true);
  fetchBudgets().then(render);
});
document.body.addEventListener('mouseleave', () => {
  clearTimeout(leaveTimer);
  leaveTimer = setTimeout(() => {
    document.body.classList.remove('expanded');
    expand(false);
  }, 220);
});

function openFlyout() {
  try { window.pywebview?.api?.open_flyout(); } catch (_) { /* ignore */ }
}
function hideMini() {
  try { window.pywebview?.api?.hide(); } catch (_) { /* ignore */ }
}

$('cZone').addEventListener('click', openFlyout);
$('fZone').addEventListener('click', openFlyout);
$('closeBtn').addEventListener('click', hideMini);
$('closeBtn2').addEventListener('click', hideMini);
$('cRange').addEventListener('click', (e) => {
  e.stopPropagation();
  setRange(range === '24h' ? 'today' : '24h');
});
$('fRange24').addEventListener('click', () => setRange('24h'));
$('fRangeToday').addEventListener('click', () => setRange('today'));

document.addEventListener('contextmenu', (e) => e.preventDefault());
window.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') hideMini();
});

loadPrefs();
fetchBudgets().then(render);
tick();
