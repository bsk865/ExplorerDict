/* Progress is calculated from the current saved tasks, never a synthetic goal. */
let taskProgressScope='week';
function taskProgressTasks(scope){
  const today=studyDate(),start=new Date(today+'T12:00:00');
  start.setDate(start.getDate()-(start.getDay()+6)%7);
  const end=new Date(start);end.setDate(end.getDate()+6);
  const first=studyLocalDate(start),last=studyLocalDate(end);
  return study.data.tasks.filter(t=>scope==='all'||(scope==='today'?t.date===today:t.date>=first&&t.date<=last));
}
function taskProgressHTML(scope,compact=false){
  const tasks=taskProgressTasks(scope),done=tasks.filter(t=>t.status==='done').length;
  const percent=tasks.length?Math.round(done/tasks.length*100):0;
  const message=!tasks.length?'留一点时间，开始一件想做的事。':done===tasks.length?'这一程已完成，给自己的坚持一个肯定。':done?`已经完成 ${done} 项，继续按自己的节奏前进。`:'从第一项开始，每一步都算数。';
  return `<div class="task-progress-copy"><strong>${compact?'今日进度':'任务完成进度'}</strong><span>${tasks.length?`${done} / ${tasks.length} 项`:'暂无安排'}</span></div><progress max="${Math.max(1,tasks.length)}" value="${done}" aria-label="${scope==='today'?'今日':scope==='week'?'本周':'全部'}任务完成进度"></progress>${compact?'':`<div class="task-progress-foot"><p>${message}</p><strong>${tasks.length?percent+'%':'—'}</strong></div>`}`;
}
function renderTaskProgress(){
  const panel=$('#task-completion');if(!panel)return;
  panel.innerHTML=`<div class="task-progress-heading"><h2>每一步，都有回响</h2><div class="task-progress-scopes" role="group" aria-label="进度范围">${[['today','今日'],['week','本周'],['all','全部']].map(([id,label])=>`<button data-progress-scope="${id}" aria-pressed="${id===taskProgressScope}">${label}</button>`).join('')}</div></div>${taskProgressHTML(taskProgressScope)}`;
}
const experienceStudyPage=studyPage;
studyPage=async function(page='themes'){
  if(await experienceStudyPage(page)===false)return false;
  if(state.page!=='study'||study.page!==page)return;
  $('#main').classList.toggle('today-view',page==='today');
  if(page==='stats'){
    $('.study-metrics').insertAdjacentHTML('afterend','<section id="task-completion" class="task-completion"></section>');
    renderTaskProgress();
  }
  if(page==='today'){
    const heading=$('#main>.page-heading');
    const banner=document.createElement('section');banner.className='today-banner';
    heading.before(banner);
    banner.innerHTML=`<img class="today-scene" data-theme-image="scene" src="${window.ExplorerThemes[theme()].scene}" alt="">`;
    banner.append(heading);
    heading.querySelector('h1').insertAdjacentHTML('afterend','<p class="today-greeting">给今天，留一点探索的时间。</p>');
    const list=$('.study-task-list');
    list.insertAdjacentHTML('beforebegin',`<section class="today-task-heading"><h2>今日安排</h2><div class="today-task-progress">${taskProgressHTML('today',true)}</div></section>`);
    if(!taskProgressTasks('today').length)list.innerHTML=`<div class="today-empty"><img src="${avatar()}" alt=""><div><strong>从一件小事开始</strong><p>读一段资料，记一个想法，或安排下一步。</p></div><button data-action="study-new-task">${studyIcon('plus')}添加计划</button></div>`;
  }
};
document.addEventListener('click',e=>{
  const control=e.target.closest('[data-progress-scope]');
  if(control){taskProgressScope=control.dataset.progressScope;renderTaskProgress();}
});
document.addEventListener('study-tasks-updated',()=>{
  const today=$('.today-task-progress');if(today)today.innerHTML=taskProgressHTML('today',true);
  renderTaskProgress();
});
