import { api, esc, icon, badge, agoCell, pageHead, emptyState, modal, toast, confirmBox, formData } from '../lib.js';
import { state, isAdmin, isOwner, route } from '../app.js';

const ROLE_LABEL = { user: 'Builder', admin: 'Admin', owner: 'Owner' };
const ROLE_HELP = 'Builder: builds and runs integrations. Admin: also approves, manages users and policy. Owner: also configures AI providers and owners.';

const roleOptions = (current) => ['user', 'admin', ...(isOwner() ? ['owner'] : [])]
  .map((r) => `<option value="${r}" ${r === current ? 'selected' : ''}>${ROLE_LABEL[r]}</option>`).join('');

export async function render({ el }) {
  if (!isAdmin()) {
    el.innerHTML = `<div class="page">${pageHead('user', 'Users', '')}${emptyState('user', 'Administrators only', 'Ask an administrator to add or change accounts.')}</div>`;
    return;
  }
  const users = await api('/admin/users');
  const active = users.filter((u) => u.status === 'active').length;
  el.innerHTML = `<div class="page">
    ${pageHead('user', 'Users', `Create accounts, set roles, disable access and reset passwords. ${ROLE_HELP}`,
      `<button class="btn primary" id="add">${icon('plus', 16)} Add user</button>`)}
    <div class="stats" style="grid-template-columns:repeat(3,1fr);margin-bottom:18px">
      <div class="card stat accent"><div class="ico">${icon('user', 20)}</div><div><b>${active}</b><span>Active users</span></div></div>
      <div class="card stat"><div class="ico">${icon('shield', 20)}</div><div><b>${users.filter((u) => u.role !== 'user' && u.status === 'active').length}</b><span>Admins &amp; owners</span></div></div>
      <div class="card stat ${users.length - active ? 'attn' : ''}"><div class="ico">${icon('x', 20)}</div><div><b>${users.length - active}</b><span>Disabled</span></div></div>
    </div>
    <div class="card"><div class="table-scroll"><table><thead><tr><th>User</th><th>Email</th><th>Role</th><th>Status</th><th>Created</th><th></th></tr></thead><tbody>
    ${users.map((u) => {
      const me = u.user_id === state.user.id;
      const locked = u.role === 'owner' && !isOwner();
      return `<tr><td><b>${esc(u.name || u.username)}</b> ${me ? badge('info', 'you') : ''}<div class="muted small mono">${esc(u.username)}</div></td>
        <td>${esc(u.email)}</td>
        <td>${me || locked ? badge('accent', ROLE_LABEL[u.role]) : `<select data-role="${u.id}" style="width:auto">${roleOptions(u.role)}</select>`}</td>
        <td>${badge(u.status === 'active' ? 'active' : 'disabled', u.status)}</td>
        <td class="nowrap">${agoCell(u.created_at)}</td>
        <td class="nowrap">${locked ? '' : `<button class="btn sm" data-edit="${u.id}">Edit</button> <button class="btn sm" data-pw="${u.id}">Reset password</button>
          ${me ? '' : u.status === 'active' ? `<button class="btn sm danger" data-status="${u.id}" data-to="disabled">Disable</button>` : `<button class="btn sm" data-status="${u.id}" data-to="active">Enable</button>`}`}</td></tr>`;
    }).join('')}
    </tbody></table></div></div>
    <p class="muted small">Disabled users cannot sign in and their sessions and API tokens stop working immediately. Accounts are kept so the audit history stays intact.</p></div>`;

  const byId = Object.fromEntries(users.map((u) => [u.id, u]));
  el.querySelector('#add').addEventListener('click', async () => {
    const r = await modal({ title: 'Add user', wide: true,
      body: `<div class="grid-2"><div class="field"><label>Username</label><input type="text" name="username" required pattern="[a-z0-9_.-]{3,32}" placeholder="lowercase, 3-32 chars"></div>
        <div class="field"><label>Full name</label><input type="text" name="name"></div>
        <div class="field"><label>Email</label><input type="email" name="email" required></div>
        <div class="field"><label>Role</label><select name="role">${roleOptions('user')}</select></div>
        <div class="field"><label>Initial password (10+ characters)</label><input type="password" name="password" required minlength="10" autocomplete="new-password"><div class="hint">Share it with the user securely.</div></div></div>
        <p class="small muted" style="margin:0">${ROLE_HELP}</p>`,
      actions: [{ label: 'Cancel', value: null }, { label: 'Create user', cls: 'primary', handler: (root) => api('/admin/users', { method: 'POST', body: formData(root) }) }] });
    if (r) { toast(`User ${r.username} created`, 'ok'); route(); }
  });
  el.addEventListener('change', async (e) => {
    const s = e.target.closest('[data-role]');
    if (!s) return;
    try { await api(`/admin/users/${s.dataset.role}`, { method: 'PATCH', body: { role: s.value } }); toast('Role updated', 'ok'); } catch (err) { toast(err.message, 'bad'); }
    route();
  });
  el.addEventListener('click', async (e) => {
    const t = e.target.closest('[data-edit], [data-pw], [data-status]');
    if (!t) return;
    try {
      if (t.dataset.edit) {
        const u = byId[t.dataset.edit];
        const r = await modal({ title: `Edit ${u.username}`, body: `<div class="field"><label>Full name</label><input type="text" name="name" value="${esc(u.name)}"></div>
          <div class="field"><label>Email</label><input type="email" name="email" value="${esc(u.email)}" required></div>`,
          actions: [{ label: 'Cancel', value: null }, { label: 'Save', cls: 'primary', handler: (root) => api(`/admin/users/${u.id}`, { method: 'PATCH', body: formData(root) }) }] });
        if (r) { toast('Saved', 'ok'); route(); }
      } else if (t.dataset.pw) {
        const u = byId[t.dataset.pw];
        const r = await modal({ title: `New password for ${u.username}`, body: '<div class="field"><label>New password (10+ characters)</label><input type="password" name="password" minlength="10" autocomplete="new-password" required></div>',
          actions: [{ label: 'Cancel', value: null }, { label: 'Set password', cls: 'primary', handler: (root) => api(`/admin/users/${u.id}/password`, { method: 'POST', body: formData(root) }) }] });
        if (r) toast('Password changed', 'ok');
      } else {
        const u = byId[t.dataset.status];
        const disable = t.dataset.to === 'disabled';
        if (disable && !(await confirmBox('Disable user', `${u.username} will be signed out and cannot sign in until re-enabled.`, 'Disable', 'danger-solid'))) return;
        await api(`/admin/users/${u.id}`, { method: 'PATCH', body: { status: t.dataset.to } });
        toast(disable ? 'User disabled' : 'User enabled', 'ok');
        route();
      }
    } catch (err) { toast(err.message, 'bad'); }
  });
}
