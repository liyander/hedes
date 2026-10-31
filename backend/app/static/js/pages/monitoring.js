import { api, esc, icon, badge, agoCell, pageHead, emptyState, fmtTime, toast } from '../lib.js';
import { go } from '../app.js';
import { executionCards, explainHandler } from './testlab.js';

let timer;

export async function render(ctx) {
  if (ctx.params[0] === 'execution') return renderExecution(ctx);
  if (ctx.params[0] === 'healing') return renderCase(ctx);
  const paint = async () => {
    const [m, fails, repairs, execs, heal] = await Promise.all([api('/monitoring/metrics'), api('/monitoring/failures'), api('/repairs'),
      api('/executions?limit=40'), api('/healing/cases')]);
    const max = Math.max(1, ...m.timeline.map((b) => b.ok + b.failed));
    const maxTool = Math.max(1, ...m.by_tool.map((t) => t.total));
    ctx.el.innerHTML = `<div class="page wide">
      ${pageHead('pulse', 'Monitoring', 'Execution health across every published tool. Failures are classified, drift is detected, and repairs are proposed for review.', `<span class="muted small">Refreshes every 15 s</span>`)}
      <div class="stats">
        <div class="card stat ${m.success_rate !== null && m.success_rate < 90 ? 'alert' : 'good'}"><div class="ico">${icon('pulse', 20)}</div><div><b>${m.success_rate ?? '—'}${m.success_rate !== null ? '%' : ''}</b><span>Success rate · ${m.executions} runs</span></div></div>
        <div class="card stat ${m.failed ? 'alert' : ''}"><div class="ico">${icon('alert', 20)}</div><div><b>${m.failed}</b><span>Failures (24 h)</span></div></div>
        <div class="card stat accent"><div class="ico">${icon('history', 20)}</div><div><b>${m.p50_ms}<small style="font-size:14px"> ms</small></b><span>p50 · p95 ${m.p95_ms} ms</span></div></div>
        <div class="card stat ${m.open_repairs ? 'attn' : ''}"><div class="ico">${icon('wrench', 20)}</div><div><b>${m.open_repairs}</b><span>Open repairs</span></div></div>
      </div>
      <div class="split" style="margin-top:18px">
        <div class="card"><div class="card-head"><h2>Executions per hour</h2><span class="spacer"></span><span class="small muted"><span class="dot" style="background:var(--series-1)"></span> ok &nbsp;<span class="dot" style="background:var(--series-bad)"></span> failed</span></div>
          <div class="card-body"><div class="bars">${m.timeline.map((b) => `<div class="b" title="${esc(fmtTime(b.t))}: ${b.ok} ok, ${b.failed} failed"><i class="ok" style="height:${(b.ok / max) * 100}%"></i><i class="bad" style="height:${(b.failed / max) * 100}%"></i></div>`).join('')}</div></div></div>
        <div class="card"><div class="card-head"><h2>Error classes</h2></div><div class="card-body">
          ${m.by_error.length ? m.by_error.map((x) => `<div class="hbar" title="${esc(x.meaning)}"><span class="mono small">${esc(x.error_class)}</span><span class="track"><i class="bad" style="width:${(x.count / m.failed) * 100}%"></i></span><span class="n">${x.count}</span></div>`).join('') : '<p class="muted small">No failures.</p>'}</div></div>
      </div>
      <div class="split" style="margin-top:18px">
        <div class="card"><div class="card-head"><h2>By tool</h2></div><div class="card-body">
          ${m.by_tool.length ? m.by_tool.map((t) => `<div class="hbar"><span class="mono small">${esc(t.tool)} <span class="muted">${t.success_rate}% · ${t.avg_ms} ms</span></span><span class="track"><i style="width:${(t.total / maxTool) * 100}%"></i></span><span class="n">${t.total}</span></div>`).join('') : '<p class="muted small">No executions yet.</p>'}</div></div>
        <div class="card"><div class="card-head"><h2>Repair proposals</h2></div><div class="card-body">
          ${repairs.length ? repairs.slice(0, 8).map((r) => `<div class="finding">${badge(r.status)}<div class="small"><b>${esc(r.integration)}</b> · ${esc(r.error_class)}<div class="muted">${esc(r.diagnosis.summary || 'Analysing…')}</div>
            ${r.approval_request_id ? `<a class="link" href="#/approvals/${r.approval_request_id}">Review proposal</a> · ` : ''}${agoCell(r.created_at)}</div></div>`).join('') : '<p class="muted small">None. Repairs are proposed automatically after repeated response-structure drift.</p>'}</div></div>
      </div>
      ${healingCard(heal)}
      <div class="card" style="margin-top:18px"><div class="card-head"><h2>Recent executions</h2><span class="spacer"></span><span class="muted small">${fails.length} recent failure(s)</span></div>
        ${execs.length ? `<div class="table-scroll"><table><thead><tr><th>When</th><th>Tool</th><th>Status</th><th>HTTP</th><th>Duration</th><th>Actor</th><th>Error</th></tr></thead><tbody>
        ${execs.map((e) => `<tr class="clickable" data-go="#/monitoring/execution/${e.id}"><td class="nowrap">${agoCell(e.created_at)}</td><td class="mono small">${esc(e.tool)}</td><td>${badge(e.status)}</td>
          <td>${e.http_status ?? '—'}</td><td>${e.duration_ms} ms</td><td class="small">${esc(e.actor)} <span class="muted">${esc(e.actor_type)}</span></td><td class="wrap small">${e.error_class ? `<code>${esc(e.error_class)}</code> ${esc(e.error.slice(0, 140))}` : ''}</td></tr>`).join('')}
        </tbody></table></div>` : emptyState('pulse', 'No executions yet', 'Run a tool from the Test Lab.')}</div></div>`;
  };
  await paint();
  ctx.el.addEventListener('click', (e) => { const r = e.target.closest('[data-go]'); if (r) go(r.dataset.go); });
  timer = setInterval(() => paint().catch(() => {}), 15000);
  return () => clearInterval(timer);
}

async function renderExecution({ el, params }) {
  const [e, logs] = await Promise.all([api(`/executions/${params[1]}`), api(`/executions/${params[1]}/logs`)]);
  el.innerHTML = `<div class="page">
    <div class="crumb"><a href="#/monitoring">Monitoring</a> / Execution</div>
    ${pageHead('pulse', e.tool, `${badge(e.status)} by ${esc(e.actor)} (${esc(e.actor_type)}) · ${esc(fmtTime(e.created_at))}`)}
    <div class="stack" id="exec">${executionCards(e)}
      <div class="card"><div class="card-head"><h2>Sanitized log</h2></div><div class="card-body"><pre>${esc(logs.lines.join('\n'))}</pre></div></div></div></div>`;
  el.querySelector('#exec').addEventListener('click', explainHandler(el.querySelector('#exec')));
}


const CASE_STATUS = { investigating: 'running', resolved: 'ok', awaiting_approval: 'pending', needs_admin: 'failed' };
const CASE_LABEL = { investigating: 'investigating', resolved: 'fixed', awaiting_approval: 'fix awaiting approval', needs_admin: 'needs an admin' };

function healingCard(h) {
  const rows = h.cases.slice(0, 10);
  return `<div class="card" style="margin-top:18px"><div class="card-head"><h2>Self-healing</h2><span class="spacer"></span>
      ${h.enabled ? badge('ok', 'on') : `${badge('disabled', 'off')} <a class="link small" href="#/settings">Turn on in Settings</a>`}</div>
    ${rows.length ? `<div class="table-scroll"><table><thead><tr><th>Target</th><th>Status</th><th>Root cause</th><th>Attempts</th><th>Seen</th><th>Updated</th></tr></thead><tbody>
      ${rows.map((c) => `<tr class="clickable" data-go="#/monitoring/healing/${c.id}"><td class="mono small">${esc(c.target)}<div class="muted">${esc(c.error_class)}</div></td>
        <td>${badge(CASE_STATUS[c.status] || '', CASE_LABEL[c.status] || c.status)}</td><td class="wrap small">${esc(c.root_cause.slice(0, 180))}</td>
        <td>${c.attempts.length}</td><td>${c.occurrences}×</td><td class="nowrap">${agoCell(c.updated_at)}</td></tr>`).join('')}
    </tbody></table></div>` : `<div class="card-body"><p class="muted small" style="margin:0">${h.enabled ? 'No failures needed attention.' : 'When self-healing is on, every failure is traced here: root cause, what was tried, and what still needs an administrator. Use “Investigate and fix” on a failed execution to try it once.'}</p></div>`}</div>`;
}

let caseTimer;

async function renderCase({ el, params }) {
  const paint = async () => {
    const c = await api(`/healing/cases/${params[1]}`);
    el.innerHTML = `<div class="page">
      <div class="crumb"><a href="#/monitoring">Monitoring</a> / Self-healing</div>
      ${pageHead('wrench', c.target, `${badge(CASE_STATUS[c.status] || '', CASE_LABEL[c.status] || c.status)} ${esc(c.error_class)} · seen ${c.occurrences}× · ${esc(fmtTime(c.updated_at))}`,
        `${c.approval_request_id ? `<a class="btn primary" href="#/approvals/${c.approval_request_id}">Review the fix</a>` : ''}
         <button class="btn" id="retry" ${c.status === 'investigating' ? 'disabled' : ''}>${icon('refresh', 15)} Investigate again</button>`)}
      <div class="stack">
        ${c.status === 'investigating' ? '<div class="callout info"><span class="spinner"></span> Investigating…</div>' : ''}
        <div class="card card-pad"><h2 style="margin-bottom:6px">Root cause</h2><p style="margin:0">${esc(c.root_cause || '—')}</p>
          ${c.analysis && c.analysis !== c.root_cause ? `<p class="small" style="margin:10px 0 0">${icon('spark', 13)} ${esc(c.analysis)} <span class="muted mono">${esc(c.model)}</span></p>` : ''}
          ${c.endpoint.url ? `<p class="small muted" style="margin:10px 0 0">Endpoint: <span class="mono">${esc(c.endpoint.method)} ${esc(c.endpoint.url)}</span>${c.endpoint.http_status ? ` · HTTP ${c.endpoint.http_status}` : ''}</p>` : ''}</div>
        <div class="card"><div class="card-head"><h2>What was tried</h2></div><div class="card-body">
          ${c.attempts.length ? c.attempts.map((a) => `<div class="finding">${badge(a.ok ? 'ok' : 'failed', a.ok ? 'worked' : 'did not fix it')}<div class="small"><b>${esc(a.action)}</b><div class="muted">${esc(a.result)}</div></div></div>`).join('')
            : '<p class="muted small" style="margin:0">Nothing could be tried automatically for this kind of problem.</p>'}</div></div>
        ${c.still_failing ? `<div class="callout bad small"><b>Still failing:</b> ${esc(c.still_failing)}</div>` : ''}
        ${c.required.length ? `<div class="callout warn"><b>Required from an administrator</b><ul style="margin:6px 0 0;padding-left:18px">${c.required.map((r) => `<li class="small">${esc(r)}</li>`).join('')}</ul></div>` : ''}
        <div class="card"><div class="card-head"><h2>Evidence</h2><span class="spacer"></span><span class="muted small">${c.notified_at ? `administrators notified ${agoCell(c.notified_at)}` : ''}</span></div>
          <div class="card-body"><pre>${esc(JSON.stringify(c.evidence, null, 2))}</pre>${c.execution_id ? `<a class="link small" href="#/monitoring/execution/${c.execution_id}">Open the failed execution</a>` : ''}</div></div>
      </div></div>`;
    el.querySelector('#retry')?.addEventListener('click', async () => {
      try { await api(`/healing/cases/${c.id}/retry`, { method: 'POST' }); paint(); } catch (err) { toast(err.message, 'bad'); }
    });
    clearTimeout(caseTimer);
    if (c.status === 'investigating') caseTimer = setTimeout(() => paint().catch(() => {}), 1500);
  };
  await paint();
  return () => clearTimeout(caseTimer);
}
