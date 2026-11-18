'use strict';

((root, factory) => {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root?.document) api.loadCodeVersion({
    document: root.document,
    fetch: typeof root.fetch === 'function' ? root.fetch.bind(root) : null,
  });
})(typeof globalThis === 'object' ? globalThis : this, () => {
  const validVersion = value => typeof value === 'string'
    && /^(?:[0-9a-f]{7,40}(?:-dirty)?|files-[0-9a-f]{12})$/.test(value);

  async function loadCodeVersion({ document, fetch }) {
    const target = document?.getElementById('code-version');
    if (!target) return 'unavailable';
    try {
      if (typeof fetch !== 'function') throw new Error('fetch unavailable');
      const response = await fetch('/api/health', {
        headers: { Accept: 'application/json' }, cache: 'no-store', credentials: 'same-origin'
      });
      if (!response.ok) throw new Error(`health ${response.status}`);
      const payload = await response.json();
      if (!validVersion(payload.code_version)) throw new Error('invalid code version');
      target.textContent = `代码 ${payload.code_version}`;
      target.dataset.state = 'ready';
      return payload.code_version;
    } catch {
      // Version info is internal diagnostics, not user state: stay silent instead
      // of surfacing an error phrase in the footer.
      target.textContent = '';
      target.dataset.state = 'unavailable';
      return 'unavailable';
    }
  }

  return { loadCodeVersion };
});
