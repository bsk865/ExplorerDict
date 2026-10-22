study.planView='all';study.weekOffset=0;
function studyLocalDate(date){return `${date.getFullYear()}-${String(date.getMonth()+1).padStart(2,'0')}-${String(date.getDate()).padStart(2,'0')}`;}
function studyWeek(){const start=new Date();start.setHours(0,0,0,0);start.setDate(start.getDate()-(start.getDay()+6)%7+study.weekOffset*7);return Array.from({length:7},(_,i)=>{const d=new Date(start);d.setDate(d.getDate()+i);return studyLocalDate(d);});}
const managementPage=studyPage;
studyPage=async function(page='themes'){
  if(await managementPage(page)===false)return false;
  if(page==='plans'){
    $('#main .page-heading').insertAdjacentHTML('afterend',`<div class="study-plan-toolbar"><div role="group" aria-label="计划视图">${button('study-view-all','全部',study.planView==='all'?'active':'')}${button('study-view-week','本周',study.planView==='week'?'active':'')}</div>${button('study-plan-history','小探规划记录')}</div>`);
    if(study.planView==='week'){
      const days=studyWeek();
      $('.study-task-list').outerHTML=`<div class="study-week-heading"><button data-action="study-week-prev" aria-label="上一周">${studyIcon('arrow-right')}</button><strong>${days[0]} 至 ${days[6]}</strong><button data-action="study-week-next" aria-label="下一周">${studyIcon('arrow-right')}</button></div><div class="study-week">${days.map(date=>`<section><h3>${date.slice(5)}<small>${['周一','周二','周三','周四','周五','周六','周日'][days.indexOf(date)]}</small></h3>${study.data.tasks.filter(t=>t.date===date).sort((a,b)=>a.time.localeCompare(b.time)).map(t=>`<button class="study-week-task ${t.status==='done'?'is-done':''}" data-study-task="${t.id}"><small>${esc(t.time||'时间待定')} · ${t.minutes} 分钟</small><strong>${esc(t.title)}</strong><span>${t.status==='done'?'已完成':t.status==='doing'?'进行中':'待完成'}</span></button>`).join('')||'<p class="muted">暂无安排</p>'}</section>`).join('')}</div>`;
    }
  }
};
const managementTaskForm=studyTaskForm;
studyTaskForm=function(task){managementTaskForm(task);if(!task)return;
  $('#study-task-form .actions').insertAdjacentHTML('afterbegin',`<button type="button" id="study-task-delete" aria-label="删除计划" title="删除计划">${studyIcon('trash')}</button><button type="button" id="study-task-delay">延后一天</button>${task.status!=='done'&&task.minutes>=10?'<button type="button" id="study-task-split">拆分</button>':''}`);
  $('#study-task-delete').onclick=()=>studyDeleteConfirm('tasks',task.id,'删除计划','这项计划将被删除，关联文章和笔记保留。');
  $('#study-task-delay').onclick=()=>{const input=$('#study-task-form [name="date"]');const d=new Date((input.value||studyDate())+'T12:00:00');d.setDate(d.getDate()+1);input.value=studyLocalDate(d);};
  if($('#study-task-split'))$('#study-task-split').onclick=()=>{modal('拆分计划',`<form id="study-split-form"><p>${esc(task.title)} · 共 ${task.minutes} 分钟</p><label class="form-field">第一部分用时<input name="minutes" type="number" min="5" max="${task.minutes-5}" value="${Math.floor(task.minutes/2)}" required></label><div class="actions">${button('close','取消')}<button class="primary" type="submit">拆分为两项</button></div></form>`);$('#study-split-form').onsubmit=async e=>{e.preventDefault();try{await api('study/split-task',{id:task.id,minutes:Number(new FormData(e.target).get('minutes'))});$('#dialog').close();await studyPage('plans');}catch(err){toast(err.message);}};};
};
function studyDeleteConfirm(kind,id,title,description){modal(title,`<p>${description}</p><div class="actions">${button('close','取消')}<button id="study-confirm-delete" class="primary">删除</button></div>`);$('#study-confirm-delete').onclick=async()=>{try{await api('study/delete',{kind,id});$('#dialog').close();if(kind==='themes')study.theme=null;await studyPage(kind==='themes'?'themes':'plans');}catch(err){toast(err.message);}};}
const managementThemeForm=studyThemeForm;
studyThemeForm=function(theme){managementThemeForm(theme);if(theme){$('#study-theme-form .actions').insertAdjacentHTML('afterbegin',`<button type="button" id="study-theme-delete">删除主题</button>`);$('#study-theme-delete').onclick=()=>studyDeleteConfirm('themes',theme.id,'删除主题','只删除主题分类，其中的文章和笔记会保留。');}};
try{const draft=JSON.parse(localStorage.getItem('study-advisor-draft')||'null');if(draft){studyAdvisorRequest=draft.request||'';studyAdvisorArticles=draft.article_ids||[];studyAdvisorAvailability=Array.isArray(draft.history)?draft.availability||null:null;studyAdvisorHistory=(draft.history||[]).filter(t=>['user','assistant'].includes(t.role)&&typeof t.content==='string').slice(-16);}}catch{}
const plansManagementDispatch=dispatch;
dispatch=async function(action){
  if(action==='study-view-all'||action==='study-view-week'){study.planView=action.endsWith('week')?'week':'all';study.weekOffset=0;return studyPage('plans');}
  if(action==='study-week-prev'||action==='study-week-next'){study.weekOffset+=action.endsWith('next')?1:-1;return studyPage('plans');}
  if(action==='study-plan-history'){
    const data=await api('study');modal('小探规划记录',`<div class="study-plan-preview">${data.batches.map(b=>`<div><small>${esc(new Date(b.created_at).toLocaleString())} · ${b.tasks.length} 项</small><p>${esc(b.request)}</p>${b.undone?'<span>已撤销</span>':`<button data-study-undo-batch="${b.id}">撤销本次添加</button>`}</div>`).join('')||'<p>还没有规划记录。</p>'}</div>`);document.querySelectorAll('[data-study-undo-batch]').forEach(el=>el.onclick=async()=>{try{await api('study/undo-plan',{id:el.dataset.studyUndoBatch});$('#dialog').close();await studyPage('plans');}catch(err){toast(err.message);}});return;
  }
  return plansManagementDispatch(action);
};
