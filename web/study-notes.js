const studyNotes={article:null,items:[],legacy:[],edit:null,pending:null,timer:null,chain:Promise.resolve()};
async function loadStudyNotes(){
  const aid=state.article.id;
  const result=await api('study-notes/'+aid);
  if(state.article?.id!==aid)return;
  studyNotes.article=aid;studyNotes.items=result.notes;studyNotes.legacy=result.legacy;
}
async function flushStudyNote(){
  clearTimeout(studyNotes.timer);
  const pending=studyNotes.pending;
  if(!pending)return studyNotes.chain;
  studyNotes.chain=studyNotes.chain.catch(()=>{}).then(async()=>{
    if(studyNotes.pending!==pending)return;
    if(!pending.content.trim())return;
    const result=await api('study/notes',pending);
    if(studyNotes.pending===pending)studyNotes.pending=null;
    if(studyNotes.edit===pending.id||!pending.id){
      studyNotes.edit=result.id;
      if(studyNotes.pending&&!studyNotes.pending.id)studyNotes.pending.id=result.id;
    }
    studyNotes.items=[result,...studyNotes.items.filter(n=>n.id!==result.id)];
    const status=$('#study-note-status');if(status)status.textContent=studyNotes.pending?'正在保存…':'已保存';
    if(studyNotes.pending)localStorage.setItem('study-note-draft:'+pending.article_id,JSON.stringify(studyNotes.pending));
    else localStorage.removeItem('study-note-draft:'+pending.article_id);
  });
  return studyNotes.chain;
}
function queueStudyNote(){
  const content=$('#study-note-content');if(!content||!state.article)return;
  const old=studyNotes.items.find(n=>n.id===studyNotes.edit);
  studyNotes.pending={id:studyNotes.edit||undefined,draft_id:studyNotes.pending?.draft_id||old?.draft_id||crypto.randomUUID(),article_id:old?.article_id||state.article.id,title:$('#study-note-title').value,content:content.value,block:studyNotes.anchor?.block||'',quote:studyNotes.anchor?.quote||''};
  localStorage.setItem('study-note-draft:'+studyNotes.pending.article_id,JSON.stringify(studyNotes.pending));
  $('#study-note-status').textContent=content.value.trim()?'正在保存…':'尚未填写正文';
  const save=$('[data-action="study-note-save"]');if(save)save.disabled=!content.value.trim();
  clearTimeout(studyNotes.timer);studyNotes.timer=setTimeout(()=>flushStudyNote().catch(err=>{if($('#study-note-status'))$('#study-note-status').textContent='保存失败，草稿已留在本机';toast(err.message);}),650);
}
function studyNotesHTML(){
  const selected=studyNotes.items.find(n=>n.id===studyNotes.edit);
  const draft=studyNotes.pending||selected||{};
  const quote=draft.quote||studyNotes.anchor?.quote;
  const quoteHTML=quote?`<details class="study-note-reference"><summary><span>引用原文</span><span class="study-note-reference-preview">${esc(quote)}</span></summary><blockquote>${esc(quote)}</blockquote></details>`:'';
  return `<section class="study-notes"><div class="study-section-head"><h3>心得与笔记</h3>${button('study-note-new','新建')}</div>${quoteHTML}<label class="form-field">标题<input id="study-note-title" maxlength="200" value="${esc(draft.title||'')}" placeholder="这次阅读的发现"></label><label class="form-field">我的心得<textarea id="study-note-content" maxlength="20000" placeholder="记录理解、疑问或新的想法">${esc(draft.content||'')}</textarea></label><div class="actions"><small id="study-note-status" role="status">${draft.id?'已保存':'尚未填写正文'}</small>${button('study-note-save','保存')}${selected?`<button data-study-delete-note="${selected.id}" title="删除笔记" aria-label="删除笔记">${studyIcon('trash')}</button>`:''}</div><div class="study-note-list">${studyNotes.items.map(n=>`<article><button data-study-edit-note="${n.id}"><strong>${esc(n.title||'阅读心得')}</strong><p>${esc(n.content.slice(0,130))}</p><small>${esc(new Date(n.updated_at).toLocaleDateString())}${n.version!==state.article.version?' · 旧版本笔记':''}</small></button>${n.block?`<button data-study-locate-note="${n.id}">查看引用</button>`:''}</article>`).join('')}</div>${studyNotes.legacy.length?`<details><summary>已有笔记 · ${studyNotes.legacy.length}</summary>${studyNotes.legacy.map(n=>`<article><h4>${esc(n.title)}</h4><p>${esc(n.note)}</p></article>`).join('')}</details>`:''}</section>`;
}
const studyNotesChat=chatHTML;
const studyNotesWireReader=wireReader;
wireReader=function(){studyNotesWireReader();const source=$('#source-body');if(!source)return;source.addEventListener('mouseup',()=>{const selection=window.getSelection();if(!selection||selection.isCollapsed)return;const start=selection.anchorNode?.parentElement?.closest('[data-block]');const end=selection.focusNode?.parentElement?.closest('[data-block]');const quote=selection.toString().trim();if(!start||start!==end||!quote||quote.length>10000)return;const block=state.article.blocks.find(b=>b.id===start.dataset.block);if(!block?.text?.includes(quote))return;state.selection={block:block.id,title:quote};$('#selected-term').textContent=quote.length>80?quote.slice(0,80)+'…':quote;$('#selection-bar').hidden=false;const explain=$('#selection-bar [data-action="selection"]');if(explain)explain.hidden=quote.length>200;});};
chatHTML=function(){return state.assistantTab==='notes'?studyNotesHTML():studyNotesChat();};
const studyNotesRender=renderWork;
renderWork=function(){studyNotesRender();if($('#study-note-content')){for(const id of ['study-note-content','study-note-title'])$('#'+id).addEventListener('input',queueStudyNote);$('[data-action="study-note-save"]').disabled=!$('#study-note-content').value.trim();}const bar=$('#selection-bar');if(bar&&!bar.querySelector('[data-action="study-note-selection"]'))bar.insertAdjacentHTML('beforeend',button('study-note-selection','记笔记'));};
const studyNotesDraft=saveDraft;
saveDraft=async function(){await flushStudyNote();return studyNotesDraft();};
const studyNotesOpen=openArticle;
openArticle=async function(id,oid=null){await flushStudyNote();studyNotes.edit=null;studyNotes.anchor=null;studyNotes.items=[];studyNotes.legacy=[];studyNotes.pending=null;await studyNotesOpen(id,oid);await loadStudyNotes();const raw=localStorage.getItem('study-note-draft:'+id);if(raw){try{studyNotes.pending=JSON.parse(raw);studyNotes.edit=studyNotes.pending.id||null;}catch{localStorage.removeItem('study-note-draft:'+id);}}};
const studyNotesDispatch=dispatch;
dispatch=async function(action){
  if(action==='notes-tab'||action==='study-note-selection'){
    await saveDraft();await loadStudyNotes();state.assistant=true;state.assistantTab='notes';state.view='explore';
    if(action==='study-note-selection'){const s=state.selection;if(!s)return;studyNotes.edit=null;studyNotes.pending=null;studyNotes.anchor={block:s.block,quote:s.title};}
    renderWork();return;
  }
  if(action==='study-note-new'){await flushStudyNote();studyNotes.edit=null;studyNotes.pending=null;studyNotes.anchor=null;renderWork();$('#study-note-content')?.focus();return;}
  if(action==='study-note-save'){queueStudyNote();await flushStudyNote();renderWork();return;}
  return studyNotesDispatch(action);
};
document.addEventListener('click',async e=>{try{
  const edit=e.target.closest('[data-study-edit-note]');if(edit){await flushStudyNote();studyNotes.edit=edit.dataset.studyEditNote;studyNotes.pending=null;studyNotes.anchor=null;renderWork();}
  const locateButton=e.target.closest('[data-study-locate-note]');if(locateButton){const note=studyNotes.items.find(n=>n.id===locateButton.dataset.studyLocateNote);if(state.article.id!==note.article_id)await openArticle(note.article_id);await locate(note.block);}
  const del=e.target.closest('[data-study-delete-note]');if(del){const id=del.dataset.studyDeleteNote;modal('删除笔记',`<p>这条笔记将被删除，文章原文保留。</p><div class="actions">${button('close','取消')}<button id="study-confirm-note-delete" class="primary">删除</button></div>`);$('#study-confirm-note-delete').onclick=async()=>{try{await flushStudyNote();await api('study/delete',{kind:'notes',id});studyNotes.edit=null;studyNotes.pending=null;studyNotes.anchor=null;await loadStudyNotes();$('#dialog').close();renderWork();}catch(err){toast(err.message);}};}
}catch(err){toast(err.message);}});
window.addEventListener('beforeunload',()=>{if(studyNotes.pending?.content.trim())navigator.sendBeacon('/api/study/notes',new Blob([JSON.stringify(studyNotes.pending)],{type:'application/json'}));});
