/* Shared local study workspace. */
const study = {data:null, theme:null, page:'themes'};
document.addEventListener('click',e=>{if(e.target.closest('#navigation button,[data-open]'))study.request=(study.request||0)+1;},true);
const studyIcon = name => window.ExplorerIcons ? `<svg class="brand-icon" viewBox="0 0 24 24" aria-hidden="true">${window.ExplorerIcons[name]||''}</svg>` : '';
const studyDate = () => {const d=new Date();return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;};
async function studyPage(page='themes'){
  const request=study.request=(study.request||0)+1;
  await saveDraft();const [,data]=await Promise.all([refresh(),api('study')]);
  if(request!==study.request)return false;
  study.data=data; study.page=page;
  state.page='study'; state.article=null; state.object=null; document.body.dataset.page='shelf';
  const names={themes:'主题资料库',plans:'计划',today:'今日',stats:'探索统计'};
  setCrumb(names[page]);
  let html=`<div class="page-heading"><div><h1>${names[page]}</h1><p>${page==='today'?esc(new Date().toLocaleDateString('zh-CN',{weekday:'long',year:'numeric',month:'long',day:'numeric'})):''}</p></div><div class="actions">${page==='themes'?button('study-new-theme','新建主题')+button('import','添加资料','primary'):page==='plans'||page==='today'?button('study-new-task','添加计划','primary'):''}</div></div>`;
  if(page==='themes'){
    const selected=study.data.themes.find(t=>t.id===study.theme);
    const assigned=new Set(study.data.memberships.map(m=>m.article_id));
    const ids=new Set(study.data.memberships.filter(m=>m.theme_id===study.theme).map(m=>m.article_id));
    const articles=state.articles.filter(a=>(study.theme==='unfiled'?!assigned.has(a.id):selected?ids.has(a.id):true)&&a.title.toLowerCase().includes($('#global-search').value.toLowerCase()));
    html+=`<div class="study-library"><aside class="study-topics"><button data-study-theme="" class="${!study.theme?'active':''}">${studyIcon('archive')}全部资料 <small>${state.articles.length}</small></button><button data-study-theme="unfiled">${studyIcon('file')}未分类 <small>${state.articles.filter(a=>!assigned.has(a.id)).length}</small></button>${study.data.themes.map(t=>`<button data-study-theme="${t.id}" class="${study.theme===t.id?'active':''}">${studyIcon('book')}${esc(t.title)}<small>${study.data.memberships.filter(m=>m.theme_id===t.id).length}</small></button>`).join('')}</aside><section class="study-contents"><div class="study-section-head"><h2>${esc(selected?.title||(study.theme==='unfiled'?'未分类':'全部资料'))}</h2>${selected?button('study-members','整理文章')+button('study-edit-theme','编辑主题'):''}</div>${selected?.description?`<p>${esc(selected.description)}</p>`:''}${!study.theme&&study.data.themes.length?`<div class="study-rooms">${study.data.themes.map(t=>`<button data-study-theme="${t.id}">${studyIcon('archive')}<strong>${esc(t.title)}</strong><span>${study.data.memberships.filter(m=>m.theme_id===t.id).length} 篇文章</span></button>`).join('')}</div>`:''}<div class="study-article-list">${articles.map(a=>`<button data-open="${a.id}">${studyIcon('file')}<span><strong>${esc(a.title)}</strong><small>${a.chars} 字 · ${study.data.notes.filter(n=>n.article_id===a.id).length} 篇笔记</small></span>${studyIcon('arrow-right')}</button>`).join('')||'<p class="empty">这里还没有文章。</p>'}</div></section></div>`;
  }else if(page==='plans'||page==='today'){
    const tasks=study.data.tasks.filter(t=>page!=='today'||t.date===studyDate()).sort((a,b)=>(a.date+a.time).localeCompare(b.date+b.time));
    html+=`<div class="study-task-list">${tasks.map(t=>`<div class="study-task ${t.status==='done'?'is-done':''}"><input type="checkbox" aria-label="完成 ${esc(t.title)}" data-study-complete="${t.id}" ${t.status==='done'?'checked':''}><span class="study-task-time">${esc(t.date||'未安排日期')}<strong>${esc(t.time||'时间待定')}</strong></span><button class="study-task-title" data-study-task="${t.id}"><strong>${esc(t.title)}</strong><small>${esc(t.goal)}${t.goal?' · ':''}${t.minutes} 分钟</small></button><div class="study-task-links">${t.article_ids.map(id=>`<button data-open="${id}" title="${esc(state.articles.find(a=>a.id===id)?.title||'文章')}">${studyIcon('book')}</button>`).join('')}</div></div>`).join('')||'<div class="empty">'+(page==='today'?'今天还没有安排。':'还没有计划。')+'</div>'}</div>`;
  }else{
    html+=`<div class="study-metrics">${[['资料',study.data.stats.articles],['心得与笔记',study.data.stats.notes],['已完成任务',study.data.stats.completed_tasks]].map(([label,n])=>`<div><strong>${n}</strong><span>${label}</span></div>`).join('')}</div><h2>最近的心得</h2><div class="study-article-list">${study.data.notes.slice(0,10).map(n=>`<button data-open="${n.article_id}">${studyIcon('note')}<span><strong>${esc(n.title||n.article_title)}</strong><small>${esc(n.content.slice(0,100))}</small></span></button>`).join('')||'<p class="empty">还没有记录心得。</p>'}</div>`;
  }
  $('#main').innerHTML=html;
  syncPrototype();
  if(page==='themes')$('#main .page-heading .actions').insertAdjacentHTML('afterbegin',button('saved','笔记与收藏'));
}
function studyThemeForm(existing){
  modal(existing?'编辑主题':'新建主题',`<form id="study-theme-form"><label class="form-field">主题名称<input name="title" maxlength="80" required value="${esc(existing?.title||'')}"></label><label class="form-field">简介<textarea name="description" maxlength="1000">${esc(existing?.description||'')}</textarea></label><div class="actions">${button('close','取消')}<button type="submit" class="primary">保存</button></div></form>`);
  $('#study-theme-form').onsubmit=async e=>{e.preventDefault();const submit=e.target.querySelector('[type="submit"]');if(submit.disabled)return;submit.disabled=true;e.target.querySelector('[data-local-error]')?.remove();try{const f=new FormData(e.target);const result=await api('study/themes',{id:existing?.id,title:f.get('title'),description:f.get('description')});study.theme=result.id;$('#dialog').close();await studyPage('themes');}catch(err){const notice=document.createElement('p');notice.className='modal-note';notice.dataset.localError='true';notice.setAttribute('role','alert');notice.textContent=err.message;e.target.insertBefore(notice,submit.closest('.actions'));}finally{submit.disabled=false;}};
}
function studyTaskForm(existing){
  modal(existing?'编辑计划':'添加计划',`<form id="study-task-form"><label class="form-field">任务<input name="title" required maxlength="200" value="${esc(existing?.title||'')}"></label><label class="form-field">目标<input name="goal" maxlength="200" value="${esc(existing?.goal||'')}"></label><div class="study-form-row"><label class="form-field">日期<input name="date" type="date" value="${esc(existing?.date||studyDate())}"></label><label class="form-field">时间<input name="time" type="time" value="${esc(existing?.time||'')}"></label><label class="form-field">预计分钟<input name="minutes" type="number" min="5" max="1440" required value="${existing?.minutes||30}"></label></div><label class="form-field">说明<textarea name="description" maxlength="5000">${esc(existing?.description||'')}</textarea></label><label class="form-field">关联文章<select name="articles" multiple>${state.articles.map(a=>`<option value="${a.id}" ${existing?.article_ids.includes(a.id)?'selected':''}>${esc(a.title)}</option>`).join('')}</select></label><div class="actions">${button('close','取消')}<button type="submit" class="primary">保存</button></div></form>`);
  $('#study-task-form').onsubmit=async e=>{e.preventDefault();try{const f=new FormData(e.target);await api('study/tasks',{id:existing?.id,title:f.get('title'),goal:f.get('goal'),date:f.get('date'),time:f.get('time'),minutes:Number(f.get('minutes')),description:f.get('description'),article_ids:f.getAll('articles')});$('#dialog').close();await studyPage(study.page);}catch(err){toast(err.message);}};
}
const studyPreviousDispatch=dispatch;
dispatch=async function(action){
  if(['study-themes','study-plans','study-today','study-stats'].includes(action))return studyPage(action.slice(6));
  if(action==='study-new-theme')return studyThemeForm();
  if(action==='study-edit-theme')return studyThemeForm(study.data.themes.find(t=>t.id===study.theme));
  if(action==='study-new-task')return studyTaskForm();
  if(action==='study-members'){
    modal('整理文章',`<form id="study-members-form"><div class="library-picker">${state.articles.map(a=>`<label><input type="checkbox" name="article" value="${a.id}" ${study.data.memberships.some(m=>m.theme_id===study.theme&&m.article_id===a.id)?'checked':''}><span>${esc(a.title)}</span></label>`).join('')}</div><div class="actions">${button('close','取消')}<button type="submit" class="primary">保存</button></div></form>`);
    $('#study-members-form').onsubmit=async e=>{e.preventDefault();try{await api('study/assign',{theme_id:study.theme,article_ids:new FormData(e.target).getAll('article')});$('#dialog').close();await studyPage('themes');}catch(err){toast(err.message);}};return;
  }
  return studyPreviousDispatch(action);
};
document.addEventListener('click',async e=>{try{
  const themeButton=e.target.closest('[data-study-theme]');if(themeButton){study.theme=themeButton.dataset.studyTheme||null;await studyPage('themes');}
  const taskButton=e.target.closest('[data-study-task]');if(taskButton)studyTaskForm(study.data.tasks.find(t=>t.id===taskButton.dataset.studyTask));
}catch(err){toast(err.message);}});
document.addEventListener('change',async e=>{if(e.target.matches('[data-study-complete]')){const target=e.target,row=target.closest('.study-task');target.disabled=true;row?.classList.toggle('is-done',target.checked);try{const result=await api('study/tasks',{id:target.dataset.studyComplete,status:target.checked?'done':'todo'});const task=study.data?.tasks.find(t=>t.id===target.dataset.studyComplete);if(task)Object.assign(task,result);document.dispatchEvent(new Event('study-tasks-updated'));toast(target.checked?'又完成一项安排':'已恢复为待完成');}catch(err){target.checked=!target.checked;row?.classList.toggle('is-done',target.checked);toast(err.message);}finally{target.disabled=false;}}});
const studyNavigation=document.querySelector('#navigation');
studyNavigation.insertAdjacentHTML('afterbegin',[['today','今日','grid'],['themes','主题资料库','archive'],['plans','计划','list'],['stats','探索统计','map']].map(([id,label,icon])=>`<button class="nav-item" data-action="study-${id}">${studyIcon(icon)}${label}</button>`).join(''));
studyNavigation.querySelectorAll('[data-action="shelf"],[data-action="concepts"],[data-action="resume-reading"],[data-action="saved"]').forEach(el=>el.remove());
shelf=async function(){return studyPage('themes');};
