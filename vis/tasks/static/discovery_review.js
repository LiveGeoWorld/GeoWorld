/* Author-only display. No candidate selection or judgement is performed here. */
let discoveryReviewMap = null;
let discoveryReviewMarkers = [];
let discoveryPersonMarker = null;
const discoveryLabel = text => `<span data-ui-text>${esc(text)}</span>`;
function discoveryReviewData(data) {
  const ex = data.curated_source?.example || {}, saved = data.discovery_gold || {};
  const gold = saved.gold || ex.gold || {}, start = saved.start || ex.start || {};
  const accepted = new Set(gold.acceptable_top1 || []);
  const places = data.world_slice?.places || [];
  const entries = new Map();
  for (const candidate of gold.candidates || []) entries.set(candidate.place_id || candidate.service_id, {...candidate});
  for (const item of saved.distractors || ex.distractors || []) {
    const key = item.place_id || item.service_id;
    entries.set(key, {...item, ...(entries.get(key) || {}), comparison_reason:item.reason_to_exclude});
  }
  const rows = [...entries.values()].map(row => {
    const place = places.find(p => p.place_id === row.place_id) || {};
    const coordinates = [row.coordinate, place.location].find(p => p && Number.isFinite(p.lat) && Number.isFinite(p.lng));
    return {...row, coordinate:coordinates, address:place.address || '', accepted:accepted.has(row.service_id) || accepted.has(row.place_id)};
  });
  // Legacy start records may point at the answer itself; never label those as user origins.
  const point = start.coordinate;
  const isAnswer = rows.some(r => r.accepted && (r.place_id === start.place_id ||
    (point && r.coordinate?.lat === point.lat && r.coordinate?.lng === point.lng)));
  const origin = !isAnswer && point && Number.isFinite(point.lat) && Number.isFinite(point.lng) ? point : null;
  const searchArea = ex.evidence_ledger?.construction_contract?.public_origin?.name || start.area_name || (ex.candidate_scope?.area_hints || []).join(' / ');
  return {ex,saved,gold,start,rows,origin,searchArea};
}
function renderDiscoverySpatial(data) {
  const {start,rows,origin,searchArea} = discoveryReviewData(data);
  return `<section class="section"><div class="section-title">${discoveryLabel('任务地图')}</div>
    <div class="ds-map-legend"><span><i class="ds-person-symbol">👤</i>${discoveryLabel('人物当前位置')}</span><span><i class="ds-dot ds-dot-gold"></i>GT</span><span><i class="ds-dot ds-dot-candidate"></i>${discoveryLabel('比较候选')}</span></div>
    <div class="ds-spatial"><div id="discovery-review-map"></div><aside class="ds-place-list">
      <div class="ds-area"><small>${discoveryLabel('搜索区域')}</small><div>${esc(searchArea || '-')}</div>
      ${origin ? `<button class="ds-person-link" onclick="focusDiscoveryPerson()">${discoveryLabel('人物当前位置')} · ${esc(start.character_name || '')}</button>` : ''}
      ${!origin ? `<small>${discoveryLabel('未标注可确认的独立出发点')}</small>` : ''}</div>
      ${rows.map((r,i) => `<button class="ds-place ${r.accepted?'ds-gold':''}" onclick="focusDiscoveryPlace(${i})"><span class="ds-role">${discoveryLabel(r.accepted?'正确答案':'比较候选')}</span><strong>${esc(r.name || r.place_id || '-')}</strong><small>${esc(r.address)}</small>${!r.coordinate?`<small>${discoveryLabel('未记录坐标')}</small>`:''}</button>`).join('')}
    </aside></div></section>`;
}
function focusDiscoveryPlace(index) {
  const marker = discoveryReviewMarkers[index];
  if (marker && discoveryReviewMap) { discoveryReviewMap.setView(marker.getLatLng(),16); marker.openPopup(); }
}
function focusDiscoveryPerson() {
  if (discoveryReviewMap && discoveryPersonMarker) {
    discoveryReviewMap.setView(discoveryPersonMarker.getLatLng(),16);
    discoveryPersonMarker.openPopup();
  }
}
async function initDiscoverySpatial(data) {
  if (discoveryReviewMap) { discoveryReviewMap.remove(); discoveryReviewMap = null; }
  discoveryReviewMarkers = [];
  discoveryPersonMarker = null;
  const el = document.getElementById('discovery-review-map');
  if (!el || !window.L) return;
  const {rows,origin,start} = discoveryReviewData(data);
  const map = discoveryReviewMap = L.map(el,{scrollWheelZoom:false});
  const bounds = L.latLngBounds([]);
  rows.forEach((r,i) => {
    if (!r.coordinate) return;
    const point = [r.coordinate.lat,r.coordinate.lng]; bounds.extend(point);
    discoveryReviewMarkers[i] = makeBenchPointMarker(point[0],point[1],r.accepted,r.name || '',r.place_type || '')
      .addTo(map).bindPopup(`<strong>${esc(r.name)}</strong><br>${discoveryLabel(r.accepted?'正确答案':'比较候选')}<br>${esc(r.address)}`);
  });
  if (origin) {
    const p=[origin.lat,origin.lng]; bounds.extend(p);
    // Same person glyph/color as the world overview; keep it legible at task zooms.
    const icon=L.divIcon({className:'actor-icon ds-person-marker',html:'<div class="ds-person-symbol">👤</div>',iconSize:[20,20],iconAnchor:[10,10]});
    discoveryPersonMarker = L.marker(p,{icon,zIndexOffset:1000}).addTo(map)
      .bindTooltip(esc(start.character_name || ''),{direction:'top',offset:[0,-10]})
      .bindPopup(`${discoveryLabel('人物当前位置')} · ${esc(start.character_name || '')}<br>${origin.lat.toFixed(6)}, ${origin.lng.toFixed(6)}`);
  }
  if (bounds.isValid()) map.fitBounds(bounds.pad(0.18),{maxZoom:16});
  else { el.insertAdjacentHTML('beforebegin',`<p class="readonly-note">${discoveryLabel('未记录坐标')}</p>`); map.setView([0,0],2); }
  try { const base = await createBenchBasemapLayer(); if (discoveryReviewMap === map && el.isConnected) base.addTo(map); }
  catch (_) { if (el.isConnected) el.insertAdjacentHTML('beforebegin',`<p class="readonly-note">${discoveryLabel('底图加载失败，地点标记仍可查看')}</p>`); }
  if (el.isConnected) map.invalidateSize();
}
function renderDiscoveryConditions(data) {
  const {ex,saved,searchArea} = discoveryReviewData(data);
  const items = (value) => (Array.isArray(value)?value:[]).map(v=>`<li>${esc(typeof v==='string'?v:JSON.stringify(v))}</li>`).join('');
  const type = saved.service_type || ex.service_type || '-';
  const displayType = {hotel_booking:'酒店住宿',restaurant_booking:'餐饮服务'}[type];
  return `<div class="ds-conditions"><h3>${discoveryLabel('任务约束')}</h3><div class="ds-condition-meta"><span>${discoveryLabel('服务类型')} · ${displayType?discoveryLabel(displayType):esc(type)}</span><span>${discoveryLabel('搜索区域')} · ${esc(searchArea || '-')}</span></div>
    <div class="ds-condition-grid"><div><h4>${discoveryLabel('硬性条件')}</h4><ul>${items(ex.hard_constraints) || `<li>${discoveryLabel('未记录')}</li>`}</ul></div><div><h4>${discoveryLabel('偏好条件')}</h4><ul>${items(ex.soft_preferences) || `<li>${discoveryLabel('未记录')}</li>`}</ul></div></div>
    <details class="gr-details"><summary>${discoveryLabel('构建时的选择依据')}</summary><ul>${items(saved.ranking_criteria || ex.ranking_criteria)}</ul></details></div>`;
}
function renderDiscoveryCandidateDetails(r) {
  const fields = [
    ['类型',r.primary_type || r.place_type],['评分',r.factual_rank_features?.rating ?? r.rating],
    ['地址',r.address],['坐标',r.coordinate && Number.isFinite(r.coordinate.lat) && Number.isFinite(r.coordinate.lng) ? `${r.coordinate.lat.toFixed(6)}, ${r.coordinate.lng.toFixed(6)}` : null],
    ['距人物（直线）',Number.isFinite(r.distance_from_person_m)?formatMeters(r.distance_from_person_m):null],
    ['距 GT（直线）',Number.isFinite(r.distance_to_gt_m)?formatMeters(r.distance_to_gt_m):null],
  ].filter(([,v])=>v !== undefined && v !== null && v !== '');
  const reasons = [...new Set(r.reasons || [])];
  return `<details class="ds-row-details"><summary>${discoveryLabel('查看详情')}</summary><div class="ds-detail-body">
    <dl class="ds-detail-grid">${fields.map(([k,v])=>`<div><dt>${discoveryLabel(k)}</dt><dd>${esc(v)}</dd></div>`).join('')}</dl>
    ${reasons.length?`<div class="ds-detail-evidence"><div class="ds-detail-label">${discoveryLabel('记录依据')}</div><ul>${reasons.map(v=>`<li>${esc(v)}</li>`).join('')}</ul></div>`:''}
    <details class="ds-raw-details"><summary>${discoveryLabel('原始字段')}</summary><pre>${esc(JSON.stringify(r,null,2))}</pre></details>
    </div></details>`;
}
function renderDiscoveryComparison(data) {
  const {rows,ex,origin} = discoveryReviewData(data);
  const landscape = ex.search_landscape || ex.evidence_ledger?.search_landscape || {};
  const entries = new Map();
  const key = r => r.place_id || r.service_id;
  for (const [i,r] of (landscape.confusable_candidates || []).entries()) entries.set(key(r) || `landscape:${i}`,{...r});
  for (const [i,r] of rows.entries()) {
    const id = key(r) || `row:${i}`;
    entries.set(id,{...entries.get(id),...r});
  }
  const all = [...entries.values()].map(r => {
    const place=(data.world_slice?.places || []).find(p=>p.place_id===r.place_id);
    const point=[r.coordinate,place?.location].find(p=>p && Number.isFinite(p.lat) && Number.isFinite(p.lng));
    return {...r,coordinate:point,address:r.address || place?.address || '', distance_to_gt_m:r.distance_m,
      distance_from_person_m:origin && point ? Math.round(distanceMeters(origin.lat,origin.lng,point.lat,point.lng)) : null,
      distance_basis:'straight_line'};
  }), targets=all.filter(r=>r.accepted), candidates=all.filter(r=>!r.accepted);
  const details = renderDiscoveryCandidateDetails;
  const rank = r => r.rank ? `<small class="ds-rank">${discoveryLabel('原始排序')} #${esc(r.rank)}</small>` : '';
  return `<section class="confusable-section ds-comparison">
    ${targets.map(r=>`<div class="ds-gt-summary"><strong class="ds-gold-text">GT · ${esc(r.name || '-')}</strong>${rank(r)}${details(r)}</div>`).join('')}
    <div class="confusable-head"><span class="confusable-title">${discoveryLabel('可混淆候选 · confusable_candidates')} (${candidates.length})</span></div>
    <div class="ds-table-scroll"><table class="confusable-table"><thead><tr><th>#</th><th>${discoveryLabel('名称')}</th><th>${discoveryLabel('距离')}</th><th>${discoveryLabel('类型')}</th><th>${discoveryLabel('排除理由')}</th></tr></thead><tbody>
    ${candidates.map((r,i)=>{
      const reasons=[...(Array.isArray(r.exclusion_reasons)?r.exclusion_reasons:r.exclusion_reasons?[r.exclusion_reasons]:[])];
      const compact = value => value.replace(/\s/g,'');
      if (r.comparison_reason && compact(reasons.join('；')) !== compact(r.comparison_reason)) reasons.push(r.comparison_reason);
      return `<tr><td class="col-idx">${i+1}</td><td class="col-name">${esc(r.name || '-')} ${rank(r)}</td><td class="col-dist" title="${attr(GeoUI.t('人物当前位置到候选的直线距离'))}">${formatMeters(r.distance_from_person_m)}</td><td class="col-type">${esc(r.primary_type || r.place_type || '-')}</td><td class="col-reason">${esc([...new Set(reasons)].join('；') || '-')} ${details(r)}</td></tr>`;
    }).join('')}</tbody></table></div>
    </section>`;
}
