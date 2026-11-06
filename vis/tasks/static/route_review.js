let routeReviewMap=null,routeReviewPins=[],routeReviewLayers=[],routeReviewData=null,routeReviewPlan=0;
const rrLegColors=['#52bfa5','#659de0','#b995df','#dbb45f','#dc829c'];
function rrLegColor(i){return i<rrLegColors.length?rrLegColors[i]:`hsl(${(i*137.508)%360} 56% 65%)`;}
function routeReviewFacts(data){
 const sb=data.route_sandbox||data.task_sandbox||{},byId=new Map();
 for(const r of [...(sb.all_routes_in_task||[]),...(sb.gt_route?[sb.gt_route]:[]),...(sb.gt_segments||[])])if(r.route_id)byId.set(r.route_id,r);
 const primary=sb.correct_leg_route_ids?.length?sb.correct_leg_route_ids:sb.correct_route_id?[sb.correct_route_id]:[],plans=primary.length?[primary]:[];
 if(sb.expected_feasible===true)for(const ids of sb.acceptable_plan_ids||[])if(Array.isArray(ids)&&ids.length&&!plans.some(p=>JSON.stringify(p)===JSON.stringify(ids)))plans.push(ids);
 const core=sb.core_places||[];
 const places=[...core.filter(p=>p.role==='origin'),...core.filter(p=>p.role==='waypoint').sort((a,b)=>(a.sequence??0)-(b.sequence??0)),...core.filter(p=>p.role==='destination')];
 return {sb,places,plans,routes:(plans[routeReviewPlan]||plans[0]||[]).map(id=>byId.get(id)||{route_id:id,missing:true})};
}
function rrNumber(v){return typeof v==='number'&&Number.isFinite(v)?v:null;}
function rrUnavailableLabel(r){return r.route?.available===false&&r.route?.summary==='UNAVAILABLE'?'路线不支持查询':'路线不可用';}
function rrMetrics(r){const v=r.route||{};if(v.available===false)return {seconds:null,meters:null,walking:null};return {seconds:rrNumber(v.duration_s)??rrNumber(r.duration_s)??(rrNumber(r.duration_min)!==null?r.duration_min*60:null),meters:rrNumber(v.distance_m)??rrNumber(r.distance_m)??(rrNumber(r.distance_km)!==null?r.distance_km*1000:null),walking:rrNumber(v.walking_distance_m)??rrNumber(r.walking_distance_m)??rrNumber(r.walking_m)};}
function rrTime(s){return s===null?'—':`${Math.floor(s/60)} min${Math.round(s%60)?' '+Math.round(s%60)+' s':''}`;}
function rrDistance(m){return m===null?'—':`${Math.round(m)} m`;}
function rrField(k,v){return `<div><dt>${discoveryLabel(k)}</dt><dd>${esc(v)}</dd></div>`;}
function rrGeometry(r){const v=r.route||{},points=v.polyline_points||v.polyline;if(Array.isArray(points)&&points.length>1)return [points.map(p=>[p.lat,p.lng])];const enc=r.polyline_encodings?.length?r.polyline_encodings:(v.encoded_polyline?[v.encoded_polyline]:(v.steps||[]).map(s=>s.polyline?.encodedPolyline).filter(Boolean));return enc.map(e=>decodePolyline(e)).filter(p=>p.length>1);}
function renderRouteReview(data){
 routeReviewData=data;routeReviewPlan=0;
 const task=data.task,{sb,places}=routeReviewFacts(data),c=sb.constraints||{},label=discoveryLabel;
 const field=(k,v)=>v===null||v===undefined||v===''?'':rrField(k,v);
 const role=p=>p.role==='origin'?'起点':p.role==='destination'?'终点':'途经点';
 return renderPlacePromptEditor(task)+`<section class="section"><div class="section-title">${label('任务地图')}</div><div class="rr-map-layout"><div id="route-review-map"></div><aside class="rr-stops">${places.map((p,i)=>`<button onclick="focusRouteReviewStop(${i})"><small>${i+1} · ${label(role(p))}</small><strong>${esc(p.name||p.place_id||'—')}</strong></button>`).join('')}</aside></div><div id="rr-map-status" class="readonly-note"></div></section>
 <section class="section difficulty-panel quality-panel rr-quality"><div class="difficulty-panel-title">${label('质量审核面板 · Quality Profile')}</div>
 <section class="rr-section"><h3>${label('任务约束')}</h3><dl class="ds-detail-grid">${field('限时(分钟)',c.time_budget_min)}${field('出发时间',c.departure_time_str)}${field('到达截止时间',c.arrival_deadline_str)}${field('停留时间（分钟）',c.stop_durations_min?.length?c.stop_durations_min.join(' / '):c.stop_duration_min)}${field('禁用交通方式',(c.forbidden_modes||[]).join(' / '))}${field('步行上限（米）',c.max_walking_distance_m)}</dl></section>
 <section class="rr-section"><div class="rr-heading"><h3>GT · ${label('可行性')}</h3><span class="rr-verdict ${sb.expected_feasible===false?'rr-negative':''}">${label(sb.expected_feasible===true?'可行':sb.expected_feasible===false?'不可行':'未记录')}</span></div><div id="rr-route-detail">${renderRouteReviewDetails(data)}</div></section>
 <details class="gr-details"><summary>${label('来源与原始记录')}</summary><pre>${esc(JSON.stringify(sb,null,2))}</pre></details></section>`+renderReviewControls(task.task_type,data.review_status);
}
function renderRouteReviewDetails(data){
 const {sb,routes,places,plans}=routeReviewFacts(data),c=sb.constraints||{},label=discoveryLabel;
 const drive=routes.length&&routes.every(r=>rrMetrics(r).seconds!==null)?routes.reduce((s,r)=>s+rrMetrics(r).seconds,0):null;
 const stops=c.stop_durations_min?.length?c.stop_durations_min.every(v=>rrNumber(v)!==null)?c.stop_durations_min.reduce((s,v)=>s+v,0)*60:null:rrNumber(c.stop_duration_min)!==null?c.stop_duration_min*60:null;
 const total=drive!==null&&stops!==null?drive+stops:null,reason=sb.infeasibility_reason||sb.infeasible_reason||sb.feasibility_reason;
 return `<div class="rr-heading"><h3>${label(sb.expected_feasible===true?'可行路线':'路线依据')}</h3>${plans.length>1?`<select aria-label="${esc(GeoUI.t('路线方案'))}" onchange="selectRouteReviewPlan(this.value)">${plans.map((_,i)=>`<option value="${i}" ${i===routeReviewPlan?'selected':''}>${GeoUI.t('方案')} ${i+1}</option>`).join('')}</select>`:''}</div>
 <dl class="rr-totals">${rrField('行驶时间',rrTime(drive))}${rrField('停留时间',rrTime(stops))}${rrField('总耗时',rrTime(total))}${rrField('时间上限',rrTime(rrNumber(c.time_budget_min)!==null?c.time_budget_min*60:null))}</dl>
 ${typeof reason==='string'&&reason?`<p class="readonly-note">${esc(reason)}</p>`:''}
 ${routes.length?`<div class="rr-legs">${routes.map((r,i)=>{const m=rrMetrics(r);return `<article class="rr-leg" id="rr-leg-${i}"><button class="rr-leg-title" onclick="focusRouteReviewLeg(${i})"><span class="rr-leg-index">${i+1}</span><strong>${esc(r.origin_name||places[i]?.name||'—')} → ${esc(r.dest_name||places[i+1]?.name||'—')}</strong><span>${esc(r.mode||'—')}</span></button>${r.missing?`<p class="readonly-note">${label('本题未加载路线明细')}</p>`:`<dl class="rr-leg-metrics">${rrField('耗时',rrTime(m.seconds))}${rrField('距离',rrDistance(m.meters))}${rrField('步行距离',rrDistance(m.walking))}</dl>`}<details class="gr-details"><summary>${label('原始记录')}</summary><pre>${esc(JSON.stringify(r,null,2))}</pre></details></article>`;}).join('')}</div>`:`<p class="readonly-note">${label('本题未加载路线明细')}</p>`}`;
}
function focusRouteReviewStop(i){const pin=routeReviewPins[i];if(pin&&routeReviewMap){routeReviewMap.setView(pin.getLatLng(),16);pin.openPopup();}}
function focusRouteReviewLeg(i,fromMap=false){document.querySelectorAll('.rr-leg').forEach((el,n)=>el.classList.toggle('rr-selected',n===i));routeReviewLayers.forEach((g,n)=>g?.eachLayer(l=>{l.setStyle({weight:n===i?6:4,opacity:n===i?1:.25});if(n===i)l.bringToFront();}));const layer=routeReviewLayers[i];if(layer&&!fromMap)routeReviewMap.fitBounds(layer.getBounds().pad(.15));if(fromMap)document.getElementById('rr-leg-'+i)?.scrollIntoView({behavior:'smooth',block:'nearest'});}
function isolateRouteReviewLeg(i){routeReviewLayers.forEach((g,n)=>{if(!g)return;if(i<0||i===n){if(!routeReviewMap.hasLayer(g))g.addTo(routeReviewMap);g.eachLayer(l=>l.setStyle({opacity:.7,weight:4}));}else routeReviewMap.removeLayer(g);});if(i>=0)focusRouteReviewLeg(i);}
function selectRouteReviewPlan(value){routeReviewPlan=Number(value);document.getElementById('rr-route-detail').innerHTML=renderRouteReviewDetails(routeReviewData);initRouteReviewMap(routeReviewData);}
async function initRouteReviewMap(data){
 if(routeReviewMap)routeReviewMap.remove();routeReviewPins=[];routeReviewLayers=[];
 const el=document.getElementById('route-review-map');if(!el||!window.L)return;
 const map=routeReviewMap=L.map(el,{scrollWheelZoom:false}),{places,routes,sb}=routeReviewFacts(data),bounds=L.latLngBounds([]);
 const folder=data.task.folder_ref||currentFolder;let worldPoints=[];
 try{worldPoints=(await loadBenchWorldPoints(folder)).places||[];}catch(_){}
 if(!el.isConnected||routeReviewMap!==map)return;
 let missing=0;
 places.forEach((p,i)=>{const match=(data.world_slice?.places||[]).find(x=>x.place_id===p.place_id),world=worldPoints.find(x=>x.id===p.place_id||x.place_id===p.place_id);const point=[p,p.location,match?.location,world].find(x=>x&&Number.isFinite(x.lat)&&Number.isFinite(x.lng));if(!point){missing++;return;}const color=p.role==='destination'?'#e94560':p.role==='origin'?'#3388ff':'#e67e22';const pin=L.circleMarker([point.lat,point.lng],{radius:8,fillColor:color,color:'#fff',weight:2,fillOpacity:.95}).addTo(map).bindPopup(esc(p.name||''));pin.bindTooltip(String(i+1),{permanent:true,direction:'top'});routeReviewPins[i]=pin;bounds.extend(pin.getLatLng());});
 let lines=0;
 const legend=document.createElement('div');legend.className='rr-legend';
 legend.innerHTML=`<button onclick="isolateRouteReviewLeg(-1)">${GeoUI.t('全部路段')}</button>`+routes.map((r,i)=>`<button style="--leg-color:${rrLegColor(i)}" onclick="isolateRouteReviewLeg(${i})">${i+1} · ${esc(r.mode||'—')}${r.route?.available===false?' · '+GeoUI.t(rrUnavailableLabel(r)):''}</button>`).join('');
 document.querySelector('.rr-legend')?.remove();document.getElementById('rr-map-status').before(legend);
 document.querySelectorAll('.rr-leg').forEach((el,i)=>{el.style.setProperty('--leg-color',rrLegColor(i));if(routes[i]?.route?.available===false){const note=document.createElement('p');note.className='readonly-note';note.textContent=GeoUI.t(rrUnavailableLabel(routes[i]));el.querySelector('.rr-leg-metrics')?.replaceWith(note);}});
 routes.forEach((r,i)=>{if(r.route?.available===false)return;const paths=rrGeometry(r);if(!paths.length)return;const group=L.featureGroup().addTo(map);for(const pts of paths){const line=L.polyline(pts,{color:rrLegColor(i),weight:4,opacity:.7,dashArray:sb.expected_feasible===true?null:'6 5'}).addTo(group);line.bindTooltip(`${i+1} · ${esc(r.origin_name||'')} → ${esc(r.dest_name||'')}`);line.on('click',()=>focusRouteReviewLeg(i,true));}routeReviewLayers[i]=group;bounds.extend(group.getBounds());lines++;});
 if(bounds.isValid())map.fitBounds(bounds.pad(.2),{maxZoom:15});else map.setView([0,0],2);
 document.getElementById('rr-map-status').textContent=[missing?GeoUI.t('部分地点缺少坐标'):null,!routes.length?GeoUI.t('本题未加载路线明细'):null,routes.some((r,i)=>r.route?.available!==false&&!routeReviewLayers[i])?GeoUI.t('部分路线几何缺失，仅绘制已有证据'):null].filter(Boolean).join(' · ');
 try{const layer=await createBenchBasemapLayer();if(el.isConnected&&routeReviewMap===map)layer.addTo(map);}catch(_){}
 if(el.isConnected)map.invalidateSize();
}
