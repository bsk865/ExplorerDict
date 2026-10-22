let ragTimer=null;
const ragStateNames={ready:'可语义检索',pending:'等待索引',error:'索引失败',empty:'暂无可检索文字'};
function ragSummary(data){
  if(!data.available)return '语义模型不可用 · 当前仅关键词检索';
  if(data.running)return `正在建立语义索引 · ${data.progress[0]} / ${data.progress[1]} 个片段`;
  if(data.error)return '语义索引异常 · 当前可使用关键词检索';
  if(data.documents.some(d=>d.state==='error'))return '部分资料索引失败 · 查看详情';
  if(data.pending)return `${data.pending} 份资料等待索引`;
  return `语义索引就绪 · ${data.ready} 份资料 · ${data.vectors} 个片段`;
}
async function refreshRagStatus(root){
  clearTimeout(ragTimer);
  try{
    const data=await api('rag/status');if(!root.isConnected)return;
    root.querySelector('.rag-status-text').textContent=ragSummary(data);
    root.dataset.running=String(data.running);
    const detail=root.querySelector('.rag-document-list');
    if(detail){
      root.querySelector('.rag-current').textContent=data.running?data.current:data.error?'暂时无法更新，请稍后重试。':'';
      detail.innerHTML=data.documents.map(d=>`<article class="rag-document"><div><strong>${esc(d.title||'阅读笔记')}</strong><small>${d.type==='note'?'个人笔记':'文章原文'} · ${d.chunks} 个片段</small>${d.error?'<p class="rag-error">尚未完成，请重新建立索引。</p>':''}</div><span class="rag-state" data-state="${d.state}">${ragStateNames[d.state]}</span><button data-rag-inspect="${esc(d.id)}" ${d.state!=='ready'?'disabled':''}>查看片段</button></article>`).join('')||'<p class="empty">添加资料后会自动建立检索索引。</p>';
      detail.querySelectorAll('[data-rag-inspect]').forEach(el=>el.onclick=()=>inspectRagDocument(el.dataset.ragInspect).catch(err=>toast(err.message)));
    }
    if(data.available)ragTimer=setTimeout(()=>refreshRagStatus(root),(data.running||data.pending)?1500:10000);
  }catch(err){if(root.isConnected)root.querySelector('.rag-status-text').textContent='暂时无法读取索引状态，请稍后重试。';}
}
async function inspectRagDocument(id,offset=0){
  const data=await api('rag/chunks?id='+encodeURIComponent(id)+'&offset='+offset);
  modal('检索片段',`<div class="rag-chunks">${data.items.map((r,i)=>`<article><small>片段 ${offset+i+1}${r.page?' · 第 '+r.page+' 页':''}</small><blockquote>${esc(r.quote)}</blockquote><button data-rag-source="${i}">查看来源</button></article>`).join('')||`<p>${data.pending?'原文已变化，正在更新索引。':'没有可显示的片段。'}</p>`}</div><div class="actions">${offset?'<button id="rag-prev">上一页</button>':''}<small>${data.total} 个片段</small>${offset+30<data.total?'<button id="rag-next">下一页</button>':''}${button('close','关闭')}</div>`);
  document.querySelectorAll('[data-rag-source]').forEach(el=>el.onclick=()=>libraryReference(data.items[Number(el.dataset.ragSource)]));
  if($('#rag-prev'))$('#rag-prev').onclick=()=>inspectRagDocument(id,offset-30).catch(e=>toast(e.message));
  if($('#rag-next'))$('#rag-next').onclick=()=>inspectRagDocument(id,offset+30).catch(e=>toast(e.message));
}
async function ragPage(){
  await saveDraft();state.page='rag';state.article=null;state.object=null;document.body.dataset.page='rag';setCrumb('知识索引');
  $('#main').innerHTML=`<div class="page-heading rag-heading"><h1>知识索引</h1><div class="actions">${button('rag-update','更新索引')}${button('rag-rebuild','重建索引')}${button('shelf','返回资料库')}</div></div><section id="rag-workspace"><div class="rag-overview"><strong class="rag-status-text" role="status">读取索引状态…</strong><span>资料在本机处理</span></div><p class="rag-current"></p><div class="rag-document-list"></div></section>`;
  await refreshRagStatus($('#rag-workspace'));
}
function addRagStatus(root){
  root.insertAdjacentHTML('beforeend',`<div class="rag-inline"><span class="rag-status-text" role="status">读取检索状态…</span>${button('rag-page','知识索引')}</div>`);
  refreshRagStatus(root.querySelector('.rag-inline'));
}
const ragRenderLibrary=renderLibrary;
renderLibrary=function(){ragRenderLibrary();if(state.page==='library')addRagStatus($('.library-scope-summary'));};
const ragStudyPage=studyPage;
studyPage=async function(page='themes'){const result=await ragStudyPage(page);if(result===false)return false;if(state.page==='study'&&page==='themes'&&study.page==='themes'){$('#main .page-heading .actions').insertAdjacentHTML('afterbegin',button('rag-page','知识索引'));}};
const ragDispatch=dispatch;
dispatch=async function(action){
  if(action==='rag-page')return ragPage();
  if(action==='rag-update')return refreshRagStatus($('#rag-workspace'));
  if(action==='rag-rebuild'){
    modal('重建检索索引',`<p>重新计算文章和笔记的语义向量。原文、笔记、标注和问答会保留，期间仍可使用关键词检索。</p><div class="actions">${button('close','取消')}<button id="rag-confirm-rebuild" class="primary">开始重建</button></div>`);
    $('#rag-confirm-rebuild').onclick=async e=>{e.target.disabled=true;try{await api('rag/rebuild',{});$('#dialog').close();await ragPage();}catch(err){e.target.disabled=false;toast(err.message);}};return;
  }
  return ragDispatch(action);
};
