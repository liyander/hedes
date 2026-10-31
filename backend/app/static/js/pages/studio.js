import { $, api, esc, icon, badge, md, json, ago, toast, readFile, riskBadge, modal, method } from '../lib.js';
import { connectModal } from './credentials.js';
import { state } from '../app.js';
import { modeBar, renderAssistantPage } from '../assistant.js';

const STAGE_LABEL = {
  COLLECT_REQUIREMENTS: 'Collect requirements', DISCOVER_API: 'Discover API', DESIGN_INTEGRATION: 'Design integration',
  GENERATE_CONFIGURATION: 'Generate configuration', VALIDATE_CONFIGURATION: 'Validate configuration', RESEARCH_SETUP: 'Research setup requirements',
  SECURITY_EVALUATION: 'Security evaluation',
  RUN_SAFE_TESTS: 'Run safe tests', AWAIT_ADMIN_APPROVAL: 'Admin approval', PUBLISHED: 'Published',
};
const SUGGESTIONS = [
  'I want to connect another task-management service and create tasks automatically.',
  'Whenever a GitHub issue is labeled security, create a Jira ticket and notify our Slack channel #security.',
  'Post a message to our Slack channel #deploys when I ask.',
  'Connect GitHub so I can list open issues in acme/platform repo and comment on them.',
];

let pollTimer = null;
const tab = {};

export async function render({ el, params }) {
  if (!params[0]) return renderAssistantPage({ el });
  const sessions = await api('/ai/sessions');
  let sid = params[0];
  if (sid === 'new') {
    const prompt = sessionStorage.getItem('integra-prompt');
    sessionStorage.removeItem('integra-prompt');
    if (prompt) {
      const s = await api('/ai/sessions', { method: 'POST', body: { message: prompt } });
      history.replaceState(null, '', `#/studio/${s.id}`);
      sid = s.id;
    } else sid = null;
  }
  el.innerHTML = `${modeBar('build')}<div class="studio" id="studio">
    <aside class="history"><div class="history-head"><b>Sessions</b><a class="btn sm ghost" href="#/studio/new">${icon('plus', 15)} New</a></div>
      ${sessions.length ? sessions.map((s) => `<a href="#/studio/${s.id}" class="${s.id === sid ? 'active' : ''}"><b>${esc(s.title)}</b><small>${ago(s.updated_at)} · ${s.messages} messages · ${esc(s.status)}</small></a>`).join('')
        : '<p class="muted small" style="padding:0 16px">No sessions yet.</p>'}</aside>
    <section class="chat"><div class="chat-log" id="log"></div>
      <div class="composer"><form class="composer-box" id="composer">
        <textarea id="msg" rows="2" placeholder="Describe what to connect, answer a question, or ask for a change…" aria-label="Message"></textarea>
        <div class="composer-foot">
          <label class="btn sm ghost" style="margin:0">${icon('upload', 15)} Upload OpenAPI<input type="file" id="file" accept=".json,.yaml,.yml" hidden></label>
          ${state.demo ? `<button type="button" class="btn sm ghost" id="sample">${icon('doc', 15)} Use sample TaskFlow API (demo)</button>` : ''}
          <span class="grow"></span>
          <button class="btn primary icon" id="send" aria-label="Send">${icon('send')}</button>
        </div></form>
        <div class="composer-hint">Enter to send · Shift+Enter for a new line · never paste API keys here — credentials go through the secure Connect form</div>
      </div></section>
    <section class="workpane" id="work"></section></div>`;

  let session = sid ? await api(`/ai/sessions/${sid}`) : null;
  const log = $('#log', el), work = $('#work', el);

  function paint() {
    if (!session) {
      log.innerHTML = `<div class="welcome"><div class="wico">${icon('spark', 26)}</div><h2>What do you want to connect?</h2>
        <p>Describe the outcome in plain language. I'll identify the services and operations, ask for anything missing, and produce a tested configuration for admin approval.</p>
        <div class="suggest">${SUGGESTIONS.map((s) => `<button data-suggest="${esc(s)}">${esc(s)}</button>`).join('')}</div></div>`;
      work.innerHTML = `<div class="card card-pad"><h2 style="margin-bottom:8px">How AI Studio works</h2>
        <ol class="small" style="padding-left:18px;margin:0;line-height:1.9">
          <li>The <b>Requirement agent</b> turns your request into services, operations and triggers.</li>
          <li>The <b>Discovery agent</b> reads uploaded OpenAPI docs (treated as untrusted data).</li>
          <li>The <b>Configuration agent</b> generates declarative configs — no code.</li>
          <li>Deterministic <b>validation, security policy and mock tests</b> run on the exact config.</li>
          <li>It is submitted for <b>admin approval</b>. The AI can never approve its own work.</li></ol></div>`;
      return;
    }
    log.innerHTML = session.messages.map((m) => m.role === 'system'
      ? `<div class="msg system"><div class="text">${icon('doc', 13)} ${esc(m.content)}</div></div>`
      : `<div class="msg ${m.role}"><div class="av">${m.role === 'user' ? 'You' : icon('spark', 15)}</div><div class="bubble">
          <div class="meta">${m.role === 'user' ? 'You' : `H.A.D.E.S${m.meta?.model ? ` · <span class="mono">${esc(m.meta.model)}</span>` : ''}`}</div>
          <div class="text">${md(m.content)}</div></div></div>`).join('') +
      (session.status === 'running' ? `<div class="msg assistant"><div class="av">${icon('spark', 15)}</div><div class="bubble"><div class="text"><span class="spinner"></span> Working on ${esc(STAGE_LABEL[session.stage] || session.stage)}…</div></div></div>` : '');
    log.scrollTop = log.scrollHeight;
    paintWork();
  }

  function paintWork() {
    const r = session.requirements || {};
    const st = session.stages || {};
    const running = session.status === 'running';
    const generated = session.integrations.length > 0;
    const failedStage = session.stage_order.find((k) => st[k]?.status === 'failed');
    work.innerHTML = `<div class="stack">
      <div class="card"><div class="card-head"><h2>Requirements</h2><span class="spacer"></span>${r.ready ? badge('ready', 'ready to generate') : badge('collecting', 'collecting')}</div>
        <div class="card-body">
          <p style="margin:0 0 10px">${esc(r.summary || 'Tell me what you want to connect.')}</p>
          ${(r.services || []).length ? `<div class="kv small"><dt>Services</dt><dd>${r.services.map((s) => `${esc(s.name)} <span class="muted">(${esc((s.source || '').replace('_', ' '))})</span>`).join(', ')}</dd>
            <dt>Operations</dt><dd>${(r.operations || []).map((o) => `<code>${esc(o.service)}.${esc(o.operation_id || o.action)}</code>`).join(' ') || '—'}</dd>
            ${(r.triggers || []).length ? `<dt>Trigger</dt><dd>${esc(r.triggers[0].event)}${r.triggers[0].condition ? ` · <code>${esc(r.triggers[0].condition)}</code>` : ''}</dd>` : ''}
            ${Object.keys(r.details || {}).length ? `<dt>Details</dt><dd>${Object.entries(r.details).map(([k, v]) => `${esc(k)}: <code>${esc(v)}</code>`).join(' ')}</dd>` : ''}</div>` : ''}
          ${(r.missing || []).length ? `<div class="callout warn small" style="margin-top:12px"><b>Open questions</b><ul style="margin:4px 0 0;padding-left:18px">${(r.missing_help || r.missing.map((q) => ({ question: q }))).map((q) =>
            `<li style="margin-bottom:6px">${esc(q.question)}${q.how ? `<div class="muted">${icon('search', 12)} <b>How to find it:</b> ${esc(q.how)}</div>` : ''}</li>`).join('')}</ul></div>` : ''}
          ${(session.documents || []).length ? `<div class="small muted" style="margin-top:10px">${icon('doc', 13)} ${session.documents.map((d) => `${esc(d.title)} (${d.operations} ops)`).join(', ')}</div>` : ''}
          <div class="row" style="margin-top:14px">
            <button class="btn primary" id="gen" ${!r.ready || running ? 'disabled' : ''}>${running ? '<span class="spinner"></span> Generating…' : `${icon('spark', 16)} ${generated ? 'Regenerate as new version' : 'Generate integration'}`}</button>
            ${failedStage && !running ? `<button class="btn" data-retry="${failedStage}">${icon('refresh', 15)} Retry from ${esc(STAGE_LABEL[failedStage])}</button>` : ''}
          </div></div></div>
      ${Object.keys(st).length ? `<div class="card card-pad"><h2 style="margin-bottom:10px">Pipeline</h2><ul class="stages">${session.stage_order.map((k) => {
        const s = st[k] || {};
        return `<li class="${esc(s.status || '')}"><span class="sdot">${s.status === 'done' ? '✓' : s.status === 'failed' ? '!' : ''}</span><div><b>${STAGE_LABEL[k]}</b>${s.model ? ` <span class="muted small mono">${esc(s.model)}</span>` : ''}<small>${esc(s.detail || (s.status ? s.status : 'pending'))}</small></div></li>`;
      }).join('')}</ul></div>` : ''}
      ${session.integrations.map(integrationCard).join('')}
      ${(session.sources || []).length ? `<div class="card card-pad"><h2 style="margin-bottom:8px">Sources referred</h2><ul class="small" style="margin:0;padding-left:18px">
        ${session.sources.map((x) => `<li>${esc(x.label)}${/^https?:\/\//i.test(x.url || '') ? ` — <a class="link" href="${esc(x.url)}" target="_blank" rel="noopener noreferrer">${esc(x.url)}</a>` : ''}</li>`).join('')}</ul></div>` : ''}
      ${session.workflow ? `<div class="card card-pad"><div class="row between"><div><h2>Workflow draft</h2><div class="muted small">${esc(session.workflow.name)}</div></div><a class="btn" href="#/workflows/${session.workflow.id}">${icon('flow', 15)} Open editor</a></div></div>` : ''}
    </div>`;
  }

  function integrationCard(i) {
    const t = tab[i.integration_id] || 'overview';
    const sec = i.security || {};
    const ops = i.config.integration.operations;
    const needsCred = i.auth_type !== 'none' && !i.credential_connected;
    return `<div class="card"><div class="card-head"><div><h2>${esc(i.name)} <span class="muted mono small">${esc(i.version)}</span></h2>
        <div class="small muted mono">${esc(i.config.integration.base_url)}</div></div><span class="spacer"></span>${badge(i.version_status)}</div>
      <div class="card-body">
        <div class="tabs" style="margin-bottom:12px">${['overview', 'config', 'security', 'tests'].map((k) => `<button class="${k === t ? 'on' : ''}" data-tab="${k}" data-iid="${i.integration_id}">${k[0].toUpperCase() + k.slice(1)}</button>`).join('')}</div>
        ${t === 'overview' ? `${ops.map((o) => `<div class="op-row">${method(o.method)}<div class="grow"><div class="path">${esc(o.path)}</div><div class="small muted">${esc(o.id)} — ${esc(o.description || '')}</div></div></div>`).join('')}
          <div class="row small" style="margin-top:12px;gap:14px"><span>Risk ${riskBadge(sec.level, sec.score)}</span><span>Tests ${i.tests ? badge(i.tests.failed ? 'failed' : 'passed', `${i.tests.passed}/${i.tests.total}`) : '—'}</span><span>Auth <code>${esc(i.auth_type)}</code></span></div>
          ${needsCred ? `<div class="callout info small" style="margin-top:12px"><div class="row between"><span>${icon('key', 14)} Needs a <b>${esc(i.auth_type)}</b> credential named <code>${esc(i.credential_ref)}</code>. Stored encrypted; never shown to the AI.${i.credential_help ? `<br><b>Where to get it:</b> ${esc(i.credential_help)}` : ''}</span>
            <button class="btn sm" data-connect="${esc(i.credential_ref)}" data-auth="${esc(i.auth_type)}" data-header="${esc(i.config.integration.authentication.header_name || '')}">Connect</button></div></div>` : ''}
          ${i.setup ? setupHtml(i.setup) : ''}
          <div class="row" style="margin-top:12px">${i.approval_id ? `<a class="btn sm" href="#/approvals/${i.approval_id}">${icon('check', 14)} View review</a>` : ''}<a class="btn sm" href="#/integrations/${i.integration_id}">Open integration</a>${i.version_status === 'published' ? `<a class="btn sm primary" href="#/testlab/${i.integration_id}">${icon('play', 14)} Run it</a>` : ''}</div>`
        : t === 'config' ? json(i.config)
        : t === 'security' ? `${sec.ai_summary ? `<p class="small" style="margin-top:0">${esc(sec.ai_summary)}</p>` : ''}${(sec.findings || []).map((f) => `<div class="finding">${badge(f.severity)}<div><b>${esc(f.title)}</b>${f.blocking ? ' <span class="badge critical">blocking</span>' : ''}<div class="small muted">${esc(f.detail)}</div></div></div>`).join('') || '<p class="muted">No findings.</p>'}`
        : i.tests ? i.tests.cases.map((c) => `<div class="finding">${badge(c.passed ? 'passed' : 'failed')}<div><b class="small">${esc(c.name)}</b><div class="small muted">expected ${esc(c.expect)} · got ${esc(c.actual)}</div></div></div>`).join('') : '<p class="muted">No tests yet.</p>'}
      </div></div>`;
  }

  async function reload() {
    session = await api(`/ai/sessions/${session.id}`);
    paint();
    schedule();
  }
  function schedule() {
    clearTimeout(pollTimer);
    if (session && session.status === 'running') pollTimer = setTimeout(reload, 1200);
  }

  async function send(text) {
    if (!text.trim()) return;
    $('#send', el).disabled = true;
    try {
      if (!session) {
        session = await api('/ai/sessions', { method: 'POST', body: { message: text } });
        history.replaceState(null, '', `#/studio/${session.id}`);
      } else {
        session.messages.push({ role: 'user', content: text, meta: {} });
        paint();
        session = await api(`/ai/sessions/${session.id}/messages`, { method: 'POST', body: { content: text } });
      }
      $('#msg', el).value = '';
      paint();
    } catch (err) { toast(err.message, 'bad'); }
    $('#send', el).disabled = false;
  }

  async function upload(name, content) {
    if (!session) session = await api('/ai/sessions', { method: 'POST', body: {} });
    history.replaceState(null, '', `#/studio/${session.id}`);
    try {
      session = await api(`/ai/sessions/${session.id}/documents`, { method: 'POST', body: { name, content } });
      paint();
      toast(`Imported ${name}`, 'ok');
    } catch (err) { toast(err.message, 'bad'); }
  }

  el.addEventListener('submit', (e) => { if (e.target.id === 'composer') { e.preventDefault(); send($('#msg', el).value); } });
  $('#msg', el).addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(e.target.value); } });
  $('#file', el).addEventListener('change', async (e) => {
    const f = e.target.files[0];
    if (f) await upload(f.name, await readFile(f));
    e.target.value = '';
  });
  $('#sample', el)?.addEventListener('click', async () => {
    const text = await (await fetch('/mock/taskflow/openapi.json')).text();
    await upload('taskflow-openapi.json', text);
  });
  el.addEventListener('click', async (e) => {
    const t = e.target.closest('[data-suggest], #gen, [data-retry], [data-tab], [data-connect]');
    if (!t) return;
    if (t.dataset.suggest) return send(t.dataset.suggest);
    if (t.dataset.tab) { tab[t.dataset.iid] = t.dataset.tab; return paintWork(); }
    if (t.dataset.connect) {
      if (await connectModal({ name: t.dataset.connect, auth_type: t.dataset.auth, header: t.dataset.header })) reload();
      return;
    }
    t.disabled = true;
    try {
      // Nothing is written to the database until the user confirms exactly what will be created.
      const plan = await api(`/ai/sessions/${session.id}/plan`);
      const ok = await modal({ title: 'Create these records?', wide: true, body: planHtml(plan),
        actions: [{ label: 'Cancel', value: false }, { label: 'Confirm and create', cls: 'primary', value: true }] });
      if (!ok) { t.disabled = false; return; }
      session = await api(`/ai/sessions/${session.id}/generate`, { method: 'POST', body: { from_stage: t.dataset.retry || null, confirm: plan.fingerprint } });
      paint();
      schedule();
    } catch (err) { toast(err.message, 'bad'); t.disabled = false; }
  });
  paint();
  schedule();
  return () => clearTimeout(pollTimer);
}


// The confirmation shown before generating: every database record the build will create.
function planHtml(plan) {
  return `<p style="margin-top:0">Generating writes these records to the database. Nothing runs yet.</p>
    ${plan.items.map((i) => `<div class="card card-pad" style="margin-bottom:10px"><div class="row between"><b>${esc(i.service)}</b>${badge(i.record === 'new integration' ? 'info' : 'medium', i.record)}</div>
      <div class="small muted" style="margin:4px 0">Tables: ${i.tables.map((t) => `<code>${esc(t)}</code>`).join(' ')}</div>
      <div class="small">Operations: ${i.operations.map((o) => `<code>${esc(o)}</code>`).join(' ') || '—'}</div>
      ${i.auth_type && i.auth_type !== 'none' ? `<div class="small" style="margin-top:4px">Credential to connect afterwards: <code>${esc(i.credential_ref || '')}</code> (${esc(i.auth_type)}). ${esc(i.credential_help)}</div>` : ''}</div>`).join('')}
    <ul class="small muted" style="margin:0;padding-left:18px">${plan.notes.map((n) => `<li>${esc(n)}</li>`).join('')}</ul>`;
}

// Setup checklist researched from the vendor's documentation: what to do in the vendor's product before it works.
export function setupHtml(g) {
  if (!g || !(g.permissions?.length || g.steps?.length || g.common_errors?.length)) return '';
  return `<div class="callout small" style="margin-top:12px"><b>${icon('check', 13)} Setup checklist</b> <span class="muted">researched from the vendor's documentation${g.model ? ` · ${esc(g.model)}` : ''}</span>
    ${g.credential ? `<div style="margin-top:6px"><b>Credential:</b> ${esc(g.credential)}</div>` : ''}
    ${g.permissions?.length ? `<div style="margin-top:6px"><b>Required scopes / permissions:</b> ${g.permissions.map((x) => `<code>${esc(x)}</code>`).join(' ')}</div>` : ''}
    ${[...(g.steps || []), ...(g.prerequisites || [])].length ? `<ol style="margin:6px 0 0;padding-left:18px">${[...(g.steps || []), ...(g.prerequisites || [])].map((x) => `<li>${esc(x)}</li>`).join('')}</ol>` : ''}
    ${g.common_errors?.length ? `<details style="margin-top:6px"><summary style="cursor:pointer"><b>Common errors and fixes</b></summary><ul style="margin:4px 0 0;padding-left:18px">
      ${g.common_errors.map((e) => `<li><code>${esc(e.code)}</code> - ${esc(e.meaning)} <b>Fix:</b> ${esc(e.fix)}</li>`).join('')}</ul></details>` : ''}
    ${g.sources?.length ? `<div class="muted" style="margin-top:6px">Sources: ${g.sources.map((u) => `<a class="link" href="${esc(u)}" target="_blank" rel="noopener noreferrer">${esc(u)}</a>`).join(', ')}</div>` : ''}
    ${g.unread?.length ? `<div class="muted" style="margin-top:4px">Could not read: ${g.unread.map(esc).join('; ')}</div>` : ''}</div>`;
}
