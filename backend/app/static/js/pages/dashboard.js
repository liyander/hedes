import { api, esc, icon, badge, agoCell, emptyState } from '../lib.js';
import { state, go, isAdmin } from '../app.js';

export async function render({ el }) {
  const [m, integrations, alerts, execs] = await Promise.all([
    api('/monitoring/metrics'), api('/integrations'), api('/alerts'), api('/executions?limit=8'),
  ]);
  const first = (state.user.name || state.user.username).split(' ')[0];
  const rate = m.success_rate === null ? '—' : `${m.success_rate}%`;
  el.innerHTML = `<div class="page stack">
    <div class="hero">
      <span class="pill"><span class="dot on"></span>${m.tools} live tools · ${m.executions} runs in 24 h</span>
      <h1>Good to see you, ${esc(first)}</h1>
      <p>Describe an integration in plain language. The AI drafts and tests the configuration, then sends it to an administrator for approval.</p>
      <form class="ask" id="ask"><input type="text" id="ask-in" placeholder="e.g. When a GitHub issue is labeled security, create a Jira ticket and notify Slack" aria-label="Describe an integration">
        <button class="btn primary icon" aria-label="Start">${icon('send')}</button></form>
    </div>
    <div class="stats">
      <a class="card stat accent" href="#/integrations"><div class="ico">${icon('plug', 20)}</div><div><b>${m.integrations.published || 0}</b><span>Published integrations</span></div></a>
      <a class="card stat ${m.pending_approvals ? 'attn' : ''}" href="#/approvals"><div class="ico">${icon('check', 20)}</div><div><b>${m.pending_approvals}</b><span>Awaiting approval</span></div></a>
      <a class="card stat ${m.failed ? 'alert' : 'good'}" href="#/monitoring"><div class="ico">${icon('pulse', 20)}</div><div><b>${rate}</b><span>Success rate (24 h)</span></div></a>
      <a class="card stat ${m.open_repairs ? 'attn' : ''}" href="#/monitoring"><div class="ico">${icon('wrench', 20)}</div><div><b>${m.open_repairs}</b><span>Repairs proposed</span></div></a>
    </div>
    <div class="card card-pad">
      <div class="row between" style="margin-bottom:12px"><h2>How an integration goes live</h2><span class="muted small">AI plans · deterministic engine executes · humans approve</span></div>
      <div class="flow">
        ${[['spark', 'Describe', 'Chat with the AI and upload API docs'], ['doc', 'Generate', 'Declarative config + tool schemas'],
           ['shield', 'Validate & test', 'Schema, egress policy, mock tests'], ['check', 'Approve', 'An admin publishes the exact version'],
           ['pulse', 'Run & heal', 'Monitored; drift gets repair proposals']].map(([i, t, d]) => `<div class="step">${icon(i, 20)}<b>${t}</b><span>${d}</span></div>`).join('')}
      </div>
    </div>
    <div class="split">
      <div class="card">
        <div class="card-head"><h2>Integrations</h2><span class="spacer"></span><a class="btn sm" href="#/integrations">View all</a></div>
        ${integrations.length ? `<div class="table-scroll"><table><thead><tr><th>Name</th><th>Status</th><th>Version</th><th>Updated</th></tr></thead><tbody>
          ${integrations.slice(0, 7).map((i) => `<tr class="clickable" data-go="#/integrations/${i.id}"><td><b>${esc(i.name)}</b><div class="muted small mono">${esc(i.base_url || '')}</div></td>
            <td>${badge(i.status)}${i.latest_status && i.latest_status !== i.status && i.latest_status !== 'published' ? ` ${badge(i.latest_status)}` : ''}</td>
            <td class="mono">${esc(i.published_version || i.latest_version || '')}</td><td class="nowrap">${agoCell(i.updated_at)}</td></tr>`).join('')}
        </tbody></table></div>` : emptyState('plug', 'No integrations yet', 'Start one in AI Studio.')}
      </div>
      <div class="stack">
        <div class="card">
          <div class="card-head"><h2>Needs attention</h2></div>
          <div class="quick-list">
            ${isAdmin() && m.pending_approvals ? `<a href="#/approvals">${icon('check')}<div class="grow"><b>${m.pending_approvals} integration version(s) to review</b><div class="muted small">Approve or reject exact configurations</div></div>${icon('chev', 16)}</a>` : ''}
            ${alerts.slice(0, 5).map((a) => `<a href="${esc(a.link || '#/monitoring')}">${icon(a.kind === 'drift' || a.kind === 'repair' ? 'wrench' : a.kind === 'approval' ? 'check' : 'alert')}<div class="grow"><div class="small">${esc(a.message)}</div><div class="muted small">${agoCell(a.created_at)}</div></div></a>`).join('')}
            ${!alerts.length && !(isAdmin() && m.pending_approvals) ? '<p class="muted small" style="padding:4px 18px 14px">All clear.</p>' : ''}
          </div>
        </div>
        <div class="card">
          <div class="card-head"><h2>Recent executions</h2><span class="spacer"></span><a class="btn sm" href="#/monitoring">Monitoring</a></div>
          <div class="quick-list">${execs.length ? execs.map((e) => `<a href="#/monitoring/execution/${e.id}">${badge(e.status)}<div class="grow"><div class="mono small">${esc(e.tool)}</div><div class="muted small">${e.duration_ms} ms · ${esc(e.actor_type)} · ${agoCell(e.created_at)}</div></div></a>`).join('')
            : '<p class="muted small" style="padding:4px 18px 14px">No executions yet. Run a tool from the Tool Registry or Test Lab.</p>'}</div>
        </div>
      </div>
    </div>
  </div>`;
  el.querySelector('#ask').addEventListener('submit', (e) => {
    e.preventDefault();
    const v = el.querySelector('#ask-in').value.trim();
    sessionStorage.setItem('integra-prompt', v);
    go('#/studio/new');
  });
  el.addEventListener('click', (e) => { const r = e.target.closest('[data-go]'); if (r) go(r.dataset.go); });
}
