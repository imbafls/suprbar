/* supr.bar — mini overlay renderer.
   Polls /api/today (server-side cached) and paints the chip. Fast cadence
   while a session is live, slow while idle. Refreshes prefs periodically so
   theme / accent / show-burn changes land without a restart. */

'use strict';

const $ = (id) => document.getElementById(id);
let prefs = null;
let timer = null;
let tickCount = 0;

function fmt(n) {
  return '$' + Number(n || 0).toLocaleString('en-US', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
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

function render(d) {
  const active = d.active || null;
  const lives = Array.isArray(d.live_sessions) ? d.live_sessions.length : 0;
  const live = !!active || lives > 0;
  document.body.classList.toggle('live', live);

  $('mCost').textContent = fmt((d.today && d.today.cost) || 0);

  const burnEl = $('mBurn');
  const burn = active && active.burn_rate_usd_per_hour;
  const showBurn = !prefs || !prefs.mini || prefs.mini.show_burn !== false;
  if (live && burn != null && burn > 0 && showBurn) {
    burnEl.textContent = fmt(burn) + '/h';
    burnEl.hidden = false;
  } else {
    burnEl.hidden = true;
  }
}

async function tick() {
  try {
    const r = await fetch('/api/today', { cache: 'no-store' });
    if (!r.ok) throw new Error('http ' + r.status);
    render(await r.json());
    document.body.classList.remove('offline', 'loading');
  } catch (_) {
    document.body.classList.add('offline');
  }
  tickCount += 1;
  if (tickCount % 6 === 0) loadPrefs();
  schedule();
}

function schedule() {
  if (timer) clearTimeout(timer);
  const live = document.body.classList.contains('live');
  timer = setTimeout(tick, live ? 5000 : 30000);
}

$('openZone').addEventListener('click', () => {
  try { window.pywebview?.api?.open_flyout(); } catch (_) { /* ignore */ }
});
$('closeBtn').addEventListener('click', (e) => {
  e.stopPropagation();
  try { window.pywebview?.api?.hide(); } catch (_) { /* ignore */ }
});
document.addEventListener('contextmenu', (e) => e.preventDefault());
window.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    try { window.pywebview?.api?.hide(); } catch (_) { /* ignore */ }
  }
});

loadPrefs();
tick();
