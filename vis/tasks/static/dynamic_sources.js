/* Existing Dynamic artifacts and static generation inputs remain separate. */
let dynamicAdmission = 'all_validated_static';
let dynamicSourceRequest = 0;
let dynamicOverviewFilter = null;
let dynamicOverviewData = null;
let dynamicConfirmed = null;
let dynamicPool = null;
let dynamicConfigRevision = 0;
let dynamicConfirmBusyRevision = null;
let dynamicCostActual = null;
let dynamicCostError = '';
let dynamicCostConfirmed = false;
let dynamicCostPreview = null;
let dynamicCostBusy = false;
let dynamicDraftKey = '';
let dynamicDraftTargets = {};

function dynamicScopeKey() {
  return JSON.stringify([currentCity, currentBatch,
    document.querySelector('input[name="gen-mode"]:checked')?.value || 'new']);
}

function dynamicSaveDraft() {
  if (!dynamicDraftKey || typeof reviewState === 'undefined') return;
  const ownsControls = document.getElementById('dynamic-config')?.dataset?.draftKey === dynamicDraftKey;
  for (const row of ownsControls ? (dynamicPool?.rows || []) : []) {
    const mode = document.getElementById(`ds-mode-${row.task_type}`);
    const value = document.getElementById(`ds-value-${row.task_type}`);
    if (mode && value) dynamicDraftTargets[row.task_type] = {mode: mode.value, value: Number(value.value)};
  }
  reviewState.write('dynamic:' + dynamicDraftKey, {admission: dynamicAdmission,
    targets: dynamicDraftTargets, confirmed: dynamicConfirmed, preview: dynamicCostPreview});
}

function dynamicRestoreDraft() {
  const scope = dynamicScopeKey();
  if (scope === dynamicDraftKey) return;
  dynamicDraftKey = scope;
  const draft = reviewState.read('dynamic:' + scope) || {};
  dynamicInvalidate();
  dynamicAdmission = draft.admission || 'all_validated_static';
  dynamicDraftTargets = draft.targets || {};
  dynamicConfirmed = draft.confirmed || null;
  dynamicCostPreview = draft.preview || null;
  // A stored browser flag is never authority to start a job.
  dynamicCostConfirmed = false;
}

async function dynamicCostPost(path, payload) {
  const response = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || '成本请求失败');
  return data;
}

function dynamicRenderCost() {
  const host = document.getElementById('dynamic-cost');
  if (!host) return;
  host.hidden = !dynamicConfirmed;
  if (!dynamicConfirmed) { host.innerHTML = ''; return; }
  const configStatus = document.getElementById('dynamic-config-status');
  if (configStatus) configStatus.textContent = `配置已确认 · ${dynamicCostBusy ? '正在预估／确认成本…' : dynamicCostError || (dynamicCostConfirmed ? '成本已确认' : dynamicCostPreview ? '成本预估已完成，请确认成本' : '待预估成本')}`;
  const metric = value => value && Number.isFinite(value.cost_usd)
    ? `$${value.cost_usd.toFixed(4)} · ${Number(Number(value.api_requests).toFixed(2))} 次请求` : '—';
  host.innerHTML = `<h3 class="ds-section-title" data-ui-text>Google API 成本</h3>
    <div class="ds-cost-grid">
      <div class="ds-count"><span data-ui-text>预计新增成本</span><b data-ui-output>${esc(dynamicCostPreview ? metric(dynamicCostPreview.cost_preview.expected) : '待预估')}</b><small data-ui-output>${dynamicCostPreview ? `整批 ${dynamicCostPreview.cost_preview.sample_count} 种情景 · 观测范围 $${dynamicCostPreview.cost_preview.minimum.cost_usd.toFixed(4)}–$${dynamicCostPreview.cost_preview.maximum.cost_usd.toFixed(4)}，非费用上限` : '只读预演，不调用模型或 Google API'}</small></div>
      <div class="ds-count"><span data-ui-text>本次构建实际消耗</span><b data-ui-output>${esc(metric(dynamicCostActual?.current))}</b><small data-ui-text>包含失败与重试产生的请求</small></div>
      <div class="ds-count"><span data-ui-text>城市累计消耗 · 动静态合计</span><b data-ui-output>${esc(metric(dynamicCostActual?.cumulative))}</b><small data-ui-text>共享成本日志，不按任务类型拆分</small></div>
    </div>
    <p class="ds-note" data-ui-text>统计范围：当前 World／城市版本的 Google Routes API。费用沿用静态参考单价，不扣账户免费额度，不含模型费用；缓存命中不重复计费。</p>
    ${dynamicCostPreview?.cost_preview?.candidate_exhausted_attempts?.length ? `<p class="ds-note">${dynamicCostPreview.cost_preview.candidate_exhausted_attempts.length} / ${dynamicCostPreview.cost_preview.sampled_attempts} 次来源抽样没有替代候选，已从同类剩余静态来源补选；每个来源在同一情景内最多尝试一次。</p>` : ''}
    ${dynamicCostPreview?.cost_preview?.source_pool_shortfalls?.length ? `<p class="ds-note">${dynamicCostPreview.cost_preview.source_pool_shortfalls.length} 个类型情景遍历剩余来源后仍未达到目标。本次为不足量预估，不代表完整目标的成本。</p>` : ''}
    <p role="status" data-ui-output>${esc(dynamicCostError || (dynamicCostBusy ? '正在处理成本预演／确认，请稍候…' : dynamicCostConfirmed ? '成本已确认，可以构建。' : '预估不含额外构造失败和服务重试，实际消耗可能超出观测范围。'))}</p>
    <button class="nav-btn" id="ds-cost-confirm" ${!dynamicCostPreview || dynamicCostBusy || dynamicCostConfirmed ? 'disabled' : ''} onclick="dynamicConfirmCost()" data-ui-output>${dynamicCostConfirmed ? '✓ 成本已确认' : '确认 Google API 成本'}</button>
    <button class="nav-btn" ${dynamicCostBusy ? 'disabled' : ''} onclick="dynamicLoadCost()" data-ui-text>重新预估</button>`;
}

async function dynamicConfirmCost() {
  if (!dynamicCostPreview || dynamicCostBusy) return;
  const token = dynamicConfirmed?.token, revision = dynamicConfigRevision;
  dynamicCostBusy = true; dynamicCostError = ''; dynamicRenderCost();
  try {
    await dynamicCostPost('/api/dynamic/cost-confirm', {dynamic_confirmation:token, preflight_id:dynamicCostPreview.preflight_id});
    if (revision !== dynamicConfigRevision || token !== dynamicConfirmed?.token) return;
    dynamicCostConfirmed = true;
  } catch(error) {
    if (revision !== dynamicConfigRevision || token !== dynamicConfirmed?.token) return;
    dynamicCostConfirmed = false; dynamicCostError = error.message;
  } finally {
    if (revision === dynamicConfigRevision && token === dynamicConfirmed?.token) {
      dynamicCostBusy = false; dynamicRenderCost(); dynamicSyncGate();
    }
  }
}

async function dynamicLoadCost() {
  const token = dynamicConfirmed?.token;
  const revision = dynamicConfigRevision;
  if (!token) return;
  if (dynamicCostBusy) return;
  dynamicCostPreview = null; dynamicCostBusy = true;
  dynamicCostActual = null; dynamicCostError = ''; dynamicCostConfirmed = false;
  dynamicRenderCost(); dynamicSyncGate();
  try {
    const actual = await dynamicJSON(`/api/dynamic/cost-actual?city=${encodeURIComponent(dynamicConfirmed.city)}`);
    if (revision !== dynamicConfigRevision || token !== dynamicConfirmed?.token) return;
    dynamicCostActual = actual.route_cost_actual;
    dynamicRenderCost();
    const data = await dynamicCostPost('/api/dynamic/cost-preview', {dynamic_confirmation:token});
    if (revision !== dynamicConfigRevision || token !== dynamicConfirmed?.token) return;
    dynamicCostActual = data.route_cost_actual;
    dynamicCostPreview = data;
  } catch(error) {
    if (revision !== dynamicConfigRevision || token !== dynamicConfirmed?.token) return;
    dynamicCostError = `成本预估失败：${error.message}`;
  }
  dynamicCostBusy = false;
  dynamicRenderCost();
}

function dynamicUpdateCostFromJob(job) {
  if (job.task_type !== 'dynamic_replanning' || job.city !== dynamicConfirmed?.city) return;
  if (job.route_cost_actual) dynamicCostActual = job.route_cost_actual;
  dynamicRenderCost();
}
const dynamicTypeNames = {route_planning:'路线', multi_entity:'多主体', itinerary:'长程', safety_planning:'Safety', region_aware:'Region', unknown:'来源信息不完整'};

async function dynamicJSON(url) {
  const response = await fetch(url);
  if (!(response.headers.get('content-type') || '').includes('application/json'))
    throw new Error(`接口未返回 JSON（HTTP ${response.status}），请确认后端已更新并刷新页面。`);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || '读取失败');
  return data;
}

async function fetchDynamicSources() {
  const city = document.getElementById('city-filter').value || '';
  const mode=document.querySelector('input[name="gen-mode"]:checked')?.value || 'new';
  return dynamicJSON(`/api/dynamic/sources?city=${encodeURIComponent(city)}&admission=${encodeURIComponent(dynamicAdmission)}&mode=${mode}&batch=${encodeURIComponent(currentBatch||'')}`);
}

function dynamicOutputChanged() {
  if(selectedTaskType()!=='dynamic_replanning')return;
  dynamicSaveDraft(); dynamicRestoreDraft();
  if(document.getElementById('dynamic-config'))dynamicRenderConfig();
}

async function showDynamicSources() {
  if (currentView !== 'tasks') return;
  dynamicSaveDraft();
  dynamicRestoreDraft();
  const request = ++dynamicSourceRequest;
  currentFolder = null;
  dynamicOverviewFilter = null;
  filterTasks();
  document.getElementById('task-detail').innerHTML = '<section class="dynamic-sources"><header><h2 data-ui-text>任务统计</h2><span class="ds-badge" data-ui-text>动态任务</span></header><div id="dynamic-source-summary" aria-live="polite"><p data-ui-text>正在读取已有动态任务…</p></div></section>';
  const city = document.getElementById('city-filter').value || '';
  if (dynamicConfirmed?.city !== city) dynamicInvalidate();
  try {
    const data = await dynamicJSON(`/api/dynamic/overview?city=${encodeURIComponent(city)}&batch=${encodeURIComponent(currentBatch || 'latest')}`);
    if (currentView !== 'tasks' || request !== dynamicSourceRequest || selectedTaskType() !== 'dynamic_replanning') return;
    dynamicOverviewData = data;
    const target = document.getElementById('dynamic-source-summary');
    if (!target) return;
    const types = Object.keys(dynamicTypeNames).filter(k=>k !== 'unknown' || data.counts.unknown);
    target.innerHTML = `<p class="ds-subtitle">${esc(data.world_id)} · ${esc(city || '全部城市')} · ${esc(data.batch === 'latest' ? '当前批次汇总（每城最新动态批次）' : data.batch)}</p>
      <div class="ds-counts ds-overview-totals"><div class="ds-count ds-total"><span data-ui-text>已有动态任务</span><b>${data.total}<small data-ui-text> 条</small></b></div>${city ? '' : `<div class="ds-count"><span data-ui-text>已有任务的城市</span><b>${data.populated_cities}<small> / ${data.cities.length}</small></b></div>`}</div>
      <div class="ds-guide"><b data-ui-text>操作指引</b><p>${city ? '点击左侧任务查看详情。需要扩增时，在下方按来源类型设置派生目标并确认，再点击左侧“生成任务”。' : '点击“进入城市”查看任务并配置扩增。进入城市或确认配置都不会自动生成任务。'}</p></div>
      ${!data.total ? '<p class="ds-empty" data-ui-text>当前范围暂无动态任务。生成后，将在这里按城市与静态来源类型展示。</p>' : ''}
      ${city ? '' : '<h3 class="ds-section-title" data-ui-text>城市分布</h3>'}
      ${city ? '<button class="nav-btn" onclick="dynamicSelectCity(\'\')" data-ui-text>← 返回全部城市</button>' : ''}
      <div class="ds-city-grid">${(city ? [] : data.cities).map(c=>`<div class="ds-city-card"><div><b>${esc(c.name)}</b><span>${c.count} 条动态任务</span></div><div class="ds-city-actions"><button class="nav-btn" data-city="${escAttr(c.city_id)}" onclick="dynamicSelectCity(this.dataset.city)" data-ui-text>进入城市</button></div></div>`).join('')}</div>
      <h3 class="ds-section-title">${city ? '当前城市动态任务来源组成' : '来源组成'} <button class="nav-btn" onclick="dynamicFilterType('')" data-ui-text>显示全部</button></h3>
      <p data-ui-text>统计当前所选动态批次，不包含其他动态批次。</p>
      <div class="ds-counts">${types.map(k=>`<button class="ds-count ds-type-button" onclick="dynamicFilterType('${k}')"><span>${dynamicTypeNames[k]}${k==='unknown'?'':'派生'}</span><b>${data.counts[k] || 0}</b></button>`).join('')}</div><p id="ds-filter-note" data-ui-text>点击来源类别，筛选左侧动态任务列表。</p>
      <details><summary data-ui-text>查看实际静态来源批次</summary><div class="ds-table-wrap"><table><thead><tr><th data-ui-text>城市</th><th data-ui-text>静态来源类型</th><th data-ui-text>静态来源批次</th><th data-ui-text>已派生动态任务</th></tr></thead><tbody>${data.rows.length ? data.rows.map(r=>`<tr><td>${esc(r.city_id)}</td><td data-ui-output>${dynamicTypeNames[r.task_type]}</td><td>${esc(r.source_batch || '来源批次未记录')}</td><td>${r.count}</td></tr>`).join('') : '<tr><td colspan="4" data-ui-text>暂无来源记录</td></tr>'}</tbody></table></div></details>
      ${city ? '<div id="dynamic-config"></div>' : ''}`;
    if (city) {
      const name = data.cities.find(c=>c.city_id===city)?.name || city;
      document.querySelector('.dynamic-sources h2').textContent = name + ' · 动态任务';
      document.querySelector('.dynamic-sources .ds-badge')?.remove();
      const cityLabel = document.getElementById('city-filter').selectedOptions[0]?.textContent || name;
      document.querySelector('.ds-subtitle').textContent = `${data.world_id} · ${cityLabel} · ${document.getElementById('batch-filter').selectedOptions[0]?.textContent || '暂无动态批次'}`;
      await dynamicRenderConfig();
    }
  } catch(error) {
    if (currentView === 'tasks' && request === dynamicSourceRequest && document.getElementById('dynamic-source-summary'))
      document.getElementById('dynamic-source-summary').textContent = error.message;
  }
}

function dynamicFilterType(kind) {
  dynamicOverviewFilter = kind ? new Set(dynamicOverviewData?.folders[kind] || []) : null;
  filterTasks();
  document.getElementById('ds-filter-note').textContent = kind ? `左侧正在显示：${dynamicTypeNames[kind]}派生的动态任务。` : '左侧显示全部来源类别。';
}

async function dynamicSelectCity(city) {
  document.getElementById('city-filter').value = city;
  await onCityChange();
}

async function checkDynamicSources() {
  const panel = document.getElementById('gen-cmd');
  try {
    const data = await fetchDynamicSources();
    if (!dynamicConfirmed || dynamicConfirmed.fingerprint !== data.fingerprint) {
      dynamicInvalidate(); panel.textContent = '请先在右侧确认派生配置（来源变化后需重新确认）。'; return false;
    }
    document.getElementById('gen-count').value = dynamicConfirmed.count;
    if (!dynamicCostConfirmed) {
      panel.textContent = '请先完成 Google API 成本预估并确认成本。'; return false;
    }
    return true;
  } catch(error) { panel.textContent=error.message; return false; }
}

function dynamicSyncGate() {
  if (selectedTaskType() !== 'dynamic_replanning') return;
  const valid = dynamicConfirmed && dynamicConfirmed.city === document.getElementById('city-filter').value;
  const reason = generationIsActive() ? '当前任务正在生成' : !valid ? '请先在右侧确认派生配置'
    : !dynamicCostConfirmed ? '请先完成 Google API 成本预估并确认成本' : '';
  const button = document.getElementById('gen-start');
  if (button) button.disabled = !!reason;
  const wrap = document.getElementById('gen-start-wrap');
  if (wrap) { wrap.title=reason; wrap.dataset.tooltip=reason; wrap.classList.toggle('has-tooltip',!!reason); }
  const status = document.getElementById('gen-cmd');
  if (status && !generationIsActive()) status.textContent = reason || `配置与成本已确认 · 本次新增 ${dynamicConfirmed.count} 条`;
}

function dynamicInvalidate() {
  dynamicConfirmed = null; dynamicConfigRevision++;
  dynamicConfirmBusyRevision = null;
  const confirmButton = document.getElementById('ds-confirm');
  if (confirmButton) {confirmButton.textContent = '确认派生配置';confirmButton.classList.remove('ds-confirmed');}
  dynamicCostActual = null; dynamicCostError = ''; dynamicCostConfirmed = false;
  dynamicCostPreview = null; dynamicCostBusy = false;
  dynamicRenderCost();
  dynamicSyncGate();
  const status = document.getElementById('dynamic-config-status');
  if (status) status.textContent = '配置未确认';
}

async function dynamicRenderConfig() {
  dynamicSaveDraft();
  const host = document.getElementById('dynamic-config');
  if (!host) return;
  host.innerHTML = `<h3 class="ds-section-title" data-ui-text>派生配置</h3><div class="ds-settings ds-config-grid">
    <label class="gen-field" data-ui-text>静态题来源<select id="ds-admission" onchange="dynamicAdmission=this.value;dynamicInvalidate();dynamicRenderConfig()"><option value="all_validated_static" data-ui-text>全部已生成</option><option value="audit_approved_only" data-ui-text>人工审核通过</option></select></label>
    <p data-ui-text>按类型设置当前目标动态批次扩增后的总量。</p>
    <div id="ds-pool" data-ui-text>读取静态来源…</div><p id="ds-preview" data-ui-output></p>
    <button class="nav-btn" id="ds-confirm" onclick="dynamicConfirm()" disabled data-ui-text>确认派生配置</button><span id="dynamic-config-status" role="status" aria-live="polite" data-ui-text>配置未确认</span></div>
    <section id="dynamic-cost" class="ds-cost-panel" hidden aria-label="Google API 成本" data-ui-aria-label></section>`;
  host.dataset.draftKey = dynamicDraftKey;
  document.getElementById('ds-admission').value=dynamicAdmission;
  document.getElementById('ds-admission').disabled=generationIsActive();
  const revision=dynamicConfigRevision;
  try {
    const pool=await fetchDynamicSources();
    if (!host.isConnected || revision!==dynamicConfigRevision) return;
    dynamicPool=pool;
    if (dynamicConfirmed?.fingerprint !== pool.fingerprint) dynamicInvalidate();
document.getElementById('ds-pool').innerHTML=`<div class="ds-table-wrap"><table class="ds-target-table"><thead><tr><th data-ui-text>类型</th><th data-ui-text>静态来源批次（默认最新）</th><th data-ui-text>静态总题数</th><th data-ui-text>当前动态数（占比）</th><th data-ui-text>派生目标总量</th></tr></thead><tbody>${pool.rows.map(r=>`<tr><td data-ui-output>${dynamicTypeNames[r.task_type]}</td><td>${esc(r.batch||'无批次')}</td><td>${r.static_total}</td><td>${r.current}（${r.static_total ? (100*r.current/r.static_total).toFixed(1) : '0.0'}%）</td><td><div class="ds-target-input"><select id="ds-mode-${r.task_type}" aria-label="${dynamicTypeNames[r.task_type]}目标方式" onchange="dynamicInvalidate();dynamicPreview()"><option value="count" data-ui-text>数量</option><option value="ratio" data-ui-text>比例（%）</option></select><input id="ds-value-${r.task_type}" aria-label="${dynamicTypeNames[r.task_type]}派生目标" type="number" min="0" value="${r.current}" oninput="dynamicInvalidate();dynamicPreview()"></div><small id="ds-result-${r.task_type}" data-ui-output></small></td></tr>`).join('')}</tbody></table></div>`;
    for(const row of pool.rows) {
      const spec=dynamicConfirmed?.targets[row.task_type] || dynamicDraftTargets[row.task_type];
      const mode=document.getElementById(`ds-mode-${row.task_type}`), value=document.getElementById(`ds-value-${row.task_type}`);
      if(spec){mode.value=spec.mode;value.value=spec.value;}
      mode.disabled=value.disabled=generationIsActive()||!row.static_total;
    }
    dynamicPreview(); dynamicSyncGate();
    if(dynamicConfirmed)document.getElementById('dynamic-config-status').textContent=`已确认 · 本次新增 ${dynamicConfirmed.count} 条`;
    dynamicRenderCost();
    if (dynamicConfirmed) {
      const token = dynamicConfirmed.token;
      try {
        const status = await dynamicCostPost('/api/dynamic/cost-status', {
          dynamic_confirmation: token, preflight_id: dynamicCostPreview?.preflight_id});
        if (revision !== dynamicConfigRevision || token !== dynamicConfirmed?.token) return;
        dynamicCostConfirmed = status.confirmed;
      } catch (error) {
        if (revision !== dynamicConfigRevision || token !== dynamicConfirmed?.token) return;
        dynamicSaveDraft();
        dynamicInvalidate();
        dynamicPreview();
        document.getElementById('dynamic-config-status').textContent = error.message;
      }
      dynamicRenderCost(); dynamicSyncGate();
    }
    dynamicSaveDraft();
  } catch(e) { if(host.isConnected)document.getElementById('ds-pool').textContent=e.message; }
}

function dynamicPreview() {
  let additional=0, valid=true;
  for(const row of dynamicPool?.rows || []) {
    const mode=document.getElementById(`ds-mode-${row.task_type}`).value;
    const raw=document.getElementById(`ds-value-${row.task_type}`).value, value=Number(raw);
    const target=mode==='ratio'?Math.floor(row.static_total*value/100):value;
    const add=Math.max(0,target-row.current);
    const ok=raw!==''&&Number.isFinite(value)&&value>=0&&Number.isInteger(target)&&target<=row.static_total&&add<=row.available&&(mode!=='ratio'||value<=100);
    valid=valid&&ok; additional+=add;
    const percent=n=>row.static_total?(100*n/row.static_total).toFixed(1) :'0.0';
    document.getElementById(`ds-result-${row.task_type}`).textContent=ok?`当前 ${row.current} 条（${percent(row.current)}%）· 新增 ${add} 条（${percent(add)}%）`:'目标无效或超出可选未派生题数';
  }
  document.getElementById('ds-preview').textContent=`本次新增 ${Number.isFinite(additional)?additional:0} 条。比例按静态总题数换算并向下取整；降低目标不会删除已有任务。构造失败可能导致不足量。`;
  const button=document.getElementById('ds-confirm');
  const busy=dynamicConfirmBusyRevision===dynamicConfigRevision;
  button.disabled=!valid||additional<1||generationIsActive()||busy||!!dynamicConfirmed;
  button.textContent=busy?'正在确认…':dynamicConfirmed?'✓ 派生配置已确认':'确认派生配置';
  button.classList.toggle('ds-confirmed',!!dynamicConfirmed&&!busy);
  button.title=busy?'正在核对静态来源':dynamicConfirmed?'配置已确认，请继续查看下方成本':
    generationIsActive()?'当前任务正在构建':!valid?'请修正无效的派生目标':additional<1?'至少增加一条派生任务后才能确认':'配置有效，点击确认后查看 Google API 成本';
}

async function dynamicConfirm() {
  if (dynamicConfirmBusyRevision === dynamicConfigRevision) return;
  const revision=dynamicConfigRevision;
  const button=document.getElementById('ds-confirm');
  dynamicConfirmBusyRevision=revision;
  if(button){button.disabled=true;button.textContent='正在确认…';}
  const status=document.getElementById('dynamic-config-status');
  if(status)status.textContent='正在核对静态来源与派生配置，请稍候；尚未开始构建。';
  try {
  const targets=Object.fromEntries(dynamicPool.rows.map(r=>[r.task_type,{mode:document.getElementById(`ds-mode-${r.task_type}`).value,value:Number(document.getElementById(`ds-value-${r.task_type}`).value)}]));
  const response=await fetch('/api/dynamic/confirm',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({city:document.getElementById('city-filter').value,admission:dynamicAdmission,fingerprint:dynamicPool?.fingerprint,targets,mode:document.querySelector('input[name="gen-mode"]:checked')?.value||'new',batch:currentBatch||''})});
  const result=await response.json();
  if(revision!==dynamicConfigRevision)return;
  if(!response.ok) {dynamicInvalidate();document.getElementById('dynamic-config-status').textContent=result.error||'确认失败';return;}
  dynamicConfirmed=result; document.getElementById('gen-count').value=result.count;
  document.getElementById('dynamic-config-status').textContent=`配置已确认 · 本次新增 ${result.count} 条 · 正在读取成本…`;
  dynamicSyncGate();
  if(button){button.textContent='✓ 派生配置已确认';button.classList.add('ds-confirmed');}
  dynamicRenderCost();
  document.getElementById('dynamic-cost')?.scrollIntoView({behavior:'smooth',block:'nearest'});
  await dynamicLoadCost();
  if(revision===dynamicConfigRevision && dynamicConfirmed){
    document.getElementById('dynamic-config-status').textContent=dynamicCostError
      ? `配置已确认 · ${dynamicCostError}` : '配置已确认 · 成本预估已完成，请确认成本';
  }
  } catch(error) {
    if(revision!==dynamicConfigRevision)return;
    dynamicInvalidate();document.getElementById('dynamic-config-status').textContent=`确认失败：${error.message}`;
  } finally {
    if(dynamicConfirmBusyRevision===revision)dynamicConfirmBusyRevision=null;
    if(button?.isConnected && revision===dynamicConfigRevision){
      button.textContent='确认派生配置';
      dynamicPreview();
    } else if(button?.isConnected && dynamicConfirmBusyRevision===null) {
      // Failure invalidates confirmation, but the unchanged inputs can retry.
      dynamicPreview();
    }
  }
}
