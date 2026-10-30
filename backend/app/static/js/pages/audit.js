import { $, api, esc, agoCell, pageHead, emptyState } from '../lib.js';
import { isAdmin } from '../app.js';

const GROUPS = ['', 'approval', 'integration', 'version', 'tool', 'connection', 'workflow', 'repair', 'ai', 'member', 'policy', 'auth'];

export async function render({ el }) {
  const paint = async (action) => {
    const rows = await api(`/audit${action ? `?action=${encodeURIComponent(action)}` : ''}`);
    $('#rows', el).innerHTML = rows.length ? `<div class="table-scroll"><table><thead><tr><th>When</th><th>Actor</th><th>Action</th><th>Target</th><th>Details</th></tr></thead><tbody>
      ${rows.map((a) => `<tr><td class="nowrap">${agoCell(a.at)}</td><td>${esc(a.actor)}</td><td><code>${esc(a.action)}</code></td><td class="mono small">${esc(a.target)}</td>
        <td class="wrap small muted mono">${esc(Object.entries(a.metadata || {}).map(([k, v]) => `${k}=${typeof v === 'object' ? JSON.stringify(v) : v}`).join('  '))}</td></tr>`).join('')}
      </tbody></table></div>` : emptyState('list', 'No events', '');
  };
  el.innerHTML = `<div class="page wide">
    ${pageHead('list', 'Audit log', isAdmin() ? 'Every governance and runtime action in your organisation.' : 'Your own actions.',
      `<select id="grp" style="width:auto">${GROUPS.map((g) => `<option value="${g}">${g || 'All actions'}</option>`).join('')}</select>`)}
    <div class="card" id="rows"></div></div>`;
  await paint('');
  $('#grp', el).addEventListener('change', (e) => paint(e.target.value));
}
