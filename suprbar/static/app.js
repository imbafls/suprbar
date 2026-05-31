// supr.bar — flyout client logic
//
// Data source: /api/today returns the aggregator response shape
// ({today, sources, active, live_sessions, last_session_seen}). Re-rendered
// every 5s and on focus. Keyboard: Esc closes, F5/Space refresh, Alt+Q quits,
// ? help, Ctrl+L opens logs, Ctrl+K focuses key field, Ctrl+E exports CSV,
// Ctrl+W closes, 1–7 switch ranges.

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

// Mirror of prefs.display, kept current by applyDisplayPrefs() so the
// formatters below can honor cost_format / token_format without a lookup.
let displayPrefs = {};

function fmtTokens(n) {
  n = Number(n || 0);
  if (displayPrefs.token_format === 'full') return n.toLocaleString();
  if (n >= 1e9) return (n / 1e9).toFixed(2) + 'B';
  if (n >= 1e6) return (n / 1e6).toFixed(2) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k';
  return String(n);
}

function fmtCost(n) {
  n = Number(n || 0);
  if (displayPrefs.cost_format === 'whole') {
    return { whole: Math.round(n).toLocaleString(), cents: '' };
  }
  const whole = Math.floor(n);
  const cents = (n - whole).toFixed(2).slice(1);
  return { whole: whole.toLocaleString(), cents };
}

function fmtMoney(n, digits = 2) {
  n = Number(n || 0);
  if (n >= 1000) return '$' + Math.round(n).toLocaleString();
  if (n >= 10) return '$' + n.toFixed(1);
  return '$' + n.toFixed(digits);
}

function fmtPct(n) {
  n = Number(n || 0);
  return Math.round(n * 100) + '%';
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
let lastRefreshAt = null;
let todayEtag = null;
let appMeta = {};

// ───────────────────────── Toast system (#2) ─────────────────────────

let _toastTimer = null;
function toast(msg, kind = 'ok', ms = 2400) {
  if (!msg) return;
  const el = document.getElementById('toast');
  if (!el) return;
  el.textContent = msg;
  el.classList.remove('ok', 'err', 'warn', 'show');
  el.classList.add(kind === 'err' ? 'err' : kind === 'warn' ? 'warn' : 'ok', 'show');
  el.hidden = false;
  if (_toastTimer) clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => {
    el.classList.remove('show');
    el.hidden = true;
  }, ms);
}

function openPath(p) {
  if (!p) return;
  fetch('/api/open-path', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ p }),
  }).catch(() => {});
}

function setConnBanner(show, text) {
  const b = document.getElementById('connBanner');
  const t = document.getElementById('connBannerText');
  if (!b) return;
  if (!show) {
    b.hidden = true;
    document.body.classList.remove('offline');
    return;
  }
  b.hidden = false;
  document.body.classList.add('offline');
  if (t && text) t.textContent = text;
}

// ───────────────────────── Update banner (auto-update) ─────────────────────────

let _updateInfo = null;
function setUpdateBanner(info) {
  const b = document.getElementById('updateBanner');
  if (!b) return;
  _updateInfo = info || null;
  const ver = info?.latest;
  if (!info?.available) { b.hidden = true; return; }
  if (localStorage.getItem('suprbar.update.dismissed') === ver) { b.hidden = true; return; }
  const v = document.getElementById('updateVersion');
  if (v) v.textContent = 'v' + ver;
  const t = document.getElementById('updateBannerText');
  if (t && t.firstChild) t.firstChild.textContent =
    `Update available — v${info.current || appMeta?.version || '?'} → `;
  b.hidden = false;
}

function _fmtCheckedAt(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return '';
  return d.toLocaleString();
}

function setUpdateAboutStatus(info) {
  // Read-only About line: surfaces availability / errors and the last-check
  // timestamp (updates.last_check, persisted server-side; shown here only).
  const about = document.getElementById('updateAboutStatus');
  if (!about || !info) return;
  const when = _fmtCheckedAt(info.checked_at);
  if (info.error) {
    about.textContent = `Check failed (${info.error}).` + (when ? ` Last tried ${when}.` : '');
  } else if (info.available) {
    about.textContent = `New version v${info.latest} available.`;
  } else if (info.latest || when) {
    about.textContent = "You're on the latest version."
      + (when ? ` Last checked ${when}.` : '');
  } else {
    about.textContent = 'No update check yet.';
  }
}

async function loadUpdateStatus() {
  // Read cached status (no network) on boot — banner shows if the once-per-
  // launch check (server-side, fired by the tray) already found one.
  try {
    const r = await fetch('/api/update/status', { cache: 'no-store' });
    if (!r.ok) return;
    const info = await r.json();
    setUpdateBanner(info);
    setUpdateAboutStatus(info);
  } catch (_) { /* ignore */ }
}

async function checkForUpdates({ manual = false } = {}) {
  try {
    const r = await fetch('/api/update/check', { method: 'POST' });
    if (!r.ok) throw new Error('http ' + r.status);
    const info = await r.json();
    setUpdateBanner(info);
    setUpdateAboutStatus(info);
    if (manual && !info.available && !info.error) toast('up to date', 'ok', 1400);
    if (manual && info.error) toast('update check failed', 'err');
    return info;
  } catch (_) {
    if (manual) toast('update check failed', 'err');
  }
}

function getTopN() {
  const n = Number(prefsCache?.projects?.top_n);
  return Number.isFinite(n) && n > 0 ? Math.min(24, Math.floor(n)) : 8;
}

function updateProjectsTitle(d) {
  const el = document.getElementById('projectsListTitle');
  if (!el) return;
  const isToday = !!(d.today || !window.__suprbar_range || window.__suprbar_range === 'today');
  const label = d.range?.label || currentRange || 'range';
  el.textContent = isToday ? 'Top projects today' : `Top projects · ${label}`;
}

function renderStatusStrip(d) {
  const el = document.getElementById('statusStrip');
  if (!el) return;
  const cm = d.cache_meta || {};
  const insights = d.insights || {};
  const parts = [];
  if (cm.fresh === false) parts.push({ t: 'stale cache', warn: true });
  if (Number(d.parse_errors || 0) > 0) {
    parts.push({ t: `${d.parse_errors} parse err`, warn: true });
  }
  if (insights.sessions_today != null) parts.push({ t: `${insights.sessions_today} sessions` });
  if (insights.projects_today != null) parts.push({ t: `${insights.projects_today} projects` });
  if (lastRefreshAt) {
    parts.push({ t: 'updated ' + fmtAgo((Date.now() - lastRefreshAt) / 1000) });
  }
  const scanMs = cm.last_scan_ms ?? d.elapsed_ms;
  if (scanMs != null) parts.push({ t: `scan ${scanMs}ms` });
  el.innerHTML = parts.map(p =>
    `<span class="status-pill${p.warn ? ' warn' : ''}">${escape(p.t)}</span>`,
  ).join('');
  el.hidden = !parts.length;
}

function rememberSources(sources) {
  if (Array.isArray(sources) && sources.length) {
    window.__suprbar_last_sources = sources;
  }
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

function shortProject(name) {
  if (!name) return '—';
  return name.length > 30 ? name.slice(0, 29) + '…' : name;
}

function renderLiveSessions(d) {
  // Glance-first: the lead session is promoted to the "Now burning" card
  // (#liveSessions); the rest fold into "Other live" rows (#liveSessionList).
  const card = document.getElementById('liveSessions');
  const otherWrap = document.getElementById('otherLive');
  const list = document.getElementById('liveSessionList');
  const countEl = document.getElementById('liveCount');

  const sessions = Array.isArray(d.live_sessions) ? d.live_sessions : [];
  if (countEl) countEl.textContent = String(sessions.length);

  if (!sessions.length) {
    if (card) card.hidden = true;
    if (otherWrap) otherWrap.hidden = true;
    if (list) list.innerHTML = '';
    return;
  }

  // Lead session → Now burning card.
  const lead = sessions[0];
  if (card) {
    card.hidden = false;
    const burn = Number(lead.burn_rate_usd_per_hour || 0);
    const age = lead.last_activity ? fmtAgo((Date.now() - new Date(lead.last_activity).getTime()) / 1000) : 'live';
    const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
    set('ncProj', shortProject(lead.project));
    set('ncCost', '$' + Number(lead.cost_today || 0).toFixed(2));
    set('ncMeta', `${shortModel(lead.model)} · ${Number(lead.messages_today || 0).toLocaleString()} msg`);
    set('ncBurn', burn > 0 ? `↑ $${burn.toFixed(2)}/h` : '');
    set('ncAgo', age);
    const projEl = document.getElementById('ncProj');
    if (projEl) {
      projEl.dataset.path = lead.path || '';
      projEl.title = (lead.path || lead.project || '') + ' · click to open';
    }
  }

  // Remaining sessions → "Other live" rows inside Details.
  const rest = sessions.slice(1);
  if (otherWrap) otherWrap.hidden = rest.length === 0;
  if (list) {
    const rows = rest.slice(0, 4).map((s) => {
      const burn = Number(s.burn_rate_usd_per_hour || 0);
      const burnTxt = burn > 0 ? `$${burn.toFixed(2)}/h` : '—';
      const age = s.last_activity ? fmtAgo((Date.now() - new Date(s.last_activity).getTime()) / 1000) : 'live';
      const pathAttr = s.path ? ` data-path="${escapeAttr(s.path)}"` : '';
      return `<div class="lrow"${pathAttr} title="${escapeAttr(s.path || s.project || '')} · click to open">
        <span class="proj"><span class="pip"></span><span class="nm">${escape(shortProject(s.project))}</span></span>
        <span class="cost">$${Number(s.cost_today || 0).toFixed(2)}</span>
        <span class="sub">${escape(shortModel(s.model))} · ${Number(s.messages_today || 0).toLocaleString()} msg · ${burnTxt}</span>
        <span class="ago">${escape(age)}</span>
      </div>`;
    }).join('');
    const overflow = rest.length > 4
      ? `<button class="lrow more">+${rest.length - 4} more session${rest.length - 4 === 1 ? '' : 's'}</button>`
      : '';
    list.innerHTML = rows + overflow;
  }
}

function totalsFromPayload(d) {
  return d.today || d.totals || {};
}

function projectRowsFromPayload(d) {
  if (Array.isArray(d.by_project)) return d.by_project;
  if (Array.isArray(d.projects)) {
    return d.projects.map(p => ({
      project: p.project || p.name || p.path || 'project',
      cost: p.cost || 0,
      messages: p.messages || 0,
      tokens: p.tokens || 0,
      models: p.models || [],
    }));
  }
  return [];
}

function renderImpactStrip(d) {
  const t = totalsFromPayload(d);
  const insights = d.insights || {};
  const cost = Number(t.cost || 0);
  const messages = Number(t.messages || 0);
  const active = d.active || (Array.isArray(d.live_sessions) ? d.live_sessions[0] : null);
  let projected = Number(insights.projected_today_cost || 0) || cost;
  if (d.today && active?.burn_rate_usd_per_hour) {
    const now = new Date();
    const end = new Date(now);
    end.setHours(24, 0, 0, 0);
    projected = Number(insights.projected_today_cost || 0) || (cost + Number(active.burn_rate_usd_per_hour || 0) * Math.max(0, (end - now) / 3600000));
  }
  const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
  // Hero "projected" signal — warn-styled when projected spend exceeds the
  // daily budget. Only meaningful for "today"; cleared in range views.
  const iProj = document.getElementById('iProjected');
  if (iProj) {
    const isToday = !!d.today || !window.__suprbar_range || window.__suprbar_range === 'today';
    if (cost > 0 && isToday) {
      iProj.innerHTML = '<span class="arrow">▲</span> projected ' + escape(fmtMoney(projected));
      const limit = Number(prefsCache?.budgets?.daily_limit || 0);
      iProj.classList.toggle('warn', limit > 0 && projected > limit);
    } else {
      iProj.textContent = '';
      iProj.classList.remove('warn');
    }
  }
  set('iAvgMsg', messages > 0 ? fmtMoney(Number(insights.cost_per_message || 0) || cost / messages, 3) : '—');
  set('iSaved', Number(insights.cache_savings_usd || t.cache_savings_usd || 0) > 0 ? fmtMoney(insights.cache_savings_usd || t.cache_savings_usd) : '—');
  const share = Number(insights.top_project_share || 0);
  set('iTopProj', share > 0 ? fmtPct(share) : '—');
}

function renderHourlySparkline(hourly) {
  const host = document.getElementById('hourlySpark');
  if (!host) return;
  const rows = Array.isArray(hourly) ? hourly : [];
  if (!rows.length) {
    host.innerHTML = '';
    host.hidden = true;
    return;
  }
  const max = Math.max(...rows.map(h => Number(h.cost || 0)), 0);
  host.hidden = false;
  host.innerHTML = rows.map(h => {
    const cost = Number(h.cost || 0);
    const pct = max > 0 ? Math.max(8, (cost / max) * 100) : 8;
    const active = cost === max && max > 0 ? ' peak' : '';
    const hour = String(h.hour ?? '').padStart(2, '0');
    return `<span class="spark-bar${active}" style="height:${pct.toFixed(1)}%" title="${hour}:00 · ${fmtMoney(cost)}"></span>`;
  }).join('');
}

function renderSourceCards(sources) {
  const host = document.getElementById('sourceCards');
  if (!host) return;
  const rows = Array.isArray(sources) ? sources : [];
  if (!rows.length) {
    host.hidden = true;
    host.innerHTML = '';
    return;
  }
  host.hidden = false;
  host.innerHTML = rows.map(s => {
    const kind = s.id === 'local' ? 'local' : s.id === 'anthropic_api' ? 'api' : 'other';
    const state = s.ok ? 'ok' : (s.error === 'disabled' || s.error === 'no admin key configured') ? 'off' : 'err';
    const label = s.id === 'local' ? 'Claude Code' : s.id === 'anthropic_api' ? 'Anthropic API' : (s.label || s.id);
    const title = s.ok ? `${Number(s.messages_today || 0).toLocaleString()} msgs` : (s.error || 'disabled');
    return `<div class="source-card ${kind} ${state}" title="${escapeAttr(title)}">
      <span class="source-dot"></span>
      <span class="source-name">${escape(label)}</span>
      <span class="source-money">${s.ok ? fmtMoney(s.cost_today || 0) : state}</span>
    </div>`;
  }).join('');
}

function renderProjectList(d) {
  const list = document.getElementById('projectsListItems');
  if (!list) return;
  const rows = projectRowsFromPayload(d).slice(0, getTopN());
  const countEl = document.getElementById('projectsListCount');
  if (countEl) countEl.textContent = rows.length ? String(rows.length) : '';
  if (!rows.length) {
    list.innerHTML = '';
    return;
  }
  const maxCost = Math.max(...rows.map(p => Number(p.cost || 0)), 0);
  list.innerHTML = rows.map((p, i) => {
    const cost = Number(p.cost || 0);
    const pct = maxCost > 0 ? Math.max(4, (cost / maxCost) * 100) : 0;
    const model = Array.isArray(p.models) && p.models.length ? shortModel(p.models[0]) : '';
    return `<li title="${escapeAttr(p.project || 'project')}">
      <span class="rank">${i + 1}</span>
      <span class="project-main">
        <span class="name">${escape(shortProject(p.project || 'project'))}</span>
        <span class="project-bar"><span style="width:${pct.toFixed(1)}%"></span></span>
      </span>
      <span class="val">${fmtMoney(cost)} · ${Number(p.messages || 0).toLocaleString()} msgs${model ? ' · ' + escape(model) : ''}</span>
    </li>`;
  }).join('');
}

function buildUsageSummary(d) {
  const t = totalsFromPayload(d);
  const active = d.active || (Array.isArray(d.live_sessions) ? d.live_sessions[0] : null);
  const rangeLabel = d.range?.label || (d.today ? 'today' : currentRange || 'range');
  const parts = [
    `supr.bar ${rangeLabel}`,
    `cost ${fmtMoney(t.cost || 0)}`,
    `${Number(t.messages || 0).toLocaleString()} messages`,
    `${fmtTokens((t.input || 0) + (t.output || 0) + (t.cache_5m || 0) + (t.cache_1h || 0) + (t.cache_read || 0))} tokens`,
  ];
  if (active?.project) parts.push(`active ${active.project}`);
  if (active?.burn_rate_usd_per_hour) parts.push(`${fmtMoney(active.burn_rate_usd_per_hour)}/h`);
  return parts.join(' · ');
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
          return `<span class="pill ${escape(srcLabel(s.id))}" title="${escapeAttr(s.error || '')}">${escape(srcLabel(s.id))} · err</span>`;
        }
        return `<span class="pill ${escape(srcLabel(s.id))}">${escape(srcLabel(s.id))} $${s.cost_today.toFixed(2)}</span>`;
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

  // Live sessions panel (all JSONL touched within live window)
  rememberSources(sources);
  renderImpactStrip(d);
  renderHourlySparkline(d.hourly);
  renderSourceCards(sources);
  renderLiveSessions(d);
  renderStatusStrip(d);
  updateProjectsTitle(d);

  // Active vs Idle
  const liveSessions = Array.isArray(d.live_sessions) ? d.live_sessions : [];
  const sessEl = document.getElementById('mSessions');
  if (sessEl) {
    const n = d.insights?.sessions_today ?? liveSessions.length ?? (d.active ? 1 : 0);
    sessEl.textContent = Number(n || 0).toLocaleString();
  }
  const nLive = liveSessions.length;
  document.body.classList.toggle('has-live', nLive > 0);
  document.body.classList.toggle('has-parse-errors', Number(d.parse_errors || 0) > 0);
  const live = $('liveIndicator');
  if (active || nLive > 0) {
    if (live) {
      live.hidden = false;
      live.classList.remove('dim');
      live.innerHTML = `<span class="pip"></span> <span id="liveCount">${Math.max(1, nLive)}</span> live`;
    }
    const mr = $('metricRow'); if (mr) mr.hidden = false;
    const es = $('emptyState'); if (es) es.hidden = true;

    const head = active || liveSessions[0] || null;
    setT('mMessages', (head?.messages_today ?? 0).toLocaleString());
    setT('mModel', shortModel(head?.model));
    startedAt = head?.started_at ? new Date(head.started_at) : null;
    updateStartedDisplay();

    const proj = head?.project || '~/.claude';
    const scanMs = d.cache_meta?.last_scan_ms ?? d.elapsed_ms ?? 0;
    const parse = Number(d.parse_errors || 0);
    const projPart = displayPrefs.show_project === false ? '' : `${shortProject(proj)} · `;
    setT('footMeta', `${projPart}scan ${scanMs}ms${parse ? ` · ${parse} parse err` : ''}`);
  } else {
    if (live) {
      live.hidden = false;
      live.classList.add('dim');
      live.innerHTML = '<span class="pip"></span> idle';
    }
    const mr = $('metricRow'); if (mr) mr.hidden = true;
    const es = $('emptyState'); if (es) es.hidden = false;
    startedAt = null;
    const scanHint = d.scan_source ? d.scan_source.replace(/^.*[\\/]/, '…/') : '~/.claude';
    if (d.last_session_seen) {
      const last = new Date(d.last_session_seen.last_activity);
      const ago = (Date.now() - last.getTime()) / 1000;
      setT('emptySub', `last seen ${fmtAgo(ago)}`);
    } else {
      setT('emptySub', `watching ${scanHint}`);
    }
    const scanMs = d.cache_meta?.last_scan_ms ?? d.elapsed_ms ?? 0;
    setT('footMeta', `watching ${scanHint} · scan ${scanMs}ms`);
  }

  renderProjectList(d);

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
    const headers = {};
    if (todayEtag && !refresh) headers['If-None-Match'] = todayEtag;
    const res = await fetch(
      refresh ? '/api/today?refresh=1' : '/api/today',
      { cache: 'no-store', signal, headers },
    );
    if (res.status === 304) {
      lastRefreshAt = Date.now();
      setConnBanner(false);
      return;
    }
    if (!res.ok) {
      const msg = statusMessage(res.status);
      // surface obvious problems
      if (res.status === 401 || res.status === 403) toast(msg, 'err');
      throw new Error('http ' + res.status);
    }
    const etag = res.headers.get('ETag');
    if (etag) todayEtag = etag;
    const data = await res.json();
    render(data);
    lastRefreshAt = Date.now();
    setConnBanner(false);
    // success — reset backoff
    consecutiveFailures = 0;
    nextBackoff = 1000;
    if (retryTimer) { clearTimeout(retryTimer); retryTimer = null; }
  } catch (e) {
    if (e?.name === 'AbortError') return;
    consecutiveFailures++;
    const msg = statusMessage(0, String(e?.message || e));
    setConnBanner(true, 'Offline — ' + msg);
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
    const adm = $('adminKeyInput');
    if (adm) {
      adm.value = '';
      adm.placeholder = anth.has_key
        ? `saved: ${anth.key_fingerprint || '••••'}`
        : 'sk-ant-admin01-…';
    }
    syncPinButton(!!(c.ui || {}).pinned);
  } catch (e) { /* swallow */ }
}

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

function applyTabOrder() {
  const order = ['adminKeyInput', 'testKeyBtn', 'clearKeyBtn', 'settingsCloseBtn'];
  order.forEach((id, i) => {
    const el = document.getElementById(id);
    if (el) el.setAttribute('tabindex', String(i + 1));
  });
}

function showSettingsStatus(kind, message) {
  const el = document.getElementById('settingsStatus');
  if (!el) return;
  if (kind === 'ready' || !kind) {
    el.hidden = true;
    el.textContent = '';
    el.className = 'settings-status';
    return;
  }
  el.hidden = false;
  el.className = 'settings-status ' + kind;
  el.textContent = message || '';
}

async function openSettings() {
  if (!overlay) return;
  overlay.hidden = false;
  const search = document.getElementById('settingsSearch');
  if (search) search.value = '';
  showSettingsStatus('loading', 'Loading settings…');
  document.getElementById('settingsSections')?.replaceChildren();
  document.getElementById('settingsQuick')?.replaceChildren();
  document.getElementById('settingsNav')?.replaceChildren();
  loadConfig();
  try {
    await loadPrefs(true);
    if (!schemaCache?.length) {
      throw new Error('Settings schema unavailable');
    }
    renderSettingsQuick();
    renderSettingsNav();
    renderSettings();
    applySettingsSearch('');
    showSettingsStatus('ready');
  } catch (e) {
    showSettingsStatus('error', (e?.message || e) + ' — check that suprbar is running.');
    const host = document.getElementById('settingsSections');
    if (host) {
      host.innerHTML = `<div class="settings-error-card">
        <p>Could not load preferences.</p>
        <button type="button" class="btn-sm" id="settingsRetryBtn">Retry</button>
      </div>`;
      document.getElementById('settingsRetryBtn')?.addEventListener('click', () => openSettings(), { once: true });
    }
  }
  loadDiagnostics();
  applyTabOrder();
  setTimeout(() => {
    const s = document.getElementById('settingsSearch');
    if (s) try { s.focus(); } catch (_) { /* ignore */ }
  }, 50);
}
function closeSettings() {
  if (overlay) overlay.hidden = true;
  showSettingsStatus('ready');
}

function syncPinButton(on) {
  $('pinBtn')?.classList.toggle('on', !!on);
  document.body.classList.toggle('is-pinned', !!on);
}

$('settingsBtn')?.addEventListener('click', openSettings);
$('settingsCloseBtn')?.addEventListener('click', closeSettings);

async function triggerRefresh() {
  const btn = $('refreshBtn');
  if (btn?.classList.contains('loading')) return;
  btn?.classList.add('loading');
  btn?.setAttribute('aria-busy', 'true');
  toast('refreshing…', 'ok', 900);
  try {
    await load({ refresh: true });
  } finally {
    btn?.classList.remove('loading');
    btn?.setAttribute('aria-busy', 'false');
  }
}

$('refreshBtn')?.addEventListener('click', triggerRefresh);

// Drop heavy effects while the native window is being dragged.
function bindWindowDragPerf() {
  const dragSel = '.b-head, .cost-eyebrow, .settings-head';
  const onDown = (e) => {
    if (e.button !== 0) return;
    if (e.target.closest('button, input, select, textarea, a, [role="button"]')) return;
    document.body.classList.add('window-dragging');
  };
  const onUp = () => document.body.classList.remove('window-dragging');
  document.querySelectorAll(dragSel).forEach(el => el.addEventListener('mousedown', onDown));
  window.addEventListener('mouseup', onUp);
  window.addEventListener('blur', onUp);
}
bindWindowDragPerf();

$('pinBtn')?.addEventListener('click', async () => {
  const next = !$('pinBtn').classList.contains('on');
  syncPinButton(next);
  await patchConfig({ pinned: next });
});

async function runTestKey() {
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
    const target = d.scan_source || '~/.claude/projects';
    fetch('/api/open-path', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ p: target }),
    });
  });
});

$('openActiveBtn')?.addEventListener('click', () => {
  const active = lastData?.active || (Array.isArray(lastData?.live_sessions) ? lastData.live_sessions[0] : null);
  const target = active?.path || lastData?.scan_source || '~/.claude/projects';
  fetch('/api/open-path', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ p: target }),
  });
});

async function copySummary() {
  const text = buildUsageSummary(lastData || {});
  try {
    await navigator.clipboard.writeText(text);
    toast('summary copied', 'ok', 1400);
  } catch (_) {
    toast(text, 'ok', 4200);
  }
}

$('copySummaryBtn')?.addEventListener('click', copySummary);

// ───────────────────────── CSV export (#5) ─────────────────────────

function downloadCSV(filename, csv) {
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { document.body.removeChild(a); URL.revokeObjectURL(url); }, 200);
}

function exportTodayCSV() {
  const d = lastData || {};
  const today = d.today || {};
  const date = new Date().toISOString().slice(0, 10);
  const sessions = d.insights?.sessions_today
    ?? (Array.isArray(d.live_sessions) ? d.live_sessions.length : 0);
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
  downloadCSV(`suprbar-today-${date}.csv`, csv);
  toast('CSV downloaded');
}

function exportRangeCSV() {
  const d = lastData || {};
  const t = totalsFromPayload(d);
  const rangeKey = window.__suprbar_range || currentRange || 'range';
  const date = new Date().toISOString().slice(0, 10);
  const summary = [
    'range', rangeKey,
    'cost', (t.cost || 0).toFixed(4),
    'messages', t.messages || 0,
    'input', t.input || 0,
    'output', t.output || 0,
    'cache_5m', t.cache_5m || 0,
    'cache_1h', t.cache_1h || 0,
    'cache_read', t.cache_read || 0,
  ].join(',');
  const projHeader = 'project,cost,messages,tokens';
  const projRows = projectRowsFromPayload(d).map(p => [
    JSON.stringify(p.project || ''),
    (p.cost || 0).toFixed(4),
    p.messages || 0,
    p.tokens || 0,
  ].join(','));
  const csv = summary + '\n' + projHeader + '\n' + projRows.join('\n') + '\n';
  downloadCSV(`suprbar-${rangeKey}-${date}.csv`, csv);
  toast('range CSV downloaded');
}

function exportCurrentCSV() {
  if (window.__suprbar_range && window.__suprbar_range !== 'today') exportRangeCSV();
  else exportTodayCSV();
}

async function requestQuit() {
  try {
    if (!prefsCache) await loadPrefs();
  } catch (_) { /* ignore */ }
  if (prefsCache?.behavior?.confirm_quit && !confirm('Quit supr.bar?')) return;
  fetch('/api/quit', { method: 'POST' });
}

async function loadVersion() {
  try {
    const r = await fetch('/api/version', { cache: 'no-store' });
    if (!r.ok) return;
    appMeta = await r.json();
    const v = 'v' + (appMeta.version || '?');
    ['headerVersion', 'aboutVersion'].forEach((id) => {
      const e = document.getElementById(id);
      if (e) e.textContent = v;
    });
    const dev = document.getElementById('devBadge');
    if (dev) dev.hidden = !appMeta.dev;
  } catch (_) { /* ignore */ }
}

async function loadDiagnostics() {
  const pre = document.getElementById('diagnosticsText');
  if (!pre) return;
  pre.textContent = 'Loading…';
  try {
    const [health, diagnostics] = await Promise.all([
      fetch('/api/health').then(r => r.json()),
      fetch('/api/diagnostics').then(r => r.json()),
    ]);
    const payload = { version: appMeta, health, diagnostics };
    window.__suprbar_diag = payload;
    pre.textContent = JSON.stringify(payload, null, 2);
  } catch (e) {
    pre.textContent = String(e);
  }
}

function maybeOpenSettingsFromNav() {
  if (location.hash === '#settings') openSettings();
  const pending = window.pywebview?.api?.consume_pending_open?.();
  if (pending === 'settings') openSettings();
}

// ───────────────────────── Click cost to copy (#8) ─────────────────────────

$('costNum')?.addEventListener('click', async () => {
  const t = totalsFromPayload(lastData || {});
  const v = Number(t.cost || (lastData?.today?.cost) || 0);
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
    'background:var(--b-elevated)',
    'border:1px solid var(--b-line-2)',
    'border-radius:var(--r-2)', 'padding:var(--sp-1,4px)',
    'box-shadow:var(--b-shadow)',
    'font-family:var(--b-mono)', 'font-size:var(--fs-11)',
    'color:var(--b-text)', 'min-width:160px', 'z-index:9998',
  ].join(';');
  const pinned = $('pinBtn')?.classList.contains('on');
  const items = [
    { label: 'Settings',       fn: openSettings },
    { label: 'Refresh',        fn: () => { triggerRefresh(); } },
    { label: pinned ? 'Unpin' : 'Pin', fn: () => $('pinBtn')?.click() },
    { label: 'Copy summary',   fn: copySummary },
    { label: 'Export CSV',     fn: exportCurrentCSV },
    { label: 'Quit',           fn: requestQuit },
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
    b.addEventListener('mouseenter', () => { b.style.background = 'var(--b-accent-soft)'; });
    b.addEventListener('mouseleave', () => { b.style.background = 'transparent'; });
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
    triggerRefresh();
    return;
  }
  // Ctrl+E → CSV export (#5)
  if (e.ctrlKey && (e.key === 'e' || e.key === 'E')) {
    e.preventDefault();
    exportCurrentCSV();
    return;
  }
  // Space → refresh (when not typing and not focused on an activatable control,
  // so Space still clicks the focused button/tab/link as users expect).
  if (e.key === ' ' && !e.ctrlKey && !e.metaKey && !e.altKey) {
    const t = e.target;
    if (t && /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName)) return;
    if (t?.closest?.('button, [role="button"], a, summary, [contenteditable]')) return;
    e.preventDefault();
    triggerRefresh();
    return;
  }
  // Ctrl+C → copy usage summary, but do not steal copy from fields.
  if (e.ctrlKey && (e.key === 'c' || e.key === 'C')) {
    const t = e.target;
    if (t && /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName)) return;
    e.preventDefault();
    copySummary();
    return;
  }
  // Ctrl+L → open scanned logs folder.
  if (e.ctrlKey && (e.key === 'l' || e.key === 'L')) {
    e.preventDefault();
    $('openLogsBtn')?.click();
    return;
  }
  // Ctrl+K → focus API key field in settings.
  if (e.ctrlKey && (e.key === 'k' || e.key === 'K')) {
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
  // 1..7 switch range tabs.
  if (!e.ctrlKey && !e.metaKey && !e.altKey && /^[1-7]$/.test(e.key)) {
    const t = e.target;
    if (t && /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName)) return;
    const btn = document.querySelectorAll('.range-tabs .rt')[Number(e.key) - 1];
    if (btn) {
      e.preventDefault();
      setRange(btn.dataset.range);
    }
    return;
  }
  // Arrow navigation across range tabs when focus is on a tab.
  if ((e.key === 'ArrowLeft' || e.key === 'ArrowRight') && e.target?.matches?.('.range-tabs .rt')) {
    const tabs = Array.from(document.querySelectorAll('.range-tabs .rt'));
    const idx = tabs.indexOf(e.target);
    const delta = e.key === 'ArrowRight' ? 1 : -1;
    const next = tabs[(idx + delta + tabs.length) % tabs.length];
    if (next) {
      e.preventDefault();
      next.focus();
      setRange(next.dataset.range);
    }
    return;
  }
  // Alt+Q quits
  if (e.altKey && (e.key === 'q' || e.key === 'Q')) {
    e.preventDefault();
    requestQuit();
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
    if (document.body.classList.contains('is-pinned')) return;
    if (overlay && !overlay.hidden) return;
    if (document.getElementById('shortcutsHelp')?.open) return;
    if (window.pywebview?.api?.hide) window.pywebview.api.hide();
  } catch (e) { /* swallow */ }
});

// ───────────────────────── Polling + visibility (#14) ─────────────────────────

function setPollInterval(ms) {
  // ms <= 0 means "manual only" — stop auto-polling instead of spinning up a
  // zero-delay interval (which would hammer /api/today every few ms).
  if (!ms || ms <= 0) {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    pollInterval = 0;
    return;
  }
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
loadVersion();
loadUpdateStatus();
loadPrefs();
setPollInterval(POLL_MS_ACTIVE);
setInterval(updateStartedDisplay, 1000);
window.addEventListener('hashchange', () => {
  if (location.hash === '#settings') openSettings();
});
setTimeout(maybeOpenSettingsFromNav, 120);

document.getElementById('liveSessionList')?.addEventListener('click', (e) => {
  const row = e.target.closest('.lrow[data-path]');
  if (!row?.dataset.path) return;
  openPath(row.dataset.path);
  toast('opening session…', 'ok', 1200);
});
// Now-burning card project name → open the session file.
document.getElementById('ncProj')?.addEventListener('click', () => {
  const p = document.getElementById('ncProj')?.dataset.path;
  if (!p) return;
  openPath(p);
  toast('opening session…', 'ok', 1200);
});

document.getElementById('connRetryBtn')?.addEventListener('click', () => {
  setConnBanner(false);
  load({ refresh: true });
});
document.getElementById('helpBtn')?.addEventListener('click', toggleShortcutsHelp);
document.getElementById('exportBtn')?.addEventListener('click', exportCurrentCSV);
document.getElementById('reportBtn')?.addEventListener('click', async () => {
  try {
    const r = await fetch('/api/open-report', { method: 'POST' }).then(r => r.json());
    toast(r.opened ? 'opening report…' : 'could not open report', r.opened ? 'ok' : 'err', 1600);
  } catch (_) {
    toast('could not open report', 'err');
  }
});
document.getElementById('updateApplyBtn')?.addEventListener('click', async () => {
  const b = document.getElementById('updateBanner');
  b?.classList.add('updating');
  toast('downloading update…', 'ok', 1500);
  try {
    const r = await fetch('/api/update/apply', { method: 'POST' }).then(r => r.json());
    toast(r.ok ? (r.message || 'update starting — restarting…') : (r.error || 'update failed'),
          r.ok ? 'ok' : 'err', r.ok ? 4000 : 3200);
  } catch (_) {
    toast('update failed', 'err');
  } finally {
    b?.classList.remove('updating');
  }
});
document.getElementById('updateDismissBtn')?.addEventListener('click', () => {
  if (_updateInfo?.latest) localStorage.setItem('suprbar.update.dismissed', _updateInfo.latest);
  const b = document.getElementById('updateBanner'); if (b) b.hidden = true;
});
document.getElementById('checkUpdateBtn')?.addEventListener('click',
  () => { toast('checking…', 'ok', 800); checkForUpdates({ manual: true }); });
document.getElementById('checkUpdateBtn2')?.addEventListener('click',
  () => { toast('checking…', 'ok', 800); checkForUpdates({ manual: true }); });
document.getElementById('copyPathBtn')?.addEventListener('click', async () => {
  const path = lastData?.scan_source || '~/.claude/projects';
  try {
    await navigator.clipboard.writeText(path);
    toast('path copied', 'ok', 1400);
  } catch (_) {
    toast(path, 'ok', 3200);
  }
});
document.getElementById('copyDiagBtn')?.addEventListener('click', async () => {
  const text = document.getElementById('diagnosticsText')?.textContent || '';
  try {
    await navigator.clipboard.writeText(text);
    toast('diagnostics copied', 'ok');
  } catch (_) {
    toast('copy failed', 'err');
  }
});
document.getElementById('openLogBtn')?.addEventListener('click', async () => {
  try {
    const d = await fetch('/api/diagnostics').then(r => r.json());
    if (d.log_file) openPath(d.log_file);
    else toast('log path unknown', 'warn');
  } catch (_) {
    toast('could not open log', 'err');
  }
});
document.getElementById('settings-sec-diagnostics')?.addEventListener('toggle', (e) => {
  if (e.target.open) loadDiagnostics();
});

// Expose a tiny debug surface — useful in DevTools.
window.suprbar = { load, loadConfig, toast, exportTodayCSV };

// ════════════════════════════════════════════════════════════════════════
//  Range tabs + budgets + dynamic settings (50+ prefs)
// ════════════════════════════════════════════════════════════════════════

let prefsCache = null;
let schemaCache = null;
let currentRange = localStorage.getItem('suprbar.range') || 'today';

const SECTION_TITLES = {
  range:    'Time range',
  display:  'Display',
  budgets:  'Budgets & alerts',
  behavior: 'Behavior',
  projects: 'Projects',
  sources:  'Sources',
  data:     'Data & privacy',
  window:   'Window',
  ui:       'Tray & startup',
  updates:  'Updates',
};

const SECTION_ORDER = ['display','budgets','behavior','ui','range','projects',
                       'sources','window','data','updates'];

// Internal update state — persisted in config (needed by set_many) but NOT
// user-facing settings. These never render as editable rows: updates.last_check
// is shown read-only in About; updates.skip_version is purely internal. Only
// updates.check_on_launch is an interactive control.
const INTERNAL_PREF_KEYS = new Set([
  'updates.last_check',
  'updates.skip_version',
]);

const SECTION_DEFAULT_OPEN = new Set(['display', 'budgets', 'behavior', 'ui']);

const QUICK_PREFS = [
  { path: 'ui.pinned', label: 'Pin flyout' },
  { path: 'ui.start_on_login', label: 'Start on login' },
  { path: 'display.theme', label: 'Theme', cycle: ['dark', 'light', 'auto'] },
  { path: 'behavior.refresh_seconds', label: 'Poll', cycle: [0, 5, 10, 30, 60] },
  { path: 'sources.anthropic_api.enabled', label: 'API source' },
  { path: 'behavior.confirm_quit', label: 'Confirm quit' },
];

const LABELS = {
  // range
  'range.default':          { label: 'Default range',         desc: 'Time range applied when popup opens.' },
  'range.week_starts_on':   { label: 'Week starts on',        desc: 'Affects the "Wk" range tab.' },
  'range.day_boundary':     { label: 'Day boundary',          desc: 'Compute "today" by local time or UTC.' },
  'range.rolling_24h':      { label: 'Rolling 24h "today"',   desc: 'Use last 24 hours instead of calendar day.' },
  'range.include_weekends': { label: 'Include weekends',      desc: 'Uncheck to exclude Sat/Sun from totals.' },
  // display
  'display.theme':          { label: 'Theme',                 desc: 'Dark, light, or follow OS.' },
  'display.accent':         { label: 'Accent color',          desc: 'Tints highlights and pin.' },
  'display.density':        { label: 'Density',               desc: 'Compact, normal, or spacious padding.' },
  'display.font_scale':     { label: 'Font scale',            desc: '0.85× to 1.25× the base size.' },
  'display.cost_format':    { label: 'Cost format',           desc: 'Show cents or round to whole dollars.' },
  'display.token_format':   { label: 'Token format',          desc: '"1.2k" compact or "1,234" full.' },
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
  'budgets.notify':         { label: 'Toast on warning',      desc: 'Pop a toast when a budget crosses its threshold.' },
  'budgets.tray_warn_color': { label: 'Tint tray icon',       desc: 'Tray icon turns amber/red when over budget.' },
  // behavior
  'behavior.refresh_seconds':      { label: 'Refresh interval',      desc: 'Seconds between auto-refreshes. 0 = manual only.' },
  'behavior.auto_hide':            { label: 'Auto-hide on blur',     desc: 'Hide popup when focus moves away.' },
  'behavior.auto_hide_delay_ms':   { label: 'Auto-hide delay (ms)',  desc: 'Grace period before hiding.' },
  'behavior.always_on_top':        { label: 'Always on top',         desc: 'Popup stays above other windows.' },
  'behavior.live_threshold_seconds': { label: 'Live session window', desc: 'Sessions touched in last N seconds are "live".' },
  'behavior.confirm_quit':         { label: 'Confirm before quit',   desc: 'Prompt before Alt+Q closes the app.' },
  'behavior.click_through':        { label: 'Click-through mode',    desc: 'Popup ignores mouse clicks (header still draggable).' },
  // projects
  'projects.allowlist':     { label: 'Allowlist',             desc: 'Comma-separated. If non-empty, only these are shown.' },
  'projects.denylist':      { label: 'Denylist',              desc: 'Always hidden. Useful for personal/secret repos.' },
  'projects.anonymize':     { label: 'Anonymize names',       desc: 'Replace with "project-1/2/3" in UI.' },
  'projects.top_n':         { label: 'Top N',                 desc: 'Number of projects in the "Top projects" list.' },
  // data
  'data.log_level':          { label: 'Log level',            desc: 'Verbosity of suprbar.log.' },
  // window
  'window.width':             { label: 'Width (px)',          desc: 'Popup width.' },
  'window.height':            { label: 'Height (px)',         desc: 'Popup height.' },
  // sources
  'sources.local.enabled':              { label: 'Local source',            desc: 'Reads ~/.claude/projects/**/*.jsonl.' },
  'sources.anthropic_api.enabled':      { label: 'Anthropic API source',    desc: 'Org-wide spend via Admin API. Requires key above.' },
  // ui
  'ui.pinned':         { label: 'Pinned',           desc: 'Popup does not auto-hide.' },
  'ui.start_on_login': { label: 'Start on Windows sign-in', desc: 'Auto-launch when you log in.' },
  // updates (only check_on_launch is user-facing; last_check / skip_version are
  // internal — see INTERNAL_PREF_KEYS — and never render as editable rows)
  'updates.check_on_launch': { label: 'Check on launch', desc: 'Look for a new release at startup.' },
};

// ──── Range tab handlers ────

window.__suprbar_range = window.__suprbar_range || currentRange || 'today';

// Client-side cache: key → last successful payload. Lets a tab click paint
// instantly while a fresh request runs in the background.
const _rangeCache = new Map();
const _rangeInflight = new Map();

function setRange(key) {
  if (!key) return;
  currentRange = key;
  window.__suprbar_range = key;
  try { localStorage.setItem('suprbar.range', key); } catch (_) { /* ignore */ }
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

if (currentRange !== 'today') {
  setTimeout(() => setRange(currentRange), 0);
}

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
  lastData = d;
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

  const todaySnap = _rangeCache.get('today');
  if (todaySnap?.live_sessions?.length) {
    renderLiveSessions(todaySnap);
  } else {
    document.getElementById('liveSessions')?.setAttribute('hidden', '');
  }
  renderSourceCards(window.__suprbar_last_sources || todaySnap?.sources || []);
  renderImpactStrip(d);
  renderHourlySparkline(d.hourly);
  renderStatusStrip(d);
  updateProjectsTitle(d);
  const live = document.getElementById('liveIndicator');
  if (live) { live.hidden = false; live.classList.add('dim'); live.textContent = `${t.sessions ?? 0} sess · ${t.projects ?? 0} proj`; }

  const metricRow = document.getElementById('metricRow');
  if (metricRow) {
    metricRow.hidden = false;
    const set = (id, v) => { const e = document.getElementById(id); if (e) e.textContent = v; };
    set('mMessages', (t.messages || 0).toLocaleString());
    set('mModel', d.by_model?.[0]?.model ? d.by_model[0].model.replace(/^claude-/, '') : '—');
    set('mStarted', (d.range?.days || 1) + 'd');
    set('mSessions', String(t.sessions ?? d.insights?.sessions_today ?? '—'));
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

  renderProjectList(d);
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
  if (!fill) return;
  const pctVal = Math.min(100, Math.max(0, active.pct));
  fill.style.width = pctVal.toFixed(1) + '%';
  fill.classList.remove('warn', 'over');
  if (active.pct >= 100) fill.classList.add('over');
  else if (active.alerting) fill.classList.add('warn');
  const pct = document.getElementById('bsPct');
  if (pct) {
    pct.textContent = active.pct >= 1000 ? '>999%' : active.pct.toFixed(0) + '%';
    pct.title = `${active.key}: $${active.spent.toFixed(2)} / $${active.limit.toFixed(2)}`;
  }
  // Window label + "$spent / $limit" amount.
  const lblEl = strip.querySelector('.fuel-top .lbl');
  if (lblEl) lblEl.textContent = active.key.charAt(0).toUpperCase() + active.key.slice(1) + ' budget';
  const amt = document.getElementById('bsAmt');
  if (amt) amt.innerHTML = `${escape(fmtMoney(active.spent))} <span class="lim">/ ${escape(fmtMoney(active.limit))}</span>`;
  const remain = document.getElementById('bsRemain');
  if (remain) {
    const left = Math.max(0, active.limit - active.spent);
    remain.textContent = left > 0 ? `${fmtMoney(left)} left` : 'over budget';
  }
  // "on pace to go over by $X" — derived from today's projected spend vs the
  // daily limit. Needs no new data; both numbers already loaded.
  const pace = document.getElementById('bsPace');
  const sep = document.getElementById('bsSep');
  if (pace) {
    let paceTxt = '';
    if (active.key === 'daily') {
      const projected = Number(lastData?.insights?.projected_today_cost || 0);
      if (projected > active.limit) paceTxt = `on pace to go over by ${fmtMoney(projected - active.limit)}`;
    }
    pace.textContent = paceTxt;
    if (sep) sep.hidden = !paceTxt;
  }
  maybeNotifyBudget(active);
}

// Toast once when a budget first crosses its alert threshold or limit, if the
// user enabled budgets.notify. Tracks last-notified state per window so we
// don't re-toast every 30s poll.
let _budgetNotified = {};
function maybeNotifyBudget(active) {
  if (!active || !prefsCache?.budgets?.notify) return;
  const state = active.pct >= 100 ? 'over' : active.alerting ? 'warn' : 'ok';
  if (state !== 'ok' && _budgetNotified[active.key] !== state) {
    if (state === 'over') {
      toast(`Over ${active.key} budget — $${active.spent.toFixed(2)} / $${active.limit.toFixed(2)}`, 'err', 4000);
    } else {
      toast(`${Math.round(active.pct)}% of ${active.key} budget used`, 'warn', 3600);
    }
  }
  _budgetNotified[active.key] = state;
}

setInterval(loadBudgets, 30_000);
loadBudgets();

// ──── Apply display prefs to the DOM ────

function repaintCurrent() {
  if (!lastData) return;
  if (window.__suprbar_range && window.__suprbar_range !== 'today') renderRangeData(lastData);
  else render(lastData);
}

function applyDisplayPrefs(prefs) {
  if (!prefs) return;
  const d = prefs.display || {};
  displayPrefs = d;
  const b = prefs.behavior || {};
  const body = document.body;

  // theme
  body.dataset.theme = (d.theme === 'light') ? 'light' :
                       (d.theme === 'auto')  ? (matchMedia?.('(prefers-color-scheme: light)').matches ? 'light' : 'dark')
                                             : '';
  // accent (default = refined indigo, the redesign default)
  body.dataset.accent = d.accent || 'blue';
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
  setHidden('.tok-bar, .tok-legend', d.show_token_bar === false);
  setHidden('#cacheHit', d.show_cache_info === false);
  setHidden('#mBurnCell, #mBurn', d.show_burn_rate === false);
  setHidden('#mModelCell', d.show_model === false);
  setHidden('#mSessionsCell', d.show_sessions_today === false);

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

function prefsCommit(path, val, { silent = false } = {}) {
  return fetch('/api/prefs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ updates: { [path]: val } }),
  }).then(r => r.json()).then(d => {
    if (d.error) {
      if (typeof toast === 'function') toast(d.error.message || 'invalid', 'err');
      return null;
    }
    prefsCache = d.prefs;
    applyDisplayPrefs(prefsCache);
    if (path === 'ui.pinned') syncPinButton(!!val);
    if (path.startsWith('range.') || path.startsWith('projects.')) {
      load({ refresh: true });
      loadBudgets();
    }
    if (path.startsWith('budgets.')) loadBudgets();
    if (path.startsWith('display.')) { renderSettingsQuick(); repaintCurrent(); }
    if (!silent && typeof toast === 'function') toast('saved', 'ok', 900);
    return d;
  });
}

function resetSettingsSection(section, items) {
  if (!items?.length) return;
  if (!confirm(`Reset all "${SECTION_TITLES[section] || section}" settings to defaults?`)) return;
  const updates = {};
  for (const s of items) updates[s.path] = s.default;
  fetch('/api/prefs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ updates }),
  }).then(r => r.json()).then(d => {
    if (d.error) {
      toast(d.error.message || 'reset failed', 'err');
      return;
    }
    prefsCache = d.prefs;
    applyDisplayPrefs(prefsCache);
    renderSettingsQuick();
    renderSettings();
    load({ refresh: true });
    loadBudgets();
    toast('section reset', 'ok');
  });
}

function buildControl(setting, value) {
  const ctl = document.createElement('div');
  ctl.className = 'ctl-wrap';
  const path = setting.path;

  const commit = (val) => prefsCommit(path, val);

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
    const isMoney = /^budgets\.(daily|weekly|monthly)_limit$/.test(path);
    const useRange = !isMoney && (setting.type === 'float'
      || (lo != null && hi != null && (hi - lo) <= 100));
    const input = document.createElement('input');
    input.className = 'input-fld' + (isMoney ? ' money' : '');
    if (useRange && lo != null && hi != null) {
      input.type = 'range';
      input.min = lo; input.max = hi;
      input.step = setting.type === 'float' ? 0.05 : 1;
    } else {
      input.type = 'number';
      if (lo != null) input.min = lo;
      if (hi != null) input.max = hi;
      if (isMoney) input.step = '0.01';
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

function settingsBySection() {
  const bySection = {};
  for (const s of schemaCache || []) {
    // R1: internal update state (last_check / skip_version) stays in config
    // but is never an editable settings row. Filtering here — the single choke
    // point feeding renderSettings, nav counts, and search — keeps the Updates
    // section showing only its check_on_launch toggle.
    if (INTERNAL_PREF_KEYS.has(s.path)) continue;
    const section = s.path.split('.')[0];
    (bySection[section] ||= []).push(s);
  }
  return bySection;
}

function renderSettingsQuick() {
  const host = document.getElementById('settingsQuick');
  if (!host || !prefsCache) return;
  host.innerHTML = '';
  for (const q of QUICK_PREFS) {
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'rt';
    const cur = getNested(prefsCache, q.path);
    let label = q.label;
    if (q.cycle) {
      if (q.path === 'behavior.refresh_seconds') {
        label = cur === 0 ? 'Manual refresh' : `Every ${cur}s`;
      } else if (q.path === 'display.theme') {
        label = `Theme: ${cur}`;
      } else {
        label = `${q.label}: ${cur}`;
      }
      chip.classList.add('active');
    } else if (!!cur) {
      chip.classList.add('active');
    }
    chip.textContent = label;
    chip.title = q.path;
    chip.addEventListener('click', () => {
      if (q.cycle) {
        const now = getNested(prefsCache, q.path);
        let idx = q.cycle.indexOf(now);
        if (idx < 0) idx = 0;
        const val = q.cycle[(idx + 1) % q.cycle.length];
        prefsCommit(q.path, val).then(() => renderSettingsQuick());
      } else {
        const next = !getNested(prefsCache, q.path);
        prefsCommit(q.path, next).then(() => renderSettingsQuick());
      }
    });
    host.appendChild(chip);
  }
}

function renderSettingsNav() {
  const nav = document.getElementById('settingsNav');
  if (!nav) return;
  const bySection = settingsBySection();
  nav.innerHTML = '';
  const jump = (id) => {
    const el = document.getElementById('settings-sec-' + id);
    el?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    nav.querySelectorAll('.rt').forEach(p => p.classList.toggle('active', p.dataset.section === id));
  };
  const addPill = (id, label, count) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'rt';
    b.dataset.section = id;
    b.textContent = count != null ? `${label} · ${count}` : label;
    b.addEventListener('click', () => jump(id));
    nav.appendChild(b);
  };
  addPill('key', 'API key', null);
  for (const section of SECTION_ORDER) {
    const n = bySection[section]?.length;
    if (!n) continue;
    addPill(section, SECTION_TITLES[section] || section, n);
  }
  addPill('diagnostics', 'Diagnostics', null);
  addPill('about', 'About', null);
}

function renderSettings() {
  const host = document.getElementById('settingsSections');
  if (!host || !schemaCache?.length) return;
  host.innerHTML = '';
  const bySection = settingsBySection();

  for (const section of SECTION_ORDER) {
    const items = bySection[section];
    if (!items?.length) continue;

    const det = document.createElement('details');
    det.className = 'settings-section-collapse';
    det.id = 'settings-sec-' + section;
    det.open = SECTION_DEFAULT_OPEN.has(section);
    det.dataset.section = section;

    const sum = document.createElement('summary');
    sum.className = 'settings-section-summary';
    const title = document.createElement('span');
    title.className = 'sec-title';
    title.textContent = SECTION_TITLES[section] || section;
    const meta = document.createElement('span');
    meta.className = 'sec-meta';
    const count = document.createElement('span');
    count.className = 'sec-count';
    count.textContent = String(items.length);
    const resetBtn = document.createElement('button');
    resetBtn.type = 'button';
    resetBtn.className = 'btn-xs section-reset';
    resetBtn.textContent = 'Reset';
    resetBtn.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      resetSettingsSection(section, items);
    });
    meta.append(count, resetBtn);
    sum.append(title, meta);
    det.appendChild(sum);

    if (section === 'budgets') {
      const card = document.createElement('div');
      card.className = 'budget-quick-card impact-strip';
      card.innerHTML = '<div class="budget-quick-title">Limits ($0 = off)</div><div class="budget-quick-grid" id="budgetQuickGrid"></div>';
      det.appendChild(card);
      const grid = card.querySelector('#budgetQuickGrid');
      for (const key of ['daily', 'weekly', 'monthly']) {
        const path = `budgets.${key}_limit`;
        const setting = items.find(s => s.path === path);
        if (!setting || !grid) continue;
        const wrap = document.createElement('label');
        wrap.className = 'budget-quick-field';
        wrap.innerHTML = `<span>${key}</span>`;
        const inp = document.createElement('input');
        inp.type = 'number';
        inp.min = '0';
        inp.step = '0.01';
        inp.className = 'input-fld money';
        inp.value = String(getNested(prefsCache, path) ?? 0);
        inp.addEventListener('change', () => {
          prefsCommit(path, parseFloat(inp.value) || 0);
        });
        wrap.appendChild(inp);
        grid.appendChild(wrap);
      }
    }

    const body = document.createElement('div');
    body.className = 'settings-section-body';

    for (const setting of items) {
      if (section === 'budgets' && /^budgets\.(daily|weekly|monthly)_limit$/.test(setting.path)) {
        continue;
      }
      const row = document.createElement('div');
      row.className = 'settings-row dense';
      row.dataset.path = setting.path;
      const main = document.createElement('div');
      main.className = 'settings-row-main';
      const meta = LABELS[setting.path] || { label: setting.path, desc: '' };
      const cur = getNested(prefsCache, setting.path);
      const changed = JSON.stringify(cur) !== JSON.stringify(setting.default);
      main.innerHTML = `<div class="lbl">${escape(meta.label)}${changed ? '<span class="changed-dot" title="Changed from default">●</span>' : ''}</div><div class="desc">${escape(meta.desc || '')}</div>`;
      row.appendChild(main);
      row.appendChild(buildControl(setting, cur ?? setting.default));
      body.appendChild(row);
    }
    det.appendChild(body);
    host.appendChild(det);
  }
}

function applySettingsSearch(q) {
  const query = (q || '').trim().toLowerCase();
  let visibleRows = 0;
  let totalRows = 0;
  document.querySelectorAll('.settings-section-collapse[data-section]').forEach(sec => {
    const rows = sec.querySelectorAll('.settings-row[data-path]');
    let any = false;
    rows.forEach(row => {
      totalRows++;
      const path = row.dataset.path.toLowerCase();
      const lbl = row.querySelector('.lbl')?.textContent.toLowerCase() || '';
      const desc = row.querySelector('.desc')?.textContent.toLowerCase() || '';
      const match = !query || path.includes(query) || lbl.includes(query) || desc.includes(query);
      row.classList.toggle('filtered', !match);
      if (match) { any = true; visibleRows++; }
    });
    sec.classList.toggle('empty-section', rows.length > 0 && !any);
    if (query && any) sec.open = true;
  });
  const keySec = document.getElementById('settings-sec-key');
  if (keySec) {
    const lbl = keySec.textContent.toLowerCase();
    const match = !query || lbl.includes(query);
    keySec.classList.toggle('empty-section', !match);
    if (query && match) keySec.open = true;
  }
  const empty = document.getElementById('settingsEmpty');
  const dynamic = document.getElementById('settingsSections');
  if (empty && dynamic) {
    const showEmpty = query && visibleRows === 0;
    empty.hidden = !showEmpty;
    dynamic.hidden = showEmpty;
  }
  const search = document.getElementById('settingsSearch');
  if (search && query) {
    search.dataset.hint = `${visibleRows} of ${totalRows}`;
  } else if (search) {
    delete search.dataset.hint;
  }
}

document.getElementById('settingsSearch')?.addEventListener('input', (e) => {
  applySettingsSearch(e.target.value);
});
document.getElementById('settingsClearSearch')?.addEventListener('click', () => {
  const s = document.getElementById('settingsSearch');
  if (s) s.value = '';
  applySettingsSearch('');
  s?.focus();
});

window.openSettings = openSettings;

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

// Boot prefs apply ASAP so theme/density takes effect on first paint
loadPrefs().catch(() => {});

window.suprbar.setRange = setRange;
window.suprbar.loadPrefs = loadPrefs;
window.suprbar.loadBudgets = loadBudgets;

} // end idempotency guard
