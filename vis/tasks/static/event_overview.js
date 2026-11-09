let aeState={city:'',time:'',zone:'',basemap:'osm',layers:'events',panel:'events',data:null,key:'',request:0,selected:null},aeMap=null,aeLayers={},aeBaseLayer=null,aeBaseRequest=0;
function aeDispose(){aeBaseRequest++;aeBaseLayer=null;if(aeMap){aeMap.remove();aeMap=null;}aeLayers={};}
function aeRender(){
 if(!acOverview||currentView!=='app-content')return;
 axDisposeMap();aeDispose();
 const host=document.getElementById('ac-records'),s=aeState;
 host.innerHTML=`<div class="am-header"><h3 data-ui-text>事件总览</h3></div><form class="am-form ae-form" onsubmit="event.preventDefault();aeQuery()"><label data-ui-text>城市<select id="ae-city" onchange="aeState.city=this.value;aeState.selected=null;aeQuery()"><option value="" data-ui-text>请选择城市</option>${(s.data?.cities||[]).map(c=>`<option value="${escAttr(c.city_id)}" ${c.city_id===s.city?'selected':''}>${esc(c.name)} · v${esc(String(c.version))}</option>`).join('')}</select></label><label data-ui-text>查看时间（可选）<input data-ui-placeholder id="ae-time" type="datetime-local" value="${escAttr(s.time)}" oninput="aeState.time=this.value"></label><label data-ui-text>时区（UTC 偏移）<input data-ui-placeholder id="ae-zone" placeholder="例如 +08:00" value="${escAttr(s.zone)}" oninput="aeState.zone=this.value"></label><button class="nav-btn" ${!acScope?'disabled':''} data-ui-text>查看事件</button></form><p class="ac-note" data-ui-text>选择城市查看事件分布；不填时间时展示全部已记录事件。</p><p id="ae-status" class="ac-note" role="status" data-ui-output></p><div class="ae-content"><div><div id="ae-map"></div><div id="ae-legend" class="ae-legend"></div><p id="ae-map-note" class="ac-note" data-ui-output></p></div><div id="ae-events" class="ae-events"></div></div>`;
 host.querySelector('.ae-form > button').insertAdjacentHTML('beforebegin',`<label data-ui-text>显示内容<select id="ae-layers" onchange="aeSetLayers(this.value)"><option value="events" ${s.layers==='events'?'selected':''} data-ui-text>事件</option><option value="events_protection" ${s.layers==='events_protection'?'selected':''} data-ui-text>事件 + 保护</option></select></label><label data-ui-text>地图底图<select id="ae-basemap" onchange="aeSetBasemap(this.value)">${[['osm','OSM'],['osm-gray','OSM（灰底）'],['google','Google'],['google-gray','Google（灰底）']].map(([value,label])=>`<option data-ui-output value="${value}" ${s.basemap===value?'selected':''}>${label}</option>`).join('')}</select></label>`);
 const timeInput=document.getElementById('ae-time');timeInput.type='text';timeInput.readOnly=true;timeInput.placeholder='全部时间';timeInput.value=s.time.replace('T',' ');timeInput.removeAttribute('oninput');timeInput.onclick=aeTimeOpen;timeInput.setAttribute('aria-haspopup','dialog');
 timeInput.parentElement.classList.add('ae-time-field');
 timeInput.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();aeTimeOpen();}if(e.key==='Escape')document.getElementById('ae-calendar')?.remove();};
 document.getElementById('ae-zone').addEventListener('input',()=>document.getElementById('ae-calendar')?.remove());
 const key=JSON.stringify([acScope?.world_id,acMode,s.city,s.time,s.zone,s.layers]);
 if(acScope&&s.key!==key){aeQuery();return;}
 aeDraw();
}
async function aeQuery(){
 if(!acScope)return;
 const s=aeState,key=JSON.stringify([acScope.world_id,acMode,s.city,s.time,s.zone,s.layers]),request=++s.request;
 const status=document.getElementById('ae-status');
 if(s.time&&!/^[+-](?:0\d|1[0-4]):[0-5]\d$/.test(s.zone)){status.textContent=GeoUI.t('请填写查看时间对应的 UTC 偏移，例如 +08:00。');return;}
 status.textContent=GeoUI.t('正在读取事件…');
 try{const params=new URLSearchParams({world:acScope.world_id,mode:acMode,city:s.city,time:s.time?s.time+s.zone:'',layers:s.layers});const data=await dynamicJSON('/api/app-content/event-overview?'+params);if(request!==s.request||!acOverview||currentView!=='app-content'||key!==JSON.stringify([acScope?.world_id,acMode,s.city,s.time,s.zone,s.layers]))return;s.data=data;s.key=key;s.selected=null;aeRender();}
 catch(e){if(request===s.request&&status.isConnected)status.textContent=GeoUI.t('读取失败：{detail}', {detail: e.message});}
}
let aeCalendar={parts:[],level:2,page:2020};
const aeUnits=['年','月','日','时','分'];
function aeOffset(){const z=aeState.zone;return /^[+-]\d{2}:\d{2}$/.test(z)?(z[0]==='-'?-1:1)*(Number(z.slice(1,3))*60+Number(z.slice(4)))*60000:0;}
function aeHasEvent(start,end){return (aeState.data?.available_windows||[]).some(w=>Date.parse(w.start)<end&&(!w.end||Date.parse(w.end)>start));}
function aeMinuteActive(day,minute){const point=Date.parse(day+'T00:00:00Z')-aeOffset()+minute*60000;return aeHasEvent(point,point+1);}
function aeStamp(parts){
 const [y,m=1,d=1,h=0,n=0]=parts;
 return Date.parse(String(y).padStart(4,'0')+'-'+String(m).padStart(2,'0')+'-'+String(d).padStart(2,'0')+'T'+String(h).padStart(2,'0')+':'+String(n).padStart(2,'0')+':00Z');
}
function aeAvailable(parts){
 const level=parts.length-1,start=aeStamp(parts)-aeOffset(),date=new Date(start+aeOffset());
 if(level===0)date.setUTCFullYear(date.getUTCFullYear()+1);
 if(level===1)date.setUTCMonth(date.getUTCMonth()+1);
 if(level===2)date.setUTCDate(date.getUTCDate()+1);
 if(level===3)date.setUTCHours(date.getUTCHours()+1);
 if(level===4)return aeHasEvent(start,start+1);
 const end=date.getTime()-aeOffset();
 return (aeState.data?.available_windows||[]).some(w=>{
  const first=Math.ceil(Math.max(start,Date.parse(w.start))/60000)*60000;
  return first<Math.min(end,w.end?Date.parse(w.end):Infinity);
 });
}
function aeTimeOpen(){
 document.getElementById('ae-calendar')?.remove();
 const s=aeState;
 if(!s.city||s.data?.city_id!==s.city||s.data?.mode!==acMode){document.getElementById('ae-status').textContent=GeoUI.t('请先选择城市并读取事件。');return;}
 const windows=s.data.available_windows||[];
 if(!s.zone&&windows.length){s.zone=windows[0].start.match(/(Z|[+-]\d{2}:\d{2})$/)?.[0]?.replace('Z','+00:00')||'';document.getElementById('ae-zone').value=s.zone;}
 if(windows.length&&!/^[+-](?:0\d|1[0-4]):[0-5]\d$/.test(s.zone)){document.getElementById('ae-status').textContent=GeoUI.t('请先填写有效时区。');return;}
 const first=windows.length?new Date(Math.min(...windows.map(w=>Date.parse(w.start)))+aeOffset()).toISOString().slice(0,10):'2026-01-01';
 const base=(s.time||first).split(/[-T:]/).map(Number);
 aeCalendar={parts:s.time?base:base.slice(0,2),level:2,page:Math.floor(base[0]/12)*12};
 document.getElementById('ae-time').parentElement.insertAdjacentHTML('beforeend','<div id="ae-calendar" role="dialog" aria-label="选择有事件的时间" data-ui-aria-label></div>');
 aeTimeDraw();
}
function aeLevel(level){if(level>aeCalendar.parts.length)return;aeCalendar.level=level;if(level===0)aeCalendar.page=Math.floor((aeCalendar.parts[0]||2026)/12)*12;aeTimeDraw();}
function aePick(value){
 const c=aeCalendar,prefix=c.parts.slice(0,c.level).concat(value);
 if(!aeAvailable(prefix))return;
 c.parts=prefix;c.level=Math.min(4,c.level+1);aeTimeDraw();
}
function aeYearPage(delta){aeCalendar.page=Math.max(0,Math.min(9996,aeCalendar.page+delta*12));aeTimeDraw();}
function aeTimeDraw(){
 const panel=document.getElementById('ae-calendar');if(!panel)return;
 const c=aeCalendar,p=c.parts,l=c.level;
 const crumb=aeUnits.map((unit,i)=>`<button type="button" data-ui-output data-ui-aria-label class="nav-btn ${l===i?'active':''}" ${i>p.length?'disabled':''} onclick="aeLevel(${i})" aria-label="选择${unit}" aria-pressed="${l===i}">${p[i]==null?'—':p[i]}${unit}</button>`).join('');
 let values=[],padding='',title='选择'+['年份','月份','日期','小时','分钟'][l];
 if(l===0){values=Array.from({length:12},(_,i)=>c.page+i);title=c.page+'—'+Math.min(9999,c.page+11);}
 if(l===1)values=Array.from({length:12},(_,i)=>i+1);
 if(l===2){
  const date=new Date(aeStamp(p.slice(0,2))),last=new Date(date);last.setUTCMonth(last.getUTCMonth()+1);last.setUTCDate(0);
  values=Array.from({length:last.getUTCDate()},(_,i)=>i+1);
  padding=(GeoUI.language==='en'?['Mon','Tue','Wed','Thu','Fri','Sat','Sun']:['一','二','三','四','五','六','日']).map(d=>'<span>'+d+'</span>').join('')+'<span></span>'.repeat((date.getUTCDay()+6)%7);
 }
 if(l===3)values=Array.from({length:24},(_,i)=>i);
 if(l===4)values=Array.from({length:60},(_,i)=>i);
 const cells=values.map(v=>{const enabled=v>=0&&(l!==0||(v>=1&&v<=9999))&&aeAvailable(p.slice(0,l).concat(v));return `<button type="button" data-ui-title data-ui-output data-value="${v}" class="ae-day ${p[l]===v?'selected':''}" ${enabled?'':'disabled'} title="${enabled?'有事件':'无事件'}" onclick="aePick(${v})">${l>=3?String(v).padStart(2,'0'):v}${l===0?'年':l===1?'月':''}</button>`;}).join('');
 const complete=p.length===5&&aeAvailable(p);
 panel.innerHTML=`<div class="ae-crumbs">${crumb}</div><div class="ae-calendar-head">${l===0?`<button type="button" class="nav-btn" data-ui-aria-label aria-label="上一页年份" ${c.page===0?'disabled':''} onclick="aeYearPage(-1)">‹</button>`:''}<span data-ui-output>${title}</span>${l===0?`<button type="button" class="nav-btn" data-ui-aria-label aria-label="下一页年份" ${c.page>=9996?'disabled':''} onclick="aeYearPage(1)">›</button>`:''}</div><div class="ae-calendar-grid ae-level-${l}">${padding}${cells}</div><p class="ac-note" data-ui-output>${(aeState.data?.warnings||[]).length?'部分数据读取异常，仅标记已读到的事件。':'灰色选项无事件，不可选择。'} UTC${esc(aeState.zone)}</p><div class="ae-calendar-actions"><button type="button" class="nav-btn" onclick="aeTimeClear()" data-ui-text>全部时间</button><button type="button" class="nav-btn" onclick="document.getElementById('ae-calendar').remove()" data-ui-text>取消</button><button type="button" class="nav-btn" id="ae-confirm" ${complete?'':'disabled'} onclick="aeTimeApply()" data-ui-text>确定</button></div>`;
}
function aeTimeClear(){aeState.time='';document.getElementById('ae-time').value='';document.getElementById('ae-calendar')?.remove();aeQuery();}
function aeTimeApply(){
 const p=aeCalendar.parts;if(p.length!==5||!aeAvailable(p))return;
 aeState.time=new Date(aeStamp(p)).toISOString().slice(0,16);
 document.getElementById('ae-time').value=aeState.time.replace('T',' ');
 document.getElementById('ae-calendar').remove();aeQuery();
}
function aeDraw(){
 const s=aeState,rows=s.data?.events||[],protections=s.layers==='events_protection'?(s.data?.protections||[]):[],list=document.getElementById('ae-events');
 document.getElementById('ae-status').textContent=(s.data?.warnings||[]).join('；');
 const eventCards=rows.map((r,i)=>{const selected=s.selected===`event:${i}`;return `<article class="ac-record ae-event ${selected?'active':''}" id="ae-event-${i}"><div class="ae-app-label" data-ui-output>${esc(acNames[r.app]||r.app)}</div><h4>${esc(r.title)}</h4><p class="ac-note" data-ui-output>${esc(axTimeRange(r.active_window))}</p><button class="nav-btn" onclick="aeSelect('event',${i})" aria-expanded="${selected}" data-ui-output>${selected?'收起详情':'查看详情'}</button>${!r.geometry?'<span class="ac-note" data-ui-text> 未提供可绘制范围</span>':''}${selected?`<div class="ax-notice-body">${esc(r.description||'未提供正文')}</div>`:''}</article>`}).join('');
 const protectionCards=protections.map((r,i)=>{const selected=s.selected===`protection:${i}`;const kind=r.dependency_kind==='route'?'路线保护':'地点保护';return `<article class="ac-record ae-event ae-protection ${selected?'active':''}" id="ae-protection-${i}"><div class="ae-app-label" data-ui-output>Safety · ${kind}</div><h4>${esc(r.task_id)}</h4><p class="ac-note" data-ui-output>${esc(axTimeRange(r.active_window))}</p><button class="nav-btn" onclick="aeSelect('protection',${i})" aria-expanded="${selected}" data-ui-output>${selected?'收起详情':'查看保护详情'}</button>${selected?`<div class="ax-notice-body" data-ui-output>候选：${esc(r.candidate_ref||'未提供')}<br>范围引用：${esc(r.scope_ref||'未提供')}<br>来源：${esc((r.source_refs||[]).join('、')||'未提供')}</div>`:''}</article>`}).join('');
 const tabs=s.layers==='events_protection'?`<div class="ae-layer-tabs"><button type="button" class="nav-btn ${s.panel==='events'?'active':''}" data-ui-output onclick="aeSetPanel('events')">事件（${rows.length}）</button><button type="button" class="nav-btn ${s.panel==='protection'?'active':''}" data-ui-output onclick="aeSetPanel('protection')">保护范围（${protections.length}）</button></div>`:'';
 const visible=s.panel==='protection'?protectionCards:eventCards;
 const summary=`<div class="ae-count" data-ui-output>${rows.length} 条事件 · ${rows.filter(r=>r.geometry).length} 条可定位${s.layers==='events_protection'?` · ${protections.length} 个保护范围`:''}</div>`;
 list.innerHTML=!s.city?'<p class="ac-note" data-ui-text>请先选择城市。</p>':`${tabs?`<div class="ae-list-header">${tabs}${summary}</div>`:summary}${visible||`<p class="ac-note" data-ui-output>当前条件下没有可显示的${s.panel==='protection'?'保护范围':'事件'}。</p>`}`;
 const host=document.getElementById('ae-map');if(!window.L){host.textContent='地图组件未加载；事件仍可在右侧查看。';return;}
 aeMap=L.map(host,{scrollWheelZoom:true});aeMap.setView([20,0],2);
 const bounds=L.latLngBounds([]),eventColor='#e06b4f',protectionColor='#4ca6c8';
 rows.forEach((r,i)=>{if(!r.geometry)return;const selected=s.selected===`event:${i}`,layer=L.geoJSON(r.geometry,{style:{color:eventColor,weight:selected?4:2,fillOpacity:selected?.32:.16},pointToLayer:(_,latlng)=>L.circleMarker(latlng,{radius:7,color:eventColor,fillOpacity:.8})}).addTo(aeMap);layer.bindTooltip(esc(r.title),{className:'ae-map-tooltip'});layer.on('click',()=>aeSelect('event',i,true));bounds.extend(layer.getBounds());aeLayers[`event:${i}`]=layer;});
 protections.forEach((r,i)=>{if(!r.geometry)return;const selected=s.selected===`protection:${i}`,layer=L.geoJSON(r.geometry,{style:{color:protectionColor,weight:selected?4:2,dashArray:selected?null:'7 5',fillOpacity:selected?.18:.07}}).addTo(aeMap);const taskId=r.task_id||'',shortTask=taskId.match(/(\d+)$/)?.[1]||'—',taskType=taskId.includes('safety_planning')?'Safety':taskId.includes('dynamic_replanning')?'动态':'任务';layer.bindTooltip(`${r.dependency_kind==='route'?'路线':'地点'}保护 · ${taskType} 任务 ${shortTask}`,{className:'ae-map-tooltip'});layer.on('click',()=>aeSelect('protection',i,true));bounds.extend(layer.getBounds());aeLayers[`protection:${i}`]=layer;});
 if(bounds.isValid()&&!s.selected)aeMap.fitBounds(bounds,{padding:[32,32],maxZoom:16});
 document.getElementById('ae-map-note').textContent='点击地图范围查看详情。无定位记录仅列在右侧。';
 document.getElementById('ae-legend').innerHTML=s.layers==='events_protection'?'<span class="ae-legend-event" data-ui-text>● 事件影响</span><span class="ae-legend-protection" data-ui-text>┄ 保护范围</span>':'';
 aeMountBasemap();
}
function aeSetLayers(value){if(!['events','events_protection'].includes(value))return;aeState.layers=value;aeState.panel='events';aeState.key='';aeState.selected=null;aeQuery();}
function aeSetPanel(value){if(!['events','protection'].includes(value))return;aeState.panel=value;aeState.selected=null;aeDraw();}
function aeSetBasemap(value){if(!['osm','osm-gray','google','google-gray'].includes(value))return;aeState.basemap=value;aeMountBasemap();}
async function aeMountBasemap(){
 const map=aeMap,request=++aeBaseRequest;if(!map)return;
 if(aeBaseLayer){map.removeLayer(aeBaseLayer);aeBaseLayer=null;}
 const note=document.getElementById('ae-map-note'),current=()=>request===aeBaseRequest&&map===aeMap&&note?.isConnected;
 const fail=message=>{if(current())note.textContent=message;};
 if(current())note.textContent='点击地图范围查看详情。无定位记录仅列在右侧。';
 try{let layer;
  if(aeState.basemap==='google'||aeState.basemap==='google-gray'){
   if(!viewerConfig.google_maps_api_key){fail('Google 底图未配置，事件范围仍可查看。');return;}
   await ensureGoogleBasemap();if(!current())return;
   layer=L.gridLayer.googleMutant({type:'roadmap',maxZoom:21,styles:aeState.basemap==='google-gray'?[{featureType:'all',elementType:'all',stylers:[{saturation:-100}]}]:[]});
  }else layer=L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,className:aeState.basemap==='osm-gray'?'ae-gray-tiles':'',attribution:'&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'});
  if(!current())return;aeBaseLayer=layer;layer.on('tileerror',()=>fail('底图加载失败，事件范围仍可查看。'));layer.addTo(map);
 }catch{fail('底图加载失败，事件范围仍可查看。');}
}
function aeSelect(kind,index,fromMap=false){const key=`${kind}:${index}`;aeState.panel=kind==='protection'?'protection':'events';aeState.selected=aeState.selected===key?null:key;const old=aeMap?{center:aeMap.getCenter(),zoom:aeMap.getZoom()}:null;aeDispose();aeDraw();if(fromMap&&old&&aeMap)aeMap.setView(old.center,old.zoom);if(fromMap)document.getElementById(`ae-${kind}-${index}`)?.scrollIntoView({block:'nearest',behavior:'smooth'});else if(aeState.selected){const focus=()=>{const layer=aeLayers[aeState.selected];if(layer&&aeMap){aeMap.invalidateSize({pan:false});const bounds=layer.getBounds();if(bounds.isValid())aeMap.fitBounds(bounds,{padding:[40,40],maxZoom:17});}};requestAnimationFrame(()=>{focus();setTimeout(focus,120);setTimeout(focus,300);});}}
