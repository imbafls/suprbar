// supr.bar mini overlay — v2.
//
// Chip: live dot, last-24h cost, $/h while a session is live. Hovering
// grows the window into a card (24h + today, messages, live project).
// Click opens the flyout; × turns the overlay off. This process only exists
// while the overlay is enabled, so it can poll unconditionally.
'use strict';

const $ = (id) => document.getElementById(id);
const POLL_LIVE_MS = 5000;
const POLL_IDLE_MS = 30000;
const POLL_OFFLINE_MS = 10000;
let timer = 0;
let leaveTimer = 0;

function money(v) {
  const n = Number(v) || 0;
  if (n >= 1000) return '$' + Math.round(n).toLocaleString('en-US');
  if (n >= 10) return '$' + n.toFixed(1);
  return '$' + n.toFixed(2);
}

function projectName(p) {
  const s = String(p || '');
  const i = s.lastIndexOf('--');
  return i >= 0 ? s.slice(i + 2) || s : s;
}

function render(d) {
  const a = d.active;
  document.body.classList.toggle('live', !!a);
  document.body.classList.remove('offline');
  const day = d.rolling_24h || { cost: 0, messages: 0 };
  $('cCost').textContent = money(day.cost);
  $('fCost').textContent = money(day.cost);
  const burn = a && a.burn_rate_usd_per_hour > 0 ? money(a.burn_rate_usd_per_hour) + '/h' : '';
  $('cBurn').textContent = burn;
  $('fLive').textContent = a ? `${projectName(a.project)}${burn ? ' · ' + burn : ''}` : 'no live session';
  $('fMeta').textContent = `last 24h · ${Number(day.messages || 0).toLocaleString('en-US')} msgs`;
  $('fToday').textContent = `today ${money(d.today?.cost)}`;
}

async function tick() {
  let next = POLL_OFFLINE_MS;
  try {
    const r = await fetch('/api/today', { cache: 'no-store', signal: AbortSignal.timeout(20000) });
    if (!r.ok) throw new Error('http ' + r.status);
    const d = await r.json();
    render(d);
    next = d.active ? POLL_LIVE_MS : POLL_IDLE_MS;
  } catch (_) {
    document.body.classList.add('offline');
  }
  clearTimeout(timer);
  timer = setTimeout(tick, next);
}

function api() {
  return window.pywebview?.api;
}

document.body.addEventListener('mouseenter', () => {
  clearTimeout(leaveTimer);
  document.body.classList.add('expanded');
  api()?.expand?.(true);
});
document.body.addEventListener('mouseleave', () => {
  clearTimeout(leaveTimer);
  leaveTimer = setTimeout(() => {
    document.body.classList.remove('expanded');
    api()?.expand?.(false);
  }, 220);
});

for (const id of ['cCost', 'fCost']) {
  $(id).addEventListener('click', () => api()?.open_flyout?.());
}
for (const id of ['closeBtn', 'closeBtn2']) {
  $(id).addEventListener('click', () => api()?.hide?.());
}
document.addEventListener('contextmenu', (e) => e.preventDefault());

tick();
