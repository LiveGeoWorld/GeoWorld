/* Task selection supplies the existing formal episode; all Apps share its checkpoint. */
let acCatalog=[],acEpisode='',acNodes=[],acNode=0,acEventMode='shared_world',acContextBusy=false,acContextRequest=0;
function acEpisodeOptions(){return acCatalog.filter(x=>x.world===acScope?.world_id&&x.mode===acMode);}
function acRenderContext(){
 const host=document.getElementById('ac-context');if(!host)return;
 const rows=acEpisodeOptions();
 host.innerHTML=`<label>Task runtime<select id="ac-episode" onchange="acChooseEpisode(this.value)"><option value="">Select a task runtime</option>${rows.map(r=>`<option value="${escAttr(r.folder)}" ${r.folder===acEpisode?'selected':''}>${esc(r.label||r.task_id)}</option>`).join('')}</select></label><label>Event pool<select id="ac-event-mode" onchange="acEventMode=this.value;acChooseEpisode(acEpisode)"><option value="shared_world" ${acEventMode==='shared_world'?'selected':''}>Shared world</option><option value="task_only" ${acEventMode==='task_only'?'selected':''}>Task only</option></select></label><label>Event checkpoint<select id="ac-node" onchange="acSetNode(this.value)">${acNodes.length?acNodes.map(n=>`<option value="${n.index}" ${n.index===acNode?'selected':''}>${esc(n.label)}${n.time?' · '+esc(n.time):''}</option>`).join(''):'<option value="0">Initial state</option>'}</select></label><p class="ac-note">${acContextBusy?'Loading the recorded runtime…':rows.length?'Queries use this task’s frozen assets and the selected virtual time.':'No task runtime records are loaded for this world.'}</p>`;
}
async function acLoadContexts(){
 const scope=acScope?.world_id;if(!scope)return;
 try{const data=await dynamicJSON('/api/app-content/catalog');if(acScope?.world_id!==scope)return;acCatalog=data.tasks||[];acAutoContext();}
 catch(e){document.getElementById('ac-context').textContent=e.message;}
}
function acAutoContext(){
 const rows=acEpisodeOptions();const current=rows.find(r=>r.folder===currentFolder);
 acChooseEpisode(current?.folder||(rows.length===1?rows[0].folder:''));
}
async function acChooseEpisode(folder){
 const request=++acContextRequest;acEpisode=folder;acNode=0;acNodes=[];amReset();acContextBusy=!!folder;acRender();
 if(!folder)return;
 try{const data=await dynamicJSON(`/api/app-content/preview?folder=${encodeURIComponent(folder)}&node=0&event_mode=${acEventMode}`);if(request!==acContextRequest)return;acNodes=data.nodes||[];}
 catch(e){if(request===acContextRequest){acNodes=[];axState().error=e.message;}}
 finally{if(request===acContextRequest){acContextBusy=false;acRender();}}
}

function acSetNode(value){
 const old=ax;acNode=Number(value);amReset();
 for(const [key,state] of Object.entries(old))ax[key]={...state,fields:{...state.fields},picks:{...state.picks},result:null,trace:null,error:'',busy:false,detail:null};
 acRender();
 if(!acOverview&&acSelectedApp!=='UserInterfaceApp'&&!axState().busy)axSubmit();
}
