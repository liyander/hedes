import { api, esc, badge, agoCell, pageHead, emptyState, riskBadge } from '../lib.js';
import { go } from '../app.js';

export async function render({ el }) {
  const rows = await api('/versions');
  el.innerHTML = `<div class="page wide">
    ${pageHead('history', 'Version History', 'Every configuration version across integrations. Versions are immutable once submitted; changes, repairs and rollbacks always create or restore an approved hash.')}
    <div class="card">${rows.length ? `<div class="table-scroll"><table><thead><tr><th>Integration</th><th>Version</th><th>Status</th><th>Source</th><th>Risk</th><th>Tests</th><th>Hash</th><th>Author</th><th>Created</th></tr></thead><tbody>
      ${rows.map((v) => `<tr class="clickable" data-go="#/integrations/${v.integration_id}/${v.id}/versions"><td><b>${esc(v.integration)}</b> ${v.is_live ? badge('published', 'live') : ''}</td>
        <td class="mono">${esc(v.version)}</td><td>${badge(v.status)}</td><td>${v.source === 'repair' ? badge('info', 'AI repair') : esc(v.source)}</td>
        <td>${riskBadge(v.risk.level, v.risk.score)}</td><td>${v.tests ? `${v.tests.passed}/${v.tests.total}` : '—'}</td>
        <td class="mono small">${esc(v.config_hash.slice(0, 10))}</td><td class="small">${esc(v.created_by)}</td><td class="nowrap">${agoCell(v.created_at)}</td></tr>`).join('')}
    </tbody></table></div>` : emptyState('history', 'No versions yet', '')}</div></div>`;
  el.addEventListener('click', (e) => { const r = e.target.closest('[data-go]'); if (r) go(r.dataset.go); });
}
