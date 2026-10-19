/* Source-grounded reading: scope choice and verifiable citation previews. */
state.researchScope = 'article';
function quoteBlock(quote) {
  const blocks=state.article.blocks;
  const exact=blocks.find(b=>(b.text||b.caption||'').includes(quote));
  if (exact) return exact;
  const start=state.article.text.indexOf(quote);
  if (start<0) return null;
  let offset=0;
  for (const block of blocks) {
    const end=offset+(block.text||block.caption||'').length;
    if (start<end) return block;
    offset=end+2;
  }
  return null;
}
let referenceReturn = null;
const researchDrawMap = drawMap;
drawMap = function() {
  researchDrawMap();
  const view = $('#map-viewport');
  if (!view || !state.layout || !state.article.map) return;
  const key = state.article.id+':'+state.depth+':'+state.zoom+':'+state.article.map.nodes.length;
  if (view.dataset.layout !== key) {
    view.dataset.layout = key;
    const root=state.layout.positions.get(state.article.map.root);
    if (root) view.scrollTop=Math.max(0,root.y*state.zoom-view.clientHeight/2);
  }
};
const researchMapAside = renderMapAside;
renderMapAside = function() {
  researchMapAside();
  if (state.article.map?.segments) $('#map-aside').insertAdjacentHTML('afterbegin',`<p class="modal-note">全文分 ${state.article.map.segments} 段研读 · 按原文顺序展开</p>`);
};
const researchDispatch = dispatch;
dispatch = async function(action) {
  if (action === 'map-fit') {
    const view=$('#map-viewport');
    if (view && state.layout) {
      state.zoom=Math.max(.04,Math.min(1,(view.clientWidth-20)/state.layout.width,(view.clientHeight-20)/state.layout.height));
      drawMap();
    }
    return;
  }
  if (action === 'delete-article') {
    const article = state.article;
    modal('删除资料',`<p>删除《${esc(article.title)}》及其历史版本、词语、笔记、导图与问答。此操作无法撤销。</p><div class="actions"><button class="secondary" data-action="close">保留资料</button><button class="primary" id="confirm-delete-article">删除</button></div>`);
    $('#confirm-delete-article').onclick = async () => {
      const control = $('#confirm-delete-article'); control.disabled = true;
      try {
        await api('article/delete',{article_id:article.id});
        $('#dialog').close();
        for (const key of drafts.keys()) if (key.startsWith(article.id+':')) drafts.delete(key);
        state.article=null; state.object=null; state.session={messages:[],draft:''};
        await shelf(); toast('资料已删除');
      } catch(error) {control.disabled=false;toast(error.message);}
    };
    return;
  }
  if (action === 'index-images') {
    const articleId = state.article.id;
    $('#dialog').close();
    await runJob('index-images',{article_id:articleId},async()=>toast('图片内容已识别，可以直接提问查找。'));
    return;
  }
  await researchDispatch(action);
  if (action === 'article-info') $('#dialog .actions').insertAdjacentHTML('beforeend',button('delete-article','删除资料'));
  if (action === 'article-info' && state.article.blocks.some(b=>b.type==='image')) {
    $('#dialog .actions').insertAdjacentHTML('beforeend',button('index-images','识别图文内容'));
    $('#dialog .actions').insertAdjacentHTML('afterend','<p class="modal-note">首次提问会识别相关资料中的图片，也可以提前识别。结果保存在本机，可随时停止。</p>');
  }
};
const researchRender = renderWork;
renderWork = function () {
  researchRender();
  const mapActions=document.querySelector('.map-toolbar .actions');
  if (mapActions) mapActions.insertAdjacentHTML('afterbegin',button('map-fit','适应窗口'));
  const composer = document.querySelector('.assistant-conversation .composer');
  if (composer && !state.object) {
    composer.insertAdjacentHTML('afterbegin', `<label class="research-scope">查阅范围 <select id="research-scope" aria-label="问答查阅范围"><option value="article">当前文章</option><option value="library">整个资料书架</option></select></label>`);
    const select = $('#research-scope');
    select.value = state.researchScope;
    select.disabled = Boolean(state.job);
    select.onchange = () => { state.researchScope = select.value; };
  }
  if (referenceReturn && referenceReturn.article !== state.article?.id) {
    $('#main').insertAdjacentHTML('afterbegin', '<button class="secondary" id="return-to-question">← 回到提问资料</button>');
    $('#return-to-question').onclick = async () => {
      const target = referenceReturn; referenceReturn = null;
      try { await openArticle(target.article, target.object); }
      catch (error) { toast(error.message); }
    };
  }
};

document.addEventListener('click', event => {
  const button = event.target.closest('[data-rag-cite]');
  if (!button) return;
  const message = state.session.messages[Number(button.dataset.ragMessage)];
  const source = message?.evidence?.find(e => (e.citation || e.block) === button.dataset.ragCite);
  if (!source) { toast('这条引用暂时无法打开，请重新提问。'); return; }
  const title = source.article_title || state.article.title;
  const image = source.image_sent && source.asset ? `<div class="reference-picture"><img class="reference-image" src="${asset(source.asset)}" alt="引用的原图">${source.region?`<span class="reference-region" style="left:${source.region[0]*100}%;top:${source.region[1]*100}%;width:${source.region[2]*100}%;height:${source.region[3]*100}%"></span>`:''}</div>` : '';
  modal('原文依据', `<h3>${esc(title)}</h3><p class="modal-note">${source.page ? '第 '+source.page+' 页' : '原文片段'}${source.region?' · 回答依据为所选图片局部':''}</p><blockquote class="reference-quote">${esc(source.quote)}</blockquote>${image}<div class="actions"><button class="primary" id="open-reference-source">在原文中定位</button></div>`);
  $('#open-reference-source').onclick = async () => {
    try {
      const articleId = source.article_id || state.article.id;
      $('#dialog').close();
      if (articleId !== state.article.id) {
        referenceReturn = {article:state.article.id, object:state.object?.id || null};
        await openArticle(articleId);
      }
      await locate(source.block);
      // A block can be a whole PDF page or a long imported paragraph.
      // Locate the quoted characters, rather than leaving the reader at its start.
      const paragraph = document.querySelector('#source-'+source.block+' p');
      if (paragraph && source.quote && paragraph.textContent.includes(source.quote)) {
        const text = paragraph.textContent;
        const start = Number.isInteger(source.start) && text.slice(source.start,source.end) === source.quote
          ? source.start : text.indexOf(source.quote);
        paragraph.replaceChildren(document.createTextNode(text.slice(0,start)));
        const mark = document.createElement('mark');
        mark.className = 'reference-highlight'; mark.textContent = source.quote;
        paragraph.append(mark,document.createTextNode(text.slice(start+source.quote.length)));
        mark.scrollIntoView({block:'center',behavior:'instant'});
      }
    } catch (error) { toast(error.message); }
  };
});

// Refresh shared records without replacing an in-progress selection or draft.
let sharedRefreshBusy = false;
setInterval(async () => {
  if (sharedRefreshBusy || document.hidden || state.job || $('#dialog')?.open ||
      document.activeElement?.matches('input,textarea,[contenteditable="true"]') ||
      window.getSelection()?.toString()) return;
  sharedRefreshBusy = true;
  try {
    const oldArticles = JSON.stringify(state.articles);
    const page = state.page;
    const articleId = state.article?.id;
    await refresh();
    if (page !== state.page || articleId !== state.article?.id) return;
    if (state.page === 'shelf' && oldArticles !== JSON.stringify(state.articles)) {
      await shelf();
    } else if (state.page === 'work' && state.article) {
      const key = sessionKey();
      const [article, session] = await Promise.all([api('article/'+articleId),api('session/'+encodeURIComponent(key))]);
      if (state.page !== 'work' || sessionKey() !== key || state.job ||
          document.activeElement?.matches('input,textarea,[contenteditable="true"]')) return;
      if (JSON.stringify(article.objects) !== JSON.stringify(state.article.objects) ||
          JSON.stringify(session.messages) !== JSON.stringify(state.session.messages)) {
        preserveScroll();
        state.article = article;
        state.object = article.objects.find(o=>o.id===state.object?.id) || null;
        state.session.messages = session.messages || [];
        renderWork();
      }
    }
  } catch (_) { /* Existing controls expose connection errors on interaction. */ }
  finally { sharedRefreshBusy = false; }
},3000);
