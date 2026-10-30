import { $, api, esc, icon, badge, method, agoCell, pageHead, emptyState, modal, toast, json, riskBadge } from '../lib.js';
import { go, route, isAdmin } from '../app.js';
import { diffHtml } from './integrations.js';

export async function render(ctx) {
  if (ctx.params[0]) return renderReview(ctx);
  const filter = sessionStorage.getItem('integra-approval-filter') || 'pending';
  const rows = isAdmin() ? await api(`/admin/approvals?status=${filter}`) : await api('/approvals/mine');
  ctx.el.innerHTML = `<div class="page">
    ${pageHead('check', 'Approvals', isAdmin() ? 'Review exactly what each integration version can do before it goes live. Approval is bound to the configuration hash.' : 'Your submissions and their review status.',
      isAdmin() ? `<div class="tabs">${['pending', 'approved', 'rejected', 'changes_requested', 'all'].map((k) => `<button class="${k === filter ? 'on' : ''}" data-f="${k}">${k.replace('_', ' ')}</button>`).join('')}</div>` : '')}
    <div class="card">${rows.length ? `<div class="table-scroll"><table><thead><tr><th>Integration</th><th>Kind</th><th>Risk</th><th>Requested by</th><th>Status</th><th>Submitted</th></tr></thead><tbody>
      ${rows.map((a) => `<tr class="clickable" data-go="#/approvals/${a.id}"><td><b>${esc(a.integration)}</b> <span class="mono muted">${esc(a.version)}</span></td>
        <td>${a.kind === 'repair' ? badge('info', 'AI repair') : esc(a.kind)}</td><td>${riskBadge(a.risk?.level, a.risk?.score)}</td>
        <td>${esc(a.requester)}</td><td>${badge(a.decision)}</td><td class="nowrap">${agoCell(a.created_at)}</td></tr>`).join('')}
    </tbody></table></div>` : emptyState('check', filter === 'pending' ? 'Nothing waiting for review' : 'Nothing here', 'Submissions from AI Studio and repairs appear here.')}</div></div>`;
  ctx.el.addEventListener('click', (e) => {
    const f = e.target.closest('[data-f]');
    if (f) { sessionStorage.setItem('integra-approval-filter', f.dataset.f); return route(); }
    const r = e.target.closest('[data-go]');
    if (r) go(r.dataset.go);
  });
}

async function renderReview({ el, params }) {
  const a = await api(`/admin/approvals/${params[0]}`);
  const cfg = a.config.integration;
  const s = a.security;
  const check = (label, v) => `<span class="${v === true ? 'y' : v === false ? 'n' : 'u'}">${v === true ? '✓' : v === false ? '✗' : '–'} ${label}</span>`;
  el.innerHTML = `<div class="page">
    <div class="crumb"><a href="#/approvals">Approvals</a> / ${esc(a.integration)} ${esc(a.version)}</div>
    <div class="card card-pad" style="border-top:3px solid var(--${a.decision === 'pending' ? 'high' : a.decision === 'approved' ? 'ok' : 'bad'})">
      <div class="row between"><div><div class="muted small">${a.decision === 'pending' ? 'Pending approval' : `Decision: ${esc(a.decision.replace('_', ' '))} by ${esc(a.reviewer)}`}</div>
        <h1 style="font-size:24px;margin:4px 0">${esc(a.integration)}</h1>
        <div class="muted">Requested by ${esc(a.requester)} · version ${esc(a.version)} · ${agoCell(a.created_at)} · ${a.kind === 'repair' ? badge('info', 'AI repair proposal') : esc(a.kind)}</div></div>
        <div class="row">${riskBadge(s.level, s.score)} ${badge(a.decision)}</div></div>
      <div class="grid-3" style="margin-top:18px">
        <div class="card card-pad"><div class="muted small">Requested actions</div><b style="font-size:18px">${esc((s.capabilities || []).join(' + ') || '—')}</b></div>
        <div class="card card-pad"><div class="muted small">Security review</div><b style="font-size:18px">${s.blocking ? 'Blocking issues' : 'Checks passed'}</b></div>
        <div class="card card-pad"><div class="muted small">Authorized destination</div><b style="font-size:18px" class="mono">${esc((s.destinations || []).join(', '))}</b></div>
      </div>
      <div class="checks" style="margin-top:16px">${check('Domain checked', s.checks.destination_checked)}${check('Schema valid', s.checks.schema_valid)}${check('Secrets referenced safely', s.checks.secrets_referenced_safely)}
        ${check('Credential connected', s.checks.credential_connected)}${check('Tests passed', a.test_result === 'passed')}${check('Hash unchanged since submission', a.hash_matches)}</div>
      ${a.repair ? `<div class="callout info" style="margin-top:16px">${icon('wrench', 15)} <b>Repair agent diagnosis</b> <span class="muted small mono">${esc(a.repair.diagnosis.model || '')}</span><p class="small" style="margin:6px 0 0">${esc(a.repair.diagnosis.summary || '')}</p></div>` : ''}
      ${a.blockers.length && isAdmin() ? `<div class="callout warn" style="margin-top:16px"><b>Cannot approve yet</b><ul style="margin:4px 0 0;padding-left:18px">${a.blockers.map((b) => `<li class="small">${esc(b)}</li>`).join('')}</ul></div>` : ''}
      ${a.comment ? `<div class="callout small" style="margin-top:16px"><b>Reviewer comment:</b> ${esc(a.comment)}</div>` : ''}
      ${a.can_decide ? `<div class="row" style="margin-top:18px"><button class="btn ok-solid" data-d="approve" ${a.blockers.length ? 'disabled' : ''}>${icon('check', 16)} Approve &amp; publish</button>
        <button class="btn" data-d="changes">Request changes</button><button class="btn danger" data-d="reject">Reject</button>
        <span class="muted small">Approval publishes exactly hash <span class="mono">${esc(a.config_hash.slice(0, 16))}…</span></span></div>`
        : a.decision === 'pending' && isAdmin() ? '<p class="muted small" style="margin:16px 0 0">You submitted this version, so another administrator must review it.</p>' : ''}
    </div>
    <div class="split" style="margin-top:18px">
      <div class="stack">
        <div class="card"><div class="card-head"><h2>What it can do</h2></div><div class="card-body">
          ${cfg.operations.map((o) => `<div class="op-row">${method(o.method)}<div class="grow"><div class="path">${esc(cfg.base_url)}${esc(o.path)}</div><div class="small muted">${esc(o.description || o.id)}</div></div>${badge(o.safety === 'destructive' ? 'critical' : o.safety === 'write' ? 'medium' : 'low', o.safety)}</div>`).join('')}
          <dl class="kv" style="margin-top:12px"><dt>Credential</dt><dd><code>${esc(cfg.authentication.type)}</code> via connection <code>${esc(cfg.authentication.credential_ref || '—')}</code>${cfg.authentication.scopes ? ` · scopes ${cfg.authentication.scopes.map(esc).join(', ')}` : ''}</dd></dl></div></div>
        ${a.diff ? `<div class="card card-pad"><h2 style="margin-bottom:8px">Changes vs live ${esc(a.diff.a)}</h2>${diffHtml(a.diff.changes)}</div>` : ''}
        <div class="card"><div class="card-head"><h2>Configuration</h2></div><div class="card-body">${json(a.config)}</div></div>
      </div>
      <div class="stack">
        <div class="card"><div class="card-head"><h2>Security findings</h2></div><div class="card-body">
          ${s.ai_summary ? `<p class="small" style="margin-top:0">${icon('spark', 13)} ${esc(s.ai_summary)}</p>` : ''}
          ${s.findings.map((f) => `<div class="finding">${badge(f.severity)}<div><b class="small">${esc(f.title)}</b>${f.blocking ? ' <span class="badge critical">blocking</span>' : ''}<div class="small muted">${esc(f.detail)}</div></div></div>`).join('') || '<p class="muted">None.</p>'}</div></div>
        <div class="card"><div class="card-head"><h2>Test results</h2></div><div class="card-body">
          ${a.tests ? a.tests.cases.map((c) => `<div class="finding">${badge(c.passed ? 'passed' : 'failed')}<div class="small"><b>${esc(c.name)}</b><div class="muted">expected ${esc(c.expect)} · got ${esc(c.actual)}</div></div></div>`).join('') : '<p class="muted">No tests.</p>'}</div></div>
        <div class="card"><div class="card-head"><h2>History</h2></div><div class="card-body">${a.events.map((ev) => `<div class="small" style="padding:4px 0">${badge(ev.action)} ${esc(ev.actor)} · ${agoCell(ev.at)}${ev.comment ? `<div class="muted">${esc(ev.comment)}</div>` : ''}</div>`).join('')}</div></div>
      </div></div></div>`;

  el.addEventListener('click', async (e) => {
    const t = e.target.closest('[data-d]');
    if (!t) return;
    const d = t.dataset.d;
    const needs = d !== 'approve';
    const res = await modal({
      title: d === 'approve' ? 'Approve and publish' : d === 'reject' ? 'Reject version' : 'Request changes',
      body: `${d === 'approve' ? `<p style="margin-top:0">Publishing makes ${cfg.operations.length} tool(s) executable immediately for permitted roles.</p>` : ''}
        <div class="field"><label>Comment${needs ? ' (required)' : ''}</label><textarea id="cm" placeholder="${needs ? 'Explain what must change' : 'Optional note'}"></textarea></div>`,
      actions: [{ label: 'Cancel', value: null }, {
        label: d === 'approve' ? 'Approve & publish' : d === 'reject' ? 'Reject' : 'Request changes', cls: d === 'approve' ? 'ok-solid' : d === 'reject' ? 'danger-solid' : 'primary',
        handler: async (root) => api(`/admin/approvals/${a.id}/${d}`, { method: 'POST', body: { comment: $('#cm', root).value } }),
      }],
    });
    if (res) { toast(`Decision recorded: ${res.decision.replace('_', ' ')}`, 'ok'); route(); }
  });
}
