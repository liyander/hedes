import { $, api, esc, icon, badge, method, agoCell, pageHead, emptyState, toast, json, confirmBox, riskBadge, readFile } from '../lib.js';
import { go, route, isAdmin, state } from '../app.js';
import { askAbout } from '../assistant.js';
import { connectModal } from './credentials.js';

export async function render(ctx) {
  if (ctx.params[0] === 'new') return renderNew(ctx);
  if (ctx.params[0] === 'sources') return renderSources(ctx);
  if (ctx.params[0]) return renderDetail(ctx);
  const rows = await api('/integrations');
  ctx.el.innerHTML = `<div class="page">
    ${pageHead('plug', 'Integrations', 'Every connection is a versioned, declarative configuration executed by the same generic engine.',
      `<a class="btn" href="#/integrations/new">${icon('upload', 16)} Import / paste config</a><a class="btn" href="#/studio/new">${icon('spark', 16)} Build with AI</a><a class="btn primary" href="#/integrations/sources">${icon('db', 16)} Add data source</a>`)}
    <div class="card">${rows.length ? `<div class="table-scroll"><table><thead><tr><th>Integration</th><th>Status</th><th>Live</th><th>Latest</th><th>Auth</th><th>Ops</th><th>Risk</th><th>Updated</th></tr></thead><tbody>
      ${rows.map((i) => `<tr class="clickable" data-go="#/integrations/${i.id}"><td><b>${esc(i.name)}</b><div class="muted small mono">${esc(i.slug)} · ${esc(i.base_url || '')}</div></td>
        <td>${badge(i.status)}</td><td class="mono">${esc(i.published_version || '—')}</td><td class="mono">${esc(i.latest_version || '')} ${i.latest_status !== 'published' ? badge(i.latest_status) : ''}</td>
        <td><code>${esc(i.auth_type || '')}</code></td><td>${i.operations}</td><td>${badge(i.risk)}</td><td class="nowrap">${agoCell(i.updated_at)}</td></tr>`).join('')}
    </tbody></table></div>` : emptyState('plug', 'No integrations yet', 'Add a real data source (Okta, GitHub, Jira, NVD, AbuseIPDB, CISA KEV), build one with AI, or import an OpenAPI document.', '<a class="btn primary" href="#/integrations/sources">Add data source</a>')}</div></div>`;
  ctx.el.addEventListener('click', (e) => { const r = e.target.closest('[data-go]'); if (r) go(r.dataset.go); });
}

async function renderNew({ el }) {
  const examples = await api('/catalog/examples');
  el.innerHTML = `<div class="page">
    <div class="crumb"><a href="#/integrations">Integrations</a> / New</div>
    ${pageHead('upload', 'New integration', 'Import an OpenAPI 3 document or paste a configuration. It becomes a draft that is validated, tested and reviewed like any AI-generated one.')}
    <div class="split even">
      <div class="card card-pad"><h2>Import OpenAPI</h2><p class="muted small">Pick the operations to expose. Base URL and authentication are read from the document.</p>
        <div class="field"><label>OpenAPI document (JSON or YAML)</label><textarea class="code" id="oas" style="min-height:180px" placeholder="Paste the document, or choose a file below"></textarea></div>
        <div class="row"><label class="btn sm" style="margin:0">${icon('upload', 14)} Choose file<input type="file" id="oas-file" accept=".json,.yaml,.yml" hidden></label>
          ${state.demo ? '<button class="btn sm ghost" id="oas-sample">Load TaskFlow sample (demo)</button>' : ''}<span class="grow"></span><button class="btn" id="oas-read">Read operations</button></div>
        <div id="oas-ops" style="margin-top:14px"></div></div>
      <div class="card card-pad"><h2>Paste configuration</h2><p class="muted small">YAML or JSON following <code>shared/integration.schema.json</code>.</p>
        <div class="field"><label>Start from an example</label><select id="ex"><option value="">—</option>${examples.map((x) => `<option value="${esc(x.slug)}">${esc(x.name)}</option>`).join('')}</select></div>
        <div class="field"><textarea class="code" id="cfg" placeholder="schema_version: &quot;1.0&quot;&#10;integration: ..."></textarea></div>
        <div class="row end"><button class="btn primary" id="create">Create draft</button></div></div>
    </div></div>`;
  $('#ex', el).addEventListener('change', (e) => {
    const x = examples.find((y) => y.slug === e.target.value);
    if (x) $('#cfg', el).value = JSON.stringify({ ...x.config, integration: { ...x.config.integration, id: `${x.slug}-copy` } }, null, 2);
  });
  $('#create', el).addEventListener('click', async () => {
    try {
      const r = await api('/integrations', { method: 'POST', body: { config_text: $('#cfg', el).value } });
      toast(r.validation.valid ? 'Draft created' : 'Draft created with validation errors', r.validation.valid ? 'ok' : 'bad');
      go(`#/integrations/${r.integration.id}`);
    } catch (err) { toast(err.message, 'bad'); }
  });
  $('#oas-file', el).addEventListener('change', async (e) => { if (e.target.files[0]) $('#oas', el).value = await readFile(e.target.files[0]); });
  $('#oas-sample', el)?.addEventListener('click', async () => { $('#oas', el).value = await (await fetch('/mock/taskflow/openapi.json')).text(); });
  $('#oas-read', el).addEventListener('click', async () => {
    try {
      const r = await api('/integrations/import/openapi', { method: 'POST', body: { content: $('#oas', el).value, preview: true } });
      $('#oas-ops', el).innerHTML = `<div class="callout info small"><b>${esc(r.catalog.title)}</b> ${esc(r.catalog.version)} · ${esc(r.catalog.base_url || 'no server URL')} · auth: ${esc(r.catalog.auth.map((a) => a.type).join(', ') || 'none')}</div>
        ${r.operations.map((o) => `<label class="check op-row"><input type="checkbox" value="${esc(o.id)}" ${o.method !== 'DELETE' ? 'checked' : ''}>${method(o.method)}<span class="path">${esc(o.path)}</span><span class="muted small">${esc(o.summary)}</span></label>`).join('')}
        ${r.catalog.base_url ? '' : '<div class="field" style="margin-top:10px"><label>Base URL</label><input type="url" id="oas-base" placeholder="https://api.example.com"></div>'}
        <div class="row end" style="margin-top:12px"><button class="btn primary" id="oas-create">Create draft</button></div>`;
      $('#oas-create', el).addEventListener('click', async () => {
        const ops = [...el.querySelectorAll('#oas-ops input[type=checkbox]:checked')].map((i) => i.value);
        try {
          const res = await api('/integrations/import/openapi', { method: 'POST', body: { content: $('#oas', el).value, operations: ops, base_url: $('#oas-base', el)?.value || null } });
          go(`#/integrations/${res.integration.id}`);
        } catch (err) { toast(err.message, 'bad'); }
      });
    } catch (err) { toast(err.message, 'bad'); }
  });
}

let tabState = 'overview';

async function renderDetail({ el, params }) {
  const i = await api(`/integrations/${params[0]}`);
  const latest = i.versions[0];
  const [v, guide] = await Promise.all([api(`/versions/${params[1] || latest.id}`), api(`/integrations/${params[0]}/guide`)]);
  const tab = params[2] || tabState;
  const cfg = v.config.integration;
  const isDraft = v.status === 'draft';
  const auth = cfg.authentication || {};
  el.innerHTML = `<div class="page">
    <div class="crumb"><a href="#/integrations">Integrations</a> / ${esc(i.name)}</div>
    ${pageHead('plug', i.name, `${esc(i.description || '')}<br><span class="mono small">${esc(cfg.base_url)}</span>`, `
      ${badge(i.status)}
      <button class="btn" data-a="discuss">${icon('spark', 15)} Discuss with AI</button>
      ${isDraft ? `<button class="btn" data-a="tests">${icon('flask', 15)} Run safe tests</button><button class="btn primary" data-a="submit">${icon('check', 15)} Submit for approval</button>`
        : `<button class="btn" data-a="revise">${icon('doc', 15)} New revision</button>`}
      ${i.status === 'published' ? `<a class="btn" href="#/testlab/${i.id}">${icon('play', 15)} Test Lab</a>` : ''}
      ${isAdmin() ? (i.status === 'disabled' ? '<button class="btn" data-a="enable">Enable</button>' : '<button class="btn danger" data-a="disable">Disable</button>') : ''}`)}
    <div class="row" style="margin-bottom:14px">
      <label class="small muted" for="vsel">Version</label>
      <select id="vsel" style="width:auto">${i.versions.map((x) => `<option value="${x.id}" ${x.id === v.id ? 'selected' : ''}>${esc(x.version)} · ${esc(x.status.replace('_', ' '))}${x.id === i.versions.find((y) => y.status === 'published')?.id ? ' (live)' : ''}</option>`).join('')}</select>
      ${badge(v.status)} ${riskBadge(v.risk.level, v.risk.score)} ${v.tests ? badge(v.tests.result, `tests ${v.tests.passed}/${v.tests.total}`) : badge('draft', 'not tested')}
      <span class="muted small mono" title="Configuration hash">#${esc(v.config_hash.slice(0, 12))}</span>
    </div>
    ${auth.type && auth.type !== 'none' ? `<div class="callout info small" style="margin-bottom:14px"><div class="row between"><span>${icon('key', 14)} Uses the <b>${esc(auth.type)}</b> connection <code>${esc(auth.credential_ref)}</code>${v.security.checks?.credential_connected === false ? ' — <b>not connected yet</b>' : v.security.checks?.credential_connected ? ' — connected' : ''}.</span>
      ${v.security.checks?.credential_connected ? '' : `<button class="btn sm" data-a="connect">Connect</button>`}</div></div>` : ''}
    ${v.notes ? `<div class="callout small" style="margin-bottom:14px">${esc(v.notes)}</div>` : ''}
    ${guideHtml(guide)}
    <div class="tabs" style="margin-bottom:16px">${['overview', 'configuration', 'security', 'tests', 'versions', 'approvals'].map((k) => `<button class="${k === tab ? 'on' : ''}" data-tab="${k}">${k[0].toUpperCase() + k.slice(1)}</button>`).join('')}</div>
    <div id="tab"></div></div>`;

  const pane = $('#tab', el);
  const paint = (t) => {
    tabState = t;
    el.querySelectorAll('[data-tab]').forEach((b) => b.classList.toggle('on', b.dataset.tab === t));
    if (t === 'overview') {
      pane.innerHTML = `<div class="split"><div class="card"><div class="card-head"><h2>Operations</h2></div><div class="card-body">
        ${cfg.operations.map((o) => `<div class="op-row">${method(o.method)}<div class="grow"><div class="path">${esc(o.path)}</div><div class="small muted"><b>${esc(o.id)}</b> — ${esc(o.description || '')}</div>
          <div class="small muted">inputs: ${Object.keys(o.input_schema.properties || {}).map((k) => `<code>${esc(k)}</code>`).join(' ') || '—'} · outputs: ${o.ingest ? `<span class="badge accent">data source · ${esc(o.ingest.entity || 'event')}</span> every ${esc((o.ingest.schedule || {}).cron ? `cron ${o.ingest.schedule.cron}` : `${(o.ingest.schedule || {}).interval_minutes || 15} min`)}` : Object.keys(o.response.mapping || {}).map((k) => `<code>${esc(k)}</code>`).join(' ') || 'raw body'}</div></div>${badge(o.safety === 'destructive' ? 'critical' : o.safety === 'write' ? 'medium' : 'low', o.safety)}</div>`).join('')}
        </div></div>
        <div class="stack"><div class="card card-pad"><h2 style="margin-bottom:10px">Details</h2><dl class="kv">
          <dt>Slug</dt><dd class="mono">${esc(i.slug)}</dd><dt>Transport</dt><dd>${esc(cfg.transport)}</dd><dt>Auth</dt><dd><code>${esc(auth.type)}</code>${auth.scopes ? ` scopes: ${auth.scopes.map(esc).join(', ')}` : ''}</dd>
          <dt>Source</dt><dd>${esc(v.source)}</dd><dt>Created</dt><dd>${esc(v.created_by)} · ${agoCell(v.created_at)}</dd><dt>Live version</dt><dd>${esc(i.published_version || 'not published')}</dd></dl></div>
          ${i.tools.length ? `<div class="card"><div class="card-head"><h2>Published tools</h2></div><div class="card-body">${i.tools.map((t) => `<div class="op-row">${method(t.method)}<span class="mono small grow">${esc(t.name)}</span>${t.mcp_enabled ? badge('info', 'MCP') : ''}</div>`).join('')}</div></div>` : ''}
          ${i.repairs.length ? `<div class="card"><div class="card-head"><h2>Repair proposals</h2></div><div class="card-body">${i.repairs.map((r) => `<div class="finding">${badge(r.status)}<div class="small">${esc(r.diagnosis.summary || r.diagnosis.missing_fields?.join(', ') || '')}<div class="muted">${agoCell(r.created_at)}</div></div></div>`).join('')}</div></div>` : ''}
        </div></div>`;
    } else if (t === 'configuration') {
      pane.innerHTML = isDraft
        ? `<div class="card card-pad"><div class="row between" style="margin-bottom:10px"><span class="muted small">Draft — editable. Saving re-runs validation and the security policy.</span><button class="btn primary" id="save">Save draft</button></div>
           <textarea class="code" id="cfg">${esc(JSON.stringify(v.config, null, 2))}</textarea><div id="val" style="margin-top:12px"></div></div>`
        : `<div class="card card-pad"><p class="muted small" style="margin-top:0">${icon('shield', 14)} This version is ${esc(v.status.replace('_', ' '))} and immutable. Create a revision to change it.</p>${json(v.config)}</div>`;
      const showVal = (val) => {
        const box = $('#val', el);
        if (box) box.innerHTML = val.valid ? `<div class="callout ok small">Valid configuration.${val.warnings.length ? ` ${val.warnings.length} warning(s).` : ''}</div>` :
          `<div class="callout bad small"><b>Validation errors</b><ul style="margin:4px 0 0;padding-left:18px">${val.errors.map((e) => `<li><code>${esc(e.path)}</code> ${esc(e.message)}</li>`).join('')}</ul></div>`;
      };
      if (isDraft) {
        showVal(v.validation);
        $('#save', el).addEventListener('click', async () => {
          try {
            const r = await api(`/versions/${v.id}`, { method: 'PUT', body: { config_text: $('#cfg', el).value } });
            showVal(r.validation);
            toast('Draft saved', 'ok');
          } catch (err) { toast(err.message, 'bad'); }
        });
      }
    } else if (t === 'security') {
      const s = v.security;
      pane.innerHTML = `<div class="split"><div class="card"><div class="card-head"><h2>Findings</h2><span class="spacer"></span>${riskBadge(s.level, s.score)}</div><div class="card-body">
        ${s.ai_summary ? `<div class="callout small" style="margin-bottom:10px">${icon('spark', 14)} ${esc(s.ai_summary)} <span class="muted mono">${esc(s.ai_model || '')}</span></div>` : ''}
        ${s.findings.map((f) => `<div class="finding">${badge(f.severity)}<div><b>${esc(f.title)}</b>${f.blocking ? ' <span class="badge critical">blocking</span>' : ''}<div class="small muted">${esc(f.detail)}</div></div></div>`).join('') || '<p class="muted">No findings.</p>'}</div></div>
        <div class="card card-pad"><h2 style="margin-bottom:10px">Policy checks</h2><div class="checks" style="flex-direction:column">${Object.entries(s.checks || {}).map(([k, val]) => `<span class="${val === true ? 'y' : val === false ? 'n' : 'u'}">${val === true ? '✓' : val === false ? '✗' : '–'} ${esc(k.replace(/_/g, ' '))}</span>`).join('')}</div>
          <dl class="kv" style="margin-top:14px"><dt>Destinations</dt><dd class="mono">${(s.destinations || []).map(esc).join(', ')}</dd><dt>Can</dt><dd>${(s.capabilities || []).join(' + ')}</dd></dl></div></div>`;
    } else if (t === 'tests') {
      pane.innerHTML = `<div class="card"><div class="card-head"><h2>Safe test runs</h2><span class="muted small">Mocked transport — no live API is contacted before approval</span><span class="spacer"></span>${isDraft ? '<button class="btn sm" data-a="tests">Run again</button>' : ''}</div><div class="card-body">
        ${v.test_runs.length ? v.test_runs.map((r, n) => `<details ${n === 0 ? 'open' : ''} style="margin-bottom:10px"><summary class="row" style="cursor:pointer">${badge(r.result)} <b>${r.report.passed}/${r.report.total} passed</b> <span class="muted small">${agoCell(r.executed_at)}${r.current ? '' : ' · for an earlier config'}</span></summary>
          ${r.report.cases.map((c) => `<div class="finding">${badge(c.passed ? 'passed' : 'failed')}<div><b class="small">${esc(c.name)}</b><div class="small muted">expected <code>${esc(c.expect)}</code>, got <code>${esc(c.actual)}</code>${c.request?.url ? ` · ${esc(c.request.method)} <span class="mono">${esc(c.request.url)}</span>` : ''}</div></div></div>`).join('')}</details>`).join('')
          : '<p class="muted">No test runs yet.</p>'}</div></div>`;
    } else if (t === 'versions') {
      pane.innerHTML = `<div class="card"><div class="table-scroll"><table><thead><tr><th>Version</th><th>Status</th><th>Source</th><th>Risk</th><th>Tests</th><th>Hash</th><th>Created</th><th></th></tr></thead><tbody>
        ${i.versions.map((x) => `<tr><td class="mono"><a class="link" href="#/integrations/${i.id}/${x.id}/configuration">${esc(x.version)}</a></td><td>${badge(x.status)}</td><td>${esc(x.source)}</td><td>${badge(x.risk.level)}</td>
          <td>${x.tests ? `${x.tests.passed}/${x.tests.total}` : '—'}</td><td class="mono small">${esc(x.config_hash.slice(0, 10))}</td><td class="small">${esc(x.created_by)} · ${agoCell(x.created_at)}</td>
          <td class="nowrap"><button class="btn sm" data-compare="${x.id}">Compare with live</button>${isAdmin() && x.status === 'superseded' ? ` <button class="btn sm" data-rollback="${x.id}">Restore</button>` : ''}</td></tr>`).join('')}
      </tbody></table></div></div><div id="diff" style="margin-top:14px"></div>`;
    } else {
      pane.innerHTML = `<div class="card">${i.approvals.length ? `<div class="table-scroll"><table><thead><tr><th>Decision</th><th>Kind</th><th>Requested by</th><th>Reviewer</th><th>Comment</th><th>When</th></tr></thead><tbody>
        ${i.approvals.map((a) => `<tr class="clickable" data-go="#/approvals/${a.id}"><td>${badge(a.decision)}</td><td>${esc(a.kind)}</td><td>${esc(a.requester)}</td><td>${esc(a.reviewer)}</td><td class="wrap small">${esc(a.comment)}</td><td class="nowrap">${agoCell(a.decided_at || a.created_at)}</td></tr>`).join('')}
      </tbody></table></div>` : emptyState('check', 'No reviews yet', 'Submit the draft for approval once its tests pass.')}</div>`;
    }
  };
  paint(tab);

  $('#vsel', el).addEventListener('change', (e) => go(`#/integrations/${i.id}/${e.target.value}`));
  el.addEventListener('click', async (e) => {
    const t = e.target.closest('[data-tab], [data-a], [data-compare], [data-rollback], [data-go]');
    if (!t) return;
    if (t.dataset.tab) return paint(t.dataset.tab);
    if (t.dataset.go) return go(t.dataset.go);
    try {
      if (t.dataset.compare) {
        const live = i.versions.find((x) => x.status === 'published');
        if (!live) return toast('Nothing is live yet');
        const d = await api(`/integrations/${i.id}/compare?a=${live.id}&b=${t.dataset.compare}`);
        $('#diff', el).innerHTML = `<div class="card card-pad"><h2 style="margin-bottom:8px">${esc(d.a)} → ${esc(d.b)}</h2>${diffHtml(d.changes)}</div>`;
        return;
      }
      if (t.dataset.rollback) {
        if (!(await confirmBox('Restore version', 'Publish this previously approved version again? Its exact configuration hash was approved before.', 'Restore'))) return;
        await api(`/integrations/${i.id}/rollback`, { method: 'POST', body: { version_id: t.dataset.rollback } });
        toast('Version restored', 'ok');
        return route();
      }
      const a = t.dataset.a;
      if (a === 'discuss') return askAbout({ type: 'integration', id: i.id, label: i.name }, `Explain the ${i.name} integration and what I should do next.`);
      if (a === 'guide') { const g = $('#guide', el); g.classList.toggle('collapsed'); try { localStorage.setItem('hedes-guide-collapsed', g.classList.contains('collapsed') ? '1' : '0'); } catch (x) { /* ignore */ } return; }
      t.disabled = true;
      if (a === 'tests') {
        const r = await api(`/integrations/${i.id}/tests`, { method: 'POST', body: { version_id: v.id } });
        toast(`Tests ${r.result}: ${r.report.passed}/${r.report.total}`, r.result === 'passed' ? 'ok' : 'bad');
        tabState = 'tests';
      } else if (a === 'submit') {
        const r = await api(`/integrations/${i.id}/submit`, { method: 'POST', body: { version_id: v.id } });
        toast('Submitted for approval', 'ok');
        return go(`#/approvals/${r.approval_id}`);
      } else if (a === 'revise') {
        const r = await api(`/integrations/${i.id}/revise`, { method: 'POST', body: { version_id: v.id } });
        tabState = 'configuration';
        return go(`#/integrations/${i.id}/${r.id}`);
      } else if (a === 'disable') {
        if (!(await confirmBox('Disable integration', 'Its tools stop executing immediately (workflows using them will fail).', 'Disable', 'danger-solid'))) { t.disabled = false; return; }
        await api(`/admin/integrations/${i.id}/disable`, { method: 'POST' });
      } else if (a === 'enable') {
        await api(`/admin/integrations/${i.id}/enable`, { method: 'POST' });
      } else if (a === 'connect') {
        await connectModal({ name: auth.credential_ref, auth_type: auth.type, header: auth.header_name });
        await api(`/integrations/${i.id}/validate`, { method: 'POST', body: { version_id: v.id } });
      }
      route();
    } catch (err) { toast(err.message, 'bad'); t.disabled = false; }
  });
}

export function diffHtml(changes) {
  if (!changes.length) return '<p class="muted">No differences.</p>';
  const s = (x) => esc(typeof x === 'string' ? x : JSON.stringify(x));
  return changes.map((c) => `<div class="diff-row">${badge(c.change === 'added' ? 'ok' : c.change === 'removed' ? 'critical' : 'medium', c.change)}<div><div class="p">${esc(c.path)}</div>
    ${c.before !== undefined ? `<del>${s(c.before)}</del> ` : ''}${c.after !== undefined ? `<ins>${s(c.after)}</ins>` : ''}</div></div>`).join('');
}


function guideHtml(g) {
  let collapsed = false;
  try { collapsed = localStorage.getItem('hedes-guide-collapsed') === '1'; } catch (e) { /* ignore */ }
  return `<div class="card guide ${collapsed ? 'collapsed' : ''}" id="guide"><div class="card-head"><h2>Setup guide</h2><span class="muted small">${g.current < g.steps.length ? `Next: ${esc(g.steps[g.current].title)}` : 'All steps complete'}</span>
      <span class="spacer"></span><button class="btn sm ghost" data-a="guide">Show / hide</button></div>
    <ol class="steps">${g.steps.map((s, i) => `<li class="${esc(s.state)}"><span class="num">${s.state === 'done' ? '✓' : i + 1}</span><div class="grow"><b>${esc(s.title)}</b><div class="small muted">${esc(s.detail)}</div>
      ${s.state === 'current' && s.href ? `<a class="btn sm ${s.key === 'approve' ? '' : 'primary'}" href="${esc(s.href)}" style="margin-top:6px">${esc(s.action)}</a>${s.blocked ? `<span class="small muted" style="margin-left:8px">${esc(s.blocked)}</span>` : ''}` : ''}</div></li>`).join('')}</ol></div>`;
}

async function renderSources({ el }) {
  const templates = await api('/source-templates');
  let chosen = null;
  const card = (t) => `<button class="tpl" data-tpl="${esc(t.id)}"><div class="row between"><b>${esc(t.title)}</b>${t.installed ? badge('ok', 'added') : ''}</div>
    <div class="small muted">${esc(t.summary)}</div><div class="small" style="margin-top:8px"><span class="badge">${esc(t.category)}</span> <span class="badge">${esc(t.auth_type === 'none' ? 'no login' : t.auth_type)}</span>
    ${[...new Set(t.operations.map((o) => o.pagination))].filter((p) => p !== 'none').map((p) => `<span class="badge">${esc(p)} paging</span>`).join(' ')}</div></button>`;
  el.innerHTML = `<div class="page">
    <div class="crumb"><a href="#/integrations">Integrations</a> / Add data source</div>
    ${pageHead('db', 'Add a data source', 'Pick a real service. A form fills in the configuration, the platform validates and tests it, then an administrator approves it. No code is written and nothing is mocked.')}
    <div class="tpl-grid">${templates.map(card).join('')}</div>
    <div id="tpl-form"></div>
    <p class="muted small" style="margin-top:14px">Need something else? <a class="link" href="#/integrations/new">Paste or import a configuration</a>, or <a class="link" href="#/studio/new">describe it to the AI</a>.</p></div>`;
  const form = $('#tpl-form', el);
  el.addEventListener('click', async (e) => {
    const b = e.target.closest('[data-tpl]');
    if (!b) return;
    chosen = templates.find((t) => t.id === b.dataset.tpl);
    el.querySelectorAll('[data-tpl]').forEach((x) => x.classList.toggle('on', x === b));
    const c = chosen.credential;
    form.innerHTML = `<div class="card card-pad" style="margin-top:16px"><div class="row between"><h2>${esc(chosen.title)}</h2><a class="link small" href="${esc(chosen.docs)}" target="_blank" rel="noopener">API docs ↗</a></div>
      <p class="muted small" style="margin-top:4px">${esc(chosen.free_tier)}</p>
      <h4 class="small muted">WHAT YOU GET</h4>${chosen.operations.map((o) => `<div class="op-row small"><span class="badge accent">${esc(o.entity)}</span><span class="grow"><b>${esc(o.id)}</b> — ${esc(o.description)}</span><span class="muted">${esc(o.pagination)} paging</span></div>`).join('')}
      <h4 class="small muted" style="margin-top:14px">STEPS</h4><ol class="small guide-list">
        ${(chosen.setup || []).map((s) => `<li>${esc(s)}</li>`).join('')}
        <li>Press <b>Create draft</b> below. The platform validates it and runs the safe tests.</li>
        ${c ? `<li>Connect the credential <code>${esc(c.name)}</code> (${esc(c.type)}): ${esc(c.how)}</li>` : ''}
        <li>Submit for review; an administrator approves. The source then starts running on its schedule (see Data Sources).</li></ol>
      ${(chosen.variables || []).length ? `<h4 class="small muted" style="margin-top:14px">YOUR DETAILS</h4><div class="grid-2">${chosen.variables.map((v) => `<div class="field"><label>${esc(v.label)}</label>
        <input type="text" data-var="${esc(v.name)}" value="${esc(v.default || '')}" placeholder="${esc(v.placeholder || '')}"><div class="hint">${esc(v.help || '')}</div></div>`).join('')}</div>` : ''}
      <div class="row end"><button class="btn" id="tpl-ask">${icon('spark', 14)} Ask the AI about this</button><button class="btn primary" id="tpl-create">Create draft</button></div></div>`;
    form.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  });
  el.addEventListener('click', async (e) => {
    if (e.target.closest('#tpl-ask')) return window.dispatchEvent(new CustomEvent('hedes:ask', { detail: { text: `Explain the ${chosen.title} data source: what it pulls, what credential I need and how to set it up.` } }));
    const b = e.target.closest('#tpl-create');
    if (!b) return;
    const variables = Object.fromEntries([...el.querySelectorAll('[data-var]')].map((i) => [i.dataset.var, i.value.trim()]));
    b.disabled = true;
    try {
      const r = await api(`/source-templates/${chosen.id}/create`, { method: 'POST', body: { variables } });
      toast(r.validation.valid ? `Draft created${r.tests ? ` · tests ${r.tests.result}` : ''}` : 'Created with validation errors', r.validation.valid ? 'ok' : 'bad');
      go(`#/integrations/${r.integration.id}`);
    } catch (err) { toast(err.message, 'bad'); b.disabled = false; }
  });
}
