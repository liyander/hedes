import { $, api, esc, icon, badge, agoCell, pageHead, modal, toast, confirmBox, formData } from '../lib.js';
import { isOwner, isAdmin, route } from '../app.js';

const TASKS = ['general', 'conversation', 'requirements', 'api_discovery', 'config_generation', 'security_review', 'test_generation', 'repair', 'workflow_planning', 'error_explanation'];
const TYPES = { openai_compatible: 'OpenAI-compatible (OpenAI, NVIDIA, Groq, Ollama, LM Studio…)', anthropic: 'Anthropic Claude', gemini: 'Google Gemini' };

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
    <div class="field" style="margin-top:12px"><label>Preferred for tasks</label><div class="row">${TASKS.map((t) => `<label class="check small"><input type="checkbox" data-purpose="${t}" ${p.has(t) ? 'checked' : ''}> ${t.replace('_', ' ')}</label>`).join('')}</div></div>`;
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
    ${pageHead('cpu', 'AI Providers', 'A unified gateway routes each AI task to a suitable model by capability, data-handling permission and budget, with automatic fallback. The built-in offline model is the last resort.',
      owner ? `<button class="btn primary" id="add">${icon('plus', 16)} Add provider</button>` : '')}
    <div class="stats">
      <div class="card stat accent"><div class="ico">${icon('cpu', 20)}</div><div><b>${providers.filter((p) => p.status === 'active').length}</b><span>Active providers</span></div></div>
      <div class="card stat"><div class="ico">${icon('spark', 20)}</div><div><b>${providers.reduce((n, p) => n + p.models.filter((m) => m.enabled).length, 0)}</b><span>Enabled models</span></div></div>
      <div class="card stat ${routing.month_spend >= routing.budget ? 'alert' : 'good'}"><div class="ico">${icon('pulse', 20)}</div><div><b>$${routing.month_spend.toFixed(3)}</b><span>Spent this month of $${routing.budget}</span></div></div>
      <div class="card stat"><div class="ico">${icon('history', 20)}</div><div><b>${usage ? usage.rows.reduce((n, r) => n + r.calls, 0) : '—'}</b><span>AI calls this month</span></div></div>
    </div>
    <div class="split" style="margin-top:18px">
      <div class="stack">${providers.map((p) => `<div class="card"><div class="card-head"><span class="dot ${p.status === 'active' ? 'on' : 'off'}"></span><div><h2>${esc(p.name)}</h2><div class="small muted">${esc(TYPES[p.provider_type] || 'Deterministic heuristic agents, no network')}${p.base_url ? ` · <span class="mono">${esc(p.base_url)}</span>` : ''}</div></div><span class="spacer"></span>
          ${p.provider_type !== 'local' ? (p.has_key ? badge('ok', 'key in vault') : badge('medium', 'no key')) : badge('info', 'offline')}
          ${isAdmin() ? `<button class="btn sm" data-test="${p.id}">Test</button>` : ''}
          ${owner ? `<button class="btn sm" data-toggle="${p.id}" data-status="${p.status === 'active' ? 'disabled' : 'active'}">${p.status === 'active' ? 'Disable' : 'Enable'}</button>
            ${p.provider_type !== 'local' ? `<button class="btn sm" data-key="${p.id}">Rotate key</button><button class="btn sm" data-addmodel="${p.id}">${icon('plus', 13)} Model</button><button class="btn sm danger" data-del="${p.id}">${icon('trash', 13)}</button>` : ''}` : ''}</div>
        ${p.last_error ? `<div class="callout bad small" style="margin:12px 18px 0">Last error: ${esc(p.last_error)}</div>` : ''}
        <div class="table-scroll"><table><thead><tr><th>Model</th><th>Preferred for</th><th>Priority</th><th>Confidential</th><th>Cost / 1M</th><th></th></tr></thead><tbody>
          ${p.models.map((m) => `<tr><td class="mono"><b>${esc(m.label || m.model_id)}</b> ${m.enabled ? '' : badge('disabled')}<div class="small muted">${esc(m.model_id)} · ${(m.capabilities?.context_tokens || 0).toLocaleString()} ctx${m.capabilities?.json_mode ? ' · JSON' : ''}</div></td>
            <td class="small">${(m.purposes || []).map((x) => `<span class="badge">${esc(x)}</span>`).join(' ')}</td><td>${m.priority}</td><td>${m.allow_confidential ? badge('ok', 'allowed') : badge('medium', 'blocked')}</td>
            <td class="small">$${m.cost_input_per_mtok} / $${m.cost_output_per_mtok}</td><td>${owner ? `<button class="btn sm" data-editmodel='${esc(JSON.stringify(m))}'>Edit</button>` : ''}</td></tr>`).join('')}
        </tbody></table></div></div>`).join('')}</div>
      <div class="stack">
        <div class="card"><div class="card-head"><h2>Task routing</h2><span class="muted small">first = tried first</span></div><div class="card-body">
          ${routing.tasks.map((t) => `<div style="padding:7px 0;border-bottom:1px solid var(--line)"><div class="row between"><b class="small">${esc(t.task.replace('_', ' '))}</b><span>${t.structured ? badge('info', 'JSON') : ''} ${t.confidential ? badge('medium', 'confidential') : ''}</span></div>
            <div class="small muted">${t.candidates.map((c, i) => `${i ? '→ ' : ''}${esc(c)}`).join(' ') || '<span style="color:var(--bad)">no eligible model</span>'}</div></div>`).join('')}</div></div>
        ${usage ? `<div class="card"><div class="card-head"><h2>Usage this month</h2></div><div class="card-body">
          ${usage.rows.length ? `<table><thead><tr><th>Model · task</th><th>Calls</th><th>Tokens</th><th>Cost</th></tr></thead><tbody>${usage.rows.map((r) => `<tr><td class="small"><span class="mono">${esc(r.model)}</span><div class="muted">${esc(r.task)}${r.failures ? ` · <span style="color:var(--bad)">${r.failures} failed</span>` : ''}</div></td><td>${r.calls}</td><td>${(r.prompt_tokens + r.completion_tokens).toLocaleString()}</td><td>$${r.cost}</td></tr>`).join('')}</tbody></table>` : '<p class="muted small">No AI calls yet.</p>'}
          ${usage.recent.filter((r) => !r.success).slice(0, 4).map((r) => `<div class="callout bad small" style="margin-top:8px">${esc(r.model)} · ${esc(r.task)} · ${agoCell(r.at)}<div class="muted">${esc(r.error)}</div></div>`).join('')}</div></div>` : ''}
      </div></div></div>`;

  $('#add', el)?.addEventListener('click', async () => {
    const r = await modal({ title: 'Add AI provider', wide: true,
      body: `<div class="grid-2"><div class="field"><label>Name</label><input type="text" name="name" required placeholder="e.g. NVIDIA NIM"></div>
        <div class="field"><label>Type</label><select name="provider_type">${Object.entries(TYPES).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join('')}</select></div>
        <div class="field"><label>Base URL (blank for the default)</label><input type="url" name="base_url" placeholder="https://integrate.api.nvidia.com/v1"></div>
        <div class="field"><label>API key</label><input type="password" name="api_key" autocomplete="new-password"><div class="hint">Stored in the vault. Optional for local servers.</div></div>
        <div class="field"><label>First model id</label><input type="text" name="model_id" required placeholder="e.g. gpt-4o-mini"></div></div>
        <div class="field"><label>Preferred for tasks</label><div class="row">${TASKS.map((t) => `<label class="check small"><input type="checkbox" data-purpose="${t}" ${t === 'general' ? 'checked' : ''}> ${t.replace('_', ' ')}</label>`).join('')}</div></div>`,
      actions: [{ label: 'Cancel', value: null }, { label: 'Add provider', cls: 'primary', handler: (root) => {
        const f = formData(root);
        return api('/ai/providers', { method: 'POST', body: { name: f.name, provider_type: f.provider_type, base_url: f.base_url, api_key: f.api_key, model_id: f.model_id,
          purposes: [...root.querySelectorAll('[data-purpose]:checked')].map((x) => x.dataset.purpose) } });
      } }] });
    if (r) { toast('Provider added', 'ok'); route(); }
  });

  el.addEventListener('click', async (e) => {
    const t = e.target.closest('[data-test],[data-toggle],[data-key],[data-addmodel],[data-editmodel],[data-del]');
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
      if (t.dataset.key) {
        const r = await modal({ title: 'Rotate API key', body: '<div class="field"><label>New API key</label><input type="password" name="api_key" autocomplete="new-password"></div>',
          actions: [{ label: 'Cancel', value: null }, { label: 'Save', cls: 'primary', handler: (root) => api(`/ai/providers/${t.dataset.key}`, { method: 'PATCH', body: { api_key: formData(root).api_key } }) }] });
        if (r) { toast('Key rotated', 'ok'); route(); }
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
