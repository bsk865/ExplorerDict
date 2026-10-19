/* Source-grounded reading: scope choice and verifiable citation previews. */
state.researchScope = 'article';
let referenceReturn = null;
const researchRender = renderWork;
renderWork = function () {
  researchRender();
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
  const image = source.image_sent && source.asset ? `<img class="reference-image" src="${asset(source.asset)}" alt="引用的原图">` : '';
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
