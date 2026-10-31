import { $, api, esc, icon, badge, agoCell, pageHead, emptyState, modal, toast, json, formData } from '../lib.js';
import { go } from '../app.js';

const OPS = ['eq', 'neq', 'contains', 'not_contains', 'exists', 'not_exists', 'gt', 'lt', 'in'];

export async function render(ctx) {
  if (ctx.params[0]) return renderEditor(ctx);
  const rows = await api('/workflows');
  ctx.el.innerHTML = `<div class="page">
    ${pageHead('flow', 'Workflows', 'Chain published tools into automations with triggers, conditions and step outputs. Runs execute as the workflow owner and every step is audited.',
      `<button class="btn" id="blank">${icon('plus', 16)} Blank workflow</button><button class="btn primary" id="plan">${icon('spark', 16)} Describe with AI</button>`)}
    <div class="card">${rows.filter((w) => !w.name.startsWith('[archived]')).length ? `<div class="table-scroll"><table><thead><tr><th>Workflow</th><th>Trigger</th><th>Steps</th><th>Status</th><th>Last run</th><th>Owner</th></tr></thead><tbody>
      ${rows.filter((w) => !w.name.startsWith('[archived]')).map((w) => `<tr class="clickable" data-go="#/workflows/${w.id}"><td><b>${esc(w.name)}</b><div class="small muted">${esc(w.description || '')}</div></td>
        <td>${badge('info', w.trigger.type || 'manual')}</td><td>${w.nodes}</td><td>${badge(w.status)}</td><td>${w.last_run ? `${badge(w.last_run.status)} ${agoCell(w.last_run.at)}` : '<span class="muted">never</span>'}</td><td>${esc(w.owner)}</td></tr>`).join('')}
    </tbody></table></div>` : emptyState('flow', 'No workflows yet', 'Describe an automation and the planning agent drafts the graph from your published tools.')}</div></div>`;
  ctx.el.addEventListener('click', (e) => { const r = e.target.closest('[data-go]'); if (r) go(r.dataset.go); });
  $('#blank', ctx.el).addEventListener('click', async () => {
    const tools = await api('/tools');
    const t = tools[0];
    const w = await api('/workflows', { method: 'POST', body: { name: 'New workflow', graph: { trigger: { type: 'manual', sample: {} }, nodes: [{ id: 'n1', tool: t ? t.name : 'notify.send_message', inputs: {} }] } } });
    go(`#/workflows/${w.id}`);
  });
  $('#plan', ctx.el).addEventListener('click', async () => {
    const r = await modal({ title: 'Describe the automation', wide: true,
      body: `<div class="field"><label>What should happen?</label><textarea name="d" placeholder="When a webhook arrives with priority high, create a TaskFlow task and notify the security-team channel in Notify with the task id."></textarea></div>
        <div class="field"><label>Workflow name</label><input type="text" name="n" value="Automation"></div>`,
      actions: [{ label: 'Cancel', value: null }, { label: 'Draft workflow', cls: 'primary', handler: async (root) => {
        const f = formData(root);
        const plan = await api('/workflows/plan', { method: 'POST', body: { description: f.d } });
        return api('/workflows', { method: 'POST', body: { name: f.n || 'Automation', description: f.d, graph: plan.graph } });
      } }] });
    if (r) { toast('Draft created — review and validate it', 'ok'); go(`#/workflows/${r.id}`); }
  });
}

function layout(graph) {
  const deps = Object.fromEntries(graph.nodes.map((n) => [n.id, n.depends_on || []]));
  const depth = {};
  const d = (id, seen = new Set()) => {
    if (depth[id] !== undefined) return depth[id];
    if (seen.has(id)) return 0;
    seen.add(id);
    depth[id] = 1 + Math.max(0, ...deps[id].filter((x) => deps[x]).map((x) => d(x, seen)));
    return depth[id];
  };
  graph.nodes.forEach((n) => d(n.id));
  const cols = {};
  const pos = { __trigger: { x: 24, y: 24 } };
  graph.nodes.forEach((n) => {
    const c = depth[n.id];
    cols[c] = (cols[c] || 0) + 1;
    pos[n.id] = { x: 24 + c * 260, y: 24 + (cols[c] - 1) * 120 };
  });
  return pos;
}

async function renderEditor({ el, params }) {
  let wf = await api(`/workflows/${params[0]}`);
  let graph = JSON.parse(JSON.stringify(wf.graph));
  let sel = graph.nodes[0]?.id || '__trigger';
  let lastRun = null;
  const tools = wf.tools;
  const events = wf.events || {};

  const stepStatus = () => Object.fromEntries((lastRun?.steps || []).map((s) => [s.node_id, s.status]));

  function canvas() {
    const pos = layout(graph);
    const st = stepStatus();
    const w = Math.max(...Object.values(pos).map((p) => p.x)) + 260;
    const h = Math.max(...Object.values(pos).map((p) => p.y)) + 120;
    const edge = (a, b) => `<path d="M${a.x + 220} ${a.y + 34} C ${a.x + 245} ${a.y + 34}, ${b.x - 25} ${b.y + 34}, ${b.x} ${b.y + 34}"/>`;
    const edges = graph.nodes.flatMap((n) => ((n.depends_on || []).length ? n.depends_on.filter((x) => pos[x]).map((x) => edge(pos[x], pos[n.id])) : [edge(pos.__trigger, pos[n.id])])).join('');
    return `<div class="wf-canvas" style="height:${Math.max(340, h + 20)}px"><svg class="edges" width="${w}" height="${h}">${edges}</svg>
      <div class="wf-node trigger ${sel === '__trigger' ? 'sel' : ''}" data-node="__trigger" style="left:${pos.__trigger.x}px;top:${pos.__trigger.y}px"><b>${icon('play', 13)} Trigger</b><div class="t">${esc(graph.trigger.type)}${graph.trigger.type === 'schedule' ? ` · every ${esc(graph.trigger.interval_minutes || 60)} min` : ''}${graph.trigger.type === 'automatic' ? ` · on ${esc(graph.trigger.event || '?')}${(graph.trigger.conditions || []).length ? ` · ${graph.trigger.conditions.length} condition(s)` : ''}` : ''}</div></div>
      ${graph.nodes.map((n) => `<div class="wf-node ${sel === n.id ? 'sel' : ''} ${st[n.id] ? `st-${st[n.id]}` : ''}" data-node="${esc(n.id)}" style="left:${pos[n.id].x}px;top:${pos[n.id].y}px">
        <b>${esc(n.label || n.id)}</b><div class="t">${esc(n.tool)}</div>${(n.when || []).length ? `<div class="cond">if ${n.when.map((c) => `${esc(c.left)} ${esc(c.op)} ${esc(c.right ?? '')}`).join(' and ')}</div>` : ''}${st[n.id] ? `<div style="margin-top:4px">${badge(st[n.id])}</div>` : ''}</div>`).join('')}
    </div>`;
  }

  function inspector() {
    if (sel === '__trigger') {
      const t = graph.trigger;
      const ev = events[t.event];
      return `<div class="card-body"><h3 style="font-size:15px;margin-bottom:10px">Trigger</h3>
        <div class="field"><label>Type</label><select id="tt">${['manual', 'webhook', 'schedule', 'automatic'].map((x) => `<option ${x === t.type ? 'selected' : ''}>${x}</option>`).join('')}</select></div>
        ${t.type === 'schedule' ? `<div class="field"><label>Every N minutes</label><input type="number" id="ti" min="1" value="${esc(t.interval_minutes || 60)}"></div>` : ''}
        ${t.type === 'automatic' ? `<div class="field"><label>Start when</label><select id="tev">${Object.entries(events).map(([k, e]) => `<option value="${esc(k)}" ${k === t.event ? 'selected' : ''}>${esc(e.description)}</option>`).join('')}</select>
          ${ev ? `<div class="hint">Event fields: ${ev.fields.map((f) => `<code>\${trigger.${esc(f)}}</code>`).join(' ')}</div>` : ''}</div>
          <div class="field"><label>Only if (all must hold)</label><div id="tconds">${(t.conditions || []).map((c, i) => condRow(c, i)).join('')}</div><button class="btn sm" id="add-tcond">${icon('plus', 13)} Condition</button>
          <div class="hint">Runs once the workflow is active. A failure caused by an automatic run never starts another one, and the same event starts this workflow at most once every 5 minutes.</div></div>` : ''}
        ${t.type === 'webhook' ? `<div class="field"><label>Webhook URL</label><pre style="white-space:pre-wrap;word-break:break-all">${esc(wf.webhook_url)}</pre>
          <div class="hint">POST JSON with headers <code>X-Integra-Timestamp</code> (unix seconds), <code>X-Integra-Signature: sha256=HMAC(secret, "&lt;timestamp&gt;.&lt;body&gt;")</code> and an optional <code>X-Integra-Delivery</code> id. Requests older than 5 minutes or replayed ids are rejected. The secret is shown once on first activation.</div></div>` : ''}
        <div class="field"><label>Sample payload (used for manual runs and dry runs)</label><textarea class="code" id="ts" style="min-height:160px">${esc(JSON.stringify(t.sample || {}, null, 2))}</textarea>
          <div class="hint">Reference fields as <code>\${trigger.field}</code>.</div></div></div>`;
    }
    const n = graph.nodes.find((x) => x.id === sel);
    if (!n) return '';
    const tool = tools[n.tool];
    const props = tool?.input_schema?.properties || {};
    const req = new Set(tool?.input_schema?.required || []);
    const keys = [...new Set([...Object.keys(props), ...Object.keys(n.inputs || {})])];
    const others = graph.nodes.filter((x) => x.id !== n.id);
    return `<div class="card-body"><div class="row between"><h3 style="font-size:15px">Step <span class="mono">${esc(n.id)}</span></h3><button class="btn sm danger" id="del-node">${icon('trash', 13)}</button></div>
      <div class="field" style="margin-top:10px"><label>Label</label><input type="text" id="nl" value="${esc(n.label || '')}"></div>
      <div class="field"><label>Tool</label><select id="nt">${Object.keys(tools).map((k) => `<option ${k === n.tool ? 'selected' : ''}>${esc(k)}</option>`).join('')}${tools[n.tool] ? '' : `<option selected>${esc(n.tool)} (unavailable)</option>`}</select>
        ${tool ? `<div class="hint">Outputs: ${(tool.output_fields || []).map((f) => `<code>\${steps.${esc(n.id)}.output.${esc(f)}}</code>`).join(' ')}</div>` : '<div class="hint" style="color:var(--bad)">Not published or not available to the owner.</div>'}</div>
      <label>Inputs</label>${keys.map((k) => inputField(k, n.inputs?.[k], req.has(k))).join('') || '<p class="muted small">No inputs.</p>'}
      <div class="field"><label>Runs after</label>${others.length ? others.map((o) => `<label class="check small"><input type="checkbox" data-dep="${esc(o.id)}" ${(n.depends_on || []).includes(o.id) ? 'checked' : ''}> ${esc(o.label || o.id)}</label>`).join('') : '<p class="muted small">The trigger.</p>'}</div>
      <div class="field"><label>Only if (all must hold)</label><div id="conds">${(n.when || []).map((c, i) => condRow(c, i)).join('')}</div><button class="btn sm" id="add-cond">${icon('plus', 13)} Condition</button></div>
      <div class="field"><label>On failure</label><select id="nf"><option value="stop" ${n.on_failure !== 'continue' ? 'selected' : ''}>Stop the run</option><option value="continue" ${n.on_failure === 'continue' ? 'selected' : ''}>Continue other steps</option></select>
        <div class="hint">Create/update calls are never retried automatically unless the API is idempotent.</div></div></div>`;
  }
  // Message-like inputs (and any value with line breaks) get a multi-line box: a one-line field would drop the breaks.
  const MULTILINE = /^(text|message|body|description|comment|content|summary|blocks)$/i;
  const inputField = (k, v, required) => {
    const value = typeof v === 'string' || v === undefined ? (v ?? '') : JSON.stringify(v);
    const attrs = `data-in="${esc(k)}" placeholder="literal or \${trigger.x}"`;
    return `<div class="field"><label class="small muted">${esc(k)}${required ? ' *' : ''}</label>${MULTILINE.test(k) || value.includes('\n')
      ? `<textarea ${attrs} rows="4" style="min-height:0">${esc(value)}</textarea>` : `<input type="text" ${attrs} value="${esc(value)}">`}</div>`;
  };
  const condRow = (c, i) => `<div class="row" style="margin-bottom:6px" data-cond="${i}"><input type="text" data-c="left" value="${esc(c.left)}" style="flex:2" placeholder="\${trigger.labels}">
    <select data-c="op" style="flex:1">${OPS.map((o) => `<option ${o === c.op ? 'selected' : ''}>${o}</option>`).join('')}</select>
    <input type="text" data-c="right" value="${esc(c.right ?? '')}" style="flex:1.4" placeholder="value"><button class="btn sm ghost" data-rmcond="${i}">${icon('x', 13)}</button></div>`;

  const condsIn = (box) => [...(box?.querySelectorAll('[data-cond]') || [])]
    .map((r) => ({ left: r.querySelector('[data-c=left]').value, op: r.querySelector('[data-c=op]').value, right: r.querySelector('[data-c=right]').value }))
    .filter((c) => c.left).map((c) => (['exists', 'not_exists'].includes(c.op) ? { left: c.left, op: c.op } : c));

  function collect() {
    const root = $('#insp', el);
    if (!root) return;
    if (sel === '__trigger') {
      graph.trigger.type = $('#tt', root).value;
      if ($('#ti', root)) graph.trigger.interval_minutes = Number($('#ti', root).value) || 60;
      if (graph.trigger.type !== 'schedule') delete graph.trigger.interval_minutes;
      if (graph.trigger.type === 'automatic') {
        const event = $('#tev', root)?.value || graph.trigger.event || Object.keys(events)[0];
        // switching event: start from that event's sample so dry runs show realistic fields
        if (event !== graph.trigger.event) $('#ts', root).value = JSON.stringify(events[event]?.sample || {}, null, 2);
        graph.trigger.event = event;
        graph.trigger.conditions = condsIn($('#tconds', root));
        if (!graph.trigger.conditions.length) delete graph.trigger.conditions;
      } else {
        delete graph.trigger.event;
        delete graph.trigger.conditions;
      }
      try { graph.trigger.sample = JSON.parse($('#ts', root).value || '{}'); } catch (e) { throw new Error('Sample payload must be valid JSON'); }
      return;
    }
    const n = graph.nodes.find((x) => x.id === sel);
    if (!n) return;
    n.label = $('#nl', root).value || undefined;
    n.tool = $('#nt', root).value.replace(/ \(unavailable\)$/, '');
    n.inputs = {};
    root.querySelectorAll('[data-in]').forEach((i) => {
      const v = i.value.trim();
      if (v === '') return;
      try { n.inputs[i.dataset.in] = /^[[{]/.test(v) ? JSON.parse(v) : v; } catch (e) { n.inputs[i.dataset.in] = v; }
    });
    n.depends_on = [...root.querySelectorAll('[data-dep]:checked')].map((x) => x.dataset.dep);
    if (!n.depends_on.length) delete n.depends_on;
    n.when = condsIn($('#conds', root));
    if (!n.when.length) delete n.when;
    n.on_failure = $('#nf', root).value;
    if (!n.label) delete n.label;
  }

  function paint() {
    el.innerHTML = `<div class="page wide">
      <div class="crumb"><a href="#/workflows">Workflows</a> / ${esc(wf.name)}</div>
      <div class="page-head"><div class="ph-title"><div class="ph-ico">${icon('flow', 20)}</div><div><input type="text" id="wname" value="${esc(wf.name)}" style="font-size:20px;font-weight:600;padding:4px 8px;width:min(520px,70vw)">
        <p>${badge(wf.status)} v${wf.version} · owner ${esc(wf.owner)}</p></div></div>
        <div class="row"><button class="btn" id="save">Save</button><button class="btn" id="dry">${icon('eye', 15)} Dry run</button><button class="btn" id="run">${icon('play', 15)} Run now</button>
          ${wf.status === 'active' ? '<button class="btn danger" id="pause">Pause</button>' : '<button class="btn primary" id="activate">Activate</button>'}<button class="btn ghost" id="archive" title="Archive">${icon('trash', 15)}</button></div></div>
      <div id="val">${valBox(wf.validation)}</div>
      <div class="split" style="grid-template-columns:minmax(0,1.6fr) minmax(320px,1fr)">
        <div class="stack"><div class="card"><div class="card-head"><h2>Graph</h2><span class="spacer"></span><button class="btn sm" id="add-node">${icon('plus', 13)} Add step</button></div>${canvas()}</div>
          <div class="card"><div class="card-head"><h2>Run history</h2></div><div class="card-body" id="runs"><span class="spinner"></span></div></div></div>
        <div class="card" id="insp">${inspector()}</div></div></div>`;
    loadRuns();
  }
  const valBox = (v) => (!v ? '' : v.valid && !v.warnings.length ? '<div class="callout ok small" style="margin-bottom:14px">Valid workflow — every step uses a published tool you may run.</div>'
    : `<div class="callout ${v.valid ? 'warn' : 'bad'} small" style="margin-bottom:14px"><ul style="margin:0;padding-left:18px">${[...v.errors, ...v.warnings].map((e) => `<li><code>${esc(e.path)}</code> ${esc(e.message)}</li>`).join('')}</ul></div>`);

  async function loadRuns() {
    const runs = await api(`/workflows/${wf.id}/runs`);
    const box = $('#runs', el);
    if (!box) return;
    box.innerHTML = runs.length ? runs.slice(0, 10).map((r, i) => `<details ${i === 0 ? 'open' : ''} style="border-bottom:1px solid var(--line);padding:8px 0"><summary class="row" style="cursor:pointer">${badge(r.status)} ${r.dry_run ? badge('planned', 'dry run') : ''} <span class="small">${esc(r.trigger)} · ${agoCell(r.started_at)}</span>${r.error ? `<span class="small muted">${esc(r.error)}</span>` : ''}</summary>
      ${r.steps.map((s) => `<div class="finding"><span>${badge(s.status)}</span><div class="small"><b class="mono">${esc(s.node_id)}</b>${s.error ? ` — ${esc(s.error)}` : ''}${s.execution_id ? ` · <a class="link" href="#/monitoring/execution/${s.execution_id}">execution</a>` : ''}
        ${s.output?.inputs ? `<details><summary class="muted" style="cursor:pointer">inputs / result</summary>${json(s.output)}</details>` : ''}</div></div>`).join('')}</details>`).join('')
      : '<p class="muted small">No runs yet. Try a dry run — it renders each request without calling any API.</p>';
    lastRun = runs[0] || null;
  }

  async function save() {
    collect();
    wf = await api(`/workflows/${wf.id}`, { method: 'PUT', body: { graph, name: $('#wname', el).value } });
    graph = JSON.parse(JSON.stringify(wf.graph));
    return wf;
  }

  el.addEventListener('click', async (e) => {
    const t = e.target.closest('[data-node], #save, #dry, #run, #activate, #pause, #archive, #add-node, #del-node, #add-cond, #add-tcond, [data-rmcond]');
    if (!t) return;
    try {
      if (t.dataset.node) { collect(); sel = t.dataset.node; return paint(); }
      if (t.id === 'add-node') {
        collect();
        let k = graph.nodes.length + 1;
        while (graph.nodes.some((n) => n.id === `n${k}`)) k++;
        graph.nodes.push({ id: `n${k}`, tool: Object.keys(tools)[0] || 'notify.send_message', inputs: {}, depends_on: sel !== '__trigger' ? [sel] : undefined });
        sel = `n${k}`;
        return paint();
      }
      if (t.id === 'del-node') {
        graph.nodes = graph.nodes.filter((n) => n.id !== sel);
        graph.nodes.forEach((n) => { if (n.depends_on) n.depends_on = n.depends_on.filter((d) => d !== sel); });
        sel = '__trigger';
        return paint();
      }
      if (t.id === 'add-cond') { collect(); const n = graph.nodes.find((x) => x.id === sel); n.when = [...(n.when || []), { left: '${trigger.}', op: 'eq', right: '' }]; return paint(); }
      if (t.id === 'add-tcond') { collect(); graph.trigger.conditions = [...(graph.trigger.conditions || []), { left: '${trigger.}', op: 'eq', right: '' }]; return paint(); }
      if (t.dataset.rmcond !== undefined) { collect(); (sel === '__trigger' ? graph.trigger.conditions : graph.nodes.find((x) => x.id === sel).when).splice(+t.dataset.rmcond, 1); return paint(); }
      t.disabled = true;
      if (t.id === 'save') { await save(); toast('Saved', 'ok'); return paint(); }
      if (t.id === 'dry' || t.id === 'run') {
        await save();
        const r = await api(`/workflows/${wf.id}/execute`, { method: 'POST', body: { dry_run: t.id === 'dry' } });
        toast(`${t.id === 'dry' ? 'Dry run' : 'Run'} ${r.status}`, r.status === 'succeeded' ? 'ok' : 'bad');
        lastRun = r;
        return paint();
      }
      if (t.id === 'activate') {
        await save();
        const r = await api(`/workflows/${wf.id}/activate`, { method: 'POST' });
        wf = r;
        if (r.webhook_secret) {
          await modal({ title: 'Webhook signing secret', body: `<p class="small" style="margin-top:0">Store it in the sending system now; it is never shown again.</p><pre style="white-space:pre-wrap;word-break:break-all">${esc(r.webhook_secret)}</pre>
            <p class="small muted">URL: <span class="mono">${esc(r.webhook_url)}</span></p>`, actions: [{ label: 'Done', cls: 'primary', value: true }] });
        }
        toast('Workflow active', 'ok');
        return paint();
      }
      if (t.id === 'pause') { wf = await api(`/workflows/${wf.id}/stop`, { method: 'POST' }); return paint(); }
      if (t.id === 'archive') { await api(`/workflows/${wf.id}`, { method: 'DELETE' }); return go('#/workflows'); }
    } catch (err) {
      toast(err.message, 'bad');
      t.disabled = false;
      if (/workflow/i.test(err.message)) { wf = await api(`/workflows/${wf.id}`); $('#val', el).innerHTML = valBox(wf.validation); }
    }
  });
  el.addEventListener('change', (e) => { if (['tt', 'tev', 'nt'].includes(e.target.id)) { collect(); paint(); } });
  paint();
}

