import { api, esc, icon } from './lib.js';

let cache = null;
export const setDrawer = (name, on) => document.body.classList.toggle(name, on);
const key = (page) => `hedes-help-seen:${page}`;
const seen = (page) => { try { return localStorage.getItem(key(page)) === '1'; } catch (e) { return false; } };
const markSeen = (page) => { try { localStorage.setItem(key(page), '1'); } catch (e) { /* ignore */ } };

async function content() { if (!cache) { try { cache = await api('/help'); } catch (e) { cache = { pages: {}, concepts: [] }; } } return cache; }

function panelHtml(page, c) {
  const p = c.pages[page];
  if (!p) return '<p class="muted small">No guide for this page.</p>';
  return `<h3 style="margin:0 0 4px">${esc(p.title)}</h3><p class="small" style="margin:0 0 12px">${esc(p.summary)}</p>
    ${p.steps?.length ? `<h4 class="small muted">HOW TO USE IT</h4><ol class="small guide-list">${p.steps.map((s) => `<li>${esc(s)}</li>`).join('')}</ol>` : ''}
    ${p.options?.length ? `<h4 class="small muted">OPTIONS</h4><dl class="small guide-dl">${p.options.map((o) => `<dt>${esc(o.name)}</dt><dd>${esc(o.text)}</dd>`).join('')}</dl>` : ''}
    ${p.tips?.length ? `<h4 class="small muted">GOOD TO KNOW</h4><ul class="small guide-list">${p.tips.map((t) => `<li>${esc(t)}</li>`).join('')}</ul>` : ''}
    <details class="small" style="margin-top:12px"><summary>Key concepts</summary><dl class="guide-dl">${c.concepts.map((k) => `<dt>${esc(k.term)}</dt><dd>${esc(k.text)}</dd>`).join('')}</dl></details>`;
}

export async function setupHelp() {
  if (document.getElementById('help-drawer')) return;
  const d = document.createElement('aside');
  d.id = 'help-drawer'; d.className = 'drawer right help'; d.hidden = true;
  d.innerHTML = `<div class="drawer-head"><b>${icon('doc', 16)} About this page</b><span class="grow"></span><button class="btn ghost icon" id="help-close" aria-label="Hide this guide">${icon('x')}</button></div>
    <div class="drawer-body" id="help-body"></div>
    <div class="drawer-foot"><button class="btn sm" id="help-ask">${icon('spark', 14)} Ask the AI about this page</button></div>`;
  document.body.appendChild(d);
  d.querySelector('#help-close').addEventListener('click', () => { d.hidden = true; setDrawer('drawer-help', false); markSeen(d.dataset.page); });
  d.querySelector('#help-ask').addEventListener('click', () => {
    d.hidden = true; setDrawer('drawer-help', false);
    window.dispatchEvent(new CustomEvent('hedes:ask', { detail: { text: 'Explain this page and what I can do here.' } }));
  });
}

export async function showHelp(page, { auto = false } = {}) {
  await setupHelp();
  const d = document.getElementById('help-drawer');
  if (auto && seen(page)) return;
  const c = await content();
  d.dataset.page = page;
  d.querySelector('#help-body').innerHTML = panelHtml(page, c);
  document.getElementById('assistant-drawer')?.setAttribute('hidden', '');
  setDrawer('drawer-ai', false);
  document.getElementById('ask-ai')?.removeAttribute('hidden');
  d.hidden = false; setDrawer('drawer-help', true);
  markSeen(page);
}

export function toggleHelp(page) {
  const d = document.getElementById('help-drawer');
  if (d && !d.hidden && d.dataset.page === page) { d.hidden = true; setDrawer('drawer-help', false); return; }
  showHelp(page);
}

export function syncHelpPage(page) {
  const d = document.getElementById('help-drawer');
  if (d && !d.hidden) showHelp(page);       // keep an open guide in step with navigation
  else if (page !== 'fix') showHelp(page, { auto: true });  // first visit to a page: open it once, then never again unless asked
                                            // (not on Fix: it is opened from WhatsApp, usually on a phone, and the guide would cover it)
}
