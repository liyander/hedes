import { $, api, icon, LOGO } from '../lib.js';
import { state, go } from '../app.js';

const ART = `<svg viewBox="0 0 400 400" aria-hidden="true">
  <defs><pattern id="dots" width="8" height="8" patternUnits="userSpaceOnUse"><circle cx="1.500" cy="1.500" r="1" fill="#2a2a2a"/></pattern></defs>
  <path d="M40 330c40-60 120-40 160-90s60-120 160-130" stroke="#fff" stroke-width="26" fill="none" stroke-linecap="round" opacity=".9"/>
  <g stroke="#0f766e" stroke-width="3" stroke-dasharray="6 7" fill="none"><path d="M110 110 L200 200 L300 110"/><path d="M200 200 L200 300"/></g>
  <circle cx="200" cy="200" r="78" fill="#111"/><circle cx="200" cy="200" r="78" fill="url(#dots)"/>
  <circle cx="200" cy="200" r="64" fill="none" stroke="#0f766e" stroke-width="4"/>
  <g fill="none" stroke="#fff" stroke-width="7" stroke-linecap="round"><circle cx="176" cy="184" r="10"/><circle cx="224" cy="184" r="10"/><circle cx="200" cy="224" r="10"/><path d="M182 193l12 22M218 193l-12 22M186 184h28"/></g>
  <g><circle cx="110" cy="110" r="34" fill="#111"/><circle cx="110" cy="110" r="42" fill="none" stroke="#0f766e" stroke-width="3" stroke-dasharray="3 6"/>
     <path d="M98 104h24M98 112h24M98 120h14" stroke="#fff" stroke-width="4" stroke-linecap="round"/></g>
  <g><circle cx="300" cy="110" r="34" fill="#111"/><circle cx="300" cy="110" r="42" fill="none" stroke="#0f766e" stroke-width="3" stroke-dasharray="3 6"/>
     <path d="M286 110l9 9 18-18" stroke="#fff" stroke-width="5" fill="none" stroke-linecap="round"/></g>
  <g><circle cx="200" cy="310" r="30" fill="#111"/><path d="M188 310h24M200 298v24" stroke="#fff" stroke-width="5" stroke-linecap="round"/></g>
  <g fill="#0f766e"><path d="M60 200h18M69 191v18" stroke="#0f766e" stroke-width="5" stroke-linecap="round"/><path d="M330 250h18M339 241v18" stroke="#0f766e" stroke-width="5" stroke-linecap="round"/></g>
</svg>`;

function card(form) {
  return `<div class="login-wrap"><div class="login-card">
    <div class="login-form">${form}</div>
    <div class="login-art">${ART}
      <div class="float" style="left:22px;bottom:110px"><span class="badge ok">${icon('check', 14)}</span><div><b>Integration approved</b><small>TaskFlow · published v1.0.0</small></div></div>
      <div class="float" style="right:22px;top:60px"><span class="badge accent">${icon('wrench', 14)}</span><div><b>Repair proposed</b><small>Schema drift · awaiting review</small></div></div>
    </div></div></div>`;
}

export function renderLogin(app) {
  app.innerHTML = card(`
    <div class="brand">${LOGO}<div><b>Integra<span>AI</span></b></div></div>
    <h1>Welcome back</h1>
    <p>Sign in to build, approve and run integrations.</p>
    <form id="login">
      <div class="field"><label for="u">Username or email</label><input type="text" id="u" autocomplete="username" placeholder="Enter your username or email" required></div>
      <div class="field"><label for="pw">Password</label><input type="password" id="pw" autocomplete="current-password" required></div>
      <div class="row between small"><span class="muted">Accounts are created by your admin</span></div>
      <button class="btn primary" type="submit">Sign in</button>
      <div class="login-err" id="err" role="alert"></div>
    </form>
    <div class="login-feats"><span>${icon('spark', 14)} AI-built configurations</span><span>${icon('shield', 14)} Admin-approved activation</span><span>${icon('wrench', 14)} Self-healing proposals</span></div>`);
  $('#u').focus();
  $('#login').addEventListener('submit', async (e) => {
    e.preventDefault();
    const btn = e.target.querySelector('button');
    btn.disabled = true;
    $('#err').textContent = '';
    try {
      state.user = await api('/auth/login', { method: 'POST', body: { username: $('#u').value, password: $('#pw').value } });
      go('#/dashboard');
    } catch (err) {
      $('#err').textContent = err.message;
      btn.disabled = false;
    }
  });
}

export function renderInvite(app, token) {
  app.innerHTML = card(`
    <div class="brand">${LOGO}<div><b>Integra<span>AI</span></b></div></div>
    <h1>Join your team</h1>
    <p>Choose a username and password to accept the invitation.</p>
    <form id="inv">
      <div class="field"><label>Username</label><input type="text" name="username" pattern="[a-z0-9_.\\-]{3,32}" required></div>
      <div class="field"><label>Full name</label><input type="text" name="name"></div>
      <div class="field"><label>Password (10+ characters)</label><input type="password" name="password" minlength="10" required></div>
      <button class="btn primary" type="submit">Create account</button>
      <div class="login-err" id="err"></div>
    </form>`);
  $('#inv').addEventListener('submit', async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      await api('/auth/accept-invite', { method: 'POST', body: { token, username: f.get('username'), name: f.get('name'), password: f.get('password') } });
      state.user = null;
      go('#/dashboard');
    } catch (err) { $('#err').textContent = err.message; }
  });
}
