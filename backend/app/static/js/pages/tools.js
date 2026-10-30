import { api, esc, icon, badge, method, pageHead, emptyState, toast, json, modal } from '../lib.js';
import { isAdmin, route } from '../app.js';

export async function render({ el }) {
  const admin = isAdmin();
  const tools = admin ? await api('/admin/tools') : await api('/tools');
  el.innerHTML = `<div class="page wide">
    ${pageHead('box', 'Tool Registry', 'Operations from published integration versions. Admins decide which roles may run each tool and whether AI agents can discover it over MCP.',
      `<a class="btn" href="#/settings">${icon('agent', 16)} MCP setup</a>`)}
    <div class="card">${tools.length ? `<div class="table-scroll"><table><thead><tr><th>Tool</th><th>Integration</th><th>Inputs</th><th>Roles</th><th>MCP</th><th>Enabled</th><th></th></tr></thead><tbody>
      ${tools.map((t) => `<tr><td>${method(t.method)} <b class="mono">${esc(t.name)}</b><div class="small muted">${esc(t.description)}</div></td>
        <td><a class="link" href="#/integrations/${t.integration_id}">${esc(t.integration)}</a> ${t.integration_status === 'disabled' ? badge('disabled') : ''}</td>
        <td class="small">${Object.keys(t.input_schema.properties || {}).map((k) => `<code>${esc(k)}</code>`).join(' ')}</td>
        <td>${admin ? ['user', 'admin'].map((r) => `<label class="check small"><input type="checkbox" data-role="${r}" data-t="${t.id}" ${t.allowed_roles.includes(r) ? 'checked' : ''}> ${r === 'user' ? 'builders' : 'admins'}</label>`).join('') : t.allowed_roles.map(esc).join(', ')}</td>
        <td>${admin ? `<label class="check small"><input type="checkbox" data-mcp="${t.id}" ${t.mcp_enabled ? 'checked' : ''}> <span class="mono">${esc(t.mcp_name)}</span></label>` : (t.mcp_enabled ? badge('info', 'exposed') : '<span class="muted">—</span>')}</td>
        <td>${admin ? `<label class="check small"><input type="checkbox" data-en="${t.id}" ${t.enabled ? 'checked' : ''}></label>` : badge(t.enabled ? 'active' : 'disabled')}</td>
        <td class="nowrap"><button class="btn sm" data-schema="${t.id}">Schema</button> <a class="btn sm" href="#/testlab/${t.integration_id}">${icon('play', 13)} Run</a></td></tr>`).join('')}
    </tbody></table></div>` : emptyState('box', 'No published tools', 'Tools appear when an integration version is approved.')}</div></div>`;

  el.addEventListener('change', async (e) => {
    const c = e.target;
    const id = c.dataset.t || c.dataset.mcp || c.dataset.en;
    if (!id) return;
    let body;
    if (c.dataset.role) body = { allowed_roles: [...el.querySelectorAll(`[data-t="${id}"]:checked`)].map((x) => x.dataset.role) };
    else if (c.dataset.mcp) body = { mcp_enabled: c.checked };
    else body = { enabled: c.checked };
    try { await api(`/admin/tools/${id}`, { method: 'PATCH', body }); toast('Tool updated', 'ok'); } catch (err) { toast(err.message, 'bad'); route(); }
  });
  el.addEventListener('click', async (e) => {
    const b = e.target.closest('[data-schema]');
    if (!b) return;
    const s = await api(`/tools/${b.dataset.schema}/schema`);
    modal({ title: s.name, wide: true, body: `<h3 class="small muted">INPUT SCHEMA</h3>${json(s.input_schema)}<h3 class="small muted" style="margin-top:12px">MCP TOOL DEFINITION</h3>${json(s.mcp)}` });
  });
}
