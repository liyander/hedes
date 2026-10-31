import { $, api, icon, LOGO } from '../lib.js';
import { state, go } from '../app.js';

const ART = `<svg viewBox="0 0 320 240" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.5">
  <rect x="24" y="24" width="96" height="40" rx="6"/><rect x="200" y="24" width="96" height="40" rx="6"/>
  <rect x="112" y="100" width="96" height="40" rx="6"/><rect x="112" y="176" width="96" height="40" rx="6"/>
  <path d="M72 64v16q0 20 40 20M248 64v16q0 20-40 20M160 140v36" stroke-dasharray="4 4" opacity=".6"/>
  <path d="M40 44h40M40 54h24M216 44h40M216 54h24M128 120h40M128 196h56" stroke-linecap="round" opacity=".5"/>
</svg>`;

function card(form) {
  return `<div class="login-wrap"><div class="login-card">
    <div class="login-form">${form}</div>
    <div class="login-art">${ART}
    </div></div></div>`;
}

export async function renderLogin(app) {
  let setup = false;
  try { setup = (await api('/auth/setup')).needs_setup; } catch (e) { /* server unreachable: show the sign-in form */ }
  if (setup) return renderSetup(app);
  app.innerHTML = card(`
    <div class="brand">${LOGO}<div><b>H.E.D.E.S</b><small>Hybrid Automated Data Exchange System</small></div></div>
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

// First run: no account exists yet. The first person creates the organisation and its administrator (owner).
function renderSetup(app) {
  app.innerHTML = card(`
    <div class="brand">${LOGO}<div><b>H.E.D.E.S</b><small>Hybrid Automated Data Exchange System</small></div></div>
    <h1>Set up H.E.D.E.S</h1>
    <p>No accounts exist yet. Create the administrator account. It has full control, including AI providers and user management;
      everyone else is added by an administrator afterwards.</p>
    <form id="setup">
      <div class="field"><label>Organisation name</label><input type="text" name="organization" required maxlength="120" placeholder="e.g. Acme Security"></div>
      <div class="grid-2"><div class="field"><label>Username</label><input type="text" name="username" required pattern="[a-z0-9_.\-]{3,32}" autocomplete="username" placeholder="lowercase, 3-32 chars"></div>
        <div class="field"><label>Full name</label><input type="text" name="name" maxlength="120"></div></div>
      <div class="field"><label>Email</label><input type="email" name="email" required autocomplete="email"></div>
      <div class="grid-2"><div class="field"><label>Password (10+ characters)</label><input type="password" name="password" required minlength="10" autocomplete="new-password"></div>
        <div class="field"><label>Repeat password</label><input type="password" name="password2" required minlength="10" autocomplete="new-password"></div></div>
      <button class="btn primary" type="submit">Create administrator</button>
      <div class="login-err" id="err" role="alert"></div>
    </form>`);
  $('[name=organization]').focus();
  $('#setup').addEventListener('submit', async (e) => {
    e.preventDefault();
    const f = Object.fromEntries(new FormData(e.target));
    if (f.password !== f.password2) { $('#err').textContent = 'The passwords do not match'; return; }
    const btn = e.target.querySelector('button');
    btn.disabled = true;
    $('#err').textContent = '';
    try {
      state.user = await api('/auth/setup', { method: 'POST', body: { organization: f.organization, username: f.username, name: f.name, email: f.email, password: f.password } });
      go('#/providers');
    } catch (err) {
      $('#err').textContent = err.message;
      btn.disabled = false;
    }
  });
}

export function renderInvite(app, token) {
  app.innerHTML = card(`
    <div class="brand">${LOGO}<div><b>H.E.D.E.S</b><small>Hybrid Automated Data Exchange System</small></div></div>
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
