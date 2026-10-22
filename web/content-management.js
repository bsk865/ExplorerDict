const contentManager={kind:'notes',query:'',article:'',data:null};
const contentKinds={notes:'阅读笔记',tasks:'计划',articles:'文章资料',themes:'主题分类',objects:'词语与图片探索',legacy_notes:'旧笔记',conversations:'问答与交接',batches:'规划记录',maps:'导图与草稿',reading:'阅读记录',weather:'天气城市',drafts:'未提交草稿'};
const contentDeleteScope={notes:'只删除这篇笔记，文章原文保留。历史回答中的引用快照不会被改写。',tasks:'只删除这项计划，文章和笔记保留。',articles:'删除文章及其历史版本、词语、笔记、导图和问答。相关计划保留，但会移除文章关联。',themes:'只删除分类，文章和笔记保留。',objects:'删除探索对象及其笔记与问答；若与悬浮取词关联，也会删除对应词条。文章原文和原图保留。',legacy_notes:'只清除笔记正文，原文、探索对象和问答保留。',conversations:'清空这组问答、输入草稿及所有交接记录。独立保存的笔记保留。',batches:'只删除这条规划历史，其中已经生成的计划仍保留。',maps:'删除该版本的导图与生成草稿，文章原文保留。',reading:'清除该文章的阅读记录与读完标记，文章保留。再次阅读会产生新记录。',weather:'移除天气城市及缓存，以后可以重新选择城市。',drafts:'丢弃这份未提交草稿，已保存的正文保留。'};
function localContentDrafts(){return Object.keys(localStorage).filter(k=>k==='study-advisor-draft'||k.startsWith('study-note-draft:')).filter(k=>{try{const d=JSON.parse(localStorage.getItem(k));return k==='study-advisor-draft'?d.request||d.history?.length:d.content||d.title;}catch{return true;}}).map(k=>({id:k,title:k==='study-advisor-draft'?'小探规划草稿':'未保存的阅读笔记'}));}
async function contentManagementPage(kind=contentManager.kind){
  if(!contentKinds[kind])kind='notes';
  await saveDraft();await refresh();contentManager.data=await api('content-management');contentManager.data.drafts=localContentDrafts();contentManager.kind=kind;
  study.data=contentManager.data;state.page='management';state.article=null;state.object=null;document.body.dataset.page='management';setCrumb(kind==='notes'?'笔记管理':'内容管理');
  document.querySelectorAll('#navigation .nav-item').forEach(el=>el.classList.toggle('active',el.dataset.action===(kind==='notes'?'manage-notes':'content-management')));
  $('#main').innerHTML=`<div class="page-heading"><div><h1>${kind==='notes'?'笔记管理':'内容管理'}</h1></div><div class="actions">${kind==='notes'?button('manage-note-new','新建笔记','primary'):kind==='tasks'?button('study-new-task','添加计划','primary'):kind==='articles'?button('import','添加资料','primary'):kind==='themes'?button('study-new-theme','新建主题','primary'):''}</div></div><div class="content-toolbar"><label>内容类型<select id="content-kind">${Object.entries(contentKinds).map(([k,v])=>`<option value="${k}" ${k===kind?'selected':''}>${v} · ${(contentManager.data[k]||[]).length}</option>`).join('')}</select></label><label class="content-search">搜索<input id="content-search" type="search" placeholder="标题或内容" value="${esc(contentManager.query)}"></label>${kind==='notes'?`<label>所属文章<select id="content-article"><option value="">全部文章</option>${state.articles.map(a=>`<option value="${a.id}" ${a.id===contentManager.article?'selected':''}>${esc(a.title)}</option>`).join('')}</select></label>`:''}</div><div id="content-results"></div>`;
  $('#content-kind').onchange=e=>{contentManager.query='';contentManager.article='';contentManagementPage(e.target.value).catch(e=>toast(e.message));};
  $('#content-search').oninput=e=>{contentManager.query=e.target.value;renderContentRows();};
  if($('#content-article'))$('#content-article').onchange=e=>{contentManager.article=e.target.value;renderContentRows();};
  renderContentRows();
}
function renderContentRows(){
  const kind=contentManager.kind,query=contentManager.query.toLowerCase();
  const rows=(contentManager.data[kind]||[]).filter(r=>(!contentManager.article||r.article_id===contentManager.article)&&[r.title,r.content,r.request,r.article_title,r.description].join(' ').toLowerCase().includes(query));
  $('#content-results').innerHTML=`<p class="content-count">${rows.length} 项</p><div class="content-list">${rows.map(r=>{
    const title=r.title||r.article_title||r.request||'阅读笔记';
    const detail=kind==='notes'?`${r.article_title} · ${r.origin==='assistant_confirmed'?'小探整理':'我的心得'}`:kind==='tasks'?`${r.date||'日期待定'} ${r.time||''} · ${r.status==='done'?'已完成':'待完成'}`:kind==='conversations'?`${r.count} 条消息 · ${r.archives} 段交接`:kind==='batches'?`${r.tasks.length} 项安排 · ${r.undone?'已撤销':'已创建'}`:r.article_title||'';
    return `<article class="content-row"><div class="content-row-copy"><strong>${esc(title)}</strong><small>${esc(detail)}</small>${r.content?`<p>${esc(r.content.slice(0,160))}</p>`:''}</div><div class="content-row-actions">${['notes','tasks','themes','legacy_notes'].includes(kind)?`<button data-content-edit="${esc(r.id)}">编辑</button>`:''}${kind==='articles'?`<button data-open="${r.id}">阅读</button>`:['notes','objects'].includes(kind)?`<button data-open="${r.article_id}" title="查看原文" aria-label="查看原文">${studyIcon('book')}</button>`:''}<button data-content-delete="${esc(r.id)}" title="删除${esc(contentKinds[kind])}" aria-label="删除${esc(title)}">${studyIcon('trash')}</button></div></article>`;
  }).join('')||'<div class="empty">没有匹配的内容。</div>'}</div>`;
  $('#content-results').querySelectorAll('[data-content-edit]').forEach(el=>el.onclick=()=>{const row=rows.find(r=>r.id===el.dataset.contentEdit);if(kind==='notes')manageNoteForm(row);else if(kind==='legacy_notes')manageLegacyNoteForm(row);else if(kind==='tasks')studyTaskForm(row);else studyThemeForm(row);});
  $('#content-results').querySelectorAll('[data-content-delete]').forEach(el=>el.onclick=()=>confirmContentDelete(kind,rows.find(r=>r.id===el.dataset.contentDelete)));
}
function manageNoteForm(note=null){
  if(!state.articles.length){toast('先添加一篇资料，再记录笔记。');return;}
  modal(note?'编辑笔记':'新建笔记',`<form id="study-managed-note"><label class="form-field">所属文章<select name="article" ${note?'disabled':''}>${note?`<option value="${note.article_id}">${esc(note.article_title)}</option>`:state.articles.map(a=>`<option value="${a.id}">${esc(a.title)}</option>`).join('')}</select></label><label class="form-field">标题<input name="title" maxlength="200" value="${esc(note?.title||'')}"></label><label class="form-field">正文<textarea name="content" maxlength="20000" required>${esc(note?.content||'')}</textarea></label>${note?.quote?`<blockquote>${esc(note.quote)}</blockquote>`:''}<div class="actions">${button('close','取消')}<button type="submit" class="primary">保存</button></div></form>`);
  const draftId=crypto.randomUUID();$('#study-managed-note').onsubmit=async e=>{e.preventDefault();const b=e.target.querySelector('[type="submit"]');b.disabled=true;try{const f=new FormData(e.target);await api('study/notes',{id:note?.id,draft_id:draftId,article_id:note?.article_id||f.get('article'),title:f.get('title'),content:f.get('content')});$('#dialog').close();await contentManagementPage('notes');}catch(err){b.disabled=false;toast(err.message);}};
}
function manageLegacyNoteForm(note){
  modal('编辑已有笔记',`<form id="study-managed-note"><p>${esc(note.title)}</p><label class="form-field">正文<textarea name="content" maxlength="10000">${esc(note.content)}</textarea></label><div class="actions">${button('close','取消')}<button type="submit" class="primary">保存</button></div></form>`);
  $('#study-managed-note').onsubmit=async e=>{e.preventDefault();const b=e.target.querySelector('[type="submit"]');b.disabled=true;try{const value=new FormData(e.target).get('content');await api(note.source==='object'?'object/update':'session',note.source==='object'?{id:note.id,note:value}:{key:note.id,note:value});$('#dialog').close();await contentManagementPage('legacy_notes');}catch(err){b.disabled=false;toast(err.message);}};
}
function confirmContentDelete(kind,item,after=null){
  if(state.job){toast('请先等待当前任务完成或停止任务。');return;}
  modal('删除'+contentKinds[kind],`<p class="content-delete-title">${esc(item.title||item.article_title||item.request||'所选内容')}</p><p>${contentDeleteScope[kind]}</p><p class="modal-note">此操作无法撤销。</p><div class="actions">${button('close','取消')}<button id="content-confirm-delete" class="primary">确认删除</button></div>`);
  $('#content-confirm-delete').onclick=async e=>{e.target.disabled=true;try{
    if(kind==='drafts'){
      localStorage.removeItem(item.id);if(item.id==='study-advisor-draft'){studyAdvisorRequest='';studyAdvisorHistory=[];studyAdvisorArticles=[];studyAdvisorAvailability=null;}else if(studyNotes.pending?.article_id===item.id.slice('study-note-draft:'.length)){clearTimeout(studyNotes.timer);studyNotes.pending=null;}
    }else if(kind==='articles'){
      const result=await api('article/delete',{article_id:item.id});
      for(const id of result.article_ids||[item.id]){localStorage.removeItem('study-note-draft:'+id);for(const key of drafts.keys())if(key.startsWith(id+':'))drafts.delete(key);if(localStorage.getItem('reading-last-article')===id)localStorage.removeItem('reading-last-article');}
    }else await api('content-delete',{kind:kind==='legacy_notes'?(item.source==='object'?'object_note':'session_note'):kind,id:item.id});
    if(kind==='notes'){localStorage.removeItem('study-note-draft:'+item.article_id);if(studyNotes.edit===item.id){studyNotes.edit=null;studyNotes.pending=null;studyNotes.anchor=null;}}
    if(kind==='conversations'){drafts.delete(item.id);if(library.key===item.id){library.session={messages:[],draft:''};library.key=null;}}
    $('#dialog').close();if(after)await after();else await contentManagementPage(kind);toast('已删除');
  }catch(err){e.target.disabled=false;toast(err.message);}};
}
const contentStudyPage=studyPage;
studyPage=async function(page='themes'){
  if(await contentStudyPage(page)===false)return false;
  if(state.page!=='study'||study.page!==page)return;
  if(page==='themes'){
    document.querySelectorAll('.study-article-list>button[data-open]').forEach(el=>{const article=state.articles.find(a=>a.id===el.dataset.open),row=document.createElement('div');row.className='managed-article-row';el.replaceWith(row);row.append(el);row.insertAdjacentHTML('beforeend',`<button data-content-article-delete="${article.id}" aria-label="删除${esc(article.title)}" title="删除资料">${studyIcon('trash')}</button>`);row.lastElementChild.onclick=()=>confirmContentDelete('articles',article,()=>studyPage('themes'));});
  }
  if(page==='plans'||page==='today'){
    document.querySelectorAll('.study-task').forEach(el=>{const task=study.data.tasks.find(t=>t.id===el.querySelector('[data-study-task]').dataset.studyTask);const tools=el.querySelector('.study-task-links');tools.insertAdjacentHTML('beforeend',`<button data-study-task="${task.id}" aria-label="编辑${esc(task.title)}" title="编辑计划">${studyIcon('note')}</button><button data-content-task-delete="${task.id}" aria-label="删除${esc(task.title)}" title="删除计划">${studyIcon('trash')}</button>`);tools.lastElementChild.onclick=()=>confirmContentDelete('tasks',task,()=>studyPage(page));});
    if(page==='plans'&&study.planView==='all'){
      const list=$('.study-task-list');let last=null;[...list.querySelectorAll('.study-task')].forEach(row=>{const t=study.data.tasks.find(t=>t.id===row.querySelector('[data-study-task]').dataset.studyTask),group=t.date||'未安排日期';if(group!==last){row.insertAdjacentHTML('beforebegin',`<h2 class="study-date-group">${esc(group)}${group===studyDate()?' · 今天':''}</h2>`);last=group;}});
      list.insertAdjacentHTML('beforebegin',`<div class="plan-list-filters"><input id="plan-search" type="search" aria-label="搜索计划" placeholder="搜索计划"><select id="plan-status" aria-label="计划状态"><option value="all">全部状态</option><option value="pending">未完成</option><option value="done">已完成</option></select></div>`);
      const filter=()=>{const q=$('#plan-search').value.toLowerCase(),status=$('#plan-status').value;list.querySelectorAll('.study-task').forEach(row=>{const done=row.querySelector('[data-study-complete]').checked;row.hidden=!row.textContent.toLowerCase().includes(q)||(status==='done'&&!done)||(status==='pending'&&done);});list.querySelectorAll('.study-date-group').forEach(h=>{let next=h.nextElementSibling,visible=false;while(next&&!next.matches('.study-date-group')){if(!next.hidden)visible=true;next=next.nextElementSibling;}h.hidden=!visible;});};
      $('#plan-search').oninput=filter;$('#plan-status').onchange=filter;list.addEventListener('change',filter);
    }
  }
};
const contentDispatch=dispatch;
dispatch=async function(action){if(action==='content-management')return contentManagementPage();if(action==='manage-notes'||action==='saved')return contentManagementPage('notes');if(action==='manage-note-new')return manageNoteForm();if(action==='sync'&&state.page==='management'){await contentDispatch(action);return contentManagementPage();}return contentDispatch(action);};
$('#navigation').insertAdjacentHTML('beforeend',`<button class="nav-item" data-action="manage-notes">${studyIcon('note')}笔记管理</button><button class="nav-item" data-action="content-management">${studyIcon('settings')}内容管理</button>`);
