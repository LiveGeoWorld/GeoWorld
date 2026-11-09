const API = '/api';
let S = { tasks:[], idx:-1, data:null, map:null, markers:{}, lines:[], hlLines:[], reviews:{},
    edDi:null, edEi:null, edIsNew:false, };

const CC = {hotel:'#f97583',restaurant:'#ffa657',attraction:'#79c0ff',cafe:'#7ee787',transit:'#d2a8ff',other:'#666'};
const TC = {arrival:'et-ar',departure:'et-dep',hotel_checkin:'et-hc',checkout:'et-co',
    lunch:'et-lu',dinner:'et-di',breakfast:'et-br',morning_activity:'et-ma',
    afternoon_activity:'et-aa',evening_leisure:'et-el',business_meeting:'et-bm'};

async function init() {
    S.map = L.map('map').setView([31.23,121.47],12);
    const googleTile = L.tileLayer('https://mt{s}.google.com/vt/lyrs=m&x={x}&y={y}&z={z}',{subdomains:'0123',maxZoom:18,attribution:'&copy; Google'}).addTo(S.map);
    const osmTile = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{attribution:'&copy; OpenStreetMap contributors',maxZoom:19});
    googleTile.on('tileerror',function(){S.map.removeLayer(googleTile);osmTile.addTo(S.map);});
    document.getElementById('btn-prev').onclick = ()=>nav(-1);
    document.getElementById('btn-next').onclick = ()=>nav(1);
    document.getElementById('btn-list').onclick = openList;
    document.getElementById('prompt-text').onchange = savePrompt;
    document.getElementById('btn-edit-save').onclick = saveEditFromModal;
    document.querySelectorAll('.section-title').forEach(el=>el.onclick=()=>{
        const t=document.getElementById(el.dataset.target);
        if(t) t.style.display=t.style.display==='none'?'':'none';
    });
    await loadTasks(); await loadReviews();
    if(S.tasks.length) loadTask(0);
}

async function loadTasks(){try{const r=await fetch(API+'/tasks');S.tasks=(await r.json()).tasks||[];updateNav();}catch(e){console.error(e);}}
async function loadReviews(){try{S.reviews=await(await fetch(API+'/review_status')).json()||{};}catch(e){console.error(e);}}

async function loadTask(idx){
    if(idx<0||idx>=S.tasks.length) return;
    S.idx=idx;updateNav();
    try{
        const r=await fetch(API+'/task?folder='+encodeURIComponent(S.tasks[idx].folder));
        S.data = await r.json();
        render();
    }catch(e){console.error(e);}
}
function nav(d){loadTask(S.idx+d);}
function updateNav(){
    document.getElementById('task-counter').textContent=S.tasks.length?`${S.idx+1}/${S.tasks.length}`:'-';
    document.getElementById('btn-prev').disabled=S.idx<=0;
    document.getElementById('btn-next').disabled=S.idx>=S.tasks.length-1;
}

function sb(){ return (S.data||{}).task_sandbox||{}; }
function idays(){ return (sb().correct_itinerary||{}).days||[]; }
function pois(){ return sb()._poi_details||[]; }
function gen(){ return sb()._gen_summary||{}; }

// ── RENDER ──

function render() {
    if(!S.data) return;
    const g=gen();
    const ch=sb().character||{}, ht=sb().hotel||{};
    const items=[
        ['Task ID',(S.data.task||{}).task_id||'-'],
        ['Days',sb().num_days||'?'],['GT Events',g.gt_event_count||'?'],['POIs',g.total_pois||'?'],
        ['Hotel',(ht.name||'?')+' | '+(ht.district_name||'?')],
        ['Char',(ch.name||'?')+' | ¥'+(ch.budget_daily||'?')+'/d'],
        ['Model',g.model||'?'],
        ['Tokens',`${g.tokens_prompt||0}+${g.tokens_completion||0}=${g.tokens_total||0}`],
        ['LLM time',(g.llm_duration_s||0)+'s'],
        ['Route API',`${g.route_api_calls||0} calls (${g.route_cache_hits||0} hits)`],
        ['Audit',`${g.audit_warnings||0} warnings`],
    ];
    document.getElementById('meta-grid').innerHTML=items.map(([l,v])=>`<div class="meta-item"><span class="ml">${l}:</span><span class="mv">${v}</span></div>`).join('');
    document.getElementById('prompt-text').value=(S.data.task||{}).user_prompt||'';
    document.getElementById('ed-feasible').value=sb().expected_feasible?'true':'false';

    document.getElementById('menus-content').innerHTML = buildMenusHTML(sb().menus);
    buildAudit();
    buildConstraints();
    renderTimeline();
    renderMap();
    renderPoiGrid();
    loadReviewStatus();
}

function buildMenusHTML(menus){
    if(!menus||!menus.length) return '<div class="menu-slot">(no menu data)</div>';
    return menus.map(m=>{
        let h=`<div class="menu-day"><div class="menu-day-title">Day ${m.day}</div>`;
        for(const e of m.fixed_events||[]) h+=`<div class="menu-slot"><span class="ms-tag ms-fixed">fixed</span>${e.event_type} @ <b>${e.place_name||'?'}</b>${e.earliest?' ('+e.earliest+(e.latest?'-'+e.latest:'')+')':''}</div>`;
        for(const s of m.required_slots||[]) h+=`<div class="menu-slot"><span class="ms-tag ms-required">req</span>${s.event_type} [${(s.time_range||[]).join('-')}] → ${(s.candidates||[]).map(c=>c.name).join(', ')}</div>`;
        for(const s of m.optional_slots||[]) h+=`<div class="menu-slot"><span class="ms-tag ms-optional">opt</span>${s.event_type} [${(s.time_range||[]).join('-')}] → ${(s.candidates||[]).map(c=>c.name).join(', ')}</div>`;
        return h+'</div>';
    }).join('');
}

function buildAudit(){
    const warns=(sb()._audit||{}).warnings||[];
    const sec=document.getElementById('sec-audit'), body=document.getElementById('audit-body');
    if(!warns.length){sec.style.display='none';return;}
    sec.style.display=''; body.innerHTML=warns.map(w=>`<div class="aw">⚠ ${w}</div>`).join('');
}

function buildConstraints(){
    const c=sb().constraints||{}, keys=Object.keys(c);
    const sec=document.getElementById('sec-constraints'), body=document.getElementById('constraints-body');
    if(!keys.length){sec.style.display='none';return;}
    sec.style.display=''; body.innerHTML=keys.map(k=>`<div class="cr"><span>${k}:</span><span>${JSON.stringify(c[k])}</span></div>`).join('');
}

// ── TIMELINE ──

function renderTimeline(){
    const days=idays(), el=document.getElementById('timeline');
    if(!days.length){el.innerHTML='<div class="day-block"><div class="day-hdr">No GT itinerary</div></div>';return;}
    let h='';
    for(let di=0;di<days.length;di++){
        const evts=days[di].events||[];
        h+=`<div class="day-block"><div class="day-hdr">
          <span>Day ${days[di].day}</span>
          <span><span class="ed-add" onclick="openAdd(${di})">+ event</span> &nbsp; ${evts.length} evts</span>
        </div>`;
        for(let ei=0;ei<evts.length;ei++){
            const e=evts[ei];
            const prev=ei>0?evts[ei-1]:null;
            const comm=prev&&e.commute_min!=null?`${e.commute_min}m`:'';
            const war=isWarn(e)?' warn':'';
            const isFixed=e.event_type==='arrival'||e.event_type==='departure'||e.event_type==='hotel_checkin'||e.event_type==='checkout';
            h+=`<div class="event-row${war}" onclick="selEvent(${di},${ei})">
              <span class="ev-time">${e.start||'?'}-${e.end||'?'}</span>
              <span class="ev-commute">${comm}</span>
              <span class="ev-type ${TC[e.event_type]||''}">${e.event_type||'?'}</span>
              <span class="ev-name">${e.place_name||'?'}</span>
              <span class="ev-edit" onclick="openEdit(${di},${ei},event)">✏</span>
              ${!isFixed?`<span class="ev-del" onclick="deleteEvent(${di},${ei},event)">✕</span>`:''}
            </div>`;
        }
        h+='</div>';
    }
    el.innerHTML=h;
}

function isWarn(e){
    for(const w of(sb()._audit||{}).warnings||[]){
        if(w.includes(e.place_name||'')||w.includes(e.event_type||'')||w.includes(e.place_id||'')) return true;
    }
    return false;
}

function deleteEvent(di,ei,ev){ev.stopPropagation();
    const days=idays();if(di>=days.length)return;
    days[di].events.splice(ei,1);
    renderTimeline(); renderMap();
}

function openAdd(di){
    S.edDi=di; S.edEi=idays()[di].events.length; S.edIsNew=true;
    document.getElementById('edit-modal-title').textContent = 'Add Event to Day '+(di+1);
    document.getElementById('ed-day').textContent = 'Day '+(di+1);
    document.getElementById('ed-start').value = '12:00';
    document.getElementById('ed-end').value = '13:00';
    const sel=document.getElementById('ed-type');
    sel.innerHTML='<option value="lunch">lunch</option><option value="dinner">dinner</option><option value="breakfast">breakfast</option><option value="morning_activity">morning_activity</option><option value="afternoon_activity">afternoon_activity</option><option value="evening_leisure">evening_leisure</option>';
    const ps=pois(); sel.value='lunch';
    document.getElementById('ed-place').innerHTML=ps.map(p=>`<option value="${p.place_id}">${p.name} [${p.category}]</option>`).join('');
    document.getElementById('edit-modal').style.display='';
}

// ── EDIT MODAL ──

function openEdit(di,ei,ev){ev.stopPropagation();
    S.edDi=di;S.edEi=ei;S.edIsNew=false;
    const days=idays();if(di>=days.length)return;
    const e=days[di].events[ei];if(!e)return;
    document.getElementById('edit-modal-title').textContent='Edit Event';
    document.getElementById('ed-day').textContent='Day '+days[di].day;
    document.getElementById('ed-start').value=e.start||'';
    document.getElementById('ed-end').value=e.end||'';
    const sel=document.getElementById('ed-type');
    sel.innerHTML=`<option value="${e.event_type||'lunch'}">${e.event_type||'lunch'}</option>
      <option value="lunch">lunch</option><option value="dinner">dinner</option><option value="breakfast">breakfast</option>
      <option value="morning_activity">morning_activity</option><option value="afternoon_activity">afternoon_activity</option>
      <option value="evening_leisure">evening_leisure</option>`;
    sel.value=e.event_type||'lunch';
    const ps=pois();
    document.getElementById('ed-place').innerHTML=ps.map(p=>`<option value="${p.place_id}" ${p.place_id===e.place_id?'selected':''}>${p.name} [${p.category}]</option>`).join('');
    document.getElementById('edit-modal').style.display='';
}

function closeEdit(){document.getElementById('edit-modal').style.display='none';S.edDi=S.edEi=null;}

function saveEditFromModal(){
    if(S.edDi==null)return;
    const days=idays();if(S.edDi>=days.length)return;
    const ps=pois();
    const pid=document.getElementById('ed-place').value;
    const np=ps.find(p=>p.place_id===pid);
    const etype=document.getElementById('ed-type').value;

    if(S.edIsNew){
        days[S.edDi].events.push({
            event_type:etype, place_id:pid, place_name:np?np.name:'',
            category:np?np.category:'other', start:document.getElementById('ed-start').value,
            end:document.getElementById('ed-end').value, commute_from_prev_event_id:null,
            commute_mode:null, commute_min:0,
        });
    } else {
        const e=days[S.edDi].events[S.edEi]; if(!e) return;
        e.place_id=pid; if(np){e.place_name=np.name;e.category=np.category;}
        e.event_type=etype; e.start=document.getElementById('ed-start').value;
        e.end=document.getElementById('ed-end').value;
    }
    closeEdit(); renderTimeline(); renderMap();
}

// ── SAVE / APPROVE / REJECT ──

function onFeasibleChange(){sb().expected_feasible=document.getElementById('ed-feasible').value==='true';}

async function saveGT(){
    onFeasibleChange();
    const body={
        folder:S.tasks[S.idx].folder,
        correct_itinerary:sb().correct_itinerary,
        expected_feasible:sb().expected_feasible,
        constraints:sb().constraints,
        user_prompt:document.getElementById('prompt-text').value,
    };
    try{
        const r=await fetch(API+'/task/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
        const d=await r.json();
        document.getElementById('editor-status').innerHTML=d.status==='saved'
            ?'<span style="color:#7ee787">✅ GT saved</span>'
            :'<span style="color:#f78166">❌ Save failed</span>';
        setTimeout(()=>{document.getElementById('editor-status').innerHTML='';},2500);
        renderMap();
    }catch(e){console.error(e);}
}

async function approve(){
    await saveGT();
    try{
        await fetch(API+'/task/review',{method:'POST',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({folder:S.tasks[S.idx].folder,status:'approved',reviewed_at:new Date().toISOString()})});
        S.reviews[S.tasks[S.idx].folder]={status:'approved'};
        loadReviewStatus();
    }catch(e){console.error(e);}
}

async function reject(){
    try{
        await fetch(API+'/task/review',{method:'POST',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({folder:S.tasks[S.idx].folder,status:'rejected',reviewed_at:new Date().toISOString()})});
        S.reviews[S.tasks[S.idx].folder]={status:'rejected'};
        loadReviewStatus();
    }catch(e){console.error(e);}
}

function loadReviewStatus(){
    const rs=S.reviews[S.tasks[S.idx].folder];
    const el=document.getElementById('editor-status');
    if(rs&&rs.status) el.innerHTML=`<span style="color:${rs.status==='approved'?'#7ee787':'#f78166'}">${rs.status==='approved'?'✅ Approved':'❌ Rejected'}</span>`;
}

async function savePrompt(){await saveGT();}

// ── MAP ──

function renderMap(){
    S.map.eachLayer(l=>{if(l instanceof L.Marker||l instanceof L.Polyline) S.map.removeLayer(l);});
    S.markers={};S.lines=[];S.hlLines=[];
    const ps=pois(); const bounds=[]; let has=false;
    for(const p of ps){
        if(!p.lat)continue;
        const c=CC[p.category]||'#666';
        const icon=L.divIcon({className:'',html:`<div style="width:10px;height:10px;background:${c};border:2px solid #fff;border-radius:50%"></div>`,iconSize:[10,10],iconAnchor:[5,5]});
        const pop=`<b>${p.name}</b>${p.category} | ${p.district||''}<br>${p.opening_hours||''} | ${p.price_level||''} | ⭐${p.rating!=null?p.rating:'-'}`;
        const m=L.marker([p.lat,p.lng],{icon}).addTo(S.map).bindPopup(pop);
        S.markers[p.place_id]=m;bounds.push([p.lat,p.lng]);has=true;
    }
    if(has)s.map.fitBounds(bounds,{padding:[40,40]});
    const conns=sb()._route_connectors||[];
    for(const cn of conns){
        if(cn.polyline&&cn.polyline.polyline_encodings){
            for(const enc of cn.polyline.polyline_encodings){
                try{S.lines.push(L.polyline(L.Polyline.fromEncoded(enc).getLatLngs(),{color:'#30363d',weight:1,opacity:0.3}).addTo(S.map));}catch(e){}
            }
        }else if(cn.from_pid&&cn.to_pid&&S.markers[cn.from_pid]&&S.markers[cn.to_pid]){
            S.lines.push(L.polyline([S.markers[cn.from_pid].getLatLng(),S.markers[cn.to_pid].getLatLng()],{color:'#30363d',weight:1,opacity:0.2,dashArray:'4 6'}).addTo(S.map));
        }
    }
}

function selEvent(di,ei){
    clearHl();
    const days=idays();if(di>=days.length)return;
    const evts=days[di].events||[];if(ei>=evts.length)return;
    const e=evts[ei],prev=ei>0?evts[ei-1]:null;
    if(e.place_id&&S.markers[e.place_id])S.markers[e.place_id].openPopup();
    if(prev&&prev.place_id&&e.place_id){
        const cm=e.commute_mode||'DRIVE',cc=cm==='DRIVE'?'#ff7b72':cm==='TRANSIT'?'#d2a8ff':'#7ee787';
        const conns=sb()._route_connectors||[];
        for(const cn of conns){
            if(cn.from_pid===prev.place_id&&cn.to_pid===e.place_id&&cn.polyline&&cn.polyline.polyline_encodings){
                for(const enc of cn.polyline.polyline_encodings){
                    try{S.hlLines.push(L.polyline(L.Polyline.fromEncoded(enc).getLatLngs(),{color:cc,weight:3,opacity:0.9}).addTo(S.map));}catch(e){}
                }
            }
        }
        if(!S.hlLines.length&&S.markers[prev.place_id]&&S.markers[e.place_id]){
            S.hlLines.push(L.polyline([S.markers[prev.place_id].getLatLng(),S.markers[e.place_id].getLatLng()],{color:cc,weight:3,opacity:0.9}).addTo(S.map));
        }
    }
}
function clearHl(){S.hlLines.forEach(l=>S.map.removeLayer(l));S.hlLines=[];}

function renderPoiGrid(){
    document.getElementById('poi-grid').innerHTML=pois().map(p=>
        `<span class="poi-chip ch-${p.category}" onclick="focusPoi('${p.place_id}')" title="${p.name} | ${p.category} | ${p.opening_hours||''} | ${p.price_level||''}">${p.name}</span>`
    ).join('');
}
function focusPoi(pid){const m=S.markers[pid];if(m){S.map.setView(m.getLatLng(),14);m.openPopup();}}

// ── TASK LIST ──

function openList(){
    document.getElementById('list-content').innerHTML=S.tasks.map((t,i)=>{
        const rs=S.reviews[t.folder]||{}, mk=rs.status?`<span class="mk ${rs.status==='approved'?'a':'r'}">${rs.status==='approved'?'✓':'✗'}</span>`:'<span class="mk"></span>';
        const fe=(t.summary||{}).expected_feasible;
        return `<div class="lr" onclick="loadTask(${i});closeList()">${mk}<span class="lid">#${i+1}</span><span class="lfe ${fe===false?'no':'ok'}">${fe===false?'NO':'OK'}</span><span class="lpre">${(t.user_prompt||'').slice(0,60)}</span></div>`;
    }).join('');
    document.getElementById('list-modal').style.display='';
}
function closeList(){document.getElementById('list-modal').style.display='none';}

document.addEventListener('DOMContentLoaded',init);
