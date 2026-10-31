import { $, api, esc, icon, badge, agoCell, pageHead, emptyState, modal, toast, confirmBox, fmtTime } from '../lib.js';
import { route, isAdmin } from '../app.js';

const AUTH_LABEL = { bearer: 'Bearer token', api_key_header: 'API key (header)', api_key_query: 'API key (query)', basic: 'Username + password',
  ssws: 'Okta API token (SSWS)', oauth2_client_credentials: 'OAuth2 client credentials', aws_sigv4: 'AWS access key (SigV4)' };
// Created only by signing in with the provider, never typed into the form.
const SIGNED_IN = 'oauth2_authorization_code';
export const typeLabel = (t) => AUTH_LABEL[t] || (t === SIGNED_IN ? 'Signed in with provider' : t);

function secretFields(auth) {
  if (auth === 'basic') return `<div class="grid-2"><div class="field"><label>Username / email</label><input type="text" name="username" autocomplete="off" required></div>
    <div class="field"><label>Password or API token</label><input type="password" name="password" autocomplete="new-password" required></div></div>`;
  if (auth === 'oauth2_client_credentials') return `<div class="grid-2"><div class="field"><label>Client ID</label><input type="text" name="client_id" autocomplete="off" required></div>
    <div class="field"><label>Client secret</label><input type="password" name="client_secret" autocomplete="new-password" required></div></div>`;
  if (auth === 'aws_sigv4') return `<div class="grid-2"><div class="field"><label>Access key ID</label><input type="text" name="access_key_id" autocomplete="off" required></div>
    <div class="field"><label>Secret access key</label><input type="password" name="secret_access_key" autocomplete="new-password" required></div></div>
    <div class="field"><label>Session token (optional)</label><input type="password" name="session_token" autocomplete="new-password"><div class="hint">Only for temporary STS credentials.</div></div>`;
  if (auth === 'bearer' || auth === 'ssws') return '<div class="field"><label>Token</label><input type="password" name="token" autocomplete="new-password" required></div>';
  return '<div class="field"><label>API key</label><input type="password" name="api_key" autocomplete="new-password" required></div>';
}

// Secure credential form. The secret is posted straight to the vault endpoint and is never echoed back.
export async function connectModal({ name = '', auth_type = 'bearer', header = '', rotateId = null } = {}) {
  if (auth_type === SIGNED_IN) return signInModal(name);
  const fixed = Boolean(name);
  const body = `<p class="small muted" style="margin-top:0">${icon('shield', 14)} Encrypted in the credential vault. Only the execution engine reads it at run time; it is never shown again or sent to any AI model.</p>
    <div class="grid-2">
      <div class="field"><label>Connection name</label><input type="text" name="name" value="${esc(name)}" ${fixed || rotateId ? 'readonly' : ''} pattern="[a-z0-9][a-z0-9_\\-]{0,63}" required><div class="hint">Configurations reference it as <code>credential_ref</code>.</div></div>
      <div class="field"><label>Type</label>${fixed || rotateId
        // the integration decides the type: show it plainly rather than as a dropdown that cannot be opened
        ? `<input type="text" value="${esc(typeLabel(auth_type))}" readonly><input type="hidden" name="auth_type" value="${esc(auth_type)}"><div class="hint">Set by the integration.${header ? ` Sent as header <code>${esc(header)}</code>.` : ''}</div>`
        : `<select name="auth_type">${Object.entries(AUTH_LABEL).map(([k, v]) => `<option value="${k}" ${k === auth_type ? 'selected' : ''}>${v}</option>`).join('')}</select>`}</div>
    </div>
    <div id="secret">${secretFields(auth_type)}</div>
    <div class="hint" style="margin:-6px 0 12px">${icon('key', 12)} Prefer not to store the secret here? Type <code>env:MY_VARIABLE</code> in the field and the server reads that environment variable each time it is used.</div>
    <div class="field"><label>Expires on (optional)</label><input type="date" name="expires_at"><div class="hint">You are alerted a week before, and executions stop using an expired secret.</div></div>
    ${rotateId ? '' : '<div class="field"><label>Test URL (optional)</label><input type="url" name="test_url" placeholder="https://api.example.com/user"><div class="hint">A read-only endpoint used by “Test connection”.</div></div>'}
    ${name === 'taskflow' ? '<div class="callout info small">Demo: the bundled TaskFlow mock API accepts the token <code>taskflow-demo-token</code>.</div>' : ''}`;
  return modal({
    title: rotateId ? 'Rotate secret' : 'Connect credential', body,
    onOpen: (root) => {
      const sel = root.querySelector('select[name=auth_type]');
      sel?.addEventListener('change', () => { root.querySelector('#secret').innerHTML = secretFields(sel.value); });
    },
    actions: [{ label: 'Cancel', value: null }, {
      label: rotateId ? 'Save new secret' : 'Save connection', cls: 'primary', handler: async (root) => {
        const v = Object.fromEntries([...root.querySelectorAll('[name]')].map((i) => [i.name, i.value]));
        if (v.expires_at) v.expires_at = `${v.expires_at}T23:59:59Z`; else delete v.expires_at;
        if (!v.session_token) delete v.session_token;
        const form = root.querySelector('.modal-body');
        if ([...form.querySelectorAll('[required]')].some((i) => !i.value)) { toast('Fill in the required fields', 'bad'); return false; }
        const res = rotateId
          ? await api(`/connections/${rotateId}/secret`, { method: 'PUT', body: v })
          : await api('/connections', { method: 'POST', body: v });
        toast(`Connection ${res.name} saved`, 'ok');
        return res;
      },
    }],
  });
}

// A credential of a "Continue with <provider>" integration: nothing to paste, the person signs in with the provider.
async function signInModal(name) {
  const providers = await api('/oauth/providers').catch(() => []);
  const pr = providers.find((x) => x.id === name);
  const label = pr?.label || name;
  const body = !pr ? `<p style="margin-top:0">This credential is connected by signing in with a provider, but no sign-in provider called <code>${esc(name)}</code> exists.</p>`
    : `<p class="small muted" style="margin-top:0">${icon('shield', 14)} Nothing to paste: you sign in with ${esc(label)} and approve read access. The tokens go straight to the vault and are refreshed automatically.</p>
      ${pr.connection ? `<p class="small">Currently connected as <b>${esc(pr.connection.account || label)}</b> (${esc(pr.connection.status)}). Signing in again replaces it.</p>` : ''}
      ${pr.configured ? '' : `<div class="callout warn small">${esc(label)} sign-in is not set up yet. ${isAdmin() ? 'Add the OAuth app in <a class="link" href="#/settings">Settings → Sign-in providers</a> first.' : 'Ask an administrator to add it in Settings → Sign-in providers.'}</div>`}`;
  return modal({
    title: `Connect ${label}`, body,
    actions: [{ label: 'Cancel', value: null }, ...(pr?.configured ? [{
      label: `Continue with ${label.split(' ')[0]}`, cls: 'primary', handler: async () => {
        try { await signIn(pr.id); } catch (err) { toast(err.message, 'bad'); return false; }
        return true;
      },
    }] : [])],
  });
}

async function signIn(provider) {
  const r = await api(`/oauth/${provider}/start`, { method: 'POST', body: {} });
  location.href = r.url;  // to the provider; it sends the browser back to #/credentials/connected/<provider>
}

function providersCard(providers, isAdmin) {
  return `<div class="card" style="margin-bottom:16px"><div class="card-head"><h2>${icon('link', 16)} Sign in with a provider</h2></div><div class="card-body">
    <p class="small muted" style="margin-top:0">Continue with the provider, approve read access, and import every data source it offers. Tokens go straight to the vault and are refreshed automatically.</p>
    <div class="grid-2">${providers.map((pr) => `<div class="op-row" style="align-items:flex-start">
      <div class="grow"><b>${esc(pr.label)}</b>
        <div class="small muted">${pr.connection ? `${badge(pr.connection.status)} ${esc(pr.connection.account || 'connected')}` : pr.configured ? 'Not connected' : `Not set up${isAdmin ? ' - add the OAuth app in <a class="link" href="#/settings">Settings → Sign-in providers</a>' : ' - ask an administrator'}`}</div>
        <div class="small muted">${pr.templates.map((t) => esc(t.title)).join(' · ') || 'No data-source templates yet'}</div></div>
      <div class="row" style="gap:6px">${pr.configured ? `<button class="btn sm ${pr.connection ? '' : 'primary'}" data-signin="${esc(pr.id)}">${pr.connection ? 'Reconnect' : `Continue with ${esc(pr.label.split(' ')[0])}`}</button>` : ''}
        ${pr.connection && pr.templates.length ? `<button class="btn sm" data-import="${esc(pr.id)}">Import sources</button>` : ''}</div></div>`).join('')}</div></div></div>`;
}

async function importModal(pr) {
  const body = `<p class="small muted" style="margin-top:0">Signed in as <b>${esc(pr.connection?.account || pr.label)}</b>. Each source becomes a draft integration that runs its safe tests and then needs an administrator's approval, like any other.</p>
    ${pr.templates.map((t) => `<div class="op-row" style="align-items:flex-start"><label class="check grow"><input type="checkbox" data-tpl="${esc(t.id)}" ${t.installed ? 'disabled' : 'checked'}>
      <span><b>${esc(t.title)}</b>${t.installed ? ' <span class="muted small">(already imported)</span>' : ''}<div class="small muted">${esc(t.summary)}</div>
      ${t.variables.map((v) => `<div class="field" style="margin-top:6px"><label class="small">${esc(v.label)}</label><input type="text" data-var="${esc(t.id)}" name="${esc(v.name)}"
        value="${esc((pr.connection?.discovered || {})[v.name] || '')}" placeholder="${esc(v.placeholder || '')}"><div class="hint">${esc(v.help || '')}</div></div>`).join('')}</span></label></div>`).join('')}`;
  return modal({
    title: `Import data sources from ${pr.label}`, body,
    actions: [{ label: 'Cancel', value: null }, {
      label: 'Import', cls: 'primary', handler: async (root) => {
        const templates = [...root.querySelectorAll('[data-tpl]:checked')].map((i) => i.dataset.tpl);
        if (!templates.length) { toast('Pick at least one source', 'bad'); return false; }
        const variables = {};
        root.querySelectorAll('[data-var]').forEach((i) => { (variables[i.dataset.var] ||= {})[i.name] = i.value.trim(); });
        const r = await api(`/oauth/${pr.id}/import`, { method: 'POST', body: { templates, variables } });
        const bad = r.results.filter((x) => !x.ok);
        bad.forEach((x) => toast(`${x.template}: ${x.error}`, 'bad'));
        if (r.results.length > bad.length) toast(`Imported ${r.results.length - bad.length} source(s). Open Integrations to submit them for approval.`, 'ok');
        return r;
      },
    }],
  });
}

export async function render({ el, params = [] }) {
  const [rows, providers] = await Promise.all([api('/connections'), api('/oauth/providers').catch(() => [])]);
  el.innerHTML = `<div class="page">
    ${pageHead('key', 'Credentials', 'External API credentials, stored encrypted and referenced by name. Integrations never contain secrets, and the AI never sees them.',
      `<button class="btn primary" id="new">${icon('plus', 16)} Connect credential</button>`)}
    ${providers.length ? providersCard(providers, isAdmin()) : ''}
    <div class="card">${rows.length ? `<div class="table-scroll"><table><thead><tr><th>Name</th><th>Type</th><th>Secret</th><th>Status</th><th>Lifetime</th><th>Used by</th><th>Last test</th><th></th></tr></thead><tbody>
      ${rows.map((c) => `<tr><td><b class="mono">${esc(c.name)}</b><div class="muted small">by ${esc(c.created_by)} · ${agoCell(c.created_at)}</div></td>
        <td>${esc(typeLabel(c.auth_type))}</td><td class="mono">${esc(c.hint || '—')}</td><td>${badge(c.status)}</td>
        <td class="small">${c.status === 'revoked' ? '<span class="muted">—</span>' : `${c.age_days} d since rotation${c.expires_at ? `<div class="muted">expires ${esc(fmtTime(c.expires_at))}</div>` : ''}`}
          ${c.warnings.map((w) => `<div>${badge(w === 'rotation_due' ? 'medium' : 'critical', w === 'expired' ? 'expired' : w === 'expiring' ? 'expires soon' : 'rotation due')}</div>`).join('')}</td>
        <td>${c.used_by.map((u) => `<a class="link" href="#/integrations/${u.id}">${esc(u.name)}</a>`).join(', ') || '<span class="muted">—</span>'}</td>
        <td class="small">${c.last_tested_at ? `${esc(c.last_test_result)}<div class="muted">${agoCell(c.last_tested_at)}</div>` : '<span class="muted">never</span>'}</td>
        <td class="nowrap"><button class="btn sm" data-test="${c.id}" ${c.status === 'revoked' ? 'disabled' : ''}>Test</button>
          ${c.auth_type === SIGNED_IN ? `<button class="btn sm" data-signin="${esc(c.name)}">Reconnect</button>`
            : `<button class="btn sm" data-rotate="${c.id}" data-name="${esc(c.name)}" data-auth="${esc(c.auth_type)}">Rotate</button>`}
          <button class="btn sm danger" data-revoke="${c.id}" ${c.status === 'revoked' ? 'disabled' : ''}>Revoke</button></td></tr>`).join('')}
    </tbody></table></div>` : emptyState('key', 'No credentials yet', 'Connect one when an integration asks for it.')}</div></div>`;

  $('#new', el).addEventListener('click', async () => { if (await connectModal()) route(); });
  // back from the provider: #/credentials/connected/<provider> or #/credentials/oauth-error/<message>
  if (params[0] === 'oauth-error') toast(`Sign-in failed: ${params[1] || 'unknown error'}`, 'bad');
  if (params[0] === 'connected') {
    const pr = providers.find((x) => x.id === params[1]);
    history.replaceState(null, '', '#/credentials');
    if (pr) { toast(`Connected to ${pr.label}`, 'ok'); if (pr.templates.length && await importModal(pr)) route(); }
  }
  el.addEventListener('click', async (e) => {
    const t = e.target.closest('[data-test], [data-rotate], [data-revoke], [data-signin], [data-import]');
    if (!t) return;
    if (t.dataset.signin) { try { await signIn(t.dataset.signin); } catch (err) { toast(err.message, 'bad'); } return; }
    if (t.dataset.import) { if (await importModal(providers.find((x) => x.id === t.dataset.import))) route(); return; }
    if (t.dataset.test) {
      t.disabled = true;
      try {
        const r = await api(`/connections/${t.dataset.test}/test`, { method: 'POST' });
        toast(r.message, r.ok ? 'ok' : r.ok === false ? 'bad' : '');
      } finally { route(); }
    } else if (t.dataset.rotate) {
      if (await connectModal({ name: t.dataset.name, auth_type: t.dataset.auth, rotateId: t.dataset.rotate })) route();
    } else if (await confirmBox('Revoke credential', 'Integrations using it will stop working until a new secret is connected.', 'Revoke', 'danger-solid')) {
      await api(`/connections/${t.dataset.revoke}`, { method: 'DELETE' });
      route();
    }
  });
}
