import { $, api, esc, icon, badge, agoCell, pageHead, modal, toast, confirmBox, formData } from '../lib.js';
import { state, isAdmin, isOwner, route } from '../app.js';

export async function render({ el }) {
  const [org, tokens, demo, notif] = await Promise.all([api('/organizations/current'), api('/tokens'), api('/demo/state'), isAdmin() ? api('/settings/notifications') : Promise.resolve(null)]);
  const p = org.policy || {};
  const admin = isAdmin();
  const origin = location.origin;
  el.innerHTML = `<div class="page stack">
    ${pageHead('cog', 'Settings', `${esc(org.name)} — policy, alert delivery, members and API access.`)}
    <div class="split even">
      <div class="card"><div class="card-head"><h2>Organisation policy</h2></div><div class="card-body" id="policy">
        <div class="field"><label>Allowed API destinations</label><input type="text" name="allowed_domains" value="${esc((p.allowed_domains || []).join(', '))}" ${admin ? '' : 'disabled'} placeholder="Empty = any public HTTPS host">
          <div class="hint">Comma-separated domains (subdomains included). Private networks and cloud metadata addresses are always blocked.</div></div>
        <div class="grid-2"><div class="field"><label>Monthly AI budget (USD)</label><input type="number" name="ai_monthly_budget_usd" value="${p.ai_monthly_budget_usd ?? 25}" ${admin ? '' : 'disabled'}><div class="hint">Over budget, only free models are used.</div></div>
          <div class="field"><label>Auto-repair after N drift failures</label><input type="number" name="repair_after_failures" value="${p.repair_after_failures ?? 2}" min="1" ${admin ? '' : 'disabled'}></div></div>
        <div class="grid-2"><div class="field"><label>Alert after N failed runs in a row</label><input type="number" name="source_failure_alert_after" value="${p.source_failure_alert_after ?? 3}" min="1" max="20" ${admin ? '' : 'disabled'}><div class="hint">A data source that fails this many consecutive runs raises an alert.</div></div>
          <div class="field"><label>Credential rotation period (days)</label><input type="number" name="credential_rotation_days" value="${p.credential_rotation_days ?? 90}" min="7" max="730" ${admin ? '' : 'disabled'}><div class="hint">Older secrets raise a "rotate me" alert.</div></div></div>
        <label class="check"><input type="checkbox" name="require_tests" ${p.require_tests !== false ? 'checked' : ''} ${isOwner() ? '' : 'disabled'}> Require passing safe tests before review <span class="muted small">(platform owner)</span></label>
        ${admin ? '<div class="row end"><button class="btn primary" id="save-policy">Save policy</button></div>' : ''}</div></div>
      <div class="card"><div class="card-head"><h2>API &amp; MCP access</h2><span class="spacer"></span><button class="btn sm" id="new-token">${icon('plus', 13)} New token</button></div><div class="card-body">
        <p class="small muted" style="margin-top:0">Personal tokens act with your role. They can run tools and read data, but can never approve, publish or change policy.</p>
        ${tokens.length ? tokens.map((t) => `<div class="op-row"><span class="grow"><b>${esc(t.name)}</b> <span class="mono muted small">${esc(t.prefix)}…</span><div class="small muted">created ${agoCell(t.created_at)} · last used ${t.last_used_at ? agoCell(t.last_used_at) : 'never'}</div></span><button class="btn sm danger" data-revoke="${t.id}">Revoke</button></div>`).join('') : '<p class="muted small">No tokens.</p>'}
        <h3 class="small muted" style="margin:14px 0 6px">MCP CLIENT CONFIG</h3>
        <pre>${esc(JSON.stringify({ mcpServers: { hedes: { type: 'http', url: `${origin}/mcp`, headers: { Authorization: 'Bearer <your token>' } } } }, null, 2))}</pre>
        <p class="small muted">Only tools an admin marks “MCP” in the Tool Registry, and that your role may run, are listed.</p></div></div>
    </div>
    ${notif ? notificationsCard(notif) : ''}
    <div class="card"><div class="card-head"><h2>Members</h2><span class="spacer"></span>${admin ? `<button class="btn sm" id="invite">${icon('plus', 13)} Invite</button>` : ''}</div>
      <div class="table-scroll"><table><thead><tr><th>User</th><th>Email</th><th>Role</th></tr></thead><tbody>
      ${org.members.map((m) => `<tr><td><b>${esc(m.name || m.username)}</b> <span class="muted mono small">${esc(m.username)}</span></td><td>${esc(m.email)}</td>
        <td>${admin && m.user_id !== state.user.id ? `<select data-member="${m.id}" style="width:auto">${['user', 'admin', ...(isOwner() ? ['owner'] : [])].map((r) => `<option value="${r}" ${m.role === r ? 'selected' : ''}>${r === 'user' ? 'builder' : r}</option>`).join('')}${!isOwner() && m.role === 'owner' ? '<option selected>owner</option>' : ''}</select>` : badge('info', m.role === 'user' ? 'builder' : m.role)}</td></tr>`).join('')}
      ${(org.invitations || []).map((i) => `<tr><td class="muted">invited</td><td>${esc(i.email)}</td><td>${badge('pending', i.role)}</td></tr>`).join('')}
      </tbody></table></div></div>
    ${demo.demo ? `<div class="card"><div class="card-head"><h2>Demo controls</h2><span class="muted small">bundled mock APIs</span></div><div class="card-body">
      <div class="row between"><div><b>TaskFlow response drift</b><div class="small muted">Changes <code>assignee.user_name</code> to <code>assignee.display_name</code> to demonstrate detection and AI repair.</div></div>
        ${admin ? `<button class="btn ${demo.drift ? 'danger-solid' : ''}" id="drift">${demo.drift ? 'Drift ON — turn off' : 'Simulate API change'}</button>` : badge(demo.drift ? 'failed' : 'ok', demo.drift ? 'drift on' : 'normal')}</div>
      <div class="split even" style="margin-top:16px">
        <div><h3 class="small muted" style="margin-bottom:6px">TASKFLOW TASKS</h3>${demo.tasks.length ? demo.tasks.slice(0, 8).map((t) => `<div class="op-row small"><b>#${t.id}</b><span class="grow">${esc(t.title)}</span><span class="muted">${esc(t.assignee.user_name || t.assignee.display_name)}</span>${badge(t.priority === 'high' ? 'high' : 'low', t.priority)}</div>`).join('') : '<p class="muted small">No tasks yet.</p>'}</div>
        <div><h3 class="small muted" style="margin-bottom:6px">NOTIFY MESSAGES</h3>${demo.messages.length ? demo.messages.slice(0, 8).map((m) => `<div class="op-row small">${badge(m.severity === 'critical' ? 'critical' : 'info', m.channel)}<span class="grow">${esc(m.text)}</span></div>`).join('') : '<p class="muted small">No messages yet.</p>'}</div>
      </div></div></div>` : ''}
  </div>`;

  $('#save-policy', el)?.addEventListener('click', async () => {
    const f = formData($('#policy', el));
    const body = { allowed_domains: f.allowed_domains.split(',').map((s) => s.trim()).filter(Boolean), ai_monthly_budget_usd: f.ai_monthly_budget_usd, repair_after_failures: f.repair_after_failures, source_failure_alert_after: f.source_failure_alert_after, credential_rotation_days: f.credential_rotation_days };
    if (isOwner()) body.require_tests = f.require_tests;
    try { await api('/organizations/current/policy', { method: 'PATCH', body }); toast('Policy saved', 'ok'); } catch (err) { toast(err.message, 'bad'); }
  });
  $('#save-notif', el)?.addEventListener('click', async () => {
    const g = (n) => $(`[name=${n}]`, $('#notif', el)).value;
    const body = { enabled: $('[name=enabled]', $('#notif', el)).checked, min_severity: g('min_severity'),
      email: { host: g('host').trim(), port: +g('port') || 587, security: g('security'), username: g('username').trim(), from: g('from').trim(), to: g('to').split(',').map((x) => x.trim()).filter(Boolean) } };
    if (g('password')) body.email.password = g('password');
    if (g('slack') || $('[name=rm_slack]', $('#notif', el))?.checked) body.slack_webhook_url = g('slack').trim();
    try { await api('/settings/notifications', { method: 'PUT', body }); toast('Alert delivery saved', 'ok'); route(); } catch (err) { toast(err.message, 'bad'); }
  });
  $('#test-notif', el)?.addEventListener('click', async (e) => {
    e.target.disabled = true;
    try {
      const r = await api('/settings/notifications/test', { method: 'POST' });
      toast(Object.entries(r.results).map(([k, v]) => `${k}: ${v}`).join(' · '), r.ok ? 'ok' : 'bad');
    } catch (err) { toast(err.message, 'bad'); }
    e.target.disabled = false;
  });
  $('#drift', el)?.addEventListener('click', async () => { await api('/demo/drift', { method: 'POST', body: { enabled: !demo.drift } }); route(); });
  $('#new-token', el).addEventListener('click', async () => {
    const r = await modal({ title: 'New API token', body: '<div class="field"><label>Name</label><input type="text" name="name" placeholder="e.g. Claude Desktop" required></div>',
      actions: [{ label: 'Cancel', value: null }, { label: 'Create', cls: 'primary', handler: (root) => api('/tokens', { method: 'POST', body: { name: formData(root).name || 'token' } }) }] });
    if (!r) return;
    await modal({ title: 'Copy your token now', body: `<p class="small" style="margin-top:0">It will not be shown again.</p><pre style="white-space:pre-wrap;word-break:break-all">${esc(r.token)}</pre>`, actions: [{ label: 'Done', cls: 'primary', value: true }] });
    route();
  });
  $('#invite', el)?.addEventListener('click', async () => {
    const r = await modal({ title: 'Invite member', body: `<div class="field"><label>Email</label><input type="email" name="email" required></div>
      <div class="field"><label>Role</label><select name="role"><option value="user">Builder</option><option value="admin">Admin</option></select></div>`,
      actions: [{ label: 'Cancel', value: null }, { label: 'Create invitation', cls: 'primary', handler: (root) => api('/organizations/invitations', { method: 'POST', body: formData(root) }) }] });
    if (!r) return;
    await modal({ title: 'Invitation link', body: `<p class="small" style="margin-top:0">Share this link (valid 7 days):</p><pre style="white-space:pre-wrap;word-break:break-all">${esc(r.link)}</pre>`, actions: [{ label: 'Done', cls: 'primary', value: true }] });
    route();
  });
  el.addEventListener('change', async (e) => {
    const s = e.target.closest('[data-member]');
    if (!s) return;
    try { await api(`/members/${s.dataset.member}/role`, { method: 'PATCH', body: { role: s.value } }); toast('Role updated', 'ok'); } catch (err) { toast(err.message, 'bad'); route(); }
  });
  el.addEventListener('click', async (e) => {
    const t = e.target.closest('[data-revoke]');
    if (t && await confirmBox('Revoke token', 'Clients using it will lose access immediately.', 'Revoke', 'danger-solid')) {
      await api(`/tokens/${t.dataset.revoke}`, { method: 'DELETE' });
      route();
    }
  });
}

function notificationsCard(n) {
  return `<div class="card"><div class="card-head"><h2>Alert delivery</h2><span class="muted small">Slack and email · secrets stored encrypted</span></div><div class="card-body" id="notif">
    <p class="small muted" style="margin-top:0">Alerts always appear in the bell menu. Turn this on to also push them out, for example when a data source keeps failing, a credential expires or an API changes shape.</p>
    <div class="row between" style="margin-bottom:12px"><label class="check"><input type="checkbox" name="enabled" ${n.enabled ? 'checked' : ''}> Send alerts to Slack / email</label>
      <div class="row"><label class="small" style="margin:0">Minimum severity</label><select name="min_severity" style="width:auto">${['info', 'low', 'medium', 'high', 'critical'].map((s) => `<option ${n.min_severity === s ? 'selected' : ''}>${s}</option>`).join('')}</select></div></div>
    <div class="split even"><div><h3 class="small muted">SLACK</h3>
        <div class="field"><label>Incoming webhook URL</label><input type="password" name="slack" autocomplete="new-password" placeholder="${n.slack.configured ? `configured ${esc(n.slack.hint)} — leave blank to keep` : 'https://hooks.slack.com/services/…'}">
          <div class="hint">Create one in Slack: Apps → Incoming Webhooks. ${n.slack.configured ? '<label class="check" style="display:inline-flex"><input type="checkbox" name="rm_slack"> remove it</label>' : ''}</div></div></div>
      <div><h3 class="small muted">EMAIL (SMTP)</h3>
        <div class="grid-2"><div class="field"><label>Server</label><input type="text" name="host" value="${esc(n.email.host)}" placeholder="smtp.example.com"></div>
          <div class="field"><label>Port</label><input type="number" name="port" value="${n.email.port}"></div></div>
        <div class="grid-2"><div class="field"><label>Security</label><select name="security">${['starttls', 'ssl', 'none'].map((s) => `<option ${n.email.security === s ? 'selected' : ''}>${s}</option>`).join('')}</select></div>
          <div class="field"><label>Username</label><input type="text" name="username" value="${esc(n.email.username)}" autocomplete="off"></div></div>
        <div class="field"><label>Password</label><input type="password" name="password" autocomplete="new-password" placeholder="${n.email.has_password ? 'saved — leave blank to keep' : ''}"></div>
        <div class="grid-2"><div class="field"><label>From</label><input type="text" name="from" value="${esc(n.email.from)}" placeholder="hedes@example.com"></div>
          <div class="field"><label>To (comma-separated)</label><input type="text" name="to" value="${esc((n.email.to || []).join(', '))}" placeholder="secops@example.com"></div></div></div></div>
    <div class="row end"><button class="btn" id="test-notif">Send test</button><button class="btn primary" id="save-notif">Save</button></div></div></div>`;
}
