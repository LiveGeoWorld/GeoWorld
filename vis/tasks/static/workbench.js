/* Workbench URL adapter. Server contracts retain their original paths. */
(() => {
  const originalFetch = window.fetch.bind(window);
  const selectedWorld = new URLSearchParams(location.search).get('world');
  window.fetch = (input, options) => {
    if (typeof input === 'string' && input.startsWith('/api/')) {
      const url = new URL('/tasks' + input, location.origin);
      if (selectedWorld && !url.searchParams.has('world')) url.searchParams.set('world', selectedWorld);
      input = url.pathname + url.search;
    }
    return originalFetch(input, options);
  };
  const updateControls = () => {
    const start = document.getElementById('gen-start');
    const type = document.getElementById('task-type-filter')?.value;
    if (start) {
      const pending = !['place_grounding', 'place_discovery', 'route_planning', 'multi_entity', 'safety_planning', 'region_aware', 'itinerary'].includes(type);
      if (pending) { start.disabled=true; start.dataset.migrationBlocked='1'; }
      else if (start.dataset.migrationBlocked) { start.disabled=false; delete start.dataset.migrationBlocked; }
      start.title = pending ? (window.GeoUI?.language === 'zh-CN' ? '待迁移接入' : 'Pending migration and integration.') : '';
    }

  };
  document.addEventListener('DOMContentLoaded', () => {
    updateControls();
    new MutationObserver(updateControls).observe(document.getElementById('app'), {childList:true, subtree:true});
  });
})();
