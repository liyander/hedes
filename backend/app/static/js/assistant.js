import { $, api, esc, icon, md, badge, toast, ago, modal } from './lib.js';
import { setDrawer } from './help.js';

// Conversation state is shared by the floating drawer and the full AI Studio page, so a chat can move between them.
const savedThread = () => { try { return sessionStorage.getItem('hedes-thread'); } catch (e) { return null; } };  // storage can be blocked
const S = { threadId: savedThread() || null, messages: [], tags: [], busy: false, status: null, threads: [] };

const TOOL_LABEL = {
  run_source: 'Run a data source now', test_source: 'Test a data source (fetch one page, store nothing)', set_source: 'Change a data source',
  reset_source: 'Re-read full history', reprocess_dead_letter: 'Reprocess a rejected record', run_tool: 'Run a tool', create_integration: 'Create a draft integration',
  create_from_template: 'Create an integration from a template', run_integration_tests: 'Run safe tests', submit_integration: 'Submit for administrator review',
  set_connection_expiry: 'Set credential expiry', fix_integration: 'Fix the integration (revise, test, verify the endpoint, publish)', start_build_session: 'Start an AI Studio build session',
};
const SUGGESTIONS = [
  'What can I do on this page?',
  'Explain how data sources and the common format work.',
  'Which of my data sources need attention, and why?',
  'Help me connect Okta step by step.',
];

const listeners = new Set();
const notify = () => listeners.forEach((fn) => fn());

async function loadStatus() { if (!S.status) { try { S.status = await api('/assistant/status'); } catch (e) { S.status = { configured: false, tools: [] }; } } return S.status; }

async function ensureThread() {
  if (S.threadId) return S.threadId;
  const t = await api('/assistant/threads', { method: 'POST', body: {} });
  S.threadId = t.id; sessionStorage.setItem('hedes-thread', t.id);
  return t.id;
}

export async function openThread(id) {
  try {
    const t = await api(`/assistant/threads/${id}`);
    S.threadId = id; S.messages = t.messages; sessionStorage.setItem('hedes-thread', id);
  } catch (e) { S.threadId = null; S.messages = []; sessionStorage.removeItem('hedes-thread'); }
  notify();
}

export function newThread() { S.threadId = null; S.messages = []; S.tags = []; sessionStorage.removeItem('hedes-thread'); notify(); }
export function addTag(tag) { if (!S.tags.some((t) => t.type === tag.type && t.id === tag.id) && S.tags.length < 6) S.tags.push(tag); notify(); }
export function getPage() { return (location.hash.replace(/^#\/?/, '').split('/')[0]) || 'dashboard'; }

const AUTH_NAME = { bearer: 'Bearer token', api_key_header: 'API key (header)', api_key_query: 'API key (query)', basic: 'Username + password',
  ssws: 'Okta API token (SSWS)', oauth2_client_credentials: 'OAuth2 client credentials', aws_sigv4: 'AWS access key (SigV4)' };

// Secure credential card: the button opens the vault form; the secret never passes through the chat or the model.
export const credentialCard = (c) => `<div class="act cred ${c.connected ? 'executed' : ''}">
    <div class="act-head">${icon('key', 14)} <b>Connect ${esc(c.provider || c.name)} credential</b> ${c.connected ? badge('ok', 'connected') : badge('pending', c.status === 'missing' ? 'not connected' : c.status)}</div>
    <div class="small muted">${esc(AUTH_NAME[c.auth_type] || c.auth_type)} named <code>${esc(c.name)}</code>${c.header_name ? `, sent as header <code>${esc(c.header_name)}</code>` : ''}. Stored encrypted; never shown to the AI.</div>
    ${c.how ? `<div class="small" style="margin-top:6px">${icon('info', 12)} <b>Where to get it:</b> ${esc(c.how)}</div>` : ''}
    ${c.docs ? `<div class="small"><a class="link" href="${esc(c.docs)}" target="_blank" rel="noopener noreferrer">Provider documentation</a></div>` : ''}
    ${c.connected ? '' : `<div class="row" style="margin-top:8px"><button class="btn sm primary" data-connect="${esc(c.name)}" data-auth="${esc(c.auth_type)}" data-header="${esc(c.header_name || '')}">${icon('shield', 13)} Connect securely</button></div>`}
  </div>`;

const STATUS_TEXT = { proposed: 'needs your approval', awaiting_admin: 'waiting for an administrator' };
const actionCard = (a) => {
  const done = a.status !== 'proposed';
  const { credential, ...rest } = a.result || {};
  const res = Object.keys(rest).length ? `<details><summary>Result</summary><pre>${esc(JSON.stringify(rest, null, 2))}</pre></details>` : '';
  const isAdmin = ['admin', 'owner'].includes(S.status?.role);
  return `<div class="act ${esc(a.status)}" data-act="${a.id}">
    <div class="act-head">${icon('shield', 14)} <b>${esc(TOOL_LABEL[a.tool] || a.tool)}</b> ${badge(['proposed', 'awaiting_admin'].includes(a.status) ? 'pending' : a.status, STATUS_TEXT[a.status] || a.status)}</div>
    ${a.why ? `<div class="small muted">${esc(a.why)}</div>` : ''}
    <details><summary>Show exact inputs</summary><pre>${esc(JSON.stringify({ action: a.tool, ...a.args }, null, 2))}</pre></details>
    ${a.error ? `<div class="callout bad small">${esc(a.error)}</div>` : ''}${res}
    ${a.status === 'awaiting_admin' ? '<div class="small muted">Sent to the administrators. Any of them can run or reject it from Approvals; you will see the outcome here.</div>' : ''}
    ${done ? '' : `<div class="row" style="margin-top:8px"><button class="btn sm primary" data-approve="${a.id}">${a.needs_role === 'admin' && !isAdmin ? 'Send to administrators' : 'Approve &amp; run'}</button><button class="btn sm" data-reject="${a.id}">Reject</button>${a.needs_role === 'admin' ? '<span class="small muted">administrator only</span>' : ''}</div>`}
  </div>${a.status === 'executed' && credential ? credentialCard(credential) : ''}`;
};

const messageHtml = (m) => {
  if (m.role === 'user') {
    return `<div class="amsg user"><div class="abubble">${(m.tags || []).map((t) => `<span class="tagchip static">@${esc(t.label || t.type)}</span>`).join(' ')}${m.tags?.length ? '<br>' : ''}${esc(m.content).replace(/\n/g, '<br>')}</div></div>`;
  }
  const looked = (m.meta?.looked_up || []).map((l) => `<span class="lookchip">${icon('eye', 11)} ${esc(l.tool)}</span>`).join(' ');
  return `<div class="amsg assistant"><div class="aav">${icon('spark', 14)}</div><div class="abubble">
    ${looked ? `<div class="small muted" style="margin-bottom:4px">looked up ${looked}</div>` : ''}<div class="text">${md(m.content)}</div>
    ${(m.actions || []).map(actionCard).join('')}${(m.meta?.credential_requests || []).map(credentialCard).join('')}</div></div>`;
};

function welcome(compact) {
  const page = getPage();
  return `<div class="awelcome"><h3>Ask me anything about H.A.D.E.S</h3>
    <p class="muted small">I can explain pages and options, look into your integrations and data sources, troubleshoot failures, and perform actions — <b>every change waits for your approval</b>. Type <code>@</code> to tag an integration, source, workflow or credential so I can read it directly.</p>
    <div class="suggest one">${SUGGESTIONS.map((s) => `<button data-suggest="${esc(s)}">${esc(s)}</button>`).join('')}</div>
    ${compact ? '' : ''}<p class="small muted" style="margin-top:10px">You are on <b>${esc(page)}</b>; I know what this page does.</p></div>`;
}

export function mountAssistant(root, { compact = false } = {}) {
  root.innerHTML = `<div class="assistant ${compact ? 'compact' : ''}">
    <div class="a-banner" id="a-banner" hidden></div>
    <div class="a-log" id="a-log" aria-live="polite"></div>
    <div class="a-composer"><div class="a-tags" id="a-tags"></div><div class="mention" id="a-mention" hidden></div>
      <form class="composer-box" id="a-form"><textarea id="a-text" rows="2" placeholder="Ask, or type @ to tag an integration or source…" aria-label="Message the assistant"></textarea>
        <div class="composer-foot"><button type="button" class="btn sm ghost" id="a-tag">@ Tag</button><button type="button" class="btn sm ghost" id="a-new">${icon('plus', 14)} New chat</button><span class="grow"></span>
        <button class="btn primary icon" id="a-send" aria-label="Send">${icon('send')}</button></div></form>
      <div class="composer-hint">Enter to send · Shift+Enter for a new line · never paste secrets — use Credentials</div></div></div>`;
  const log = $('#a-log', root), text = $('#a-text', root), tagsEl = $('#a-tags', root), pop = $('#a-mention', root);
  let mentionStart = -1, options = [], sel = 0;

  const paint = async () => {
    const st = await loadStatus();
    const b = $('#a-banner', root);
    b.hidden = st.configured;
    if (!st.configured) b.innerHTML = `<div class="callout warn small"><b>No AI provider is connected.</b> The assistant runs on a real model only. An owner can add OpenAI-compatible, Anthropic or Gemini credentials in <a class="link" href="#/providers">AI Providers</a>.</div>`;
    log.innerHTML = (S.messages.length ? S.messages.map(messageHtml).join('') : welcome(compact))
      + (S.busy ? `<div class="amsg assistant"><div class="aav">${icon('spark', 14)}</div><div class="abubble"><span class="spinner"></span> Thinking…</div></div>` : '');
    log.scrollTop = log.scrollHeight;
    tagsEl.innerHTML = S.tags.map((t, i) => `<span class="tagchip">@${esc(t.label)} <button type="button" data-untag="${i}" aria-label="Remove tag">×</button></span>`).join('');
    $('#a-send', root).disabled = S.busy;
  };
  const unsub = () => listeners.delete(paint);
  listeners.add(paint);
  paint();

  async function send(content) {
    content = content.trim();
    if (!content || S.busy) return;
    const tags = S.tags.map((t) => ({ type: t.type, id: t.id, label: t.label }));
    S.messages.push({ role: 'user', content, tags, actions: [] });
    S.busy = true; S.tags = []; text.value = ''; notify();
    try {
      const tid = await ensureThread();
      const r = await api(`/assistant/threads/${tid}/messages`, { method: 'POST', body: { content, tags, page: getPage() } });
      S.messages.push(r.message);
    } catch (e) {
      S.messages.push({ role: 'assistant', content: `⚠️ ${e.message}`, actions: [], meta: {} });
    }
    S.busy = false; notify();
  }

  async function decide(id, approve) {
    if (S.busy) return;
    S.busy = true; notify();
    try {
      const r = await api(`/assistant/actions/${id}/${approve ? 'approve' : 'reject'}`, { method: 'POST', body: { page: getPage() } });
      for (const m of S.messages) m.actions = (m.actions || []).map((a) => (a.id === id ? r.action : a));
      if (r.message) S.messages.push(r.message);
      if (r.action.status === 'executed') toast('Action completed', 'ok'); else if (r.action.status === 'awaiting_admin') toast('Sent to the administrators for approval', 'ok');
      else if (r.action.status === 'failed') toast(r.action.error, 'bad');
    } catch (e) { toast(e.message, 'bad'); }
    S.busy = false; notify();
  }

  async function connect({ connect: name, auth, header }) {
    const { connectModal } = await import('./pages/credentials.js');
    if (!await connectModal({ name, auth_type: auth, header })) return;
    if (S.threadId) await openThread(S.threadId);   // re-read so every card for this credential shows it as connected
  }

  // @-mentions
  async function mention(query) {
    options = (await api(`/assistant/taggables?q=${encodeURIComponent(query)}`)).filter((o) => !S.tags.some((t) => t.type === o.type && t.id === o.id));
    sel = 0;
    pop.hidden = !options.length;
    pop.innerHTML = options.map((o, i) => `<button type="button" class="${i === sel ? 'on' : ''}" data-pick="${i}"><b>${esc(o.label)}</b><small>${esc(o.hint)}</small></button>`).join('');
  }
  const closeMention = () => { pop.hidden = true; mentionStart = -1; };
  function pick(i) {
    const o = options[i];
    if (!o) return;
    if (mentionStart >= 0) text.value = text.value.slice(0, mentionStart) + text.value.slice(text.selectionStart);
    addTag({ type: o.type, id: o.id, label: o.label });
    closeMention(); text.focus();
  }
  text.addEventListener('input', () => {
    const upto = text.value.slice(0, text.selectionStart);
    const m = upto.match(/(?:^|\s)@([^\s@]{0,30})$/);
    if (m) { mentionStart = upto.length - m[1].length - 1; mention(m[1]); } else closeMention();
  });
  text.addEventListener('keydown', (e) => {
    if (!pop.hidden && options.length) {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); sel = (sel + (e.key === 'ArrowDown' ? 1 : options.length - 1)) % options.length; pop.querySelectorAll('button').forEach((b, i) => b.classList.toggle('on', i === sel)); return; }
      if (e.key === 'Enter' || e.key === 'Tab') { e.preventDefault(); pick(sel); return; }
      if (e.key === 'Escape') { closeMention(); return; }
    }
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(text.value); }
  });
  root.addEventListener('click', (e) => {
    const t = e.target.closest('[data-approve],[data-reject],[data-connect],[data-suggest],[data-untag],[data-pick],#a-tag,#a-new');
    if (!t) return;
    if (t.dataset.connect) connect(t.dataset);
    else if (t.dataset.approve) decide(t.dataset.approve, true);
    else if (t.dataset.reject) decide(t.dataset.reject, false);
    else if (t.dataset.suggest) send(t.dataset.suggest);
    else if (t.dataset.untag) { S.tags.splice(+t.dataset.untag, 1); notify(); }
    else if (t.dataset.pick) pick(+t.dataset.pick);
    else if (t.id === 'a-tag') { text.focus(); mentionStart = text.selectionStart; mention(''); }
    else if (t.id === 'a-new') newThread();
  });
  $('#a-form', root).addEventListener('submit', (e) => { e.preventDefault(); send(text.value); });
  root._send = send;
  return () => { unsub(); };
}

// ---- floating drawer on every page ----
let drawerRoot, drawerCleanup;
export function installDrawer() {
  if (document.getElementById('ask-ai')) return;
  const btn = document.createElement('button');
  btn.id = 'ask-ai'; btn.className = 'ask-fab'; btn.innerHTML = `${icon('spark', 20)}`; btn.title = 'Ask the AI assistant'; btn.setAttribute('aria-label', 'Open the AI assistant');
  const drawer = document.createElement('aside');
  drawer.id = 'assistant-drawer'; drawer.className = 'drawer right'; drawer.hidden = true;
  drawer.innerHTML = `<div class="drawer-head"><b>${icon('spark', 16)} AI assistant</b><span class="grow"></span><a class="btn sm ghost" href="#/studio" id="drawer-full">Open in AI Studio</a><button class="btn ghost icon" id="drawer-close" aria-label="Close">${icon('x')}</button></div><div id="drawer-body"></div>`;
  document.body.append(btn, drawer);
  drawerRoot = $('#drawer-body', drawer);
  const open = async (opts = {}) => {
    document.getElementById('help-drawer')?.setAttribute('hidden', '');
    setDrawer('drawer-help', false);
    drawer.hidden = false; btn.hidden = true; setDrawer('drawer-ai', true);
    if (!drawerCleanup) drawerCleanup = mountAssistant(drawerRoot, { compact: true });
    if (S.threadId && !S.messages.length) await openThread(S.threadId);
    (opts.tags || []).forEach(addTag);
    if (opts.text) { const t = $('#a-text', drawerRoot); t.value = opts.text; t.focus(); }
  };
  const close = () => { drawer.hidden = true; btn.hidden = false; setDrawer('drawer-ai', false); };
  btn.addEventListener('click', () => open());
  $('#drawer-close', drawer).addEventListener('click', close);
  $('#drawer-full', drawer).addEventListener('click', close);
  window.addEventListener('hedes:ask', (e) => open(e.detail || {}));
  window.addEventListener('hashchange', () => {
    if (getPage() === 'studio') { drawer.hidden = true; setDrawer('drawer-ai', false); btn.hidden = true; } else if (drawer.hidden) btn.hidden = false;
  });
  if (getPage() === 'studio') btn.hidden = true;
}

export const askAbout = (tag, text = '') => window.dispatchEvent(new CustomEvent('hedes:ask', { detail: { tags: [tag], text } }));

// ---- full page (AI Studio > Assistant) ----
export function modeBar(active) {
  return `<div class="mode-bar" id="mode-bar"><div class="tabs"><a class="tabbtn ${active === 'assistant' ? 'on' : ''}" href="#/studio">${icon('spark', 14)} Assistant</a><a class="tabbtn ${active === 'build' ? 'on' : ''}" href="#/studio/new">${icon('flow', 14)} Build an integration</a></div>
    <span class="grow"></span>${active === 'assistant' ? `<button class="btn sm ghost" id="can-do">${icon('shield', 14)} What can it do?</button>` : '<span class="muted small">Guided pipeline: describe → generate → test → submit</span>'}</div>`;
}

export async function renderAssistantPage({ el }) {
  const st = await loadStatus();
  S.threads = await api('/assistant/threads').catch(() => []);
  if (S.threadId && !S.messages.length) await openThread(S.threadId);
  el.innerHTML = `${modeBar('assistant')}<div class="studio astudio"><aside class="history"><div class="history-head"><b>Conversations</b><button class="btn sm ghost" id="t-new">${icon('plus', 15)} New</button></div>
      <div id="t-list">${S.threads.length ? S.threads.map((t) => `<a href="#/studio" data-thread="${t.id}" class="${t.id === S.threadId ? 'active' : ''}"><b>${esc(t.title)}</b><small>${ago(t.updated_at)}</small></a>`).join('') : '<p class="muted small" style="padding:0 16px">No conversations yet.</p>'}</div></aside>
    <section class="chat" id="a-host"></section></div>`;
  const cleanup = mountAssistant($('#a-host', el), { compact: false });
  const un = () => { /* thread list refreshes on navigation */ };
  el.addEventListener('click', async (e) => {
    if (e.target.closest('#can-do')) {
      return modal({ title: 'What the assistant can do', wide: true, actions: [{ label: 'Close', value: null }], body: `<ol class="small guide-list"><li><b>Ask</b> anything: pages, options, errors, how to connect a service.</li><li><b>Tag</b> with <code>@</code> so it reads that integration, source, workflow or credential (never secrets).</li><li>It <b>looks things up</b> on its own (read-only).</li><li>Anything that changes data or calls an external API becomes an <b>approval card</b> with the exact inputs. It runs only when you approve, with your permissions, and is audited.</li></ol>
          ${(st.tools || []).map((t) => `<div class="op-row"><span class="grow"><b class="mono">${esc(t.name)}</b><div class="muted small">${esc(t.description)}</div></span>${t.needs_approval ? badge('pending', 'approval') : badge('ok', 'auto')}</div>`).join('')}
          <p class="muted small" style="margin-top:10px">It can never approve or publish integrations, reveal or rotate secrets, or change policy.</p>` });
    }
    const t = e.target.closest('[data-thread], #t-new');
    if (!t) return;
    e.preventDefault();
    if (t.id === 't-new') newThread(); else await openThread(t.dataset.thread);
    el.querySelectorAll('[data-thread]').forEach((a) => a.classList.toggle('active', a.dataset.thread === S.threadId));
  });
  return () => { cleanup(); un(); };
}
