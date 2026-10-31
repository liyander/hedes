// Fix page: opened from a WhatsApp alert's link (/fix/<token>). Shows the issue and one-tap actions for it.
import { api, esc, icon, badge, agoCell, fmtTime, pageHead, modal, toast, formData } from '../lib.js';
import { go } from '../app.js';

const SEV = { critical: 'critical', high: 'high', medium: 'medium', low: 'low', info: 'info' };

export async function render({ el, params, refreshCounts }) {
  const token = params[0] || '';
  let ctx;
  try {
    ctx = await api(`/fix/${encodeURIComponent(token)}`);
  } catch (err) {
    el.innerHTML = `<div class="page narrow stack">${pageHead('wrench', 'Fix', '')}
      <div class="callout bad"><b>${esc(err.message)}</b></div><div class="row"><a class="btn" href="#/monitoring">Open Monitoring</a></div></div>`;
    return;
  }
  const paint = (c, result) => {
    const a = c.alert, ap = c.approval;
    el.innerHTML = `<div class="page narrow stack">
      ${pageHead('wrench', c.test ? 'Fix link test' : 'Fix this issue', c.test ? '' : 'Opened from a WhatsApp alert. Every action runs with your own account and is audited.')}
      ${result ? `<div class="callout ok small">${esc(result)}</div>` : ''}
      ${c.test ? '<div class="callout ok"><b>Fix links work.</b> Real alerts will open here with the actions that apply.</div>' : ''}
      ${a ? `<div class="card"><div class="card-body">
        <div class="row" style="gap:8px;flex-wrap:wrap">${badge(SEV[a.severity] || 'info', a.severity)} <b>${esc(a.kind.replace(/_/g, ' '))}</b>
          ${a.acknowledged ? badge('ok', 'acknowledged') : ''} ${c.muted_until ? badge('info', 'muted') : ''}<span class="spacer"></span><span class="muted small">${agoCell(a.created_at)}</span></div>
        <p style="margin:10px 0 0">${esc(a.message)}</p>
        ${c.integration ? `<p class="small muted" style="margin:6px 0 0">${icon('plug', 13)} ${esc(c.integration.name)} · ${esc(c.integration.status)}</p>` : ''}</div></div>` : ''}
      ${ap ? `<div class="card"><div class="card-head"><h2>${ap.kind === 'repair' ? 'Proposed fix' : 'Waiting for review'}</h2><span class="spacer"></span>${badge(ap.decision, ap.decision)}</div><div class="card-body">
        <div class="small"><b>${esc(ap.integration)} ${esc(ap.version)}</b> · risk ${esc(ap.risk || 'n/a')} · tests ${ap.test_result ? badge(ap.test_result, ap.test_result) : 'not run'}</div>
        ${ap.summary ? `<p class="small" style="margin:8px 0 0">${esc(ap.summary)}</p>` : ''}
        ${ap.changes && Object.keys(ap.changes).length ? `<div class="small mono" style="margin-top:8px">${Object.entries(ap.changes).map(([k, v]) => `${esc(k)} → ${esc(v)}`).join('<br>')}</div>` : ''}
        ${ap.blockers.length ? `<div class="callout warn small" style="margin-top:10px">${ap.blockers.map(esc).join('<br>')}</div>` : ''}
        <div class="small" style="margin-top:8px"><a href="#/approvals/${esc(ap.id)}">Full review (diff, security, tests)</a></div></div></div>` : ''}
      ${c.sources.length ? `<div class="card"><div class="card-head"><h2>Data sources</h2></div><div class="card-body">${c.sources.map((s) => `<div class="op-row small">
        <b class="mono">${esc(s.operation)}</b>${badge(s.enabled ? (s.state === 'ok' ? 'ok' : s.state === 'failing' ? 'failed' : 'info') : 'pending', s.enabled ? s.state.replace(/_/g, ' ') : 'paused')}
        <span class="grow muted">${s.consecutive_failures ? `${s.consecutive_failures} failed in a row` : ''} ${esc(s.last_error)}</span></div>`).join('')}</div></div>` : ''}
      ${c.actions.length ? `<div class="fix-actions">${c.actions.map((x, i) => `<button class="btn ${x.style === 'primary' ? 'primary' : x.style === 'danger' ? 'danger' : ''}" data-i="${i}" ${x.disabled ? 'disabled title="Resolve the problems above first"' : ''}>${esc(x.label)}</button>`).join('')}</div>`
        : (c.test ? '' : '<p class="muted small">Nothing left to do here.</p>')}
      <p class="muted small">This link expires ${esc(fmtTime(c.expires_at))} and only works for you.</p></div>`;
  };
  paint(ctx);

  el.addEventListener('click', async (e) => {
    const b = e.target.closest('[data-i]');
    if (!b) return;
    const act = ctx.actions[+b.dataset.i];
    if (act.href) return go(act.href);
    // request_id: the review this page shows, so a newer version submitted meanwhile is never approved unseen
    const body = { source_id: act.source_id, minutes: act.minutes, request_id: ctx.approval?.id };
    if (act.needs_comment) {
      const r = await modal({ title: 'Reject the fix', body: '<div class="field"><label>Reason (shown to the requester)</label><textarea name="comment" rows="3" required></textarea></div>',
        actions: [{ label: 'Cancel', value: null }, { label: 'Reject', cls: 'danger-solid', handler: (root) => formData(root).comment.trim() || false }] });
      if (!r) return;
      body.comment = r;
    }
    b.disabled = true;
    const label = b.textContent;
    b.innerHTML = '<span class="spinner"></span>';
    try {
      const r = await api(`/fix/${encodeURIComponent(token)}/${act.id}`, { method: 'POST', body });
      ctx = r;
      paint(r, `${label}: ${r.result}`);
      refreshCounts();
    } catch (err) {
      toast(err.message, 'bad');
      b.disabled = false;
      b.textContent = label;
    }
  });
}
