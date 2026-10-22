const researchToolsRender=renderLibrary;
renderLibrary=function(){researchToolsRender();if(state.page!=='library')return;
  $('.library-heading .actions').insertAdjacentHTML('afterbegin',button('study-compare','比较阅读'));
  document.querySelectorAll('#library-messages .message').forEach((el,i)=>{const m=library.session.messages[i];if(m?.role==='assistant'&&m.evidence?.length)el.insertAdjacentHTML('beforeend',`<button class="study-save-answer" data-study-save-answer="${i}">${studyIcon('note')}整理为笔记</button>`);});
};
const researchToolsDispatch=dispatch;
dispatch=async function(action){
  if(action==='study-compare'){
    if(state.job||library.busy)throw Error('请先等待当前任务完成。');
    if(library.ids.length<2||library.ids.length>12)throw Error('比较阅读请选择2至12篇文章。');
    const question=$('#library-question').value.trim()||'请比较所选文章的研究对象、方法、主要结论与局限，并提出需要进一步核对的问题。';
    await saveDraft();library.busy=true;
    try{await runJob('library-chat',{article_ids:[...library.ids],include_notes:library.includeNotes===true,mode:'compare',question,session_key:library.key},async result=>{library.session=result;if(!result.closed){library.session.draft='';await api('session',{key:library.key,draft:''});}if(state.page==='library'){state.session=result;renderLibrary();}});}finally{library.busy=false;}return;
  }
  return researchToolsDispatch(action);
};
document.addEventListener('click',event=>{const el=event.target.closest('[data-study-save-answer]');if(!el)return;
  const index=Number(el.dataset.studySaveAnswer),message=library.session.messages[index],key=library.key,draftId=crypto.randomUUID();
  if(!message)return;
  modal('整理为阅读笔记',`<form id="study-answer-note-form"><label class="form-field">归入文章<select name="article">${state.articles.filter(a=>library.ids.includes(a.id)).map(a=>`<option value="${a.id}">${esc(a.title)}</option>`).join('')}</select></label><label class="form-field">标题<input name="title" maxlength="200" value="比较与思考" required></label><label class="form-field">笔记正文<textarea name="content" maxlength="20000" required>${esc(message.content)}</textarea></label><p class="modal-note">保存后标记为小探整理、经你确认的笔记，并保留本次回答的引用。</p><div class="actions">${button('close','取消')}<button type="submit" class="primary">确认并保存</button></div></form>`);
  $('#study-answer-note-form').onsubmit=async e=>{e.preventDefault();const submit=e.target.querySelector('[type="submit"]');submit.disabled=true;try{const f=new FormData(e.target);await api('study/answer-note',{session_key:key,message_index:index,source_content:message.content,article_id:f.get('article'),title:f.get('title'),content:f.get('content'),draft_id:draftId});$('#dialog').close();toast('笔记已随文章保存');}catch(err){submit.disabled=false;toast(err.message);}};
});
const researchNoteHTML=studyNotesHTML;
studyNotesHTML=function(){const html=researchNoteHTML();const note=studyNotes.items.find(n=>n.id===studyNotes.edit);return note?.origin==='assistant_confirmed'?html.replace('<h3>心得与笔记</h3>','<h3>心得与笔记</h3><small>小探整理 · 已确认</small>'):html;};
