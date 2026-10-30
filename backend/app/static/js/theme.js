// Applied before first paint so there is no light/dark flash.
try {
  const t = localStorage.getItem('integra-theme') || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  document.documentElement.dataset.theme = t;
} catch (e) { /* storage unavailable */ }
