// Shared helpers: API client, escaping, icons, formatting, toast and modal.

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

export const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

export class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

export async function api(path, { method = 'GET', body } = {}) {
  const res = await fetch('/api/v1' + path, {
    method,
    credentials: 'same-origin',
    headers: { 'X-Requested-With': 'integra', ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}) },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch (e) { /* empty body */ }
  if (!res.ok) {
    let msg = data && data.detail;
    if (Array.isArray(msg)) msg = msg.map((d) => `${(d.loc || []).slice(1).join('.')}: ${d.msg}`).join('; ');
    if (res.status === 401 && path !== '/auth/login' && path !== '/auth/me') { location.hash = '#/login'; }
    throw new ApiError(res.status, msg || `Request failed (${res.status})`);
  }
  return data;
}

const ICONS = {
  home: 'M3 11l9-8 9 8M5 10v10h5v-6h4v6h5V10',
  search: 'M11 18a7 7 0 100-14 7 7 0 000 14zM20 20l-4-4',
  grid: 'M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z',
  spark: 'M12 3l2 6 6 2-6 2-2 6-2-6-6-2 6-2z',
  plug: 'M9 3v5M15 3v5M6 8h12v4a6 6 0 01-12 0zM12 18v3',
  flow: 'M5 5h5v5H5zM14 14h5v5h-5zM10 7.5h4a2 2 0 012 2V14',
  flask: 'M9 3h6M10 3v6L4.500 18.500A2 2 0 006.300 21h11.400a2 2 0 001.800-2.500L14 9V3M7 15h10',
  check: 'M9 12l2 2 4-4M12 3l8 3v6c0 5-3.500 8-8 9-4.500-1-8-4-8-9V6z',
  pulse: 'M3 12h4l3-8 4 16 3-8h4',
  key: 'M14 10a4 4 0 10-3.500 3.970L9 15.500V18H6.500v2.500H4V17l6.030-6.030A4 4 0 0014 10zM15.500 8.500h.01',
  cpu: 'M8 8h8v8H8zM5 5h14v14H5zM9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3',
  history: 'M3 12a9 9 0 109-9 9.700 9.700 0 00-6.700 2.700L3 8M3 3v5h5M12 7v5l3 2',
  box: 'M21 8l-9-5-9 5v8l9 5 9-5zM3 8l9 5 9-5M12 13v8',
  cog: 'M12 15a3 3 0 100-6 3 3 0 000 6zM19.400 15a1.700 1.700 0 00.300 1.800l.1.1a2 2 0 11-2.800 2.800l-.1-.1a1.700 1.700 0 00-1.800-.3 1.700 1.700 0 00-1 1.500V21a2 2 0 01-4 0v-.1A1.700 1.700 0 009 19.400a1.700 1.700 0 00-1.800.3l-.1.1a2 2 0 11-2.800-2.800l.1-.1A1.700 1.700 0 004.600 15a1.700 1.700 0 00-1.500-1H3a2 2 0 010-4h.1A1.700 1.700 0 004.600 9a1.700 1.700 0 00-.3-1.800l-.1-.1a2 2 0 112.800-2.800l.1.1A1.700 1.700 0 009 4.600a1.700 1.700 0 001-1.500V3a2 2 0 014 0v.1a1.700 1.700 0 001 1.500 1.700 1.700 0 001.800-.3l.1-.1a2 2 0 112.800 2.800l-.1.1a1.700 1.700 0 00-.3 1.800V9a1.700 1.700 0 001.500 1H21a2 2 0 010 4h-.1a1.700 1.700 0 00-1.500 1z',
  list: 'M4 6h16M4 12h16M4 18h16',
  bell: 'M18 8a6 6 0 10-12 0c0 7-3 9-3 9h18s-3-2-3-9M13.700 21a2 2 0 01-3.400 0',
  sun: 'M12 17a5 5 0 100-10 5 5 0 000 10zM12 2v2M12 20v2M4.900 4.900l1.400 1.400M17.700 17.700l1.400 1.400M2 12h2M20 12h2M4.900 19.100l1.400-1.400M17.700 6.300l1.400-1.400',
  moon: 'M21 12.800A9 9 0 1111.200 3a7 7 0 009.800 9.800z',
  logout: 'M10 4H5v16h5M15 8l4 4-4 4M19 12H9',
  send: 'M5 12h14M13 6l6 6-6 6',
  plus: 'M12 5v14M5 12h14',
  x: 'M6 6l12 12M18 6L6 18',
  up: 'M6 15l6-6 6 6',
  menu: 'M4 6h16M4 12h16M4 18h16',
  upload: 'M12 16V4M7 9l5-5 5 5M4 20h16',
  play: 'M7 4l13 8-13 8z',
  shield: 'M12 3l8 3v6c0 5-3.500 8-8 9-4.500-1-8-4-8-9V6z',
  alert: 'M12 3l10 18H2zM12 10v5M12 18v.5',
  wrench: 'M14.700 6.300a4 4 0 00-5.400 5.400L3 18l3 3 6.300-6.300a4 4 0 005.400-5.400l-2.500 2.500-2.400-.6-.6-2.400z',
  doc: 'M6 3h9l4 4v14H6zM14 3v5h5M9 13h7M9 17h7',
  user: 'M12 12a4 4 0 100-8 4 4 0 000 8zM4 21a8 8 0 0116 0',
  link: 'M10 14a5 5 0 007 0l3-3a5 5 0 00-7-7l-1 1M14 10a5 5 0 00-7 0l-3 3a5 5 0 007 7l1-1',
  trash: 'M4 7h16M10 11v6M14 11v6M5 7l1 13h12l1-13M9 7V4h6v3',
  copy: 'M9 9h11v11H9zM5 15H4V4h11v1',
  eye: 'M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12zM12 15a3 3 0 100-6 3 3 0 000 6z',
  refresh: 'M4 4v6h6M20 20v-6h-6M5.500 15A8 8 0 0019 13M18.500 9A8 8 0 005 11',
  chev: 'M9 6l6 6-6 6',
  db: 'M4 6c0-1.700 3.600-3 8-3s8 1.300 8 3-3.600 3-8 3-8-1.300-8-3zM4 6v6c0 1.700 3.600 3 8 3s8-1.300 8-3V6M4 12v6c0 1.700 3.600 3 8 3s8-1.300 8-3v-6',
  agent: 'M12 3v3M8 8h8a3 3 0 013 3v5a3 3 0 01-3 3H8a3 3 0 01-3-3v-5a3 3 0 013-3zM9.500 13h.01M14.500 13h.01M2 13v2M22 13v2',
};

export function icon(name, size = 18, extra = '') {
  return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" ${extra}><path d="${ICONS[name] || ICONS.box}"/></svg>`;
}

export const LOGO = `<svg width="30" height="30" viewBox="0 0 32 32" aria-hidden="true"><g fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M6 4v24M26 4v24"/><path d="M10 12h12M19 8.500l3.500 3.500-3.500 3.500"/><path d="M22 21H10M13 17.500 9.500 21l3.500 3.500"/></g></svg>`;

export function fmtTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts);
  return isNaN(d) ? esc(ts) : d.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

export function ago(ts) {
  if (!ts) return '—';
  const s = Math.max(0, (Date.now() - new Date(ts).getTime()) / 1000);
  if (isNaN(s)) return '—';
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}
export const agoCell = (ts) => `<span title="${esc(fmtTime(ts))}">${ago(ts)}</span>`;

const STATUS_CLASS = {
  published: 'ok', approved: 'ok', succeeded: 'ok', passed: 'ok', active: 'ok', done: 'ok', ready: 'ok', planned: 'info',
  pending_approval: 'high', pending: 'high', waiting: 'high', collecting: 'info', running: 'info', queued: 'info',
  draft: '', paused: '', superseded: 'low', skipped: 'low', cancelled: 'low', disabled: 'low', archived: 'low', revoked: 'low',
  rejected: 'critical', failed: 'critical', invalid: 'critical', changes_requested: 'medium', revision: 'medium', proposed: 'info',
  low: 'low', medium: 'medium', high: 'high', critical: 'critical', info: 'info',
  healthy: 'ok', failing: 'critical', stale: 'high', degraded: 'medium', partial: 'medium', success: 'ok', failure: 'critical', unknown: '',
};
export const badge = (s, label) => (s ? `<span class="badge ${STATUS_CLASS[s] ?? ''}">${esc(label ?? String(s).replace(/_/g, ' '))}</span>` : '');
export const method = (m) => `<span class="method ${esc(m)}">${esc(m)}</span>`;
export const json = (v) => `<pre>${esc(JSON.stringify(v, null, 2))}</pre>`;

export function md(text) {
  // Minimal, safe markdown: escape first, then bold, inline code, bullet lists, paragraphs.
  const lines = esc(text).split('\n');
  let out = '', list = false;
  for (const raw of lines) {
    const line = raw.replace(/\*\*(.+?)\*\*/g, '<b>$1</b>').replace(/`([^`]+)`/g, '<code>$1</code>');
    if (/^\s*[-*] /.test(line)) {
      if (!list) { out += '<ul>'; list = true; }
      out += `<li>${line.replace(/^\s*[-*] /, '')}</li>`;
    } else {
      if (list) { out += '</ul>'; list = false; }
      if (line.trim()) out += `<p>${line}</p>`;
    }
  }
  return out + (list ? '</ul>' : '');
}

let toastTimer;
export function toast(msg, kind = '') {
  const t = $('#toast');
  t.textContent = msg;
  t.className = `toast show ${kind}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.className = 'toast'; }, kind === 'bad' ? 6000 : 3200);
}

export function modal({ title, body, actions = [], wide = false, onOpen }) {
  // actions: [{label, cls, value, handler?}] -> resolves with the clicked action's value (handler may return false to keep open)
  return new Promise((resolve) => {
    const back = document.createElement('div');
    back.className = 'modal-back';
    back.innerHTML = `<div class="modal ${wide ? 'wide' : ''}" role="dialog" aria-modal="true" aria-label="${esc(title)}">
      <div class="modal-head"><h3>${esc(title)}</h3><button class="btn ghost icon" data-close aria-label="Close">${icon('x')}</button></div>
      <div class="modal-body">${body}</div>
      ${actions.length ? `<div class="modal-foot">${actions.map((a, i) => `<button class="btn ${a.cls || ''}" data-i="${i}">${esc(a.label)}</button>`).join('')}</div>` : ''}</div>`;
    const close = (v) => { back.remove(); document.removeEventListener('keydown', onKey); resolve(v); };
    const onKey = (e) => { if (e.key === 'Escape') close(null); };
    document.addEventListener('keydown', onKey);
    back.addEventListener('click', async (e) => {
      if (e.target === back || e.target.closest('[data-close]')) return close(null);
      const b = e.target.closest('[data-i]');
      if (!b) return;
      const a = actions[+b.dataset.i];
      if (a.handler) {
        b.disabled = true;
        try {
          const r = await a.handler(back);
          if (r === false) { b.disabled = false; return; }
          return close(r ?? a.value);
        } catch (err) { b.disabled = false; toast(err.message, 'bad'); return; }
      }
      close(a.value);
    });
    document.body.appendChild(back);
    if (onOpen) onOpen(back);
    const first = back.querySelector('input, textarea, select');
    if (first) first.focus();
  });
}

export const confirmBox = (title, text, label = 'Confirm', cls = 'primary') =>
  modal({ title, body: `<p style="margin:0">${esc(text)}</p>`, actions: [{ label: 'Cancel', value: false }, { label, cls, value: true }] });

export function pageHead(ico, title, sub, actions = '') {
  return `<div class="page-head"><div class="ph-title"><div class="ph-ico">${icon(ico, 20)}</div><div><h1>${esc(title)}</h1>${sub ? `<p>${sub}</p>` : ''}</div></div><div class="row">${actions}</div></div>`;
}

export const emptyState = (ico, title, text, action = '') =>
  `<div class="empty">${icon(ico, 40)}<h3>${esc(title)}</h3><p>${text}</p>${action}</div>`;

export function formData(root) {
  const out = {};
  $$('[name]', root).forEach((el) => {
    if (el.type === 'checkbox') out[el.name] = el.checked;
    else if (el.type === 'radio') { if (el.checked) out[el.name] = el.value; }
    else if (el.type === 'number') out[el.name] = el.value === '' ? null : Number(el.value);
    else out[el.name] = el.value;
  });
  return out;
}

export function readFile(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(r.result);
    r.onerror = () => reject(new Error('Could not read the file'));
    r.readAsText(file);
  });
}

// Form for an input JSON Schema (object of scalar/array properties). Returns HTML; read back with schemaValues.
export function schemaForm(schema, values = {}, prefix = 'in') {
  const props = (schema && schema.properties) || {};
  const req = new Set((schema && schema.required) || []);
  if (!Object.keys(props).length) return '<p class="muted small">This operation takes no inputs.</p>';
  return Object.entries(props).map(([k, p]) => {
    const id = `${prefix}-${k}`;
    const v = values[k] ?? p.default ?? '';
    const label = `<label for="${id}">${esc(k)}${req.has(k) ? ' <span style="color:var(--bad)">*</span>' : ''} <span class="muted small">${esc(p.type || '')}</span></label>`;
    let ctl;
    if (p.enum) ctl = `<select id="${id}" data-k="${esc(k)}" data-t="${esc(p.type || 'string')}"><option value="">—</option>${p.enum.map((o) => `<option ${String(o) === String(v) ? 'selected' : ''}>${esc(o)}</option>`).join('')}</select>`;
    else if (p.type === 'boolean') ctl = `<select id="${id}" data-k="${esc(k)}" data-t="boolean"><option value="">—</option><option ${v === true ? 'selected' : ''}>true</option><option ${v === false ? 'selected' : ''}>false</option></select>`;
    else if (p.type === 'array' || p.type === 'object') ctl = `<input type="text" id="${id}" data-k="${esc(k)}" data-t="${p.type}" value="${esc(typeof v === 'string' ? v : JSON.stringify(v))}" placeholder='${p.type === 'array' ? '["a","b"] or a, b' : '{"key": "value"}'}'>`;
    else ctl = `<input type="${p.type === 'integer' || p.type === 'number' ? 'number' : 'text'}" id="${id}" data-k="${esc(k)}" data-t="${esc(p.type || 'string')}" value="${esc(v)}">`;
    return `<div class="field">${label}${ctl}${p.description ? `<div class="hint">${esc(p.description)}</div>` : ''}</div>`;
  }).join('');
}

export function schemaValues(root) {
  const out = {};
  $$('[data-k]', root).forEach((el) => {
    const raw = el.value.trim();
    if (raw === '') return;
    const t = el.dataset.t;
    if (t === 'integer' || t === 'number') out[el.dataset.k] = Number(raw);
    else if (t === 'boolean') out[el.dataset.k] = raw === 'true';
    else if (t === 'array') {
      try { out[el.dataset.k] = JSON.parse(raw); } catch (e) { out[el.dataset.k] = raw.split(',').map((s) => s.trim()).filter(Boolean); }
    } else if (t === 'object') {
      try { out[el.dataset.k] = JSON.parse(raw); } catch (e) { throw new Error(`${el.dataset.k} must be JSON`); }
    } else out[el.dataset.k] = raw;
  });
  return out;
}

export function riskBadge(level, score) {
  return level ? badge(level, `${level}${score !== undefined && score !== null ? ` · ${score}` : ''}`) : '';
}
