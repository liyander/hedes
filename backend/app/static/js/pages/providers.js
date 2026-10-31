import { $, api, esc, icon, badge, agoCell, pageHead, modal, toast, confirmBox, formData } from '../lib.js';
import { isOwner, isAdmin, route } from '../app.js';

const TASKS = ['general', 'assistant', 'conversation', 'requirements', 'api_discovery', 'config_generation', 'security_review', 'test_generation', 'repair', 'workflow_planning', 'error_explanation'];
const TYPES = { openai_compatible: 'OpenAI-compatible endpoint', anthropic: 'Anthropic Claude', gemini: 'Google Gemini', bedrock: 'Amazon Bedrock' };
const REGIONS = ['us-east-1', 'us-west-2', 'eu-west-1', 'eu-central-1', 'ap-southeast-2', 'ap-northeast-1'];

// Task checkboxes with a "Select all" box. One delegated listener keeps it in sync in every dialog.
function purposeBoxes(selected) {
  const all = TASKS.every((t) => selected.has(t));
  return `<div class="row"><label class="check small"><input type="checkbox" data-all-purposes ${all ? 'checked' : ''}> <b>Select all</b></label>
    ${TASKS.map((t) => `<label class="check small"><input type="checkbox" data-purpose="${t}" ${selected.has(t) ? 'checked' : ''}> ${t.replace(/_/g, ' ')}</label>`).join('')}</div>`;
}
document.addEventListener('change', (e) => {
  const scope = e.target.closest('.modal');
  if (!scope) return;
  if (e.target.matches('[data-all-purposes]')) scope.querySelectorAll('[data-purpose]').forEach((c) => { c.checked = e.target.checked; });
  else if (e.target.matches('[data-purpose]')) {
    const all = scope.querySelector('[data-all-purposes]');
    if (all) all.checked = [...scope.querySelectorAll('[data-purpose]')].every((c) => c.checked);
  }
});

// Credential fields differ by provider type; Bedrock takes AWS access keys or a Bedrock API key.
function credFields(type, keyLabel = 'API key') {
  if (type === 'bedrock') return `<div class="field"><label>Authentication</label><select name="aws_mode"><option value="keys">AWS access keys (IAM user / role keys)</option><option value="bearer">Bedrock API key</option></select></div>
    <div data-mode="keys"><div class="grid-2"><div class="field"><label>Access key ID</label><input type="text" name="aws_access_key_id" autocomplete="off" placeholder="AKIA… or env:AWS_ACCESS_KEY_ID"></div>
      <div class="field"><label>Secret access key</label><input type="password" name="aws_secret_access_key" autocomplete="new-password"></div></div>
      <div class="field"><label>Session token (optional, for temporary credentials)</label><input type="password" name="aws_session_token" autocomplete="new-password"></div></div>
    <div data-mode="bearer" hidden><div class="field"><label>Bedrock API key</label><input type="password" name="api_key" autocomplete="new-password"></div></div>
    <div class="hint" style="margin:-4px 0 10px">Stored encrypted. Or type <code>env:NAME</code> to read a server environment variable. The IAM identity needs <code>bedrock:InvokeModel</code>.</div>`;
  return `<div class="field"><label>${esc(keyLabel)}</label><input type="password" name="api_key" autocomplete="new-password"><div class="hint">Stored encrypted in the vault. Leave blank for local servers.</div></div>`;
}
const credBody = (root, type) => {
  const f = formData(root);
  if (type !== 'bedrock') return { api_key: f.api_key || '' };
  return f.aws_mode === 'bearer' ? { api_key: f.api_key || '' } : { aws_access_key_id: f.aws_access_key_id || '', aws_secret_access_key: f.aws_secret_access_key || '', aws_session_token: f.aws_session_token || '' };
};
const wireMode = (root) => {
  const sel = root.querySelector('[name=aws_mode]');
  if (!sel) return;
  const apply = () => root.querySelectorAll('[data-mode]').forEach((d) => { d.hidden = d.dataset.mode !== sel.value; });
  sel.addEventListener('change', apply); apply();
};

function modelForm(m = {}) {
  const p = new Set(m.purposes || ['general']);
  return `<div class="grid-2"><div class="field"><label>Model id</label><input type="text" name="model_id" value="${esc(m.model_id || '')}" required></div>
    <div class="field"><label>Label</label><input type="text" name="label" value="${esc(m.label || '')}"></div>
    <div class="field"><label>Priority (lower is tried first)</label><input type="number" name="priority" value="${m.priority ?? 50}"></div>
    <div class="field"><label>Context window (tokens)</label><input type="number" name="context_tokens" value="${m.capabilities?.context_tokens ?? 128000}"></div>
    <div class="field"><label>Cost $ / 1M input tokens</label><input type="number" step="0.01" name="cost_input_per_mtok" value="${m.cost_input_per_mtok ?? 0}"></div>
    <div class="field"><label>Cost $ / 1M output tokens</label><input type="number" step="0.01" name="cost_output_per_mtok" value="${m.cost_output_per_mtok ?? 0}"></div></div>
    <label class="check"><input type="checkbox" name="json_mode" ${m.capabilities?.json_mode !== false ? 'checked' : ''}> Reliable structured (JSON) output</label>
    <label class="check"><input type="checkbox" name="allow_confidential" ${m.allow_confidential !== false ? 'checked' : ''}> May receive confidential material (API docs, configs, error details)</label>
    <label class="check"><input type="checkbox" name="enabled" ${m.enabled !== false ? 'checked' : ''}> Enabled</label>
    <div class="field" style="margin-top:12px"><label>Preferred for tasks</label>${purposeBoxes(p)}</div>`;
}
const modelBody = (root) => {
  const f = formData(root);
  return { model_id: f.model_id, label: f.label, priority: f.priority ?? 50, context_tokens: f.context_tokens ?? 128000,
    cost_input_per_mtok: f.cost_input_per_mtok ?? 0, cost_output_per_mtok: f.cost_output_per_mtok ?? 0, json_mode: f.json_mode,
    allow_confidential: f.allow_confidential, enabled: f.enabled, purposes: [...root.querySelectorAll('[data-purpose]:checked')].map((x) => x.dataset.purpose) };
};

export async function render({ el }) {
  const [providers, routing, usage] = await Promise.all([api('/ai/providers'), api('/ai/routing'), isAdmin() ? api('/ai/usage') : null]);
  const owner = isOwner();
  el.innerHTML = `<div class="page wide">
    ${pageHead('cpu', 'AI Providers', 'Connect Amazon Bedrock, NVIDIA NIM, Groq, xAI Grok, Gemini, OpenAI, Anthropic, OpenRouter, a local Ollama or any OpenAI-compatible endpoint. The gateway routes each task (the AI assistant, creating integrations, ...) to a suitable model by capability, data-handling permission and budget, with automatic fallback.',
      owner ? `<button class="btn primary" id="add">${icon('plus', 16)} Add provider</button>` : '')}
    ${owner ? '' : `<div class="callout info" style="margin-bottom:18px">${icon('key', 14)} Only an <b>owner</b> can add or change AI providers and models. Ask an owner, or have one give you the owner role in <b>Users</b>.</div>`}
    <div class="stats">
      <div class="card stat accent"><div class="ico">${icon('cpu', 20)}</div><div><b>${providers.filter((p) => p.status === 'active').length}</b><span>Active providers</span></div></div>
      <div class="card stat"><div class="ico">${icon('spark', 20)}</div><div><b>${providers.reduce((n, p) => n + p.models.filter((m) => m.enabled).length, 0)}</b><span>Enabled models</span></div></div>
      <div class="card stat ${routing.month_spend >= routing.budget ? 'alert' : 'good'}"><div class="ico">${icon('pulse', 20)}</div><div><b>$${routing.month_spend.toFixed(3)}</b><span>Spent this month of $${routing.budget}</span></div></div>
      <div class="card stat"><div class="ico">${icon('history', 20)}</div><div><b>${usage ? usage.rows.reduce((n, r) => n + r.calls, 0) : '—'}</b><span>AI calls this month</span></div></div>
    </div>
    <div class="split" style="margin-top:18px">
      <div class="stack">${providers.map((p) => `<div class="card"><div class="card-head"><span class="dot ${p.status === 'active' ? 'on' : 'off'}"></span><div><h2>${esc(p.name)}</h2><div class="small muted">${esc(TYPES[p.provider_type] || 'Deterministic heuristic agents, no network')}${p.region ? ` · ${esc(p.region)}` : ''}${p.base_url ? ` · <span class="mono">${esc(p.base_url)}</span>` : ''}</div></div><span class="spacer"></span>
          ${p.id === routing.active_provider ? badge('ok', 'active') : ''}
          ${p.provider_type !== 'local' ? (p.has_key ? badge('ok', 'key in vault') : badge('medium', 'no key')) : badge('info', 'offline')}
          ${isAdmin() ? `<button class="btn sm" data-test="${p.id}">Test</button>` : ''}
          ${owner ? `<button class="btn sm" data-toggle="${p.id}" data-status="${p.status === 'active' ? 'disabled' : 'active'}">${p.status === 'active' ? 'Disable' : 'Enable'}</button>
            ${p.provider_type !== 'local' ? `<button class="btn sm" data-edit="${p.id}">Edit</button><button class="btn sm" data-key="${p.id}">Rotate key</button><button class="btn sm" data-addmodel="${p.id}">${icon('plus', 13)} Model</button><button class="btn sm danger" data-del="${p.id}">${icon('trash', 13)}</button>` : ''}` : ''}</div>
        ${p.last_error ? `<div class="callout bad small" style="margin:12px 18px 0">Last error: ${esc(p.last_error)}</div>` : ''}
        <div class="table-scroll"><table><thead><tr><th>Model</th><th>Preferred for</th><th>Priority</th><th>Confidential</th><th>Cost / 1M</th><th></th></tr></thead><tbody>
          ${p.models.map((m) => `<tr><td class="mono"><b>${esc(m.label || m.model_id)}</b> ${m.enabled ? '' : badge('disabled')}<div class="small muted">${esc(m.model_id)} · ${(m.capabilities?.context_tokens || 0).toLocaleString()} ctx${m.capabilities?.json_mode ? ' · JSON' : ''}</div></td>
            <td class="small">${(m.purposes || []).map((x) => `<span class="badge">${esc(x)}</span>`).join(' ')}</td><td>${m.priority}</td><td>${m.allow_confidential ? badge('ok', 'allowed') : badge('medium', 'blocked')}</td>
            <td class="small">$${m.cost_input_per_mtok} / $${m.cost_output_per_mtok}</td><td>${owner ? `<button class="btn sm" data-editmodel='${esc(JSON.stringify(m))}'>Edit</button>` : ''}</td></tr>`).join('')}
        </tbody></table></div></div>`).join('')}</div>
      <div class="stack">
        <div class="card"><div class="card-head"><h2>Active provider</h2></div><div class="card-body">
          <div class="field"><label>Use for all AI tasks</label><select id="active-provider" ${isAdmin() ? '' : 'disabled'}>
            <option value="">Automatic (route by task, priority and budget)</option>
            ${providers.filter((p) => p.status === 'active').map((p) => `<option value="${esc(p.id)}" ${p.id === routing.active_provider ? 'selected' : ''}>${esc(p.name)}</option>`).join('')}</select></div>
          <label class="check small"><input type="checkbox" id="active-only" ${routing.active_only ? 'checked' : ''} ${isAdmin() && routing.active_provider ? '' : 'disabled'}> Use only this provider (no fallback to other providers; built-in rules remain the last resort)</label>
          <div class="hint">The chosen provider is tried first for every task. Switch any time; nothing else changes.</div></div></div>
        <div class="card"><div class="card-head"><h2>Task routing</h2><span class="muted small">first = tried first</span></div><div class="card-body">
          ${routing.tasks.map((t) => `<div style="padding:7px 0;border-bottom:1px solid var(--line)"><div class="row between"><b class="small">${esc(t.task.replace('_', ' '))}</b><span>${t.structured ? badge('info', 'JSON') : ''} ${t.confidential ? badge('medium', 'confidential') : ''}</span></div>
            <div class="small muted">${t.candidates.map((c, i) => `${i ? '→ ' : ''}${esc(c)}`).join(' ') || '<span style="color:var(--bad)">no eligible model</span>'}</div></div>`).join('')}</div></div>
        ${usage ? `<div class="card"><div class="card-head"><h2>Usage this month</h2></div><div class="card-body">
          ${usage.rows.length ? `<table><thead><tr><th>Model · task</th><th>Calls</th><th>Tokens</th><th>Cost</th></tr></thead><tbody>${usage.rows.map((r) => `<tr><td class="small"><span class="mono">${esc(r.model)}</span><div class="muted">${esc(r.task)}${r.failures ? ` · <span style="color:var(--bad)">${r.failures} failed</span>` : ''}</div></td><td>${r.calls}</td><td>${(r.prompt_tokens + r.completion_tokens).toLocaleString()}</td><td>$${r.cost}</td></tr>`).join('')}</tbody></table>` : '<p class="muted small">No AI calls yet.</p>'}
          ${usage.recent.filter((r) => !r.success).slice(0, 4).map((r) => `<div class="callout bad small" style="margin-top:8px">${esc(r.model)} · ${esc(r.task)} · ${agoCell(r.at)}<div class="muted">${esc(r.error)}</div></div>`).join('')}</div></div>` : ''}
      </div></div></div>`;

  const switchProvider = async () => {
    const id = $('#active-provider', el).value;
    try {
      await api('/ai/active-provider', { method: 'PUT', body: { provider_id: id, only: id ? $('#active-only', el).checked : false } });
      toast(id ? `Switched to ${providers.find((p) => p.id === id).name}` : 'Switched to automatic routing', 'ok');
    } catch (err) { toast(err.message, 'bad'); }
    route();
  };
  $('#active-provider', el)?.addEventListener('change', switchProvider);
  $('#active-only', el)?.addEventListener('change', switchProvider);

  $('#add', el)?.addEventListener('click', async () => {
    const presets = await api('/ai/provider-presets');
    let cur = presets[0];
    const fields = () => `<div class="callout info small" style="margin-bottom:12px">${esc(cur.note || 'Pick the endpoint to use.')} ${cur.docs ? `<a class="link" href="${esc(cur.docs)}" target="_blank" rel="noopener">Docs ↗</a>` : ''}</div>
      <div class="grid-2"><div class="field"><label>Display name</label><input type="text" name="name" value="${esc(cur.name)}" required></div>
        ${cur.provider_type === 'bedrock' ? `<div class="field"><label>AWS region</label><input type="text" name="region" value="us-east-1" list="regions" required><datalist id="regions">${REGIONS.map((r) => `<option value="${r}">`).join('')}</datalist></div>`
          : `<div class="field"><label>Base URL</label><input type="url" name="base_url" value="${esc(cur.base_url)}" placeholder="https://…/v1"></div>`}</div>
      ${credFields(cur.provider_type, cur.key_label)}
      <div class="field"><label>Model</label><div class="row"><input type="text" name="model_id" class="grow" required placeholder="${esc(cur.models[0] || 'model id')}" value="${esc(cur.models[0] || '')}">
        <button type="button" class="btn sm" id="fetch-models">${icon('refresh', 13)} Load models</button></div>
        <select id="model-pick" style="margin-top:8px" hidden></select>
        <div class="hint" id="model-hint">${cur.provider_type === 'bedrock' ? 'Fill in the credentials, then Load models to list the models and inference profiles available in that region (the IAM identity needs bedrock:ListFoundationModels). Newer models must be used through an inference profile (us.…, apac.…).' : 'Fill in the key, then Load models to pick from the models this endpoint offers, or type an id.'}</div></div>
      <div class="row" style="margin-bottom:12px"><button type="button" class="btn" id="test-config">${icon('play', 14)} Test connection</button><span class="small" id="test-result"></span></div>
      <div class="field"><label>Use it for</label>${purposeBoxes(new Set(['general']))}
        <div class="hint">"general" covers every task, including the AI assistant and creating integrations. Tick specific tasks to prefer this model for them.</div></div>`;
    const r = await modal({ title: 'Add AI provider', wide: true,
      body: `<div class="field"><label>Provider</label><select id="preset">${presets.map((p) => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join('')}</select></div><div id="pform">${fields()}</div>`,
      onOpen: (root) => {
        const paint = () => { root.querySelector('#pform').innerHTML = fields(); wireMode(root); };
        wireMode(root);
        root.addEventListener('change', (e) => { if (e.target.id === 'preset') { cur = presets.find((p) => p.id === e.target.value); paint(); } });
        const draft = () => {
          const f = formData(root);
          return { provider_type: cur.provider_type, base_url: f.base_url || '', region: f.region || '', model_id: (f.model_id || '').trim(), ...credBody(root, cur.provider_type) };
        };
        root.addEventListener('change', (e) => { if (e.target.id === 'model-pick' && e.target.value) root.querySelector('[name=model_id]').value = e.target.value; });
        root.addEventListener('click', async (e) => {
          const b = e.target.closest('#fetch-models, #test-config');
          if (!b) return;
          b.disabled = true;
          if (b.id === 'fetch-models') {
            const hint = root.querySelector('#model-hint');
            hint.textContent = 'Loading models…';
            try {
              const res = await api('/ai/providers/discover-models', { method: 'POST', body: draft() });
              const pick = root.querySelector('#model-pick');
              const current = root.querySelector('[name=model_id]').value;
              pick.innerHTML = `<option value="">Select a model (${res.models.length} available)</option>` +
                res.models.map((m) => `<option value="${esc(m)}" ${m === current ? 'selected' : ''}>${esc(m)}</option>`).join('');
              pick.hidden = !res.models.length;
              hint.textContent = res.models.length ? `${res.models.length} models found. Pick one, then Test connection.` : 'The endpoint returned no models. Type the model id instead.';
            } catch (err) { hint.textContent = err.message; toast(err.message, 'bad'); }
          } else {
            const out = root.querySelector('#test-result');
            out.innerHTML = '<span class="spinner"></span> Testing…';
            try {
              const r = await api('/ai/providers/test-config', { method: 'POST', body: draft() });
              out.innerHTML = r.ok ? `<span style="color:var(--ok)">✓ Connected: the model replied “${esc(r.reply)}” in ${r.latency_ms} ms</span>`
                : `<span style="color:var(--bad)">✗ ${esc(r.error)}</span>`;
            } catch (err) { out.innerHTML = `<span style="color:var(--bad)">✗ ${esc(err.message)}</span>`; }
          }
          b.disabled = false;
        });
      },
      actions: [{ label: 'Cancel', value: null }, { label: 'Add provider', cls: 'primary', handler: (root) => {
        const f = formData(root);
        if (!f.name || !f.model_id) { toast('Name and model id are required', 'bad'); return false; }
        return api('/ai/providers', { method: 'POST', body: { name: f.name, provider_type: cur.provider_type, base_url: f.base_url || '', region: f.region || '', model_id: f.model_id,
          ...credBody(root, cur.provider_type), purposes: [...root.querySelectorAll('[data-purpose]:checked')].map((x) => x.dataset.purpose) } });
      } }] });
    if (r) { toast('Provider added. Press Test to check it.', 'ok'); route(); }
  });

  el.addEventListener('click', async (e) => {
    const t = e.target.closest('[data-test],[data-toggle],[data-key],[data-edit],[data-addmodel],[data-editmodel],[data-del]');
    if (!t) return;
    try {
      if (t.dataset.test) {
        t.disabled = true;
        const r = await api(`/ai/providers/${t.dataset.test}/test`, { method: 'POST' });
        toast(r.ok ? `OK: ${r.reply}` : `Failed: ${r.error}`, r.ok ? 'ok' : 'bad');
        return route();
      }
      if (t.dataset.toggle) { await api(`/ai/providers/${t.dataset.toggle}`, { method: 'PATCH', body: { status: t.dataset.status } }); return route(); }
      if (t.dataset.del) {
        if (await confirmBox('Remove provider', 'Its models and stored key are deleted.', 'Remove', 'danger-solid')) { await api(`/ai/providers/${t.dataset.del}`, { method: 'DELETE' }); route(); }
        return;
      }
      if (t.dataset.key || t.dataset.edit) {
        const pr = providers.find((x) => x.id === (t.dataset.key || t.dataset.edit));
        const editing = Boolean(t.dataset.edit);
        const r = await modal({ title: editing ? `Edit ${pr.name}` : `Rotate credentials · ${pr.name}`,
          body: editing ? `<div class="field"><label>Display name</label><input type="text" name="name" value="${esc(pr.name)}"></div>
              ${pr.provider_type === 'bedrock' ? `<div class="field"><label>AWS region</label><input type="text" name="region" value="${esc(pr.region)}" list="regions"><datalist id="regions">${REGIONS.map((x) => `<option value="${x}">`).join('')}</datalist></div>`
                : `<div class="field"><label>Base URL</label><input type="url" name="base_url" value="${esc(pr.base_url)}"></div>`}`
            : credFields(pr.provider_type),
          onOpen: wireMode,
          actions: [{ label: 'Cancel', value: null }, { label: 'Save', cls: 'primary', handler: (root) => {
            const f = formData(root);
            return api(`/ai/providers/${pr.id}`, { method: 'PATCH', body: editing ? { name: f.name, base_url: f.base_url, region: f.region } : credBody(root, pr.provider_type) });
          } }] });
        if (r) { toast(editing ? 'Saved' : 'Credentials rotated', 'ok'); route(); }
        return;
      }
      const m = t.dataset.editmodel ? JSON.parse(t.dataset.editmodel) : {};
      const r = await modal({ title: m.id ? `Edit ${m.model_id}` : 'Register model', wide: true, body: modelForm(m),
        actions: [{ label: 'Cancel', value: null }, ...(m.id ? [{ label: 'Delete', cls: 'danger', handler: () => api(`/ai/models/${m.id}`, { method: 'DELETE' }) }] : []),
          { label: 'Save', cls: 'primary', handler: (root) => (m.id ? api(`/ai/models/${m.id}`, { method: 'PUT', body: modelBody(root) }) : api(`/ai/providers/${t.dataset.addmodel}/models`, { method: 'POST', body: modelBody(root) })) }] });
      if (r) route();
    } catch (err) { toast(err.message, 'bad'); t.disabled = false; }
  });
}
