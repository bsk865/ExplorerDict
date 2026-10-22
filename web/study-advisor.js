let studyAdvisorRequest='';
let studyAdvisorArticles=[];
let studyAdvisorAvailability=null;
let studyAdvisorHistory=[];
function saveAdvisorDraft(){
  try{localStorage.setItem('study-advisor-draft',JSON.stringify({request:studyAdvisorRequest,article_ids:studyAdvisorArticles,availability:studyAdvisorAvailability,history:studyAdvisorHistory}));}catch{}
}
function studyAdvisorForm(){
  const a=studyAdvisorAvailability||{};
  modal('和小探安排计划',`<form id="study-advisor-form">
    <div class="study-advisor-dialogue"><div class="study-advisor-intro"><img src="${avatar()}" alt=""><p>想完成什么？把今天的安排告诉我。</p></div>${studyAdvisorHistory.map(t=>`<div class="study-advisor-turn ${t.role}"><small>${t.role==='user'?'你':'小探'}</small><p>${esc(t.content)}</p></div>`).join('')}</div>
    <label class="form-field" for="study-advisor-request">${studyAdvisorHistory.length?'补充一句':'你的安排'}</label><textarea id="study-advisor-request" required maxlength="6000" placeholder="今天上午开会，下午想读完一篇论文。">${esc(studyAdvisorRequest)}</textarea>
    <details class="study-advisor-options"><summary>${studyIcon('settings')}偏好与相关资料<span>可选</span></summary>
      <label class="study-option-toggle"><input id="study-limit-toggle" type="checkbox" ${studyAdvisorAvailability?'checked':''}>限定每天的可用时间</label>
      <fieldset class="study-availability" ${studyAdvisorAvailability?'':'hidden disabled'}><div class="study-form-row"><label class="form-field">开始日期<input type="date" name="start_date" required value="${esc(a.start_date||studyDate())}"></label><label class="form-field">截止日期<input type="date" name="end_date" required value="${esc(a.end_date||studyDate())}"></label></div><div class="study-form-row"><label class="form-field">开始时间<input type="time" name="start_time" required value="${esc(a.start_time||'14:00')}"></label><label class="form-field">结束时间<input type="time" name="end_time" required value="${esc(a.end_time||'18:00')}"></label><label class="form-field">每天最多分钟<input type="number" name="daily_minutes" min="5" max="1440" required value="${Number(a.daily_minutes)||60}"></label></div></fieldset>
      <fieldset class="library-picker"><legend>相关资料</legend>${state.articles.map(a=>`<label><input type="checkbox" name="article" value="${a.id}" ${studyAdvisorArticles.includes(a.id)?'checked':''}><span>${esc(a.title)}</span></label>`).join('')||'<p class="muted">暂无资料</p>'}</fieldset>
    </details><p class="modal-note">小探会参考资料标题和已有日程。安排保存在本机，可修改或撤销。</p>
    <div class="actions"><button type="button" id="study-advisor-reset" ${!studyAdvisorHistory.length?'hidden':''}>重新开始</button><button class="primary" type="submit">${studyIcon('send')} ${studyAdvisorHistory.length?'继续规划':'交给小探'}</button></div></form>`);
  const form=$('#study-advisor-form'),dialogue=form.querySelector('.study-advisor-dialogue');dialogue.scrollTop=dialogue.scrollHeight;
  const persist=()=>{const f=new FormData(form);studyAdvisorRequest=$('#study-advisor-request').value;studyAdvisorArticles=f.getAll('article');studyAdvisorAvailability=$('#study-limit-toggle').checked?{start_date:f.get('start_date'),end_date:f.get('end_date'),start_time:f.get('start_time'),end_time:f.get('end_time'),daily_minutes:Number(f.get('daily_minutes'))}:null;saveAdvisorDraft();};
  form.oninput=persist;
  form.addEventListener('invalid',()=>{form.querySelector('.study-advisor-options').open=true;},true);
  $('#study-limit-toggle').onchange=e=>{const field=form.querySelector('.study-availability');field.hidden=field.disabled=!e.target.checked;persist();};
  $('#study-advisor-reset').onclick=()=>{studyAdvisorHistory=[];studyAdvisorRequest='';saveAdvisorDraft();studyAdvisorForm();};
  form.onsubmit=async e=>{
    e.preventDefault();if(state.job){toast('小探正在处理上一项安排。');return;}
    persist();const request=studyAdvisorRequest.trim();if(!request)return;
    if(studyAdvisorHistory.length>=16){toast('这次对话比较长，请点击重新开始，概括新的安排。');return;}
    const submitted=[...studyAdvisorHistory,{role:'user',content:request}];
    $('#dialog').close();
    try{await runJob('study-plan',{request,history:studyAdvisorHistory,article_ids:studyAdvisorArticles,availability:studyAdvisorAvailability},async result=>{
      if(result.questions.length){studyAdvisorHistory=[...submitted,{role:'assistant',content:[result.message,...result.questions].filter(Boolean).join('\n')}];studyAdvisorRequest='';saveAdvisorDraft();studyAdvisorForm();return;}
      studyAdvisorHistory=[];studyAdvisorRequest='';saveAdvisorDraft();await studyPage('plans');
      modal('计划已添加',`<p>${esc(result.message)}</p><p>新增 ${result.tasks.length} 项安排。</p><div class="study-plan-preview">${result.tasks.map(t=>`<div><small>${esc(t.date)} ${esc(t.time)} · ${t.minutes} 分钟</small><strong>${esc(t.title)}</strong><p>${esc(t.description)}</p></div>`).join('')}</div><div class="actions"><button id="study-undo-plan">撤销本次添加</button>${button('close','查看计划','primary')}</div>`);
      $('#study-undo-plan').onclick=async()=>{try{await api('study/undo-plan',{id:result.batch_id});$('#dialog').close();await studyPage('plans');toast('本次添加已撤销');}catch(err){toast(err.message);}};
    });}catch(err){toast(err.message);studyAdvisorForm();}
  };
}
const advisorStudyPage=studyPage;
studyPage=async function(page='themes'){if(await advisorStudyPage(page)===false)return false;if(state.page==='study'&&study.page===page&&(page==='plans'||page==='today'))$('#main .page-heading .actions')?.insertAdjacentHTML('afterbegin',button('study-advisor','请小探规划'));};
const advisorDispatch=dispatch;
dispatch=async function(action){if(action==='study-advisor'){studyAdvisorForm();return;}return advisorDispatch(action);};
