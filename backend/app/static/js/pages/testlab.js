import { $, api, esc, icon, badge, method, pageHead, emptyState, toast, json, schemaForm, schemaValues } from '../lib.js';

export async function render({ el, params }) {
  const tools = await api('/tools');
  const preselect = params[0] ? tools.find((t) => t.integration_id === params[0]) : null;
  el.innerHTML = `<div class="page wide">
    ${pageHead('flask', 'Test Lab', 'Preview the exact HTTP request (credentials redacted), then execute published tools and inspect structured results.')}
    ${tools.length ? `<div class="split" style="grid-template-columns:minmax(0,1fr) minmax(0,1.2fr)">
      <div class="card"><div class="card-head"><h2>Tool</h2></div><div class="card-body">
        <div class="field"><select id="tool">${tools.map((t) => `<option value="${t.id}" ${preselect?.id === t.id ? 'selected' : ''}>${esc(t.name)} — ${esc(t.method)}</option>`).join('')}</select></div>
        <div id="desc" class="small muted" style="margin-bottom:12px"></div>
        <form id="inputs"></form>
        <div class="row"><button class="btn" id="preview">${icon('eye', 15)} Preview request</button><button class="btn primary" id="run">${icon('play', 15)} Execute live</button></div>
      </div></div>
      <div class="stack" id="out"><div class="card card-pad muted">Results appear here.</div></div></div>`
      : emptyState('flask', 'No published tools', 'Tools appear here once an administrator approves an integration.', '<a class="btn primary" href="#/studio">Build one</a>')}</div>`;
  if (!tools.length) return;

  const cur = () => tools.find((t) => t.id === $('#tool', el).value);
  const paintForm = () => {
    const t = cur();
    $('#desc', el).innerHTML = `${method(t.method)} ${esc(t.description)} · outputs: ${t.output_fields.map((f) => `<code>${esc(f)}</code>`).join(' ') || 'raw'}`;
    $('#inputs', el).innerHTML = schemaForm(t.input_schema);
  };
  paintForm();
  $('#tool', el).addEventListener('change', paintForm);

  const out = $('#out', el);
  $('#preview', el).addEventListener('click', async () => {
    const t = cur();
    try {
      const r = await api(`/integrations/${t.integration_id}/preview`, { method: 'POST', body: { version_id: t.version_id, operation: t.operation_id, inputs: schemaValues($('#inputs', el)) } });
      out.innerHTML = r.ok ? `<div class="card"><div class="card-head"><h2>Request preview</h2><span class="spacer"></span>${badge('planned', 'dry run')}</div><div class="card-body">
          <p class="small muted" style="margin-top:0">Nothing was sent. Credentials are shown as •••.</p>${r.egress_warning ? `<div class="callout warn small">${esc(r.egress_warning)}</div>` : ''}${json(r.request)}</div></div>`
        : `<div class="callout bad"><b>Inputs rejected</b><ul>${r.errors.map((e) => `<li>${esc(e)}</li>`).join('')}</ul></div>`;
    } catch (err) { toast(err.message, 'bad'); }
  });
  $('#run', el).addEventListener('click', async (e) => {
    const t = cur();
    e.target.disabled = true;
    out.innerHTML = '<div class="card card-pad"><span class="spinner"></span> Executing…</div>';
    try {
      const r = await api(`/tools/${t.id}/execute`, { method: 'POST', body: { inputs: schemaValues($('#inputs', el)) } });
      out.innerHTML = executionCards(r);
    } catch (err) { out.innerHTML = `<div class="callout bad">${esc(err.message)}</div>`; }
    e.target.disabled = false;
  });
  out.addEventListener('click', explainHandler(out));
}

export function executionCards(r) {
  return `<div class="card"><div class="card-head"><h2>Result</h2><span class="spacer"></span>${badge(r.status)}${r.http_status ? ` <span class="badge">HTTP ${r.http_status}</span>` : ''}<span class="muted small">${r.duration_ms} ms · ${r.attempts} attempt(s)</span></div>
    <div class="card-body">
      ${r.error_class ? `<div class="callout bad small" style="margin-bottom:12px"><b>${esc(r.error_class)}</b> — ${esc(r.error)}${r.guide ? `<div style="margin-top:4px">${esc(r.guide[0])} <b>${esc(r.guide[1])}</b></div>` : ''}
        <div class="row" style="margin-top:8px"><button class="btn sm" data-explain="${r.id}">${icon('spark', 14)} Explain with AI</button>${r.error_class === 'response_drift' ? `<button class="btn sm" data-repair="${r.id}">${icon('wrench', 14)} Propose repair</button>` : ''}
        <a class="btn sm ghost" href="#/monitoring/execution/${r.id}">Open execution</a></div><div data-explanation></div></div>` : ''}
      <h3 class="small muted" style="margin:0 0 6px">OUTPUT</h3>${json(r.output)}
      ${r.diagnostics?.missing_fields ? `<h3 class="small muted" style="margin:12px 0 6px">OBSERVED RESPONSE STRUCTURE</h3>${json(r.diagnostics.response_shape)}` : ''}
    </div></div>
    <div class="card"><div class="card-head"><h2>Request sent</h2><span class="muted small">credentials redacted</span></div><div class="card-body">${json(r.request)}</div></div>`;
}

export function explainHandler(root) {
  return async (e) => {
    const t = e.target.closest('[data-explain], [data-repair]');
    if (!t) return;
    t.disabled = true;
    try {
      if (t.dataset.explain) {
        const r = await api(`/executions/${t.dataset.explain}/explain`, { method: 'POST' });
        root.querySelector('[data-explanation]').innerHTML = `<p class="small" style="margin:10px 0 0">${icon('spark', 13)} ${esc(r.explanation)} <span class="muted mono">${esc(r.model)}</span></p>`;
      } else {
        const r = await api(`/executions/${t.dataset.repair}/repair`, { method: 'POST' });
        root.querySelector('[data-explanation]').innerHTML = `<p class="small" style="margin:10px 0 0">${icon('wrench', 13)} Repair ${esc(r.status.replace('_', ' '))}: ${esc(r.diagnosis.summary || '')}
          ${r.approval_request_id ? ` <a class="link" href="#/approvals/${r.approval_request_id}">Open review</a>` : ''}</p>`;
      }
    } catch (err) { toast(err.message, 'bad'); }
    t.disabled = false;
  };
}
