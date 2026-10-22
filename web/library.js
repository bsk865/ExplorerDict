/* The library assistant owns its conversation; no placeholder article is created. */
let savedLibraryIds=null;try{const value=JSON.parse(localStorage.getItem('library-article-ids'));if(Array.isArray(value))savedLibraryIds=value;}catch{}
const library = {ids:savedLibraryIds,key:null,session:{messages:[],draft:''},busy:false,request:0};
const librarySaveDraft = saveDraft;
saveDraft = async function(){
  if(state.page!=='library')return librarySaveDraft();
  const input=$('#library-question');
  if(input&&library.key){library.session.draft=input.value;await api('session',{key:library.key,draft:input.value});}
};
async function libraryPage(){
  await saveDraft();await refresh();
  const available=state.articles.filter(a=>a.chars>0&&a.source_status!=='pending_source');
  library.ids=library.ids===null?available.map(a=>a.id):library.ids.filter(id=>available.some(a=>a.id===id));
  state.page='library';state.article=null;state.object=null;document.body.dataset.page='library';
  setCrumb('小探 · 资料库问答');
  const request=++library.request;
  const result=library.ids.length?await api('library-session',{article_ids:library.ids,include_notes:library.includeNotes===true}):{key:null,session:{messages:[],draft:''}};
  if(request!==library.request||state.page!=='library')return;
  library.key=result.key;library.session=result.session;state.session=result.session;
  renderLibrary();
}
function renderLibrary(){
  if(state.page!=='library')return;
  document.querySelectorAll('#navigation .nav-item').forEach(el=>el.classList.toggle('active',el.dataset.action==='library'));
  $('#main').innerHTML=`<div class="library-assistant"><header class="library-heading"><div class="chat-title"><img src="${avatar()}" alt="小探"><div><h1>小探 · 资料库问答</h1><p>查阅你的学习资料，把回答带回原文。</p></div></div><div class="actions">${button('library-scope','选择资料')}${button('shelf','管理资料')}${button('library-clear','清空对话')}</div></header><div class="library-scope-summary"><strong>查阅范围 · ${library.ids.length} 篇资料</strong><span>${esc(state.articles.filter(a=>library.ids.includes(a.id)).map(a=>a.title).join('、')||'先添加文字资料，再选择查阅范围')}</span></div><section class="panel library-conversation"><div id="library-messages" class="chat-body" aria-live="polite">${libraryMessages()}</div><div class="composer"><textarea id="library-question" maxlength="6000" aria-label="向资料库小探提问" placeholder="例如：这些资料如何解释检索增强生成？有哪些不同观点？" ${!library.ids.length?'disabled':''}>${esc(library.session.draft||'')}</textarea><p class="library-privacy">文字检索在本机完成。发送后，相关原文片段及本段对话会交给已配置的 API；本入口不发送图片。</p><div class="actions"><span class="muted">${esc(state.config.model||'请在设置中配置模型')}</span>${contextMeterHTML(library.session.context_usage)}${button('library-search','查找依据')}${button('library-send','发送','primary')}</div></div></section></div>`;
  $('#library-question').oninput=e=>{library.session.draft=e.target.value;};
  $('#library-question').onkeydown=e=>{if(e.key==='Enter'&&(e.ctrlKey||e.metaKey)){e.preventDefault();dispatch('library-send').catch(e=>toast(e.message));}};
  $('#library-messages').scrollTop=$('#library-messages').scrollHeight;
}
// Escape first: model text is never trusted as HTML or executable links.
function libraryAnswerHTML(content,evidence,index){
  const inline=text=>esc(text).replace(/`([^`\n]+)`/g,'<code>$1</code>').replace(/\*\*([^*\n]+)\*\*/g,'<strong>$1</strong>').replace(/\[S(\d+)\]/g,(raw,n)=>{
    const j=evidence.findIndex(e=>e.citation==='S'+n);
    return j<0?raw:`<button class="library-cite" data-library-reference="${index}:${j}" title="${esc(evidence[j].article_title)}" aria-label="查看来源 ${j+1}">${j+1}</button>`;
  });
  const lines=String(content||'').split(/\r?\n/),out=[];let list='',code=null;
  const close=()=>{if(list){out.push('</'+list+'>');list='';}};
  for(const line of lines){
    if(/^\s*```/.test(line)){close();if(code!==null){out.push('<pre><code>'+esc(code.join('\n'))+'</code></pre>');code=null;}else code=[];continue;}
    if(code!==null){code.push(line);continue;}
    if(!line.trim()){close();continue;}
    const item=line.match(/^\s*(?:([-*+])|\d+[.)])\s+(.+)$/);
    if(item){const type=item[1]?'ul':'ol';if(list!==type){close();out.push('<'+type+'>');list=type;}out.push('<li>'+inline(item[2])+'</li>');continue;}
    close();const heading=line.match(/^#{1,6}\s+(.+)$/);
    out.push(heading?'<h3>'+inline(heading[1])+'</h3>':'<p>'+inline(line)+'</p>');
  }
  close();if(code!==null)out.push('<pre><code>'+esc(code.join('\n'))+'</code></pre>');
  return out.join('');
}
function libraryMessages(){
  if(!library.session.messages.length)return `<div class="library-welcome"><img src="${avatar()}" alt=""><h2>从资料中找到答案</h2><p>可以查概念、比较观点，或追问一段论述的依据。</p><p>选择资料后直接提问，也可以先「查找依据」预览本机检索结果。切换资料范围会进入独立对话。</p></div>`;
  return library.session.messages.map((m,i)=>{const evidence=m.evidence||[];return `<div class="message ${m.role==='assistant'?'assistant':'user'}">${m.role==='assistant'?'<small>小探</small><div class="library-answer">'+libraryAnswerHTML(m.content,evidence,i)+'</div>':esc(m.content)}${evidence.length?`<details class="library-sources"><summary>参考来源 · ${evidence.length}</summary><div class="citations">${evidence.map((e,j)=>`<button data-library-reference="${i}:${j}">${j+1}. ${esc(e.article_title)}${e.source==='personal_note'?' · 个人笔记':''}</button>`).join('')}</div></details>`:''}</div>`;}).join('');
}
function libraryReference(e){
  modal('原文依据',`<h3>${esc(e.article_title)}</h3><p>${e.page?'第 '+e.page+' 页':'原文片段'}</p><blockquote class="reference-quote">${esc(e.quote)}</blockquote><div class="actions"><button id="library-open-source" class="primary">在原文中定位</button></div>`);
  $('#library-open-source').onclick=async()=>{
    try{await saveDraft();const a=await api('article/'+e.article_id);if(a.version!==e.version)throw Error('原文版本已变化，请重新检索核对。');$('#dialog').close();await openArticle(a.id);if(state.page!=='work'||state.article?.id!==a.id)return;await locate(e.block);
      const p=document.querySelector('#source-'+e.block+' p');if(p&&p.textContent.includes(e.quote)){const t=p.textContent,pos=t.indexOf(e.quote);const mark=document.createElement('mark');mark.textContent=e.quote;p.replaceChildren(document.createTextNode(t.slice(0,pos)),mark,document.createTextNode(t.slice(pos+e.quote.length)));mark.scrollIntoView({block:'center'});}
      $('#main').insertAdjacentHTML('afterbegin',button('page-back','返回上一页','library-return'));
    }catch(error){toast(error.message);}
  };
}
const libraryDispatch=dispatch;
dispatch=async function(action){
  if(action==='library'||action==='character')return libraryPage();
  if(!action.startsWith('library-'))return libraryDispatch(action);
  if(library.busy||state.job)throw Error('请先等待当前任务完成，或点击停止。');
  if(action==='library-scope'){
    await saveDraft();await refresh();
    modal('选择查阅资料',`<p class="modal-note">资料书架中的文章直接用于检索，不会重复保存。没有文字原文的资料暂不参与问答。</p><div class="library-picker">${state.articles.map(a=>`<label><input type="checkbox" value="${a.id}" ${library.ids.includes(a.id)?'checked':''} ${!a.chars||a.source_status==='pending_source'?'disabled':''}><span>${esc(a.title)}<small>${a.chars?'可检索文字 · '+a.chars+' 字':'暂无文字原文'}</small></span></label>`).join('')||'<p>资料库还是空的，请先到资料书架添加文件。</p>'}</div><div class="actions">${button('close','取消')}${button('library-apply','使用所选资料','primary')}</div>`);return;
  }
  if(action==='library-apply'){const ids=[...document.querySelectorAll('.library-picker input:checked:not(:disabled)')].map(e=>e.value);if(!ids.length)throw Error('请至少选择一篇资料。');library.ids=ids;localStorage.setItem('library-article-ids',JSON.stringify(ids));$('#dialog').close();return libraryPage();}
  if(!library.ids.length)throw Error('请先添加并选择资料。');
  if(action==='library-clear'){modal('清空当前对话',`<p>只清空此资料范围的问答和草稿，原文与文章笔记保留。</p><div class="actions">${button('close','取消')}${button('library-confirm-clear','清空对话','primary')}</div>`);return;}
  if(action==='library-confirm-clear'){await api('library-clear',{article_ids:library.ids,include_notes:library.includeNotes===true});library.session={messages:[],draft:''};$('#library-question').value='';$('#dialog').close();return libraryPage();}
  const question=$('#library-question')?.value.trim();if(!question)throw Error('请先输入问题。');
  library.busy=true;
  $('#library-question').disabled=true;
  try{
    await saveDraft();const body={article_ids:[...library.ids],question,session_key:library.key,include_notes:library.includeNotes===true};
    if(action==='library-search'){
      const result=await api('library-search',body);
      modal('本机查找结果',`<p class="modal-note">${result.evidence.length?'找到 '+result.evidence.length+' 处相关文字，尚未发送给模型。':'未找到匹配的文字依据，请补充具体术语或调整范围。'}</p><div class="library-results">${result.evidence.map((e,i)=>`<button data-library-hit="${i}"><strong>${esc(e.article_title)} [${e.citation}]</strong><p>${esc(e.quote.slice(0,220))}</p></button>`).join('')}</div>${button('close','返回提问')}`);
      document.querySelectorAll('[data-library-hit]').forEach(el=>el.onclick=()=>libraryReference(result.evidence[Number(el.dataset.libraryHit)]));
    }else if(action==='library-send'){
      await runJob('library-chat',body,async result=>{library.session=result;if(!result.closed){library.session.draft='';await api('session',{key:body.session_key,draft:''});}if(state.page==='library'){state.session=result;renderLibrary();}});
    }
  }finally{library.busy=false;if(state.page==='library'&&$('#library-question'))$('#library-question').disabled=!library.ids.length||Boolean(library.session.closed);}
};
document.addEventListener('click',e=>{const control=e.target.closest('[data-library-reference]');if(control){const [i,j]=control.dataset.libraryReference.split(':').map(Number);libraryReference(library.session.messages[i].evidence[j]);}});
window.addEventListener('beforeunload',()=>{if(state.page==='library'&&library.key)navigator.sendBeacon('/api/session',new Blob([JSON.stringify({key:library.key,draft:$('#library-question')?.value||''})],{type:'application/json'}));});
