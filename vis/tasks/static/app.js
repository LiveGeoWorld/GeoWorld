// Task Browser — D3.js flowchart + task details

let manifestData = null;
let svg = null;
let mainGroup = null;
let currentFolder = null;
let judgeState = null;
let sourceState = null;
let selectedJudgeNode = null;
let currentView = 'tasks';
let currentBatch = 'latest';
let currentTaskType = '';
let currentCity = '';
let regionBrowseMode = 'tasks';
let regionRuleWorkspace = 'review';
let regionRuleCities = [];
let regionRulesData = null;
let regionRuleStatusFilter = '';
let regionBuildConfig = null;
let regionBuildSelectedCities = new Set();
let regionBuildTextModel = '';
let regionBuildVisionModel = '';
let regionBuildPollTimer = null;
let regionBuildStatusRefreshInFlight = false;
let regionBuildStatusResults = new Map();
let cityOptions = [];
let runScope = { taskType: '', dynamicEventMode: 'shared_world', city: '', batch: 'latest', allManifest: null, manifest: null };
let generationJob = null;
let generationPollTimer = null;
let generationPanelCollapsed = false;
let generationJobTaskFilter = null;
let taskDirty = false;
const DEFAULT_GENERATION_MODEL_KEY = 'qwen_default';
const DEFAULT_TEST_MODEL_KEY = 'qwen_vl';
let modelOptions = { generation: [], testing: [] };
let generationFeasibility = { mixedTaskTypes: [] };
let routeResourceState = null;
let routeResourceKey = '';
let routeResourceRequestId = 0;
let routeResourcePendingKey = '';
let routeCostPreview = null;
let routeCostPreviousDetail = '';
let serviceDetailPreflight = null;
let serviceDetailJob = null;
let serviceDetailPollTimer = null;
let serviceDetailPreviousDetail = '';

function openWorldStudio() {
  window.location.href = '/world/';
}

async function onTaskTypeChange() {
  dynamicSaveDraft();
  currentTaskType = document.getElementById('task-type-filter').value;
  regionBrowseMode = 'tasks';
  currentBatch = 'latest';
  await loadBatches(currentTaskType, currentCity);
  await switchBatch(reviewState.read('batch:' + JSON.stringify([currentTaskType, currentCity])) || 'latest');
  showGenPanel();
  updateFeasibilityControls();
  await refreshRouteResources();
}

async function onCityChange() {
  dynamicSaveDraft();
  currentCity = document.getElementById('city-filter').value || '';
  regionBrowseMode = 'tasks';
  currentBatch = 'latest';
  await loadBatches(currentTaskType, currentCity);
  await switchBatch(reviewState.read('batch:' + JSON.stringify([currentTaskType, currentCity])) || 'latest');
  showGenPanel();
  updateFeasibilityControls();
  await refreshRouteResources();
}

function supportsMixedFeasibility(taskType) {
  return (generationFeasibility.mixedTaskTypes || []).includes(taskType);
}

function updateFeasibilityControls() {
  const taskType = currentTaskType || document.getElementById('task-type-filter')?.value || '';
  const section = document.getElementById('gen-feasibility');
  const percentWrap = document.getElementById('gen-infeasible-percent-wrap');
  const modeSelect = document.getElementById('gen-feasibility-mode');
  const mixedOption = modeSelect?.querySelector('option[value="mixed"]');
  const status = document.getElementById('gen-feasibility-status');
  if (!section || !percentWrap || !modeSelect || !mixedOption || !status) return;
  const supported = supportsMixedFeasibility(taskType);
  section.hidden = !taskType;
  mixedOption.disabled = !supported;
  const routeDefault = taskType === 'route_planning';
  modeSelect.hidden = routeDefault;
  if (routeDefault) {
    modeSelect.value = 'feasible_only';
    percentWrap.hidden = true;
    status.hidden = false;
    status.textContent = '路线任务使用内置可行性采样。';
    return;
  }
  if (!supported) {
    modeSelect.value = 'feasible_only';
    percentWrap.hidden = true;
    status.hidden = false;
    status.textContent = '不可完成任务构建尚未启用；当前仅支持可完成构建。';
    return;
  }
  status.hidden = true;
  const mode = modeSelect.value || 'feasible_only';
  const mixed = mode === 'mixed';
  percentWrap.hidden = !mixed;
}

function hasTaskTypeSelection(taskType = currentTaskType) {
  return Boolean(taskType);
}

function canInspectBatchHistory(taskType = currentTaskType, city = currentCity) {
  return Boolean(taskType && city);
}

async function loadBatches(taskType, city = '') {
  const sel = document.getElementById('batch-filter');
  if (!sel) return;
  if (!hasTaskTypeSelection(taskType)) {
    sel.style.display = 'none';
    sel.innerHTML = '';
    currentBatch = 'latest';
    return;
  }
  sel.style.display = '';
  if (!city) {
    const regionOption = taskType === 'region_aware'
      ? '<option value="__region_rule_cities__" data-ui-text>城市地域准则</option>' : '';
    sel.innerHTML = `<option value="latest" data-ui-text>当前 ${esc(taskType)} 汇总 · 全部城市</option>${regionOption}`;
    currentBatch = 'latest';
    return;
  }
  try {
    const r = await fetch(`/api/batches?history=1&task_type=${encodeURIComponent(taskType)}&city=${encodeURIComponent(city)}`);
    const d = await r.json();
    let h = '';
    for (const b of (d.batches || [])) {
      const bold = b.is_latest ? 'font-weight:bold' : '';
      const createdAt = formatBatchCreatedAt(b.created_at);
      const label = [
        b.is_latest ? '当前批次' : '',
        b.display_name || '正式任务构建',
        createdAt,
        `${b.task_count}题`,
        b.history_only_count ? `${b.history_only_count}题仅历史查看` : '',
      ].filter(Boolean).join(' · ');
      const count = Number(b.task_count) || 0;
      const historyCount = Number(b.history_only_count) || 0;
      const englishLabel = [
        b.is_latest ? 'Current batch' : '',
        !b.display_name || b.display_name === '正式任务构建' ? 'Task batch' : b.display_name,
        createdAt,
        count === 1 ? '1 task' : `${count} tasks`,
        historyCount ? `${historyCount} historical only` : '',
      ].filter(Boolean).join(' · ');
      h += `<option data-ui-text data-ui-en-text="${attr(englishLabel)}" value="${attr(b.name)}" title="${attr(b.name)}" style="${bold}" ${b.is_latest ? 'selected' : ''}>${esc(label)}</option>`;
    }
    sel.innerHTML = h || '<option value="" data-ui-text>无批次</option>';
    currentBatch = d.current || 'latest';
  } catch(e) { console.error(e); }
}

function formatBatchCreatedAt(raw) {
  if (!raw) return '';
  const value = new Date(raw);
  if (Number.isNaN(value.getTime())) return '';
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(value).replace(/\//g, '-');
}

async function switchBatch(batchName) {
  if (currentView !== 'tasks') return;
  const request = ++dynamicSourceRequest;
  dynamicSaveDraft();
  if (batchName === '__region_rule_cities__') {
    regionBrowseMode = 'rules';
    regionRuleWorkspace = 'review';
    currentBatch = batchName;
    await loadRegionRuleCities();
    return;
  }
  regionBrowseMode = 'tasks';
  if (!canInspectBatchHistory()) batchName = 'latest';
  currentBatch = batchName || currentBatch;
  reviewState.write('batch:' + JSON.stringify([currentTaskType, currentCity]), currentBatch);
  currentFolder = null;
  taskDirty = false;
  if (selectedTaskType() === 'dynamic_replanning') dynamicRestoreDraft();
  if (routeCostSelected()) invalidateRouteCostPreview();
  const tt = document.getElementById('task-type-filter').value || '';
  const city = currentCity || document.getElementById('city-filter')?.value || '';
  try {
    const r = await fetch(
      `/api/manifest?history=1&task_type=${encodeURIComponent(tt)}&batch=${encodeURIComponent(currentBatch)}&city=${encodeURIComponent(city)}`,
    );
    const data = await r.json();
    if (currentView !== 'tasks' || request !== dynamicSourceRequest) return;
    manifestData = data;
    renderTaskList();
    renderHeader();
    document.getElementById('task-detail').dataset.serviceDetailView = '';
    document.getElementById('task-detail').innerHTML = '<div class="placeholder" data-ui-text>← 从左侧选择一个任务查看详情</div>';
    showGenPanel();  // update generate panel for new batch
    if (tt === 'dynamic_replanning') await showDynamicSources();
  } catch(e) { console.error(e); }
}

function showGenPanel() {
  const panel = document.getElementById('gen-panel');
  if (!panel) return;
  if (regionBrowseMode === 'rules') { panel.style.display = 'none'; return; }
  const tt = document.getElementById('task-type-filter').value;
  document.getElementById('dynamic-source-field').hidden = true;
  document.getElementById('gen-count').closest('label').hidden = tt === 'dynamic_replanning';
  dynamicSyncGate();
  const city = currentCity || document.getElementById('city-filter')?.value || '';
  if (!tt || (!city && !generationJob)) { panel.style.display = 'none'; return; }
  renderGenerationModelOptions();
  updateGenerationModeAvailability();
  updateFeasibilityControls();
  panel.style.display = '';
  renderGenerationPanelState();
  renderGenerationProgress();
  refreshRouteResources();
}

function selectedTaskType() {
  return document.getElementById('task-type-filter')?.value || currentTaskType || '';
}

function routePlanningSelected() {
  return selectedTaskType() === 'route_planning';
}

function multiEntitySelected() {
  return selectedTaskType() === 'multi_entity';
}

function itinerarySelected() {
  return selectedTaskType() === 'itinerary';
}

function regionSelected() {
  return selectedTaskType() === 'region_aware';
}

function safetySelected() {
  return selectedTaskType() === 'safety_planning';
}

function routeCostSelected() {
  return routePlanningSelected() || multiEntitySelected() || itinerarySelected()
    || regionSelected() || safetySelected();
}

async function refreshRouteResources(force = false) {
  const section = document.getElementById('route-resource-fields');
  if (!section) return;
  const city = currentCity || document.getElementById('city-filter')?.value || '';
  section.hidden = !(routeCostSelected() && city);
  if (section.hidden) {
    routeResourceRequestId += 1;
    routeResourcePendingKey = '';
    document.getElementById('task-detail').dataset.serviceDetailView = '';
    return;
  }
  const multiEntity = multiEntitySelected();
  const itinerary = itinerarySelected();
  const region = regionSelected();
  const safety = safetySelected();
  const fixedPoolTask = multiEntity || itinerary || region || safety;
  document.getElementById('route-diffusion-field').hidden = fixedPoolTask;
  document.getElementById('route-pool-max-field').hidden = fixedPoolTask;
  document.getElementById('multi-character-current').hidden = false;
  const world = manifestData?.world_id || '';
  const key = `${currentTaskType || document.getElementById('task-type-filter')?.value || ''}:${world}:${city}`;
  if (!force && routeResourceKey === key && routeResourceState) {
    validateRouteResourceFields();
    return routeResourceState;
  }
  if (!force && routeResourcePendingKey === key) return null;
  const requestId = ++routeResourceRequestId;
  routeResourcePendingKey = key;
  routeResourceKey = key;
  routeResourceState = null;
  routeCostPreview = null;
  serviceDetailPreflight = null;
  if (serviceDetailJob && (serviceDetailJob.city !== city || serviceDetailJob.world_id !== world)) {
    serviceDetailJob = null;
    if (serviceDetailPollTimer) clearTimeout(serviceDetailPollTimer);
    serviceDetailPollTimer = null;
  }
  const serviceButton = document.getElementById('service-detail-build');
  if (serviceButton) serviceButton.hidden = true;
  document.getElementById('route-pool-current').textContent = '正在读取…';
  document.querySelector('#multi-character-current b').textContent = '正在读取…';
  try {
    const resourcePath = itinerary
      ? '/api/itinerary/route-resources'
      : multiEntity ? '/api/multi-entity/route-resources'
      : region ? '/api/region/route-resources'
      : safety ? '/api/safety/route-resources'
      : '/api/route-planning/resources';
    const response = await fetch(`${resourcePath}?city=${encodeURIComponent(city)}`);
    const data = await response.json();
    if (requestId !== routeResourceRequestId || routeResourceKey !== key) return null;
    if (data.status === 'not_connected') throw new Error('路线资源待迁移接入。');
    if (!response.ok || !data.ok) throw new Error(data.error || '路线资源读取失败');
    routeResourceState = data;
    document.querySelector('#multi-character-current b').textContent = `${data.character_count || 0} 名`;
    if (routePlanningSelected()) {
      document.getElementById('route-diffusion-start').value = data.diffusion_start;
      document.getElementById('route-pool-max').value = data.pool_max;
    }
    renderRoutePoolCurrent();
    validateRouteResourceFields();
    routeResourcePendingKey = '';
    return data;
  } catch (error) {
    if (requestId !== routeResourceRequestId || routeResourceKey !== key) return null;
    routeResourcePendingKey = '';
    document.getElementById('route-pool-current').textContent = '无法读取';
    document.querySelector('#multi-character-current b').textContent = '无法读取';
    if (serviceButton) serviceButton.hidden = true;
    const message = document.getElementById('route-resource-error');
    message.hidden = false;
    message.textContent = error.message;
    setRouteGenerationButtonsDisabled(true);
    return null;
  }
}

function routeResourceValues() {
  return {
    diffusionStart: Number(document.getElementById('route-diffusion-start')?.value),
    poolMax: Number(document.getElementById('route-pool-max')?.value),
  };
}

function renderRoutePoolCurrent() {
  if (!routeResourceState) return;
  if (!routePlanningSelected()) {
    document.getElementById('route-pool-current').textContent = `${Number(routeResourceState.pool_size) || 0} 个 POI`;
    return;
  }
  const { poolMax } = routeResourceValues();
  const current = Number(routeResourceState.pool_size) || 0;
  const compatible = Number.isInteger(poolMax) && current > poolMax;
  document.getElementById('route-pool-current').textContent =
    `${current} / ${Number.isInteger(poolMax) ? poolMax : '--'}${compatible ? '（兼容模式）' : ''}`;
}

function setRouteGenerationButtonsDisabled(parametersInvalid) {
  const preview = document.getElementById('route-cost-preview');
  const start = document.getElementById('gen-start');
  const startWrap = document.getElementById('gen-start-wrap');
  const active = generationIsActive();
  if (preview) preview.disabled = parametersInvalid || active;
  if (start) start.disabled = active || parametersInvalid || !routeCostPreview;
  if (startWrap) {
    const parameterMessage = document.getElementById('route-resource-error')?.textContent || '';
    const tooltip = active
      ? '当前任务正在生成'
      : parametersInvalid
        ? (parameterMessage || '路线资源尚未准备完成')
        : !routeCostPreview
          ? '请先查看本次 Google API 成本预估'
          : '';
    startWrap.dataset.tooltip = tooltip;
    startWrap.classList.toggle('has-tooltip', Boolean(tooltip));
  }
}

function validateRouteResourceFields() {
  if (!routeCostSelected()) return true;
  const message = document.getElementById('route-resource-error');
  if (multiEntitySelected() || itinerarySelected() || regionSelected() || safetySelected()) {
    let error = '';
    if (!routeResourceState) error = '路线资源尚未准备完成。';
    else if ((Number(routeResourceState.pool_size) || 0) < 1) error = '当前城市没有可用的 Routing POI池。';
    else if (multiEntitySelected() && (Number(routeResourceState.character_count) || 0) < 2) error = '当前城市至少需要两名可用人物。';
    else if ((itinerarySelected() || regionSelected() || safetySelected()) && (Number(routeResourceState.character_count) || 0) < 1) error = '当前城市至少需要一名可用人物。';
    else if (regionSelected() && !routeResourceState.service_detail_ready) error = routeResourceState.error || '当前城市缺少服务详情缓存。';
    else if (regionSelected() && !routeResourceState.region_rules_ready) error = routeResourceState.error || '当前城市缺少 Region 正式规则资产。';
    const serviceButton = document.getElementById('service-detail-build');
    if (serviceButton) {
      const canBuild = regionSelected() && routeResourceState
        && !routeResourceState.service_detail_ready
        && (routeResourceState.resource_issues || []).some(item => item?.action === 'build_service_detail');
      serviceButton.hidden = !canBuild;
      serviceButton.disabled = !canBuild || generationIsActive();
      const matchingJob = serviceDetailJob?.city === currentCity
        && serviceDetailJob?.world_id === (manifestData?.world_id || '');
      serviceButton.textContent = matchingJob && ['queued', 'running'].includes(serviceDetailJob?.status || '')
        ? '查看服务详情构建进度'
        : '构建服务详情';
    }
    message.hidden = !error;
    message.textContent = error;
    renderRoutePoolCurrent();
    setRouteGenerationButtonsDisabled(Boolean(error));
    return !error;
  }
  const { diffusionStart, poolMax } = routeResourceValues();
  let error = '';
  if (!Number.isInteger(diffusionStart) || !Number.isInteger(poolMax) || diffusionStart < 1 || poolMax < 2) {
    error = '扩散阶段起点和路线池上限必须是正整数。';
  } else if (diffusionStart >= poolMax) {
    error = '扩散阶段起点必须小于路线池上限，请修改。';
  }
  message.hidden = !error;
  message.textContent = error;
  renderRoutePoolCurrent();
  setRouteGenerationButtonsDisabled(Boolean(error));
  return !error;
}

function invalidateRouteCostPreview() {
  routeCostPreview = null;
  validateRouteResourceFields();
  const detail = document.getElementById('task-detail');
  if (detail?.dataset.routeCostView === 'true') {
    detail.innerHTML = '<div class="route-cost-detail"><div class="route-cost-stale" data-ui-text>生成参数已发生变化，请重新预估 Google API 成本。</div></div>';
  }
}

async function previewRouteGoogleCost() {
  if (!validateRouteResourceFields()) return;
  const detail = document.getElementById('task-detail');
  if (detail.dataset.routeCostView !== 'true') routeCostPreviousDetail = detail.innerHTML;
  detail.dataset.routeCostView = 'true';
  const multiEntity = multiEntitySelected();
  const itinerary = itinerarySelected();
  const region = regionSelected();
  const safety = safetySelected();
  const previewLabel = itinerary ? 'Itinerary' : multiEntity ? 'Multi-Entity'
    : region ? 'Region' : safety ? 'Safety' : 'Routing';
  detail.innerHTML = `<div class="route-cost-detail route-cost-loading" role="status" aria-live="polite">
    <div class="route-cost-loading-copy">
      <div class="route-cost-loading-title" data-ui-text>正在预演 ${previewLabel} 路线请求</div>
      <div class="route-cost-loading-note" data-ui-text>${itinerary ? '固定 seed 下的任务结构，并对 Agent 决策进行有限只读模拟' : multiEntity ? '根据人物、POI采样和现有路线缓存计算成本区间' : region ? '根据地域规则候选、人物和现有路线缓存预演 Stage 3 路线补缺' : safety ? '根据风险目标、替代地点、干扰地点和现有路线缓存预演路线补缺' : '根据当前路线池、任务数量和路线参数计算成本区间'}</div>
      <div class="route-cost-loading-track" aria-hidden="true"><span></span></div>
    </div>
  </div>`;
  const { diffusionStart, poolMax } = routeResourceValues();
  const payload = {
    city: currentCity,
    count: Number(document.getElementById('gen-count')?.value) || 1,
    diffusion_start: diffusionStart,
    pool_max: poolMax,
    mode: document.querySelector('input[name="gen-mode"]:checked')?.value || 'append',
    batch: document.getElementById('batch-filter')?.value || currentBatch || '',
  };
  try {
    const preflightPath = itinerary
      ? '/api/itinerary/cost-preflight'
      : multiEntity ? '/api/multi-entity/cost-preflight'
      : region ? '/api/region/cost-preflight'
      : safety ? '/api/safety/cost-preflight'
      : '/api/route-planning/cost-preflight';
    const response = await fetch(preflightPath, {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error || '成本预估失败');
    routeCostPreview = data;
    if (itinerary) renderItineraryCostDetail(data);
    else if (multiEntity) renderMultiEntityCostDetail(data);
    else if (region || safety) renderCompositionalCostDetail(data, region ? 'Region' : 'Safety');
    else renderRouteCostDetail(data);
    validateRouteResourceFields();
  } catch (error) {
    routeCostPreview = null;
    detail.innerHTML = `<div class="route-cost-detail"><div class="route-cost-stale" data-ui-output>${esc(error.message)}</div></div>`;
    validateRouteResourceFields();
  }
}

function closeServiceDetailBuild() {
  const detail = document.getElementById('task-detail');
  detail.dataset.serviceDetailView = '';
  detail.innerHTML = serviceDetailPreviousDetail || '<div class="placeholder" data-ui-text>← 从左侧选择一个任务查看详情</div>';
}

async function previewRegionServiceDetailBuild() {
  if (!regionSelected() || !currentCity) return;
  const detail = document.getElementById('task-detail');
  if (detail.dataset.serviceDetailView !== 'true') serviceDetailPreviousDetail = detail.innerHTML;
  detail.dataset.serviceDetailView = 'true';
  const matchingJob = serviceDetailJob?.city === currentCity
    && serviceDetailJob?.world_id === (manifestData?.world_id || '');
  if (matchingJob && ['queued', 'running'].includes(serviceDetailJob.status || '')) {
    renderServiceDetailJob(serviceDetailJob);
    if (['queued', 'running'].includes(serviceDetailJob.status)) pollRegionServiceDetailBuild();
    return;
  }
  detail.innerHTML = `<div class="route-cost-detail route-cost-loading" role="status" aria-live="polite">
    <div class="route-cost-loading-copy"><div class="route-cost-loading-title" data-ui-text>正在检查服务详情构建范围</div>
    <div class="route-cost-loading-note" data-ui-text>读取当前城市版本和已有服务详情记录，不发起外部请求</div>
    <div class="route-cost-loading-track" aria-hidden="true"><span></span></div></div></div>`;
  try {
    const response = await fetch(`/api/region/service-detail/preflight?city=${encodeURIComponent(currentCity)}`);
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error || '服务详情构建信息读取失败');
    serviceDetailPreflight = data;
    renderServiceDetailPreflight(data);
  } catch (error) {
    detail.innerHTML = `<div class="route-cost-detail"><div class="route-cost-stale" data-ui-output>${esc(error.message)}</div></div>`;
  }
}

function renderServiceDetailPreflight(data) {
  const detail = document.getElementById('task-detail');
  const requests = `${Number(data.estimated_request_minimum || 0).toLocaleString()}–${Number(data.estimated_request_maximum || 0).toLocaleString()} 次`;
  const checkpointActions = data.checkpoint_available
    ? `<button class="save-btn" type="button" onclick="startRegionServiceDetailBuild('resume')" data-ui-text>续采集</button>
      <button class="service-detail-restart" type="button" onclick="startRegionServiceDetailBuild('restart')" data-ui-text>重新采集</button>`
    : '<button class="save-btn" type="button" onclick="startRegionServiceDetailBuild(\'resume\')" data-ui-text>确认构建</button>';
  detail.innerHTML = `<div class="route-cost-detail">
    <div class="route-cost-header"><div class="route-cost-heading"><h2 data-ui-text>构建服务详情</h2><div>${esc(data.city_label)}</div></div></div>
    <div class="route-cost-section"><h3 data-ui-text>构建范围</h3><div class="route-cost-grid">
      <span data-ui-text>地图地点</span><b>${Number(data.place_count || 0).toLocaleString()} 个</b>
      <span data-ui-text>含官网地点</span><b>${Number(data.website_place_count || 0).toLocaleString()} 个</b>
      <span data-ui-text>无官网地点</span><b>${Number(data.without_website_count || 0).toLocaleString()} 个</b>
      <span data-ui-text>可直接复用</span><b>${Number(data.reusable_website_records || 0).toLocaleString()} 个</b>
      ${data.checkpoint_available ? `<span data-ui-text>上次已完成地点</span><b>${Number(data.checkpoint_processed_places || 0).toLocaleString()} 个</b>
      <span data-ui-text>上次已发起网站请求</span><b>${Number(data.checkpoint_website_requests || 0).toLocaleString()} 次</b>` : ''}
      <span data-ui-text>预计外部网站请求</span><b>${requests}</b>
    </div><div class="route-cost-note">${data.checkpoint_available ? '已找到与当前城市版本匹配的采集断点。续采集只请求尚未完成的地点；重新采集会放弃该断点并从头开始。' : '构建会访问地点官网及少量同站服务页面。'}不调用 Google API，也不会产生 Google Maps 费用。无官网或无法访问的地点仍会保留明确的不可用记录。</div></div>
    <div class="service-detail-actions"><button class="service-detail-cancel" type="button" onclick="closeServiceDetailBuild()" data-ui-text>取消</button>
      ${checkpointActions}</div>
  </div>`;
}

async function startRegionServiceDetailBuild(collectionMode = 'resume') {
  if (!serviceDetailPreflight?.preflight_id) return;
  const detail = document.getElementById('task-detail');
  detail.innerHTML = `<div class="route-cost-detail route-cost-loading" role="status" aria-live="polite">
    <div class="route-cost-loading-copy"><div class="route-cost-loading-title" data-ui-text>正在启动服务详情构建</div>
    <div class="route-cost-loading-note" data-ui-text>构建将在后台继续运行</div><div class="route-cost-loading-track" aria-hidden="true"><span></span></div></div></div>`;
  try {
    const response = await fetch('/api/region/service-detail/start', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        preflight_id: serviceDetailPreflight.preflight_id,
        collection_mode: collectionMode,
      }),
    });
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error || '服务详情构建启动失败');
    serviceDetailJob = data.job;
    renderServiceDetailJob(serviceDetailJob);
    validateRouteResourceFields();
    pollRegionServiceDetailBuild();
  } catch (error) {
    detail.innerHTML = `<div class="route-cost-detail"><div class="route-cost-stale" data-ui-output>${esc(error.message)}</div></div>`;
  }
}

function renderServiceDetailJob(job) {
  const detail = document.getElementById('task-detail');
  const active = ['queued', 'running'].includes(job?.status || '');
  const failed = job?.status === 'failed';
  const heading = active ? '正在构建服务详情' : failed ? '服务详情构建失败' : '服务详情构建完成';
  const total = Number(job?.place_count || 0);
  const processed = Number(job?.processed_places || (job?.status === 'completed' ? total : 0));
  const percent = Math.max(0, Math.min(100, total ? Math.round(processed * 100 / total) : Number(job?.progress_percent || 0)));
  const websiteTotal = Number(job?.website_place_count || 0);
  const websiteProcessed = Number(job?.processed_website_places || 0);
  const withoutWebsiteTotal = Number(job?.without_website_count || Math.max(0, total - websiteTotal));
  const withoutWebsiteProcessed = Number(job?.processed_without_website_places || 0);
  const statusCounts = job?.status_counts || {};
  const unusable = Object.entries(statusCounts).reduce(
    (sum, [status, count]) => ['source_backed', 'source_unavailable'].includes(status) ? sum : sum + Number(count || 0), 0,
  );
  const started = Date.parse(job?.started_at || '');
  const elapsedSeconds = Number.isFinite(started) ? Math.max(0, Math.floor((Date.now() - started) / 1000)) : 0;
  const elapsed = elapsedSeconds >= 3600
    ? `${Math.floor(elapsedSeconds / 3600)}小时${Math.floor((elapsedSeconds % 3600) / 60)}分`
    : `${Math.floor(elapsedSeconds / 60)}分${elapsedSeconds % 60}秒`;
  detail.innerHTML = `<div class="route-cost-detail service-detail-progress" role="status" aria-live="polite">
    <div class="route-cost-header"><div class="route-cost-heading"><h2>${heading}</h2><div>${esc(job?.city_label || '')}</div></div></div>
    <div class="route-cost-section">
      <div class="service-detail-progress-copy">${esc(job?.message || '')}</div>
      <div class="service-detail-progress-head"><span data-ui-text>总体进度</span><b>${percent}%</b></div>
      <div class="service-detail-progress-track" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${percent}"><span style="width:${percent}%"></span></div>
      <div class="route-cost-grid service-detail-live-grid">
        <span data-ui-text>已处理地点</span><b>${processed.toLocaleString()} / ${total.toLocaleString()}</b>
        <span data-ui-text>含官网地点</span><b>${websiteProcessed.toLocaleString()} / ${websiteTotal.toLocaleString()}</b>
        <span data-ui-text>无官网记录</span><b>${withoutWebsiteProcessed.toLocaleString()} / ${withoutWebsiteTotal.toLocaleString()}</b>
        <span data-ui-text>实际网站请求</span><b>${Number(job?.website_requests || 0).toLocaleString()} 次</b>
        <span data-ui-text>未获取有效网页</span><b>${unusable.toLocaleString()} 个</b>
        <span data-ui-text>已用时</span><b>${elapsed}</b>
      </div>
      ${job?.status === 'completed' ? `<div class="route-cost-note">已生成 ${Number(job.records || processed).toLocaleString()} 条地点记录。</div>` : ''}
    </div>
    ${failed ? '<div class="service-detail-actions"><button class="save-btn" type="button" onclick="previewRegionServiceDetailBuild()" data-ui-text>重新查看构建范围</button></div>' : ''}
  </div>`;
}

async function pollRegionServiceDetailBuild() {
  if (!serviceDetailJob?.job_id || !['queued', 'running'].includes(serviceDetailJob.status)) return;
  if (serviceDetailPollTimer) clearTimeout(serviceDetailPollTimer);
  serviceDetailPollTimer = null;
  try {
    const response = await fetch(`/api/region/service-detail/status?job_id=${encodeURIComponent(serviceDetailJob.job_id)}`);
    const data = await response.json();
    if (!response.ok || !data.ok || !data.job) throw new Error(data.error || '服务详情构建状态读取失败');
    serviceDetailJob = data.job;
    if (document.getElementById('task-detail')?.dataset.serviceDetailView === 'true') {
      renderServiceDetailJob(serviceDetailJob);
    }
    validateRouteResourceFields();
    if (serviceDetailJob.status === 'completed') {
      routeResourceState = null;
      routeResourceKey = '';
      await refreshRouteResources(true);
      return;
    }
    if (serviceDetailJob.status === 'failed') return;
  } catch (error) {
    serviceDetailJob = {...serviceDetailJob, status: 'failed', message: error.message};
    if (document.getElementById('task-detail')?.dataset.serviceDetailView === 'true') {
      renderServiceDetailJob(serviceDetailJob);
    }
    validateRouteResourceFields();
    return;
  }
  serviceDetailPollTimer = setTimeout(pollRegionServiceDetailBuild, 1500);
}

function renderCompositionalCostDetail(data, label) {
  const detail = document.getElementById('task-detail');
  const preview = data.cost_preview || {};
  const minimum = preview.minimum || {};
  const expected = preview.expected || {};
  const maximum = preview.maximum || {};
  const actualCost = data.route_cost_actual || {};
  const currentActual = actualCost.current || {};
  const cumulativeActual = actualCost.cumulative || {};
  const money = value => `$${(Number(value) || 0).toFixed(2)}`;
  const en = window.GeoUI?.language === 'en';
  const unit = zh => en ? '' : ' ' + zh;
  const requests = value => `${(Number(value) || 0).toLocaleString()}${unit('次')}`;
  const rule = label === 'Region'
    ? '按人物、地域规则命中目标和本地对照候选，复用 Routing POI覆盖与共享 RouteCache，只补齐缺失路线。'
    : '目标、替代地点和干扰地点优先复用 Routing POI覆盖，复用共享 RouteCache，只补齐人物到候选的缺失路线。';
  detail.innerHTML = `<div class="route-cost-detail">
    <div class="route-cost-header"><button class="route-cost-back" type="button" onclick="closeRouteCostDetail()" title="返回任务详情" data-ui-title>‹</button>
      <div class="route-cost-heading"><h2 data-ui-text>Google API 成本预估</h2><div>${esc(data.city_label)} · ${label} · ${data.count}${en ? (Number(data.count) === 1 ? ' task' : ' tasks') : ' 条任务'}</div></div></div>
    <div class="route-cost-section"><h3 data-ui-text>路线使用规则</h3><div class="route-cost-rule">${esc(en ? (label === 'Region'
      ? 'Reuse cached routes for the traveler and nearby candidates, and fetch missing routes as needed.'
      : 'Prefer places already covered by the route cache, and fetch missing routes from the traveler to candidates as needed.') : rule)}</div></div>
    <div class="route-cost-section"><h3 data-ui-text>当前资源</h3><div class="route-cost-grid">
      <span data-ui-text>Routing POI覆盖</span><b>${Number(data.pool_size || 0).toLocaleString()}${unit('个')}</b>
      <span data-ui-text>可用人物</span><b>${Number(data.character_count || 0).toLocaleString()}${unit('名')}</b>
      <span data-ui-text>共享缓存路线事实</span><b>${Number(data.cached_route_facts || 0).toLocaleString()}${unit('条')}</b>
      ${label === 'Region' ? `<span data-ui-text>服务详情缓存</span><b>${data.service_detail_ready ? (en ? 'Ready' : '已就绪') : (en ? 'Missing' : '缺失')}</b><span data-ui-text>地域规则资产</span><b>${data.region_rules_ready ? (en ? 'Ready' : '已就绪') : (en ? 'Missing' : '缺失')}</b>` : ''}
    </div></div>
    <div class="route-cost-section"><h3 data-ui-text>本次 Google API 真实成本（实时计价）</h3><div class="route-cost-grid route-cost-metrics">
      <span data-ui-text>请求数</span><b>${requests(currentActual.api_requests)}</b><span data-ui-text>坐标复用</span><b>${Number(currentActual.coordinate_reuse || 0).toLocaleString()}${unit('条')}</b><span data-ui-text>实际费用</span><b>${money(currentActual.cost_usd)}</b>
    </div></div>
    <div class="route-cost-section"><h3 data-ui-text>Google API 累计成本</h3><div class="route-cost-grid route-cost-metrics">
      <span data-ui-text>累计请求数</span><b>${requests(cumulativeActual.api_requests)}</b><span data-ui-text>累计坐标复用</span><b>${Number(cumulativeActual.coordinate_reuse || 0).toLocaleString()}${unit('条')}</b><span data-ui-text>累计费用</span><b>${money(cumulativeActual.cost_usd)}</b>
    </div><div class="route-cost-note">${esc(en ? (actualCost.historical_baseline ? 'Includes the historical cache baseline and subsequently tracked usage.' : 'Usage since cost tracking began.') : actualCost.tracking_note || '')}</div></div>
    <div class="route-cost-section"><h3 data-ui-text>本批预计成本</h3><div class="route-cost-grid route-cost-metrics">
      <span data-ui-text>最低成本</span><b>${requests(minimum.requests)} · ${money(minimum.cost_usd)}</b>
      <span>${preview.estimate_kind === 'bounded_agent_proxy_simulation' ? (en ? 'Simulation estimate' : '模拟预计') : (en ? 'Seed-based estimate' : 'Seed 预演预计')}</span><b>${requests(expected.requests)} · ${money(expected.cost_usd)}</b>
      <span>${preview.upper_bound_scope === 'retained_task_structure_only' ? (en ? 'Retained-task upper bound' : '任务结构上限') : (en ? 'Maximum cost' : '最高成本')}</span><b>${requests(maximum.requests)} · ${money(maximum.cost_usd)}</b>
      <span data-ui-text>预演候选地点</span><b>${Number(expected.candidate_places || 0).toLocaleString()}${unit('个')}</b>
      <span data-ui-text>预计路线复用</span><b>${Number(expected.route_reuse_hits || 0).toLocaleString()}${unit('条')}</b>
      <span data-ui-text>预计坐标复用</span><b>${Number(expected.coordinate_reuse_hits || 0).toLocaleString()}${unit('条')}</b>
    </div></div>
    ${preview.estimate_kind === 'bounded_agent_proxy_simulation' ? `<div class="route-cost-note">${en ? 'Average of ' + Number(preview.sample_count || 0) + ' read-only simulations. The upper bound covers retained tasks. Discarded candidates can increase actual costs.' : '预计值为 ' + Number(preview.sample_count || 0) + ' 次只读代理模拟的平均值，不是模型真实决策概率。结构上限不包含淘汰候选的路线请求；相关费用与淘汰原因另行记录，实际费用可能超过此值。'}</div>` : ''}
    <div class="route-cost-section"><h3 data-ui-text>计费参考</h3><div class="route-cost-grid"><span data-ui-text>计费类型</span><b>${esc(data.sku)}</b><span data-ui-text>参考单价</span><b>$${Number(data.unit_price_usd || 0).toFixed(3)} / ${en ? 'request' : '次'}</b></div><div class="route-cost-note" data-ui-text>预演只读现有资产，不调用 Google API 或构建模型；金额按 Compute Routes Essentials 毛费用计算。</div></div>
  </div>`;
}

function renderItineraryCostDetail(data) {
  const detail = document.getElementById('task-detail');
  const preview = data.cost_preview || {};
  const minimum = preview.minimum || {};
  const expected = preview.expected || {};
  const maximum = preview.maximum || {};
  const actualCost = data.route_cost_actual || {};
  const currentActual = actualCost.current || {};
  const cumulativeActual = actualCost.cumulative || {};
  const money = value => `$${(Number(value) || 0).toFixed(2)}`;
  const en = window.GeoUI?.language === 'en';
  const label = (zh, english) => en ? english : zh;
  const requests = value => `${(Number(value) || 0).toLocaleString()}${label(" 次", "")}`;
  const seededDays = (minimum.days || []).map((days, index) => en ? `Task ${index + 1}: ${days} days` : `第${index + 1}题 ${days}天`).join(' · ') || label('无', 'None');
  detail.innerHTML = `<div class="route-cost-detail">
    <div class="route-cost-header"><button class="route-cost-back" type="button" onclick="closeRouteCostDetail()" title="返回任务详情" data-ui-title>‹</button>
      <div class="route-cost-heading"><h2 data-ui-text>Google API 成本预估</h2><div>${esc(data.city_label)} · Itinerary · ${data.count} ${label("条任务", Number(data.count) === 1 ? "task" : "tasks")}</div></div></div>
    <div class="route-cost-section"><h3 data-ui-text>路线使用规则</h3><div class="route-cost-rule" data-ui-text>完全复用 Routing POI池 · 不做局部扩散</div><div class="route-cost-rule-details">
      <div data-ui-text>任务地点只从当前 Routing POI池选择；复用已有路线，只为固定边界可达性检查及最终相邻活动缺失的路线发起请求。</div>
      <div data-ui-text>批次 seed 已固定人物、天数、酒店、固定事项和可用时间窗口；预估不调用构建 Agent。</div>
    </div></div>
    <div class="route-cost-section"><h3 data-ui-text>当前资源</h3><div class="route-cost-grid">
      <span data-ui-text>Routing POI池</span><b>${Number(data.pool_size || 0).toLocaleString()}${label(" 个", "")}</b>
      <span data-ui-text>可用人物</span><b>${Number(data.character_count || 0).toLocaleString()}${label(" 名", "")}</b>
      <span data-ui-text>共享缓存路线事实</span><b>${Number(data.cached_route_facts || 0).toLocaleString()}${label(" 条", "")}</b>
      <span data-ui-text>本批 seed 固定天数</span><b>${esc(seededDays)}</b>
    </div></div>
    <div class="route-cost-section"><h3 data-ui-text>本次 Google API 真实成本（实时计价）</h3><div class="route-cost-grid route-cost-metrics">
      <span data-ui-text>请求数</span><b>${requests(currentActual.api_requests)}</b><span data-ui-text>坐标复用</span><b>${Number(currentActual.coordinate_reuse || 0).toLocaleString()}${label(" 条", "")}</b><span data-ui-text>实际费用</span><b>${money(currentActual.cost_usd)}</b>
    </div></div>
    <div class="route-cost-section"><h3 data-ui-text>Google API 累计成本</h3><div class="route-cost-grid route-cost-metrics">
      <span data-ui-text>累计请求数</span><b>${requests(cumulativeActual.api_requests)}</b><span data-ui-text>累计坐标复用</span><b>${Number(cumulativeActual.coordinate_reuse || 0).toLocaleString()}${label(" 条", "")}</b><span data-ui-text>累计费用</span><b>${money(cumulativeActual.cost_usd)}</b>
    </div><div class="route-cost-note">${en ? "Cumulative route costs for this city version." : "当前城市版本共享 RouteCache 的累计 Routes 成本。" + esc(actualCost.tracking_note || "")}</div></div>
    <div class="route-cost-section"><h3 data-ui-text>本批预计成本</h3><div class="route-cost-grid route-cost-metrics">
      <span data-ui-text>最低必然成本</span><b>${requests(minimum.requests)} · ${money(minimum.cost_usd)}</b>
      <span data-ui-text>模拟预计</span><b>${requests(expected.requests)} · ${money(expected.cost_usd)}</b>
      <span data-ui-text>保守最高成本</span><b>${requests(maximum.requests)} · ${money(maximum.cost_usd)}</b>
      <span data-ui-text>模拟活动块</span><b>${label("平均", "Average")} ${Number(expected.activity_blocks || 0).toFixed(1)}${label(" 个", "")}</b>
      <span data-ui-text>最多独立 POI活动块</span><b>${Number(maximum.activity_blocks || 0).toLocaleString()}${label(" 个", "")}</b>
      <span data-ui-text>预计复用路线命中</span><b>${Number(expected.route_reuse_hits || 0).toLocaleString()}${label(" 条", "")}</b>
    </div><div class="route-cost-note">${en ? `Estimate from ${Number(preview.sample_count || 0).toLocaleString()} read-only simulations. Actual route requests may vary.` : `模拟预计来自 ${Number(preview.sample_count || 0).toLocaleString()} 次固定规模的只读 Agent 决策代理，不代表 Agent 输出的真实概率均值。`}</div></div>
    <div class="route-cost-section"><h3 data-ui-text>计费参考</h3><div class="route-cost-grid"><span data-ui-text>计费类型</span><b>${esc(data.sku)}</b><span data-ui-text>参考单价</span><b>$${Number(data.unit_price_usd || 0).toFixed(3)} / ${label("次", "request")}</b></div><div class="route-cost-note" data-ui-text>预演不调用 Google API 或构建 Agent；费用按 Compute Routes Essentials 毛费用计算，不扣账户免费额度，也不包含任务构建模型费用。</div></div>
  </div>`;
}

function renderMultiEntityCostDetail(data) {
  const detail = document.getElementById('task-detail');
  const preview = data.cost_preview || {};
  const minimum = preview.minimum || {};
  const expected = preview.expected || {};
  const maximum = preview.maximum || {};
  const deterministic = preview.deterministic || {};
  const actualCost = data.route_cost_actual || {};
  const currentActual = actualCost.current || {};
  const cumulativeActual = actualCost.cumulative || {};
  const money = value => `$${(Number(value) || 0).toFixed(2)}`;
  const requests = value => `${(Number(value) || 0).toLocaleString()} 次`;
  const reuseCount = (deterministic.branches || []).filter(value => value === 'reuse').length;
  const diffusionCount = (deterministic.branches || []).filter(value => value === 'diffusion').length;
  detail.innerHTML = `<div class="route-cost-detail">
    <div class="route-cost-header"><button class="route-cost-back" type="button" onclick="closeRouteCostDetail()" title="返回任务详情">‹</button>
      <div class="route-cost-heading"><h2 data-ui-text>Google API 成本预估</h2><div>${esc(data.city_label)} · Multi-Entity · ${data.count} 条任务</div></div></div>
    <div class="route-cost-section"><h3 data-ui-text>路线使用规则</h3><div class="route-cost-rule" data-ui-text>POI复用 90% · 局部扩散 10%</div><div class="route-cost-rule-details">
      <div><b data-ui-text>复用：</b>从 Routing POI池选择 GT及周边 POI，复用人物到这些 POI的已有路线，只查询缺失路线。</div>
      <div><b data-ui-text>局部扩散：</b>在 Routing POI附近选择新 GT及其周边 POI，只构建本题人物到这些 POI的稀疏路线；新 POI不加入 Routing POI池。</div>
    </div></div>
    <div class="route-cost-section"><h3 data-ui-text>当前资源</h3><div class="route-cost-grid">
      <span data-ui-text>Routing POI池</span><b>${Number(data.pool_size || 0).toLocaleString()} 个</b>
      <span data-ui-text>可用人物</span><b>${Number(data.character_count || 0).toLocaleString()} 名</b>
      <span data-ui-text>共享缓存路线事实</span><b>${Number(data.cached_route_facts || 0).toLocaleString()} 条</b>
      <span data-ui-text>其中人物→POI路线事实</span><b>${Number(data.character_route_facts || 0).toLocaleString()} 条</b>
    </div></div>
    <div class="route-cost-section"><h3 data-ui-text>本次 Google API 真实成本（实时计价）</h3><div class="route-cost-grid route-cost-metrics">
      <span data-ui-text>请求数</span><b>${requests(currentActual.api_requests)}</b><span data-ui-text>坐标复用</span><b>${Number(currentActual.coordinate_reuse || 0).toLocaleString()} 条</b><span data-ui-text>实际费用</span><b>${money(currentActual.cost_usd)}</b>
    </div></div>
    <div class="route-cost-section"><h3 data-ui-text>Google API 累计成本</h3><div class="route-cost-grid route-cost-metrics">
      <span data-ui-text>累计请求数</span><b>${requests(cumulativeActual.api_requests)}</b><span data-ui-text>累计坐标复用</span><b>${Number(cumulativeActual.coordinate_reuse || 0).toLocaleString()} 条</b><span data-ui-text>累计费用</span><b>${money(cumulativeActual.cost_usd)}</b>
    </div><div class="route-cost-note">当前城市版本共享 RouteCache 的累计 Routes 成本。${esc(actualCost.tracking_note || '')}</div></div>
    <div class="route-cost-section"><h3 data-ui-text>本批预计成本</h3><div class="route-cost-grid route-cost-metrics">
      <span data-ui-text>最低成本</span><b>${requests(minimum.requests)} · ${money(minimum.cost_usd)}</b>
      <span data-ui-text>概率预计</span><b>${requests(expected.requests)} · ${money(expected.cost_usd)}</b>
      <span data-ui-text>最大成本</span><b>${requests(maximum.requests)} · ${money(maximum.cost_usd)}</b>
      <span data-ui-text>预计复用路线命中</span><b>${Number(expected.route_reuse_hits || 0).toLocaleString()} 条</b>
      <span data-ui-text>预计坐标复用命中</span><b>${Number(expected.coordinate_reuse_hits || 0).toLocaleString()} 条</b>
    </div><div class="route-cost-plan"><b data-ui-text>本次确定性预演</b><span>POI复用 ${reuseCount} 条 · 局部扩散 ${diffusionCount} 条</span><span>${requests(deterministic.requests)} · ${money(deterministic.cost_usd)} · 路线复用 ${Number(deterministic.route_reuse_hits || 0).toLocaleString()} 条 · 坐标复用 ${Number(deterministic.coordinate_reuse_hits || 0).toLocaleString()} 条</span></div></div>
    <div class="route-cost-section"><h3 data-ui-text>计费参考</h3><div class="route-cost-grid"><span data-ui-text>计费类型</span><b>${esc(data.sku)}</b><span data-ui-text>参考单价</span><b>$${Number(data.unit_price_usd || 0).toFixed(3)} / 次</b></div><div class="route-cost-note" data-ui-text>预演不调用 Google API；费用按 Compute Routes Essentials 毛费用计算，不扣账户免费额度，也不包含任务构建模型费用。</div></div>
  </div>`;
}

function renderRouteCostDetail(data) {
  const detail = document.getElementById('task-detail');
  const compatible = data.pool_size > data.pool_max;
  const preview = data.cost_preview || {};
  const minimum = preview.minimum || {};
  const expected = preview.expected || {};
  const maximum = preview.maximum || {};
  const deterministic = preview.deterministic || {};
  const toLimit = preview.to_limit_expected || {};
  const remaining = preview.after_batch_remaining || {};
  const actualCost = data.route_cost_actual || {};
  const currentActual = actualCost.current || {};
  const cumulativeActual = actualCost.cumulative || {};
  const stageLabels = {
    bootstrap:'初始化', reuse:'复用', diffusion:'局部扩散', new_origin:'新起点',
    reuse_full:'满池复用', reuse_capacity_guard:'容量保护复用',
  };
  const deterministicStages = (deterministic.stages || []).map(stage => stageLabels[stage] || stage).join(' → ') || '无新增阶段';
  const graphLabel = data.graph_mode === 'legacy_sparse' ? '兼容路线池模式' : '完整路线池模式';
  const money = value => `$${(Number(value) || 0).toFixed(2)}`;
  const requests = value => `${(Number(value) || 0).toLocaleString()} 次`;
  const reachable = Number(preview.reachable_pool_size ?? data.pool_size);
  detail.innerHTML = `<div class="route-cost-detail">
    <div class="route-cost-header"><button class="route-cost-back" type="button" onclick="closeRouteCostDetail()" title="返回任务详情">‹</button>
      <div class="route-cost-heading"><h2 data-ui-text>Google API 成本预估</h2><div>${esc(data.city_label)} · ${data.count} 条任务</div></div></div>
    <div class="route-cost-section"><h3 data-ui-text>路线池规则</h3><div class="route-cost-rule">
      少于扩散阶段起点 ${data.diffusion_start} 个 POI 时，建立分散的路线区域；达到 ${data.diffusion_start} 个 POI 后，以路线复用和局部扩散为主；达到路线池上限 ${data.pool_max} 个 POI 后，停止新增 POI 路线，复用现有 POI 路线生成任务。
    </div><div class="route-cost-rule-details">
      <div class="route-cost-rule-probability" data-ui-text>达到扩散阶段起点后：复用 20% · 局部扩散 70% · 新起点 10%</div>
      <div><b data-ui-text>新起点：</b>在尚未参与路线的区域重新随机选择 2–4 个核心 POI，并在各核心附近建立新的干扰路线区域，实现路线网络广域丰富。</div>
      <div><b data-ui-text>局部扩散：</b>从已有路线 POI 附近选择一个新核心，并加入其周围 3–4 个干扰 POI，补充新旧 POI 之间的路线，实现密集路线网络。</div>
      <div><b data-ui-text>复用：</b>本题全部使用路线池中已有的 POI，不新增 POI 路线，也不产生新的 Google Routes 请求。</div>
    </div></div>
    <div class="route-cost-section"><h3 data-ui-text>当前资源</h3><div class="route-cost-grid">
      <span data-ui-text>当前路线池</span><b>${data.pool_size} / ${data.pool_max}${compatible ? '（兼容模式）' : ''}</b>
      <span data-ui-text>路线池维护方式</span><b>${graphLabel}</b>
      <span data-ui-text>已缓存路线事实</span><b>${data.cached_route_facts.toLocaleString()} 条</b>
    </div></div>
    <div class="route-cost-section"><h3 data-ui-text>本次 Google API 真实成本（实时计价）</h3><div class="route-cost-grid route-cost-metrics">
      <span data-ui-text>请求数</span><b>${requests(currentActual.api_requests)}</b>
      <span data-ui-text>坐标复用</span><b>${(Number(currentActual.coordinate_reuse) || 0).toLocaleString()} 条</b>
      <span data-ui-text>实际费用</span><b>${money(currentActual.cost_usd)}</b>
    </div></div>
    <div class="route-cost-section"><h3 data-ui-text>Google API 累计成本</h3><div class="route-cost-grid route-cost-metrics">
      <span data-ui-text>累计请求数</span><b>${requests(cumulativeActual.api_requests)}</b>
      <span data-ui-text>累计坐标复用</span><b>${(Number(cumulativeActual.coordinate_reuse) || 0).toLocaleString()} 条</b>
      <span data-ui-text>累计费用</span><b>${money(cumulativeActual.cost_usd)}</b>
    </div><div class="route-cost-note">${esc(actualCost.tracking_note || '自真实成本统计启用后累计')}</div></div>
    <div class="route-cost-section"><h3 data-ui-text>本批预计成本</h3><div class="route-cost-grid route-cost-metrics">
      <span data-ui-text>最低成本</span><b>${requests(minimum.requests)} · ${money(minimum.cost_usd)}</b>
      <span data-ui-text>概率预计</span><b>${requests(expected.requests)} · ${money(expected.cost_usd)}</b>
      <span data-ui-text>最大成本</span><b>${requests(maximum.requests)} · ${money(maximum.cost_usd)}</b>
      <span data-ui-text>预计新增路线 POI</span><b>${Number(expected.new_pois || 0).toFixed(1)} 个</b>
      <span data-ui-text>预计坐标复用命中</span><b>${(Number(expected.coordinate_reuse_hits) || 0).toLocaleString()} 条</b>
      <span data-ui-text>预计复用路线命中</span><b>${(Number(expected.route_reuse_hits) || 0).toLocaleString()} 条</b>
    </div><div class="route-cost-plan"><b data-ui-text>本次确定性预演</b><span>${esc(deterministicStages)}</span><span>${requests(deterministic.requests)} · ${money(deterministic.cost_usd)} · 新增 ${deterministic.new_pois || 0} 个 POI</span></div></div>
    <div class="route-cost-section"><h3 data-ui-text>路线池长期成本</h3><div class="route-cost-grid route-cost-metrics">
      <span data-ui-text>扩展至路线池上限预计成本</span><b>${requests(toLimit.requests)} · ${money(toLimit.cost_usd)}</b>
      <span data-ui-text>本批完成后预计剩余扩展成本</span><b>${requests(remaining.requests)} · ${money(remaining.cost_usd)}</b>
    </div>${reachable < data.pool_max ? `<div class="route-cost-reachability">当前冻结候选预计最高扩展到 ${reachable} / ${data.pool_max} 个 POI。</div>` : ''}</div>
    <div class="route-cost-section"><h3 data-ui-text>计费参考</h3><div class="route-cost-grid">
      <span data-ui-text>计费类型</span><b>${esc(data.sku)}</b>
      <span data-ui-text>参考单价</span><b>$${data.unit_price_usd.toFixed(3)} / 次</b>
    </div><div class="route-cost-note" data-ui-text>预演不调用 Google API；费用按 Compute Routes Essentials 毛费用计算，不扣账户免费额度，也不包含任务构建模型费用。最大成本包含正式生成的重试尝试上限。</div></div>
  </div>`;
}

function updateRouteCostActualFromJob(job) {
  if (job.task_type === 'dynamic_replanning') { dynamicUpdateCostFromJob(job); return; }
  if (!routeCostPreview || !['route_planning', 'multi_entity', 'itinerary', 'region_aware', 'safety_planning'].includes(job?.task_type) || job?.city !== routeCostPreview.city) return;
  routeCostPreview.route_cost_actual = job.route_cost_actual || routeCostPreview.route_cost_actual;
  const detail = document.getElementById('task-detail');
  if (detail?.dataset.routeCostView === 'true') {
    if (job.task_type === 'itinerary') renderItineraryCostDetail(routeCostPreview);
    else if (job.task_type === 'multi_entity') renderMultiEntityCostDetail(routeCostPreview);
    else if (job.task_type === 'region_aware') renderCompositionalCostDetail(routeCostPreview, 'Region');
    else if (job.task_type === 'safety_planning') renderCompositionalCostDetail(routeCostPreview, 'Safety');
    else renderRouteCostDetail(routeCostPreview);
  }
}

function closeRouteCostDetail() {
  const detail = document.getElementById('task-detail');
  delete detail.dataset.routeCostView;
  detail.innerHTML = routeCostPreviousDetail || '<div class="placeholder" data-ui-text>← 从左侧选择一个任务查看详情</div>';
}

function updateGenerationModeAvailability() {
  const append = document.querySelector('input[name="gen-mode"][value="append"]');
  const create = document.querySelector('input[name="gen-mode"][value="new"]');
  const status = document.getElementById('gen-cmd');
  const city = currentCity || document.getElementById('city-filter')?.value || '';
  const taskType = currentTaskType || document.getElementById('task-type-filter')?.value || '';
  const batch = document.getElementById('batch-filter')?.value || '';
  const appendable = Boolean(city && taskType && batch);
  if (append) append.disabled = !appendable;
  if (!appendable && create) create.checked = true;
  if (!appendable && city && taskType && status && !generationJob) {
    status.textContent = '该城市尚无此类任务，将新建第一批任务。';
  }
}

function renderGenerationModelOptions() {
  const select = document.getElementById('gen-model');
  if (!select) return;
  const options = modelOptions.generation || [];
  const selected = select.value || DEFAULT_GENERATION_MODEL_KEY;
  select.innerHTML = options.map(option =>
    `<option value="${attr(option.endpoint_key)}" ${option.endpoint_key === selected ? 'selected' : ''}>${esc(option.label)}</option>`
  ).join('') || '<option value="qwen_default">Qwen-Plus</option>';
}

function renderTestModelOptions() {
  const options = modelOptions.testing || [];
  return options.map(option =>
    `<option value="${attr(option.endpoint_key)}" ${option.endpoint_key === DEFAULT_TEST_MODEL_KEY ? 'selected' : ''}>${esc(option.label)}</option>`
  ).join('') || '<option value="" disabled selected data-ui-text>未配置可用的多模态模型</option>';
}

async function showGenCmd() {
  const tt = document.getElementById('task-type-filter').value;
  const count = document.getElementById('gen-count').value || 5;
  const mode = document.querySelector('input[name="gen-mode"]:checked').value;
  const stageModel = document.getElementById('gen-model')?.value || DEFAULT_GENERATION_MODEL_KEY;
  const supportsMixed = supportsMixedFeasibility(tt);
  const feasibilityMode = document.getElementById('gen-feasibility-mode')?.value || 'feasible_only';
  const infeasiblePercent = feasibilityMode === 'mixed' && supportsMixed
    ? Number(document.getElementById('gen-infeasible-percent')?.value) : 0;
  const panel = document.getElementById('gen-cmd');
  if (tt === 'dynamic_replanning' && !await checkDynamicSources()) return;
  const batch = document.getElementById('batch-filter')?.value || currentBatch || '';
  if (!tt) { panel.textContent = '请先选择任务类型'; return; }
  const city = (document.getElementById('city-filter')?.value) || currentCity || '';
  if (!city) {
    panel.textContent = '请先选择一个具体城市，再生成任务。';
    return;
  }
  if (mode === 'append' && document.querySelector('input[name="gen-mode"][value="append"]')?.disabled) {
    panel.textContent = '当前没有可沿用的批次，请选择“新建批次”。';
    return;
  }
  if (['route_planning', 'multi_entity', 'itinerary', 'region_aware', 'safety_planning'].includes(tt) && (!validateRouteResourceFields() || !routeCostPreview)) {
    panel.textContent = '请先查看当前参数的 Google API 成本详情。';
    return;
  }
  if (!Number.isInteger(infeasiblePercent) || infeasiblePercent < 0 || infeasiblePercent > 100 || (feasibilityMode === 'mixed' && infeasiblePercent < 1)) {
    panel.textContent = '不可完成任务占比必须是 1 到 100 的整数百分比';
    return;
  }
  panel.textContent = '正在启动后台构建...';
  try {
    const resp = await fetch('/api/generate', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        task_type: tt, count: parseInt(count), mode: mode, city: city, batch: batch,
        stage_model: stageModel, infeasible_percent: infeasiblePercent,
        dynamic_pretask_admission: tt === 'dynamic_replanning' ? dynamicAdmission : undefined,
        dynamic_confirmation: tt === 'dynamic_replanning' ? dynamicConfirmed?.token : undefined,
        dynamic_cost_preflight_id: tt === 'dynamic_replanning' ? dynamicCostPreview?.preflight_id : undefined,
        route_preflight_id: tt === 'route_planning' ? routeCostPreview?.preflight_id : '',
        multi_entity_preflight_id: tt === 'multi_entity' ? routeCostPreview?.preflight_id : '',
        itinerary_preflight_id: tt === 'itinerary' ? routeCostPreview?.preflight_id : '',
        region_preflight_id: tt === 'region_aware' ? routeCostPreview?.preflight_id : '',
        safety_preflight_id: tt === 'safety_planning' ? routeCostPreview?.preflight_id : '',
        route_diffusion_start: tt === 'route_planning' ? routeCostPreview?.diffusion_start : null,
        route_pool_max: tt === 'route_planning' ? routeCostPreview?.pool_max : null,
      }),
    });
    const data = await resp.json();
    if (data.ok) {
      panel.textContent = '';
      generationPanelCollapsed = false;
      generationJob = data.job;
      updateRouteCostActualFromJob(generationJob);
      setGenerationFormLocked(true);
      renderGenerationPanelState();
      renderGenerationProgress();
      startGenerationPolling();
    } else {
      const code = data.error_code ? `[${data.error_code}] ` : '';
      panel.textContent = `❌ ${code}${data.error || '生成失败'}`;
      if (data.job) {
        generationJob = data.job;
        renderGenerationProgress();
      }
    }
  } catch(e) {
    panel.textContent = `❌ ${e.message}`;
  }
}
let runData = null;
let selectedRunId = '';
let selectedRunTaskId = '';
let runSidebarCollapsed = false;
let collapsedRunTaskGroups = new Set();
let comparisonState = null;
let viewerConfig = { google_maps_api_key: '', preferred_basemap: 'osm' };
let googleBasemapReady = false;
let googleBasemapPromise = null;
let groundingPreviewMap = null;
const benchWorldPoints = new Map();

const GOOGLE_MUTANT_SCRIPT =
  'https://unpkg.com/leaflet.gridlayer.googlemutant@0.15.0/dist/Leaflet.GoogleMutant.js';

const STAGE_COLORS = {
  search: '#3498db',
  reserve: '#4ecca3',
  book: '#4ecca3',
  notify: '#e67e22',
  confirm: '#9b59b6',
  plan: '#f39c12',
  navigate: '#1abc9c',
  locate: '#3498db',
  evidence: '#9b59b6',
  search: '#3498db',
  filter: '#f39c12',
  rank: '#4ecca3',
  exclude: '#e67e22',
  fallback: '#7f8c8d',
};

async function init() {
  try {
    const configReady = fetch('/api/config').then(async response => {
      if (!response.ok) return;
      viewerConfig = await response.json();
      modelOptions = viewerConfig.model_options || modelOptions;
      generationFeasibility = viewerConfig.generation_feasibility || generationFeasibility;
      const modelSelect = document.getElementById('run-model');
      if (modelSelect) {
        const previous = modelSelect.value;
        modelSelect.innerHTML = renderTestModelOptions();
        if ([...modelSelect.options].some(option => option.value === previous)) modelSelect.value = previous;
      }
    }).catch(() => null);
    const [manifestResp, citiesResp] = await Promise.all([
      fetch('/api/manifest?history=1'),
      fetch('/api/cities').catch(() => null),
      configReady,
    ]);
    manifestData = await manifestResp.json();
    cityOptions = citiesResp && citiesResp.ok ? await citiesResp.json() : [];
    renderHeader();
    renderTaskList();
    await loadBatches('', '');
    if (currentView === 'tasks') await reviewState.restore(manifestData.world_id || '');
    restoreGenerationJob();
  } catch (e) {
    console.error('Failed to load manifest', e);
  }
}

function renderHeader() {
  const world = manifestData.world_id || '未选择世界';
  document.getElementById('city-info').textContent = `当前世界：${world}`;
}

function renderTaskList() {
  document.getElementById('task-detail').classList.remove('region-review-content');
  if (regionBrowseMode === 'rules') {
    renderRegionRuleCities();
    return;
  }
  document.querySelector('#task-list .sidebar-header h2').textContent = '任务列表';
  const list = document.getElementById('task-items');
  document.getElementById('task-count').textContent =
    `共 ${(manifestData.tasks || []).length} 个`;

  // This one selector filters tasks and chooses the generation target.
  const citySel = document.getElementById('city-filter');
  if (citySel) {
    citySel.innerHTML = '<option value="" data-ui-text>全部城市</option>' + cityOptions.map(city =>
      `<option value="${escAttr(city.city_id)}">${esc(city.label || city.city_name || city.city_id)}</option>`
    ).join('');
    citySel.value = cityOptions.some(city => city.city_id === currentCity) ? currentCity : '';
  }

  const tasks = manifestData.tasks || [];
  if (!tasks.length) {
    list.innerHTML = currentCity
      ? '<div class="task-empty" data-ui-text>该城市当前没有任务。选择任务类型后可直接新建第一批任务。</div>'
      : '<div class="task-empty" data-ui-text>当前世界还没有可显示的任务。</div>';
    return;
  }
  list.innerHTML = tasks.map(t => {
    const typeMeta = typeMetaLine(t);
    const reviewClass = `status-${t.review_status || 'none'}`;
    return `
    <div class="task-item ${reviewClass}"
      data-folder="${attr(t.folder_ref || t.folder)}"
      data-task-type="${esc(t.task_type || '')}"
      data-city-id="${esc(t.city_id || '')}"
      data-review-status="${esc(t.review_status || '')}"
      data-prompt-language="${esc(t.prompt_language || t.summary?.prompt_language || '')}"
      onclick="selectTask(this.dataset.folder)">
      <div class="folder">${renderStatusIcon(t.review_status)} <span data-ui-text data-ui-en-text="${attr(englishTaskFolder(t.folder, t.task_id))}">${esc(t.folder)}</span></div>
      <div class="preview">${esc(t.query_preview || t.folder)}</div>
      <div class="meta">
        ${t.task_type ? `<span class="tag task-type-tag">${esc(t.task_type)}</span>` : ''}
        ${renderReviewStatusTag(t.review_status || '')}
        ${t.history_only ? '<span class="tag" title="当前契约或世界绑定检查未通过；保留历史查看，不代表可评测" data-ui-text>历史任务 · 当前校验未通过</span>' : ''}
        ${t.generation_origin === 'legacy_writer_compat' ? '<span class="tag" title="保留旧事件和答案，仅兼容重写题面；不是独立新增样本" data-ui-text>旧版兼容重写</span>' : ''}
        ${t.generation_origin === 'current_flow' ? '<span class="tag" data-ui-text>当前流程生成</span>' : ''}
        ${renderFeasibilityTag(t.summary?.review_feasibility || '')}
        ${t.prompt_language ? `<span class="tag language-tag">${esc(t.prompt_language)}</span>` : ''}
        ${typeMeta}
      </div>
    </div>`;
  }).join('');
}

function typeMetaLine(t) {
  const s = t.summary || {};
  const tt = t.task_type || '';
  switch (tt) {
    case 'route_planning':
      return `${s.distance_km ? `<span>${s.distance_km}km</span>` : ''}
        <span>${s.mode || ''}</span>
        ${s.expected_feasible !== undefined ? `<span data-ui-text>${s.expected_feasible ? '可行' : '不可行'}</span>` : ''}`;
    case 'multi_entity':
      return `${s.character_count ? `<span data-ui-text>${s.character_count}人</span>` : ''}
        ${s.place_count ? `<span data-ui-text>${s.place_count}个地点</span>` : ''}`;
    case 'itinerary':
      return `${s.num_days ? `<span>${s.num_days}d</span>` : ''}
        ${s.event_count ? `<span data-ui-text>${s.event_count}事件</span>` : ''}`;
    case 'safety_planning':
      return `${s.risk_dimensions ? `<span>${esc(s.risk_dimensions)}</span>` : ''}
        ${s.expected_safe !== undefined ? `<span data-ui-text>${s.expected_safe ? '安全' : '不安全'}</span>` : ''}`;
    case 'region_aware':
      return `${s.scene ? `<span>${esc(s.scene)}</span>` : ''}
        ${s.constraint_count ? `<span data-ui-text>${s.constraint_count}约束</span>` : ''}`;
    case 'dynamic_replanning':
      return `${s.base_task_type ? `<span>${esc(s.base_task_type)}</span>` : ''}
        ${s.event_count || s.shared_events || s.new_events ? `<span data-ui-text>${s.event_count || ((s.shared_events || 0) + (s.new_events || 0))}事件</span>` : ''}
        <span>${esc(s.dynamic_pressure || s.frequency_level || '')}</span>`;
    case 'place_grounding':
    case 'place_discovery':
      return `${t.grounding_target_type ? `<span>${esc(t.grounding_target_type)}</span>` : ''}
        ${t.service_type ? `<span>${esc(t.service_type)}</span>` : ''}
        ${t.top_candidate ? `<span>${esc(t.top_candidate)}</span>` : ''}
        ${t.quality_passed === true ? '<span class="tag quality-pass">quality ok</span>' : ''}
        ${t.quality_passed === false ? '<span class="tag quality-fail">quality fail</span>' : ''}`;
    default:
      return `${t.category ? `<span>${esc(t.category)}</span>` : ''}
        <span>${t.scenario_type || ''}</span>
        <span data-ui-text>${t.env_event_count || 0}事件</span>
        <span data-ui-text>${t.judge_stages || 0}阶段</span>`;
  }
}

function showTaskView() {
  document.getElementById('nav-app-content').classList.remove('active');
  currentView = 'tasks';
  document.getElementById('nav-tasks').classList.add('active');
  document.getElementById('nav-runs').classList.remove('active');
  document.getElementById('task-list').style.display = '';
  document.getElementById('task-detail').dataset.serviceDetailView = '';
  document.getElementById('task-detail').innerHTML =
    '<div class="placeholder" data-ui-text>← 从左侧选择一个任务查看详情</div>';
  if (currentFolder) selectTask(currentFolder);
  else if (document.getElementById('task-type-filter').value === 'dynamic_replanning') showDynamicSources();
}

async function showRunView() {
  reviewState.save();
  document.getElementById('nav-app-content').classList.remove('active');
  dynamicSourceRequest++;
  currentView = 'runs';
  document.getElementById('nav-tasks').classList.remove('active');
  document.getElementById('nav-runs').classList.add('active');
  document.getElementById('task-list').style.display = 'none';
  document.getElementById('task-detail').dataset.serviceDetailView = '';
  document.getElementById('task-detail').innerHTML = renderRunDashboardShell();
  // Reading saved results must not wait for formal input validation.
  await Promise.all([initialiseRunScope().then(loadRuns), loadRuns()]);
}

function filterTasks() {
  if (regionBrowseMode === 'rules') {
    const q = document.getElementById('filter-input').value.toLowerCase();
    document.querySelectorAll('.region-city-item').forEach(el => el.classList.toggle('hidden', !q || el.textContent.toLowerCase().includes(q) ? false : true));
    return;
  }
  const q = document.getElementById('filter-input').value.toLowerCase();
  const status = (document.getElementById('status-filter') || {}).value || '';
  const language = (document.getElementById('language-filter') || {}).value || '';
  const taskType = (document.getElementById('task-type-filter') || {}).value || '';
  const cityFilter = (document.getElementById('city-filter') || {}).value || '';
  document.querySelectorAll('.task-item').forEach(el => {
    const text = (el.textContent || '').toLowerCase();
    const taskTypeOk = !taskType || el.dataset.taskType === taskType;
    const statusOk = !status || el.dataset.reviewStatus === status;
    const languageOk = !language || el.dataset.promptLanguage === language;
    const cityOk = !cityFilter || (el.dataset.cityId || '') === cityFilter;
    const queryOk = !q || text.includes(q);
    const generationJobOk = !generationJobTaskFilter || generationJobTaskFilter.has(el.dataset.folder);
    const sourceOk = taskType !== 'dynamic_replanning' || !dynamicOverviewFilter || dynamicOverviewFilter.has(el.dataset.folder);
    el.classList.toggle('hidden', !(taskTypeOk && statusOk && languageOk && cityOk && queryOk && generationJobOk && sourceOk));
  });
}

function renderReviewStatusTag(status) {
  if (!status) return '';
  return `<span class="tag review-status ${esc(status)}">${esc(status)}</span>`;
}

function renderFeasibilityTag(feasibility) {
  if (feasibility !== 'infeasible_verified') return '';
  return '<span class="tag infeasible-verified" data-ui-text>不可完成 · 已验证</span>';
}

function renderStatusIcon(status) {
  if (status === 'approved') return '✅';
  if (status === 'rejected') return '❌';
  if (status === 'needs_fix') return '🔧';
  return '⚠️';
}

async function selectTask(folder) {
  dynamicSaveDraft();
  taskDirty = false;
  dynamicSourceRequest++;
  currentFolder = folder;
  document.getElementById('task-detail').dataset.serviceDetailView = '';
  document.querySelectorAll('.task-item').forEach(el => el.classList.remove('active'));
  const el = Array.from(document.querySelectorAll('.task-item')).find(item => item.dataset.folder === folder);
  if (el) el.classList.add('active');

  try {
    const url = `/api/task?folder=${encodeURIComponent(folder)}`;
    const cached = reviewState.peek(url);
    if (cached) renderTaskDetail(cached);
    const data = await reviewState.json(url);
    if (currentFolder !== folder || currentView !== 'tasks') return;
    if (taskDirty) return;
    taskDirty = false;
    if (!cached || JSON.stringify(cached) !== JSON.stringify(data)) renderTaskDetail(data);
    reviewState.save();
  } catch (e) {
    console.error('Failed to load task', e);
    document.getElementById('task-detail').innerHTML =
      `<div class="section"><div class="section-title" data-ui-text>加载失败</div><div class="prompt-box">${esc(e.message || e)}</div></div>`;
  }
}

// ========== RENDER ==========

function renderTaskDetail(data) {
  const tk = data.task || {};
  if (tk.task_type === 'dynamic_replanning') {
    judgeState=null;sourceState=null;
    const detail=document.getElementById('task-detail');
    detail.dataset.taskType=tk.task_type;detail.dataset.serviceDetailView='';
    detail.innerHTML=renderDynamicReview(data);initDynamicReviewMap();return;
  }
  if (tk.task_type === 'region_aware') {
    judgeState=null;sourceState=null;
    const detail=document.getElementById('task-detail');
    detail.dataset.taskType=tk.task_type;detail.dataset.serviceDetailView='';
    detail.innerHTML=renderRegionReview(data);initRegionReviewMap();return;
  }
  if (tk.task_type === 'safety_planning') {
    judgeState = null; sourceState = null;
    const detail = document.getElementById('task-detail');
    detail.dataset.taskType = tk.task_type;
    detail.dataset.serviceDetailView = '';
    detail.innerHTML = renderSafetyReview(data);
    initSafetyReviewMap();
    return;
  }
  if (tk.task_type === 'itinerary') {
    judgeState = null; sourceState = null;
    const detail = document.getElementById('task-detail');
    detail.dataset.taskType = tk.task_type;
    detail.dataset.serviceDetailView = '';
    detail.innerHTML = renderItineraryReview(data);
    initItineraryReviewMap();
    return;
  }
  if (tk.task_type === 'place_grounding') {
    judgeState = null;
    sourceState = null;
    const detail = document.getElementById('task-detail');
    detail.dataset.serviceDetailView = '';
    detail.dataset.taskType = tk.task_type;
    detail.innerHTML = renderGroundingReview(data);
    initGroundingPreviewMap(tk, data.world_slice || {});
    return;
  }
  const env = data.env_events ? (data.env_events.env_events || []) : [];
  if (tk.task_type === 'route_planning') {
    judgeState = null; sourceState = null;
    const detail = document.getElementById('task-detail');
    detail.dataset.taskType = tk.task_type;
    detail.dataset.serviceDetailView = '';
    detail.innerHTML = renderRouteReview(data);
    initRouteReviewMap(data);
    return;
  }
  if (tk.task_type === 'multi_entity') {
    judgeState=null; sourceState=null;
    const detail=document.getElementById('task-detail');
    detail.dataset.taskType=tk.task_type;
    detail.dataset.serviceDetailView='';
    detail.innerHTML=renderMultiReview(data);
    initMultiReviewMap(data);
    return;
  }
  const seq = data.judge_sequence || {};
  const judge = seq.stages || [];
  const ws = data.world_slice || {};
  const source = data.curated_source || {};
  const isPlaceTask = tk.task_type === 'place_grounding' || tk.task_type === 'place_discovery';
  const hasCuratedSource = !!(source && source.example);

  // Keep an editable copy of the judge sequence for this task.
  judgeState = { stages: JSON.parse(JSON.stringify(judge)) };
  judgeState.schema_version = seq.schema_version || 'curated.judge.v1';
  judgeState.completion_policy = seq.completion_policy || 'key_nodes';
  sourceState = source.example ? JSON.parse(JSON.stringify(source.example)) : null;
  selectedJudgeNode = firstJudgeNodePath(judgeState.stages);

  let html = taskDirty
    ? '<div class="edit-notice" data-ui-text>本地修改尚未保存。保存后任务会自动标记为“需修改”，等待重新审核。</div>'
    : '';

  if (tk.review_metadata?.feasibility === 'infeasible_verified') {
    html += '<div class="edit-notice" data-ui-text>不可完成任务 · 已验证。请审核公开事实是否足以支持该结论；这不是构建失败或自动拒绝。</div>';
  }

  // ── User prompt + summary ──
  if (tk.task_type === 'place_discovery') {
    html += renderPlacePromptEditor(tk);
  } else {
    html += '<div class="section"><div class="section-title">📋 User Prompt</div>';
    html += `<div class="prompt-box">${esc(tk.user_prompt || '(empty)')}</div></div>`;
  }

  const s = tk.summary || {};
  if (tk.task_type === 'place_discovery') {
    const detail = document.getElementById('task-detail');
    detail.dataset.taskType = tk.task_type;
    detail.dataset.serviceDetailView = '';
    detail.innerHTML = html + renderDiscoverySpatial(data) + renderDiscoverySourceReviewPanel(source, source.example || {}, data)
      + renderReviewControls(tk.task_type, data.review_status);
    initDiscoverySpatial(data);
    return;
  }
  if (tk.task_type === 'place_grounding') {
    html += renderGroundingImage(tk, data.grounding_gold || {}, ws);
  } else if (tk.task_type === 'place_discovery') {
    html += renderDiscoveryOverview(tk, data.discovery_gold || {}, source);
  } else {
    html += renderTypeEditorPanel(tk, data);
  }
  html += '<div class="summary-grid">';
  html += card(s.character_count || 0, '参与者');
  html += card(s.place_count || 0, '地点');
  if (isPlaceTask) {
    html += card(s.judge_node_count || judge.length || 0, '评判节点');
    html += card(s.key_node_count || 0, '关键节点');
  } else {
    if (tk.task_type === 'safety_planning' && data.task_sandbox?.safety_choice) {
      html += card((data.task_sandbox.safety_choice.events || []).length, '本题安全公告');
    } else {
      html += card(s.env_event_count || env.length || 0, '环境事件');
    }
    html += card(s.judge_stage_count || judge.length || 0, '评判阶段');
  }
  html += '</div>';
  html += renderReviewControls(tk.task_type, data.review_status);

  document.getElementById('task-detail').dataset.serviceDetailView = '';
  document.getElementById('task-detail').innerHTML = html;
  if (tk.task_type === 'dynamic_replanning') {
    document.getElementById('task-detail').insertAdjacentHTML('afterbegin',
      '<button class="nav-btn" type="button" onclick="showDynamicSources()" data-ui-text>← 返回任务统计</button>');
  }
  document.getElementById('task-detail').dataset.taskType = tk.task_type || '';
  if (tk.task_type === 'place_grounding') initGroundingPreviewMap(tk, ws);
  const mapTypes = ['route_planning','multi_entity','itinerary','safety_planning','region_aware','dynamic_replanning'];
  if (mapTypes.includes(tk.task_type)) { initTaskMap('task-map'); setTimeout(() => { if(taskMap) taskMap.invalidateSize(); }, 500); }
  if (tk.task_type === 'route_planning') {
    loadRPData().then(() => {
      if (taskMap) { renderRPBgRoutes(); renderRPPlaces(); renderRPPOIQueryDropdowns(); renderRPTaskMap(tk, data); }
    });
  }

  // ── D3 Flowchart (read-only overview) ──
  if (tk.task_type !== 'place_discovery') {
    document.getElementById('task-detail').insertAdjacentHTML('beforeend', '<div id="graph-slot"></div>');
    renderFlowchart(judge);
  }

  // ── Editable judge panel + env events + world slice ──
  const rest = document.getElementById('task-detail');
  let restHtml = '';
  if (isPlaceTask && hasCuratedSource) {
    restHtml += renderSourceReviewPanel(source);
    if (tk.task_type !== 'place_discovery') restHtml += `<div id="judge-editor-wrap">${renderJudgeInspector()}</div>`;
  }
  if (tk.task_type !== 'safety_planning' || !data.task_sandbox?.safety_choice) restHtml += renderEnvEvents(env);
  restHtml += renderWorldSlice(ws);
  rest.insertAdjacentHTML('beforeend', restHtml);
}

function renderDiscoveryOverview(task, discoveryGold, source) {
  const summary = task.summary || {};
  const example = (source || {}).example || {};
  const gold = discoveryGold.gold || example.gold || {};
  const candidates = gold.candidates || [];
  const distractors = discoveryGold.distractors || example.distractors || [];
  const criteria = discoveryGold.ranking_criteria || example.ranking_criteria || summary.ranking_criteria || [];
  const start = discoveryGold.start || example.start || summary.start || {};
  return `<div class="section discovery-overview">
    <div class="section-title" data-ui-text>地点发现任务 / Discovery Evidence</div>
    <div class="discovery-grid">
      <div class="discovery-card">
        <div class="discovery-card-title" data-ui-text>任务约束</div>
        <div class="kv"><span data-ui-text>服务类型</span><b>${esc(discoveryGold.service_type || example.service_type || summary.service_type || '-')}</b></div>
        <div class="kv"><span data-ui-text>搜索区域</span><b>${esc(start.area_name || start.district_id || '-')}</b></div>
        <div class="criteria-list">${criteria.length ? criteria.map(item => `<div>${esc(item)}</div>`).join('') : '<div class="muted" data-ui-text>暂无 ranking criteria</div>'}</div>
      </div>
      <div class="discovery-card">
        <div class="discovery-card-title" data-ui-text>Gold 推荐候选</div>
        ${candidates.length ? candidates.map(renderDiscoveryCandidate).join('') : '<div class="muted" data-ui-text>暂无 gold candidates</div>'}
      </div>
      <div class="discovery-card">
        <div class="discovery-card-title" data-ui-text>干扰/排除候选</div>
        ${distractors.length ? distractors.map(renderDiscoveryDistractor).join('') : '<div class="muted" data-ui-text>暂无显式 distractors</div>'}
      </div>
    </div>
  </div>`;
}

function renderDiscoveryCandidate(candidate) {
  return `<div class="discovery-candidate">
    <div class="candidate-main">
      <b>#${esc(candidate.rank || '')} ${esc(candidate.name || candidate.place_id || '')}</b>
      <code>${esc(candidate.place_id || '')}</code>
    </div>
    <div class="candidate-meta">${esc(candidate.place_type || '')}</div>
    <ul>${(candidate.reasons || []).map(reason => `<li>${esc(reason)}</li>`).join('')}</ul>
  </div>`;
}

function renderDiscoveryDistractor(candidate) {
  return `<div class="discovery-candidate distractor">
    <div class="candidate-main">
      <b>${esc(candidate.name || candidate.place_id || '')}</b>
      <code>${esc(candidate.place_id || '')}</code>
    </div>
    <div class="candidate-meta">${esc(candidate.reason_to_exclude || candidate.place_type || '')}</div>
  </div>`;
}

// ========== TYPE-SPECIFIC GT EDITOR PANELS ==========

function renderTypeEditorPanel(tk, data) {
  switch (tk.task_type) {
    case 'route_planning': return renderRoutePlanningPanel(tk, data);
    case 'multi_entity': return renderMultiEntityEditor(tk, data);
    case 'itinerary': return renderItineraryEditor(tk, data);
    case 'safety_planning': return renderSafetyEditor(tk, data);
    case 'region_aware': return renderRegionEditor(tk, data);
    case 'dynamic_replanning': return renderReplanEditor(tk, data);
    default: return '';
  }
}

function renderReviewControls(taskType, currentStatus) {
  const statuses = ['pending', 'approved', 'needs_fix', 'rejected'];
  const labels = {pending: '🕐 待审核', approved: '✅ 通过', needs_fix: '⚠️ 需修改', rejected: '❌ 拒绝'};
  let h = '<div class="section"><div class="section-title" data-ui-text>📝 审批</div><div class="review-bar">';
  for (const s of statuses) {
    const active = currentStatus === s ? ' active' : '';
    h += `<button class="review-btn${active}" onclick="reviewTask('${s}')" data-ui-text>${labels[s]}</button>`;
  }
  h += `<input id="review-notes" placeholder="备注..." data-ui-placeholder style="flex:1;margin-left:8px;padding:4px;border:1px solid #444;background:#1e1e2e;color:#cdd6f4;border-radius:4px;" />`;
  h += '</div></div>';
  return h;
}

async function reviewTask(status) {
  if (!currentFolder) return;
  const notes = (document.getElementById('review-notes') || {}).value || '';
  try {
    const resp = await fetch(`/api/task/review?folder=${encodeURIComponent(currentFolder)}`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({status, notes}),
    });
    const d = await resp.json();
    const labelMap = {'approved':'✅ 已通过','rejected':'❌ 已拒绝'};
    const msg = (document.getElementById('review-status') || {});
    if (d.ok) {
      document.getElementById('review-notes').value = '';
      await refreshManifest();
      await selectTask(currentFolder);
    } else if (msg) {
      msg.textContent = `Error: ${d.error}`; msg.style.color = '#e74c3c';
    }
  } catch (err) {
    const msg = (document.getElementById('review-status') || {});
    if (msg) { msg.textContent = '❌ ' + err.message; msg.style.color = '#e74c3c'; }
  }
}

function markTaskDirty() {
  taskDirty = true;
}

async function refreshManifest() {
  const taskType = document.getElementById('task-type-filter')?.value || '';
  const response = await fetch(
    `/api/manifest?history=1&task_type=${encodeURIComponent(taskType)}&batch=${encodeURIComponent(currentBatch)}&city=${encodeURIComponent(currentCity)}`,
  );
  if (!response.ok) return;
  manifestData = await response.json();
  renderHeader();
  renderTaskList();
}

async function showTaskSaveResult(result, messageElementId) {
  const message = document.getElementById(messageElementId);
  if (!result.ok) {
    if (message) { message.textContent = `保存失败: ${result.error || '未知错误'}`; message.style.color = '#d95d39'; }
    return false;
  }
  taskDirty = false;
  const contract = result.contract || {};
  const suffix = contract.valid === false ? '，但产物契约仍有警告' : '，已标记为需重新审核';
  if (message) {
    message.textContent = `已保存${suffix}`;
    message.style.color = contract.valid === false ? '#c58b1d' : '#16806a';
    setTimeout(() => { message.textContent = ''; }, 3500);
  }
  try {
    await refreshManifest();
  } catch (error) {
    console.warn('Saved task but could not refresh task list', error);
  }
  return true;
}

async function saveTask(fields) {
  if (!currentFolder) return;
  const taskType = document.getElementById('task-detail').dataset.taskType || '';
  try {
    const resp = await fetch(`/api/task/save?folder=${encodeURIComponent(currentFolder)}`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({task_type: taskType, fields}),
    });
    const d = await resp.json();
    if (d.ok) { alert('GT 保存成功'); }
    else { alert(`Error: ${d.error}`); }
  } catch (err) { alert('保存失败: ' + err.message); }
}

function taskSaveBtn(fields, label) {
  const json = JSON.stringify(fields).replace(/'/g, "&#39;").replace(/"/g, '&quot;');
  return `<button class="save-btn" onclick='saveTask(${json})' style="margin-top:8px;">${label || '💾 保存'}</button>`;
}

// ── multi_entity editor (full port from per-type vis) ──
const ME_COLORS = { participant: '#3388ff', restaurant: '#e67e22', gt_restaurant: '#4ecca3' };
let meTaskLayers = [];

function clearMETask() { meTaskLayers.forEach(l => taskMap && taskMap.removeLayer(l)); meTaskLayers = []; }

function renderMultiEntityEditor(tk, data) {
  const sb = (data.task_sandbox || {});
  const gen = data.generation || {};
  const participants = sb.participants || [];
  const restaurants = sb.candidate_restaurants || [];
  const expected = sb.expected_restaurant_service_id || '';
  const constraints = sb.constraints || {};
  const plans = sb.acceptable_plans || [];
  const routeQueries = gen.route_queries || [];

  // Map rendering
  setTimeout(() => {
    clearMETask();
    if (!taskMap) return;
    const bounds = [];
    for (const p of participants) {
      const lat = p.home_lat, lng = p.home_lng;
      if (!lat) continue;
      const m = L.circleMarker([lat, lng], {radius:7,fillColor:ME_COLORS.participant,color:'#fff',weight:2,fillOpacity:1}).addTo(taskMap);
      const car = p.has_car ? '🚗' : '';
      m.bindTooltip(`<b>${car} ${esc(p.name)}</b><br>可用:${p.available_from}-${p.must_leave_by}<br>饮食:${p.dietary||''} · 预算:${p.budget}`);
      meTaskLayers.push(m);
      bounds.push([lat, lng]);
      const lbl = L.marker([lat, lng], {icon: L.divIcon({className:'', html:`<div style="background:${ME_COLORS.participant};color:#fff;padding:1px 5px;border-radius:3px;font-size:10px;white-space:nowrap;transform:translateX(-50%);position:absolute;bottom:8px">${(p.name||'').substr(0,4)}</div>`, iconSize:[0,0]})}).addTo(taskMap);
      meTaskLayers.push(lbl);
    }
    for (const r of restaurants) {
      const loc = r.location || {};
      const lat = loc.lat, lng = loc.lng;
      if (!lat) continue;
      const isGT = r.service_id === expected;
      const color = isGT ? ME_COLORS.gt_restaurant : ME_COLORS.restaurant;
      const m = L.circleMarker([lat, lng], {radius:isGT?10:6,fillColor:color,color:isGT?'#000':'#fff',weight:2,fillOpacity:1}).addTo(taskMap);
      const gtTag = isGT ? '⭐ GT 正确答案<br>' : '';
      m.bindTooltip(`<b>${esc(r.name)}</b><br>${gtTag}¥${r.price_pp}/人 | ${r.cuisine} | ${r.rating}★<br>${r.district||''} | 容纳:${r.max_party}人`);
      meTaskLayers.push(m);
      bounds.push([lat, lng]);
    }
    if (bounds.length > 0) taskMap.fitBounds(bounds, {padding:[30,30], maxZoom:15});
  }, 500);

  // HTML panel
  let h = '<div id="task-map" style="width:100%;height:350px;border-radius:4px;background:#111;margin-bottom:8px;"></div>';
  h += '<div class="section"><div class="section-title" data-ui-text>🍽️ 多主体协同</div>';

  // Participants
  h += '<div style="margin-bottom:8px"><b>👥 参与者(' + participants.length + '人)</b>';
  for (const p of participants) {
    h += `<div style="font-size:11px;padding:4px 6px;margin:2px 0;background:#1e1e2e;border-radius:3px">${esc(p.name)} · ${p.age||''}岁 · ${p.occupation||''}<br>时间: ${p.available_from}-${p.must_leave_by} · 饮食: ${p.dietary||'无'} · 预算: ${p.budget} · 有车: ${p.has_car?'是':'否'} · 可接送: ${p.can_drive_others?'是':'否'}</div>`;
  }
  h += '</div>';

  // Restaurants
  h += '<div style="margin-bottom:8px"><b>🍽️ 候选餐厅(' + restaurants.length + '个)</b>';
  for (const r of restaurants) {
    const isGT = r.service_id === expected;
    h += `<div style="font-size:11px;padding:4px 6px;margin:2px 0;background:#1e1e2e;border-radius:3px;border-left:3px solid ${isGT?ME_COLORS.gt_restaurant:ME_COLORS.restaurant}">${isGT?'⭐ ':''}${esc(r.name)} · ${r.cuisine} · ¥${r.price_pp}/人 · ${r.rating}★<br>${r.district||''} · 容纳:${r.max_party}人 · 营业至:${r.open_until||'?'}<br>饮食选项: ${(r.dietary_options||[]).join(', ')}</div>`;
  }
  h += '</div>';

  // Constraints
  h += '<div style="margin-bottom:8px"><b data-ui-text>📐 约束</b><div style="font-size:11px;padding:4px 6px;background:#1e1e2e;border-radius:3px">';
  h += `最大预算: ¥${constraints.max_budget_pp||'-'} · 最大通勤: ${constraints.max_commute_min||'-'}min · 禁止模式: ${(constraints.forbidden_modes||[]).join(',')||'无'}<br>`;
  h += `包含所有饮食: ${constraints.must_accommodate_all_dietary?'是':'否'} · 拼车容量: ${constraints.carpool_capacity||'-'} · 最大步行: ${constraints.max_walking_distance_m?constraints.max_walking_distance_m+'m':'无'} · 停留: ${constraints.stop_duration_min?constraints.stop_duration_min+'min':'无'}`;
  h += '</div></div>';

  // GT answer
  h += `<div style="margin-bottom:8px"><b data-ui-text>🎯 GT 答案</b><div style="font-size:11px;padding:4px 6px;background:#1e1e2e;border-radius:3px" data-ui-text>预期可行: <b>${sb.expected_feasible!==false?'✅ 是':'❌ 否'}</b>${sb.minimum_possible_duration_min?` · 最短耗时: ${sb.minimum_possible_duration_min}min`:''}</div></div>`;

  // Acceptable plans
  if (plans.length > 0) {
    h += '<div style="margin-bottom:8px"><b>📋 可接受方案(' + plans.length + ')</b>';
    for (const plan of plans.slice(0, 3)) {
      const rid = plan.restaurant_service_id || '';
      const r = restaurants.find(rr => rr.service_id === rid) || {};
      h += `<div style="font-size:11px;padding:4px 6px;margin:2px 0;background:#1e1e2e;border-radius:3px">🍽️ ${esc(r.name||rid)||'?'} | 🚗 接送: ${(plan.pickup_order||[]).join(' → ')} | ⏱ 通勤: ${plan.total_commute_min||'?'}min | 💰 ¥${plan.total_cost_pp||'?'}/人</div>`;
    }
    h += '</div>';
  }

  // Commute matrix
  if (routeQueries.length > 0) {
    const byMode={}; for(const q of routeQueries){byMode[q.mode]=(byMode[q.mode]||0)+1;}
    h += `<div style="margin-bottom:8px"><b>🗺️ 通勤矩阵(${routeQueries.length}条)</b><div style="font-size:10px;max-height:120px;overflow-y:auto;padding:4px;background:#1e1e2e;border-radius:3px">模式:${Object.keys(byMode).join(',')}<br>`;
    for (const q of routeQueries.slice(0, 15)) {
      const dur = Math.round((q.duration_s||0)/60);
      const walkTag = q.walking_m ? ` 🚶${q.walking_m}m` : '';
      h += `${q.from}→${q.to} ${q.mode}: ${((q.distance_m||0)/1000).toFixed(1)}km ${dur}min${walkTag}<br>`;
    }
    if (routeQueries.length>15) h += '...';
    h += '</div></div>';
  }

  // GT Editor
  h += '<div style="background:#1e1e2e;border-radius:4px;padding:8px"><b style="font-size:12px" data-ui-text>GT 编辑器</b>';
  h += `<div style="margin:4px 0"><label data-ui-text>预期餐厅</label><select id="me-restaurant-sel" style="width:100%;margin-top:2px;background:#111;color:#cdd6f4;border:1px solid #444;padding:4px;border-radius:3px">`;
  for (const r of restaurants) {
    h += `<option value="${escAttr(r.service_id)}" ${r.service_id===expected?'selected':''}>${esc(r.name)} · ${r.cuisine} · ¥${r.price_pp}</option>`;
  }
  h += '</select></div>';
  h += `<div style="margin:4px 0"><label data-ui-text>可行性</label><select id="me-feasible" style="margin-left:8px;background:#111;color:#cdd6f4;border:1px solid #444;padding:2px;border-radius:3px"><option value="true" ${sb.expected_feasible!==false?'selected':''} data-ui-text>可行</option><option value="false" ${sb.expected_feasible===false?'selected':''} data-ui-text>不可行</option></select></div>`;
  h += `<div style="margin:4px 0"><label>user_prompt</label><textarea id="me-prompt" rows="3" style="width:100%;margin-top:2px;background:#111;color:#cdd6f4;border:1px solid #444;padding:4px;border-radius:3px;font-size:11px">${esc(tk.user_prompt||'')}</textarea></div>`;
  h += `<button class="save-btn" onclick="meSaveGT()" data-ui-text>💾 保存 GT</button><span id="me-save-msg" style="font-size:11px;margin-left:8px"></span>`;
  h += '</div></div>';
  return h;
}

async function meSaveGT() {
  if (!currentFolder) return;
  const sid = document.getElementById('me-restaurant-sel')?.value || '';
  const feasible = document.getElementById('me-feasible')?.value === 'true';
  const prompt = document.getElementById('me-prompt')?.value || '';
  try {
    const r = await fetch(`/api/task/save?folder=${encodeURIComponent(currentFolder)}`, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({task_type:'multi_entity', fields:{expected_restaurant_service_id:sid, expected_feasible:feasible, user_prompt:prompt, constraints:{}}}),
    });
    const d = await r.json();
    await showTaskSaveResult(d, 'me-save-msg');
  } catch(e) { const m=document.getElementById('me-save-msg');if(m){m.textContent='❌'+e.message;m.style.color='#e74c3c';} }
}

// ── itinerary editor (full port) ──
const IT_CC = {hotel:'#f97583',restaurant:'#ffa657',attraction:'#79c0ff',cafe:'#7ee787',transit:'#d2a8ff',other:'#666'};
let itMarkers = {}, itLines = [], itHlLines = [], itData = null, regionEditData = null;

function itClearMap() {
  itLines.forEach(l => taskMap && taskMap.removeLayer(l));
  itHlLines.forEach(l => taskMap && taskMap.removeLayer(l));
  for (const k in itMarkers) taskMap && taskMap.removeLayer(itMarkers[k]);
  itMarkers = {}; itLines = []; itHlLines = [];
}

function renderItineraryEditor(tk, data) {
  const sb = data.task_sandbox || {};
  itData = data;
  const ch = sb.character || {}, ht = sb.hotel || {};
  const gen = sb._gen_summary || {};
  const days = (sb.correct_itinerary || {}).days || [];
  const poiDetails = sb._poi_details || [];
  const menus = sb.menus || [];

  // Map
  setTimeout(() => {
    itClearMap();
    if (!taskMap || !poiDetails.length) return;
    const bounds = [];
    for (const p of poiDetails) {
      if (!p.lat) continue;
      const c = IT_CC[p.category] || '#666';
      const icon = L.divIcon({className:'',html:`<div style="width:10px;height:10px;background:${c};border:2px solid #fff;border-radius:50%"></div>`,iconSize:[10,10],iconAnchor:[5,5]});
      const pop = `<b>${esc(p.name)}</b><br>${p.category||''} | ${p.district||''}<br>${p.opening_hours||''} | ${p.price_level||''} | ⭐${p.rating!=null?p.rating:'-'}`;
      const m = L.marker([p.lat, p.lng], {icon}).addTo(taskMap).bindPopup(pop);
      itMarkers[p.place_id] = m;
      bounds.push([p.lat, p.lng]);
    }
    if (bounds.length) taskMap.fitBounds(bounds, {padding:[40,40]});
    setTimeout(() => taskMap.invalidateSize(), 200);
  }, 500);

  let h = '<div id="task-map" style="width:100%;height:350px;border-radius:4px;background:#111;margin-bottom:8px;"></div>';
  h += '<div class="section"><div class="section-title" data-ui-text>📅 行程审核</div>';
  // Meta
  h += `<div class="editor-grid" style="margin-bottom:8px">`;
  h += `<div class="kv"><span>Task ID</span><b>${esc(tk.task_id||'-')}</b></div>`;
  h += `<div class="kv"><span data-ui-text>天数</span><b>${sb.num_days||days.length||'?'}</b></div>`;
  h += `<div class="kv"><span data-ui-text>GT事件</span><b>${gen.gt_event_count||'?'}</b></div>`;
  h += `<div class="kv"><span>POIs</span><b>${gen.total_pois||poiDetails.length||'?'}</b></div>`;
  h += `<div class="kv"><span data-ui-text>酒店</span><b>${esc(ht.name||'?')} | ${esc(ht.district_name||'?')}</b></div>`;
  h += `<div class="kv"><span data-ui-text>角色</span><b>${esc(ch.name||'?')} | ¥${ch.budget_daily||'?'}/d</b></div>`;
  h += `</div>`;

  // Timeline
  h += '<div style="margin-bottom:8px"><b data-ui-text>📅 时间线</b>';
  for (let di = 0; di < days.length; di++) {
    const evts = days[di].events || [];
    h += `<div style="margin:4px 0;padding:6px;background:#1e1e2e;border-radius:4px"><b>Day ${days[di].day}</b> <span style="font-size:10px;color:#a6adc8">(${evts.length} events)</span>`;
    for (let ei = 0; ei < evts.length; ei++) {
      const e = evts[ei];
      const isFixed = e.event_type==='arrival' || e.event_type==='departure' || e.event_type==='hotel_checkin' || e.event_type==='checkout';
      h += `<div onclick="itSelEvent(${di},${ei})" style="cursor:pointer;font-size:11px;padding:2px 6px;margin:1px 0;border-radius:2px;display:flex;align-items:center;gap:4px">`;
      h += `<span style="color:#a6adc8;min-width:80px">${e.start||'?'}-${e.end||'?'}</span> `;
      if (ei > 0 && e.commute_min != null) h += `<span style="color:#f0c060;min-width:30px">${e.commute_min}m</span> `;
      h += `<span style="color:#79c0ff;min-width:80px">${e.event_type||'?'}</span> <b style="flex:1">${esc(e.place_name||'?')}</b>`;
      h += `<span onclick="event.stopPropagation();itOpenEdit(${di},${ei})" style="cursor:pointer;padding:0 4px;color:#a6adc8" title="编辑">✏</span>`;
      if (!isFixed) h += `<span onclick="event.stopPropagation();itDelEvent(${di},${ei})" style="cursor:pointer;padding:0 4px;color:#e74c3c" title="删除" data-ui-title>✕</span>`;
      h += `</div>`;
    }
    h += `<div style="margin-top:4px"><button class="save-btn" style="font-size:10px;padding:2px 8px" onclick="itAddEvent(${di})" data-ui-text>+ 添加事件</button></div>`;
    h += '</div>';
  }
  h += '</div>';

  // POI grid
  if (poiDetails.length) {
    h += '<div style="margin-bottom:8px"><b>📍 POIs</b><div style="display:flex;flex-wrap:wrap;gap:4px">';
    for (const p of poiDetails) {
      h += `<span onclick="itFocusPoi('${escAttr(p.place_id)}')" style="cursor:pointer;font-size:10px;padding:2px 6px;background:#1e1e2e;border-radius:3px;border-left:3px solid ${IT_CC[p.category]||'#666'}">${esc(p.name||'')}</span>`;
    }
    h += '</div></div>';
  }

  // GT Editor
  h += '<div style="background:#1e1e2e;border-radius:4px;padding:8px"><b style="font-size:12px" data-ui-text>GT 编辑器</b>';
  h += `<div style="margin:4px 0"><label data-ui-text>可行性</label><select id="it-feasible" style="margin-left:8px;background:#111;color:#cdd6f4;border:1px solid #444;padding:2px;border-radius:3px"><option value="true" ${sb.expected_feasible!==false?'selected':''} data-ui-text>可行</option><option value="false" ${sb.expected_feasible===false?'selected':''} data-ui-text>不可行</option></select></div>`;
  h += `<div style="margin:4px 0"><label>user_prompt</label><textarea id="it-prompt" rows="3" style="width:100%;margin-top:2px;background:#111;color:#cdd6f4;border:1px solid #444;padding:4px;border-radius:3px;font-size:11px">${esc(tk.user_prompt||'')}</textarea></div>`;
  h += `<button class="save-btn" onclick="itSaveGT()" data-ui-text>💾 保存 GT</button><span id="it-save-msg" style="font-size:11px;margin-left:8px"></span>`;
  h += '</div>';

  // Audit warnings
  const warns = (sb._audit || {}).warnings || [];
  if (warns.length) {
    h += '<div style="margin-top:8px"><b style="color:#f0c060" data-ui-text>⚠ 审计警告</b>';
    for (const w of warns) h += `<div style="font-size:11px;color:#f0c060;padding:2px 0">⚠ ${esc(w)}</div>`;
    h += '</div>';
  }

  // Menus
  if (menus && menus.length) {
    h += '<div style="margin-top:8px"><b data-ui-text>📋 菜单模板</b>';
    for (const m of menus) {
      h += `<div style="margin:4px 0;padding:4px;background:#1e1e2e;border-radius:3px;font-size:11px"><b>Day ${m.day}</b>`;
      for (const e of (m.fixed_events||[])) h += `<div style="padding:1px 0 0 8px"><span style="color:#4ecca3">[fixed]</span> ${e.event_type} @ ${esc(e.place_name||'?')}</div>`;
      for (const s of (m.required_slots||[])) h += `<div style="padding:1px 0 0 8px"><span style="color:#f0c060">[req]</span> ${s.event_type} → ${(s.candidates||[]).map(c=>c.name).join(',')}</div>`;
      h += '</div>';
    }
    h += '</div>';
  }

  // Constraints
  const constraints = sb.constraints || {};
  if (Object.keys(constraints).length) {
    h += '<div style="margin-top:8px"><b data-ui-text>📐 约束</b><div style="font-size:11px;padding:4px;background:#1e1e2e;border-radius:3px">';
    h += Object.entries(constraints).map(([k,v]) => `${k}: ${JSON.stringify(v)}`).join('<br>');
    h += '</div></div>';
  }
  // Edit modal
  h += `<div id="it-edit-modal" style="display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.7);z-index:9999">
    <div style="position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);background:#1e1e2e;border-radius:8px;padding:16px;width:400px;max-height:80vh;overflow-y:auto;box-shadow:0 4px 20px rgba(0,0,0,0.5)">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px"><b id="it-ed-title" data-ui-text>编辑事件</b><span onclick="itCloseEdit()" style="cursor:pointer;font-size:18px">✕</span></div>
      <div style="margin:4px 0"><label style="font-size:11px;color:#a6adc8" data-ui-text>开始</label><input id="it-ed-start" style="width:100%;margin-top:2px;background:#111;color:#cdd6f4;border:1px solid #444;padding:4px;border-radius:3px" placeholder="HH:MM" /></div>
      <div style="margin:4px 0"><label style="font-size:11px;color:#a6adc8" data-ui-text>结束</label><input id="it-ed-end" style="width:100%;margin-top:2px;background:#111;color:#cdd6f4;border:1px solid #444;padding:4px;border-radius:3px" placeholder="HH:MM" /></div>
      <div style="margin:4px 0"><label style="font-size:11px;color:#a6adc8" data-ui-text>类型</label><select id="it-ed-type" style="width:100%;margin-top:2px;background:#111;color:#cdd6f4;border:1px solid #444;padding:4px;border-radius:3px"></select></div>
      <div style="margin:4px 0"><label style="font-size:11px;color:#a6adc8" data-ui-text>地点</label><select id="it-ed-place" style="width:100%;margin-top:2px;background:#111;color:#cdd6f4;border:1px solid #444;padding:4px;border-radius:3px"></select></div>
      <button class="save-btn" onclick="itSaveEdit()" style="margin-top:8px;margin-right:8px" data-ui-text>💾 保存</button>
      <button onclick="itCloseEdit()" style="background:#444;color:#cdd6f4;border:none;padding:6px 16px;border-radius:4px;cursor:pointer" data-ui-text>取消</button>
    </div>
  </div>`;

  h += '</div>';
  return h;
}

function itSelEvent(di, ei) {
  itHlLines.forEach(l => taskMap && taskMap.removeLayer(l)); itHlLines = [];
  const days = (itData.task_sandbox || {}).correct_itinerary || {};
  const evts = (days.days || [])[di]?.events || [];
  if (ei >= evts.length) return;
  const e = evts[ei];
  if (e.place_id && itMarkers[e.place_id]) itMarkers[e.place_id].openPopup();
}

function itAddEvent(di) {
  const days = ((itData.task_sandbox || {}).correct_itinerary || {}).days || [];
  if (di >= days.length) return;
  const poiDetails = (itData.task_sandbox || {})._poi_details || [];
  const firstPoi = poiDetails.length ? poiDetails[0] : {};
  days[di].events = days[di].events || [];
  const newIdx = days[di].events.length;
  days[di].events.push({
    event_type:'lunch', place_id:firstPoi.place_id||'',
    place_name:firstPoi.name||'', category:firstPoi.category||'other',
    start:'12:00', end:'13:00', commute_from_prev_event_id:null, commute_mode:null, commute_min:0,
  });
  itOpenEdit(di, newIdx);
}

function itDelEvent(di, ei) {
  const days = ((itData.task_sandbox || {}).correct_itinerary || {}).days || [];
  if (di >= days.length || ei >= (days[di].events||[]).length) return;
  days[di].events.splice(ei, 1);
  markTaskDirty();
  renderTaskDetail(itData);
}

function itOpenEdit(di, ei) {
  const days = ((itData.task_sandbox || {}).correct_itinerary || {}).days || [];
  if (di >= days.length || ei >= (days[di].events||[]).length) return;
  const e = days[di].events[ei];
  const poiDetails = (itData.task_sandbox || {})._poi_details || [];
  const m = document.getElementById('it-edit-modal');
  if (!m) return;
  m.style.display = 'block';
  m.dataset.di = di; m.dataset.ei = ei;
  document.getElementById('it-ed-title').textContent = `编辑事件 — Day ${days[di].day}`;
  document.getElementById('it-ed-start').value = e.start||'';
  document.getElementById('it-ed-end').value = e.end||'';
  const typeSel = document.getElementById('it-ed-type');
  typeSel.innerHTML = `<option value="${e.event_type||'lunch'}">${e.event_type||'lunch'}</option>
    <option>lunch</option><option>dinner</option><option>breakfast</option>
    <option>morning_activity</option><option>afternoon_activity</option><option>evening_leisure</option>
    <option>business_meeting</option>`;
  typeSel.value = e.event_type||'lunch';
  const placeSel = document.getElementById('it-ed-place');
  placeSel.innerHTML = poiDetails.map(p => `<option value="${escAttr(p.place_id)}" ${p.place_id===e.place_id?'selected':''}>${esc(p.name)} [${p.category||''}]</option>`).join('');
}

function itCloseEdit() {
  const m = document.getElementById('it-edit-modal');
  if (m) m.style.display = 'none';
}

function itSaveEdit() {
  const m = document.getElementById('it-edit-modal');
  if (!m) return;
  const di = parseInt(m.dataset.di), ei = parseInt(m.dataset.ei);
  const days = ((itData.task_sandbox || {}).correct_itinerary || {}).days || [];
  if (isNaN(di) || di >= days.length) return;
  const e = (days[di].events || [])[ei];
  if (!e) return;
  const poiDetails = (itData.task_sandbox || {})._poi_details || [];
  const pid = document.getElementById('it-ed-place').value;
  const np = poiDetails.find(p => p.place_id === pid);
  e.place_id = pid; if (np) { e.place_name = np.name; e.category = np.category; }
  e.event_type = document.getElementById('it-ed-type').value;
  e.start = document.getElementById('it-ed-start').value;
  e.end = document.getElementById('it-ed-end').value;
  itCloseEdit();
  markTaskDirty();
  renderTaskDetail(itData);
}

function itFocusPoi(pid) {
  const m = itMarkers[pid];
  if (m) { taskMap.setView(m.getLatLng(), 14); m.openPopup(); }
}

async function itSaveGT() {
  if (!currentFolder) return;
  const feasible = document.getElementById('it-feasible')?.value === 'true';
  const prompt = document.getElementById('it-prompt')?.value || '';
  const it = (itData.task_sandbox || {}).correct_itinerary || {};
  const constraints = (itData.task_sandbox || {}).constraints || {};
  try {
    const r = await fetch(`/api/task/save?folder=${encodeURIComponent(currentFolder)}`, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({task_type:'itinerary', fields:{correct_itinerary:it, expected_feasible:feasible, user_prompt:prompt, constraints:constraints}}),
    });
    const d = await r.json();
    await showTaskSaveResult(d, 'it-save-msg');
  } catch(e) { const m=document.getElementById('it-save-msg');if(m){m.textContent='❌'+e.message;m.style.color='#e74c3c';} }
}

// ── safety_planning editor (full port) ──
let sfMarkers = {};

function sfClearMap() { for (const k in sfMarkers) taskMap && taskMap.removeLayer(sfMarkers[k]); sfMarkers = {}; }

function renderSafetyEditor(tk, data) {
  if (data.task_sandbox?.safety_choice && typeof renderSafetyChoiceReview === 'function') {
    return renderSafetyChoiceReview(tk, data);
  }
  const sb = data.task_sandbox || {};
  const gen = sb._gen_summary || {};
  const ch = sb.character || {};
  const rr = sb.risky_target || {};
  const cond = sb.risk_conditions || {};
  const plan = sb.expected_plan || {};
  const alts = plan.alternatives || [];
  const rej = plan.rejected || [];
  const risk = sb.risk_factors || [];
  const constraints = sb.constraints || {};
  const poiDetails = sb._poi_details || [];
  const rid = rr.place_id || '';
  const altIds = new Set((sb.alternatives||[]).map(a=>a.place_id));
  const distIds = new Set((sb.distractors||[]).map(d=>d.place_id));

  // Map
  setTimeout(() => {
    sfClearMap();
    if (!taskMap || !poiDetails.length) return;
    const bounds = [];
    for (const p of poiDetails) {
      if (!p.lat) continue;
      const isRisky = p.place_id === rid, isDist = distIds.has(p.place_id);
      const color = isRisky ? '#f85149' : isDist ? '#d29922' : '#7ee787';
      const label = isRisky ? 'RISKY' : isDist ? 'DISTRACTOR' : 'SAFE';
      const icon = L.divIcon({className:'',html:`<div style="width:10px;height:10px;background:${color};border:2px solid #fff;border-radius:50%"></div>`,iconSize:[10,10],iconAnchor:[5,5]});
      const m = L.marker([p.lat,p.lng],{icon}).addTo(taskMap).bindPopup(`<b>${label}: ${esc(p.name)}</b><br>indoor:${p.indoor} weather_sensitive:${p.weather_sensitive}`);
      sfMarkers[p.place_id] = m; bounds.push([p.lat, p.lng]);
    }
    if (bounds.length) taskMap.fitBounds(bounds, {padding:[40,40]});
    setTimeout(() => taskMap.invalidateSize(), 200);
  }, 500);

  let h = '<div id="task-map" style="width:100%;height:350px;border-radius:4px;background:#111;margin-bottom:8px;"></div>';
  h += '<div class="section"><div class="section-title" data-ui-text>安全规划审核 · 旧版任务</div><p data-ui-text>此任务使用旧版风险目标与替代方案结构，不包含新版 C + 安全事件的唯一 GT 证明。</p>';
  // Meta
  h += '<div class="editor-grid" style="margin-bottom:8px">';
  h += `<div class="kv"><span>Task ID</span><b>${esc(tk.task_id||'-')}</b></div>`;
  h += `<div class="kv"><span data-ui-text>风险维度</span><b>${esc((gen.risk_dimensions||[]).join(',')||'?')}</b></div>`;
  h += `<div class="kv"><span data-ui-text>预期</span><b>${sb.expected_safe === true ? 'Safe' : sb.expected_safe === false ? 'Unsafe' : '未记录'}</b></div>`;
  h += `<div class="kv"><span data-ui-text>推荐</span><b>${esc(plan.recommendation||'?')}</b></div>`;
  h += '</div>';

  // Risk factors
  if (risk.length) {
    h += '<div style="margin-bottom:8px"><b style="color:#f85149" data-ui-text>⚠ 风险因素</b><ul style="margin:2px 0;font-size:11px">';
    for (const f of risk) h += `<li style="color:#f85149">${esc(f)}</li>`;
    h += '</ul></div>';
  }

  // Conditions
  if (Object.keys(cond).length) {
    h += '<div style="margin-bottom:8px"><b data-ui-text>🌡️ 风险条件</b><div style="font-size:10px;padding:4px;background:#1e1e2e;border-radius:3px">';
    h += Object.entries(cond).map(([k,v]) => `${k}: ${v}`).join('; ');
    h += '</div></div>';
  }

  // GT Plan
  h += '<div style="margin-bottom:8px"><b data-ui-text>🎯 GT 方案</b>';
  if (alts.length + rej.length > 0) {
    h += '<div style="font-size:11px;padding:4px;background:#1e1e2e;border-radius:3px;margin-top:4px">';
    h += '<div style="color:#4ecca3"><b data-ui-text>替代方案:</b><ul style="margin:2px 0">';
    for (const a of alts) h += `<li>${esc(a.name)}: ${esc(a.reason||'')}</li>`;
    h += '</ul></div>';
    h += '<div style="color:#e74c3c"><b data-ui-text>已拒绝:</b><ul style="margin:2px 0">';
    for (const r of rej) h += `<li>${esc(r.name)}: ${esc(r.reason||'')}</li>`;
    h += '</ul></div></div>';
  }

  // POI grid
  if (poiDetails.length) {
    h += '<div style="margin-bottom:8px"><b>📍 POIs</b><div style="display:flex;flex-wrap:wrap;gap:4px;margin-top:4px">';
    for (const p of poiDetails) {
      const cls = p.place_id===rid?'ch-risky':distIds.has(p.place_id)?'ch-distractor':'ch-alternative';
      const bg = p.place_id===rid?'#f85149':distIds.has(p.place_id)?'#d29922':'#7ee787';
      h += `<span onclick="sfFocusPoi('${escAttr(p.place_id)}')" style="cursor:pointer;font-size:10px;padding:2px 6px;background:#1e1e2e;border-radius:3px;border-left:3px solid ${bg}">${esc(p.name||'')}</span>`;
    }
    h += '</div></div>';
  }

  // Audit
  const warns = (sb._audit||{}).warnings||[];
  if (warns.length) {
    h += '<div style="margin-bottom:8px"><b style="color:#f0c060" data-ui-text>⚠ 审计警告</b>';
    for (const w of warns) h += `<div style="font-size:11px;color:#f0c060">⚠ ${esc(w)}</div>`;
    h += '</div>';
  }

  // GT Editor
  h += '<div style="background:#1e1e2e;border-radius:4px;padding:8px"><b style="font-size:12px" data-ui-text>GT 编辑器</b>';
  h += `<div style="margin:4px 0"><label data-ui-text>安全性</label><select id="sf-safe" style="margin-left:8px;background:#111;color:#cdd6f4;border:1px solid #444;padding:2px;border-radius:3px">${typeof sb.expected_safe !== 'boolean' ? '<option value="" selected data-ui-text>未记录</option>' : ''}<option value="true" ${sb.expected_safe===true?'selected':''}>Safe</option><option value="false" ${sb.expected_safe===false?'selected':''}>Unsafe</option></select></div>`;
  h += `<div style="margin:4px 0"><label data-ui-text>风险因素 (每行一个)</label><textarea id="sf-risk" rows="3" style="width:100%;margin-top:2px;background:#111;color:#cdd6f4;border:1px solid #444;padding:4px;border-radius:3px;font-size:11px">${esc((risk).join('\n'))}</textarea></div>`;
  h += `<div style="margin:4px 0"><label data-ui-text>推荐方案</label><input id="sf-rec" value="${escAttr(plan.recommendation||'')}" style="width:100%;margin-top:2px;background:#111;color:#cdd6f4;border:1px solid #444;padding:4px;border-radius:3px;font-size:11px" /></div>`;
  h += `<div style="margin:4px 0"><label data-ui-text>替代方案 (每行: name|reason)</label><textarea id="sf-alts" rows="3" style="width:100%;margin-top:2px;background:#111;color:#cdd6f4;border:1px solid #444;padding:4px;border-radius:3px;font-size:11px">${esc(alts.map(a=>`${a.name||''}|${a.reason||''}`).join('\n'))}</textarea></div>`;
  h += `<div style="margin:4px 0"><label data-ui-text>已拒绝方案 (每行: name|reason)</label><textarea id="sf-rej" rows="3" style="width:100%;margin-top:2px;background:#111;color:#cdd6f4;border:1px solid #444;padding:4px;border-radius:3px;font-size:11px">${esc(rej.map(r=>`${r.name||''}|${r.reason||''}`).join('\n'))}</textarea></div>`;
  h += `<div style="margin:4px 0"><label data-ui-text>约束 (JSON)</label><textarea id="sf-constraints" rows="3" style="width:100%;margin-top:2px;background:#111;color:#cdd6f4;border:1px solid #444;padding:4px;border-radius:3px;font-size:11px">${esc(JSON.stringify(constraints,null,2))}</textarea></div>`;
  h += `<div style="margin:4px 0"><label>user_prompt</label><textarea id="sf-prompt" rows="3" style="width:100%;margin-top:2px;background:#111;color:#cdd6f4;border:1px solid #444;padding:4px;border-radius:3px;font-size:11px">${esc(tk.user_prompt||'')}</textarea></div>`;
  h += `<button class="save-btn" onclick="sfSaveGT()" data-ui-text>💾 保存 GT</button><span id="sf-save-msg" data-ui-output style="font-size:11px;margin-left:8px"></span>`;
  h += '</div></div>';
  return h;
}

function sfFocusPoi(pid) { const m = sfMarkers[pid]; if (m) { taskMap.setView(m.getLatLng(),14); m.openPopup(); } }

async function sfSaveGT() {
  if (!currentFolder) return;
  if (document.getElementById('sf-safe')?.value === '') {
    document.getElementById('sf-save-msg').textContent = '旧版安全性未记录，请明确选择后保存。';
    return;
  }
  const safe = document.getElementById('sf-safe')?.value === 'true';
  const risk = (document.getElementById('sf-risk')?.value || '').split('\n').filter(s => s.trim());
  const rec = document.getElementById('sf-rec')?.value || '';
  const altsRaw = (document.getElementById('sf-alts')?.value || '').split('\n').filter(s => s.trim()).map(s => {const [n,r]=s.split('|'); return {name:(n||'').trim(),reason:(r||'').trim()};});
  const rejRaw = (document.getElementById('sf-rej')?.value || '').split('\n').filter(s => s.trim()).map(s => {const [n,r]=s.split('|'); return {name:(n||'').trim(),reason:(r||'').trim()};});
  let constraints = {};
  try { constraints = JSON.parse(document.getElementById('sf-constraints')?.value || '{}'); } catch(e) {}
  const prompt = document.getElementById('sf-prompt')?.value || '';
  const expected_plan = { recommendation: rec, alternatives: altsRaw, rejected: rejRaw };
  try {
    const r = await fetch(`/api/task/save?folder=${encodeURIComponent(currentFolder)}`, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({task_type:'safety_planning', fields:{expected_safe:safe, risk_factors:risk, expected_plan:expected_plan, constraints:constraints, user_prompt:prompt}}),
    });
    const d = await r.json();
    await showTaskSaveResult(d, 'sf-save-msg');
  } catch(e) { const m=document.getElementById('sf-save-msg');if(m){m.textContent='❌'+e.message;m.style.color='#e74c3c';} }
}

// ── region_aware editor (full port) ──
let raMarkers = {};

function raClearMap() { for (const k in raMarkers) taskMap && taskMap.removeLayer(raMarkers[k]); raMarkers = {}; }

function renderRegionEditor(tk, data) {
  const sb = data.task_sandbox || {};
  regionEditData = data;
  const gen = sb._gen_summary || {};
  const ch = sb.character || {};
  const cds = sb.cultural_decisions || [];
  const cs = sb.constraints || [];
  const wp = sb.wrong_plans || [];
  const cids = sb.constraint_ids || [];
  const plan = sb.expected_plan || {};
  const poiDetails = sb._poi_details || [];

  const cMap = {};
  cs.forEach(c => { cMap[c.id] = (c.rule||'').slice(0,50); });

  // Map
  setTimeout(() => {
    raClearMap();
    if (!taskMap || !poiDetails.length) return;
    const bounds = [];
    for (const p of poiDetails) {
      if (!p.lat) continue;
      const icon = L.divIcon({className:'',html:`<div style="width:10px;height:10px;background:#79c0ff;border:2px solid #fff;border-radius:50%"></div>`,iconSize:[10,10],iconAnchor:[5,5]});
      const m = L.marker([p.lat,p.lng],{icon}).addTo(taskMap).bindPopup(`<b>${esc(p.name)}</b><br>${p.category||''} | ${p.district||''}`);
      raMarkers[p.place_id] = m; bounds.push([p.lat, p.lng]);
    }
    if (bounds.length) taskMap.fitBounds(bounds, {padding:[40,40]});
    setTimeout(() => taskMap.invalidateSize(), 200);
  }, 500);

  let h = '<div id="task-map" style="width:100%;height:350px;border-radius:4px;background:#111;margin-bottom:8px;"></div>';
  h += '<div class="section"><div class="section-title" data-ui-text>🌍 地域感知审核</div>';
  // Meta
  h += '<div class="editor-grid" style="margin-bottom:8px">';
  h += `<div class="kv"><span>Task ID</span><b>${esc(tk.task_id||'-')}</b></div>`;
  h += `<div class="kv"><span data-ui-text>城市</span><b>${esc(gen.city||'?')}</b></div>`;
  h += `<div class="kv"><span data-ui-text>场景</span><b>${esc(gen.scene||'?')}</b></div>`;
  h += `<div class="kv"><span data-ui-text>约束数</span><b>${cids.length}</b></div>`;
  h += '</div>';

  // Cultural decisions table
  if (cds.length) {
    h += '<div style="margin-bottom:8px"><b data-ui-text>📋 文化决策</b><div style="max-height:200px;overflow-y:auto;font-size:10px;background:#1e1e2e;border-radius:3px;padding:4px">';
    h += '<table style="width:100%"><tr style="color:#a6adc8"><th data-ui-text>约束</th><th data-ui-text>事件</th><th data-ui-text>决策</th><th data-ui-text>推理</th></tr>';
    for (const d of cds) {
      h += `<tr onclick="raFocusPoi('${escAttr(d.place_id||'')}')" style="cursor:pointer" title="点击定位地图"><td>${esc(d.constraint_id||'').slice(-8)}</td><td>${esc(d.event_type||'')}</td><td>${esc(d.decision||'')}</td><td style="color:#a6adc8">${esc((d.reasoning||'').slice(0,60))}</td></tr>`;
    }
    h += '</table></div></div>';
  }

  // Wrong plans
  if (wp.length) {
    h += '<div style="margin-bottom:8px"><b style="color:#e74c3c" data-ui-text>✗ 错误方案</b>';
    for (const w of wp) {
      h += `<div style="font-size:11px;padding:3px 6px;margin:2px 0;background:#1e1e2e;border-radius:3px;border-left:3px solid #e74c3c"><span style="color:#f85149">✗</span> ${esc(w.description||'')} <span style="color:#a6adc8">违反: ${esc(w.violates||'')} (${cMap[w.violates]||''})</span></div>`;
    }
    h += '</div>';
  }

  // Constraints
  if (cs.length) {
    h += '<div style="margin-bottom:8px"><b data-ui-text>📐 约束参考</b><div style="max-height:200px;overflow-y:auto;font-size:10px">';
    for (const c of cs) {
      h += `<div style="padding:2px 4px;margin:1px 0;background:#1e1e2e;border-radius:2px"><code style="color:#79c0ff">${esc(c.id||'').slice(-12)}</code> ${esc(c.rule||'')} <span style="color:#a6adc8">${esc(c.type||'')} ${esc(c.severity||'')} ${esc(c.review||'')}</span></div>`;
    }
    h += '</div></div>';
  }

  // Constraint IDs
  if (cids.length) {
    h += '<div style="margin-bottom:8px"><b data-ui-text>🔑 约束 ID</b><div style="display:flex;flex-wrap:wrap;gap:3px;margin-top:2px">';
    for (const cid of cids) h += `<code style="background:#1e1e2e;padding:2px 6px;border-radius:3px;font-size:10px">${esc(cid)}</code>`;
    h += '</div></div>';
  }

  // KB Manager
  h += '<div style="margin-bottom:8px"><b data-ui-text>📚 KB 管理器</b> <button class="save-btn" style="font-size:10px;padding:2px 8px" onclick="raLoadKB()" data-ui-text>🔄 加载KB</button><span id="ra-kb-status" class="inline-status"></span>';
  h += '<div id="ra-kb-cities" style="margin:4px 0"></div>';
  h += '<div id="ra-kb-list" style="max-height:300px;overflow-y:auto;font-size:10px;margin-top:4px"></div>';
  h += '</div>';

  // GT Editor
  h += '<div style="background:#1e1e2e;border-radius:4px;padding:8px"><b style="font-size:12px" data-ui-text>GT 编辑器</b>';
  h += `<div style="margin:4px 0"><label data-ui-text>可行性</label><select id="ra-feasible" style="margin-left:8px;background:#111;color:#cdd6f4;border:1px solid #444;padding:2px;border-radius:3px"><option value="true" ${sb.expected_feasible!==false?'selected':''} data-ui-text>可行</option><option value="false" ${sb.expected_feasible===false?'selected':''} data-ui-text>不可行</option></select></div>`;
  h += `<div style="margin:4px 0"><label>user_prompt</label><textarea id="ra-prompt" rows="3" style="width:100%;margin-top:2px;background:#111;color:#cdd6f4;border:1px solid #444;padding:4px;border-radius:3px;font-size:11px">${esc(tk.user_prompt||'')}</textarea></div>`;
  h += `<button class="save-btn" onclick="raSaveGT()" data-ui-text>💾 保存 GT</button><span id="ra-save-msg" style="font-size:11px;margin-left:8px"></span>`;
  h += '</div></div>';
  return h;
}

function raFocusPoi(pid) { const m = raMarkers[pid]; if (m) { taskMap.setView(m.getLatLng(),14); m.openPopup(); } }

async function raSaveGT() {
  if (!currentFolder) return;
  const feasible = document.getElementById('ra-feasible')?.value === 'true';
  const prompt = document.getElementById('ra-prompt')?.value || '';
  const sb = (regionEditData || {}).task_sandbox || {};
  try {
    const r = await fetch(`/api/task/save?folder=${encodeURIComponent(currentFolder)}`, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({task_type:'region_aware', fields:{expected_feasible:feasible, user_prompt:prompt, expected_plan:sb.expected_plan||{}, wrong_plans:sb.wrong_plans||[], explanation_keywords:sb.explanation_keywords||[]}}),
    });
    const d = await r.json();
    await showTaskSaveResult(d, 'ra-save-msg');
  } catch(e) { const m=document.getElementById('ra-save-msg');if(m){m.textContent='❌'+e.message;m.style.color='#e74c3c';} }
}

async function raLoadKB() {
  try {
    const r = await fetch('/api/kb/cities');
    const d = await r.json();
    const cities = d.cities || [];
    const el = document.getElementById('ra-kb-cities');
    let h = '';
    for (const c of cities) {
      h += `<button onclick="raLoadKBDetail('${escAttr(c.city)}')" style="margin:2px;padding:2px 8px;background:#111;color:#cdd6f4;border:1px solid #444;border-radius:3px;cursor:pointer;font-size:10px">${esc(c.city)} (${c.approved}/${c.total})</button>`;
    }
    el.innerHTML = h || '无KB城市数据';
  } catch(e) { console.error(e); }
}

// Region review is an audit surface, not a raw private-bundle browser.
// Keep private identifiers available on disk for traceability, but do not
// render them as the primary information an auditor must interpret.
function raReadablePlan(plan) {
  const event = ((plan || {}).events || [])[0] || {};
  const actions = ((plan || {}).cultural_actions || []).map(v => String(v).replaceAll('_', ' '));
  return [event.place_name, event.start, actions.join(', ')].filter(Boolean).join(' · ') || '无额外要求';
}

renderRegionEditor = function(tk, data) {
  const sb = data.task_sandbox || {};
  regionEditData = data;
  const gen = sb._gen_summary || {};
  const character = sb.character || {};
  const constraints = sb.constraints || [];
  const material = (sb.decision_contract || {}).material_constraints || [];
  const projection = sb.review_projection || {};
  const wrongPlans = sb.wrong_plans || [];
  const poiDetails = sb._poi_details || [];
  const ruleById = {};
  constraints.forEach(item => { ruleById[item.id] = item.rule_text || item.rule || '已验证的地域要求'; });

  setTimeout(() => {
    raClearMap();
    if (!taskMap || !poiDetails.length) return;
    const bounds = [];
    poiDetails.forEach(poi => {
      if (!poi.lat) return;
      const icon = L.divIcon({className:'', html:'<div style="width:10px;height:10px;background:#79c0ff;border:2px solid #fff;border-radius:50%"></div>', iconSize:[10,10], iconAnchor:[5,5]});
      const marker = L.marker([poi.lat, poi.lng], {icon}).addTo(taskMap).bindPopup(`<b>${esc(poi.name)}</b>`);
      raMarkers[poi.place_id] = marker;
      bounds.push([poi.lat, poi.lng]);
    });
    if (bounds.length) taskMap.fitBounds(bounds, {padding:[40,40]});
    setTimeout(() => taskMap.invalidateSize(), 200);
  }, 500);

  let h = '<div id="task-map" style="width:100%;height:350px;border-radius:4px;background:#111;margin-bottom:8px;"></div>';
  h += '<div class="section"><div class="section-title" data-ui-text>地域感知审核</div>';
  h += '<div class="editor-grid" style="margin-bottom:8px">';
  h += `<div class="kv"><span data-ui-text>访客</span><b>${esc(character.name || '-')}</b></div>`;
  h += `<div class="kv"><span data-ui-text>城市</span><b>${esc(gen.city || tk.city_id || '-')}</b></div>`;
  h += `<div class="kv"><span data-ui-text>任务活动</span><b>${esc((sb.planning_profile || {}).activity || '本地规划')}</b></div>`;
  h += `<div class="kv"><span data-ui-text>审核依据</span><b>${constraints.length} 项</b></div>`;
  h += '</div>';

  if (projection.complete) {
    const context = projection.region_context || {};
    const evidence = projection.evidence || [];
    const impacts = projection.decision_impacts || [];
    h += '<div style="margin-bottom:10px"><b data-ui-text>地域文化情境</b>';
    h += `<div style="font-size:11px;padding:6px;background:#1e1e2e;border-radius:3px">${esc([context.city, ...(context.districts || []), context.activity].filter(Boolean).join(' · '))}</div></div>`;
    h += '<div style="margin-bottom:10px"><b data-ui-text>来源证据</b><div style="max-height:220px;overflow-y:auto;font-size:11px">';
    evidence.forEach(item => {
      h += `<div style="padding:6px;margin:3px 0;background:#1e1e2e;border-radius:3px"><div style="color:#a6adc8">${esc([item.source, item.section].filter(Boolean).join(' · '))}</div><div style="margin-top:3px">${esc(item.quote || '未保存原文摘录')}</div><div style="color:#a6adc8;margin-top:3px">${esc(item.applicability || '')}</div></div>`;
    });
    h += '</div></div>';
    h += '<div style="margin-bottom:10px"><b data-ui-text>规划决策影响</b><div style="max-height:220px;overflow-y:auto;font-size:11px;background:#1e1e2e;border-radius:3px;padding:6px">';
    h += '<table style="width:100%"><tr style="color:#a6adc8"><th data-ui-text>影响维度</th><th data-ui-text>纳入地域条件后</th><th data-ui-text>移除地域条件后</th></tr>';
    impacts.forEach(item => {
      h += `<tr><td>${esc(item.dimension || '规划决策')}</td><td>${esc(item.with_constraint || '')}</td><td style="color:#a6adc8">${esc(item.without_constraint || '')}</td></tr>`;
    });
    h += '</table></div></div>';
  } else {
    h += '<div style="margin-bottom:10px;padding:7px;background:#3a2b16;color:#f0c674;border-radius:3px;font-size:11px" data-ui-text>该历史任务未保存可读来源投影，且不能据此证明地域条件改变了规划决策；仅保留审计，不作为新 Region-Aware 正式任务的展示范式。</div>';
  }

  if (false && material.length) {
    h += '<div style="margin-bottom:10px"><b data-ui-text>地域决策影响</b><div style="max-height:220px;overflow-y:auto;font-size:11px;background:#1e1e2e;border-radius:3px;padding:6px">';
    h += '<table style="width:100%"><tr style="color:#a6adc8"><th data-ui-text>地域要求</th><th data-ui-text>影响维度</th><th data-ui-text>纳入要求后</th><th data-ui-text>移除要求后</th></tr>';
    material.forEach(item => {
      h += `<tr><td>${esc(ruleById[item.constraint_id] || '已验证的地域要求')}</td><td>${esc((item.changed_dimensions || []).join('、'))}</td><td>${esc(raReadablePlan(item.with_constraint))}</td><td style="color:#a6adc8">${esc(raReadablePlan(item.without_constraint))}</td></tr>`;
    });
    h += '</table></div></div>';
  }

  if (false && constraints.length) {
    h += '<div style="margin-bottom:10px"><b data-ui-text>审核依据</b><div style="max-height:160px;overflow-y:auto;font-size:11px">';
    constraints.forEach(item => {
      h += `<div style="padding:4px 6px;margin:2px 0;background:#1e1e2e;border-radius:2px">${esc(item.rule_text || item.rule || '未提供可读依据')} <span style="color:#a6adc8">${esc(item.severity || '')}</span></div>`;
    });
    h += '</div></div>';
  }

  if (wrongPlans.length) {
    h += '<div style="margin-bottom:10px"><b style="color:#e74c3c" data-ui-text>不应接受的方案</b>';
    wrongPlans.forEach(item => { h += `<div style="font-size:11px;padding:4px 6px;margin:2px 0;background:#1e1e2e;border-radius:3px">${esc(item.description || '')}</div>`; });
    h += '</div>';
  }

  h += '<div style="background:#1e1e2e;border-radius:4px;padding:8px"><b style="font-size:12px" data-ui-text>题面复核</b>';
  h += `<div style="margin:4px 0"><label data-ui-text>可行性</label><select id="ra-feasible" style="margin-left:8px;background:#111;color:#cdd6f4;border:1px solid #444;padding:2px;border-radius:3px"><option value="true" ${sb.expected_feasible!==false?'selected':''} data-ui-text>可行</option><option value="false" ${sb.expected_feasible===false?'selected':''} data-ui-text>不可行</option></select></div>`;
  h += `<div style="margin:4px 0"><label data-ui-text>题面</label><textarea id="ra-prompt" rows="4" style="width:100%;margin-top:2px;background:#111;color:#cdd6f4;border:1px solid #444;padding:4px;border-radius:3px;font-size:11px">${esc(tk.user_prompt || '')}</textarea></div>`;
  h += '<button class="save-btn" onclick="raSaveGT()" data-ui-text>保存审核修改</button><span id="ra-save-msg" style="font-size:11px;margin-left:8px"></span></div></div>';
  return h;
};

async function raLoadKBDetail(city) {
  try {
    const r = await fetch(`/api/kb?city=${encodeURIComponent(city)}`);
    const d = await r.json();
    const cs = d.constraints || [];
    const el = document.getElementById('ra-kb-list');
    let h = `<div style="font-size:10px;color:#a6adc8;margin-bottom:4px">${esc(city)} — ${cs.length} 约束</div>`;
    for (const c of cs) {
      const bg = c.review==='approved'?'#1a4a2e':c.review==='rejected'?'#4a1a1e':'#1e1e2e';
      h += `<div style="padding:4px;margin:2px 0;background:${bg};border-radius:3px;font-size:10px">
        <input value="${escAttr(c.rule||'')}" onchange="raEditKBRule('${escAttr(city)}','${escAttr(c.id)}',this.value)" style="width:100%;background:transparent;border:none;color:#cdd6f4;font-size:10px;padding:2px" />
        <div style="display:flex;gap:4px;margin-top:2px">
          <span style="color:#a6adc8">${esc(c.type||'')} ${esc(c.severity||'')}</span>
          <button onclick="raApproveKB('${escAttr(city)}','${escAttr(c.id)}')" style="font-size:9px;padding:1px 6px;background:${c.review==='approved'?'#4ecca3':'#333'};color:#fff;border:none;border-radius:2px;cursor:pointer">✓</button>
          <button onclick="raRejectKB('${escAttr(city)}','${escAttr(c.id)}')" style="font-size:9px;padding:1px 6px;background:${c.review==='rejected'?'#e74c3c':'#333'};color:#fff;border:none;border-radius:2px;cursor:pointer">✗</button>
        </div>
      </div>`;
    }
    el.innerHTML = h;
  } catch(e) { console.error(e); }
}

function raEditKBRule(city, cid, val) {
  return updateRegionKB('/api/kb/update', {city, constraint_id:cid, rule:val});
}

function raApproveKB(city, cid) {
  return updateRegionKB('/api/kb/approve', {city, constraint_id:cid});
}

function raRejectKB(city, cid) {
  return updateRegionKB('/api/kb/reject', {city, constraint_id:cid});
}

async function updateRegionKB(path, payload) {
  const status = document.getElementById('ra-kb-status');
  if (status) status.textContent = '正在同步关联任务...';
  try {
    const response = await fetch(path, {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || `HTTP ${response.status}`);
    const sync = data.synchronization || {};
    const changed = (sync.refreshed || []).length + (sync.invalidated || []).length;
    if (status) status.textContent = changed ? `已同步 ${changed} 个关联任务，均需重新审核。` : '知识库已保存；没有受影响任务。';
    await refreshManifest();
    return data;
  } catch (error) {
    if (status) status.textContent = `同步失败: ${error.message || error}`;
    return null;
  }
}

function generationIsActive(job = generationJob) {
  return !!job && ['queued', 'running', 'cancelling'].includes(job.status);
}

function formatGenerationDuration(seconds) {
  const total = Math.max(0, Number(seconds) || 0);
  const minutes = Math.floor(total / 60);
  const remaining = total % 60;
  return minutes ? `${minutes}:${String(remaining).padStart(2, '0')}` : (window.GeoUI?.t('{count} 秒', {count:remaining}) || `${remaining} 秒`);
}

function formatGenerationHeartbeat(value) {
  const timestamp = value ? new Date(value).getTime() : NaN;
  if (Number.isNaN(timestamp)) return '暂无心跳';
  const seconds = Math.max(0, Math.round((Date.now() - timestamp) / 1000));
  return seconds < 5 ? (window.GeoUI?.t('刚刚有新进度') || '刚刚有新进度') : (window.GeoUI?.t('{seconds} 秒前有新进度', {seconds}) || `${seconds} 秒前有新进度`);
}

function setGenerationFormLocked(locked) {
  const panel = document.getElementById('gen-panel');
  if (!panel) return;
  panel.querySelectorAll('#gen-model, #gen-count, input[name="gen-mode"], #gen-feasibility-mode, #gen-infeasible-percent, #route-diffusion-start, #route-pool-max, #route-cost-preview, #service-detail-build').forEach(el => { el.disabled = locked; });
  const start = document.getElementById('gen-start');
  if (start) {
    start.textContent = locked ? '正在生成' : '生成任务';
    if (routeCostSelected()) {
      validateRouteResourceFields();
    } else {
      start.disabled = locked;
      const startWrap = document.getElementById('gen-start-wrap');
      if (startWrap) {
        startWrap.dataset.tooltip = locked ? '当前任务正在生成' : '';
        startWrap.classList.toggle('has-tooltip', locked);
      }
    }
  }
  dynamicSyncGate();
}

function renderGenerationPanelState() {
  const panel = document.getElementById('gen-panel');
  const toggle = document.getElementById('gen-panel-toggle');
  const compact = document.getElementById('gen-panel-compact');
  if (!panel || !toggle || !compact) return;

  const active = generationIsActive();
  panel.classList.toggle('collapsed', generationPanelCollapsed);
  toggle.innerHTML = generationPanelCollapsed ? '&#9662;' : '&#9652;';
  toggle.title = generationPanelCollapsed ? '展开任务构建详情' : '收起任务构建详情';
  toggle.setAttribute('aria-label', toggle.title);
  toggle.setAttribute('aria-expanded', String(!generationPanelCollapsed));

  compact.hidden = !generationPanelCollapsed;
  if (!generationPanelCollapsed) {
    compact.innerHTML = '';
    return;
  }
  if (!active) {
    compact.innerHTML = '<span class="gen-panel-compact-status" data-ui-text>展开后可配置任务构建。</span><div class="gen-panel-compact-actions"><button type="button" onclick="toggleGenerationPanel()" data-ui-text>展开</button></div>';
    return;
  }
  const job = generationJob;
  const requested = Math.max(1, Number(job.requested_count) || 1);
  const completed = Math.min(Number(job.completed_count) || 0, requested);
  const scope = [job.city, job.task_type].filter(Boolean).join(' · ');
  compact.innerHTML = `
    <span class="gen-panel-compact-status" data-ui-output>构建中 · ${esc(scope || '当前任务')} · 有效 ${completed}/${requested}</span>
    <div class="gen-panel-compact-actions">
      <button type="button" onclick="toggleGenerationPanel()" data-ui-text>展开</button>
      <button type="button" class="gen-stop" onclick="cancelGenerationJob()" data-ui-text>停止</button>
    </div>`;
}

function toggleGenerationPanel() {
  generationPanelCollapsed = !generationPanelCollapsed;
  renderGenerationPanelState();
}

function renderGenerationProgress() {
  const container = document.getElementById('gen-progress');
  if (!container) return;
  if (!generationJob) {
    container.hidden = true;
    container.innerHTML = '';
    return;
  }

  const job = generationJob;
  const active = generationIsActive(job);
  const requested = Math.max(1, Number(job.requested_count) || 1);
  const construction = Math.min(Number(job.construction_count) || 0, requested);
  const completed = Math.min(Number(job.completed_count) || 0, requested);
  const observed = Math.min(Number(job.observed_count) || 0, requested);
  const candidate = Math.min(Math.max(Number(job.candidate_count) || 0, observed), requested);
  const rejected = Math.max(0, Number(job.rejected_count) || 0);
  // The primary completion bar is deliberately tied to formal tasks only.
  // Evidence and candidate work remain visible as separate upstream stages.
  const progress = Math.round((completed / requested) * 100);
  const scope = `${job.model_label || ''} · ${job.city || ''} · ${job.task_type || ''}`;

  container.hidden = false;

  const stages = [
    { label: '任务证据', count: construction, tone: 'evidence' },
    { label: '候选写入', count: candidate, tone: 'candidate' },
    { label: '有效任务', count: completed, tone: 'effective' },
  ].map(stage => {
    const width = Math.round((stage.count / requested) * 100);
    return `<div class="gen-progress-stage gen-progress-stage-${stage.tone}">
      <span data-ui-output>${stage.label} ${stage.count}/${requested}</span>
      <div class="gen-progress-track"><div class="gen-progress-fill" style="width:${width}%"></div></div>
    </div>`;
  }).join('');
  const rejectionNote = rejected ? ` · 已拒绝候选 ${rejected} 条，系统会补齐有效任务缺口` : '';
  const failure = job.error ? `<div class="gen-progress-detail">${esc(job.error)}</div>` : '';
  const terminalActions = job.status === 'completed'
    ? `<button onclick="viewGenerationJobTasks()" data-ui-output>查看本次新增 ${completed} 条</button><button onclick="clearGenerationProgress()" data-ui-text>继续配置</button>`
    : !active
      ? `<button onclick="viewGenerationJobTasks()" ${completed ? '' : 'disabled'} data-ui-text>查看已完成任务</button><button onclick="retryRemainingGeneration()" ${completed < requested ? '' : 'disabled'} data-ui-output>重试剩余 ${Math.max(0, requested - completed)} 条</button><button onclick="clearGenerationProgress()" data-ui-text>关闭</button>`
      : `<button onclick="toggleGenerationPanel()" data-ui-text>收起任务构建</button><button class="gen-stop" onclick="cancelGenerationJob()" data-ui-text>停止</button>`;
  container.innerHTML = `
    <div class="gen-progress-title"><span data-ui-output>${esc(job.phase_label || '正在构建')}</span><span data-ui-output>有效 ${completed}/${requested}</span></div>
    <div class="gen-progress-phase">${esc(scope)}</div>
    <div class="gen-progress-track"><div class="gen-progress-fill" style="width:${progress}%"></div></div>
    <div class="gen-progress-detail" data-ui-output>${esc(job.detail || '正在等待构建进度')}</div>
    <div class="gen-progress-meta">${stages}<span data-ui-output>${rejectionNote}</span><br><span data-ui-output>已耗时 ${formatGenerationDuration(job.elapsed_seconds)} · ${formatGenerationHeartbeat(job.last_activity_at)}</span></div>
    ${failure}
    <div class="gen-progress-actions">${terminalActions}</div>
  `;
}

async function pollGenerationJob() {
  if (!generationJob?.job_id) return;
  try {
    const response = await fetch(`/api/generate/status?job_id=${encodeURIComponent(generationJob.job_id)}`);
    const data = await response.json();
    if (!data.job) return;
    generationJob = data.job;
    updateRouteCostActualFromJob(generationJob);
    const terminal = !generationIsActive();
    const finishedRoutePreview = terminal && ['route_planning', 'multi_entity', 'itinerary', 'region_aware', 'safety_planning'].includes(generationJob.task_type)
      && routeCostPreview && generationJob.city === routeCostPreview.city
      ? routeCostPreview
      : null;
    if (terminal && ['route_planning', 'multi_entity', 'itinerary', 'region_aware', 'safety_planning'].includes(generationJob.task_type)) {
      // A terminal job may have changed both the pool and cache. Its preflight
      // must never authorize another generation attempt.
      routeCostPreview = null;
      routeResourceKey = '';
      routeResourceState = null;
    }
    setGenerationFormLocked(generationIsActive());
    renderGenerationPanelState();
    renderGenerationProgress();
    if (terminal) {
      stopGenerationPolling();
      if (['route_planning', 'multi_entity', 'itinerary', 'region_aware', 'safety_planning'].includes(generationJob.task_type)
          && routeCostSelected() && generationJob.city === currentCity) {
        const refreshed = await refreshRouteResources(true);
        const detail = document.getElementById('task-detail');
        if (finishedRoutePreview && refreshed && detail?.dataset.routeCostView === 'true') {
          const completedPreview = {...finishedRoutePreview, ...refreshed};
          if (generationJob.task_type === 'itinerary') renderItineraryCostDetail(completedPreview);
          else if (generationJob.task_type === 'multi_entity') renderMultiEntityCostDetail(completedPreview);
          else if (generationJob.task_type === 'region_aware') renderCompositionalCostDetail(completedPreview, 'Region');
          else if (generationJob.task_type === 'safety_planning') renderCompositionalCostDetail(completedPreview, 'Safety');
          else renderRouteCostDetail(completedPreview);
        }
      }
      await loadBatches(currentTaskType, currentCity);
    }
  } catch (error) {
    console.error('Failed to poll generation job', error);
  }
}

function startGenerationPolling() {
  stopGenerationPolling();
  pollGenerationJob();
  generationPollTimer = setInterval(pollGenerationJob, 1000);
}

function stopGenerationPolling() {
  if (generationPollTimer) clearInterval(generationPollTimer);
  generationPollTimer = null;
}

async function restoreGenerationJob() {
  try {
    const response = await fetch('/api/generate/status');
    const data = await response.json();
    if (!data.job || !generationIsActive(data.job)) return;
    generationJob = data.job;
    currentTaskType = data.job.task_type || currentTaskType;
    currentCity = data.job.city || currentCity;
    const typeSelect = document.getElementById('task-type-filter');
    if (typeSelect && currentTaskType) typeSelect.value = currentTaskType;
    setGenerationFormLocked(true);
    showGenPanel();
    startGenerationPolling();
  } catch (error) {
    console.error('Failed to restore generation job', error);
  }
}

async function cancelGenerationJob() {
  if (!generationJob?.job_id || !generationIsActive()) return;
  try {
    const response = await fetch('/api/generate/cancel', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({job_id: generationJob.job_id}),
    });
    const data = await response.json();
  if (data.job) generationJob = data.job;
    renderGenerationPanelState();
    renderGenerationProgress();
  } catch (error) {
    console.error('Failed to cancel generation job', error);
  }
}

async function viewGenerationJobTasks() {
  if (!generationJob) return;
  currentTaskType = generationJob.task_type;
  currentCity = generationJob.city;
  const typeSelect = document.getElementById('task-type-filter');
  if (typeSelect) typeSelect.value = currentTaskType;
  await loadBatches(currentTaskType, currentCity);
  const batchSelect = document.getElementById('batch-filter');
  if (batchSelect) batchSelect.value = generationJob.batch;
  generationJobTaskFilter = new Set(generationJob.generated_folders || []);
  await switchBatch(generationJob.batch);
  filterTasks();
}

async function retryRemainingGeneration() {
  const job = generationJob;
  if (!job) return;
  const remaining = Math.max(0, (Number(job.requested_count) || 0) - (Number(job.completed_count) || 0));
  if (!remaining) return;
  currentTaskType = job.task_type;
  currentCity = job.city;
  document.getElementById('task-type-filter').value = currentTaskType;
  document.getElementById('city-filter').value = currentCity;
  await loadBatches(currentTaskType, currentCity);
  const batchSelect = document.getElementById('batch-filter');
  const canAppend = job.retry_batch_available && Boolean(job.batch);
  if (canAppend && ![...batchSelect.options].some(option => option.value === job.batch)) {
    batchSelect.add(new Option(job.batch, job.batch));
  }
  if (canAppend) batchSelect.value = job.batch;
  clearGenerationProgress();
  await switchBatch(batchSelect.value);
  document.getElementById('gen-count').value = remaining;
  document.querySelector(`input[name="gen-mode"][value="${canAppend ? 'append' : 'new'}"]`).checked = true;
  if (routeCostSelected()) invalidateRouteCostPreview();
}

function clearGenerationProgress() {
  stopGenerationPolling();
  generationJob = null;
  generationPanelCollapsed = false;
  generationJobTaskFilter = null;
  setGenerationFormLocked(false);
  renderGenerationPanelState();
  renderGenerationProgress();
  filterTasks();
}

// ── dynamic_replanning editor (full) ──
function renderReplanEditor(tk, data) {
  const sb = (data.task_sandbox || {});
  const summary = tk.summary || {};
  const events = sb.events || ((data.env_events || {}).env_events || []);
  const pressure = summary.dynamic_pressure || sb.dynamic_pressure || sb.frequency_level || '-';
  let h = '<div class="section"><div class="section-title" data-ui-text>🔄 动态重规划审核</div>';
  h += '<div class="editor-grid">';
  if (sb.base_summary) {
    h += `<div class="kv"><span data-ui-text>基础任务</span><b>${esc(sb.base_summary.task_type||'')}: ${esc(sb.base_summary.task_id||'')}</b></div>`;
  }
  h += `<div class="kv"><span data-ui-text>动态压力</span><b>${esc(pressure)}</b></div>`;
  h += `<div class="kv"><span data-ui-text>事件契约</span><b>${esc(sb.dynamic_contract || 'legacy')}</b></div>`;
  if (events.length) {
    h += '<div class="dynamic-event-list"><b data-ui-text>城市事件时间线</b><span class="readonly-note" data-ui-text>事件由城市账本维护；在此只读展示，避免单任务编辑破坏跨任务一致性。</span>';
    for (const event of events) {
      const scope = event.scope || {};
      const activation = event.activation || {};
      const state = event.state_patch || event.entry || {};
      const parents = event.causal_parents || [];
      h += '<div class="dynamic-event-card">';
      h += `<div><b>${esc(event.event_type || event.category || 'event')}</b><code>${esc(event.event_id || '')}</code></div>`;
      h += `<div class="dynamic-event-meta">${esc(event.app || '')} · ${esc(scope.type || event.scope_type || '')}:${esc(scope.id || event.scope_id || '')}</div>`;
      h += `<div class="dynamic-event-meta">${esc(event.activation_time || activation.time || '任务开始')} · ${esc(activation.mode || 'absolute')}</div>`;
      h += `<div class="dynamic-event-desc">${esc(event.description_zh || '')}</div>`;
      h += `<div class="dynamic-event-meta">状态变更: ${esc(JSON.stringify(state))}${parents.length ? ` · 因果前置: ${esc(parents.join(', '))}` : ''}</div>`;
      h += '</div>';
    }
    h += '</div>';
  }
  if (summary.base_task_type) h += `<div class="kv"><span data-ui-text>来源类型</span><b>${esc(summary.base_task_type)}</b></div>`;
  h += '</div>';
  h += '<p class="readonly-note" data-ui-text>基础任务的用户指令、事件与适配约束共同构成动态 episode 的可追溯证据。需要更改事件时，请将任务标为“需修改”并重新生成该动态 overlay。</p></div>';
  return h;
}

// ========== SHARED TASK MAP ==========

let taskMap = null;
let routeCacheData = null;

async function loadRouteCache() {
  if (routeCacheData) return routeCacheData;
  try {
    const r = await fetch('/api/route_cache');
    routeCacheData = await r.json();
  } catch (e) { routeCacheData = {}; }
  return routeCacheData;
}

function initTaskMap(elId) {
  const el = document.getElementById(elId);
  if (!el) return;
  if (taskMap && taskMap._container === el) {  // same container, reuse
    taskMap.invalidateSize();
    return;
  }
  if (taskMap) { taskMap.remove(); taskMap = null; }
  taskMap = L.map(el).setView([35.68, 139.76], 12);
  const googleTile = L.tileLayer('https://mt{s}.google.com/vt/lyrs=m&x={x}&y={y}&z={z}', {
    subdomains: ['0','1','2','3'], attribution: '&copy; Google', maxZoom: 20,
  }).addTo(taskMap);
  const osmTile = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '&copy; OpenStreetMap contributors', maxZoom: 19,
  });
  googleTile.on('tileerror', function() { taskMap.removeLayer(googleTile); osmTile.addTo(taskMap); });
  setTimeout(() => { if (taskMap) taskMap.invalidateSize(); }, 300);
}

function clearTaskMap() {
  if (!taskMap) return;
  taskMap.eachLayer(layer => { if (layer instanceof L.Marker || layer instanceof L.Polyline || layer instanceof L.CircleMarker) taskMap.removeLayer(layer); });
}

function renderRouteMap(tk, data) {
  const sb = data.route_sandbox || data.task_sandbox || {};
  loadRouteCache().then(cache => {
    clearTaskMap();
    const bounds = [];
    const routeId = sb.correct_route_id;
    if (routeId && cache && cache.routes && cache.routes[routeId]) {
      const rte = cache.routes[routeId];
      if (rte.polyline) {
        const pts = decodeRoutePolyline(rte);
        if (pts && pts.length) {
          L.polyline(pts, { color: '#4ecca3', weight: 4 }).addTo(taskMap);
          for (const p of pts) bounds.push(p);
        }
      }
    }
    if (sb.core_places) {
      for (const p of sb.core_places) {
        if (p.lat && p.lng) {
          const color = p.role === 'origin' ? '#3498db' : p.role === 'waypoint' ? '#e67e22' : '#3498db';
          L.circleMarker([p.lat, p.lng], { radius: 6, color, fillOpacity: 0.8 }).addTo(taskMap)
            .bindPopup(`${p.role}: ${p.name || p.place_id}`);
          bounds.push([p.lat, p.lng]);
        }
      }
    }
    const worldPoints = data.world_slice && data.world_slice.places ? data.world_slice.places : [];
    if (worldPoints.length > 0) {
      for (const wp of worldPoints) {
        if (wp.lat && wp.lng) {
          L.circleMarker([wp.lat, wp.lng], { radius: 3, color: '#a0a0a0', fillOpacity: 0.5 }).addTo(taskMap)
            .bindPopup(wp.name || wp.place_id || '');
        }
      }
    }
    if (bounds.length) taskMap.fitBounds(bounds, { padding: [30, 30] });
  });
}

function renderRunDashboardShell() {
  return `
    <div class="run-dashboard">
      <div class="section">
        <div class="section-title" data-ui-text>模型测试 / 结果对比</div>
        <div class="run-controls">
          <label data-ui-text>当前世界 <input id="run-world" value="" readonly></label>
          <label data-ui-text>任务类型 <select id="run-task-type" onchange="onRunTaskTypeChange()"></select></label>
          <label data-ui-text>城市 <select id="run-city" onchange="onRunCityChange()"></select></label>
          <label data-ui-text>任务批次 <select id="run-batch" onchange="onRunBatchChange()"></select></label>
          <label data-ui-text>本次测试数量 <input id="run-task-limit" type="number" min="0" step="1" placeholder="全部" oninput="renderRunScope()" data-ui-placeholder></label>
          <label data-ui-text>Agent 模型 <select id="run-model">${renderTestModelOptions()}</select></label>
          <label class="run-check"><input type="checkbox" id="run-approved-only" onchange="renderRunScope()"><span data-ui-text>仅运行已审核任务</span></label>
          <button class="btn-save" onclick="startRunFromUI()" data-ui-text>新建评测</button>
        </div>
        <p class="run-hint" data-ui-text>模型凭据、Base URL 与采样参数由 <code>config.yaml</code> 统一管理，避免在浏览器中暴露或显示当前 runner 不支持的参数。</p>
        <div id="run-scope" class="run-scope"></div>
        <div id="run-status" class="save-status" data-ui-output></div>
      </div>
      <div class="run-layout">
        <div class="run-list-panel" id="run-list-panel">
          <div class="section-title" data-ui-text>运行列表</div>
          <div id="run-list"></div>
        </div>
        <div class="run-main-panel">
          <div id="run-summary"></div>
          <div id="run-task-list"></div>
          <div id="run-comparison"></div>
        </div>
      </div>
    </div>`;
}

function currentRunScope() {
  const taskType = runScope.taskType || '';
  const city = runScope.city || '';
  const batch = taskType && city ? (runScope.batch || 'latest') : 'latest';
  const tasks = (runScope.manifest || {}).tasks || [];
  const approvedCount = tasks.filter(task => task.review_status === 'approved').length;
  return { taskType, city, batch, taskCount: tasks.length, approvedCount,
    worldId: (runScope.manifest || runScope.allManifest || {}).world_id || '' };
}

function runCityLabel(cityId) {
  const registered = cityOptions.find(city => city.city_id === cityId);
  if (registered) return registered.label || registered.city_name || cityId;
  return String(cityId || '').split('_').map(part => part ? part[0].toUpperCase() + part.slice(1) : '').join(' ');
}

function runTaskTypeLabel(taskType) {
  const labels = {
    route_planning: '路线规划',
    place_grounding: '地点识别',
    place_discovery: '地点发现',
    region_aware: '区域感知',
    itinerary: '长程行程',
    multi_entity: '多主体',
    safety_planning: '安全规划',
    dynamic_replanning: '动态重规划',
  };
  return labels[taskType] || taskType || '未知';
}

function renderRunScopeControls() {
  const all = runScope.allManifest || {};
  const tasks = all.tasks || [];
  const world = (runScope.manifest || all).world_id || '';
  const worldInput = document.getElementById('run-world');
  if (worldInput) worldInput.value = world;
  const typeSelect = document.getElementById('run-task-type');
  const citySelect = document.getElementById('run-city');
  const batchSelect = document.getElementById('run-batch');
  if (!typeSelect || !citySelect || !batchSelect) return;
  const types = [...new Set(tasks.map(task => task.task_type).filter(Boolean))].sort();
  if (runScope.taskType && !types.includes(runScope.taskType)) runScope.taskType = '';
  typeSelect.innerHTML = '<option value="" data-ui-text>全部任务类型</option>' + types.map(type => {
    if (type === 'dynamic_replanning') return ['shared_world', 'task_only'].map(mode =>
      `<option value="dynamic_replanning:${mode}" ${type === runScope.taskType && mode === runScope.dynamicEventMode ? 'selected' : ''}>dynamic_replanning · ${mode === 'task_only' ? '仅本题事件' : '共享世界事件'}</option>`
    ).join('');
    return `<option value="${attr(type)}" ${type === runScope.taskType ? 'selected' : ''}>${esc(type)}</option>`;
  }).join('');
  const cities = [...new Set(tasks.filter(task => !runScope.taskType || task.task_type === runScope.taskType)
    .map(task => task.city_id).filter(Boolean))].sort();
  if (runScope.city && !cities.includes(runScope.city)) runScope.city = '';
  citySelect.innerHTML = '<option value="" data-ui-text>全部城市</option>' + cities.map(city =>
    `<option value="${attr(city)}" ${city === runScope.city ? 'selected' : ''}>${esc(runCityLabel(city))}</option>`
  ).join('');
  if (!runScope.taskType || !runScope.city) {
    runScope.batch = 'latest';
    batchSelect.innerHTML = '<option value="latest" data-ui-text>各范围最新正式批次</option>';
    batchSelect.disabled = true;
  }
}

async function initialiseRunScope() {
  try {
    const response = await fetch('/api/manifest');
    runScope.allManifest = await response.json();
    runScope.taskType = '';
    runScope.dynamicEventMode = 'shared_world';
    runScope.city = '';
    runScope.batch = 'latest';
    await refreshRunScope();
  } catch (error) {
    const target = document.getElementById('run-scope');
    if (target) target.textContent = `无法读取测试范围：${error.message || error}`;
  }
}

async function refreshRunScope() {
  renderRunScopeControls();
  const query = new URLSearchParams({ task_type: runScope.taskType, city: runScope.city, batch: runScope.batch || 'latest' });
  const response = await fetch(`/api/manifest?${query.toString()}`);
  runScope.manifest = await response.json();
  renderRunScopeControls();
  renderRunScope();
}

async function onRunTaskTypeChange() {
  const selection = document.getElementById('run-task-type')?.value || '';
  const [type, mode] = selection.split(':');
  runScope.taskType = type;
  runScope.dynamicEventMode = type === 'dynamic_replanning' && mode === 'task_only' ? 'task_only' : 'shared_world';
  runScope.city = '';
  runScope.batch = 'latest';
  await refreshRunScope();
  await loadRuns();
}

async function onRunCityChange() {
  runScope.city = document.getElementById('run-city')?.value || '';
  runScope.batch = 'latest';
  if (runScope.taskType && runScope.city) await loadRunBatchOptions();
  await refreshRunScope();
  await loadRuns();
}

async function loadRunBatchOptions() {
  const select = document.getElementById('run-batch');
  if (!select) return;
  const response = await fetch(`/api/batches?task_type=${encodeURIComponent(runScope.taskType)}&city=${encodeURIComponent(runScope.city)}`);
  const data = await response.json();
  const batches = data.batches || [];
  const current = data.current || 'latest';
  runScope.batch = 'latest';
  select.disabled = false;
  select.innerHTML = batches.map(batch => {
    const label = batch.is_latest ? '当前正式批次' : '历史正式批次';
    const value = batch.is_latest ? 'latest' : batch.name;
    return `<option value="${attr(value)}" ${batch.name === current ? 'selected' : ''} data-ui-text>${label} · ${batch.task_count} 条</option>`;
  }).join('') || '<option value="latest" data-ui-text>当前正式批次</option>';
}

async function onRunBatchChange() {
  runScope.batch = document.getElementById('run-batch')?.value || 'latest';
  await refreshRunScope();
  await loadRuns();
}

function renderRunScope() {
  const target = document.getElementById('run-scope');
  if (!target) return;
  const scope = currentRunScope();
  if (scope.taskCount === 0) {
    target.className = 'run-scope warning';
    target.textContent = '\u5f53\u524d\u4e16\u754c\u6ca1\u6709\u53ef\u8fd0\u884c\u7684\u6b63\u5f0f\u4efb\u52a1\u3002';
    return;
  }
  target.className = 'run-scope';
  const limit = Number(document.getElementById('run-task-limit')?.value || 0);
  const runCount = limit > 0 ? Math.min(limit, scope.taskCount) : scope.taskCount;
  const limitText = limit > 0
    ? `；<span data-ui-text>本次将稳定抽取</span> ${runCount} <span data-ui-text>条</span>`
    : '；<span data-ui-text>本次运行全部任务</span>';
  const batchText = scope.batch === 'latest' ? '各范围最新正式批次' : '指定历史正式批次';
  target.innerHTML = `<span data-ui-text>可测正式任务</span>：${scope.taskCount} <span data-ui-text>条</span>；<span data-ui-text>范围</span>：<span data-ui-output>${esc(scope.taskType ? runTaskTypeLabel(scope.taskType) : '全部任务类型')}</span> · <span data-ui-output>${esc(scope.city ? runCityLabel(scope.city) : '全部城市')}</span> · <span data-ui-text>${batchText}</span>${limitText}`;
  window.GeoUI?.scan(target);
}

async function loadRuns() {
  const list = document.getElementById('run-list');
  if (!list) return;
  if (!list.childElementCount) list.innerHTML = '<div class="judge-empty" data-ui-text>正在读取运行记录…</div>';
  let data;
  try {
    const resp = await fetch('/api/runs');
    data = await resp.json();
    if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
  } catch (error) {
    list.innerHTML = `<div class="save-status err"><span data-ui-text>运行记录读取失败</span>: ${esc(error.message || error)}</div>`;
    return;
  }
  // History is world-scoped. The selectors define the next evaluation and
  // must not hide previous single-city or single-type runs.
  const runs = (data.runs || []).filter(runMatchesActiveWorld);
  if (selectedRunId && !runs.some(run => run.run_id === selectedRunId)) {
    selectedRunId = '';
    runData = null;
  }
  document.getElementById('run-list').innerHTML = runs.length ? runs.map((r, index) => {
    const active = isRunActive(r);
    const progress = runProgress(r);
    const deleteControl = active
      ? '<button class="btn-del-run is-disabled" title="运行尚未结束，不能删除" disabled data-ui-text>运行中</button>'
      : `<button class="btn-del-run" title="删除此运行" onclick="event.stopPropagation();deleteRun('${attr(r.run_id)}')" data-ui-title data-ui-text>删除</button>`;
    return `
    <div class="run-item ${r.run_id === selectedRunId ? 'active' : ''}" onclick="selectRun('${attr(r.run_id)}', true)">
      <div class="run-name"><span data-ui-text>${String(r.run_id || '').startsWith('example_') ? '保存结果示例' : (isCombinedRun(r) ? '组合运行' : '运行')}</span> #${String(index + 1).padStart(2, '0')}</div>
      <div class="run-meta"><span data-ui-text>${esc(runStatusLabel(r.status))}</span> · ${r.completed_count || 0}/${r.task_count || 0} · PASS ${r.passed || 0}${renderTokenInline(r.token_usage)}</div>
      ${active ? `<div class="run-progress" role="progressbar" aria-label="运行进度" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${progress.percent}"><div class="run-progress-fill" style="width:${progress.percent}%"></div></div><div class="run-progress-label"><span>${progress.completed}/${progress.total}</span> <span data-ui-text>已完成</span></div>` : ''}
      ${deleteControl}
    </div>`;
  }).join('') : '<div class="judge-empty" data-ui-text>暂无运行结果</div>';
  document.querySelectorAll('#run-list .run-item').forEach((element, index) => {
    const title = element.querySelector('.run-name');
    if (title && runs[index]) title.insertAdjacentHTML('afterend', `<div class="run-scope-line">${renderRunBadges(runs[index])}</div>`);
  });
  renderRunSidebarState();
  if (!selectedRunId && runs.length) await selectRun(runs[0].run_id);
}

function runStatusLabel(status) {
  return ({ prepared: '待续跑', queued: '等待中', running: '运行中', completed: '已完成', failed: '失败', interrupted: '已中断', cancelled: '已取消' })[status] || status || '未知';
}

function isRunActive(run) {
  return ['queued', 'running', 'cancelling'].includes(String((run || {}).status || ''));
}

function runProgress(run) {
  const total = Math.max(0, Number((run || {}).task_count || 0));
  const completed = Math.min(total, Math.max(0, Number((run || {}).completed_count || 0)));
  return { total, completed, percent: total > 0 ? Math.round(completed * 100 / total) : 0 };
}

function isCombinedRun(run) {
  const scope = run.scope || {};
  return !scope.task_type || scope.task_type === 'all' || !scope.city || scope.city === 'all_cities';
}

function runMatchesActiveWorld(run) {
  const activeWorld = (runScope.allManifest || runScope.manifest || {}).world_id || '';
  const source = run.scope || {};
  return !activeWorld || source.world_id === activeWorld;
}

function renderRunBadges(run) {
  const eventBadge = renderRunEventBadge(run);
  if (isCombinedRun(run)) {
    return '<span class="run-scope-badge run-city-badge">Mix</span>' +
      '<span class="run-scope-badge run-type-badge">Mix</span>' + eventBadge;
  }
  const scope = run.scope || {};
  return `<span class="run-scope-badge run-city-badge">${esc(runCityLabel(scope.city || 'Unknown'))}</span>` +
    `<span class="run-scope-badge run-type-badge" data-ui-output>${esc(runTaskTypeLabel(scope.task_type))}</span>` + eventBadge;
}

function renderRunSidebarState() {
  const layout = document.querySelector('.run-layout');
  const panel = document.getElementById('run-list-panel');
  if (layout) layout.classList.toggle('is-collapsed', runSidebarCollapsed);
  if (panel) panel.classList.toggle('is-collapsed', runSidebarCollapsed);
  renderRunSummary();
}

function toggleRunSidebar() {
  runSidebarCollapsed = !runSidebarCollapsed;
  renderRunSidebarState();
}

function runSourceRows(summary) {
  const scope = (summary || {}).scope || {};
  const sources = Array.isArray(scope.source_batches) ? scope.source_batches : [];
  const versions = summary.required_city_versions || {};
  if (!sources.length) {
    return `<div class="run-source-row"><b>Scope</b><span>${esc(scope.city || 'all_cities')}</span><span>${esc(scope.task_type || 'all')}</span></div>`;
  }
  const grouped = new Map();
  for (const source of sources) {
    const city = source.city_id || scope.city || 'unknown';
    const item = grouped.get(city) || { types: new Set(), batches: new Set() };
    if (source.task_type) item.types.add(source.task_type);
    if (source.batch_id) item.batches.add(source.batch_id);
    grouped.set(city, item);
  }
  return [...grouped.entries()].map(([city, item]) => {
    const cityLabel = formatRunCityVersion(city, versions[city]);
    const types = [...item.types].map(type => `<span class="run-badge">${esc(type)}</span>`).join('');
    const batches = [...item.batches].join(', ');
    return `<div class="run-source-row"><b>${esc(cityLabel)}</b>${types}` +
      `${batches ? `<small><span data-ui-text>批次</span> ${esc(batches)}</small>` : ''}</div>`;
  }).join('');
}

async function loadRegionRuleCities() {
  try {
    const response = await fetch('/api/region/rule-cities');
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    regionRuleCities = (await response.json()).cities || [];
    document.getElementById('task-detail').innerHTML = '<div class="placeholder" data-ui-text>← 从左侧选择一个城市查看地域准则</div>';
    renderRegionRuleCities();
    showGenPanel();
  } catch (error) {
    console.error('Failed to load Region rule cities', error);
    regionRuleCities = [];
    renderRegionRuleCities();
  }
}

function renderRegionRuleCities() {
  const list = document.getElementById('task-items');
  document.querySelector('#task-list .sidebar-header h2').textContent = '城市地域准则';
  document.getElementById('task-count').textContent = GeoUI.t('共 {count} 个城市', {count: regionRuleCities.length});
  const workspaceToggle = `<div class="region-workspace-toggle" role="tablist" aria-label="城市地域准则工作模式">
    <button type="button" role="tab" aria-selected="${regionRuleWorkspace === 'review'}" class="${regionRuleWorkspace === 'review' ? 'active' : ''}" onclick="setRegionRuleWorkspace('review')" data-ui-text>审核</button>
    <button type="button" role="tab" aria-selected="${regionRuleWorkspace === 'build'}" class="${regionRuleWorkspace === 'build' ? 'active' : ''}" onclick="setRegionRuleWorkspace('build')" data-ui-text>新建</button>
  </div>`;
  const cards = regionRuleCities.map(city => `
    <div class="task-item region-city-item" data-city-id="${escAttr(city.city_id)}" onclick="selectRegionRuleCity(this.dataset.cityId)">
      <div class="region-city-name">${esc(city.city_name)}</div>
      <div class="region-city-count" data-ui-output>地域准则：${Number(city.rule_count) || 0} 条</div>
    </div>`).join('');
  const body = regionRuleWorkspace === 'review'
    ? (cards || '<div class="task-empty" data-ui-text>当前没有可审批的城市地域准则。</div>')
    : '<div class="region-workspace-hint" data-ui-text>请在右侧选择需要构建地域准则的城市。</div>';
  list.innerHTML = workspaceToggle + body;
}

async function setRegionRuleWorkspace(workspace) {
  if (!['review', 'build'].includes(workspace) || regionRuleWorkspace === workspace) return;
  regionRuleWorkspace = workspace;
  if (workspace === 'review' && regionBuildPollTimer) {
    clearInterval(regionBuildPollTimer);
    regionBuildPollTimer = null;
  }
  renderRegionRuleCities();
  if (workspace === 'build') {
    await openRegionBuildPanel();
  } else {
    document.getElementById('task-detail').innerHTML = '<div class="placeholder" data-ui-text>← 从左侧选择一个城市查看地域准则</div>';
  }
}

async function selectRegionRuleCity(cityId) {
  if (regionBuildPollTimer) clearInterval(regionBuildPollTimer);
  regionRuleWorkspace = 'review';
  document.querySelectorAll('.region-city-item').forEach(el => el.classList.toggle('active', el.dataset.cityId === cityId));
  regionRuleStatusFilter = '';
  try {
    const response = await fetch(`/api/region/rules?city=${encodeURIComponent(cityId)}`);
    if (!response.ok) throw new Error((await response.json()).error || `HTTP ${response.status}`);
    regionRulesData = await response.json();
    renderRegionRulesDetail();
  } catch (error) {
    document.getElementById('task-detail').innerHTML = `<div class="section"><div class="section-title" data-ui-text>加载失败</div><div class="prompt-box">${esc(error.message || error)}</div></div>`;
  }
}

async function openRegionBuildPanel() {
  regionRuleWorkspace = 'build';
  document.querySelectorAll('.region-city-item').forEach(el => el.classList.remove('active'));
  const detail = document.getElementById('task-detail');
  detail.classList.remove('region-review-content');
  detail.innerHTML = '<div class="section"><div class="section-title" data-ui-text>构建城市地域准则</div><div class="muted" data-ui-text>正在读取当前世界的城市与构建状态……</div></div>';
  try {
    const response = await fetch('/api/region/build/config');
    if (!response.ok) throw new Error((await response.json()).error || `HTTP ${response.status}`);
    regionBuildConfig = await response.json();
    const candidates = (regionBuildConfig.cities || []).filter(city => !city.built);
    regionBuildSelectedCities = new Set([...regionBuildSelectedCities].filter(cityId => candidates.some(city => city.city_id === cityId)));
    renderRegionBuildPanel();
  } catch (error) {
    detail.innerHTML = `<div class="section"><div class="section-title" data-ui-text>构建城市地域准则</div><div class="prompt-box">${esc(error.message || error)}</div></div>`;
  }
}

function renderRegionBuildPanel() {
  const candidates = (regionBuildConfig?.cities || []).filter(city => !city.built);
  const textModelItems = regionBuildConfig?.text_models || regionBuildConfig?.models || [];
  const visionModelItems = regionBuildConfig?.vision_models || [];
  const cityOptionsHtml = candidates.map(item => `<label class="region-build-city-option"><input type="checkbox" value="${escAttr(item.city_id)}" ${regionBuildSelectedCities.has(item.city_id) ? 'checked' : ''} onchange="toggleRegionBuildCity(this.value, this.checked)"><span>${esc(item.city_name)} · v${esc(item.active_version || 1)}</span></label>`).join('');
  if (!regionBuildTextModel) regionBuildTextModel = textModelItems.some(item => item.endpoint_key === DEFAULT_GENERATION_MODEL_KEY) ? DEFAULT_GENERATION_MODEL_KEY : (textModelItems[0]?.endpoint_key || '');
  if (!regionBuildVisionModel) regionBuildVisionModel = regionBuildConfig?.default_vision_model || visionModelItems[0]?.endpoint_key || '';
  const textModelOptionsHtml = textModelItems.map(item => `<option value="${escAttr(item.endpoint_key)}" ${item.endpoint_key === regionBuildTextModel ? 'selected' : ''}>${esc(item.label || item.model || item.endpoint_key)}</option>`).join('');
  const visionModelOptionsHtml = visionModelItems.map(item => `<option value="${escAttr(item.endpoint_key)}" ${item.endpoint_key === regionBuildVisionModel ? 'selected' : ''}>${esc(item.label || item.model || item.endpoint_key)}</option>`).join('');
  const selected = candidates.filter(item => regionBuildSelectedCities.has(item.city_id));
  document.getElementById('task-detail').innerHTML = `<div class="section region-build-section"><div class="section-title" data-ui-text>构建城市地域准则</div>
    <div class="region-edit-note" data-ui-text>一次启动后按城市顺序完成 Region Stage 1 → Stage 2，不生成 Stage 3 任务。城市名称中的版本是 Stage 2 自动使用的当前激活版本。</div>
    ${candidates.length ? `<div class="region-build-form"><div class="region-field"><span data-ui-text>目标城市（可多选）</span><div class="region-build-city-checklist">${cityOptionsHtml}</div></div><div class="region-build-model-grid"><label class="region-field" data-ui-text>文本模型<select id="region-build-text-model">${textModelOptionsHtml}</select><small data-ui-text>用于 Stage 1 文字内容与 Stage 2 准则评估</small></label><label class="region-field" data-ui-text>图像模型<select id="region-build-vision-model">${visionModelOptionsHtml}</select><small data-ui-text>用于 Stage 1 含图片内容</small></label></div><div id="region-build-state"></div><div class="region-review-actions"><button class="save-btn" ${selected.length ? '' : 'disabled'} onclick="startRegionBuild()" data-ui-text>开始构建地域准则</button><button id="region-build-cancel" class="region-cancel-btn" disabled onclick="cancelRegionBuild()" data-ui-text>中断构建</button></div></div>` : '<div class="task-empty" data-ui-text>当前世界中的城市均已有完整 Region 准则资产。</div>'}
  </div>`;
  if (selected.length) refreshRegionBuildStatuses();
}

function captureRegionBuildInputs() {
  regionBuildTextModel = document.getElementById('region-build-text-model')?.value || regionBuildTextModel;
  regionBuildVisionModel = document.getElementById('region-build-vision-model')?.value || regionBuildVisionModel;
}

function toggleRegionBuildCity(cityId, checked) {
  captureRegionBuildInputs();
  if (checked) regionBuildSelectedCities.add(cityId); else regionBuildSelectedCities.delete(cityId);
  renderRegionBuildPanel();
}

async function refreshRegionBuildStatuses() {
  if (regionBuildStatusRefreshInFlight) return;
  regionBuildStatusRefreshInFlight = true;
  const cityIds = [...regionBuildSelectedCities];
  regionBuildStatusResults = new Map([...regionBuildStatusResults].filter(([cityId]) => cityIds.includes(cityId)));
  const refreshOne = async cityId => {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 5000);
    try {
      const response = await fetch(`/api/region/build/status?city=${encodeURIComponent(cityId)}`, {signal:controller.signal});
      const result = await response.json();
      regionBuildStatusResults.set(cityId, response.ok
        ? result.build
        : {city_id:cityId, status:'failed', error:result.error || `HTTP ${response.status}`});
    } catch (error) {
      regionBuildStatusResults.set(cityId, {
        city_id:cityId,
        status:'failed',
        error:error?.name === 'AbortError' ? '构建状态读取超时，请稍后重试。' : `构建状态读取失败：${error?.message || error}`,
      });
    } finally {
      clearTimeout(timeout);
      renderRegionBuildStates(cityIds.map(id => regionBuildStatusResults.get(id)).filter(Boolean));
    }
  };
  try {
    await Promise.allSettled(cityIds.map(refreshOne));
  } finally {
    regionBuildStatusRefreshInFlight = false;
  }
}

function regionBuildModelLabel(endpointKey) {
  const key = String(endpointKey || '').trim();
  if (!key) return '';
  const items = [
    ...(regionBuildConfig?.text_models || regionBuildConfig?.models || []),
    ...(regionBuildConfig?.vision_models || []),
  ];
  const item = items.find(candidate => String(candidate.endpoint_key || '') === key);
  return String(item?.label || item?.model || key);
}

function renderRegionBuildModelSummary(builds) {
  const configurations = new Map();
  builds.forEach(build => {
    const textKey = String(build.text_model || '').trim();
    const visionKey = String(build.vision_model || '').trim();
    if (!textKey && !visionKey) return;
    configurations.set(`${textKey}\u0000${visionKey}`, {textKey, visionKey});
  });
  if (!configurations.size) return '';
  if (configurations.size > 1) return '多个模型配置';

  const {textKey, visionKey} = configurations.values().next().value;
  const textLabel = regionBuildModelLabel(textKey);
  const visionLabel = regionBuildModelLabel(visionKey);
  if (textLabel && visionLabel && textLabel === visionLabel) return `文本 / 图像模型：${textLabel}`;
  if (textLabel && visionLabel) return `文本：${textLabel} · 图像：${visionLabel}`;
  if (textLabel) return `文本模型：${textLabel}`;
  return `图像模型：${visionLabel}`;
}

function renderRegionBuildStates(builds) {
  const target = document.getElementById('region-build-state');
  if (!target) return;
  const cityMap = new Map((regionBuildConfig?.cities || []).map(city => [city.city_id, city]));
  const running = builds.some(build => ['queued','running','retry_wait'].includes(build.status));
  const cancelButton = document.getElementById('region-build-cancel');
  if (cancelButton) cancelButton.disabled = !running;
  const completedCities = builds.filter(build => build.phase === 'stage2_complete').length;
  const modelSummary = renderRegionBuildModelSummary(builds);
  target.innerHTML = builds.length ? `<div class="region-build-progress-heading"><strong data-ui-text>构建进度</strong><span>${modelSummary ? `${esc(modelSummary)} · ` : ''}已完成 ${completedCities} / ${builds.length} 个城市</span></div>${builds.map((build, index) => renderRegionBuildProgressCard(build, cityMap.get(build.city_id) || {}, index)).join('')}` : '';
  if (running && !regionBuildPollTimer) regionBuildPollTimer = setInterval(refreshRegionBuildStatuses, 1000);
  if (!running && regionBuildPollTimer) { clearInterval(regionBuildPollTimer); regionBuildPollTimer = null; }
  const allComplete = builds.length && builds.every(build => build.phase === 'stage2_complete');
  if (allComplete) {
    regionRuleWorkspace = 'review';
    loadRegionRuleCities();
  }
}

function renderRegionBuildProgressCard(build, city, index) {
  const statusLabel = {not_started:'尚未开始',queued:'等待中',running:'正在构建',retry_wait:`${Number(build.retry_remaining) || 0} 秒后重试`,interrupted:'可继续',failed:'构建失败',completed:'已完成'}[build.status] || build.status;
  const cityName = city.city_name || build.city_name || build.city_id;
  const cityVersion = city.active_version || String(build.city_version || '1').replace(/^v/, '');
  if (build.status === 'not_started' || build.status === 'queued') {
    return `<div class="region-build-status queued"><div class="region-build-status-head"><strong>${esc(cityName)} · v${esc(cityVersion)}</strong><span class="region-build-status-label" data-ui-output>${esc(statusLabel)} · 第 ${index + 1} 个</span></div></div>`;
  }
  return `<div class="region-build-status ${escAttr(build.status || '')}"><div class="region-build-status-head"><strong>${esc(cityName)} · v${esc(cityVersion)}</strong><span class="region-build-status-label" data-ui-output>${esc(statusLabel)}</span></div>${renderRegionBuildStage(build, 1)}${renderRegionBuildStage(build, 2)}${build.error?`<div class="region-build-error" data-ui-output>${esc(build.error)}</div>`:''}</div>`;
}

const REGION_BUILD_PHASES = [
  ['source_read', '来源读取'], ['source_parse', '来源解析'], ['media_fetch', '图片准备'],
  ['stage1_extraction', '准则提取'], ['type_catalog', '类型目录'],
  ['stage2_assessment', '准则评估'], ['asset_generation', '资产生成'],
  ['quality_validation', '质量校验'], ['formal_promotion', '正式入库'],
];

function regionBuildPhaseIndex(build) {
  const phase = String(build.phase || '');
  if (phase === 'stage2_complete') return REGION_BUILD_PHASES.length;
  if (phase === 'stage1_complete' || phase === 'stage2_queued') return 4;
  if (phase === 'stage1_queued') return 0;
  const index = REGION_BUILD_PHASES.findIndex(([id]) => id === phase);
  return Math.max(0, index);
}

function renderRegionBuildStage(build, stageNumber) {
  const current = regionBuildPhaseIndex(build);
  const allComplete = build.phase === 'stage2_complete';
  const start = stageNumber === 1 ? 0 : 4;
  const end = stageNumber === 1 ? 4 : REGION_BUILD_PHASES.length;
  const phases = REGION_BUILD_PHASES.slice(start, end);
  const stageComplete = stageNumber === 1 ? current >= 4 : allComplete;
  const stageActive = stageNumber === 1 ? current < 4 : current >= 4 && !allComplete;
  const title = stageNumber === 1 ? 'Stage 1 · 来源构建' : 'Stage 2 · 准则评估与入库';
  const summaryCount = stageNumber === 1
    ? Number(build.stage1_rule_count) || 0
    : Number(build.stage2_total) || Number(build.stage1_rule_count) || 0;
  const summary = stageComplete
    ? stageNumber === 1
      ? `结构化地域准则：${summaryCount} 条`
      : `准则评估结果：${summaryCount} 条 · 已正式入库`
    : '';
  const activeSummary = stageActive && stageNumber === 1 && String(build.phase || '') === 'stage1_extraction'
    ? `准则提取进度：${Number(build.completed) || 0} / ${Number(build.total) || 0} 个来源切片`
    : stageActive && stageNumber === 2 && String(build.phase || '') === 'stage2_assessment'
      ? `准则评估进度：${Number(build.completed) || 0} / ${Number(build.total) || 0} 条`
      : '';
  const usageSummary = renderRegionBuildUsageSummary(build, stageNumber, summary || activeSummary);
  return `<div class="region-build-stage-group ${stageActive ? 'active' : stageComplete ? 'complete' : 'pending'}"><div class="region-build-stage-title" data-ui-output>${esc(title)}</div><div class="region-build-phase-strip stage-${stageNumber}">${phases.map(([id, label], localIndex) => {
    const index = start + localIndex;
    let state = allComplete || index < current ? 'complete' : index === current ? 'current' : 'pending';
    if (index === current && build.status === 'failed') state = 'failed';
    if (index === current && ['interrupted', 'retry_wait'].includes(build.status)) state = 'paused';
    return `<div class="region-build-phase ${state}" data-phase="${escAttr(id)}" data-ui-output>${esc(label)}</div>`;
  }).join('')}</div>${stageActive ? renderRegionBuildCurrentStep(build, stageNumber) : ''}${usageSummary}</div>`;
}

function renderRegionBuildUsageSummary(build, stageNumber, summary) {
  const usage = (build.stage_usage || {})[`stage${stageNumber}`] || {};
  const requests = Number(usage.requests) || 0;
  if (!summary || !requests) return summary ? `<div class="region-build-stage-summary"><span data-ui-output>${esc(summary)}</span></div>` : '';
  const prompt = Number(usage.prompt_tokens) || 0;
  const completion = Number(usage.completion_tokens) || 0;
  const total = Number(usage.total_tokens) || prompt + completion;
  return `<div class="region-build-stage-summary"><span data-ui-output>${esc(summary)}</span><span class="region-build-usage"><b data-ui-output>总 Token：${formatTokenNumber(total)}</b><span data-ui-output>输入：${formatTokenNumber(prompt)}</span><span data-ui-output>输出：${formatTokenNumber(completion)}</span><span data-ui-output>模型耗时：${formatGenerationDuration(Number(usage.elapsed_s) || 0)}</span></span></div>`;
}

function renderRegionBuildCurrentStep(build, stageNumber) {
  const phase = String(build.phase || '');
  const phaseLabel = Object.fromEntries(REGION_BUILD_PHASES)[phase] || (phase === 'stage2_complete' ? '正式入库' : '来源读取');
  let completed = Number(build.completed) || 0;
  let total = Number(build.total) || 0;
  let detail = `${phaseLabel}：正在准备`;
  let secondary = '';
  if (phase === 'source_read') detail = `来源读取：正在读取 ${build.page_title || build.city_name || '城市'} 的 Wikivoyage 页面`;
  if (phase === 'source_parse') detail = '来源解析：正在生成结构化来源与模型请求切片';
  if (phase === 'media_fetch') {
    completed = Number(build.media_completed ?? build.completed) || 0;
    total = Number(build.media_total ?? build.total) || 0;
    const currentIndex = Number(build.current_index) || Math.min(total, completed + 1);
    const innerRemaining = Number(build.internal_retry_remaining) || 0;
    if (build.status === 'retry_wait') {
      detail = '图片准备：Wikimedia 暂时限制图片下载';
      secondary = `图片：${build.retry_file || build.current_item || '未知'} · ${Number(build.retry_remaining) || 0} 秒后自动重新尝试，已下载图片不会重复下载`;
    } else if (innerRemaining || build.internal_retry_attempt) {
      detail = '图片准备：Wikimedia 请求受限';
      secondary = `图片：${build.current_item || '未知'} · 第 ${Number(build.internal_retry_attempt) || 1} / ${Number(build.internal_retry_max) || 4} 次重试 · ${innerRemaining} 秒后继续`;
    } else {
      detail = `图片准备：正在下载第 ${currentIndex} / ${total || '?'} 张图片`;
      secondary = build.current_item || '';
    }
  }
  if (phase === 'stage1_extraction') {
    if (build.status === 'retry_wait' && build.error_code === 'model_infrastructure_unavailable') {
      const timeout = build.model_failure_reason === 'timeout';
      detail = timeout ? '准则提取：当前来源切片模型响应超时' : '准则提取：模型服务连接暂时不可用';
      secondary = `切片：${build.retry_item || build.current_item || '未知'} · 第 ${Number(build.model_retry_attempt) || 1} / 5 轮恢复 · ${Number(build.retry_remaining) || 0} 秒后自动继续，已有提取结果不会重复请求`;
    } else if (Number(build.internal_model_retry_remaining) > 0) {
      detail = '准则提取：当前模型请求正在重试';
      secondary = `切片：${build.model_retry_item || build.current_item || '未知'} · 第 ${Number(build.internal_model_retry_attempt) || 2} / ${Number(build.internal_model_retry_max) || 3} 次请求 · ${Number(build.internal_model_retry_remaining) || 0} 秒后继续`;
    } else {
      detail = `准则提取：正在处理第 ${Math.min(total, completed + 1)} / ${total || '?'} 个来源切片`;
    }
  }
  if (phase === 'type_catalog') detail = `类型目录：正在读取 ${build.city_name || '当前城市'} · ${build.city_version || ''} 的 POI 类型`;
  if (phase === 'stage2_assessment') {
    const currentItem = String(build.current_item || '');
    total = Number(build.current_total) || total;
    if (Number(build.current_index) > 0) completed = Math.max(completed, Number(build.current_index) - 1);
    const waitingSeconds = Math.max(0, Math.floor((Date.now() - new Date(build.updated_at || Date.now()).getTime()) / 1000));
    if (build.status === 'retry_wait' && build.error_code === 'model_infrastructure_unavailable') {
      detail = '准则评估：模型服务连接暂时不可用';
      secondary = `条目：${build.retry_item || currentItem || '未知'} · 第 ${Number(build.model_retry_attempt) || 1} / 5 轮恢复 · ${Number(build.retry_remaining) || 0} 秒后自动继续，已有评估不会重复请求`;
    } else if (Number(build.internal_model_retry_remaining) > 0) {
      detail = '准则评估：当前模型请求正在重试';
      secondary = `条目：${build.model_retry_item || currentItem || '未知'} · 第 ${Number(build.internal_model_retry_attempt) || 2} / ${Number(build.internal_model_retry_max) || 3} 次请求 · ${Number(build.internal_model_retry_remaining) || 0} 秒后继续`;
    } else if (currentItem.startsWith('恢复已有评估 · ')) {
      detail = `准则评估：正在恢复已有结果 ${Number(build.current_index) || Math.min(total, completed + 1)} / ${Number(build.current_total) || total || '?'}`;
      secondary = currentItem.replace(/^恢复已有评估 · /, '');
    } else if (currentItem.startsWith('模型请求 · ')) {
      detail = `准则评估：正在请求模型处理第 ${Number(build.current_index) || Math.min(total, completed + 1)} / ${Number(build.current_total) || total || '?'} 条`;
      secondary = `${currentItem.replace(/^模型请求 · /, '')} · 已等待 ${waitingSeconds} 秒`;
    } else {
      detail = `准则评估：正在处理第 ${Math.min(total, completed + 1)} / ${total || '?'} 条地域准则`;
    }
  }
  if (phase === 'asset_generation') detail = '资产生成：正在汇总地域准则评估结果';
  if (phase === 'quality_validation') detail = '质量校验：正在检查资产格式和 Stage 2 Rule ID 覆盖关系';
  if (phase === 'formal_promotion') detail = '正式入库：正在保存城市地域准则';
  if (phase === 'stage2_complete') detail = `正式入库：${Number(build.stage2_total) || Number(build.stage1_rule_count) || 0} 条地域准则已进入人工审核列表`;
  if (build.status === 'interrupted') secondary = '构建已中断，可从当前环节继续';
  const percent = total ? Math.min(100, Math.round(completed * 100 / total)) : 0;
  const measurable = ['media_fetch', 'stage1_extraction', 'stage2_assessment'].includes(phase) && total > 0;
  return `<div class="region-build-current-step stage-${stageNumber}"><div><span data-ui-output>当前：${esc(detail.replace(/^[^：]+：/, ''))}</span>${measurable ? `<small>${completed} / ${total} · ${percent}%</small>` : ''}</div>${secondary ? `<div class="region-build-current-item" data-ui-output>${esc(secondary)}</div>` : ''}${measurable ? `<div class="region-progress-track"><span style="width:${percent}%"></span></div>` : ''}</div>`;
}

async function startRegionBuild() {
  captureRegionBuildInputs();
  const payload = {cities:[...regionBuildSelectedCities].map(city_id => ({city_id})), text_model:regionBuildTextModel || DEFAULT_GENERATION_MODEL_KEY, vision_model:regionBuildVisionModel};
  const response = await fetch('/api/region/build/start', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
  const result = await response.json();
  if (!response.ok) { alert(result.error || '启动失败'); return; }
  refreshRegionBuildStatuses();
}

async function cancelRegionBuild() {
  const button = document.getElementById('region-build-cancel');
  if (button) button.disabled = true;
  const response = await fetch('/api/region/build/cancel', {method:'POST'});
  const result = await response.json();
  if (!response.ok) {
    if (button) button.disabled = false;
    alert(result.error || '中断失败');
    return;
  }
  await refreshRegionBuildStatuses();
}

function renderRegionRulesDetail() {
  const data = regionRulesData || {};
  const rules = data.rules || [];
  const visibleRules = rules.map((row, index) => ({row, index})).filter(({row}) => {
    const reviewStatus = row.assessment?.human_review?.status || 'pending';
    const stage2Status = row.assessment?.status || '';
    if (regionRuleStatusFilter === 'source_only') return stage2Status === 'source_only';
    if (regionRuleStatusFilter === 'eligible') return stage2Status === 'task_source_eligible';
    if (regionRuleStatusFilter === 'needs_split') return stage2Status === 'needs_stage1_split';
    if (regionRuleStatusFilter === 'pending') return reviewStatus === 'pending';
    if (regionRuleStatusFilter === 'reviewed') return reviewStatus !== 'pending';
    return true;
  });
  const first = visibleRules[0] || null;
  const detail = document.getElementById('task-detail');
  detail.classList.add('region-review-content');
  detail.innerHTML = `
    <div class="section region-review-section"><div class="section-title" data-ui-output>${esc(data.city_name || data.city_id || '')} · 地域准则审核</div>
      <div class="region-rules-layout"><div class="region-rule-pane"><div class="region-rule-list-toolbar"><span data-ui-output>地域准则列表 · 共 ${visibleRules.length} 条</span><select onchange="setRegionRuleStatusFilter(this.value)"><option value="" ${regionRuleStatusFilter === '' ? 'selected' : ''} data-ui-text>全部</option><option value="source_only" ${regionRuleStatusFilter === 'source_only' ? 'selected' : ''} data-ui-text>仅保留</option><option value="eligible" ${regionRuleStatusFilter === 'eligible' ? 'selected' : ''} data-ui-text>可出题</option><option value="needs_split" ${regionRuleStatusFilter === 'needs_split' ? 'selected' : ''} data-ui-text>需拆分</option><option value="pending" ${regionRuleStatusFilter === 'pending' ? 'selected' : ''} data-ui-text>待审核</option><option value="reviewed" ${regionRuleStatusFilter === 'reviewed' ? 'selected' : ''} data-ui-text>已审核</option></select></div><div class="region-rule-list">${visibleRules.map(({row, index}, visibleIndex) => {
        const review = row.assessment?.human_review?.status || 'pending';
        const reviewLabel = {pending: '待人工审核', approved: '已通过', rejected: '已拒绝', needs_fix: '需修改'}[review] || review;
        const eligibility = {task_source_eligible: '可用于出题', source_only: '仅保留来源', needs_stage1_split: '需要拆分', not_bindable_in_current_map: '当前地图不可绑定'}[row.assessment?.status] || '未判断';
        const text = row.rule?.statement || row.rule?.summary || row.rule?.subject || row.rule?.rule_id || '';
        return `<button class="region-rule-row status-${escAttr(review)} ${visibleIndex === 0 ? 'active' : ''}" data-rule-index="${index}" onclick="selectRegionRule(${index})"><div class="region-rule-card-meta"><span class="region-review-pill ${escAttr(review)}" data-ui-output>${esc(reviewLabel)}</span><span class="region-eligibility ${eligibility === '可用于出题' ? 'eligible' : ''}" data-ui-output>${esc(eligibility)}</span></div><div class="region-rule-card-text">${esc(text)}</div></button>`;
      }).join('') || '<div class="task-empty" data-ui-text>当前筛选条件下没有地域准则。</div>'}</div></div><div id="region-rule-inspector"></div></div>
    </div>`;
  if (first) selectRegionRule(first.index);
}

function setRegionRuleStatusFilter(status) {
  regionRuleStatusFilter = status;
  renderRegionRulesDetail();
}

function selectRegionRule(index) {
  document.querySelectorAll('.region-rule-row').forEach(el => el.classList.toggle('active', Number(el.dataset.ruleIndex) === index));
  const row = (regionRulesData?.rules || [])[index];
  if (!row) return;
  const rule = row.rule || {}, assessment = row.assessment || {}, review = assessment.human_review || {};
  const checks = assessment.checks || {};
  const restrictions = Array.isArray(assessment.independent_restrictions) ? assessment.independent_restrictions : [];
  const selectedTypes = new Set(assessment.affected_poi_primary_types || []);
  const typeOptions = (regionRulesData?.poi_type_catalog || []).map(type =>
    `<label class="region-type-option" data-type-search="${escAttr(type.toLowerCase())}"><input type="checkbox" value="${escAttr(type)}" ${selectedTypes.has(type) ? 'checked' : ''} onchange="updateRegionTypeSelection()"><span>${esc(type)}</span></label>`
  ).join('');
  const evidence = (row.evidence || []).map(item => {
    const original = item.raw?.source_text || item.raw?.caption_source_text || '';
    const readable = cleanRegionEvidenceText(original);
    return `<details class="region-evidence"><summary data-ui-text>Wikivoyage 来源正文（只读）</summary><pre>${esc(readable || original)}</pre><details class="region-raw-wikitext"><summary data-ui-text>查看完整原始 Wikitext</summary><pre>${esc(original)}</pre></details></details>`;
  }).join('');
  document.getElementById('region-rule-inspector').innerHTML = `
    <div class="region-review-heading"><h3>${esc(rule.statement || '')}</h3><span class="region-review-pill ${escAttr(review.status || 'pending')}" data-ui-output>${esc({pending: '待人工审核', approved: '已通过', rejected: '已拒绝', needs_fix: '需修改'}[review.status || 'pending'] || review.status || 'pending')}</span></div>
    <div class="region-edit-note" data-ui-text>当前使用 regional_rules.v3 与 Stage 2 assessment v4。人工保存形成覆盖层；Wikivoyage 原始内容保持只读。</div>
    <div class="section-title" data-ui-text>地域准则</div>
    <label class="region-field" data-ui-text>完整准则<textarea id="region-rule-statement" rows="5">${esc(rule.statement || '')}</textarea></label>
    <div class="region-form-grid"><label data-ui-text>Stage 1 来源状态<select id="region-extraction-status"><option value="supported" ${rule.extraction_status === 'supported' ? 'selected' : ''} data-ui-text>来源支持充分</option><option value="ambiguous" ${rule.extraction_status === 'ambiguous' ? 'selected' : ''} data-ui-text>来源存在歧义</option><option value="incomplete" ${rule.extraction_status === 'incomplete' ? 'selected' : ''} data-ui-text>来源不完整</option></select></label><label data-ui-text>Stage 2 原子性<select id="region-atomicity" onchange="updateRegionConditionalFields(true)"><option value="atomic" ${assessment.atomicity === 'atomic' ? 'selected' : ''} data-ui-text>单一独立准则</option><option value="needs_split" ${assessment.atomicity === 'needs_split' ? 'selected' : ''} data-ui-text>需要拆分</option></select></label></div>
    <label id="region-split-field" class="region-field" data-ui-text>拆分建议（每行一条）<textarea id="region-independent-restrictions" rows="4">${esc(restrictions.join('\n'))}</textarea></label>
    <div id="region-stage2-checks"><div class="section-title" data-ui-text>Stage 2 判断</div>
    <div class="region-form-grid region-check-grid">
      ${regionBooleanSelect('region-check-regional', '是否属于城市或地域事实', checks.regional_fact)}
      ${regionBooleanSelect('region-check-scope', '适用对象或适用情形是否明确', checks.has_applicability_scope)}
      ${regionBooleanSelect('region-check-choice', '是否影响城市内部真实地点选择', checks.can_distinguish_real_choices)}
    </div></div>
    <div class="section-title" data-ui-text>出题类型绑定与结论</div>
    <div class="region-form-grid"><label data-ui-text>出题来源结论<select id="region-assessment-status" onchange="updateRegionConditionalFields(false)"><option value="task_source_eligible" ${assessment.status === 'task_source_eligible' ? 'selected' : ''} data-ui-text>可作为出题来源</option><option value="source_only" ${assessment.status === 'source_only' ? 'selected' : ''} data-ui-text>仅保留地域来源</option><option value="needs_stage1_split" ${assessment.status === 'needs_stage1_split' ? 'selected' : ''} data-ui-text>需要拆分</option><option value="not_bindable_in_current_map" ${assessment.status === 'not_bindable_in_current_map' ? 'selected' : ''} data-ui-text>当前地图中无法绑定</option></select></label></div>
    <input id="region-review-status" type="hidden" value="${escAttr(review.status || 'pending')}"><div class="region-review-controls"><span data-ui-text>人工审核状态</span><div class="review-bar"><button class="review-btn ${(review.status || 'pending') === 'pending' ? 'active' : ''}" onclick="chooseRegionReviewStatus('pending', this)" data-ui-text>待人工审核</button><button class="review-btn ${(review.status || '') === 'approved' ? 'active' : ''}" onclick="chooseRegionReviewStatus('approved', this)" data-ui-text>通过</button><button class="review-btn ${(review.status || '') === 'needs_fix' ? 'active' : ''}" onclick="chooseRegionReviewStatus('needs_fix', this)" data-ui-text>需修改</button><button class="review-btn ${(review.status || '') === 'rejected' ? 'active' : ''}" onclick="chooseRegionReviewStatus('rejected', this)" data-ui-text>拒绝</button></div></div>
    <div id="region-binding-fields"><div class="region-field"><span data-ui-text>关联 POI 类型</span><div id="region-selected-types" class="region-selected-types"></div><input id="region-type-search" class="region-type-search" type="search" placeholder="搜索 POI 类型…" data-ui-placeholder data-ui-placeholder oninput="filterRegionTypes(this.value)"><div id="region-assessment-types" class="region-type-checklist">${typeOptions}</div></div>
    <label class="region-field" data-ui-text>POI 检索词（每行一条）<textarea id="region-assessment-phrases" rows="4">${esc((assessment.poi_retrieval_phrases || []).join('\n'))}</textarea></label></div><label class="region-field" data-ui-text>Stage 2 判断理由<textarea id="region-assessment-reason" rows="4">${esc(assessment.reason || '')}</textarea></label><label class="region-field" data-ui-text>人工审核备注<textarea id="region-review-notes" rows="3">${esc(review.notes || '')}</textarea></label>
    <div class="section-title" data-ui-text>来源证据</div>${evidence || '<div class="muted" data-ui-text>未找到来源证据。</div>'}
    <div class="region-review-actions"><button class="save-btn" onclick="saveRegionRule('${escAttr(rule.rule_id)}')" data-ui-text>保存修订</button></div>`;
  updateRegionTypeSelection();
  updateRegionConditionalFields(false);
}

function updateRegionConditionalFields(fromAtomicity) {
  const atomicity = document.getElementById('region-atomicity')?.value || '';
  const statusSelect = document.getElementById('region-assessment-status');
  if (fromAtomicity && statusSelect) {
    if (atomicity === 'needs_split') statusSelect.value = 'needs_stage1_split';
    else if (statusSelect.value === 'needs_stage1_split') statusSelect.value = 'source_only';
  }
  const needsSplit = atomicity === 'needs_split';
  const eligible = statusSelect?.value === 'task_source_eligible' && !needsSplit;
  document.getElementById('region-split-field')?.classList.toggle('region-hidden', !needsSplit);
  document.getElementById('region-stage2-checks')?.classList.toggle('region-hidden', needsSplit);
  document.getElementById('region-binding-fields')?.classList.toggle('region-hidden', !eligible);
  if (statusSelect) statusSelect.disabled = needsSplit;
}

function filterRegionTypes(query) {
  const normalized = (query || '').trim().toLowerCase();
  document.querySelectorAll('#region-assessment-types .region-type-option').forEach(option => {
    option.classList.toggle('region-hidden', normalized && !option.dataset.typeSearch.includes(normalized));
  });
}

function updateRegionTypeSelection() {
  const container = document.getElementById('region-selected-types');
  if (!container) return;
  const checked = [...document.querySelectorAll('#region-assessment-types input[type="checkbox"]:checked')];
  container.innerHTML = checked.length
    ? `<span class="region-selected-count">已选择 ${checked.length} 项</span>${checked.map(input => `<button type="button" class="region-type-chip" data-type="${escAttr(input.value)}" onclick="removeRegionType(this.dataset.type)">${esc(input.value)} <span>×</span></button>`).join('')}`
    : '<span class="region-selected-empty" data-ui-text>尚未选择 POI 类型</span>';
}

function removeRegionType(type) {
  const input = [...document.querySelectorAll('#region-assessment-types input[type="checkbox"]')].find(item => item.value === type);
  if (input) input.checked = false;
  updateRegionTypeSelection();
}

function regionBooleanSelect(id, label, value) {
  const normalized = value === true ? 'true' : value === false ? 'false' : '';
  return `<label data-ui-text>${esc(label)}<select id="${escAttr(id)}"><option value="" ${normalized === '' ? 'selected' : ''} data-ui-text>未判断</option><option value="true" ${normalized === 'true' ? 'selected' : ''} data-ui-text>是</option><option value="false" ${normalized === 'false' ? 'selected' : ''} data-ui-text>否</option></select></label>`;
}

function cleanRegionEvidenceText(source) {
  return String(source || '')
    .replace(/^\s*\{\{pagebanner\b[^\n]*\}\}\s*/gim, '')
    .replace(/^\s*\{\{printDistricts\s*\}\}\s*/gim, '')
    .replace(/<!--[^]*?-->/g, '')
    .replace(/\[\[[^\]|]+\|([^\]]+)\]\]/g, '$1')
    .replace(/\[\[([^\]]+)\]\]/g, '$1')
    .replace(/'{2,5}/g, '')
    .replace(/<br\s*\/?\s*>/gi, '\n')
    .replace(/<[^>]+>/g, '')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

function chooseRegionReviewStatus(status, button) {
  document.getElementById('region-review-status').value = status;
  button.closest('.review-bar').querySelectorAll('.review-btn').forEach(item => item.classList.toggle('active', item === button));
}

async function saveRegionRule(ruleId) {
  const row = (regionRulesData.rules || []).find(item => item.rule?.rule_id === ruleId);
  if (!row) return;
  const rule = JSON.parse(JSON.stringify(row.rule || {}));
  const assessment = JSON.parse(JSON.stringify(row.assessment || {}));
  const values = id => document.getElementById(id)?.value || '';
  const lineValues = value => value.split(/\r?\n/).map(item => item.trim()).filter(Boolean);
  const boolValue = id => values(id) === 'true' ? true : values(id) === 'false' ? false : null;
  rule.statement = values('region-rule-statement').trim();
  rule.extraction_status = values('region-extraction-status');
  assessment.atomicity = values('region-atomicity');
  assessment.independent_restrictions = assessment.atomicity === 'needs_split' ? lineValues(values('region-independent-restrictions')) : [];
  assessment.checks = assessment.atomicity === 'needs_split' ? null : {
    regional_fact: boolValue('region-check-regional'),
    has_applicability_scope: boolValue('region-check-scope'),
    can_distinguish_real_choices: boolValue('region-check-choice'),
  };
  assessment.status = values('region-assessment-status');
  if (assessment.atomicity === 'needs_split') assessment.status = 'needs_stage1_split';
  const bindingEnabled = assessment.status === 'task_source_eligible' && assessment.atomicity !== 'needs_split';
  assessment.affected_poi_primary_types = bindingEnabled ? [...document.querySelectorAll('#region-assessment-types input[type="checkbox"]:checked')].map(input => input.value) : [];
  assessment.poi_retrieval_phrases = bindingEnabled ? lineValues(values('region-assessment-phrases')) : [];
  assessment.reason = values('region-assessment-reason').trim();
  assessment.human_review = {...(assessment.human_review || {}), status: values('region-review-status'), notes: values('region-review-notes').trim()};
  const response = await fetch('/api/region/rules/update', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({city_id: regionRulesData.city_id, rule_id: ruleId, rule, assessment})});
  const result = await response.json();
  if (!response.ok) { alert(result.error || '保存失败'); return; }
  if (row) {
    row.rule = result.rule;
    row.assessment = result.assessment;
  }
  renderRegionRulesDetail();
}

function formatRunCityVersion(cityId, version) {
  const label = runCityLabel(cityId || 'unknown');
  return version && !new RegExp(`\\bv${version}\\b`, 'i').test(label) ? `${label} v${version}` : label;
}

function renderRunEventBadge(summary) {
  const scope = (summary || {}).scope || {};
  const sources = scope.source_batches || [];
  const dynamic = scope.task_type === 'dynamic_replanning' ||
    sources.some(source => source.task_type === 'dynamic_replanning') ||
    Boolean((summary.tasks_by_type || {}).dynamic_replanning);
  if (!dynamic) return '';
  const labels = {task_only: '仅本题事件', shared_world: '共享世界事件'};
  const label = labels[summary.dynamic_event_mode];
  return label ? `<span class="run-scope-badge">${label}</span>` : '';
}

function renderRunConfiguration(summary) {
  const scope = (summary || {}).scope || {};
  const sources = Array.isArray(scope.source_batches) ? scope.source_batches : [];
  const versions = (summary || {}).required_city_versions || {};
  if (sources.length === 1) {
    const source = sources[0];
    const city = source.city_id || scope.city || 'unknown';
    return `<div class="run-configuration-line">
      <span class="run-scope-badge run-city-badge">${esc(formatRunCityVersion(city, versions[city]))}</span>
      <span class="run-scope-badge run-type-badge" data-ui-output>${esc(runTaskTypeLabel(source.task_type || scope.task_type || 'unknown'))}</span>
      ${renderRunEventBadge(summary)}
      ${source.batch_id ? `<span class="run-configuration-batch"><span data-ui-text>批次</span> ${esc(source.batch_id)}</span>` : ''}
    </div>`;
  }
  if (sources.length > 1) {
    return `<details class="run-source-disclosure" open>
      <summary><span data-ui-text>实际任务配置（${sources.length} 个来源）</span> ${renderRunEventBadge(summary)}</summary>
      <div class="run-source-rows">${runSourceRows(summary)}</div>
    </details>`;
  }
  return `<div class="run-configuration-line">${renderRunEventBadge(summary)}</div>`;
}

async function restoreRunConfiguration() {
  const summary = (runData || {}).summary || {};
  const source = summary.scope || {};
  runScope.taskType = source.task_type === 'all' ? '' : (source.task_type || '');
  runScope.dynamicEventMode = summary.dynamic_event_mode || 'shared_world';
  runScope.city = source.city === 'all_cities' ? '' : (source.city || '');
  runScope.batch = source.batch || 'latest';
  if (runScope.taskType && runScope.city) await loadRunBatchOptions();
  // loadRunBatchOptions initially selects latest; restore a historical batch
  // afterwards when this exact run was pinned to one.
  runScope.batch = source.batch || 'latest';
  await refreshRunScope();
  const batch = document.getElementById('run-batch');
  if (batch) batch.value = runScope.batch;
  const model = document.getElementById('run-model');
  if (model && summary.model_key && [...model.options].some(option => option.value === summary.model_key)) {
    model.value = summary.model_key;
  }
  const limit = document.getElementById('run-task-limit');
  if (limit) limit.value = source.task_limit || '';
  const approved = document.getElementById('run-approved-only');
  if (approved) approved.checked = Boolean(source.approved_only);
  const status = document.getElementById('run-status');
  if (status) { status.setAttribute('data-ui-output', ''); status.textContent = '已恢复此运行的范围与模型配置；确认后可新建独立评测。'; window.GeoUI?.scan(status); }
}

async function deleteRun(runId) {
  if (!confirm(GeoUI.t('确认删除运行 "{runId}" 及其所有数据？', {runId}))) return;
  const resp = await fetch(`/api/run?run=${encodeURIComponent(runId)}`, { method: 'DELETE' });
  const data = await resp.json();
  if (data.ok) {
    if (selectedRunId === runId) {
      selectedRunId = '';
      runData = null;
      document.getElementById('run-summary').innerHTML = '';
      document.getElementById('run-task-list').innerHTML = '';
      document.getElementById('run-comparison').innerHTML = '';
    }
    await loadRuns();
  } else {
    alert(GeoUI.t('删除失败'));
  }
}

async function selectRun(runId, collapseRunList = false) {
  selectedRunId = runId;
  selectedRunTaskId = '';
  runSidebarCollapsed = Boolean(collapseRunList);
  collapsedRunTaskGroups = new Set();
  const resp = await fetch(`/api/run?run=${encodeURIComponent(runId)}`);
  runData = await resp.json();
  renderRunSummary();
  renderRunTaskList();
  document.getElementById('run-comparison').innerHTML = '<div class="judge-empty" data-ui-text>请选择一个任务查看对比。</div>';
  renderRunSidebarState();
  await restoreRunConfiguration();
  await loadRuns();
}

function renderRunSummary() {
  const target = document.getElementById('run-summary');
  if (!target) return;
  if (!selectedRunId || !runData || !runData.summary || !runData.summary.status) {
    target.innerHTML = '';
    return;
  }
  const s = (runData || {}).summary || {};
  const statusTone = s.status === 'completed' ? 'completed' : isRunActive(s) ? 'active' : 'failed';
  let html = `<div class="run-summary-heading">
    <div class="section-title run-status-title ${statusTone}"><span data-ui-text>${isCombinedRun(s) ? '组合运行' : '运行'}</span> · <span data-ui-text>${esc(runStatusLabel(s.status))}</span></div>
    ${runSidebarCollapsed ? '<button class="btn-run-sidebar-expand" type="button" onclick="toggleRunSidebar()" data-ui-text>显示运行列表</button>' : ''}
  </div>`;
  html += renderRunConfiguration(s);
  html += `<div class="run-inline-metrics">
    <span><b>${s.completed_count || 0}/${s.task_count || 0}</b> <span data-ui-text>完成</span></span>
    <span><b>${s.passed || 0}</b> PASS</span>
    <span><b>${formatTokenNumber((s.token_usage || {}).total || 0)}</b> Tokens</span>
    <span><b>${Number(s.elapsed_seconds || 0).toFixed(1)}s</b> <span data-ui-text>耗时</span></span>
  </div>`;
  if (s.error) html += `<div class="save-status err">${esc(s.error)}</div>`;

  document.getElementById('run-summary').innerHTML = html;
}

function runTaskSourceContext(result, summary) {
  const scope = (summary || {}).scope || {};
  const raw = result.raw || {};
  const selected = (scope.selected_tasks || []).find(task => task.task_id === result.task_id) || {};
  const sourceBatch = raw.source_batch_id || raw.source_batch || selected.source_batch || '';
  const sourceTasksDir = raw.source_tasks_dir || selected.source_tasks_dir || '';
  const sources = Array.isArray(scope.source_batches) ? scope.source_batches : [];
  const source = sources.find(item => sourceTasksDir && item.tasks_dir === sourceTasksDir) ||
    sources.find(item => sourceBatch && item.batch_id === sourceBatch && item.task_type === result.task_type) ||
    (sources.length === 1 ? sources[0] : {});
  const city = source.city_id || scope.city || '';
  const version = ((summary || {}).required_city_versions || {})[city] || '';
  return { city, version, batch: source.batch_id || sourceBatch || '', taskType: result.task_type || 'unknown' };
}

function groupedRunTasks() {
  const summary = (runData || {}).summary || {};
  const groups = new Map();
  for (const result of (runData || {}).results || []) {
    const source = runTaskSourceContext(result, summary);
    const key = [source.city, source.version, source.taskType, source.batch].join('|');
    const group = groups.get(key) || { key, source, tasks: [], passed: 0, tokens: 0 };
    group.tasks.push(result);
    group.passed += (result.grade || '').toUpperCase() === 'PASS' ? 1 : 0;
    group.tokens += Number((result.token_usage || {}).total || 0);
    groups.set(key, group);
  }
  const values = [...groups.values()];
  const duplicateCityTypes = new Set();
  for (const group of values) {
    const cityTypeKey = [group.source.city, group.source.version, group.source.taskType].join('|');
    if (values.filter(item => [item.source.city, item.source.version, item.source.taskType].join('|') === cityTypeKey).length > 1) {
      duplicateCityTypes.add(cityTypeKey);
    }
  }
  return values.map(group => ({ ...group, duplicateCityTypes }));
}

function runTaskGroupTitle(group, groupCount) {
  const type = `<span data-ui-text>${esc(runTaskTypeLabel(group.source.taskType))}</span>`;
  if (groupCount === 1) return type;
  const city = group.source.city ? `${runCityLabel(group.source.city)}${group.source.version ? ` v${group.source.version}` : ''}` : '未记录城市';
  const cityTypeKey = [group.source.city, group.source.version, group.source.taskType].join('|');
  const batch = group.duplicateCityTypes.has(cityTypeKey) && group.source.batch ? ` · ${group.source.batch}` : '';
  return `<span${group.source.city ? '' : ' data-ui-text'}>${esc(city)}</span> · ${type}${esc(batch)}`;
}

function englishTaskFolder(folder, fallback) {
  const value = String(folder || fallback || '');
  return /[\u3400-\u9fff]/.test(value)
    ? value.match(/^task_\d+/)?.[0] || String(fallback || value)
    : value;
}

function renderRunTaskTitle(folder, fallback) {
  const value = String(folder || fallback || '');
  const match = value.match(/^(.*_)(餐厅|酒店|咖啡馆|博物馆|公园|画廊|餐馆)$/);
  if (!match) {
    const english = englishTaskFolder(folder, fallback);
    return english === value ? esc(value) : `<span data-ui-text data-ui-en-text="${attr(english)}">${esc(value)}</span>`;
  }
  return `${esc(match[1])}<span data-ui-output>${match[2]}</span>`;
}

function runTaskStatus(result) {
  const raw = result.raw || {};
  if (raw.infrastructure_failure) return { label: '运行错误', tone: 'error' };
  const grade = String(result.grade || '').toUpperCase();
  if (grade === 'PASS') return { label: 'PASS', tone: 'pass' };
  if (grade === 'FAIL') return { label: 'FAIL', tone: 'fail' };
  return { label: '未判定', tone: 'pending' };
}

function toggleRunTaskGroup(groupKey) {
  if (collapsedRunTaskGroups.has(groupKey)) collapsedRunTaskGroups.delete(groupKey);
  else collapsedRunTaskGroups.add(groupKey);
  renderRunTaskList();
}

function renderRunTaskList() {
  const groups = groupedRunTasks();
  let html = '<div class="run-detail-layout">';
  html += '<div class="run-task-groups">';
  for (const group of groups) {
    const collapsed = collapsedRunTaskGroups.has(group.key);
    const rate = group.tasks.length ? Math.round(group.passed * 100 / group.tasks.length) : 0;
    const resultTone = rate >= 70 ? 'pass' : rate >= 40 ? 'warn' : 'fail';
    html += `<section class="run-task-group ${collapsed ? 'is-collapsed' : ''}">
      <button class="run-task-group-header" type="button" onclick="toggleRunTaskGroup('${attr(group.key)}')" aria-expanded="${!collapsed}">
        <span class="run-group-chevron" aria-hidden="true">${collapsed ? '▸' : '▾'}</span>
        <span class="run-group-title" data-ui-output>${runTaskGroupTitle(group, groups.length)}</span>
        <span class="run-group-metrics"><b class="${resultTone}">(${group.passed}/${group.tasks.length}) PASS</b><span class="${resultTone}">${rate}%</span><span>${formatTokenNumber(group.tokens)} tok</span></span>
      </button>`;
    if (!collapsed) {
      html += '<div class="run-task-rows">';
      for (const result of group.tasks) {
        const status = runTaskStatus(result);
        const active = result.task_id === selectedRunTaskId ? 'active' : '';
        html += `<button class="run-task-row ${status.tone} ${active}" type="button" onclick="selectRunTask('${attr(result.task_id)}')">
          <span class="run-task-title">${renderRunTaskTitle(result.task_folder, result.task_id)}</span>
          <span class="run-task-row-meta"><b class="${status.tone}">${status.label}</b><span>${formatTokenNumber((result.token_usage || {}).total || 0)} tok</span></span>
        </button>`;
      }
      html += '</div>';
    }
    html += '</section>';
  }
  html += '</div>';
  html += '<div class="run-task-detail" id="run-task-detail">';
  if (!selectedRunTaskId) html += '<div class="judge-empty" data-ui-text>从左侧选择一个任务查看执行详情。</div>';
  html += '</div></div>';
  document.getElementById('run-task-list').innerHTML = html;
}

async function selectRunTask(taskId) {
  selectedRunTaskId = taskId;
  const taskGroup = groupedRunTasks().find(group => group.tasks.some(task => task.task_id === taskId));
  if (taskGroup) collapsedRunTaskGroups.delete(taskGroup.key);
  renderRunTaskList();
  const resp = await fetch(`/api/run/task?run=${encodeURIComponent(selectedRunId)}&task_id=${encodeURIComponent(taskId)}`);
  const runTask = await resp.json();
  // Also load task data for prompt
  const manifest = runScope.manifest || { tasks: [] };
  const taskEntry = (manifest.tasks || []).find(t => t.task_id === taskId);
  const folder = taskEntry ? (taskEntry.folder_ref || taskEntry.folder) : '';
  let taskData = {task: {user_prompt: (runTask.run_report || {}).user_prompt || ''}};
  if (folder) {
    try {
      const tr = await fetch(`/api/task?folder=${encodeURIComponent(folder)}`);
      taskData = await tr.json();
    } catch(e) {}
  }
  // Render comparison in the right panel
  const detailEl = document.getElementById('run-task-detail');
  if (detailEl) {
    detailEl.innerHTML = renderRunComparison(taskData, runTask);
  } else {
    renderRunComparison(taskData, runTask);
  }
}

function renderAgentRunDetail(taskData, runTask) {
  const report = runTask.run_report || {};
  const outcome = runTask.outcome || {};
  const isDiscovery = (taskData.task?.task_type || outcome.task_type) === 'place_discovery';
  const metrics = (Array.isArray(outcome.metrics) ? outcome.metrics : [])
    .filter(metric => !isDiscovery || metric.metric !== 'judge_sequence');
  const trace = Array.isArray(runTask.trajectory) ? runTask.trajectory : [];
  const grade = outcome.grade || report.grade || 'UNKNOWN';
  const answer = outcome.final_answer ?? report.final_answer;
  const answerHtml = renderAgentAnswer(answer);
  const answerMetric = metrics.find(metric => metric.metric === 'answer_correct');
  const trajectoryMetric = metrics.find(metric => metric.metric === 'judge_sequence');
  const evaluationSummaryHtml = answerMetric ? `
    <div class="judge-report-row ${answerMetric.pass ? 'pass' : 'fail'}"><div><b data-ui-text>答案正确性（主判定）</b><br><span data-ui-text>决定该任务最终 PASS/FAIL</span></div><div>${answerMetric.pass ? 'PASS' : 'FAIL'}</div></div>
    ${trajectoryMetric ? `<div class="judge-report-row ${trajectoryMetric.pass ? 'pass' : 'fail'}"><div><b data-ui-text>轨迹完整性（辅助诊断）</b><br><span data-ui-text>展示搜索、详情查询与候选比较过程，不影响最终判定</span></div><div>${trajectoryMetric.pass ? 'PASS' : 'FAIL'}</div></div>` : ''}
  ` : '';
  const metricHtml = metrics.length ? metrics.map(metric => {
    const statusText = {unverifiable: '无法核验', not_applicable: metric.pass ? '分支通过' : '未通过'}[metric.verification_status];
    const tone = statusText ? '' : (metric.pass ? 'pass' : 'fail');
    return `<div class="judge-report-row ${tone}"><div><b data-ui-output>${esc(metric.label || metric.metric || 'evaluation')}</b><br><span data-ui-output>${esc(metric.detail || '')}</span></div><div data-ui-output>${statusText || (metric.pass ? 'PASS' : 'FAIL')}</div></div>`;
  }).join('') : '<div class="judge-empty" data-ui-text>该评测未产生单项判定明细。</div>';
  const traceHtml = trace.length ? trace.map((event, index) => {
    const tool = event.tool || event.kind || event.action || 'event';
    const args = event.args || event.input || event.payload || {};
    return `<div class="judge-report-row"><div><b>${index + 1}. ${esc(tool)}</b><br><span class="trace-args">${esc(JSON.stringify(args))}</span></div><div>${esc(event.virtual_time || '')}</div></div>`;
  }).join('') : '<div class="judge-empty" data-ui-text>该评测未返回工具调用轨迹。</div>';
  return `<div class="comparison-section">
    <div class="comparison-header"><div><div class="section-title" data-ui-text>Agent 评测详情</div><div class="run-task-meta">${esc(grade)} · ${Number(outcome.passed ?? report.passed ?? 0)}/${Number(outcome.total ?? report.total ?? 0)}</div></div></div>
    ${renderPromptForComparison(taskData)}
    ${renderTokenUsageCard(outcome.token_usage || report.tokens_used || {})}
    ${evaluationSummaryHtml ? `<div class="comparison-card"><div class="comparison-title" data-ui-text>评分口径</div>${evaluationSummaryHtml}</div>` : ''}
    <div class="comparison-card"><div class="comparison-title" data-ui-text>判定结果</div>${metricHtml}</div>
    <div class="comparison-card"><div class="comparison-title" data-ui-text>最终答案</div>${answerHtml}</div>
    <div class="comparison-card"><div class="comparison-title" data-ui-text>工具轨迹</div>${traceHtml}</div>
  </div>`;
}

function renderAgentAnswer(answer) {
  if (answer === null || answer === undefined || answer === '') {
    return '<div class="judge-empty" data-ui-text>该评测未返回最终答案。</div>';
  }
  if (typeof answer === 'string') return `<div class="agent-answer-text">${esc(answer)}</div>`;
  if (typeof answer === 'object') {
    const explanation = typeof answer.explanation === 'string' ? answer.explanation : '';
    const extras = {...answer};
    delete extras.explanation;
    const details = Object.keys(extras).length
      ? `<details class="agent-answer-extra"><summary data-ui-text>结构化字段</summary><pre>${esc(JSON.stringify(extras, null, 2))}</pre></details>`
      : '';
    return `${explanation ? `<div class="agent-answer-text">${esc(explanation)}</div>` : ''}${details || '<div class="judge-empty" data-ui-text>未返回可展示的答案文本。</div>'}`;
  }
  return `<div class="agent-answer-text">${esc(String(answer))}</div>`;
}

function renderRunComparison(taskData, runTask) {
  if ((runTask || {}).result_kind === 'agent_run') return renderAgentRunDetail(taskData, runTask);
  const judgeSeq = (taskData.judge_sequence || {}).stages || [];
  const agentGraph = (runTask.agent_trace_graph || {});
  const keyGraph = (runTask.matched_key_operation_graph || {});
  const report = runTask.judge_report || {};
  comparisonState = {
    taskData,
    runTask,
    selected: { kind: 'agent', id: ((agentGraph.nodes || [])[0] || {}).id || '' },
  };
  return `
    <div class="comparison-section">
      <div class="comparison-header">
        <div>
          <div class="section-title" data-ui-text>答案流程图 vs 模型行为流程图</div>
          <div class="run-task-meta">${report.success ? 'PASS' : 'FAIL'} · ${esc(report.rationale || '')}</div>
        </div>
      </div>
      ${renderPromptForComparison(taskData)}
      ${renderTokenUsageCard(report.token_usage || (runTask.llm_trace || {}).token_usage || {})}
      ${renderProcessMetricCard(report, keyGraph)}
      ${renderLLMIOTraceInline(runTask)}
      <div id="run-map-compare" style="width:100%;height:350px;margin-bottom:12px;border-radius:6px;background:#111;display:none;"></div>
      <div class="comparison-workspace">
        <div class="comparison-graphs">
          <div class="comparison-card">
          <div class="comparison-title" data-ui-text>标准答案 / Judge Flow</div>
            ${renderJudgeGraphCards(judgeSeq, report)}
          </div>
          <div class="comparison-card">
          <div class="comparison-title" data-ui-text>关键操作匹配 / Key Operation Match</div>
            ${renderKeyOperationGraphCards(keyGraph)}
          </div>
          <div class="comparison-card">
          <div class="comparison-title" data-ui-text>模型实际行为 / Agent Trace</div>
            ${renderAgentGraphCards(agentGraph)}
          </div>
          <div class="comparison-card">
            <div class="comparison-title" data-ui-text>节点级比对</div>
            ${renderJudgeReportTable(report)}
          </div>
        </div>
        <div class="comparison-card comparison-detail-card">
          <div class="comparison-title" data-ui-text>节点详情</div>
          <div id="comparison-detail">${renderComparisonDetail()}</div>
        </div>
      </div>
    </div>`;
}

function renderProcessMetricCard(report, keyGraph) {
  const summary = keyGraph.summary || {};
  return `<div class="comparison-card token-usage-card">
    <div class="comparison-title" data-ui-text>过程评测指标</div>
    <div class="token-usage-grid">
      <div><b>${metricLabel(report.tool_grounded_success ?? summary.tool_grounded_success)}</b><span>Tool-grounded</span></div>
      <div><b>${metricLabel(report.user_facing_success ?? summary.user_facing_success)}</b><span>User-facing</span></div>
      <div><b>${metricLabel(report.key_operation_success ?? summary.key_operation_success)}</b><span>Key operations</span></div>
      <div><b>${formatTokenNumber(report.matched_key_count ?? summary.matched_key_count ?? 0)}/${formatTokenNumber(report.key_node_count ?? summary.key_node_count ?? 0)}</b><span>Matched keys</span></div>
    </div>
  </div>`;
}

function metricLabel(value) {
  if (value === true) return 'PASS';
  if (value === false) return 'FAIL';
  return 'N/A';
}

function renderPromptForComparison(taskData) {
  const task = taskData.task || {};
  return `<div class="comparison-card prompt-compare-card">
    <div class="comparison-title" data-ui-text>题目</div>
    <div class="prompt-box">${esc(task.user_prompt || '')}</div>
  </div>`;
}

function renderTokenUsageCard(usage) {
  usage = normalizeTokenUsage(usage);
  if (!hasTokenUsage(usage)) {
    return `<div class="comparison-card token-usage-card">
      <div class="comparison-title" data-ui-text>Token 消耗</div>
      <div class="judge-empty" data-ui-text>该运行未返回 token usage，或不是 LLM 模式。</div>
    </div>`;
  }
  return `<div class="comparison-card token-usage-card">
    <div class="comparison-title" data-ui-text>Token 消耗</div>
    <div class="token-usage-grid">
      <div><b>${formatTokenNumber(usage.total_tokens || 0)}</b><span>Total</span></div>
      <div><b>${formatTokenNumber(usage.prompt_tokens || 0)}</b><span>Prompt</span></div>
      <div><b>${formatTokenNumber(usage.completion_tokens || 0)}</b><span>Completion</span></div>
      <div><b>${formatTokenNumber(usage.request_count || 0)}</b><span>Requests</span></div>
      ${usage.cached_tokens ? `<div><b>${formatTokenNumber(usage.cached_tokens)}</b><span>Cached</span></div>` : ''}
      ${usage.reasoning_tokens ? `<div><b>${formatTokenNumber(usage.reasoning_tokens)}</b><span>Reasoning</span></div>` : ''}
    </div>
  </div>`;
}

function renderJudgeGraphCards(stages, report) {
  const nodeStatus = {};
  for (const st of (report.stages || [])) {
    for (const n of (st.nodes || [])) nodeStatus[n.node_id] = n;
  }
  return `<div class="flow-graph judge-flow-graph">${(stages || []).map((stage, si) => `
    <div class="flow-stage graph-stage">
      <div class="flow-stage-title">${esc(stage.stage_id)}</div>
      ${(stage.nodes || []).map(n => {
        const status = nodeStatus[n.node_id] || {};
        return `<div class="flow-node graph-node-card ${status.hard_pass === false ? 'fail' : 'pass'}"
          onclick="selectComparisonNode('judge','${attr(n.node_id)}')">
          <div class="flow-node-title">${esc(n.label_zh || n.node_id)} ${n.is_key ? '<span class="key-pill">KEY</span>' : ''}</div>
          <div class="flow-node-meta">${esc(n.agent_action || '')}</div>
        </div>`;
      }).join('')}
    </div>${si < stages.length - 1 ? '<div class="graph-arrow">→</div>' : ''}`).join('')}</div>`;
}

function renderAgentGraphCards(graph) {
  const nodes = graph.nodes || [];
  return `<div class="flow-graph agent-flow-graph">${nodes.map((n, i) => `
    <div class="flow-node graph-node-card observed ${n.matched_is_key ? (n.match_pass ? 'pass' : 'fail') : ''}" onclick="selectComparisonNode('agent','${attr(n.id)}')">
      <div class="flow-node-title">${n.step}. ${esc(n.label || n.agent_action)} ${renderAgentMatchBadge(n)}</div>
      <div class="flow-node-meta">${esc(n.agent_action || '')}</div>
      <div class="flow-node-meta">${esc(n.matched_judge_label || n.node_id || '')}</div>
    </div>${i < nodes.length - 1 ? '<div class="graph-arrow">→</div>' : ''}`).join('')}</div>`;
}

function renderKeyOperationGraphCards(graph) {
  const nodes = graph.nodes || [];
  if (!nodes.length) return '<div class="judge-empty" data-ui-text>暂无关键操作匹配图。</div>';
  return `<div class="flow-graph key-operation-graph">${nodes.map((n, i) => `
    <div class="flow-node graph-node-card observed ${n.hard_pass ? 'pass' : 'fail'}" onclick="selectComparisonNode('key','${attr(n.id)}')">
      <div class="flow-node-title">${i + 1}. ${esc(n.label || n.judge_node_id)} <span class="${n.hard_pass ? 'match-pill pass' : 'match-pill fail'}">KEY</span></div>
      <div class="flow-node-meta">${esc(n.agent_action || '')}</div>
      <div class="flow-node-meta">matched step ${esc(n.matched_event_step ?? 'missing')} · ${esc(n.verdict_target || '')}</div>
      ${n.failure_type ? `<div class="flow-node-meta">failure: ${esc(n.failure_type)}</div>` : ''}
    </div>${i < nodes.length - 1 ? '<div class="graph-arrow">→</div>' : ''}`).join('')}</div>`;
}

function renderAgentMatchBadge(n) {
  if (!n.matched_judge_node_id) return '';
  const cls = n.match_pass ? 'match-pill pass' : 'match-pill fail';
  const label = n.matched_is_key ? 'KEY' : 'MATCH';
  return `<span class="${cls}">${label}</span>`;
}

function renderJudgeReportTable(report) {
  const rows = [];
  for (const st of (report.stages || [])) {
    for (const n of (st.nodes || [])) rows.push({ stage: st.stage_id, ...n });
  }
  return `<div class="judge-report-list">${rows.map(r => `
    <div class="judge-report-row ${r.hard_pass ? 'pass' : 'fail'}">
      <div><b>${esc(r.label_zh || r.node_id)}</b><br><span>${esc(r.stage)} · ${esc(r.agent_action)}</span></div>
      <div>${r.hard_pass ? 'PASS' : 'FAIL'}<br><span>soft ${r.soft_pass_count}/${r.soft_total}</span></div>
    </div>`).join('')}</div>`;
}

function selectComparisonNode(kind, id) {
  if (!comparisonState) return;
  comparisonState.selected = { kind, id };
  const el = document.getElementById('comparison-detail');
  if (el) el.innerHTML = renderComparisonDetail();
}

function renderLLMIOTraceInline(runTask) {
  const traj = runTask.trajectory || {};
  const trajEvents = traj.events || traj;
  let h = '<div class="comparison-card llm-io-card"><div class="comparison-title" data-ui-text>📝 模型输入输出</div>';
  h += '<div style="max-height:500px;overflow-y:auto;font-size:10px">';
  let hasContent = false;
  if (trajEvents && Array.isArray(trajEvents) && trajEvents.length > 0) {
    for (const evt of trajEvents.slice(0, 30)) {
      const func = evt.function || evt.tool || evt.agent_action || '';
      const resp = evt.response || evt.result || {};
      h += `<div style="margin:4px 0;padding:4px;background:#111;border-radius:3px"><b style="color:#79c0ff">${esc(String(func))}</b>
        <details><summary style="cursor:pointer;color:#a6adc8">args / result</summary>
        <pre style="font-size:9px;color:#cdd6f4;margin:2px 0;max-height:150px;overflow-y:auto">${esc(JSON.stringify({args:evt.args||{},result:resp},null,1))}</pre></details></div>`;
      hasContent = true;
    }
  }
  if (!hasContent) {
    const stages = (runTask.judge_report || {}).stages || [];
    for (const st of stages) {
      for (const node of (st.nodes || [])) {
        if (node.hard_pass !== undefined) {
          h += `<div style="margin:2px 0;padding:2px;background:#111;border-radius:2px"><span style="color:${node.hard_pass?'#4ecca3':'#e74c3c'}">${node.hard_pass?'✅':'❌'}</span> ${esc(node.label_zh||node.node_id||'')}</div>`;
          hasContent = true;
        }
      }
    }
  }
  if (!hasContent) h += '<div style="color:#a6adc8" data-ui-text>暂无 trace 数据</div>';
  h += '</div></div>';
  return h;
}

function renderComparisonDetail() {
  if (!comparisonState || !comparisonState.selected) {
    return '<div class="judge-empty" data-ui-text>点击左侧流程节点查看详情。</div>';
  }
  const { kind, id } = comparisonState.selected;
  if (kind === 'agent') {
    return renderAgentEventDetail(id);
  }
  if (kind === 'key') {
    return renderKeyOperationDetail(id);
  }
  return renderJudgeNodeDetail(id);
}

function renderAgentEventDetail(id) {
  const trajectory = (comparisonState.runTask.trajectory || {}).events || [];
  const event = trajectory.find(e => `event_${String(e.step).padStart(3, '0')}` === id)
    || trajectory.find(e => e.node_id === id);
  if (!event) return '<div class="judge-empty" data-ui-text>未找到模型行为节点。</div>';
  const graphNode = ((comparisonState.runTask.agent_trace_graph || {}).nodes || []).find(n => n.id === id) || {};
  return `<div class="detail-block">
    <div class="detail-title">${event.step}. ${esc(event.agent_action)}</div>
    <div class="detail-sub">${esc(event.node_id || '')} · ${esc(event.stage_id || '')}</div>
    ${renderAgentMatchDetail(graphNode)}
    ${renderLlmStepUsage(event)}
    ${renderAppEventSummary(event)}
    <div class="detail-label">Args</div><pre>${esc(JSON.stringify(event.args || {}, null, 2))}</pre>
    <div class="detail-label">Response</div><pre>${esc(JSON.stringify(event.response || {}, null, 2))}</pre>
  </div>`;
}

function renderLlmStepUsage(event) {
  const llmEvent = findLlmTraceEventForAgentEvent(event);
  const usage = (llmEvent || {}).usage || {};
  if (!hasTokenUsage(usage)) return '';
  return `<div class="detail-label" data-ui-text>Token 消耗</div>
    <div class="token-usage-grid compact">
      <div><b>${formatTokenNumber(usage.total_tokens || 0)}</b><span>Total</span></div>
      <div><b>${formatTokenNumber(usage.prompt_tokens || 0)}</b><span>Prompt</span></div>
      <div><b>${formatTokenNumber(usage.completion_tokens || 0)}</b><span>Completion</span></div>
      ${usage.cached_tokens ? `<div><b>${formatTokenNumber(usage.cached_tokens)}</b><span>Cached</span></div>` : ''}
      ${usage.reasoning_tokens ? `<div><b>${formatTokenNumber(usage.reasoning_tokens)}</b><span>Reasoning</span></div>` : ''}
    </div>`;
}

function findLlmTraceEventForAgentEvent(event) {
  const events = ((comparisonState.runTask.llm_trace || {}).events || []);
  const match = String(event.node_id || '').match(/^agent_step_(\d+)$/);
  if (match) {
    const step = Number(match[1]);
    return events.find(e => Number(e.step) === step) || null;
  }
  return null;
}

function renderAgentMatchDetail(node) {
  if (!node || !node.matched_judge_node_id) return '';
  const verdict = node.match_pass ? 'PASS' : 'FAIL';
  const cls = node.match_pass ? 'detail-pass' : 'detail-fail';
  return `<div class="detail-label" data-ui-text>Judge 匹配</div>
    <div class="${cls}">${esc(verdict)} · ${esc(node.matched_judge_label || node.matched_judge_node_id)} ${node.matched_is_key ? '(KEY)' : ''}</div>`;
}

function renderKeyOperationDetail(id) {
  const graph = comparisonState.runTask.matched_key_operation_graph || {};
  const node = (graph.nodes || []).find(n => n.id === id);
  if (!node) return '<div class="judge-empty" data-ui-text>未找到关键操作节点。</div>';
  const trajectory = (comparisonState.runTask.trajectory || {}).events || [];
  const event = trajectory.find(e => e.step === node.matched_event_step);
  return `<div class="detail-block">
    <div class="detail-title">${esc(node.label || node.judge_node_id)} ${node.hard_pass ? '<span class="match-pill pass">PASS</span>' : '<span class="match-pill fail">FAIL</span>'}</div>
    <div class="detail-sub">${esc(node.stage_id || '')} · ${esc(node.agent_action || '')}</div>
    <div class="detail-label" data-ui-text>关键操作判定</div>
    <div class="${node.hard_pass ? 'detail-pass' : 'detail-fail'}">
      ${node.hard_pass ? 'PASS' : 'FAIL'} · ${esc(node.verdict_target || '')}${node.failure_type ? ` · ${esc(node.failure_type)}` : ''}
    </div>
    <div class="detail-label">Matched Event</div>
    ${event ? `<pre>${esc(JSON.stringify(event, null, 2))}</pre>` : '<div class="judge-empty" data-ui-text>没有匹配到实际 Agent 操作。</div>'}
    <div class="detail-label">Graph Node</div><pre>${esc(JSON.stringify(node, null, 2))}</pre>
  </div>`;
}

function renderAppEventSummary(event) {
  const discovery = renderDiscoveryEventSummary(event);
  if (discovery) return discovery;
  return renderGroundingEventSummary(event);
}

function renderDiscoveryEventSummary(event) {
  const resp = event.response || {};
  if (Array.isArray(resp.results)) {
    return `<div class="detail-label" data-ui-text>服务候选</div>
      <div class="candidate-list">${resp.results.map(c => `
        <div class="candidate-row">
          <b>${esc(c.name || c.service_id)}</b>
          <span>${esc(c.service_type || '')}</span>
          <span>${esc(c.price_band || '')}</span>
          <span>${c.rating ?? ''}</span>
          <code>${esc(c.service_id || c.place_id || '')}</code>
        </div>`).join('')}</div>`;
  }
  if (resp.detail && Object.keys(resp.detail).length) {
    const d = resp.detail;
    return `<div class="detail-label" data-ui-text>商户详情</div>
      <div class="candidate-row selected">
        <b>${esc(d.name || d.service_id)}</b>
        <span>${esc(d.service_type || '')}</span>
        <span>${esc(d.price_band || '')}</span>
        <span>${d.rating ?? ''}</span>
        <span>${esc(d.open_status || '')}</span>
        <code>${esc(d.service_id || d.place_id || '')}</code>
      </div>`;
  }
  if (Array.isArray(resp.comparisons)) {
    return `<div class="detail-label" data-ui-text>候选比较</div>
      <div class="candidate-list">${resp.comparisons.map(c => `
        <div class="candidate-row">
          <b>${esc(c.name || c.service_id)}</b>
          <span>${(c.evidence || []).filter(e => e.matched).length}/${(c.evidence || []).length} constraints</span>
          <code>${esc(c.service_id || c.place_id || '')}</code>
        </div>`).join('')}</div>`;
  }
  return '';
}

function renderGroundingEventSummary(event) {
  const resp = event.response || {};
  if (Array.isArray(resp.candidates)) {
    return `<div class="detail-label" data-ui-text>Grounding 候选</div>
      <div class="candidate-list">${resp.candidates.map(c => `
        <div class="candidate-row">
          <b>${esc(c.name || c.candidate_id)}</b>
          <span>${esc(c.district_id || '')}</span>
          <code>${esc(c.candidate_id || '')}</code>
        </div>`).join('')}</div>`;
  }
  if (resp.selected && Object.keys(resp.selected).length) {
    const loc = resp.selected.location || {};
    const diagnostic = groundingDistanceDiagnostic(loc);
    return `<div class="detail-label" data-ui-text>选择结果</div>
      <div class="candidate-row selected">
        <b>${esc(resp.selected.name || resp.selected.candidate_id)}</b>
        <span>${esc(resp.selected.district_id || '')}</span>
        <code>${esc(resp.selected.candidate_id || '')}</code>
        <span>${esc(loc.lat || '')}, ${esc(loc.lng || '')}</span>
        ${diagnostic ? `<span>${diagnostic}</span>` : ''}
      </div>`;
  }
  return '';
}

function groundingDistanceDiagnostic(loc) {
  const gold = ((comparisonState.taskData.task || {}).summary || {}).gold_summary || {};
  const coord = gold.coordinate || {};
  const distance = distanceMeters(Number(coord.lat), Number(coord.lng), Number(loc.lat), Number(loc.lng));
  if (!Number.isFinite(distance)) return '';
  const radius = Number(gold.acceptable_radius_m || 0);
  return `Distance to gold: ${Math.round(distance)}m${radius ? ` / diagnostic radius ${radius}m` : ''}`;
}

function renderJudgeNodeDetail(nodeId) {
  const report = comparisonState.runTask.judge_report || {};
  let node = null;
  let stageId = '';
  for (const st of (report.stages || [])) {
    const found = (st.nodes || []).find(n => n.node_id === nodeId);
    if (found) { node = found; stageId = st.stage_id; break; }
  }
  if (!node) return '<div class="judge-empty" data-ui-text>未找到标准答案节点。</div>';
  return `<div class="detail-block">
    <div class="detail-title">${esc(node.label_zh || node.node_id)} ${node.is_key ? '<span class="key-pill">KEY</span>' : ''}</div>
    <div class="detail-sub">${esc(stageId)} · ${esc(node.agent_action || '')}</div>
    <div class="detail-label" data-ui-text>判定</div>
    <div class="${node.hard_pass ? 'detail-pass' : 'detail-fail'}">${node.hard_pass ? 'PASS' : 'FAIL'} · soft ${node.soft_pass_count}/${node.soft_total}</div>
    <div class="detail-label">Hard checks</div><pre>${esc(JSON.stringify(node.hard_checks || [], null, 2))}</pre>
    <div class="detail-label">Soft checks</div><pre>${esc(JSON.stringify(node.soft_checks || [], null, 2))}</pre>
  </div>`;
}

async function startRunFromUI() {
  const status = document.getElementById('run-status');
  const scope = currentRunScope();
  if (scope.taskCount === 0) {
    status.textContent = '\u8fd0\u884c\u5931\u8d25\uff1a\u5f53\u524d\u4e16\u754c\u6ca1\u6709\u53ef\u8fd0\u884c\u7684\u6b63\u5f0f\u4efb\u52a1\u3002';
    status.className = 'save-status err';
    return;
  }
  const approvedOnly = (document.getElementById('run-approved-only') || {}).checked || false;
  if (approvedOnly && scope.approvedCount === 0) {
    status.textContent = '\u8fd0\u884c\u672a\u542f\u52a8\uff1a\u5f53\u524d\u8303\u56f4\u6ca1\u6709\u5df2\u5ba1\u6838\u4efb\u52a1\u3002';
    status.className = 'save-status err';
    return;
  }
  status.textContent = '运行中...';
  status.className = 'save-status';
  const payload = {
    mode: 'llm',
    model_key: document.getElementById('run-model').value || DEFAULT_TEST_MODEL_KEY,
    dynamic_event_mode: runScope.dynamicEventMode || 'shared_world',
    task_type: scope.taskType,
    city: scope.city,
    batch: scope.batch,
    task_type_filter: scope.taskType,
    approved_only: approvedOnly,
    task_limit: Number(document.getElementById('run-task-limit')?.value || 0),
  };
  try {
    const resp = await fetch('/api/run/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || `HTTP ${resp.status}`);
    status.textContent = `完成: ${data.passed ?? data.success_count ?? 0}/${data.task_count}`;
    status.className = 'save-status ok';
    selectedRunId = data.run_id;
    await loadRuns();
    await watchRunProgress(data.run_id, status);
  } catch (e) {
    status.textContent = '运行失败: ' + (e.message || e);
    status.className = 'save-status err';
  }
}

async function watchRunProgress(runId, status) {
  const poll = async () => {
    try {
      const resp = await fetch(`/api/run/status?run=${encodeURIComponent(runId)}`);
      const state = await resp.json();
      if (!resp.ok) throw new Error(state.error || `HTTP ${resp.status}`);
      const done = Number(state.completed_count || 0);
      const total = Number(state.total_count || 0);
      const tokens = Number((state.token_usage || {}).total || 0);
      const current = state.current_task_id ? ` · ${state.current_task_id}` : '';
      status.textContent = `${state.status === 'completed' ? '完成' : '运行中'}: ${done}/${total} · PASS ${state.passed || 0} · ${state.elapsed_seconds || 0}s · ${tokens} tokens${current}`;
      status.className = state.status === 'completed' ? 'save-status ok' : 'save-status';
      await loadRuns();
      if (selectedRunId === runId) {
        const runResp = await fetch(`/api/run?run=${encodeURIComponent(runId)}`);
        if (runResp.ok) {
          runData = await runResp.json();
          renderRunSummary();
          renderRunTaskList();
        }
      }
      if (state.status === 'queued' || state.status === 'running') setTimeout(poll, 1200);
      else if (state.status !== 'completed') {
        status.textContent = `运行${state.status}: ${state.error || '请查看运行记录'}`;
        status.className = 'save-status err';
      }
    } catch (err) {
      status.textContent = '运行状态读取失败: ' + (err.message || err);
      status.className = 'save-status err';
    }
  };
  await poll();
}

// ========== EDITABLE JUDGE PANEL ==========

function resolveQualityProfile(ex) {
  return (ex && ex.quality_profile) || (ex && ex.difficulty_profile) || {};
}

function renderSourceReviewPanel(source) {
  if (!source || !source.example) return '';
  const ex = sourceState || source.example || {};
  if (ex.task_type === 'place_discovery') return renderDiscoverySourceReviewPanel(source, ex);
  const status = ex.review_status || 'draft';
  let h = '<div class="section source-review">';
  h += '<div class="source-toolbar">';
  h += '<div class="section-title" style="border:none;margin:0;padding:0" data-ui-text>批量样例审查 / Evidence Provenance</div>';
  h += '<div class="judge-toolbar-right">';
  h += '<span id="source-save-status" data-ui-output class="save-status"></span>';
  h += `<button class="btn-save" onclick="saveSource()" data-ui-text>保存样例修改</button>`;
  h += '</div></div>';
  h += `<div class="source-path">source: ${esc(source.source_path || '')}</div>`;
  h += '<div class="source-grid">';
  h += '<label>Review status<select id="src-review-status" onchange="updateSourceField(\'review_status\', this.value)">';
  for (const item of ['draft', 'approved', 'needs_fix', 'rejected']) {
    h += `<option value="${item}" ${status === item ? 'selected' : ''}>${item}</option>`;
  }
  h += '</select></label>';
  h += `<label>Final difficulty<input readonly value="${attr((resolveQualityProfile(ex).display_difficulty) || ex.difficulty || '')}" title="由 quality_profile 的 display_difficulty 推导；如需修改，请同步编辑下方 quality_profile"></label>`;
  h += `<label>Prompt language<input value="${attr(ex.prompt_language || '')}" onchange="updateSourceField('prompt_language', this.value)" placeholder="zh-CN / ja-JP / en-GB"></label>`;
  h += `<label>Review language<input value="${attr(ex.review_language || ((ex.review_translation || {}).language) || '')}" onchange="updateSourceField('review_language', this.value)" placeholder="zh-CN"></label>`;
  h += '</div>';
  h += renderQualityPanel(ex);
  h += '<div class="source-language-panel">';
  h += '<div class="language-panel-card primary">';
  h += `<div class="language-panel-title" data-ui-text>Agent 实际输入 <span>${esc(ex.prompt_language || '-')}</span></div>`;
  h += `<textarea class="source-textarea large" oninput="updateSourceField('user_prompt', this.value)">${esc(ex.user_prompt || '')}</textarea>`;
  h += '</div>';
  h += '<div class="language-panel-card review">';
  h += `<div class="language-panel-title" data-ui-text>人工审查翻译 <span>${esc(ex.review_language || ((ex.review_translation || {}).language) || '-')}</span></div>`;
  h += `<textarea class="source-textarea large" oninput="updateReviewTranslation(this.value)">${esc(ex.user_prompt_translation || ((ex.review_translation || {}).user_prompt) || '')}</textarea>`;
  h += '<div class="language-panel-help" data-ui-text>用于人工检查本地语言 prompt 的语义、模糊性和线索是否合理；不会作为 Agent 输入。</div>';
  h += '</div></div>';
  h += '<div class="source-grid two">';
  h += sourceJsonEditor('visual_clues', ex.visual_clues || [], 'Visual clues');
  h += sourceJsonEditor('text_clues', ex.text_clues || [], 'Text clues');
  h += sourceJsonEditor('negative_clues', ex.negative_clues || [], 'Negative clues');
  h += sourceJsonEditor('candidate_scope', ex.candidate_scope || {}, 'Candidate scope');
  h += sourceJsonEditor('quality_profile', resolveQualityProfile(ex), 'Quality profile');
  h += sourceJsonEditor('prompt_evidence_map', ex.prompt_evidence_map || {}, 'Prompt evidence map');
  h += '</div>';
  h += '<div class="source-grid two">';
  h += sourceReadonlyBlock('Evidence ledger', ex.evidence_ledger || ex.source_evidence || {});
  h += sourceReadonlyBlock('Quality gate', ex.quality_gate || {});
  h += '</div>';
  h += '</div>';
  return h;
}

function sourceJsonEditor(field, value, title) {
  return `<label class="source-label">${esc(title)}
    <textarea class="source-textarea" onchange="updateSourceJsonField('${field}', this.value)">${esc(JSON.stringify(value, null, 2))}</textarea>
  </label>`;
}

function renderDiscoverySourceReviewPanel(source, ex, data = null) {
  const status = ex.review_status || 'draft';
  let h = '<div class="section difficulty-panel quality-panel discovery-quality-review"><div class="difficulty-panel-title" data-ui-text>质量审核面板 · Quality Profile</div><div class="source-review discovery-review">';
  h += '<div class="source-toolbar">';
  h += '<div class="section-title" style="border:none;margin:0;padding:0" data-ui-text>Discovery 样例审查 / Service Evidence</div>';
  h += '<div class="judge-toolbar-right">';
  h += '<span id="source-save-status" data-ui-output class="save-status"></span>';
  h += `<button class="btn-save" onclick="saveSource()" data-ui-text>保存样例修改</button>`;
  h += '</div></div>';
  const construction = ex.evidence_ledger?.construction_contract;
  if (data && !construction) h += renderDiscoveryConditions(data);
  const profile = resolveQualityProfile(ex);
  h += construction ? renderDiscoveryConstruction(construction) : (profile.rubric_version || '').startsWith('place_discovery.quality')
    ? renderDiscoveryQualityPanel(ex, profile, true, data) : renderDifficultyBreakdown(profile) + (data ? renderDiscoveryComparison(data) : '');
  h += '<details class="gr-details"><summary data-ui-text>来源与原始记录</summary>';
  h += `<div class="source-path">source: ${esc(source.source_path || '')}</div>`;
  h += '<div class="source-grid">';
  h += '<label>Review status<select id="src-review-status" onchange="updateSourceField(\'review_status\', this.value)">';
  for (const item of ['draft', 'approved', 'needs_fix', 'rejected']) {
    h += `<option value="${item}" ${status === item ? 'selected' : ''}>${item}</option>`;
  }
  h += '</select></label>';
  h += `<label>Service type<input value="${attr(ex.service_type || '')}" onchange="updateSourceField('service_type', this.value)"></label>`;
  h += `<label>Difficulty<input readonly value="${attr((resolveQualityProfile(ex).display_difficulty) || ex.difficulty || '')}" title="由 quality_profile 的 display_difficulty 推导；如需修改，请同步编辑下方 quality_profile"></label>`;
  h += `<label>Prompt language<input value="${attr(ex.prompt_language || '')}" onchange="updateSourceField('prompt_language', this.value)" placeholder="zh-CN / ja-JP / en-GB"></label>`;
  h += `<label>Review language<input value="${attr(ex.review_language || ((ex.review_translation || {}).language) || '')}" onchange="updateSourceField('review_language', this.value)" placeholder="zh-CN"></label>`;
  h += '</div>';
  h += '<div class="source-grid two">';
  h += sourceJsonEditor('start', ex.start || {}, 'Start / search area');
  h += sourceJsonEditor('hard_constraints', ex.hard_constraints || [], 'Hard constraints');
  h += sourceJsonEditor('soft_preferences', ex.soft_preferences || [], 'Soft preferences');
  h += sourceJsonEditor('ranking_criteria', ex.ranking_criteria || [], 'Ranking criteria');
  h += sourceJsonEditor('gold', ex.gold || {}, 'Gold ranked candidates');
  h += sourceJsonEditor('distractors', ex.distractors || [], 'Distractors / exclusions');
  h += sourceJsonEditor('evaluation', ex.evaluation || {}, 'Evaluation');
  h += sourceJsonEditor('prompt_evidence_map', ex.prompt_evidence_map || {}, 'Prompt evidence map');
  h += sourceJsonEditor('quality_profile', resolveQualityProfile(ex), 'Quality profile');
  h += '</div>';
  h += '<div class="source-grid two">';
  h += sourceReadonlyBlock('Evidence ledger', ex.evidence_ledger || ex.source_evidence || {});
  h += sourceReadonlyBlock('Quality gate', ex.quality_gate || {});
  h += '</div>';
  h += '</details></div></div>';
  return h;
}

// ── Quality profile (objective model) ──
// place_grounding.quality.v1 / place_discovery.quality.v1 → dedicated panels.
// Legacy difficulty_profile still uses renderDifficultyBreakdown (no radar chart).

function renderQualityPanel(ex) {
  const profile = resolveQualityProfile(ex);
  if (!profile || !profile.rubric_version) return renderDifficultyBreakdown(profile);
  const rv = profile.rubric_version || '';
  if (rv.startsWith('place_grounding.quality')) return renderGroundingQualityPanel(ex, profile);
  if (rv.startsWith('place_discovery.quality')) return renderDiscoveryQualityPanel(ex, profile);
  return renderDifficultyBreakdown(profile);
}

function renderGroundingQualityPanel(ex, profile, evidence = '', records = '') {
  const ambiguityScore = Number(profile.ambiguity_score ?? 0);
  const complexityScore = Number(profile.complexity_score ?? 0);
  const place = profile.place_ambiguity || {};
  const task = profile.task_complexity_post || profile.task_complexity_pre || {};
  const f = place.factors || {};
  const landscape = ex.ambiguity_landscape || {};
  const confusable = landscape.confusable_candidates || [];
  const confusableCount = Number(profile.confusable_candidate_count ?? f.confusable_candidate_count ?? confusable.length);
  return `<div class="difficulty-panel quality-panel">
    <div class="difficulty-panel-head">
      <div>
        <div class="difficulty-panel-title" data-ui-text>质量审核面板 · Quality Profile</div>
        <div class="difficulty-panel-desc" data-ui-text>
          客观质量模型：ambiguity_score 衡量 gold 地点在地图上的客观歧义（由同类候选密度与最近同类距离推导），complexity_score 衡量任务输入给 Agent 的信息复杂度。下方"可混淆候选"为 gold 500m 内的全部同类 POI，是审核 gold 可区分性的关键依据。
        </div>
      </div>
    </div>
    <div class="quality-score-row">
      <div class="quality-score-chip">
        <span class="quality-score-label">ambiguity_score</span>
        <span class="quality-score-value">${ambiguityScore}/10</span>
      </div>
      <div class="quality-score-chip">
        <span class="quality-score-label">complexity_score</span>
        <span class="quality-score-value">${complexityScore}/10</span>
      </div>
      <div class="quality-score-chip">
        <span class="quality-score-label">confusable_candidates</span>
        <span class="quality-score-value">${confusableCount}</span>
      </div>
      <div class="quality-score-chip">
        <span class="quality-score-label" data-ui-text>最近同类</span>
        <span class="quality-score-value">${formatMeters(f.nearest_same_type_m)}</span>
      </div>
    </div>
    <div class="difficulty-breakdown">
      ${difficultyLayerCard('地点歧义 · place_ambiguity', place.level, '', [
        ['得分', place.score ?? ambiguityScore],
        ['300m 内同类', f.same_type_within_300m ?? '-'],
        ['500m 内同类', f.same_type_within_500m ?? '-'],
        ['1km 内同类', f.same_type_within_1000m ?? '-'],
        ['500m 内全部 POI', f.all_places_within_500m ?? '-'],
        ['最近同类名称', landscape.nearest_same_type_name || '-'],
        ['可混淆候选数', f.confusable_candidate_count ?? confusableCount],
      ], '同类候选越密集、最近同类越近、可混淆候选越多，地图歧义越高。')}
      ${difficultyLayerCard('任务复杂度 · task_complexity', task.level, '', [
        ['得分', task.score ?? complexityScore],
        ['阶段', task.stage || '-'],
        ['属性覆盖', (task.attribute_coverage || []).join(', ') || '-'],
        ['证据映射数', task.mapped_evidence_count ?? '-'],
        ['线索数量', profile.info_count ?? '-'],
      ], '可用线索与可验证属性越多、证据映射越充分，Agent 可用信息越丰富。')}
    </div>
    ${evidence}
    ${renderConfusableCandidates(confusable, 'grounding')}
    ${records}
    <div class="difficulty-footnote">Rubric: ${esc(profile.rubric_version || '')}</div>
  </div>`;
}

function renderDiscoveryQualityPanel(ex, profile, nested = false, data = null) {
  const searchScore = Number(profile.search_ambiguity_score ?? 0);
  const constraintScore = Number(profile.constraint_complexity ?? 0);
  const search = profile.search_ambiguity || {};
  const constraint = profile.constraint_complexity_post || profile.constraint_complexity_pre || {};
  const sf = search.factors || {};
  const landscape = ex.search_landscape || {};
  const confusable = landscape.confusable_candidates || [];
  const confusableCount = Number(profile.confusable_candidate_count ?? sf.confusable_candidate_count ?? confusable.length);
  return `<div class="difficulty-panel ${nested ? 'discovery-quality-metrics' : 'quality-panel'}">
    <div class="difficulty-panel-head">
      <div>
        <div class="difficulty-panel-title" data-ui-text>${nested ? '质量评分与指标' : '质量审核面板 · Quality Profile'}</div>
        <div class="difficulty-panel-desc" data-ui-text>
          客观质量模型：search_ambiguity_score 衡量 gold 服务在搜索空间中的客观歧义（由同区/近邻/同类服务密度推导），constraint_complexity 衡量角色约束与任务输入的复杂度。下方"可混淆候选"列出 1200m 内服务及排除理由，是审核 gold 唯一性的关键依据。
        </div>
      </div>
    </div>
    <div class="quality-score-row">
      <div class="quality-score-chip">
        <span class="quality-score-label">search_ambiguity_score</span>
        <span class="quality-score-value">${searchScore}/10</span>
      </div>
      <div class="quality-score-chip">
        <span class="quality-score-label">constraint_complexity</span>
        <span class="quality-score-value">${constraintScore}/10</span>
      </div>
      <div class="quality-score-chip">
        <span class="quality-score-label">confusable_candidates</span>
        <span class="quality-score-value">${confusableCount}</span>
      </div>
      <div class="quality-score-chip">
        <span class="quality-score-label" data-ui-text>候选范围内总数</span>
        <span class="quality-score-value">${sf.candidates_in_scope_count ?? (landscape.candidates_in_scope || []).length}</span>
      </div>
    </div>
    <div class="difficulty-breakdown">
      ${difficultyLayerCard('搜索歧义 · search_ambiguity', search.level, '', [
        ['得分', search.score ?? searchScore],
        ['同区域服务数', sf.same_district_service_count ?? '-'],
        ['900m 内同类服务', sf.nearby_same_service_count_900m ?? '-'],
        ['同类主类型数', sf.same_primary_type_count ?? '-'],
        ['可验证属性数', sf.verifiable_attribute_count ?? '-'],
        ['可混淆候选数', sf.confusable_candidate_count ?? confusableCount],
      ], '同区域/近邻/同类服务候选越多，搜索歧义越高，越需要比较排除。')}
      ${difficultyLayerCard('约束复杂度 · constraint_complexity', constraint.level, '', [
        ['得分', constraint.score ?? constraintScore],
        ['阶段', constraint.stage || '-'],
        ['角色硬约束数', constraint.character_hard_constraint_count ?? '-'],
        ['角色软偏好数', constraint.character_soft_preference_count ?? '-'],
        ['任务硬约束数', constraint.hard_constraint_count ?? '-'],
        ['任务软偏好数', constraint.soft_preference_count ?? '-'],
        ['评判条数', constraint.criteria_count ?? '-'],
      ], '角色与任务约束越多、越硬，推荐排序的复杂度越高。')}
    </div>
    ${data ? renderDiscoveryComparison(data) : renderConfusableCandidates(confusable, 'discovery')}
    <div class="difficulty-footnote">Rubric: ${esc(profile.rubric_version || '')}</div>
  </div>`;
}

function renderConfusableCandidates(candidates, kind) {
  if (!Array.isArray(candidates) || !candidates.length) {
    return `<div class="confusable-section">
      <div class="confusable-head" data-ui-text>可混淆候选 · confusable_candidates</div>
      <div class="confusable-empty" data-ui-text>无可混淆候选（gold 在阈值范围内无同类竞争者）</div>
    </div>`;
  }
  const reasonLabel = kind === 'discovery' ? '排除理由' : '相似原因';
  const hint = kind === 'discovery'
    ? '同区域 1200m 内服务候选及排除理由；排除理由为空表示该候选无法被角色约束排除，需重点核查'
    : 'gold 500m 内同类 POI，Agent 在地图定位时可能误选，需核查 gold 是否可区分';
  const rows = candidates.map((c, i) => {
    const reasons = kind === 'discovery'
      ? (Array.isArray(c.exclusion_reasons) ? c.exclusion_reasons.join('；') : (c.exclusion_reasons || '-'))
      : (c.similarity_reason || '-');
    const reasonCls = kind === 'discovery' && (!reasons || reasons === '-') ? 'reason-flag' : '';
    return `<tr>
      <td class="col-idx">${i + 1}</td>
      <td class="col-name">${esc(c.name || '-')}</td>
      <td class="col-dist">${formatMeters(c.distance_m)}</td>
      <td class="col-type">${esc(c.primary_type || '-')}</td>
      <td class="col-reason ${reasonCls}">${esc(reasons)}</td>
    </tr>`;
  }).join('');
  return `<div class="confusable-section">
    <div class="confusable-head">
      <span class="confusable-title" data-ui-text>可混淆候选 · confusable_candidates (${candidates.length})</span>
      <span class="confusable-hint" data-ui-text>${esc(hint)}</span>
    </div>
    <table class="confusable-table">
      <thead><tr><th>#</th><th data-ui-text>名称</th><th data-ui-text>距离</th><th data-ui-text>类型</th><th data-ui-text>${reasonLabel}</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
  </div>`;
}

function renderDifficultyBreakdown(profile) {
  if (!profile) return '';
  if (!profile.rubric_version) return renderLegacyDifficultyBreakdown(profile);
  if (profile.rubric_version.startsWith('place_discovery')) return renderDiscoveryDifficultyBreakdown(profile);
  const place = profile.place_difficulty || {};
  const task = profile.task_difficulty || {};
  return `<div class="difficulty-panel">
    <div class="difficulty-panel-head">
      <div>
        <div class="difficulty-panel-title" data-ui-text>难度构成面板</div>
        <div class="difficulty-panel-desc" data-ui-text>
          Final difficulty 由“地点本身的地图歧义”与“任务输入提供给 Agent 的信息难度”共同推导。
          如果人工修改难度，应同步修改下方 difficulty_profile，而不是只改顶层字符串。
        </div>
      </div>
      <div class="difficulty-badge ${esc(profile.difficulty || '')}">${esc(profile.difficulty || '-')}</div>
    </div>
    <div class="difficulty-breakdown">
      ${difficultyLayerCard('地点本身难度', place.level, place.rationale, [
        ['全城同类地点', valueAt(place, 'factors.same_type_city_count')],
        ['同区域候选', valueAt(place, 'factors.same_district_count')],
        ['300m 内 POI', valueAt(place, 'factors.nearby_300m_count')],
        ['800m 内 POI', valueAt(place, 'factors.nearby_800m_count')],
        ['1km 内同类候选', valueAt(place, 'factors.same_type_nearby_1000m_count')],
        ['最近同类距离', formatMeters(valueAt(place, 'factors.nearest_same_type_m'))],
        ['干扰候选数', valueAt(place, 'factors.distractor_count')],
      ], '地点越处在同类密集、附近 POI 密集或强干扰候选较多的环境中，地点本身越难。')}
      ${difficultyLayerCard('任务输入难度', task.level, task.rationale, [
        ['信息数量', task.info_count],
        ['属性覆盖', (task.attribute_coverage || []).join(', ') || '-'],
        ['信息质量', task.info_quality],
        ['冲突程度', task.conflict_level],
        ['证据映射数', task.mapped_evidence_count],
        ['干扰候选数', valueAt(task, 'candidate_ambiguity.distractor_count')],
        ['同类干扰数', valueAt(task, 'candidate_ambiguity.same_type_distractors')],
      ], '给 Agent 的线索越少、属性覆盖越窄、冲突越强，任务输入难度越高；线索充分且证据映射清楚会降低难度。')}
    </div>
    <div class="difficulty-footnote">Rubric: ${esc(profile.rubric_version || '')}</div>
  </div>`;
}

function renderDiscoveryDifficultyBreakdown(profile) {
  const service = profile.service_difficulty || {};
  const constraint = profile.constraint_difficulty || {};
  return `<div class="difficulty-panel">
    <div class="difficulty-panel-head">
      <div>
        <div class="difficulty-panel-title" data-ui-text>难度构成面板</div>
        <div class="difficulty-panel-desc" data-ui-text>
          Final difficulty 由“服务候选的天然歧义”与“任务约束的输入难度”共同推导。
          如果人工修改难度，应同步修改下方 difficulty_profile，而不是只改顶层字符串。
        </div>
      </div>
      <div class="difficulty-badge ${esc(profile.difficulty || '')}">${esc(profile.difficulty || '-')}</div>
    </div>
    <div class="difficulty-breakdown">
      ${difficultyLayerCard('服务候选歧义', service.level, service.rationale, [
        ['同区域服务数', valueAt(service, 'factors.same_district_service_count')],
        ['900m 内同类服务', valueAt(service, 'factors.nearby_same_service_count_900m')],
        ['同类主类型数', valueAt(service, 'factors.same_primary_type_count')],
        ['可验证属性数', valueAt(service, 'factors.verifiable_attribute_count')],
        ['干扰候选数', valueAt(service, 'factors.distractor_count')],
      ], '同区/近邻/同类候选越多，越需要比较排除，服务候选歧义越高。')}
      ${difficultyLayerCard('约束输入难度', constraint.level, constraint.rationale, [
        ['请求难度', constraint.requested_difficulty || '-'],
        ['评判条数', constraint.criteria_count],
        ['硬约束数', constraint.hard_constraint_count],
        ['软偏好数', constraint.soft_preference_count],
        ['干扰候选数', constraint.distractor_count],
      ], '约束越多、越硬、干扰越强，推荐排序越难。')}
    </div>
    <div class="difficulty-footnote">Rubric: ${esc(profile.rubric_version || '')}</div>
  </div>`;
}

function renderLegacyDifficultyBreakdown(profile) {
  if (!profile || !Object.keys(profile).length) return '';
  const ambiguity = profile.candidate_ambiguity || {};
  const attrs = profile.attribute_coverage || [];
  return `<div class="difficulty-panel legacy">
    <div class="difficulty-panel-head">
      <div>
        <div class="difficulty-panel-title" data-ui-text>难度构成面板</div>
        <div class="difficulty-panel-desc" data-ui-text>
          该样例使用旧版扁平 difficulty_profile，这里保留展示其信息量、属性覆盖、冲突程度和候选歧义。
        </div>
      </div>
      <div class="difficulty-badge ${esc(profile.difficulty || '')}">${esc(profile.difficulty || '-')}</div>
    </div>
    <div class="difficulty-breakdown">
      ${difficultyLayerCard('旧版任务难度摘要', profile.difficulty || '-', '', [
        ['信息数量', profile.info_count ?? '-'],
        ['属性覆盖', attrs.length ? attrs.join(', ') : '-'],
        ['信息质量', profile.info_quality || '-'],
        ['冲突程度', profile.conflict_level || '-'],
        ['干扰候选数', ambiguity.distractor_count ?? '-'],
        ['同类干扰数', ambiguity.same_type_distractors ?? '-'],
      ], '建议用最新 grounding batch 输出查看完整 quality profile。')}
    </div>
  </div>`;
}

function difficultyLayerCard(title, level, rationale, rows, explanation) {
  return `<div class="difficulty-card detailed">
    <div class="difficulty-title" data-ui-text>${esc(title)}</div>
    <div class="difficulty-level ${esc(level || '')}">${esc(level || '-')}</div>
    <div class="difficulty-explain" data-ui-text>${esc(explanation)}</div>
    <div class="difficulty-rationale">${esc(rationale || '')}</div>
    <div class="difficulty-factor-list">
      ${rows.map(([label, value]) => `<div><span data-ui-text>${esc(label)}</span><b>${esc(value ?? '-')}</b></div>`).join('')}
    </div>
  </div>`;
}

function valueAt(obj, path) {
  let cur = obj || {};
  for (const part of path.split('.')) {
    if (!cur || typeof cur !== 'object') return undefined;
    cur = cur[part];
  }
  return cur;
}

function formatMeters(value) {
  if (value === null || value === undefined || value === '') return '-';
  const n = Number(value);
  return Number.isFinite(n) ? `${Math.round(n)}m` : value;
}

function sourceReadonlyBlock(title, value) {
  return `<div class="source-readonly"><div class="detail-label">${esc(title)}</div><pre>${esc(JSON.stringify(value, null, 2))}</pre></div>`;
}

function updateSourceField(field, value) {
  if (!sourceState) sourceState = {};
  sourceState[field] = value;
  if (field === 'review_language') {
    sourceState.review_translation = {
      ...(sourceState.review_translation || {}),
      language: value,
      user_prompt: sourceState.user_prompt_translation || (sourceState.review_translation || {}).user_prompt || '',
    };
  }
}

function updateReviewTranslation(value) {
  if (!sourceState) sourceState = {};
  const language = sourceState.review_language || (sourceState.review_translation || {}).language || 'zh-CN';
  sourceState.user_prompt_translation = value;
  sourceState.review_language = language;
  sourceState.review_translation = {
    ...(sourceState.review_translation || {}),
    language,
    user_prompt: value,
  };
}

function updateSourceJsonField(field, raw) {
  if (!sourceState) sourceState = {};
  try {
    const parsed = JSON.parse(raw || 'null');
    sourceState[field] = parsed;
    if (field === 'quality_profile' && parsed) {
      const display = parsed.display_difficulty || parsed.difficulty;
      if (display) sourceState.difficulty = display;
      sourceState.difficulty_profile = parsed;
    } else if (field === 'difficulty_profile' && parsed && parsed.difficulty) {
      sourceState.difficulty = parsed.difficulty;
    }
  } catch (e) {
    const status = document.getElementById('source-save-status');
    if (status) {
      status.textContent = `${field} JSON 格式错误: ${e.message || e}`;
      status.className = 'save-status err';
    }
  }
}

async function saveSource() {
  const status = document.getElementById('source-save-status');
  if (!currentFolder || !sourceState) {
    if (status) status.textContent = '无可保存的 source';
    return;
  }
  if (status) { status.textContent = '保存中…'; status.className = 'save-status'; }
  try {
    const resp = await fetch(`/api/task/source?folder=${encodeURIComponent(currentFolder)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ example: sourceState }),
    });
    const data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || `HTTP ${resp.status}`);
    if (status) {
      status.textContent = `已保存到源样例 ✓ ${data.example_id || ''}`;
      status.className = 'save-status ok';
    }
  } catch (e) {
    if (status) {
      status.textContent = '保存失败: ' + (e.message || e);
      status.className = 'save-status err';
    }
  }
}

function firstJudgeNodePath(stages) {
  for (let si = 0; si < (stages || []).length; si++) {
    if ((stages[si].nodes || []).length > 0) return { si, ni: 0 };
  }
  return null;
}

function getSelectedNode() {
  if (!selectedJudgeNode || !judgeState) return null;
  const { si, ni } = selectedJudgeNode;
  const stage = (judgeState.stages || [])[si];
  const node = stage ? (stage.nodes || [])[ni] : null;
  return stage && node ? { stage, node, si, ni } : null;
}

function selectJudgeNode(si, ni) {
  selectedJudgeNode = { si, ni };
  renderFlowchart(judgeState.stages);
  rerenderJudge();
}

function renderJudgeInspector() {
  let h = '<div class="section judge-editor">';
  h += '<div class="judge-toolbar">';
  h += '<div class="section-title" style="border:none;margin:0;padding:0" data-ui-text>⚙ 评判节点详情</div>';
  h += '<div class="judge-toolbar-right">';
  h += '<span id="judge-save-status" class="save-status"></span>';
  h += `<button class="btn-save" onclick="saveJudge()" data-ui-text>💾 保存修改</button>`;
  h += '</div></div>';

  if (!judgeState || !judgeState.stages || judgeState.stages.length === 0) {
    h += '<div class="judge-empty" data-ui-text>该任务暂无评判节点</div></div>';
    return h;
  }

  const selected = getSelectedNode();
  if (!selected) {
    h += '<div class="judge-empty" data-ui-text>请在上方流程图中选择一个节点进行编辑。</div></div>';
    return h;
  }

  const { stage, node, si, ni } = selected;
  h += '<div class="judge-hint" data-ui-text>流程图用于浏览结构；点击节点后在这里编辑完整信息。只有带容忍度的检查才显示 tolerance 字段。</div>';
  h += `<div class="judge-stage">`;
  h += `<div class="judge-stage-head"><span class="stage-id">阶段 ${si + 1} · ${esc(stage.stage_id || '')}</span>`;
  if (stage.depends_on && stage.depends_on.length) {
    h += `<span class="dep">依赖 ${esc(stage.depends_on.join(', '))}</span>`;
  }
  h += `</div>`;
  if (stage.description) h += `<div class="judge-stage-desc">${esc(stage.description)}</div>`;
  h += renderJudgeNode(node, si, ni);
  h += `</div>`;
  h += '</div>';
  return h;
}

function renderJudgeNode(node, si, ni) {
  let h = `<div class="judge-node ${node.is_key ? 'key' : ''}">`;
  h += `<div class="jn-head">`;
  h += `<span class="jn-action">${esc(node.agent_action || '(no action)')}</span>`;
  h += `<label class="jn-key"><input type="checkbox" ${node.is_key ? 'checked' : ''} `;
  h += `onchange="updateNode(${si},${ni},'is_key',this.checked)"> 关键节点</label>`;
  h += `</div>`;
  h += `<div class="jn-id">${esc(node.node_id || '')}</div>`;
  h += `<input class="jn-label" placeholder="流程图短标签" value="${attr(node.label_zh || '')}" `;
  h += `oninput="updateNode(${si},${ni},'label_zh',this.value)">`;
  h += `<textarea class="jn-desc" placeholder="节点描述" `;
  h += `oninput="updateNode(${si},${ni},'description_zh',this.value)">${esc(node.description_zh || '')}</textarea>`;
  h += renderChecks(node.hard_checks || [], si, ni, 'hard_checks', '硬性检查（必须满足）', 'hard');
  h += renderChecks(node.soft_checks || [], si, ni, 'soft_checks', '软性检查（加分项）', 'soft');
  h += `</div>`;
  return h;
}

function renderChecks(checks, si, ni, key, title, cls) {
  let h = `<div class="checks-block ${cls}">`;
  h += `<div class="checks-title"><span>${title}</span>`;
  h += `<button class="btn-mini" onclick="addCheck(${si},${ni},'${key}')" data-ui-text>+ 添加检查</button></div>`;
  if (!checks.length) {
    h += `<div class="check-empty" data-ui-text>无</div></div>`;
    return h;
  }
  checks.forEach((c, ci) => {
    h += `<div class="check-row">`;
    h += checkField(si, ni, key, ci, 'check_type', c.check_type, '类型');
    h += checkField(si, ni, key, ci, 'field', c.field, '字段路径');
    h += checkField(si, ni, key, ci, 'expected', fmtVal(c.expected), '期望值');
    if (c.tolerance !== null && c.tolerance !== undefined) {
      h += checkField(si, ni, key, ci, 'tolerance', fmtVal(c.tolerance), '容忍度(JSON)');
      h += `<button class="btn-mini" onclick="removeTolerance(${si},${ni},'${key}',${ci})" data-ui-text>移除容忍度</button>`;
    } else {
      h += `<button class="btn-mini" onclick="addTolerance(${si},${ni},'${key}',${ci})" data-ui-text>+ 容忍度</button>`;
    }
    h += `<button class="btn-del" title="删除该检查" onclick="removeCheck(${si},${ni},'${key}',${ci})">×</button>`;
    h += `</div>`;
  });
  h += `</div>`;
  return h;
}

function checkField(si, ni, key, ci, field, value, placeholder) {
  const v = (value === null || value === undefined) ? '' : String(value);
  return `<input class="check-input ci-${field}" placeholder="${placeholder}" value="${attr(v)}" `
    + `onchange="updateCheck(${si},${ni},'${key}',${ci},'${field}',this.value)">`;
}

function updateNode(si, ni, field, value) {
  judgeState.stages[si].nodes[ni][field] = value;
  if (field === 'is_key' || field === 'label_zh') {
    renderFlowchart(judgeState.stages);
  }
}

function updateCheck(si, ni, key, ci, field, value) {
  judgeState.stages[si].nodes[ni][key][ci][field] = value;
}

function addCheck(si, ni, key) {
  judgeState.stages[si].nodes[ni][key].push({
    check_type: 'action_executed', field: '', expected: null, tolerance: null,
    weight: key === 'hard_checks' ? 'hard' : 'soft',
  });
  rerenderJudge();
}

function addTolerance(si, ni, key, ci) {
  judgeState.stages[si].nodes[ni][key][ci].tolerance = {};
  rerenderJudge();
}

function removeTolerance(si, ni, key, ci) {
  judgeState.stages[si].nodes[ni][key][ci].tolerance = null;
  rerenderJudge();
}

function removeCheck(si, ni, key, ci) {
  judgeState.stages[si].nodes[ni][key].splice(ci, 1);
  rerenderJudge();
}

function rerenderJudge() {
  const wrap = document.getElementById('judge-editor-wrap');
  if (wrap) wrap.innerHTML = renderJudgeInspector();
}

async function saveJudge() {
  const status = document.getElementById('judge-save-status');
  if (!currentFolder) { if (status) status.textContent = '无任务'; return; }
  if (status) { status.textContent = '保存中…'; status.className = 'save-status'; }
  try {
    const payload = normalizeJudge(judgeState);
    const resp = await fetch(`/api/task/judge?folder=${encodeURIComponent(currentFolder)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || `HTTP ${resp.status}`);
    if (status) {
      status.textContent = data.source_synced ? '已保存并同步到源样例 ✓' : '已保存到任务目录 ✓';
      status.className = 'save-status ok';
    }
  } catch (e) {
    if (status) { status.textContent = '保存失败: ' + (e.message || e); status.className = 'save-status err'; }
  }
}

function normalizeJudge(state) {
  const clone = JSON.parse(JSON.stringify(state));
  for (const st of clone.stages || []) {
    for (const nd of st.nodes || []) {
      for (const key of ['hard_checks', 'soft_checks']) {
        for (const c of nd[key] || []) {
          c.expected = parseMaybeJson(c.expected);
          c.tolerance = parseMaybeJson(c.tolerance);
        }
      }
    }
  }
  return clone;
}

function parseMaybeJson(v) {
  if (v === '' || v === null || v === undefined) return null;
  if (typeof v !== 'string') return v;
  try { return JSON.parse(v); } catch (_) { return v; }
}

function fmtVal(v) {
  if (v === null || v === undefined) return '';
  if (typeof v === 'object') return JSON.stringify(v);
  return String(v);
}

function checkLabel(c) {
  if (typeof c === 'string') return c;
  if (!c) return '';
  const parts = [c.check_type || ''];
  if (c.field) parts.push(c.field);
  if (c.expected !== null && c.expected !== undefined && c.expected !== '') {
    parts.push('= ' + fmtVal(c.expected));
  }
  if (c.tolerance) parts.push('± ' + fmtVal(c.tolerance));
  return parts.filter(Boolean).join(' ');
}

function renderGroundingImage(task, groundingGold = {}, worldSlice = {}) {
  if ((task.task_type || '') !== 'place_grounding') return '';
  const assets = (task.summary || {}).input_assets || {};
  const imagePath = assets.image_path || assets.resolved_image_path || '';
  const promptReferencePath = assets.prompt_reference_image_path || '';
  const taskFolder = task.folder_ref || task.folder || currentFolder || '';
  const gold = (task.summary || {}).gold_summary || {};
  const coord = gold.coordinate || {};
  if (!imagePath && (!coord.lat || !coord.lng)) return '';
  const imageUrl = `/tasks/api/asset?path=${encodeURIComponent(imagePath)}&folder=${encodeURIComponent(taskFolder)}`;
  const lat = Number(coord.lat);
  const lng = Number(coord.lng);
  const hasMap = Number.isFinite(lat) && Number.isFinite(lng);
  const goldPlaceId = ((groundingGold.gold || {}).place_id) || '';
  const firstCharacter = ((worldSlice.characters || [])[0] || {});
  const goldCandidateId = goldPlaceId || (firstCharacter.character_id ? `map_location:${firstCharacter.character_id}` : '');
  return `
    <div class="section">
      <div class="section-title" data-ui-text>Grounding 输入与答案位置</div>
      <div class="grounding-context-grid">
        ${hasMap ? `
          <div class="grounding-map-card">
            <div class="grounding-card-head">
              <div>
                <b>${esc(gold.place_name || 'Gold Location')}</b>
                <span>${esc((gold.anchor_places || []).join(' / '))}</span>
              </div>
              <code>${lat.toFixed(6)}, ${lng.toFixed(6)}</code>
            </div>
            <div id="grounding-map-preview" class="grounding-map-preview"
              data-lat="${lat}" data-lng="${lng}" data-name="${attr(gold.place_name || 'Gold Location')}"
              data-place-id="${attr(goldCandidateId)}"></div>
            <div id="grounding-map-status" class="map-status" data-ui-text>加载 Bench 地图底图...</div>
          </div>` : ''}
        ${imagePath ? `
          <div class="grounding-image-card">
            <img src="${imageUrl}" alt="place grounding input image" loading="lazy">
            <div class="asset-path">${esc(imagePath)}</div>
            ${promptReferencePath ? `<div class="asset-path muted">Prompt reference: ${esc(promptReferencePath)}</div>` : ''}
          </div>` : ''}
      </div>
    </div>`;
}

async function initGroundingPreviewMap(task, worldSlice = {}) {
  const el = document.getElementById('grounding-map-preview');
  if (!el || !window.L) return;
  const lat = Number(el.dataset.lat);
  const lng = Number(el.dataset.lng);
  const gold = (task.summary || {}).gold_summary || {};
  if (!Number.isFinite(lat) || !Number.isFinite(lng)) return;
  if (groundingPreviewMap) {
    groundingPreviewMap.stop();
    groundingPreviewMap.remove();
    groundingPreviewMap = null;
  }

  groundingPreviewMap = L.map(el, {
    zoomControl: true,
    attributionControl: true,
    scrollWheelZoom: false,
  }).setView([lat, lng], 15);

  const map = groundingPreviewMap;
  const baseLayer = await createBenchBasemapLayer();
  if (groundingPreviewMap !== map || !el.isConnected) return;
  baseLayer.addTo(map);

  const goldPlaceId = el.dataset.placeId || '';
  const layerInfo = await addBenchPointLayer(
    map, lat, lng, goldPlaceId, worldSlice, task.folder_ref || ''
  );
  if (groundingPreviewMap !== map || !el.isConnected) return;

  const marker = L.marker([lat, lng], {
    title: gold.place_name || 'Gold Location',
    zIndexOffset: 1000,
  }).addTo(map);
  marker.bindPopup(`<strong>${esc(gold.place_name || 'Gold Location')}</strong><br>${lat.toFixed(6)}, ${lng.toFixed(6)}`);
  marker.openPopup();

  if (!layerInfo.goldFound) {
    makeBenchPointMarker(lat, lng, true, gold.place_name || 'Gold Location', 'gold location not found in Bench points').addTo(map);
  }

  if (layerInfo.bounds && layerInfo.bounds.isValid()) {
    map.fitBounds(layerInfo.bounds.pad(0.18), { maxZoom: 14, animate: false });
  }
  setTimeout(() => { if (groundingPreviewMap === map && el.isConnected) map.invalidateSize(); }, 0);
}

async function addBenchPointLayer(map, goldLat, goldLng, goldPlaceId = '', worldSlice = {}, taskFolder = '') {
  const data = await loadBenchWorldPoints(taskFolder);
  if (!map.getContainer().isConnected) return {goldFound:false, bounds:null};
  const places = [...(data.places || []), ...taskGroundingPoints(worldSlice)];
  const contextRadiusM = 8000;
  const visible = places
    .map(p => ({ ...p, distance_m: distanceMeters(goldLat, goldLng, Number(p.lat), Number(p.lng)) }))
    .filter(p => Number.isFinite(p.distance_m) && p.distance_m <= contextRadiusM)
    .sort((a, b) => a.distance_m - b.distance_m)
    .slice(0, 450);

  const group = L.layerGroup().addTo(map);
  let goldFound = false;
  let goldPoint = null;
  const bounds = L.latLngBounds([[goldLat, goldLng]]);
  for (const p of visible) {
    const isGold = Boolean(goldPlaceId && p.id === goldPlaceId);
    if (isGold) goldFound = true;
    const marker = makeBenchPointMarker(Number(p.lat), Number(p.lng), isGold, p.name || p.id, p.type || '');
    if (isGold) {
      goldPoint = marker;
    } else {
      marker.addTo(group);
    }
    bounds.extend([Number(p.lat), Number(p.lng)]);
    marker.bindPopup(`<strong>${esc(p.name || p.id)}</strong><br>${esc(p.type || '')}<br>${Math.round(p.distance_m)}m from gold`);
  }
  if (goldPoint) goldPoint.addTo(group);
  const status = document.getElementById('grounding-map-status');
  if (status) {
    if (data.error) {
      status.textContent = `任务绑定地图不可用：${data.error}`;
    } else {
      const city = data.city_name || data.city_id || 'Unknown city';
      const version = data.city_version || '';
      const total = (data.places || []).length;
      status.textContent = `任务绑定地图：${city}${version ? ` · ${version}` : ''} · 本地 POI ${total} 个 · 附近 ${visible.length} 个 · ${goldFound ? 'gold 已高亮' : 'gold 不在本地 POI 中'}`;
    }
  }
  return { goldFound, bounds, count: visible.length };
}

function taskGroundingPoints(worldSlice = {}) {
  return (worldSlice.characters || [])
    .map(character => {
      const mobility = character.mobility || {};
      const lat = Number(mobility.lat);
      const lng = Number(mobility.lng);
      if (!Number.isFinite(lat) || !Number.isFinite(lng) || !character.character_id) return null;
      return {
        id: `map_location:${character.character_id}`,
        name: '地图候选位置',
        type: 'candidate_location',
        lat,
        lng,
      };
    })
    .filter(Boolean);
}

function makeBenchPointMarker(lat, lng, isGold, title, type) {
  return L.circleMarker([lat, lng], {
    radius: isGold ? 8 : 4.5,
    fillColor: isGold ? '#e94560' : '#3388ff',
    color: '#fff',
    weight: isGold ? 2.5 : 1,
    opacity: 0.95,
    fillOpacity: isGold ? 0.95 : 0.82,
    pane: 'markerPane',
  }).bindTooltip(`${esc(title)}${type ? ` · ${esc(type)}` : ''}`, { direction: 'top', offset: [0, -6] });
}

async function loadBenchWorldPoints(taskFolder = '') {
  const cacheKey = taskFolder || '__legacy__';
  if (benchWorldPoints.has(cacheKey)) return benchWorldPoints.get(cacheKey);
  try {
    const query = taskFolder ? `?folder=${encodeURIComponent(taskFolder)}` : '';
    const resp = await fetch(`/api/world/points${query}`);
    const payload = await resp.json();
    if (!resp.ok) throw new Error(payload.error || payload.message || `HTTP ${resp.status}`);
    benchWorldPoints.set(cacheKey, payload);
  } catch (err) {
    console.warn('Failed to load Bench world points.', err);
    benchWorldPoints.set(cacheKey, { places: [], actors: [], error: err.message || 'Unknown error' });
  }
  return benchWorldPoints.get(cacheKey);
}

function distanceMeters(lat1, lng1, lat2, lng2) {
  if (![lat1, lng1, lat2, lng2].every(Number.isFinite)) return NaN;
  const toRad = x => x * Math.PI / 180;
  const dLat = toRad(lat2 - lat1);
  const dLng = toRad(lng2 - lng1);
  const a = Math.sin(dLat / 2) ** 2
    + Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLng / 2) ** 2;
  return 6371000 * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

async function createBenchBasemapLayer() {
  const status = document.getElementById('grounding-map-status');
  if ((viewerConfig.preferred_basemap || '') === 'google' && viewerConfig.google_maps_api_key) {
    try {
      await ensureGoogleBasemap();
      if (window.L.gridLayer?.googleMutant) {
        if (status) status.textContent = '当前底图：Google Maps（Bench viewer 同款）';
        return L.gridLayer.googleMutant({ type: 'roadmap', maxZoom: 21 });
      }
    } catch (err) {
      console.warn('Google basemap unavailable for grounding preview.', err);
    }
  }
  if (status) status.textContent = 'Google Maps 不可用，已回退 OSM。';
  return L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '&copy; OpenStreetMap contributors',
  });
}

async function ensureGoogleBasemap() {
  if (googleBasemapReady) return;
  if (!googleBasemapPromise) {
    const key = viewerConfig.google_maps_api_key || '';
    googleBasemapPromise = loadScript(`https://maps.googleapis.com/maps/api/js?key=${encodeURIComponent(key)}&v=weekly`)
      .then(() => loadScript(GOOGLE_MUTANT_SCRIPT))
      .then(() => {
        if (!window.L.gridLayer?.googleMutant) {
          throw new Error('Leaflet GoogleMutant plugin did not register.');
        }
        googleBasemapReady = true;
      });
  }
  await googleBasemapPromise;
}

function loadScript(src) {
  if ([...document.scripts].some(s => s.src === src)) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const script = document.createElement('script');
    script.src = src;
    script.async = true;
    script.onload = resolve;
    script.onerror = () => reject(new Error(`Failed to load script: ${src}`));
    document.head.appendChild(script);
  });
}

// ========== D3 FLOWCHART ==========

function renderFlowchart(stages) {
  const slot = document.getElementById('graph-slot') || document.getElementById('task-detail');

  if (!stages || stages.length === 0) {
    slot.innerHTML =
      '<div class="section"><div class="section-title" data-ui-text>⚖ 评判节点序列</div><div style="color:var(--text2);font-size:13px" data-ui-text>无评判节点</div></div>';
    return;
  }

  // Build graph data: nodes = individual judge nodes, edges = depends_on
  const graphNodes = [];
  const graphEdges = [];
  const nodeIdToIdx = {};
  let nid = 0;

  for (let si = 0; si < stages.length; si++) {
    const stage = stages[si];
    const nodes = stage.nodes || [];
    for (let ni = 0; ni < nodes.length; ni++) {
      const node = nodes[ni];
      const id = node.node_id || `node_${nid}`;
      const idx = nid++;
      nodeIdToIdx[id] = idx;
      graphNodes.push({
        id, idx,
        stageId: stage.stage_id,
        stageDesc: stage.description || '',
        stageIdx: si,
        nodeIdx: ni,
        agentAction: (node.agent_action || {}).function || node.agent_action || '',
        desc: node.description_zh || '',
        label: node.label_zh || '',
        role: node.role || '',
        isKey: !!node.is_key,
        hardChecks: node.hard_checks || [],
        softChecks: node.soft_checks || [],
        nodeDeps: node.depends_on || [],
      });
    }
  }

  // Build edges from depends_on
  for (const node of graphNodes) {
    for (const depId of node.nodeDeps) {
      const targetIdx = nodeIdToIdx[depId];
      if (targetIdx !== undefined && targetIdx !== node.idx) {
        graphEdges.push({ source: targetIdx, target: node.idx });
      }
    }
  }
  // Stage-level edges (connect last node of stage N to first node of stage N+dep)
  for (let si = 0; si < stages.length; si++) {
    const stage = stages[si];
    const stageDeps = stage.depends_on || [];
    for (const depStageId of stageDeps) {
      const depIdx = stages.findIndex(s => s.stage_id === depStageId);
      if (depIdx >= 0) {
        const depLastNode = findLastNodeInStage(graphNodes, depIdx);
        const curFirstNode = findFirstNodeInStage(graphNodes, si);
        if (depLastNode >= 0 && curFirstNode >= 0 && depLastNode !== curFirstNode) {
          // Add only if not already present
          const exists = graphEdges.some(e => e.source === depLastNode && e.target === curFirstNode);
          if (!exists) {
            graphEdges.push({ source: depLastNode, target: curFirstNode });
          }
        }
      }
    }
  }

// Create SVG
const wrap = document.createElement('div');
  wrap.id = 'graph-wrap';
  wrap.innerHTML = `
    <div class="section"><div class="section-title" data-ui-text>⚖ 评判节点流程图</div></div>
    <div id="graph-container">
      <svg id="graph-svg"></svg>
      <div class="legend-row" id="legend-row"></div>
    </div>`;
  slot.innerHTML = '';
  slot.appendChild(wrap);

  const svgEl = d3.select('#graph-svg');
  const container = document.getElementById('graph-container');
  const width = container.clientWidth;
  const nodeCount = graphNodes.length;

  // Layout: tiered by stage
  const nodeW = 170, nodeH = 70;
  const paddingX = 40, paddingY = 64;
  const minColSpan = 230;

  // Group nodes by stageIdx
  const stageGroups = {};
  for (const n of graphNodes) {
    if (!stageGroups[n.stageIdx]) stageGroups[n.stageIdx] = [];
    stageGroups[n.stageIdx].push(n);
  }

  // Compute layout — columns distributed evenly across the available width.
  const stageKeys = Object.keys(stageGroups).map(Number).sort((a, b) => a - b);
  const ordinalOf = {};
  stageKeys.forEach((si, oi) => { ordinalOf[si] = oi; });
  const maxNodesInStage = Math.max(...stageKeys.map(k => stageGroups[k].length), 1);
  const colCount = Math.max(stageKeys.length, 1);
  const svgWidth = Math.max(width, colCount * minColSpan + paddingX * 2);
  const colSpan = (svgWidth - paddingX * 2) / colCount;
  const svgHeight = maxNodesInStage * (nodeH + 20) + paddingY * 2 + 30;

  svgEl.attr('width', svgWidth).attr('height', svgHeight);

  // Assign positions
  const positions = {};
  for (const si of stageKeys) {
    const nodes = stageGroups[si];
    const cx = paddingX + colSpan * (ordinalOf[si] + 0.5);
    const totalH = nodes.length * (nodeH + 20);
    const startY = (svgHeight - totalH) / 2;
    for (let j = 0; j < nodes.length; j++) {
      positions[nodes[j].idx] = {
        x: cx - nodeW / 2,
        y: startY + j * (nodeH + 20) + 18,
        stageIdx: si,
      };
    }
  }

  // Draw
  const g = svgEl.append('g');

  // Edges
  for (const edge of graphEdges) {
    const src = positions[edge.source];
    const tgt = positions[edge.target];
    if (!src || !tgt) continue;

    // Path: from right side of source to left side of target
    const sx = src.x + nodeW, sy = src.y + nodeH / 2;
    const tx = tgt.x, ty = tgt.y + nodeH / 2;
    const mx = (sx + tx) / 2;

    const path = d3.path();
    path.moveTo(sx, sy);
    path.bezierCurveTo(mx, sy, mx, ty, tx, ty);

    g.append('path')
      .attr('d', path.toString())
      .attr('fill', 'none')
      .attr('stroke', 'var(--text2)')
      .attr('stroke-width', 2)
      .attr('stroke-opacity', 0.5)
      .attr('marker-end', 'url(#arrow)');
  }

  // Arrow marker
  g.append('defs').append('marker')
    .attr('id', 'arrow')
    .attr('viewBox', '0 -5 10 10')
    .attr('refX', 8)
    .attr('refY', 0)
    .attr('markerWidth', 6)
    .attr('markerHeight', 6)
    .attr('orient', 'auto')
    .append('path')
    .attr('d', 'M0,-5L10,0L0,5')
    .attr('fill', 'var(--text2)');

  // Stage label headers
  for (const si of stageKeys) {
    const nx = paddingX + colSpan * (ordinalOf[si] + 0.5);
    const stage = stages[si];
    g.append('text')
      .attr('x', nx)
      .attr('y', paddingY / 2 - 4)
      .attr('text-anchor', 'middle')
      .attr('fill', 'var(--accent)')
      .attr('font-size', 12)
      .attr('font-weight', 700)
      .text(shortText(stage.stage_id || `stage_${si + 1}`, 22));
  }

  // Nodes
  for (const n of graphNodes) {
    const pos = positions[n.idx];
    if (!pos) continue;
    const color = STAGE_COLORS[n.role] || STAGE_COLORS[n.stageId] || STAGE_COLORS.fallback;
    const selected = selectedJudgeNode && selectedJudgeNode.si === n.stageIdx && selectedJudgeNode.ni === n.nodeIdx;

    const nodeG = g.append('g')
      .attr('class', `graph-node ${selected ? 'selected' : ''}`)
      .style('cursor', 'pointer');

    // Rounded rect
    nodeG.append('rect')
      .attr('x', pos.x)
      .attr('y', pos.y)
      .attr('width', nodeW)
      .attr('height', nodeH)
      .attr('rx', 8).attr('ry', 8)
      .attr('fill', 'var(--bg2)')
      .attr('stroke', color)
      .attr('stroke-width', selected ? 3 : 2);

    // Compact label only. Full text lives in the inspector/tooltip.
    const label = shortText(compactNodeLabel(n), 12);
    nodeG.append('text')
      .attr('x', pos.x + nodeW / 2)
      .attr('y', pos.y + 25)
      .attr('text-anchor', 'middle')
      .attr('dominant-baseline', 'middle')
      .attr('fill', 'var(--text)')
      .attr('font-size', 13)
      .attr('font-weight', 700)
      .text(label);

    const checkCount = (n.hardChecks || []).length + (n.softChecks || []).length;
    const meta = `${n.isKey ? '关键 · ' : ''}${checkCount} checks`;
    nodeG.append('text')
      .attr('x', pos.x + nodeW / 2)
      .attr('y', pos.y + 48)
      .attr('text-anchor', 'middle')
      .attr('dominant-baseline', 'middle')
      .attr('fill', n.isKey ? 'var(--green)' : 'var(--text2)')
      .attr('font-size', 11)
      .text(meta);

    // Hover: tooltip
    nodeG.on('mouseenter', (event) => {
      showTooltip(event, n, color);
    });
    nodeG.on('mouseleave', hideTooltip);
    nodeG.on('click', () => selectJudgeNode(n.stageIdx, n.nodeIdx));
  }

  // Legend
  const legendEl = document.getElementById('legend-row');
  const uniqueStages = [...new Set(graphNodes.map(n => n.stageId))];
  legendEl.innerHTML = uniqueStages.map(sid => `
    <div class="litem">
      <span class="legend-swatch" style="background:${STAGE_COLORS[sid] || STAGE_COLORS.fallback}"></span>
      ${sid}
    </div>`).join('');
}

function showTooltip(event, node, color) {
  const tip = document.getElementById('tooltip');
  tip.innerHTML = `
    <div class="tt-action" style="color:${color}">${esc(node.agentAction)}</div>
    <div class="tt-desc">${esc(node.desc)}</div>
    <div class="tt-checks">
      ${(node.hardChecks || []).map(c => `<span class="hard">● ${esc(checkLabel(c))}</span>`).join('')}
      ${(node.softChecks || []).map(c => `<span class="soft">○ ${esc(checkLabel(c))}</span>`).join('')}
    </div>`;
  tip.classList.remove('hidden');
  let tx = event.pageX + 15;
  let ty = event.pageY - 10;
  if (tx + 360 > window.innerWidth) tx = event.pageX - 375;
  if (ty + 120 > window.innerHeight) ty = event.pageY - 130;
  tip.style.left = tx + 'px';
  tip.style.top = ty + 'px';
}

function hideTooltip() {
  document.getElementById('tooltip').classList.add('hidden');
}

function compactNodeLabel(node) {
  if (node.label) return node.label;
  const action = node.agentAction || '';
  if (action.includes('search_places')) return '区域检索';
  if (action.includes('search_services')) return '候选检索';
  if (action.includes('get_nearby')) return '附近搜索';
  if (action.includes('send_message_to_user')) return '结果说明';
  return node.desc || node.stageId || '评判节点';
}

function shortText(text, maxLen) {
  const s = String(text || '');
  return s.length > maxLen ? `${s.slice(0, maxLen - 1)}…` : s;
}

function findFirstNodeInStage(graphNodes, stageIdx) {
  for (let i = 0; i < graphNodes.length; i++) {
    if (graphNodes[i].stageIdx === stageIdx) return i;
  }
  return -1;
}

function findLastNodeInStage(graphNodes, stageIdx) {
  let last = -1;
  for (let i = 0; i < graphNodes.length; i++) {
    if (graphNodes[i].stageIdx === stageIdx) last = i;
  }
  return last;
}

// ========== ENV EVENTS ==========

function renderEnvEvents(env) {
  if (!env || env.length === 0) return '';
  let h = '<div class="section"><div class="section-title" data-ui-text>🌧 环境事件节点</div><div class="env-grid">';
  for (const e of env) {
    const scope = e.scope || {};
    const activation = e.activation || {};
    const isDynamicV2 = Boolean(e.event_id || e.activation_time || e.state_patch);
    const label = e.event_type || e.category || 'event';
    const activationText = e.activation_time || activation.time || (activation.mode === 'on_start' ? '任务开始' : '未指定');
    h += `<div class="env-card">
      <div class="cat">${esc(label)} · ${esc(scope.type || e.scope_type || '')} · ${esc(scope.id || e.scope_id || '')}</div>
      <div class="desc">${esc(e.description_zh || '')}</div>
      <div class="prob">${isDynamicV2 ? `触发: ${esc(activationText)} · ${esc(activation.mode || 'absolute')}` : `触发概率 ${Math.round((e.trigger_probability || 0) * 100)}%`}</div>`;
    if (isDynamicV2 && e.state_patch) {
      h += `<div class="event-state">状态变更: ${esc(JSON.stringify(e.state_patch))}</div>`;
    }
    if (isDynamicV2 && (e.causal_parents || []).length) {
      h += `<div class="event-state">因果前置: ${esc(e.causal_parents.join(', '))}</div>`;
    }
    if (e.effects_summary && e.effects_summary.length > 0) {
      h += '<div style="font-size:11px;color:var(--text2);margin-top:4px">' +
        e.effects_summary.map(s => `· ${esc(s)}`).join('<br>') + '</div>';
    }
    h += '</div>';
  }
  h += '</div></div>';
  return h;
}

// ========== WORLD SLICE ==========

function renderWorldSlice(ws) {
  let h = '<div class="section"><div class="section-title" data-ui-text>👤 参与人物 & 地点</div>';
  h += '<div class="ws-grid">';

  // Characters
  const chars = ws.characters || [];
  h += '<div>';
  for (const c of chars) {
    h += `<div class="item-row">
      <div class="avatar char">${(c.name||'?')[0]}</div>
      <div class="info">
        <div class="name">${esc(c.name)} · ${c.age || ''}岁 · ${esc(c.occupation || '')}</div>
        <div class="detail">📍 ${esc(c.home_district || '')}`;
    if (c.preferences) {
      const p = c.preferences;
      const parts = [];
      if (p.prefer && p.prefer.length) parts.push('偏好:' + p.prefer.join(','));
      if (p.avoid && p.avoid.length) parts.push('避讳:' + p.avoid.join(','));
      if (p.dietary) parts.push('饮食:' + p.dietary);
      if (parts.length) h += ' · ' + parts.join('; ');
    }
    h += '</div>';
    if (c.constraints && c.constraints.length) {
      for (const ct of c.constraints) {
        h += `<div class="extra"><span class="${(ct.type||'').includes('hidden')?'ct-hidden':''}">` +
          `${(ct.type||'').includes('hidden') ? '🔒' : '📋'} ${esc(ct.content)}</span></div>`;
      }
    }
    if (c.relationships && c.relationships.length) {
      h += `<div class="extra" style="color:var(--green)">🔗 ${c.relationships.join(', ')}</div>`;
    }
    h += '</div></div>';
  }
  h += '</div>';

  // Places + Districts
  h += '<div>';
  const places = ws.places || [];
  for (const p of places) {
    h += `<div class="item-row">
      <div class="avatar place">📍</div>
      <div class="info">
        <div class="name">${esc(p.name)}</div>
        <div class="detail">${esc(p.type||'')} · ${esc(p.district||'')}` +
        (p.rating ? ` · ⭐${p.rating}` : '') + '</div>';
    if (p.address) h += `<div class="detail" style="font-size:10px">${esc(p.address).slice(0,60)}</div>`;
    h += '</div></div>';
  }

  const districts = ws.districts || [];
  for (const d of districts) {
    if (d.name) {
      h += `<div class="item-row">
        <div class="avatar dist">🗺</div>
        <div class="info">
          <div class="name">${esc(d.name)}</div>
          <div class="detail">${(d.role_tags||[]).join(', ')}</div>
        </div></div>`;
    }
  }
  h += '</div>';
  h += '</div></div>';
  return h;
}

// ========== HELPERS ==========

function card(val, label) {
  return `<div class="summary-card"><div class="val">${val}</div><div class="lbl" data-ui-text>${label}</div></div>`;
}

function hasTokenUsage(usage) {
  usage = normalizeTokenUsage(usage);
  return Boolean(usage && (
    Number(usage.total_tokens || 0) ||
    Number(usage.prompt_tokens || 0) ||
    Number(usage.completion_tokens || 0) ||
    Number(usage.request_count || 0)
  ));
}

function normalizeTokenUsage(usage) {
  const value = usage || {};
  return {
    ...value,
    prompt_tokens: value.prompt_tokens ?? value.prompt ?? 0,
    completion_tokens: value.completion_tokens ?? value.completion ?? 0,
    total_tokens: value.total_tokens ?? value.total ?? 0,
  };
}

function renderTokenInline(usage) {
  usage = normalizeTokenUsage(usage);
  if (!hasTokenUsage(usage)) return '';
  return ` · ${formatTokenNumber(usage.total_tokens || 0)} tok`;
}

function formatTokenNumber(value) {
  const n = Number(value || 0);
  if (!Number.isFinite(n)) return '0';
  return new Intl.NumberFormat('en-US').format(Math.round(n));
}

function esc(s) {
  if (!s) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function attr(s) {
  if (s === null || s === undefined) return '';
  return String(s).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// ========== ROUTE PLANNING REVIEW PANEL ==========

let rpAllRoutes = [];           // route_cache routes (array)
let rpWorldPlaces = [];         // world.json places
let rpCachedPlaces = [];        // cached_places from route_cache
let rpRouteGraph = {};          // {origin_id: {dest_id: [mode, ...]}}
let rpCurrentLayers = {bg:[], places:[], task:[], query:[]};
let rpQueryHighlight = {};

function clearRPLayer(name) {
  for (const l of rpCurrentLayers[name] || []) taskMap && taskMap.removeLayer(l);
  rpCurrentLayers[name] = [];
}

function findWorldPlace(pid) {
  return rpWorldPlaces.find(p => p.place_id === pid) || null;
}

async function loadRPData() {
  if (rpAllRoutes.length > 0) return;
  try {
    const [wr, rr, cp] = await Promise.all([
      fetch('/api/world/full'),
      fetch('/api/routes'),
      fetch('/api/cached_places'),
    ]);
    const w = await wr.json();
    rpWorldPlaces = w.places || [];
    const rc = await rr.json();
    const arr = rc.cache || rc.routes || [];
    rpAllRoutes = Array.isArray(arr) ? arr : Object.values(arr);
    rpCachedPlaces = (await cp.json()).places || [];
  } catch(e) {
    if (!rpWorldPlaces.length) rpWorldPlaces = [];
    if (!rpAllRoutes.length) rpAllRoutes = [];
    if (!rpCachedPlaces.length) rpCachedPlaces = [];
  }
  rpRouteGraph = {};
  for (const r of rpAllRoutes) {
    const rid = r.route_id || r.routeId || '';
    const oid = r.origin_place_id, did = r.dest_place_id;
    if (!oid || !did) continue;
    if (!rpRouteGraph[oid]) rpRouteGraph[oid] = {};
    if (!rpRouteGraph[oid][did]) rpRouteGraph[oid][did] = [];
    const m = r.mode || '';
    if (!rpRouteGraph[oid][did].includes(m)) rpRouteGraph[oid][did].push(m);
  }
}

let rpBgRoutesCreated = false;

function renderRPBgRoutes() {
  if (rpBgRoutesCreated) return;
  clearRPLayer('bg');
  if (!taskMap) return;
  for (const r of rpAllRoutes) {
    const pts = typeof decodeRoutePolyline === 'function' ? decodeRoutePolyline(r) : [];
    if (pts.length >= 2) {
      const poly = L.polyline(pts, {color:'#3a3a5c',weight:1,opacity:0.35}).addTo(taskMap);
      rpCurrentLayers.bg.push(poly);
    }
  }
  rpBgRoutesCreated = true;
}

function renderRPPlaces() {
  clearRPLayer('places');
  if (!taskMap) return;
  for (const p of rpWorldPlaces) {
    if (!p.lat) continue;
    const m = L.circleMarker([p.lat, p.lng], {radius:3,color:'#888',fillColor:'#556',fillOpacity:0.6}).addTo(taskMap);
    m.bindTooltip(`${p.name}<br>${p.primary_type||''}`);
    rpCurrentLayers.places.push(m);
  }
}

function renderRPPOIQueryDropdowns() {
  const fromSel = document.getElementById('rp-poi-from');
  const wpSel = document.getElementById('rp-poi-waypoint');
  const toSel = document.getElementById('rp-poi-to');
  if (!fromSel) return;
  const opts = rpCachedPlaces.map(p =>
    `<option value="${escAttr(p.place_id)}">${p.cached_routes > 0 ? '●' : '○'} ${esc(p.name)} (${p.cached_routes})</option>`
  ).join('');
  fromSel.innerHTML = `<option value="" data-ui-text>-- 出发 --</option>${opts}`;
  wpSel.innerHTML = `<option value="" data-ui-text>-- 无途经 --</option>${opts}`;
  toSel.innerHTML = `<option value="" data-ui-text>-- 到达 --</option>${opts}`;
}

function onRPPoiChange(role) {
  clearRPLayer('task');
  if (rpQueryHighlight[role]) { taskMap.removeLayer(rpQueryHighlight[role]); rpQueryHighlight[role] = null; }
  const sel = document.getElementById(`rp-poi-${role}`);
  const id = sel.value;
  if (!id) {
    if (role === 'from') { renderRPPOIQueryDropdowns(); }
    return;
  }
  const place = findWorldPlace(id);
  if (!place || !place.lat) return;
  const marker = L.circleMarker([place.lat, place.lng], {radius:8,color:role==='from'?'#3388ff':role==='to'?'#e67e22':'#e67e22',fillOpacity:0.8}).addTo(taskMap);
  const label = L.marker([place.lat, place.lng], {icon: L.divIcon({className:'rp-label',html:`<div style="background:${role==='from'?'#3388ff':'#e67e22'};color:#fff;padding:2px 4px;border-radius:3px;font-size:10px">${role==='from'?'出发':role==='to'?'到达':'途经'}</div>`,iconSize:[40,16]})}).addTo(taskMap);
  rpQueryHighlight[role] = L.layerGroup([marker, label]).addTo(taskMap);
  taskMap.panTo([place.lat, place.lng]);
  // Cascade: rebuild downstream dropdowns based on reachable destinations
  if (role === 'from') {
    const reachable = rpRouteGraph[id] || {};
    const wpSel2 = document.getElementById('rp-poi-waypoint');
    const toSel2 = document.getElementById('rp-poi-to');
    const oldWp = wpSel2.value, oldTo = toSel2.value;
    const reachableOpts = rpCachedPlaces.filter(p => reachable[p.place_id]).map(p =>
      `<option value="${escAttr(p.place_id)}">● ${esc(p.name)} (${Object.keys(reachable[p.place_id]).length}模式)</option>`
    ).join('');
    wpSel2.innerHTML = `<option value="" data-ui-text>-- 无途经 --</option>${reachableOpts}`;
    toSel2.innerHTML = `<option value="" data-ui-text>-- 到达 --</option>${reachableOpts}`;
    if (oldWp && reachable[oldWp]) wpSel2.value = oldWp;
    if (oldTo && reachable[oldTo]) toSel2.value = oldTo;
  }
  rpUpdateModeDropdown();
}

function rpUpdateModeDropdown() {
  const from = document.getElementById('rp-poi-from')?.value || '';
  const to = document.getElementById('rp-poi-to')?.value || '';
  const s = document.getElementById('rp-poi-mode');
  if (!s) return;
  if (from && to && rpRouteGraph[from] && rpRouteGraph[from][to]) {
    const modes = rpRouteGraph[from][to];
    const cur = s.value;
    s.innerHTML = `<option value="" data-ui-text>全部</option>` + modes.map(m => `<option value="${escAttr(m)}">${esc(m)}</option>`).join('');
    if (modes.includes(cur)) s.value = cur;
  } else {
    s.innerHTML = '<option value="" data-ui-text>全部</option><option>WALK</option><option>DRIVE</option><option>TRANSIT</option>';
  }
}

async function rpPOIQuery() {
  const from = document.getElementById('rp-poi-from').value;
  const to = document.getElementById('rp-poi-to').value;
  const wp = document.getElementById('rp-poi-waypoint').value;
  const mode = document.getElementById('rp-poi-mode').value;
  if (!from || !to) return;
  let url = `/api/routes?from=${from}&to=${to}`;
  if (mode && mode !== 'ALL') url += `&mode=${mode}`;
  if (wp) url += `&waypoint=${wp}`;
  const r = await fetch(url);
  const data = await r.json();
  clearRPLayer('query');
  const bounds = [];
  for (const rt of (data.routes||[])) {
    const pts = typeof decodeRoutePolyline === 'function' ? decodeRoutePolyline(rt) : [];
    if (pts.length >= 2) {
      const poly = L.polyline(pts, {color:'#e67e22',weight:3,opacity:0.9}).addTo(taskMap);
      poly.bindTooltip(`${rt.origin_name||''}→${rt.dest_name||''} | ${rt.mode||''} | ${rt.distance_km||0}km | ${rt.duration_min||0}min`);
      rpCurrentLayers.query.push(poly);
      bounds.push(...pts);
    }
  }
  for (const seg of (data.segments||[])) {
    for (const rt of (seg.routes||[])) {
      const pts = typeof decodeRoutePolyline === 'function' ? decodeRoutePolyline(rt) : [];
      if (pts.length >= 2) {
        const poly = L.polyline(pts, {color:'#e67e22',weight:3,opacity:0.8,dashArray:'6,3'}).addTo(taskMap);
        rpCurrentLayers.query.push(poly);
        bounds.push(...pts);
      }
    }
  }
  if (bounds.length) taskMap.fitBounds(bounds, {padding:[30,30]});

  // Render result cards
  const panel = document.getElementById('rp-query-result');
  if (!panel) return;
  const directRoutes = data.routes || [];
  let rh = '';
  if (directRoutes.length) {
    rh += `<div><b>直达路线 (${directRoutes.length}条)</b><div style="display:flex;flex-wrap:wrap;gap:4px;margin-top:4px">`;
    for (const rt of directRoutes) {
      rh += `<div style="padding:3px 6px;background:#1e1e2e;border-radius:3px;font-size:10px;border-left:3px solid #e67e22">${rt.mode||''} ${(rt.distance_km||0).toFixed(1)}km ${Math.round((rt.duration_s||0)/60)}m</div>`;
    }
    rh += '</div></div>';
  }
  if ((data.segments||[]).length) {
    rh += `<div style="margin-top:4px"><b data-ui-text>途经分段</b>`;
    for (const seg of data.segments||[]) {
      rh += `<div style="margin:2px 0;font-size:10px;color:#a6adc8">${seg.label||''}: ${(seg.routes||[]).length}条</div>`;
    }
    rh += '</div>';
  }
  panel.innerHTML = rh || '<div style="color:#a6adc8" data-ui-text>无匹配路线</div>';
}

function renderRPTaskMap(tk, data) {
  const sb = data.route_sandbox || data.task_sandbox || {};
  clearRPLayer('task');
  if (!taskMap) return;
  const bounds = [];
  if (sb.core_places) {
    for (const p of sb.core_places) {
      const lat = p.lat, lng = p.lng;
      if (!lat) continue;
      const isWaypoint = p.role === 'waypoint';
      const color = isWaypoint ? '#e67e22' : '#3388ff';
      const radius = isWaypoint ? 7 : 8;
      const label = p.role === 'origin' ? '起点' : p.role === 'destination' ? '终点' : '途经';
      L.circleMarker([lat, lng], {radius:radius,color:color,fillOpacity:0.8,weight:2}).addTo(taskMap).bindTooltip(`${label}: ${p.name||p.place_id}`);
      L.marker([lat, lng], {icon: L.divIcon({className:'',html:`<div style="background:${color};color:#fff;padding:2px 6px;border-radius:3px;font-size:10px;white-space:nowrap;transform:translateX(-50%);position:absolute;bottom:${radius+4}px">${label}</div>`,iconSize:[0,0]})}).addTo(taskMap);
      bounds.push([lat, lng]);
    }
  }
  // GT route — use server-injected gt_route first (always present), fallback to client-side lookup
  const gtr = sb.gt_route || rpAllRoutes.find(r => r.route_id === sb.correct_route_id);
  if (gtr) {
    const pts = typeof decodeRoutePolyline === 'function' ? decodeRoutePolyline(gtr) : [];
    if (pts.length >= 2) {
      const poly = L.polyline(pts, {color:'#4ecca3',weight:4,opacity:1}).addTo(taskMap);
      poly.bindTooltip(`GT: ${gtr.origin_name||''}→${gtr.dest_name||''} | ${gtr.mode||''} | ${gtr.distance_km||0}km`);
      bounds.push(...pts);
    }
    // Waypoint segments — use server-injected gt_segments if available
    const segs = sb.gt_segments;
    if (segs && segs.length) {
      for (const seg of segs) {
        const pts = typeof decodeRoutePolyline === 'function' ? decodeRoutePolyline(seg) : [];
        if (pts.length >= 2) {
          const poly = L.polyline(pts, {color:'#4ecca3',weight:4,opacity:1}).addTo(taskMap);
          bounds.push(...pts);
        }
      }
    }
  }
  if (bounds.length) taskMap.fitBounds(bounds, {padding:[50,50],maxZoom:15});
  setTimeout(() => { if (taskMap) taskMap.invalidateSize(); }, 300);
}

function renderRoutePlanningPanel(tk, data) {
  const sb = data.route_sandbox || data.task_sandbox || {};
  // HTML panel
  const summary = tk.summary || {};
  let h = '<div class="rp-review-layout"><section class="section rp-map-section"><div class="rp-map-toolbar"><div class="section-title" data-ui-text>任务地图</div><div class="rp-map-toggles"><label><input type="checkbox" checked onchange="rpToggleBg(this.checked)" /> <span data-ui-text>背景路线</span></label><label><input type="checkbox" checked onchange="rpTogglePlaces(this.checked)" /> <span data-ui-text>地点</span></label></div></div>';
  h += '<div id="task-map"></div>';
  h += '</section><div class="section rp-audit-section"><div class="section-title" data-ui-text>🗺️ 路线规划审核</div>';

  // Legend
  h += '<div style="margin-bottom:6px;font-size:11px"><span style="color:#4ecca3">━</span> GT路线 <span style="color:#3388ff;margin-left:8px">●</span> 起/终点 <span style="color:#e67e22">●</span> 途经点</div>';

  // GT Editor
  const allRoutesIn = sb.all_routes_in_task || [];
  h += '<div class="rp-editor-card">';
  h += '<b style="font-size:12px" data-ui-text>GT 编辑器</b>';
  h += `<div style="margin:4px 0"><label data-ui-text>路线</label><select id="rp-gt-route" style="width:100%;margin-top:2px;background:#111;color:#cdd6f4;border:1px solid #444;padding:4px;border-radius:3px">`;
  h += `<option value="" data-ui-text>-- 选择 GT 路线 --</option>`;
  for (const r of allRoutesIn) {
    const sel = r.route_id === sb.correct_route_id ? ' selected' : '';
    h += `<option value="${escAttr(r.route_id)}"${sel}>${esc(r.origin_name||'')} → ${esc(r.dest_name||'')} | ${r.mode||''} ${r.distance_km||0}km</option>`;
  }
  h += '</select></div>';

  h += `<div style="margin:4px 0"><label><input type="checkbox" id="rp-has-wp" onchange="document.getElementById('rp-wp-section').style.display=this.checked?'':'none'" ${sb.correct_leg_route_ids && sb.correct_leg_route_ids.length ? 'checked' : ''}> 途经点</label></div>`;
  h += `<div id="rp-wp-section" style="display:${sb.correct_leg_route_ids && sb.correct_leg_route_ids.length ? '' : 'none'};margin:4px 0">`;
  h += `<label data-ui-text>Leg1 (起点→途经)</label><select id="rp-gt-leg1" style="width:100%;margin:2px 0;background:#111;color:#cdd6f4;border:1px solid #444;padding:4px;border-radius:3px">${allRoutesIn.map(r => `<option value="${escAttr(r.route_id)}"${sb.correct_leg_route_ids&&sb.correct_leg_route_ids[0]===r.route_id?' selected':''}>${esc(r.origin_name||'')} → ${esc(r.dest_name||'')} | ${r.mode||''}</option>`).join('')}</select>`;
  h += `<label data-ui-text>Leg2 (途经→终点)</label><select id="rp-gt-leg2" style="width:100%;margin:2px 0;background:#111;color:#cdd6f4;border:1px solid #444;padding:4px;border-radius:3px">${allRoutesIn.map(r => `<option value="${escAttr(r.route_id)}"${sb.correct_leg_route_ids&&sb.correct_leg_route_ids[1]===r.route_id?' selected':''}>${esc(r.origin_name||'')} → ${esc(r.dest_name||'')} | ${r.mode||''}</option>`).join('')}</select>`;
  h += '</div>';

  h += `<div style="margin:4px 0"><label data-ui-text>可行性</label><select id="rp-feasible" style="margin-left:8px;background:#111;color:#cdd6f4;border:1px solid #444;padding:2px;border-radius:3px"><option value="true" ${sb.expected_feasible!==false?'selected':''} data-ui-text>可行</option><option value="false" ${sb.expected_feasible===false?'selected':''} data-ui-text>不可行</option></select></div>`;
  h += `<div style="margin:4px 0"><label data-ui-text>模式</label><select id="rp-mode" style="margin-left:8px;background:#111;color:#cdd6f4;border:1px solid #444;padding:2px;border-radius:3px"><option ${(sb.correct_route_summary||{}).mode==='TRANSIT'?'selected':''}>TRANSIT</option><option ${(sb.correct_route_summary||{}).mode==='DRIVE'?'selected':''}>DRIVE</option><option ${(sb.correct_route_summary||{}).mode==='WALK'?'selected':''}>WALK</option></select></div>`;
  h += `<div style="margin:4px 0"><label data-ui-text>紧急度</label><select id="rp-urgency" style="margin-left:8px;background:#111;color:#cdd6f4;border:1px solid #444;padding:2px;border-radius:3px"><option value="relaxed" ${(sb.correct_route_summary||{}).urgency==='relaxed'?'selected':''}>relaxed</option><option value="tight" ${(sb.correct_route_summary||{}).urgency==='tight'?'selected':''}>tight</option><option value="urgent" ${(sb.correct_route_summary||{}).urgency==='urgent'?'selected':''}>urgent</option></select></div>`;
  h += `<div style="margin:4px 0"><label data-ui-text>限时(分钟)</label><input id="rp-time-limit" value="${escAttr(String((sb.correct_route_summary||{}).time_limit_min||summary.time_limit_min||''))}" style="width:60px;margin-left:8px;background:#111;color:#cdd6f4;border:1px solid #444;padding:2px;border-radius:3px" /></div>`;
  h += `<div style="margin:4px 0"><label>user_prompt</label><textarea id="rp-prompt" rows="3" style="width:100%;margin-top:2px;background:#111;color:#cdd6f4;border:1px solid #444;padding:4px;border-radius:3px;font-size:11px">${esc(tk.user_prompt||'')}</textarea></div>`;
  h += `<button class="save-btn" onclick="rpSaveGT()" style="margin-right:8px" data-ui-text>💾 保存 GT</button>`;
  h += `<div id="rp-save-msg" style="display:inline;font-size:11px"></div>`;
  h += '</div>';

  // POI Query section
  h += '<div class="section rp-query-card"><div class="section-title" data-ui-text>🔍 POI 路线查询</div>';
  h += '<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:4px;margin-bottom:6px">';
  h += '<div><label style="font-size:11px" data-ui-text>出发</label><select id="rp-poi-from" onchange="onRPPoiChange(\'from\')" style="width:100%;background:#111;color:#cdd6f4;border:1px solid #444;padding:3px;border-radius:3px;font-size:11px"></select></div>';
  h += '<div><label style="font-size:11px" data-ui-text>途经</label><select id="rp-poi-waypoint" onchange="onRPPoiChange(\'waypoint\')" style="width:100%;background:#111;color:#cdd6f4;border:1px solid #444;padding:3px;border-radius:3px;font-size:11px"></select></div>';
  h += '<div><label style="font-size:11px" data-ui-text>到达</label><select id="rp-poi-to" onchange="onRPPoiChange(\'to\')" style="width:100%;background:#111;color:#cdd6f4;border:1px solid #444;padding:3px;border-radius:3px;font-size:11px"></select></div>';
  h += '</div>';
  h += '<div style="display:flex;gap:8px"><select id="rp-poi-mode" style="background:#111;color:#cdd6f4;border:1px solid #444;padding:2px 6px;border-radius:3px"><option value="ALL" data-ui-text>全部</option><option>WALK</option><option>DRIVE</option><option>TRANSIT</option></select>';
  h += '<button class="save-btn" onclick="rpPOIQuery()" data-ui-text>🔍 查询</button></div>';
  h += '<div id="rp-query-result" style="margin-top:6px;font-size:11px"></div>';
  h += '</div>';

  // Route summary info
  h += `<div class="editor-grid" style="margin-top:8px">`;
  h += `<div class="kv"><span data-ui-text>距离</span><b>${summary.distance_km||'-'}km</b></div>`;
  h += `<div class="kv"><span data-ui-text>限时</span><b>${summary.time_limit_min||'-'}min</b></div>`;
  h += `<div class="kv"><span data-ui-text>途经点</span><b>${summary.has_waypoint?'有':'无'}</b></div>`;
  h += `<div class="kv"><span data-ui-text>模式</span><b>${summary.mode||'-'}</b></div>`;
  h += '</div></div></div>';
  return h;
}

async function rpSaveGT() {
  if (!currentFolder) return;
  const rid = document.getElementById('rp-gt-route')?.value || '';
  const hasWp = document.getElementById('rp-has-wp')?.checked || false;
  const leg1 = document.getElementById('rp-gt-leg1')?.value || '';
  const leg2 = document.getElementById('rp-gt-leg2')?.value || '';
  const feasible = document.getElementById('rp-feasible')?.value === 'true';
  const timeLimit = parseInt(document.getElementById('rp-time-limit')?.value || '0') || 0;
  const mode = document.getElementById('rp-mode')?.value || '';
  const urgency = document.getElementById('rp-urgency')?.value || '';
  const prompt = document.getElementById('rp-prompt')?.value || '';
  const fields = {
    correct_route_id: rid,
    correct_leg_route_ids: hasWp ? [leg1, leg2] : [],
    correct_route_summary: {has_waypoint: hasWp, time_limit_min: timeLimit, mode: mode, urgency: urgency},
    expected_feasible: feasible,
    user_prompt: prompt,
  };
  try {
    const r = await fetch(`/api/task/save?folder=${encodeURIComponent(currentFolder)}`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({task_type:'route_planning', fields}),
    });
    const d = await r.json();
    await showTaskSaveResult(d, 'rp-save-msg');
  } catch(e) {
    const msg = document.getElementById('rp-save-msg');
    if (msg) { msg.textContent = '❌ ' + e.message; msg.style.color = '#e74c3c'; }
  }
}

function escAttr(s) { return String(s||'').replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

function rpToggleBg(show) {
  for (const l of rpCurrentLayers.bg) show ? l.addTo(taskMap) : taskMap.removeLayer(l);
}
function rpTogglePlaces(show) {
  for (const l of rpCurrentLayers.places) show ? l.addTo(taskMap) : taskMap.removeLayer(l);
}

// ========== END ROUTE PLANNING ==========

init();
