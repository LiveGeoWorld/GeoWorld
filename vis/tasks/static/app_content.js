let acMode='static',acSelectedApp='MapApp',acScope=null,acRequest=0,acOverview=false;
const acNames={MapApp:'地图',LocalServiceApp:'本地服务',WeatherApp:'天气',CityNewsApp:'城市公告',CalendarApp:'日历',MessagingApp:'消息',UserInterfaceApp:'交互接口'};
async function showAppContent(){
 amReset();acScope=null;const request=++acRequest;currentView='app-content';dynamicSourceRequest++;
 document.getElementById('task-list').style.display='none';
 for(const id of ['nav-tasks','nav-runs'])document.getElementById(id).classList.remove('active');
 document.getElementById('nav-app-content').classList.add('active');
 const host=document.getElementById('task-detail');host.classList.remove('region-review-content');delete host.dataset.serviceDetailView;
 host.innerHTML=`<section class="ac-page"><header><h2>APP</h2></header><div class="ac-toolbar"><p id="ac-world" class="ac-note" data-ui-text>正在读取当前世界…</p><div class="ac-modes" role="group" aria-label="事件数据模式" data-ui-aria-label><button id="ac-static" class="nav-btn" onclick="acSetMode('static')" data-ui-text>静态</button><button id="ac-dynamic" class="nav-btn" onclick="acSetMode('dynamic')" data-ui-text>动态</button></div></div><div id="ac-context" class="am-form"></div><div class="ac-layout"><nav id="ac-apps" aria-label="APP 列表" data-ui-aria-label></nav><section id="ac-records" aria-live="polite"></section></div></section>`;
 acRender();
 try{const scope=await dynamicJSON('/world-api/map-scope');if(request!==acRequest||currentView!=='app-content')return;if(!scope.world_id)throw new Error('请在世界构建中选择世界。');acScope=scope;acLoadContexts();document.getElementById('ac-world').textContent=scope.world_id;acRender();}
 catch(e){if(request===acRequest&&currentView==='app-content')document.getElementById('ac-world').textContent=`世界读取失败：${e.message}`;}
}
function acSetMode(mode){if(!['static','dynamic'].includes(mode))return;acMode=mode;acAutoContext();}
function acSelectApp(name){if(!Object.hasOwn(acNames,name))return;acSelectedApp=name;acRender();}
function acRender(){
 if(currentView!=='app-content')return;
 acRenderContext();
 for(const mode of ['static','dynamic']){const b=document.getElementById('ac-'+mode);b.classList.toggle('active',mode===acMode);b.setAttribute('aria-pressed',String(mode===acMode));}
 document.getElementById('ac-apps').innerHTML=Object.entries(acNames).map(([name,label])=>`<button class="ac-app ${name===acSelectedApp?'active':''}" aria-pressed="${name===acSelectedApp}" onclick="acSelectApp('${name}')"><b data-ui-output>${label}</b><small>${name}</small></button>`).join('');
 document.querySelector('.ac-layout').classList.toggle('ae-expanded',acOverview);
 document.getElementById('ac-apps').hidden=acOverview;
 let overviewButton=document.getElementById('ac-overview');
 if(!overviewButton){overviewButton=document.createElement('button');overviewButton.id='ac-overview';overviewButton.className='nav-btn';overviewButton.textContent='事件总览';overviewButton.setAttribute('data-ui-output','');overviewButton.onclick=()=>{acOverview=!acOverview;acRender();};document.querySelector('.ac-modes').prepend(overviewButton);}
 overviewButton.classList.toggle('active',acOverview);overviewButton.setAttribute('aria-pressed',String(acOverview));
 if(acOverview)aeRender();else{aeDispose();axRender();}
}
