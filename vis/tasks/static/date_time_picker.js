/* UI-only local date/time editor. No event reads or time-zone conversion. */
function axDateTimeField(key,label,value){
 return `<div class="ax-datetime"><label for="ax-${key}" data-ui-output>${label}</label><div class="ax-datetime-input"><input id="ax-${key}" type="text" value="${escAttr((value||'').replace('T',' '))}" placeholder="YYYY-MM-DD HH:mm" autocomplete="off" oninput="axDateTimeInput(this,'${key}')"><button type="button" class="nav-btn" data-ui-title title="选择日期时间" onclick="axOpenDateTime('${key}')">▦</button></div></div>`;
}
function axDateTimeValid(value){
 if(!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(value))return false;
 const [y,m,d,h,n]=value.match(/\d+/g).map(Number),end=new Date(0);end.setUTCFullYear(y,m,0);
 return y>=1&&m>=1&&m<=12&&d>=1&&d<=end.getUTCDate()&&h<24&&n<60;
}
function axDateTimeInput(input,key){
 const value=input.value.trim().replace(' ','T');
 input.setCustomValidity(!value||axDateTimeValid(value)?'':(window.GeoUI?.language==='en'?'Use YYYY-MM-DD HH:mm with a valid date and time.':'请输入有效日期时间，格式 YYYY-MM-DD HH:mm。'));
 axFieldInput(key,value);
}
function axOpenDateTime(key){
 document.querySelector('#ax-datetime-dialog')?.remove();
 const input=document.getElementById('ax-'+key),old=input.value.trim().replace(' ','T');
 const now=new Date(),parts=axDateTimeValid(old)?old.match(/\d+/g).map(Number):[now.getFullYear(),now.getMonth()+1,now.getDate(),now.getHours(),now.getMinutes()];
 const dialog=document.createElement('dialog');dialog.id='ax-datetime-dialog';dialog.className='ax-datetime-dialog';
 dialog.innerHTML=`<form method="dialog"><h3 data-ui-text>选择日期时间</h3><div class="ax-datetime-parts"><label data-ui-text>年<input id="ax-dt-year" type="number" min="1" max="9999" required value="${parts[0]}"></label><label data-ui-text>月<select id="ax-dt-month">${Array.from({length:12},(_,i)=>`<option value="${i+1}" ${i+1===parts[1]?'selected':''}>${i+1}</option>`).join('')}</select></label></div><div class="ax-datetime-days" role="group" data-ui-aria-label aria-label="日期"></div><div class="ax-datetime-parts"><label data-ui-text>小时<input id="ax-dt-hour" type="number" min="0" max="23" required value="${parts[3]}"></label><label data-ui-text>分钟<input id="ax-dt-minute" type="number" min="0" max="59" required value="${parts[4]}"></label></div><div class="ax-datetime-actions"><button type="button" class="nav-btn" id="ax-dt-clear" data-ui-text>清空</button><button type="button" class="nav-btn" id="ax-dt-cancel" data-ui-text>取消</button><button type="submit" class="nav-btn" data-ui-text>确定</button></div></form>`;
 document.body.append(dialog);let day=parts[2];
 const read=id=>Number(dialog.querySelector('#ax-dt-'+id).value);
 function renderDays(){
  const year=read('year'),month=read('month');if(year<1||year>9999)return;
  const date=new Date(0);date.setUTCFullYear(year,month-1,1);date.setUTCHours(0,0,0,0);
  const offset=(date.getUTCDay()+6)%7;date.setUTCMonth(month,0);const count=date.getUTCDate();day=Math.min(day,count);
  const grid=dialog.querySelector('.ax-datetime-days');
  grid.innerHTML=['一','二','三','四','五','六','日'].map(x=>`<span data-ui-text>周${x}</span>`).join('')+'<span></span>'.repeat(offset)+Array.from({length:count},(_,i)=>`<button type="button" class="nav-btn" aria-pressed="${i+1===day}" data-day="${i+1}">${i+1}</button>`).join('');
  grid.querySelectorAll('button').forEach(button=>button.onclick=()=>{day=Number(button.dataset.day);grid.querySelectorAll('button').forEach(b=>b.setAttribute('aria-pressed',String(b===button)));});
  window.GeoUI?.scan(dialog);
 }
 dialog.querySelector('#ax-dt-year').oninput=renderDays;dialog.querySelector('#ax-dt-month').onchange=renderDays;
 function close(){dialog.close();dialog.remove();input.focus();}
 dialog.querySelector('#ax-dt-cancel').onclick=close;
 dialog.addEventListener('cancel',event=>{event.preventDefault();close();});
 function commit(value){input.value=value.replace('T',' ');axDateTimeInput(input,key);close();}
 dialog.querySelector('#ax-dt-clear').onclick=()=>commit('');
 dialog.querySelector('form').onsubmit=event=>{event.preventDefault();const pad=n=>String(n).padStart(2,'0');const value=String(read('year')).padStart(4,'0')+'-'+pad(read('month'))+'-'+pad(day)+'T'+pad(read('hour'))+':'+pad(read('minute'));if(axDateTimeValid(value))commit(value);};
 renderDays();dialog.showModal();
}
