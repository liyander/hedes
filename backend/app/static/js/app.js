import { $, api, esc, icon, LOGO, toast, ago, ApiError } from './lib.js';
import { installDrawer } from './assistant.js';
import { syncHelpPage, toggleHelp } from './help.js';

export const state = { user: null, cleanup: null, demo: false, page: 'dashboard', counts: { approvals: 0, alerts: 0 } };
export const isAdmin = () => ['admin', 'owner'].includes(state.user?.role);
export const isOwner = () => state.user?.role === 'owner';
const roleName = (r) => ({ owner: 'Platform owner', admin: 'Admin', user: 'Builder' }[r] || r);

const NAV = [
  { items: [['dashboard', 'Dashboard', 'grid'], ['studio', 'AI Studio', 'spark']] },
  { label: 'Build', items: [['integrations', 'Integrations', 'plug'], ['workflows', 'Workflows', 'flow'], ['testlab', 'Test Lab', 'flask'], ['credentials', 'Credentials', 'key']] },
  { label: 'Govern', items: [['approvals', 'Approvals', 'check', 'approvals'], ['tools', 'Tool Registry', 'box'], ['versions', 'Version History', 'history'], ['audit', 'Audit log', 'list']] },
  { label: 'Operate', items: [['sources', 'Data Sources', 'db', 'sourcebad'], ['monitoring', 'Monitoring', 'pulse', 'alerts'], ['healing', 'Self-healing', 'wrench', 'healing']] },
  { label: 'Platform', items: [['providers', 'AI Providers', 'cpu'], ['users', 'Users', 'user', null, 'admin'], ['settings', 'Settings', 'cog']] },
];

const ROUTES = {
  dashboard: ['./pages/dashboard.js', 'Dashboard'],
  studio: ['./pages/studio.js', 'AI Studio'],
  integrations: ['./pages/integrations.js', 'Integrations'],
  workflows: ['./pages/workflows.js', 'Workflows'],
  testlab: ['./pages/testlab.js', 'Test Lab'],
  credentials: ['./pages/credentials.js', 'Credentials'],
  approvals: ['./pages/approvals.js', 'Approvals'],
  tools: ['./pages/tools.js', 'Tool Registry'],
  versions: ['./pages/versions.js', 'Version History'],
  audit: ['./pages/audit.js', 'Audit log'],
  sources: ['./pages/sources.js', 'Data Sources'],
  monitoring: ['./pages/monitoring.js', 'Monitoring'],
  healing: ['./pages/healing.js', 'Self-healing'],
  providers: ['./pages/providers.js', 'AI Providers'],
  users: ['./pages/users.js', 'Users'],
  settings: ['./pages/settings.js', 'Settings'],
  fix: ['./pages/fix.js', 'Fix'],
};

export const go = (hash) => { if (location.hash === hash) route(); else location.hash = hash; };

function setTheme(t) {
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem('integra-theme', t); } catch (e) { /* ignore */ }
}

function shell() {
  const u = state.user;
  return `<div class="shell" id="shell">
    <aside class="sidebar">
      <a class="brand" href="#/dashboard">${LOGO}<div><b>H.E.D.E.S</b><small>Hybrid Automated Data Exchange System</small></div></a>
      <nav class="nav" id="nav">${NAV.map((g) => `${g.label ? `<div class="nav-label">${g.label}</div>` : ''}${g.items.filter((it) => !it[4] || (it[4] === 'admin' ? isAdmin() : isOwner())).map(([k, label, ic, cnt]) =>
        `<a href="#/${k}" data-nav="${k}">${icon(ic)}<span>${label}</span>${cnt ? `<span class="count warn" data-count="${cnt}" hidden></span>` : ''}</a>`).join('')}`).join('')}</nav>
      <div class="side-status"><a href="#/providers"><span class="dot on" id="ai-dot"></span><div class="ss-text"><b>AI gateway</b><small id="ai-route">checking…</small></div></a></div>
      <div class="side-foot">
        <button class="side-user" id="user-btn"><span class="initial">${esc(u.username[0])}</span><span class="me"><b>${esc(u.name || u.username)}</b><small>${roleName(u.role)} · ${esc(u.organization.name)}</small></span>${icon('up', 16)}</button>
        <div class="pop user" id="user-pop" hidden>
          <button class="menu-item" data-act="theme">${icon('moon', 16)} Toggle dark mode</button>
          <a class="menu-item" href="#/settings">${icon('cog', 16)} Settings &amp; API tokens</a>
          <button class="menu-item" data-act="logout">${icon('logout', 16)} Sign out</button>
        </div>
      </div>
    </aside>
    <div class="main">
      <header class="topbar">
        <button class="btn ghost icon" id="collapse" aria-label="Toggle navigation">${icon('menu')}</button>
        <span class="top-title" id="top-title"></span>
        <span class="spacer"></span>
        <span class="live"><span class="dot on"></span>Live</span>
        <button class="btn ghost" id="help-btn" aria-label="About this page" title="About this page: usage and options">${icon('doc', 16)} Page guide</button>
        <a class="btn ghost" href="#/studio">${icon('spark', 16)} AI Studio</a>
        <button class="btn ghost icon bell" id="bell" aria-label="Alerts">${icon('bell')}<span class="count" id="bell-count" hidden></span></button>
        <button class="btn ghost icon" data-act="theme" aria-label="Toggle theme">${icon('moon')}</button>
        <div class="pop alerts" id="alert-pop" hidden></div>
      </header>
      <main class="content" id="content"></main>
    </div>
  </div>`;
}

async function refreshCounts() {
  if (!state.user) return;
  try {
    const [alerts, m, src, heal] = await Promise.all([api('/alerts'), api('/monitoring/metrics'), api('/sources').catch(() => null), api('/healing/cases?limit=1').catch(() => null)]);
    state.alerts = alerts;
    state.counts = { approvals: isAdmin() ? m.pending_approvals : 0, alerts: alerts.length,
      sourcebad: src ? (src.summary.failing || 0) + (src.summary.stale || 0) + (src.summary.degraded || 0) : 0,
      healing: heal ? (heal.summary.needs_admin || 0) + (heal.summary.awaiting_approval || 0) : 0 };
    for (const [k, n] of Object.entries(state.counts)) {
      const el = document.querySelector(`[data-count="${k}"]`);
      if (el) { el.textContent = n > 99 ? '99+' : n; el.hidden = !n; }
    }
    const bc = $('#bell-count');
    if (bc) { bc.textContent = alerts.length; bc.hidden = !alerts.length; }
  } catch (e) { /* transient */ }
}

async function refreshAi() {
  try {
    const r = await api('/ai/routing');
    const first = r.tasks.find((t) => t.task === 'config_generation')?.candidates[0] || 'none';
    $('#ai-route').textContent = first;
    $('#ai-dot').className = `dot ${first === 'none' ? 'bad' : first.startsWith('Built-in') ? 'off' : 'on'}`;
  } catch (e) { /* ignore */ }
}

function renderAlerts() {
  const pop = $('#alert-pop');
  const list = state.alerts || [];
  pop.innerHTML = `<div class="row between" style="padding:6px 8px"><b>Alerts</b>${list.length ? '<button class="btn sm ghost" data-act="ackall">Mark all read</button>' : ''}</div>` +
    (list.length ? list.map((a) => `<a class="alert-row" href="${esc(a.link || '#/monitoring')}" data-ack="${a.id}"><span class="badge ${a.severity === 'high' ? 'high' : a.severity === 'info' ? 'info' : 'medium'}">${esc(a.kind)}</span> ${esc(a.message)}<small>${ago(a.created_at)}</small></a>`).join('')
      : '<p class="muted small" style="padding:4px 10px 10px">No new alerts.</p>');
}

function bindShell() {
  document.addEventListener('click', async (e) => {
    const t = e.target.closest('[data-act], #user-btn, #bell, #collapse, [data-ack], #help-btn');
    const pop = $('#user-pop'), apop = $('#alert-pop');
    if (!e.target.closest('#user-pop, #user-btn') && pop) pop.hidden = true;
    if (!e.target.closest('#alert-pop, #bell') && apop) apop.hidden = true;
    if (!t) return;
    if (t.id === 'help-btn') toggleHelp(state.page || 'dashboard');
    else if (t.id === 'user-btn') pop.hidden = !pop.hidden;
    else if (t.id === 'bell') { renderAlerts(); apop.hidden = !apop.hidden; }
    else if (t.id === 'collapse') {
      const sh = $('#shell');
      sh.classList.toggle(matchMedia('(max-width: 820px)').matches ? 'open' : 'collapsed');
    } else if (t.dataset.ack) { api(`/alerts/${t.dataset.ack}/ack`, { method: 'POST' }).then(refreshCounts); apop.hidden = true; }
    else if (t.dataset.act === 'ackall') { await api('/alerts/ack-all', { method: 'POST' }); await refreshCounts(); renderAlerts(); }
    else if (t.dataset.act === 'theme') setTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
    else if (t.dataset.act === 'logout') { await api('/auth/logout', { method: 'POST' }); state.user = null; go('#/login'); }
  });
}

let shellBound = false;
let counter;

export async function route() {
  if (state.cleanup) { try { state.cleanup(); } catch (e) { /* ignore */ } state.cleanup = null; }
  const parts = (location.hash.replace(/^#\/?/, '') || 'dashboard').split('/').map(decodeURIComponent);
  const app = $('#app');
  if (parts[0] === 'invite') return (await import('./pages/login.js')).renderInvite(app, parts[1]);
  if (!state.user) {
    try { state.user = await api('/auth/me'); } catch (e) { state.user = null; }
  }
  if (!state.user || parts[0] === 'login') {
    if (state.user && parts[0] === 'login') return go('#/dashboard');
    // A WhatsApp fix link opened while signed out: come back to it after signing in.
    if (parts[0] === 'fix') { try { sessionStorage.setItem('integra-return', location.hash); } catch (e) { /* ignore */ } }
    clearInterval(counter);
    document.querySelectorAll('#ask-ai, #assistant-drawer, #help-drawer').forEach((n) => n.remove());
    return (await import('./pages/login.js')).renderLogin(app);
  }
  if (!$('#shell')) {
    app.innerHTML = shell();
    if (!shellBound) { bindShell(); shellBound = true; }
    refreshCounts(); refreshAi();
    try { state.demo = (await api('/demo/state')).demo === true; } catch (e) { state.demo = false; }
    clearInterval(counter);
    counter = setInterval(refreshCounts, 20000);
  }
  const key = ROUTES[parts[0]] ? parts[0] : 'dashboard';
  state.page = key;
  document.querySelectorAll('[data-nav]').forEach((a) => a.classList.toggle('active', a.dataset.nav === key));
  $('#top-title').textContent = ROUTES[key][1];
  document.title = `${ROUTES[key][1]} — H.E.D.E.S`;
  if (matchMedia('(max-width: 820px)').matches) $('#shell').classList.remove('open');
  installDrawer();
  syncHelpPage(key);
  // A fresh element per route, so listeners a page attaches never outlive it.
  const el = $('#content').cloneNode(false);
  $('#content').replaceWith(el);
  el.innerHTML = '<div class="page"><span class="spinner"></span></div>';
  try {
    const mod = await import(ROUTES[key][0]);
    const cleanup = await mod.render({ el, params: parts.slice(1), refreshCounts });
    state.cleanup = typeof cleanup === 'function' ? cleanup : null;
  } catch (err) {
    console.error(err);
    if (err instanceof ApiError && err.status === 401) return;
    el.innerHTML = `<div class="page"><div class="callout bad"><b>Could not load this page.</b> ${esc(err.message)}</div></div>`;
  }
}

window.addEventListener('hashchange', route);
window.addEventListener('unhandledrejection', (e) => { if (e.reason?.message) toast(e.reason.message, 'bad'); });
route();
