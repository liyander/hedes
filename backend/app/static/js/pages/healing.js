import { $, api, esc, icon, badge, agoCell, pageHead, emptyState, toast, fmtTime } from '../lib.js';
import { go, isAdmin } from '../app.js';
import { askAbout } from '../assistant.js';

export const CASE_STATUS = { investigating: 'running', resolved: 'ok', awaiting_approval: 'pending', needs_admin: 'failed' };
export const CASE_LABEL = { investigating: 'Investigating', resolved: 'Fixed', awaiting_approval: 'Fix awaiting approval', needs_admin: 'Needs an admin' };
const STAGE = {
  opened: ['alert', 'Failure detected'], recurred: ['refresh', 'Happened again'], started: ['play', 'Investigation started'],
  evidence: ['list', 'Evidence collected'], attempt: ['wrench', 'Fix attempted'], diagnosis: ['doc', 'Root cause'],
  analysis: ['spark', 'AI analysis'], outcome: ['check', 'Outcome'], notified: ['bell', 'Administrators notified'],
  fix_decision: ['shield', 'Fix decision'], error: ['alert', 'Investigation error'],
  auto_applied: ['check', 'Fix applied automatically'], auto_skip: ['shield', 'Kept for approval'], verified: ['eye', 'Verified after the fix'],
  rolled_back: ['refresh', 'Rolled back'],
};
const dur = (s) => (s == null ? '—' : s < 60 ? `${s}s` : s < 3600 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1)} h`);

export async function render(ctx) {
  if (ctx.params[0]) return renderCase(ctx);
  const { el } = ctx;
  let filter = '', q = '', timer;
  const paint = async () => {
    const qs = new URLSearchParams({ limit: '200' });
    if (filter) qs.set('status', filter);
    if (q) qs.set('q', q);
    const h = await api(`/healing/cases?${qs}`);
    const s = h.summary;
    $('#hl-stats', el).innerHTML = `
      <div class="card stat"><div class="ico"></div><div><b>${s.total}</b><span>Sessions</span></div></div>
      <div class="card stat good"><div class="ico"></div><div><b>${s.resolved}${s.fixed_rate != null ? `<small style="font-size:13px;font-weight:500"> · ${s.fixed_rate}%</small>` : ''}</b><span>Fixed</span></div></div>
      <div class="card stat ${s.awaiting_approval ? 'attn' : ''}"><div class="ico"></div><div><b>${s.awaiting_approval}</b><span>Fix awaiting approval</span></div></div>
      <div class="card stat ${s.needs_admin ? 'alert' : ''}"><div class="ico"></div><div><b>${s.needs_admin}</b><span>Need an admin</span></div></div>`;
    $('#hl-state', el).innerHTML = h.enabled ? (h.auto ? badge('ok', 'fixing automatically') : badge('ok', 'preparing fixes for approval')) : `${badge('disabled', 'automatic self-healing off')} ${isAdmin() ? '<a class="link small" href="#/settings">Turn on in Settings</a>' : ''}`;
    $('#hl-body', el).innerHTML = h.cases.length ? `<div class="table-scroll"><table><thead><tr><th>Session</th><th>Outcome</th><th>Root cause</th><th>Fixes tried</th><th>Runs</th><th>Seen</th><th>Duration</th><th>Started</th></tr></thead><tbody>
      ${h.cases.map((c) => `<tr class="clickable" data-go="#/healing/${c.id}"><td><b class="mono small">${esc(c.target)}</b><div class="muted small">${esc(c.error_class.replace(/_/g, ' '))}</div></td>
        <td>${badge(CASE_STATUS[c.status] || '', CASE_LABEL[c.status] || c.status)}</td>
        <td class="wrap small">${esc((c.root_cause || '—').slice(0, 160))}</td>
        <td class="small">${c.fixes_tried ? `${c.fixes_worked}/${c.fixes_tried} worked` : '<span class="muted">none</span>'}</td>
        <td>${c.runs}</td><td>${c.occurrences}×</td><td class="small">${dur(c.duration_s)}</td><td class="nowrap">${agoCell(c.created_at)}</td></tr>`).join('')}
      </tbody></table></div>`
      : emptyState('wrench', filter || q ? 'No sessions match' : 'No self-healing sessions yet', filter || q ? 'Change the filters.' : 'When an integration, data source or workflow fails, self-healing traces it here: what failed, the root cause, every fix it tried and the outcome. You can also start one from a failed execution in Monitoring.');
  };
  el.innerHTML = `<div class="page wide">
    ${pageHead('wrench', 'Self-healing', 'Every automatic investigation of a failure: what broke, the root cause, each fix that was tried, the outcome and a full timestamped log.', '<span id="hl-state"></span>')}
    <div class="stats" id="hl-stats" style="margin-bottom:18px"></div>
    <div class="card"><div class="card-head"><div class="tabs" id="hl-tabs">${[['', 'All'], ['needs_admin', 'Needs an admin'], ['awaiting_approval', 'Awaiting approval'], ['resolved', 'Fixed'], ['investigating', 'Running']]
      .map(([k, l]) => `<button data-f="${k}" class="${k === filter ? 'on' : ''}">${l}</button>`).join('')}</div><span class="spacer"></span>
      <input type="text" id="hl-q" placeholder="Search target, error, cause…" style="width:240px"></div><div id="hl-body"></div></div></div>`;
  await paint();
  el.addEventListener('click', (e) => {
    const f = e.target.closest('[data-f]');
    if (f) { filter = f.dataset.f; el.querySelectorAll('[data-f]').forEach((b) => b.classList.toggle('on', b === f)); paint(); return; }
    const r = e.target.closest('[data-go]');
    if (r) go(r.dataset.go);
  });
  $('#hl-q', el).addEventListener('input', (e) => { clearTimeout(timer); timer = setTimeout(() => { q = e.target.value.trim(); paint(); }, 250); });
  const poll = setInterval(() => paint().catch(() => {}), 20000);
  return () => { clearInterval(poll); clearTimeout(timer); };
}

function timeline(log) {
  if (!log.length) return '<p class="muted small" style="margin:0">This session predates the detailed log; see the attempts and outcome above.</p>';
  let run = null;
  return `<ol class="tl">${log.map((x) => {
    const [ic, label] = STAGE[x.stage] || ['dot', x.stage];
    const head = x.run !== run && x.run ? `<li class="tl-run">Investigation #${x.run}</li>` : '';
    run = x.run;
    return `${head}<li class="tl-item ${x.ok === true ? 'ok' : x.ok === false ? 'bad' : ''}"><span class="tl-dot">${icon(ic, 12)}</span>
      <div class="grow"><div class="row between"><b class="small">${esc(label)}</b><span class="muted small nowrap" title="${esc(fmtTime(x.at))}">${esc(new Date(x.at).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' }))}</span></div>
      <div class="small" style="overflow-wrap:anywhere">${esc(x.text)}</div></div></li>`;
  }).join('')}</ol>`;
}

let caseTimer;

async function renderCase({ el, params }) {
  const paint = async () => {
    const c = await api(`/healing/cases/${params[0]}`);
    const outcomeCls = { resolved: 'ok', awaiting_approval: 'warn', needs_admin: 'bad', investigating: 'info' }[c.status] || 'info';
    el.innerHTML = `<div class="page wide">
      <div class="crumb"><a href="#/healing">Self-healing</a> / ${esc(c.target)}</div>
      ${pageHead('wrench', c.target, `${esc(c.error_class.replace(/_/g, ' '))} · started ${esc(fmtTime(c.created_at))} · ${c.runs} investigation(s) · seen ${c.occurrences}×`,
        `<button class="btn" id="hl-ask">${icon('spark', 15)} Discuss with AI</button>
         ${c.approval_request_id && c.status === 'awaiting_approval' ? `<a class="btn primary" href="#/approvals/${c.approval_request_id}">Review the fix</a>` : ''}
         ${isAdmin() ? `<button class="btn" id="retry" ${c.status === 'investigating' ? 'disabled' : ''}>${icon('refresh', 15)} Investigate again</button>` : ''}`)}
      <div class="callout ${outcomeCls}" style="margin-bottom:16px"><div class="row between"><div><b>${esc(CASE_LABEL[c.status] || c.status)}</b>
        ${c.status === 'investigating' ? ' <span class="spinner"></span>' : ''}<div class="small">${esc(c.root_cause || 'Working out the root cause…')}</div></div>
        <div class="small muted nowrap" style="text-align:right">${c.resolved_at ? `fixed ${agoCell(c.resolved_at)}<br>` : ''}took ${dur(c.duration_s)}</div></div></div>
      <div class="split">
        <div class="stack">
          <div class="card"><div class="card-head"><h2>Log</h2><span class="muted small">every step, oldest first</span></div><div class="card-body">${timeline(c.log || [])}</div></div>
          <div class="card"><div class="card-head"><h2>Fixes tried</h2><span class="muted small">${c.fixes_worked}/${c.fixes_tried} worked in the latest investigation</span></div><div class="card-body">
            ${c.attempts.length ? c.attempts.map((a) => `<div class="finding">${badge(a.ok ? 'ok' : 'failed', a.ok ? 'worked' : 'did not fix it')}<div class="small"><b>${esc(a.action)}</b><div class="muted">${esc(a.result)}</div>${a.at ? `<div class="muted">${agoCell(a.at)}</div>` : ''}</div></div>`).join('')
              : '<p class="muted small" style="margin:0">Nothing could be tried automatically for this kind of problem.</p>'}</div></div>
        </div>
        <div class="stack">
          ${c.analysis && c.analysis !== c.root_cause ? `<div class="card card-pad"><h2 style="margin-bottom:6px">${icon('spark', 15)} AI analysis</h2><p class="small" style="margin:0">${esc(c.analysis)}</p><div class="muted small mono" style="margin-top:6px">${esc(c.model)}</div></div>` : ''}
          ${c.required.length && c.status !== 'resolved' ? `<div class="callout warn"><b>Required from an administrator</b><ul style="margin:6px 0 0;padding-left:18px">${c.required.map((r) => `<li class="small">${esc(r)}</li>`).join('')}</ul></div>` : ''}
          ${c.still_failing && c.status !== 'resolved' ? `<div class="callout bad small"><b>Still failing:</b> ${esc(c.still_failing)}</div>` : ''}
          ${c.fix ? `<div class="card card-pad"><h2 style="margin-bottom:6px">Prepared fix</h2><dl class="kv small"><dt>Version</dt><dd class="mono">${esc(c.fix.version)} ${badge(c.fix.status)}</dd>
            <dt>Decision</dt><dd>${c.fix.decision ? badge(c.fix.decision) : '—'}${c.fix.comment ? ` <span class="muted">${esc(c.fix.comment)}</span>` : ''}</dd>${c.fix.notes ? `<dt>Notes</dt><dd>${esc(c.fix.notes)}</dd>` : ''}</dl>
            ${c.integration ? `<a class="link small" href="#/integrations/${c.integration.id}/${c.fix.version_id}">Open this version</a>` : ''}</div>` : ''}
          <div class="card card-pad"><h2 style="margin-bottom:8px">Details</h2><dl class="kv small">
            ${c.integration ? `<dt>Integration</dt><dd><a class="link" href="#/integrations/${c.integration.id}">${esc(c.integration.name)}</a></dd>` : ''}
            ${c.endpoint.url ? `<dt>Endpoint</dt><dd class="mono">${esc(c.endpoint.method)} ${esc(c.endpoint.url)}${c.endpoint.http_status ? ` · HTTP ${c.endpoint.http_status}` : ''}</dd>` : ''}
            ${Object.entries(c.evidence).filter(([k]) => !['error', 'last_error'].includes(k)).map(([k, v]) => `<dt>${esc(k.replace(/_/g, ' '))}</dt><dd>${esc(typeof v === 'object' ? JSON.stringify(v) : v)}</dd>`).join('')}
            <dt>Notified</dt><dd>${c.notified_at ? agoCell(c.notified_at) : 'no'}</dd></dl></div>
          ${c.executions?.length ? `<div class="card"><div class="card-head"><h2>Related executions</h2></div><div class="quick-list">${c.executions.map((x) => `<a href="#/monitoring/execution/${x.id}">${badge(x.status)}<div class="grow"><div class="mono small">${esc(x.tool)}</div><div class="muted small">${x.http_status ? `HTTP ${x.http_status} · ` : ''}${agoCell(x.created_at)}</div></div></a>`).join('')}</div></div>` : ''}
          <details class="card card-pad"><summary class="small" style="cursor:pointer"><b>Report sent to administrators</b></summary><pre style="white-space:pre-wrap;margin-top:8px">${esc(c.report)}</pre></details>
        </div></div></div>`;
    $('#retry', el)?.addEventListener('click', async () => {
      try { await api(`/healing/cases/${c.id}/retry`, { method: 'POST' }); toast('Investigating again', 'ok'); paint(); } catch (err) { toast(err.message, 'bad'); }
    });
    $('#hl-ask', el).addEventListener('click', () => (c.integration
      ? askAbout({ type: 'integration', id: c.integration.id, label: c.integration.name }, `Explain self-healing session for ${c.target}: root cause "${c.root_cause}". What should I do?`)
      : window.dispatchEvent(new CustomEvent('hedes:ask', { detail: { text: `Explain this self-healing session for ${c.target}: ${c.root_cause}` } }))));
    clearTimeout(caseTimer);
    if (c.status === 'investigating') caseTimer = setTimeout(() => paint().catch(() => {}), 1500);
  };
  await paint();
  return () => clearTimeout(caseTimer);
}
