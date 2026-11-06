/* Tab-local UI drafts, separate from authoritative task/confirmation data. */
const reviewState = (() => {
  const requests = new Map(), values = new Map();
  let world = '', restoring = false;
  const read = key => { try { return JSON.parse(sessionStorage.getItem(key) || 'null'); } catch (_) { return null; } };
  const write = (key, value) => { try { sessionStorage.setItem(key, JSON.stringify(value)); } catch (_) {} };
  const key = name => `geoworld.review.v1:${world}:${name}`;
  function json(url) {
    const scoped = key(url);
    if (!requests.has(scoped)) requests.set(scoped, fetch(url).then(async response => {
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      values.set(scoped, data);
      return data;
    }).finally(() => requests.delete(scoped)));
    return requests.get(scoped);
  }
  function save() {
    if (!world || restoring) return;
    if (typeof dynamicSaveDraft === 'function') dynamicSaveDraft();
    write(key('navigation'), {taskType: currentTaskType, city: currentCity, batch: currentBatch,
      folder: currentFolder, view: currentView,
      generationMode: document.querySelector('input[name="gen-mode"]:checked')?.value || 'new',
      search: document.getElementById('filter-input')?.value || '',
      status: document.getElementById('status-filter')?.value || '',
      detailScroll: document.getElementById('task-detail')?.scrollTop || 0,
      listScroll: document.querySelector('.sidebar-scroll')?.scrollTop || 0});
  }
  async function restore(worldId) {
    world = worldId;
    const saved = read(key('navigation'));
    if (!saved) return;
    restoring = true;
    try {
      currentTaskType = saved.taskType || ''; currentCity = saved.city || '';
      document.getElementById('task-type-filter').value = currentTaskType;
      document.getElementById('city-filter').value = currentCity;
      const mode = document.querySelector(`input[name="gen-mode"][value="${saved.generationMode === 'append' ? 'append' : 'new'}"]`);
      if (mode) mode.checked = true;
      await loadBatches(currentTaskType, currentCity);
      await switchBatch(saved.batch || 'latest');
      if (saved.folder && (manifestData.tasks || []).some(t => (t.folder_ref || t.folder) === saved.folder)) await selectTask(saved.folder);
      document.getElementById('filter-input').value = saved.search || '';
      document.getElementById('status-filter').value = saved.status || '';
      filterTasks();
      if (saved.view === 'runs') await showRunView();
      else if (saved.view === 'app-content' && typeof showAppContent === 'function') await showAppContent();
      document.getElementById('task-detail').scrollTop = saved.detailScroll || 0;
      const list = document.querySelector('.sidebar-scroll');
      if (list) list.scrollTop = saved.listScroll || 0;
    } finally { restoring = false; }
  }
  window.addEventListener('pagehide', save);
  document.addEventListener('change', () => setTimeout(save, 0));
  return {json, save, restore, peek: url => values.get(key(url)),
    read: name => read(key(name)), write: (name, value) => write(key(name), value)};
})();
