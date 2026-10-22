library.includeNotes=localStorage.getItem('library-include-notes')==='true';
const studyLibraryRender=renderLibrary;
renderLibrary=function(){
  studyLibraryRender();if(state.page!=='library')return;
  $('.library-scope-summary').insertAdjacentHTML('beforeend',`<label class="study-note-toggle"><input id="library-include-notes" type="checkbox" ${library.includeNotes?'checked':''}>包含我的笔记</label>`);
  $('.library-privacy').textContent='检索在本机完成。发送后，相关原文'+(library.includeNotes?'、个人笔记':'')+'及本段对话会交给已配置的 API。';
  $('#library-include-notes').onchange=async e=>{try{if(state.job||library.busy)throw Error('请先等待回答完成。');e.target.disabled=true;$('#library-question').disabled=true;document.querySelectorAll('[data-action="library-send"],[data-action="library-search"]').forEach(el=>el.disabled=true);await saveDraft();library.includeNotes=e.target.checked;localStorage.setItem('library-include-notes',String(library.includeNotes));await libraryPage();}catch(err){renderLibrary();toast(err.message);}};
};
const studyLibraryReference=libraryReference;
libraryReference=function(e){
  if(e.source!=='personal_note')return studyLibraryReference(e);
  modal('个人笔记依据',`<h3>${esc(e.note_title||e.article_title)}</h3><p>来自你的心得与笔记</p><blockquote class="reference-quote">${esc(e.quote)}</blockquote><div class="actions"><button id="study-open-cited-note" class="primary">查看笔记</button>${button('close','关闭')}</div>`);
  $('#study-open-cited-note').onclick=async()=>{try{await openArticle(e.article_id);const note=studyNotes.items.find(n=>n.id===e.note_id);if(!note)throw Error('这条笔记已删除，以上保留的是回答时的引用。');studyNotes.edit=note.id;state.assistant=true;state.assistantTab='notes';$('#dialog').close();renderWork();}catch(err){toast(err.message);}};
};
const studyLibraryDispatch=dispatch;
dispatch=async function(action){
  if(action==='library-scope'){
    await studyLibraryDispatch(action);const data=await api('study');
    $('.library-picker').insertAdjacentHTML('beforebegin',`<div class="study-scope-themes"><button data-study-scope="all">全部资料</button>${data.themes.map(t=>`<button data-study-scope="${t.id}">${esc(t.title)}</button>`).join('')}</div>`);
    document.querySelectorAll('[data-study-scope]').forEach(el=>el.onclick=()=>{const ids=new Set(data.memberships.filter(m=>m.theme_id===el.dataset.studyScope).map(m=>m.article_id));document.querySelectorAll('.library-picker input:not(:disabled)').forEach(input=>input.checked=el.dataset.studyScope==='all'||ids.has(input.value));});return;
  }
  if(action==='library-search'){
    if(state.job||library.busy)throw Error('请等待当前任务完成。');
    const question=$('#library-question')?.value.trim();if(!question)throw Error('请先输入问题。');
    await saveDraft();const result=await api('library-search',{article_ids:library.ids,question,include_notes:library.includeNotes===true});
    modal('查找依据',`<p>${result.reason==='budget'?'已找到相关内容，但剩余上下文不足。请接续新对话或提高预算。':result.evidence.length?`找到 ${result.evidence.length} 处依据`:'未找到匹配内容，请调整问题或资料范围。'}</p><div class="library-results">${result.evidence.map((e,i)=>`<button data-study-hit="${i}"><small>${e.source==='personal_note'?'个人笔记':'文章原文'}</small><strong>${esc(e.article_title)}</strong><p>${esc(e.quote.slice(0,220))}</p></button>`).join('')}</div>${button('close','返回提问')}`);
    const retrieval=result.retrieval||{};
    $('#dialog-body').insertAdjacentHTML('afterbegin',`<p class="muted">${retrieval.semantic_error?'语义检索暂不可用，本次使用关键词检索。':retrieval.mode==='hybrid'?'本次结合语义与关键词查找。':'本次使用关键词检索结果。'}</p>`);
    document.querySelectorAll('[data-study-hit]').forEach(el=>el.onclick=()=>libraryReference(result.evidence[Number(el.dataset.studyHit)]));return;
  }
  return studyLibraryDispatch(action);
};
