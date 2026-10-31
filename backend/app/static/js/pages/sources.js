import { $, api, esc, icon, badge, agoCell, pageHead, emptyState, modal, toast, confirmBox, fmtTime } from '../lib.js';
import { route, isAdmin, state } from '../app.js';
import { askAbout } from '../assistant.js';

const HEALTH_LABEL = { healthy: 'Healthy', failing: 'Failing', stale: 'Stale', degraded: 'Bad data', paused: 'Paused', pending: 'Not run yet', running: 'Running' };
const AUTH_LABEL = { ssws: 'Okta SSWS', aws_sigv4: 'AWS SigV4', api_key_header: 'API key', bearer: 'Bearer', oauth2_client_credentials: 'OAuth2', basic: 'Basic', api_key_query: 'API key', none: 'None' };
const PAGE_LABEL = { link: 'Link header', cursor: 'Cursor / token', offset: 'Offset', page: 'Page number', none: 'Single page' };
const RUN_CLASS = { succeeded: 'ok', partial: 'warn', failed: 'bad', running: 'run' };

const spark = (runs) => `<span class="spark" aria-label="Recent runs">${runs.length ? runs.map((r) => `<i class="${RUN_CLASS[r] || ''}" title="${esc(r)}"></i>`).join('') : '<span class="muted small">—</span>'}</span>`;
const until = (ts) => {
  const s = (new Date(ts).getTime() - Date.now()) / 1000;
  if (isNaN(s)) return '—';
  if (s < 60) return s < 0 ? 'due now' : 'in < 1 min';
  return s < 3600 ? `in ${Math.round(s / 60)} min` : `in ${Math.round(s / 3600)} h`;
};
const fmtNum = (n) => Number(n || 0).toLocaleString();

function credCell(c) {
  if (!c) return '<span class="muted">none</span>';
  const warn = c.status === 'expired' ? '<span class="badge critical">expired</span>'
    : c.status === 'missing' ? '<span class="badge critical">missing</span>'
    : c.status !== 'active' ? badge(c.status)
    : c.expires_at && new Date(c.expires_at) - Date.now() < 7 * 864e5 ? '<span class="badge high">expires soon</span>'
    : c.age_days > 90 ? '<span class="badge medium">rotate</span>' : '';
  return `<a class="link mono small" href="#/credentials">${esc(c.name)}</a> ${warn}<div class="muted small">${c.age_days != null ? `${c.age_days} d old` : ''}${c.expires_at ? ` · expires ${esc(fmtTime(c.expires_at))}` : ''}</div>`;
}

function healthTable(list) {
  if (!list.length) return emptyState('db', 'No data sources yet', 'Add a real source (Okta, GitHub, Jira, NVD, AbuseIPDB, CISA KEV) from a form. Once an administrator approves it, it appears here and starts running.', '<a class="btn primary" href="#/integrations/sources">Add data source</a>');
  return `<div class="table-scroll"><table><thead><tr><th>Connection</th><th>Health</th><th>Last run</th><th>Next run</th><th>Recent runs</th><th>Ingested</th><th>Credential</th><th></th></tr></thead><tbody>
    ${list.map((s) => `<tr>
      <td><b>${esc(s.integration)}</b><div class="muted small"><span class="badge accent">${esc(s.entity)}</span> ${esc(AUTH_LABEL[s.auth_type] || s.auth_type)} · ${esc(PAGE_LABEL[s.pagination] || s.pagination)}${s.rate_limit ? ` · ${s.rate_limit}/min cap` : ''}</div></td>
      <td>${badge(s.health, HEALTH_LABEL[s.health])}<div class="small ${s.health === 'failing' ? '' : 'muted'}" style="max-width:240px">${esc(s.health_reason)}</div></td>
      <td class="nowrap">${s.last_run_at ? agoCell(s.last_run_at) : '<span class="muted">never</span>'}<div class="muted small">${s.last_success_at ? `ok ${esc(new Date(s.last_success_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }))}` : ''}</div></td>
      <td class="nowrap">${s.running ? '<span class="spinner"></span>' : s.next_run_at ? until(s.next_run_at) : '<span class="muted">—</span>'}<div class="muted small">${s.cron ? `cron <code>${esc(s.cron)}</code>` : `every ${s.interval_minutes} min`}</div></td>
      <td>${spark(s.recent)}</td>
      <td class="small"><b>${fmtNum(s.totals.stored)}</b> stored<div class="muted">${s.totals.updated ? `${fmtNum(s.totals.updated)} updated · ` : ''}${fmtNum(s.totals.duplicates)} dup · <span ${s.totals.rejected ? 'style="color:var(--warn)"' : ''}>${fmtNum(s.totals.rejected)} rejected</span></div></td>
      <td>${credCell(s.credential)}</td>
      <td class="nowrap"><button class="btn sm" data-test="${s.id}" ${s.enabled ? '' : 'disabled'} title="Fetch one page and preview the mapped records. Nothing is stored.">Test</button> <button class="btn sm" data-run="${s.id}" ${s.enabled ? '' : 'disabled'}>Run now</button> <button class="btn sm ghost" data-detail="${s.id}">Details</button></td></tr>`).join('')}
  </tbody></table></div>`;
}

async function detailModal(id) {
  const s = await api(`/sources/${id}`);
  const err = s.last_error ? `<div class="callout bad small" style="margin-bottom:14px"><b>${esc(s.error_meaning || s.last_error_class)}</b><div>${esc(s.error_fix)}</div><div class="mono muted" style="margin-top:4px;overflow-wrap:anywhere">${esc(s.last_error.slice(0, 300))}</div></div>` : '';
  const body = `${err}
    <dl class="kv" style="margin-bottom:16px">
      <dt>Health</dt><dd>${badge(s.health, HEALTH_LABEL[s.health])} ${esc(s.health_reason)}</dd>
      <dt>Operation</dt><dd class="mono">${esc(s.slug)}.${esc(s.operation_id)}</dd>
      <dt>High-water mark</dt><dd class="mono">${esc(s.watermark || 'none yet - the next run reads everything')}</dd>
      <dt>Schedule</dt><dd>${s.cron ? `cron <code>${esc(s.cron)}</code> (UTC)` : `every ${s.interval_minutes} min`}${s.consecutive_failures ? ` · backing off after ${s.consecutive_failures} failure(s)` : ''}</dd>
      <dt>Format</dt><dd><span class="badge accent">${esc(s.entity)}</span> ${esc(s.category || '')}</dd>
      <dt>Credential</dt><dd>${credCell(s.credential)}</dd>
    </dl>
    ${isAdmin() ? `<div class="card card-pad" style="margin-bottom:16px"><h4 style="margin:0 0 8px">Schedule</h4><div class="grid-2">
        <div class="field"><label>Every N minutes</label><input type="number" id="sch-int" min="1" max="1440" value="${s.interval_minutes}"></div>
        <div class="field"><label>Or a cron expression (UTC)</label><input type="text" id="sch-cron" value="${esc(s.cron)}" placeholder="*/15 * * * *"><div class="hint">minute hour day-of-month month day-of-week. Empty = use the interval.</div></div></div>
        <div class="row end"><button class="btn sm primary" id="sch-save">Save schedule</button></div></div>` : ''}
    <h4 style="margin:0 0 6px">Mapping into the common format</h4>
    <div class="table-scroll" style="margin-bottom:16px"><table class="small"><thead><tr><th>Common field</th><th>Source path</th></tr></thead><tbody>
      ${Object.entries(s.fields).map(([k, v]) => `<tr><td class="mono">${esc(k)}</td><td class="mono">${esc(typeof v === 'string' ? v : JSON.stringify(v))}</td></tr>`).join('')}</tbody></table></div>
    <h4 style="margin:0 0 6px">Run history</h4>
    <div class="table-scroll"><table class="small"><thead><tr><th>Started</th><th>Result</th><th>Fetched</th><th>Stored</th><th>Dup</th><th>Rejected</th><th>Requests</th><th>Time</th></tr></thead><tbody>
      ${s.runs.map((r) => `<tr><td class="nowrap">${agoCell(r.started_at)} <span class="muted">${esc(r.trigger)}</span></td><td>${badge(r.status)}${r.error_class ? ` <code>${esc(r.error_class)}</code>` : ''}</td>
        <td>${r.fetched}</td><td>${r.stored}</td><td>${r.duplicates}</td><td>${r.rejected}</td><td>${r.attempts}</td><td>${r.duration_ms} ms</td></tr>`).join('') || '<tr><td colspan="8" class="muted">No runs yet.</td></tr>'}</tbody></table></div>`;
  const actions = [{ label: 'Close', value: null }, { label: 'Discuss with AI', value: 'discuss' }];
  if (isAdmin()) {
    actions.unshift({ label: s.enabled ? 'Pause' : 'Resume', value: 'toggle' }, { label: 'Re-read full history', value: 'reset' });
  }
  const act = await modal({ title: s.integration, body, wide: true, actions, onOpen: (root) => {
    root.querySelector('#sch-save')?.addEventListener('click', async () => {
      try {
        await api(`/sources/${id}`, { method: 'PATCH', body: { interval_minutes: +root.querySelector('#sch-int').value, cron: root.querySelector('#sch-cron').value.trim() } });
        toast('Schedule saved', 'ok'); root.remove(); route();
      } catch (err) { toast(err.message, 'bad'); }
    });
  } });
  if (act === 'discuss') askAbout({ type: 'source', id, label: `${s.integration} · ${s.operation_id}` }, `Explain the health of ${s.integration} and what I should do.`);
  if (act === 'toggle') { await api(`/sources/${id}`, { method: 'PATCH', body: { enabled: !s.enabled } }); toast(s.enabled ? 'Source paused' : 'Source resumed', 'ok'); route(); }
  if (act === 'reset' && await confirmBox('Re-read full history', 'The next run ignores the saved high-water mark and fetches everything again. Events stay unique, so nothing is duplicated.', 'Reset')) {
    await api(`/sources/${id}/reset`, { method: 'POST' }); toast('High-water mark cleared', 'ok'); route();
  }
}

const ENTITY_COLS = {
  user: [['Name', 'name'], ['Email', 'email'], ['Status', 'status'], ['Last login', 'last_login'], ['Source', 'source'], ['Fetched', 'fetched_at']],
  device: [['Hostname', 'hostname'], ['IP', 'ip'], ['OS', 'os'], ['Owner', 'owner'], ['Last seen', 'last_seen'], ['Source', 'source'], ['Fetched', 'fetched_at']],
};
let kind = 'event';

function eventsPane(sources) {
  return `<div class="card"><div class="card-head">
      <div class="tabs"><button data-kind="event" class="${kind === 'event' ? 'on' : ''}">Events &amp; alerts</button><button data-kind="user" class="${kind === 'user' ? 'on' : ''}">Users</button><button data-kind="device" class="${kind === 'device' ? 'on' : ''}">Devices</button></div>
      <select id="f-source" style="width:auto"><option value="">All sources</option>${Object.keys(sources).map((k) => `<option value="${esc(k)}">${esc(k)} (${fmtNum(sources[k])})</option>`).join('')}</select>
      ${kind === 'event' ? `<select id="f-sev" style="width:auto"><option value="">Any severity</option>${['critical', 'high', 'medium', 'low', 'info'].map((v) => `<option>${v}</option>`).join('')}</select>
      <select id="f-out" style="width:auto"><option value="">Any outcome</option>${['success', 'failure', 'unknown'].map((v) => `<option>${v}</option>`).join('')}</select>` : ''}
      <input type="text" id="f-q" placeholder="Search…" style="width:200px"><span class="spacer"></span><span class="muted small" id="ev-count"></span></div>
    <div id="ev-body"></div></div>`;
}

async function loadEvents(el) {
  const qs = new URLSearchParams({ limit: '50' });
  for (const [k, id] of [['source', 'f-source'], ['severity', 'f-sev'], ['outcome', 'f-out'], ['q', 'f-q']]) { const n = $('#' + id, el); if (n && n.value) qs.set(k, n.value); }
  if (kind !== 'event') {
    const r = await api(`/entities/${kind}?${qs}`);
    $('#ev-count', el).textContent = `${fmtNum(r.total)} ${kind}(s)`;
    const cols = ENTITY_COLS[kind];
    $('#ev-body', el).innerHTML = r.items.length ? `<div class="table-scroll"><table><thead><tr>${cols.map(([h]) => `<th>${h}</th>`).join('')}</tr></thead><tbody>
      ${r.items.map((row) => `<tr>${cols.map(([, k]) => `<td class="small ${k === 'source' ? 'mono' : ''}">${k.endsWith('_at') || k === 'last_login' || k === 'last_seen' ? (row[k] ? agoCell(row[k]) : '<span class="muted">—</span>') : k === 'status' ? badge(row[k] === 'active' ? 'ok' : row[k] ? 'medium' : '', row[k] || '—') : esc(row[k] || '—')}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`
      : emptyState('list', `No ${kind}s yet`, `Add a source whose ingest block uses the ${kind} format (for example Okta users or Jira users).`);
    return;
  }
  const r = await api('/events?' + qs);
  $('#ev-count', el).textContent = `${fmtNum(r.total)} event(s)`;
  $('#ev-body', el).innerHTML = r.items.length ? `<div class="table-scroll"><table><thead><tr><th>Time</th><th>Source</th><th>Severity</th><th>Outcome</th><th>Actor</th><th>Action</th><th>Target</th></tr></thead><tbody>
    ${r.items.map((e) => `<tr class="clickable" data-event="${e.id}"><td class="nowrap">${agoCell(e.timestamp)}</td><td class="mono small">${esc(e.source)}</td><td>${badge(e.severity)}</td><td>${badge(e.outcome)}</td>
      <td class="small">${esc(e.actor)}</td><td class="mono small">${esc(e.action.slice(0, 90))}</td><td class="small">${esc(e.target)}</td></tr>`).join('')}</tbody></table></div>`
    : emptyState('list', 'No events match', 'Run a source, or loosen the filters.');
}

async function deadPane() {
  const r = await api('/dead-letters');
  if (!r.items.length) return `<div class="card">${emptyState('check', 'No rejected records', 'Records that cannot be mapped into the common format are parked here instead of failing a run.')}</div>`;
  return `<div class="card"><div class="card-head"><h2>Rejected records</h2><span class="muted small">${fmtNum(r.total)} parked · the rest of each run was ingested normally</span></div>
    ${r.items.map((d) => `<div class="list-row" style="align-items:flex-start"><div class="grow"><div class="small"><span class="mono">${esc(d.source)}</span> · ${agoCell(d.created_at)}</div>
      <ul class="small" style="margin:4px 0 6px;padding-left:18px;color:var(--bad)">${d.reasons.map((x) => `<li>${esc(x)}</li>`).join('')}</ul>
      <pre style="max-height:110px;font-size:12px">${esc(JSON.stringify(d.raw))}</pre></div>
      <div class="row"><button class="btn sm" data-reprocess="${d.id}">Reprocess</button>${isAdmin() ? `<button class="btn sm danger" data-dismiss="${d.id}">Dismiss</button>` : ''}</div></div>`).join('')}</div>`;
}

export async function render({ el, params }) {
  const data = await api('/sources');
  const s = data.summary;
  const attention = s.failing + s.stale + s.degraded;
  const dead = data.sources.reduce((n, x) => n + x.dead_letters, 0);
  let tab = params[0] === 'events' ? 'events' : params[0] === 'rejected' ? 'rejected' : 'health';
  let timer;

  const paint = async () => {
    const pane = $('#pane', el);
    if (tab === 'health') pane.innerHTML = `<div class="card">${healthTable(data.sources)}</div>`;
    else if (tab === 'events') { pane.innerHTML = eventsPane(await api(kind === 'event' ? '/events?limit=1' : `/entities/${kind}?limit=1`).then((r) => r.sources)); await loadEvents(el); }
    else pane.innerHTML = await deadPane();
    el.querySelectorAll('[data-tab]').forEach((b) => b.classList.toggle('on', b.dataset.tab === tab));
  };

  el.innerHTML = `<div class="page wide">
    ${pageHead('db', 'Data sources', 'Each connection pulls records on a schedule and maps them into one common format (events, users or devices). The platform handles retries, rate limits, de-duplication and bad data.',
      `<a class="btn primary" href="#/integrations/sources">${icon('plus', 15)} Add data source</a>${isAdmin() && state.demo ? '<button class="btn" id="simulate">Simulate new data (demo)</button><button class="btn ghost" id="flaky">Toggle firewall throttling (demo)</button>' : ''}`)}
    <div class="stats" style="margin-bottom:18px">
      <div class="card stat ${attention ? 'attn' : 'good'}"><div class="ico"></div><div><b>${s.healthy} / ${data.sources.length}</b><span>Connections healthy</span></div></div>
      <div class="card stat ${s.failing ? 'alert' : ''}"><div class="ico"></div><div><b>${s.failing + s.stale}</b><span>Failing or stale</span></div></div>
      <div class="card stat"><div class="ico"></div><div><b>${fmtNum(data.events_total)}</b><span>Events in common format</span></div></div>
      <div class="card stat ${dead ? 'attn' : ''}"><div class="ico"></div><div><b>${fmtNum(dead)}</b><span>Rejected records</span></div></div>
    </div>
    <div class="row between" style="margin-bottom:12px"><div class="tabs"><button data-tab="health">Connections</button><button data-tab="events">Data</button><button data-tab="rejected">Rejected${dead ? ` (${dead})` : ''}</button></div>
      <span class="muted small">Common format: ${data.common_format.map((f) => `<code>${f}</code>`).join(' ')}</span></div>
    <div id="pane"></div></div>`;
  await paint();

  el.addEventListener('click', async (e) => {
    const t = e.target.closest('[data-tab], [data-run], [data-test], [data-kind], [data-detail], [data-event], [data-reprocess], [data-dismiss], #simulate, #flaky');
    if (!t) return;
    if (t.dataset.tab) { tab = t.dataset.tab; history.replaceState(null, '', `#/sources/${tab === 'health' ? '' : tab}`); await paint(); }
    else if (t.dataset.kind) { kind = t.dataset.kind; await paint(); }
    else if (t.dataset.test) {
      t.disabled = true; t.textContent = 'Testing…';
      try {
        const r = await api(`/sources/${t.dataset.test}/test`, { method: 'POST' });
        if (!r.ok) {
          await modal({ title: 'Test failed', body: `<div class="callout bad small"><b>${esc(r.error_class)}</b> ${esc(r.error)}</div><p class="small muted">Nothing was stored.</p>`, actions: [{ label: 'Close', value: null }] });
        } else {
          await modal({ title: `Test passed · ${r.fetched} record(s) in one page`, wide: true, actions: [{ label: 'Close', value: null }],
            body: `<p class="small muted" style="margin-top:0">Nothing was stored. This is how the first records map into the <b>${esc(r.entity)}</b> format${r.rejected_count ? ` (${r.rejected_count} record(s) in this page would be rejected)` : ''}.</p>
              <pre>${esc(JSON.stringify(r.sample.slice(0, 3), null, 2))}</pre>
              ${r.rejected.length ? `<h4 class="small muted">WOULD BE REJECTED</h4>${r.rejected.map((x) => `<div class="callout warn small">${esc(x.reasons.join('; '))}</div>`).join('')}` : ''}` });
        }
      } catch (err) { toast(err.message, 'bad'); }
      route();
    } else if (t.dataset.run) {
      t.disabled = true; t.textContent = 'Running…';
      try {
        const r = await api(`/sources/${t.dataset.run}/run`, { method: 'POST' });
        const x = r.run;
        toast(x.status === 'failed' ? `Run failed: ${x.error_class}` : `${x.stored} new, ${x.duplicates} duplicate, ${x.rejected} rejected (${x.fetched} fetched)`, x.status === 'failed' ? 'bad' : 'ok');
      } catch (err) { toast(err.message, 'bad'); }
      route();
    } else if (t.dataset.detail) await detailModal(t.dataset.detail);
    else if (t.dataset.event) {
      const ev = await api(`/events/${t.dataset.event}`);
      const { raw, ...common } = ev;
      modal({ title: `${ev.source} · ${ev.action}`, wide: true, actions: [{ label: 'Close', value: null }],
        body: `<div class="split even"><div><h4 style="margin:0 0 6px">Common format</h4><pre>${esc(JSON.stringify(common, null, 2))}</pre></div><div><h4 style="margin:0 0 6px">Original record</h4><pre>${esc(JSON.stringify(raw, null, 2))}</pre></div></div>` });
    } else if (t.dataset.reprocess) {
      const r = await api(`/dead-letters/${t.dataset.reprocess}/reprocess`, { method: 'POST' });
      toast(r.stored ? 'Record stored' : `Still invalid: ${r.reasons[0]}`, r.stored ? 'ok' : 'bad');
      if (r.stored) route();
    } else if (t.dataset.dismiss) {
      if (await confirmBox('Dismiss record', 'The rejected record is deleted.', 'Dismiss', 'danger-solid')) { await api(`/dead-letters/${t.dataset.dismiss}`, { method: 'DELETE' }); route(); }
    } else if (t.id === 'simulate') {
      await api('/demo/feeds', { method: 'POST', body: { okta: 8, aws: 6, firewall: 12 } });
      toast('New records added to the mock sources. Run a source to ingest only what is new.', 'ok');
    } else if (t.id === 'flaky') {
      const st = await api('/demo/state');
      await api('/demo/feeds', { method: 'POST', body: { firewall_flaky: !st.firewall_flaky } });
      toast(!st.firewall_flaky ? 'Firewall now rate-limits every third request. Run it: retries honour Retry-After.' : 'Firewall throttling off', 'ok');
    }
  });
  el.addEventListener('input', (e) => { if (e.target.closest('#f-q')) { clearTimeout(timer); timer = setTimeout(() => loadEvents(el), 250); } });
  el.addEventListener('change', (e) => { if (e.target.closest('#f-source, #f-sev, #f-out')) loadEvents(el); });
  const poll = setInterval(() => { if (tab === 'health' && !document.querySelector('.modal-back')) route(); }, 30000);
  return () => { clearInterval(poll); clearTimeout(timer); };
}
