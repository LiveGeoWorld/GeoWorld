let multiReviewMap=null,multiReviewMarkers=[],multiReviewData=null;
function multiReviewFacts(data){const sb=data.task_sandbox||{},places=sb.candidate_places||sb.candidate_restaurants||[];const legacy=places.find(p=>p.service_id===sb.expected_restaurant_service_id);return {sb,people:sb.participants||[],places,plans:sb.acceptable_plans||[],expected:sb.expected_place_id||(legacy?multiReviewId(legacy):sb.expected_restaurant_service_id)};}
function multiReviewId(p){return p.place_id||p.service_id;}
function mrRaw(v){return `<details class="gr-details"><summary>${discoveryLabel('原始记录')}</summary><pre>${esc(JSON.stringify(v,null,2))}</pre></details>`;}
function mrFields(rows){return `<dl class="ds-detail-grid">${rows.filter(([,v])=>v!==undefined&&v!==null&&v!=='').map(([k,v])=>rrField(k,Array.isArray(v)?v.join(' / '):v)).join('')}</dl>`;}
function multiReviewCandidateList(places,expected){return `<div class="mr-map-candidates"><div class="mr-people-heading">${discoveryLabel('其他候选地点')}</div>${places.filter(p=>multiReviewId(p)!==expected).map(p=>`<button onclick="focusMultiCandidate('${escAttr(multiReviewId(p))}')"><span class="mr-poi-dot"></span><strong>${esc(p.name||p.place_name)}</strong></button>`).join('')}</div>`;}
function renderMultiReview(data){
 multiReviewData=data;const {sb,people,places,plans,expected}=multiReviewFacts(data),c=sb.constraints||{},sc=sb.scenario||{},label=discoveryLabel,gt=places.find(p=>multiReviewId(p)===expected);
 const table=(heads,rows)=>`<div class="mr-table"><table><thead><tr>${heads.map(h=>`<th>${label(h)}</th>`).join('')}</tr></thead><tbody>${rows.join('')}</tbody></table></div>`;
 return renderPlacePromptEditor(data.task)+`<section class="section"><div class="section-title">${label('任务地图')}</div><div class="mr-spatial"><div id="multi-review-map"></div><aside class="mr-locations"><div class="mr-people-heading">${label('参与者')}</div><div class="mr-people-list">${people.map((p,i)=>`<button onclick="focusMultiReview(${i})"><span class="mr-person-small">👤</span><span class="mr-person-copy"><strong>${esc(p.name)}</strong><small>${esc(p.available_from||'—')} — ${esc(p.must_leave_by||'—')}</small></span></button>`).join('')}</div>${gt?`<div class="mr-map-gt"><button class="mr-gt" onclick="focusMultiReview(${people.length})"><span>GT</span><strong>${esc(gt.name||gt.place_name)}</strong></button></div>`:''}${multiReviewCandidateList(places,expected)}</aside></div></section>
 <section class="section difficulty-panel quality-panel mr-quality"><div class="difficulty-panel-title">${label('质量审核面板 · Quality Profile')}</div>
 ${multiComparisonHTML(data)}
 <details class="gr-details mr-source"><summary>${label('原始记录')}</summary>${mrRaw(sb)}</details></section>`+renderReviewControls(data.task.task_type,data.review_status);
}
function mrValue(v){return Array.isArray(v)?v.join(' / '):v??'—';}
function mrClock(v){return Number.isFinite(v)?`${String(Math.floor(v/60)).padStart(2,'0')}:${String(v%60).padStart(2,'0')}`:'—';}
function mrCondition(c){const names={start_time:'共同活动开始时间',commute_min:'单程耗时',travel_mode:'交通方式',google_types:'地点类型'},relations={le:'不超过',ge:'不少于',eq:'等于',in:'允许',contains:'包含'};const subject={activity:'共同活动',place:'地点',all_participants:'所有参与者'}[c.object]||c.object;return `${discoveryLabel(subject)} · ${discoveryLabel(names[c.attribute]||c.attribute)} · ${discoveryLabel(relations[c.relation]||c.relation)} ${esc(mrValue(c.value))}${c.attribute==='commute_min'?' min':''}`;}
function mrFailure(f){
 const label=discoveryLabel;
 if(typeof f.constraint==='object')return `${mrCondition(f.constraint)}；${label('实际未包含')} ${esc(f.constraint.value)}`;
 if(f.constraint==='required_place_type')return `${label('地点类型')}：${label('要求')} ${esc(f.expected)}；${label('实际')} ${esc(f.actual)}`;
 if(['joint_time_window','joint_time_and_open_window'].includes(f.constraint))return `${label('最早开始')} ${mrClock(f.earliest_start_min)}；${label('最迟开始')} ${mrClock(f.latest_start_min)}${f.duration_min!=null?`；${label('活动时长（分钟）')} ${esc(f.duration_min)}`:''}`;
 if(f.constraint==='party_size')return `${label('人数')}：${label('要求')} ${esc(f.required)}；${label('容量')} ${esc(f.capacity)}`;
 if(f.constraint==='dietary')return `${label('饮食')}：${label('要求')} ${esc(mrValue(f.required))}；${label('提供')} ${esc(mrValue(f.provided))}`;
 if(f.constraint==='budget')return `${esc(f.participant)} · ${label('预算上限')} ${esc(f.maximum)}；${label('人均价格')} ${esc(f.price)}`;
 if(f.constraint==='commute_min')return `${esc(f.participant)}：${(f.conditions||[]).map(mrCondition).join('；')}；${label('实际')} ${Object.entries(f.actual_route_minutes||{}).map(([m,v])=>`${esc(m)} ${esc(v)} min`).join(' / ')}`;
 if(f.expected!==undefined&&f.actual!==undefined)return `${esc(f.participant||'')} · ${label(f.constraint)}：${label('要求')} ${esc(mrValue(f.expected))}；${label('实际')} ${esc(mrValue(f.actual))}`;
 return label('详见保存的核验记录');
}
function mrSuccessEvidence(r,p,facts){
 const label=discoveryLabel,parts=[label('满足保存的本题 C')];
 if(r.meeting_start)parts.push(`${label('共同活动时间')} ${esc(r.meeting_start)}${r.meeting_end?` — ${esc(r.meeting_end)}`:''}`);
 if(facts?.activity?.duration_min!==undefined)parts.push(`${label('活动时长（分钟）')} ${esc(facts.activity.duration_min)}`);
 for(const c of facts?.additional_constraints||[]){
  if(c.object==='place')parts.push(`${label('实际类型')} ${esc(c.value)}`);
  if(c.attribute==='commute_min'||c.attribute==='travel_mode'){
   const travel=(r.travel_evidence||[]).filter(t=>c.object==='all_participants'||t.participant===c.object);
   parts.push(travel.map(t=>`${esc(t.participant)} ${esc(t.travel_mode)} ${esc(t.travel_min)} min`).join(' / '));
  }
 }
 if(facts?.activity?.max_commute_min!==undefined)parts.push((r.travel_evidence||[]).map(t=>`${esc(t.participant)} ${esc(t.travel_min)} min`).join(' / '));
 const originalVenue=facts?.candidate_venues?.find(v=>v.name===p.name);
 if(originalVenue){
  if(originalVenue.max_party!==undefined)parts.push(`${label('人数')} ${facts.participants.length}；${label('容量')} ${esc(originalVenue.max_party)}`);
  if(facts.participants.some(x=>x.dietary&&!['none','无'].includes(x.dietary)))parts.push(`${label('提供')} ${esc(mrValue(originalVenue.dietary_options))}`);
  if(facts.participants.some(x=>x.budget!==undefined))parts.push(`${label('人均价格')} ${esc(originalVenue.price_per_person)}`);
 }
 return parts.filter(Boolean).join('；');
}
function multiComparisonHTML(data){
 const {sb,places,expected}=multiReviewFacts(data),label=discoveryLabel;
 const proof=sb.candidate_choice_verification||data.candidate_choice_verification;
 const facts=sb.scenario?.public_fact_projection||data.public_fact_projection||proof?.public_constraints;
 const activity=facts?.activity||facts?.meeting||{},people=facts?.participants||[];
 const requirements=facts?`${mrFields([['活动类型',activity.activity_type],['地点类型',activity.required_place_type],['活动时长（分钟）',activity.duration_min],['到达方式',activity.arrival_mode],['允许交通方式',activity.allowed_travel_modes],['通勤上限（分钟）',activity.max_commute_min]])}<div class="mr-c-people">${people.map(p=>`<span><strong>${esc(p.name)}</strong> ${esc(p.available_from)} — ${esc(p.must_leave_by)}${p.budget!==undefined?` · ${label('预算')} ${esc(p.budget)}`:''}${p.dietary&&!['none','无'].includes(p.dietary)?` · ${label('饮食')} ${esc(p.dietary)}`:''}</span>`).join('')}</div>${(facts.additional_constraints||[]).length?`<ul class="mr-added-c">${facts.additional_constraints.map(c=>`<li>${mrCondition(c)}</li>`).join('')}</ul>`:''}`:`<p class="readonly-note">${label('未保存本题关键 C')}</p>`;
 const rows=places.map(p=>{
  const r=proof?.candidate_results?.find(r=>r.place_id===multiReviewId(p)||(p.service_id&&r.service_id===p.service_id)),status=r?.status;
  const reason=status==='excluded'?(r.failures||[]).map(mrFailure).join('<br>'):status==='feasible'?mrSuccessEvidence(r,p,facts):status==='unresolved'?(r.missing_evidence||[]).map(g=>`${label('缺少证据')}：${esc(g.participant||'')} ${label(g.field==='google_types'?'地点类型':'路线耗时')}${g.required_modes?` · ${esc(mrValue(g.required_modes))}`:''}`).join('<br>'):label('未保存核验结果');
  const details=r?`<details class="mr-candidate-detail"><summary>${label('查看详情')}</summary><div class="mr-candidate-body">${mrFields([['地点类型',p.primary_type],['实际类型',p.types],['共同活动时间',r.meeting_start?`${r.meeting_start} — ${r.meeting_end}`:null]])}${(r.travel_evidence||[]).length?`<table><thead><tr>${['参与者','交通方式','耗时','到达时间'].map(h=>`<th>${label(h)}</th>`).join('')}</tr></thead><tbody>${r.travel_evidence.map(t=>`<tr><td>${esc(t.participant)}</td><td>${esc(t.travel_mode)}</td><td>${esc(t.travel_min)} min</td><td>${mrClock(t.arrival_min)}</td></tr>`).join('')}</tbody></table>`:''}${mrRaw({candidate:p,verification:r})}</div></details>`:'';
  const warning=(status==='feasible'&&multiReviewId(p)!==expected)?`<div class="mr-inline-warning mr-feasible">${label('该候选仍满足本题 C')}</div>`:status==='unresolved'?`<div class="mr-inline-warning mr-unresolved">${label('证据不足')}</div>`:'';
  return `<tr><td><button class="mr-link ${multiReviewId(p)===expected?'mr-gt':''}" onclick="focusMultiCandidateOrGT('${escAttr(multiReviewId(p))}')">${multiReviewId(p)===expected?'GT · ':''}${esc(p.name||p.place_name)}</button></td><td class="mr-${status||'unknown'}-evidence">${warning}${reason}${details}</td></tr>`;
 }).join('');
 return `<div class="mr-reference"><h3>${label('本题关键 C')}</h3>${requirements}</div><div class="mr-comparison"><h3>${label('候选比较')}</h3><div class="mr-table"><table><thead><tr><th>${label('名称')}</th><th>${label('关键对比依据')}</th></tr></thead><tbody>${rows}</tbody></table></div></div>`;
}
function focusMultiCandidateOrGT(id){const {people,expected}=multiReviewFacts(multiReviewData);if(id===expected)focusMultiReview(people.length);else focusMultiCandidate(id);}
function multiPlanHTML(i){const {plans,people}=multiReviewFacts(multiReviewData),p=plans[i];if(!p)return `<p class="readonly-note">${discoveryLabel('未保存协调方案')}</p>`;return mrFields([['共同活动时间',p.meeting_start&&p.meeting_end?`${p.meeting_start} — ${p.meeting_end}`:null],['合计通勤（分钟）',p.total_commute_min],['最长个人通勤（分钟）',p.max_individual_commute_min]])+`<div class="mr-assignments">${(p.assignments||[]).map(a=>`<span>${esc(people.find(x=>x.character_id===a.character_id)?.name||a.character_id)} · ${discoveryLabel(a.arrival_mode==='independent'?'独立到达':a.arrival_mode||'未记录')}</span>`).join('')}</div>`+mrRaw(p);}
function renderMultiPlan(i){document.getElementById('mr-plan').innerHTML=multiPlanHTML(i);}
function focusMultiReview(i){const m=multiReviewMarkers[i];if(m&&multiReviewMap){multiReviewMap.setView(m.getLatLng(),14);m.openPopup();}}
let multiCandidateLayers=[];
function focusMultiCandidate(id){const {places,expected}=multiReviewFacts(multiReviewData);const candidates=places.filter(p=>multiReviewId(p)!==expected);const p=candidates.find(p=>multiReviewId(p)===id);if(!p)return;const m=multiCandidateLayers.find(m=>m.options.reviewPlaceId===id);if(m){multiReviewMap.setView(m.getLatLng(),15);m.openPopup();}}
function toggleMultiCandidates(show){multiCandidateLayers.forEach(m=>show?m.addTo(multiReviewMap):multiReviewMap.removeLayer(m));}
async function initMultiReviewMap(data){
 if(multiReviewMap)multiReviewMap.remove();multiReviewMarkers=[];multiCandidateLayers=[];
 const el=document.getElementById('multi-review-map');if(!el||!window.L)return;const map=multiReviewMap=L.map(el,{scrollWheelZoom:false}),bounds=L.latLngBounds([]),{people,places,expected}=multiReviewFacts(data);
 function marker(loc,name,person,color){if(!Number.isFinite(loc?.lat)||!Number.isFinite(loc?.lng))return null;const m=person?L.marker([loc.lat,loc.lng],{icon:L.divIcon({className:'mr-person',html:'👤',iconSize:[20,20],iconAnchor:[10,10]})}):L.circleMarker([loc.lat,loc.lng],{radius:color==='#e94560'?8:5,color:'#fff',weight:1.5,fillColor:color,fillOpacity:.9});m.bindPopup(esc(name));return m;}
 people.forEach((p,i)=>{const m=marker({lat:p.home_lat,lng:p.home_lng},p.name,true);if(m){m.addTo(map);multiReviewMarkers[i]=m;bounds.extend(m.getLatLng());}});
 places.forEach(p=>{const gt=multiReviewId(p)===expected,m=marker(p.location,p.name||p.place_name,false,gt?'#e94560':'#3388ff');if(!m)return;m.options.reviewPlaceId=multiReviewId(p);m.addTo(map);bounds.extend(m.getLatLng());if(gt){multiReviewMarkers[people.length]=m;}else multiCandidateLayers.push(m);});
 if(bounds.isValid())map.fitBounds(bounds.pad(.2),{maxZoom:14});else map.setView([0,0],2);
 multiReviewMarkers[people.length]?.bringToFront();
 try{const layer=await createBenchBasemapLayer();if(el.isConnected&&multiReviewMap===map)layer.addTo(map);}catch(_){}if(el.isConnected)map.invalidateSize();
}
