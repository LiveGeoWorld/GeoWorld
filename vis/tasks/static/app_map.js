/* App forms use public methods; suggestions also come from real MapApp search. */
let ax={},axEpoch=0,axTimer={},axLeaflet=null;
function amReset(){axDisposeMap();ax={};axEpoch++;for(const t of Object.values(axTimer))clearTimeout(t);axTimer={};}
function axState(){const key=acSelectedApp+':'+acMode;return ax[key]||(ax[key]={tab:'search',fields:{limit:10,mode:'ALL',hours:24},picks:{},suggestions:{},seq:{},result:null,trace:null,error:'',busy:false,detail:null});}
function axField(key,label,placeholder='',type='text'){const s=axState();if(type==='datetime-local')return axDateTimeField(key,label,s.fields[key]);return `<label data-ui-output>${label}<input data-ui-placeholder id="ax-${key}" type="${type}" value="${escAttr(s.fields[key]??'')}" placeholder="${escAttr(placeholder)}" oninput="axFieldInput('${key}',this.value)"></label>`;}
function axFieldInput(key,value){axState().fields[key]=value;axState().offset=0;const app=acSelectedApp;if(!value.trim()&&((app==='MapApp'&&['query','primary_type'].includes(key))||(app==='LocalServiceApp'&&key==='query')||(acMode==='static'&&((app==='WeatherApp'&&key==='district_id')||(app==='CityNewsApp'&&['district_id','info_type','time_range'].includes(key))))))axSubmit();}
function axPicker(key,label){const s=axState();return `<label class="ax-picker" data-ui-output>${label}<input data-ui-placeholder id="ax-${key}" autocomplete="off" value="${escAttr(s.fields[key]||'')}" placeholder="输入名称后，从下拉结果选择" oninput="axSuggest('${key}',this.value)" onfocus="if(!axState().picks['${key}'])axSuggest('${key}',this.value)"><div id="ax-options-${key}" class="ax-options"></div></label>`;}
function axRouteReady(){const s=axState();if(acSelectedApp!=='MapApp'||s.tab!=='routes')return;const button=document.querySelector('.am-form > button');if(button){const ready=!!(s.picks.origin&&s.picks.destination);button.disabled=s.busy||!acScope||!ready;button.setAttribute('data-ui-title','');button.title=ready?'查询缓存路线':'请先从下拉结果中选定起点和终点';}}
function axTrace(t){if(acSelectedApp==='LocalServiceApp')return (t?.warnings||[]).map(w=>`<p class="ac-note">${esc(w)}</p>`).join('');return t?`<details class="ac-record"><summary data-ui-output>${t.projection?'公开内容投影与读取方法':'请求与原始 Observation'}</summary><pre class="am-json">${esc(JSON.stringify({tool:t.tool,read_method:t.read_method,arguments:t.arguments},null,2))}</pre><pre class="am-json">${esc(JSON.stringify(t.observation,null,2))}</pre></details>${(t.warnings||[]).map(w=>`<p class="ac-note">${esc(w)}</p>`).join('')}`:'';}
const axLabels={name:'名称',address:'地址',location:'坐标',primary_type:'地点类型',google_types:'类别',title:'标题',description:'内容',scope:'范围',active_window:'有效时间',start:'开始',end:'结束',opening_hours:'营业时间',price_level:'价格等级',open_status:'营业状态',availability:'可用状态',merchant_information:'商户资料',slots:'可用时段',status:'状态',source:'来源',alerts:'预警',forecast:'预报',district:'区域',queue_time_min:'排队分钟',duration_min:'时长（分钟）',dietary_options:'饮食选项',max_party:'人数上限',crowding_level:'拥挤程度'};
function axValue(v){if(v==null||v==='')return '<span class="ac-note" data-ui-text>未提供</span>';if(Array.isArray(v))return v.length?`<ul>${v.map(x=>`<li>${axValue(x)}</li>`).join('')}</ul>`:'<span class="ac-note" data-ui-text>无条目</span>';if(typeof v==='object')return `<dl>${Object.entries(v).map(([k,x])=>`<dt data-ui-output>${esc(axLabels[k]||k)}</dt><dd>${axValue(x)}</dd>`).join('')}</dl>`;return esc(String(v));}
Object.assign(axLabels,{found:'是否找到',info_type:'信息类型',category:'类别',city_name:'城市',approximate:'近似范围',center:'中心位置',radius_m:'半径（米）',lat:'纬度',lng:'经度',count:'数量'});
function axTimeRange(window){
 if(GeoUI?.language==='en'){const f=v=>String(v||'').replace('T',' ');return window?.end?[f(window.start),f(window.end)].join(' — '):window?.start?f(window.start)+' onwards':'Time not provided';}
 const parse=value=>{const m=String(value||'').match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}:\d{2})(?::\d{2}(?:\.\d+)?)?(Z|[+-]\d{2}:\d{2})?$/);return m?{date:`${m[1]}年${Number(m[2])}月${Number(m[3])}日`,time:m[4],zone:m[5]==='Z'?'UTC':m[5]?'UTC'+m[5]:''}:null;};
 const a=parse(window?.start),b=parse(window?.end);
 if(a&&!window?.end)return `${a.date} ${a.time} 起${a.zone?'（'+a.zone+'）':''}`;
 if(!a||!b)return [window?.start,window?.end].filter(Boolean).join(' — ')||'未提供时间';
 if(a.date===b.date&&a.zone===b.zone)return `${a.date} ${a.time}–${b.time}${a.zone?'（'+a.zone+'）':''}`;
 const format=x=>`${x.date} ${x.time}${x.zone?'（'+x.zone+'）':''}`;
 return `${format(a)} — ${format(b)}`;
}
function axHours(hours){
 const english=window.GeoUI?.language==='en', tr=x=>window.GeoUI?.t(x)||x;
 if(typeof hours==='string')return hours.trim()||'未提供';if(!hours)return '未提供';
 const periods=hours.periods;if(Array.isArray(periods)&&periods.length){
  if(periods.length===1&&periods[0].open?.day===0&&!periods[0].close)return '每天 24 小时营业';
  const days=['周日','周一','周二','周三','周四','周五','周六'],groups=new Map(),clock=p=>`${String(p.hour||0).padStart(2,'0')}:${String(p.minute||0).padStart(2,'0')}`;
  for(const day of [1,2,3,4,5,6,0]){const entries=periods.filter(p=>p.open?.day===day&&p.close);if(!entries.length)continue;const value=entries.map(p=>`${clock(p.open)}–${p.close.day!==day?'次日 ':''}${clock(p.close)}`).join('、');groups.set(value,[...(groups.get(value)||[]),tr(days[day])]);}
  if(groups.size)return [...groups].map(([hours,days])=>`${days.length===7?tr('每天'):days.join(english?', ':'、')} ${hours}`).join('；');
 }
 const text=hours.weekdayDescriptions||hours.weekday_descriptions||hours.weekday_text;
 return Array.isArray(text)&&text.length?text.join('；'):'未提供可读时间';
}
function axServiceMoney(m){
 if(!m||typeof m!=='object')return typeof m==='string'&&m.trim()?m:'未提供';
 const amount=m.amount??(m.units!=null?Number(m.units)+Number(m.nanos||0)/1e9:null),currency=m.currency||m.currencyCode;
 if(amount==null||amount===''||!Number.isFinite(Number(amount)))return '未提供';
 if(!currency)return String(amount)+'（币种未记录）';
 try{return new Intl.NumberFormat('en-GB',{style:'currency',currency,maximumFractionDigits:9,minimumFractionDigits:0}).format(Number(amount));}catch{return String(currency)+' '+amount;}
}
function axServicePriceRange(p){
 if(typeof p==='string')return p;
 if(!p||(!p.startPrice&&!p.endPrice))return '未提供';
 const start=p.startPrice?axServiceMoney(p.startPrice):'',end=p.endPrice?axServiceMoney(p.endPrice):'';
 return start&&end?start+'–'+end:start?start+' 起':'最高 '+end;
}
const axServiceTypes={british_restaurant:'英式餐厅',clothing_store:'服装店',restaurant:'餐厅',cafe:'咖啡店',coffee_shop:'咖啡店',bar:'酒吧',hospital:'医院',pharmacy:'药店',hotel:'酒店',supermarket:'超市',shopping_mall:'购物中心',bakery:'烘焙店',store:'商店'};
function axServiceCard(r){
 const row=(label,value)=>`<dt data-ui-output>${label}</dt><dd data-ui-output>${value}</dd>`;
 const level={PRICE_LEVEL_FREE:'免费',PRICE_LEVEL_INEXPENSIVE:'较低',PRICE_LEVEL_MODERATE:'中等',PRICE_LEVEL_EXPENSIVE:'较高',PRICE_LEVEL_VERY_EXPENSIVE:'很高'};
 let prices='';
 if(r.price_per_person)prices+=row('人均消费',esc(axServiceMoney(r.price_per_person)));
 if(r.price_range&&(typeof r.price_range==='string'||Object.keys(r.price_range).length))prices+=row('价格范围',esc(axServicePriceRange(r.price_range)));
 if(r.price_level)prices+=row('价格等级',esc(level[r.price_level]||r.price_level));
 if(!prices)prices=row('消费信息','价格信息未提供');
 const options={reservable:'支持预约',dine_in:'堂食',takeout:'外带',delivery:'配送',curbside_pickup:'路边取餐'};
 const tags=Object.entries(options).filter(([key])=>typeof r[key]==='boolean').map(([key,label])=>`<span data-ui-output class="ax-service-tag ${r[key]?'':'ac-muted'}">${r[key]?label:'不支持'+label.replace('支持','')}</span>`).join('');
 const hours=esc(axHours(r.opening_hours)).split('；').join('<br>');
 const effects=axServiceStates(r,axState().result?.query_time||'');
 return `<article class="ac-record ax-service"><h4>${esc(r.name)}</h4><details class="ax-disclosure"><summary class="nav-btn"><span class="ax-open-label" data-ui-text>查看详情</span><span class="ax-close-label" data-ui-text>收起详情</span></summary><div class="ax-notice-body"><dl class="ax-service-facts">
 ${row('服务类型',esc(axServiceTypes[r.primary_type]||r.primary_type||'未提供'))}
 ${row('评分',r.rating!=null?esc(String(r.rating))+(r.user_rating_count!=null?' · '+esc(String(r.user_rating_count))+(window.GeoUI?.language==='en'?' reviews':' 条评价'):''):'未提供')}
 ${prices}${row('营业时间',hours)}
 ${r.open_status?row('Current operating status',esc(r.open_status)):''}${r.service_status?row('服务状态',esc(r.service_status)):r.regular_status?row('常规营业状态',esc(r.regular_status)):''}
 ${r.status_note?row('说明',esc(r.status_note)):''}
 ${tags?row('服务选项',`<div class="ax-service-tags">${tags}</div>`):''}
 </dl>${effects}</div></details></article>`;
}
function axServiceStates(r,time){
 if(!time)return '';
 if(r.review_impacts)return r.review_impacts.length?'<h4 class="ax-section-title" data-ui-text>事件影响</h4>'+r.review_impacts.map(x=>`<section><p>${esc(x.status)} · ${esc(x.title)}</p><p class="ac-note">${esc(axTimeRange(x.active_window))}</p><p>${esc(x.reason)}</p></section>`).join(''):'';
 const bound=t=>typeof t==='string'&&/(Z|[+-]\d{2}:\d{2})$/.test(t)&&Number.isFinite(Date.parse(t));
 const valid=(r.state_records||[]).filter(x=>bound(x.active_window?.start)&&bound(x.active_window?.end)&&Date.parse(x.active_window.end)>Date.parse(x.active_window.start)&&(!time||(Date.parse(x.active_window.start)<=Date.parse(time)&&Date.parse(time)<Date.parse(x.active_window.end))));
 if(!valid.length)return '';
 const labels={open:'营业',closed:'暂停营业',available:'可用',unavailable:'不可用'};
 return '<h4 class="ax-section-title" data-ui-text>额外影响</h4>'+valid.map(x=>`<section><p class="ac-note">${esc(axTimeRange(x.active_window))}</p>${axValue(Object.fromEntries(Object.entries(x.values||{}).map(([k,v])=>[k,labels[v]||v])))}</section>`).join('');
}
function axCard(r){if(r?.title)return `<article class="ac-record ax-notice"><h4>${esc(r.title)}</h4><p class="ac-note">${esc(axTimeRange(r.active_window))}</p><details class="ax-disclosure"><summary class="nav-btn"><span class="ax-open-label" data-ui-text>查看详情</span><span class="ax-close-label" data-ui-text>收起详情</span></summary><div class="ax-notice-body">${r.description?`<p>${esc(r.description)}</p>`:''}${axValue(Object.fromEntries(Object.entries(r).filter(([k])=>!['title','description','active_window'].includes(k))))}</div></details></article>`;return `<article class="ac-record">${axValue(r)}</article>`;}
function axTab(tab){axState().tab=tab;axState().offset=0;axState().result=null;axState().detail=null;axState().error='';axState().trace=null;axRender();}
function axRender(){
 if(currentView!=='app-content'||acOverview)return;
 axDisposeMap();
 const s=axState(),app=acSelectedApp,host=document.getElementById('ac-records');
 let fields='',tabs='',button='查询',note='';
 if(app==='MapApp'){
  tabs=`<div class="am-tabs"><button class="nav-btn ${s.tab==='search'?'active':''}" onclick="axTab('search')" data-ui-text>地点搜索</button><button class="nav-btn ${s.tab==='routes'?'active':''}" onclick="axTab('routes')" data-ui-text>路线查询</button></div>`;
  if(s.tab==='search'){fields=axField('query','地点名称','输入名称关键词')+axField('primary_type','类型（可选）','例如 hospital')+axField('limit','返回数量','','number');button='搜索';}
  else{fields=`<div class="ax-endpoints">${axPicker('origin','起点')}<button type="button" class="nav-btn ax-swap" data-ui-title title="交换起终点" onclick="axSwap()">⇅</button>${axPicker('destination','终点')}</div><label data-ui-text>交通方式<select id="ax-mode" onchange="axState().fields.mode=this.value">${[['ALL','全部'],['WALK','步行'],['DRIVE','驾车'],['TRANSIT','公共交通']].map(([value,label])=>`<option data-ui-output value="${value}" ${value===(s.fields.mode||'ALL').toUpperCase()?'selected':''}>${label}</option>`).join('')}</select></label><label data-ui-text>地图底图<select id="ax-basemap" onchange="axSetBasemap(this.value)">${[['osm','OSM'],['google','Google']].map(([value,label])=>`<option data-ui-output value="${value}" ${value===axBasemap(s)?'selected':''}>${label}</option>`).join('')}</select></label>`;button=s.busy?'正在查询…':'查询路线';}
 }else if(app==='LocalServiceApp'){
  s.tab='search';fields=axField('query','地点名称','筛选服务地点')+axField('time','查看时间（可选）','','datetime-local')+axField('timezone','时区（UTC 偏移）','例如 +08:00')+axField('limit','每页数量','','number');
 }else if(app==='WeatherApp'){
  s.tab='search';fields=axField('district_id','区域或地点','留空查看全部')+axField('time','查看时间（可选）','','datetime-local')+axField('timezone','时区（UTC 偏移）','例如 +08:00');note='不填时间时展示全部已有记录；填写后仅展示该时刻有效的记录。';
 }else if(app==='CityNewsApp'){fields=axField('district_id','区域或地点','留空查询全部')+axField('info_type','公告类型（可选）','例如 notice')+axField('time_range','时间（可选）','日期、时间点或 起始/结束');}
 else if(app==='CalendarApp'){fields=`<label>Query<select id="ax-calendar-tool" onchange="axState().fields.calendar_tool=this.value;axRender()">${[['get_availability','Availability'],['get_weekday','Weekday'],['get_festivals','Festivals'],['get_holidays','Holidays'],['check_conflicts','Schedule conflicts']].map(([v,t])=>`<option value="${v}" ${v===(s.fields.calendar_tool||'get_availability')?'selected':''}>${t}</option>`).join('')}</select></label>`+axField('character_id','Participant names','Name, or comma-separated names for conflicts')+axField('city_name','City','London')+axField('date','Date','','date')+axField('start_time','Start time','','time')+axField('end_time','End time','','time');}

 else if(app==='MessagingApp'){fields=`<label>Query<select id="ax-message-tool" onchange="axState().fields.message_tool=this.value;axRender()">${[['search_contacts','Contacts'],['get_contact','Contact details and messages'],['get_user_messages','User updates']].map(([v,t])=>`<option value="${v}" ${v===(s.fields.message_tool||'search_contacts')?'selected':''}>${t}</option>`).join('')}</select></label>`+axField('query','Contact name','Name');button='Query';}

 else{host.innerHTML='<div class="ac-record"><h3 data-ui-text>交互接口</h3><p class="ac-note" data-ui-text>方案提交和评测执行待迁移接入。当前运行时间见上方事件节点。</p></div>';return;}
 host.innerHTML=`<div class="am-header"><h3 data-ui-output>${esc(acNames[app])}</h3></div>${tabs}<form class="am-form" onsubmit="event.preventDefault();axSubmit()">${fields}<button class="nav-btn" data-ui-output ${s.busy||!acScope||acContextBusy?'disabled':''}>${button}</button></form>${note?`<p class="ac-note" data-ui-output>${note}</p>`:''}<p class="ac-note" role="status" data-ui-output>${esc(s.error||(s.busy?'正在查询…':''))}</p><div id="ax-results"></div><div id="ax-detail"></div><div id="ax-trace">${axTrace(s.trace)}</div>`;
 const results=document.getElementById('ax-results');
 if(s.result!==null){
  if(app==='MapApp'&&s.tab==='search'){results.innerHTML=`<p class="ac-note" data-ui-output>返回 ${(s.result.results||[]).length} 个地点</p>`+(s.result.results||[]).map((p,i)=>`<article class="ac-record"><h4>${esc(p.name)}</h4><p class="ac-note">${esc(p.address||'')} · ${esc(p.primary_type||'')}</p><button class="nav-btn" onclick="axDetail(${i})" data-ui-output aria-expanded="${s.detail?.index===i}">${s.detail?.index===i?'收起详情':'查看详情'}</button>${s.detail?.index===i?`<div class="ax-place-detail"><h4 data-ui-text>地点详情</h4>${axValue(s.detail.observation)}</div>`:''}</article>`).join('');}
  else if(app==='MapApp'){results.innerHTML=axRoutes(s);}
  else if(app==='LocalServiceApp'){results.innerHTML=(s.result.results||[]).map(axServiceCard).join('')||'<p class="ac-note" data-ui-text>没有匹配的服务。</p>';}
  else if(app==='WeatherApp'){results.innerHTML=(Array.isArray(s.result)?s.result:[...(s.result.alerts||[]),...(s.result.updates||[])]).map(axCard).join('')||'<p class="ac-note" data-ui-text>没有符合条件的天气记录。</p>';}
  else{const rows=Array.isArray(s.result)?s.result:[s.result];results.innerHTML=rows.length?rows.map(axCard).join(''):'<p class="ac-note" data-ui-text>本次查询没有返回条目。</p>';}
 }
 if(s.result&&Number.isFinite(s.result.total))results.insertAdjacentHTML('beforeend',axPagination(s));
 if(app==='MapApp'&&s.tab==='routes'&&s.result)axMountRouteMap(s);
 axRouteReady();
 if(s.busy)host.querySelectorAll('input,select,button').forEach(e=>e.disabled=true);
 const defaultQuery=(['MapApp','LocalServiceApp'].includes(app)&&s.tab==='search'&&!(s.fields.query||'').trim())||['WeatherApp','CityNewsApp'].includes(app);
 if(defaultQuery&&!acContextBusy&&acScope&&s.result===null&&!s.busy&&!s.error)axSubmit();
}
async function axRequest(app,tool,args){const world=acScope?.world_id;if(!world)throw new Error('世界尚未加载');const url=app==='MapApp'?'/api/app-content/map-query':'/api/app-content/query';const runtime=acEpisode?`&folder=${encodeURIComponent(acEpisode)}&node=${acNode}&event_mode=${acEventMode}`:'';const d=await dynamicJSON(`${url}?world=${encodeURIComponent(world)}&app=${app}&mode=${acMode}&tool=${tool}&args=${encodeURIComponent(JSON.stringify(args))}${runtime}`);if(d.world_id!==world)throw new Error('世界已变化，请重新打开 APP。');return d;}
async function axSuggest(key,value){
 const s=axState(),epoch=axEpoch,app=acSelectedApp,mode=acMode; s.fields[key]=value;delete s.picks[key];s.result=null;const seq=s.seq[key]=(s.seq[key]||0)+1;clearTimeout(axTimer[key]);
 document.getElementById('ax-results').innerHTML='';axRouteReady();
 const panel=document.getElementById('ax-options-'+key);if(panel)panel.textContent=value.trim()?'正在搜索地点…':'';if(!value.trim())return;
 axTimer[key]=setTimeout(async()=>{try{const d=await axRequest('MapApp','search_places',{query:value,limit:6});if(epoch!==axEpoch||s.seq[key]!==seq||acSelectedApp!==app||acMode!==mode)return;s.suggestions[key]=d.observation.results||[];s.trace=d;const el=document.getElementById('ax-options-'+key);if(el)el.innerHTML=s.suggestions[key].map((p,i)=>`<button type="button" onclick="axChoose('${key}',${i})"><b>${esc(p.name)}</b><small>${esc(p.address||'')}</small></button>`).join('')||'<p class="ac-note" data-ui-text>未找到地点</p>';document.getElementById('ax-trace').innerHTML=axTrace(d);}catch(e){if(epoch===axEpoch&&s.seq[key]===seq&&acSelectedApp===app&&acMode===mode){const el=document.getElementById('ax-options-'+key);if(el)el.textContent=e.message;}}},350);
}
function axChoose(key,i){const s=axState(),p=s.suggestions[key]?.[i];if(p){s.seq[key]++;s.picks[key]={name:p.name,location:p.location};s.fields[key]=p.name;s.result=null;s.error='';axRender();}}
function axSwap(){const s=axState();[s.picks.origin,s.picks.destination]=[s.picks.destination,s.picks.origin];[s.fields.origin,s.fields.destination]=[s.fields.destination,s.fields.origin];s.seq.origin=(s.seq.origin||0)+1;s.seq.destination=(s.seq.destination||0)+1;s.result=null;axRender();}
async function axExecute(app,tool,args,detailIndex=null){const s=axState(),epoch=axEpoch;s.busy=true;s.error='';axRender();try{const d=await axRequest(app,tool,args);if(epoch!==axEpoch)return;s.trace=d;if(d.observation?.error||['invalid_arguments','execution_error','unknown_tool','tool_error'].includes(d.observation?.status)){s.result=null;s.detail=null;throw new Error(d.observation.error||d.observation.status);}if(detailIndex!==null)s.detail={index:detailIndex,observation:d.observation};else{s.result=d.observation;s.detail=null;}}catch(e){s.error=e.message;}finally{s.busy=false;if(epoch===axEpoch)axRender();}}
function axSubmit(paging=false){const s=axState(),f=s.fields,app=acSelectedApp;if(!paging)s.offset=0;let tool,args;
 if(app==='MapApp'){if(s.tab==='search'){tool='search_places';args={query:f.query||'',primary_type:f.primary_type||'',limit:Number(f.limit),offset:s.offset||0};}else{if(!s.picks.origin||!s.picks.destination){s.error='请从起点和终点输入框的查询结果中选定地点。';axRender();return;}tool='query_routes';args={origin:s.picks.origin,destination:s.picks.destination,mode:(f.mode||'ALL').toUpperCase(),detail_level:'geometry'};s.routeSelection=0;}}
 else if(app==='LocalServiceApp'){if(s.tab==='search'){const zone=(f.timezone||'').trim();if(f.time&&(!/^[+-](?:0\d|1[0-4]):[0-5]\d$/.test(zone)||!Number.isFinite(Date.parse(f.time+zone)))){s.error='请填写有效查看时间和时区，例如 +08:00。';axRender();return;}tool='browse_services';args={query:f.query||'',limit:Number(f.limit),offset:s.offset||0,time:f.time?f.time+zone:''};}else{if(!s.picks.place){s.error='请先选定地点。';axRender();return;}tool=s.tab==='availability'?'check_availability':'get_service_detail';args={...s.picks.place,...(s.tab==='availability'?{date:f.date||''}:{})};}}
 else if(app==='WeatherApp'){const zone=(f.timezone||'').trim();if(f.time&&!/^[+-](?:0\d|1[0-4]):[0-5]\d$/.test(zone)){s.error='请为查看时间填写时区，例如 +08:00。';axRender();return;}tool=acEpisode?'get_alerts':'browse_weather';args=acEpisode?{district_id:f.district_id||''}:{district_id:f.district_id||'',time:f.time?f.time+zone:''};}
 else if(app==='CityNewsApp'){tool='get_info';args={district_id:f.district_id||'',info_type:f.info_type||'',time_range:f.time_range||''};}else if(app==='CalendarApp'){
 tool=f.calendar_tool||'get_availability';args=tool==='get_availability'?{character_id:f.character_id||'',date:f.date||''}:tool==='get_weekday'?{date:f.date||''}:tool==='check_conflicts'?{character_ids:(f.character_id||'').split(',').map(x=>x.trim()).filter(Boolean),start_time:f.start_time||'',end_time:f.end_time||''}:{city_name:f.city_name||'',date:f.date||''};
 }else if(app==='MessagingApp'){tool=f.message_tool||'search_contacts';args=tool==='get_user_messages'?{}:tool==='get_contact'?{contact_name:f.query||''}:{query:f.query||''};}else return;
 if(['CalendarApp','MessagingApp'].includes(app)&&!acEpisode){s.error='Select a task runtime first.';axRender();return;}
 axExecute(app,tool,args);
}
function axDetail(i){const s=axState();if(s.busy)return;if(s.detail?.index===i){s.detail=null;axRender();return;}const p=s.result?.results?.[i];if(p)axExecute('MapApp','get_place_detail',{name:p.name,location:p.location},i);}

function axPagination(s){const offset=s.result.offset||0,count=(s.result.results||[]).length,total=s.result.total;return `<div class="am-actions ax-pagination"><span class="ac-note" data-ui-output>共 ${total} 条 · ${total?offset+1:0}–${offset+count}</span><button class="nav-btn" ${offset===0?'disabled':''} onclick="axPage(-1)" data-ui-text>上一页</button><button class="nav-btn" ${offset+count>=total?'disabled':''} onclick="axPage(1)" data-ui-text>下一页</button></div>`;}
function axPage(direction){const s=axState();s.offset=Math.max(0,(s.result?.offset||0)+direction*Math.min(50,Math.max(1,Number(s.fields.limit)||10)));axSubmit(true);}
function axDecodePolyline(encoded){
 let i=0,lat=0,lng=0;const points=[];
 const read=()=>{let result=0,shift=0,b;do{if(i>=encoded.length||shift>30)throw Error('Invalid cached polyline');b=encoded.charCodeAt(i++)-63;if(b<0||b>63)throw Error('Invalid cached polyline');result|=(b&31)<<shift;shift+=5;}while(b>=32);return result&1?~(result>>1):result>>1;};
 while(i<encoded.length){lat+=read();lng+=read();if(Math.abs(lat)>9000000||Math.abs(lng)>18000000)throw Error('Invalid route coordinates');points.push([lat/1e5,lng/1e5]);}return points;
}
function axRouteStatus(route){
 if(route.available===false)return /UNAVAILABLE|NO_ROUTE|ZERO_RESULTS/i.test(route.summary||'')?'unsupported':'data_unavailable';
 return route.available===true||(Number(route.distance_m)>0&&Number(route.duration_s)>0)?'available':'data_unavailable';
}
function axRouteSegments(route){
 if(route.encoded_polyline){try{return [axDecodePolyline(route.encoded_polyline)].filter(p=>p.length>1);}catch{}}
 const points=(route.polyline_points||[]).map(p=>[p.lat,p.lng]);
 return points.length>1&&points.every(p=>Number.isFinite(p[0])&&Number.isFinite(p[1]))?[points]:[];
}
function axSelectRoute(index){const route=axState().result?.routes?.[index];if(!route||axRouteStatus(route)!=='available')return;axState().routeSelection=index;axRender();}
function axRoutes(s){
 const rows=(s.result.routes||[]).map(r=>({...r,status:axRouteStatus(r)}));
 const selected=rows[s.routeSelection||0]?.status==='available'?(s.routeSelection||0):rows.findIndex(r=>r.status==='available'),colors=['#67b7ef','#d8a66b','#b49add','#74cbb4'];s.routeSelection=selected;
 const names={WALK:'步行',DRIVE:'驾车',TRANSIT:'公共交通'};
 const segments=rows.map(r=>r.status==='available'?axRouteSegments(r):[]);
 const args=s.trace?.arguments||{},ends=[args.origin,args.destination];
 const endpoints=ends.map(p=>p?.location).filter(p=>p&&Number.isFinite(p.lat)&&Number.isFinite(p.lng)).map(p=>[p.lat,p.lng]);
 const all=segments.flat(2).concat(endpoints);let diagram='';
 if(all.length){
  const lat0=all.reduce((sum,p)=>sum+p[0],0)/all.length,cos=Math.cos(lat0*Math.PI/180);
  const xs=all.map(p=>p[1]*cos),ys=all.map(p=>p[0]);const minX=Math.min(...xs),maxX=Math.max(...xs),minY=Math.min(...ys),maxY=Math.max(...ys);
  const scale=Math.min(720/Math.max(maxX-minX,1e-6),310/Math.max(maxY-minY,1e-6));
  const project=p=>[400+(p[1]*cos-(minX+maxX)/2)*scale,190-(p[0]-(minY+maxY)/2)*scale];
  const order=rows.map((_,i)=>i).filter(i=>i!==selected).concat(selected>=0?[selected]:[]);
  diagram=`<svg class="ax-route-diagram" viewBox="0 0 800 380" role="img" data-ui-aria-label aria-label="缓存路线轨迹图"><rect width="800" height="380" fill="var(--bg)"/>${order.map(i=>segments[i].map(seg=>`<polyline points="${seg.map(p=>project(p).map(v=>v.toFixed(2)).join(',')).join(' ')}" fill="none" stroke="${colors[i%colors.length]}" stroke-width="${i===selected?4:2}" opacity="${i===selected?1:0.25}"/>`).join('')).join('')}${endpoints.map((p,i)=>{const [x,y]=project(p);return `<circle cx="${x}" cy="${y}" r="7" fill="${i?'#d8a66b':'#67b7ef'}"/><text x="${x+11}" y="${y-11}" fill="var(--text)" font-size="16" data-ui-output>${i?'终点':'起点'}</text>`;}).join('')}<text x="760" y="28" fill="var(--text2)" font-size="14" data-ui-text>北 ↑</text></svg>`;
 }
 const labels={unsupported:'无支持路线',not_cached:'暂无路线缓存',data_unavailable:'路线数据暂不可用'};
 const requested=args.mode&&args.mode!=='ALL'?[args.mode]:['WALK','TRANSIT','DRIVE'];
 const cards=rows.concat(requested.filter(mode=>!rows.some(r=>r.mode===mode)).map(mode=>({mode,status:'not_cached'})));
 s.routeDrawing={segments,endpoints,selected,colors};
 return `<div class="ax-route-heading"><strong>${esc(args.origin?.name||'起点')} → ${esc(args.destination?.name||'终点')}</strong></div><div class="ax-route-layout"><div><div id="ax-route-map"></div><div id="ax-route-fallback">${diagram}</div><p data-ui-output id="ax-basemap-status" class="ac-note"></p>${selected>=0&&!segments[selected]?.length?'<p class="ac-note" data-ui-text>暂无轨迹</p>':''}${!rows.length?'<p class="ac-note" data-ui-text>当前map没有返回缓存路线。</p>':''}</div><div class="ax-route-cards">${cards.map((r,i)=>`<button class="ac-record ax-route-card ${i===selected?'active':''}" ${r.status!=='available'?'disabled':''} onclick="axSelectRoute(${i})" aria-pressed="${i===selected}"><strong data-ui-output>${esc(names[r.mode]||r.mode)}</strong><span data-ui-output>${r.status==='available'?`${r.distance_m>=1000?(r.distance_m/1000).toFixed(2)+' 公里':r.distance_m+' 米'} · 约 ${Math.max(1,Math.round(r.duration_s/60))} 分钟`:labels[r.status]||labels.data_unavailable}</span>${r.status==='available'&&!segments[i]?.length?'<small data-ui-text>暂无轨迹</small>':''}</button>`).join('')}</div></div>`;
}

function axDisposeMap(){if(axLeaflet){axLeaflet.remove();axLeaflet=null;}}
function axBasemap(s){return s.basemap||(s.trace?.viewer_basemap==='configured'&&viewerConfig.preferred_basemap==='google'?'google':'osm');}
function axSetBasemap(value){if(!['google','osm'].includes(value))return;axState().basemap=value;axRender();}
async function axMountRouteMap(s){
 const host=document.getElementById('ax-route-map'),fallback=document.getElementById('ax-route-fallback'),status=document.getElementById('ax-basemap-status');
 if(!host||!s.routeDrawing)return;
 const fail=()=>{if(status.isConnected)status.textContent='底图加载失败，仍可查看缓存轨迹';};
 if(!window.L){host.hidden=true;fail();return;}
 const {segments,endpoints,selected,colors}=s.routeDrawing;
 const map=L.map(host,{scrollWheelZoom:true});axLeaflet=map;fallback.hidden=true;
 const points=segments.flat(2).concat(endpoints);if(points.length)map.fitBounds(points,{padding:[30,30],maxZoom:18});else map.setView([0,0],2);
 segments.forEach((list,i)=>list.forEach(seg=>L.polyline(seg,{color:colors[i%colors.length],weight:i===selected?4:2,opacity:i===selected?1:0.25}).addTo(map)));
 endpoints.forEach((point,i)=>L.circleMarker(point,{radius:7,color:i?'#d8a66b':'#67b7ef',fillOpacity:1}).addTo(map).bindTooltip(GeoUI.t(i?'终点':'起点'),{permanent:true,direction:'top'}));
 try{let layer;if(axBasemap(s)==='google'){if(!viewerConfig.google_maps_api_key)throw Error('missing key');await ensureGoogleBasemap();layer=L.gridLayer.googleMutant({type:'roadmap',maxZoom:21});}else{layer=L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,attribution:'&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'});}if(axLeaflet!==map||!host.isConnected)return;layer.on('tileerror',fail);layer.addTo(map);}catch{fail();}
}

Object.assign(axLabels,{available_from:'Available from',must_leave_by:'Must leave by',windows:'Availability windows',dietary:'Dietary preference',budget:'Budget',recent_messages:'Recent messages',content:'Message',sender_name:'Sender',time:'Time',contacts:'Contacts',messages:'Messages',phone:'Phone',has_car:'Has a car',current_context:'Recorded context'});

Object.assign(axLabels,{is_free:'Free',from:'From',is_new:'New message'});
