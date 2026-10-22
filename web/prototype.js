/* Visual adapter for the approved web prototype. Data and model calls remain shared. */
const icon=name=>`<svg viewBox="0 0 24 24" aria-hidden="true">${window.ExplorerIcons[name]||window.ExplorerIcons.file}</svg>`;
function syncPrototype(){
    document.querySelectorAll('[data-icon]').forEach(el=>el.innerHTML=icon(el.dataset.icon));
    $('#brand-mark').src='assets/'+(window.ExplorerThemes[theme()].assetPrefix||'')+'brand-mark.svg';
    document.querySelectorAll('[data-theme-image]').forEach(el=>el.src=window.ExplorerThemes[theme()][el.dataset.themeImage]);
    const activeNav=state.page==='study'?'study-'+study.page:state.page==='library'?'library':state.page==='work'||state.page==='saved'?'study-themes':'shelf';
    $('#navigation').querySelectorAll('button').forEach(el=>el.classList.toggle('active',el.dataset.action===activeNav));
}
const baseApplyTheme=applyTheme;
applyTheme=function(){baseApplyTheme();syncPrototype();};
const baseRefresh=refresh;
refresh=async function(){await baseRefresh();$('#recent').innerHTML=state.articles.slice(0,6).map(a=>`<button class="material-link" data-open="${a.id}" title="${esc(a.title)}">${icon('file')}<span class="material-title">${esc(a.title)}</span></button>`).join('');syncPrototype();};
state.bookFilter='all';state.bookList=false;
function matchesBook(a){return state.bookFilter==='all'||state.bookFilter==='web'&&a.source_url||state.bookFilter==='local'&&!a.source_url||state.bookFilter==='image'&&a.images>0;}
renderBooks=function(query=''){
    const rows=state.articles.filter(a=>matchesBook(a)&&a.title.toLowerCase().includes(query.toLowerCase()));
    $('#books').className='document-grid'+(state.bookList?' documents-list':'');
    $('#books').innerHTML=rows.map(a=>`<article class="document-card"><button class="document-open" data-open="${a.id}"><div class="document-cover" data-cover="${a.images?'image':'note'}">${a.cover?`<img src="${asset(a.cover)}" alt="">`:`<div class="mini-paper ${a.source_url?'':'mini-note'}"><span>${a.source_url?'WEB':'NOTE'}</span><i></i><i></i><i></i><b>${icon(a.images?'image':'book')}</b></div>`}<span class="document-format">${a.images?'图文':a.source_url?'网页':'文档'}</span></div><div class="document-card-body"><span class="document-topic">${a.origin==='demo'?'示例阅读':a.source_url?'网页文章':'我的资料'}</span><h3>${esc(a.title)}</h3><p>${a.chars.toLocaleString()} 字${a.images?' · '+a.images+' 张图片':''}</p><div class="document-card-bottom"><span>${a.has_map?'已有导图':'继续阅读'}</span>${icon('arrow-up-right')}</div></div></button></article>`).join('')||'<div class="knowledge-empty"><h2>这里还没有文章</h2><p>添加文件、粘贴正文，或保存一篇网页文章。</p>'+button('import','添加资料','primary')+'</div>';
    if($('#book-count'))$('#book-count').textContent=rows.length+' 份';
};
shelf=async function(){
    await saveDraft();state.page='shelf';state.article=null;state.object=null;document.body.dataset.page='shelf';await refresh();setCrumb('资料书架');
    $('#main').innerHTML=`<section class="hero"><div class="hero-title-block"><div class="eyebrow">A HOME FOR YOUR READING</div><h1>给好奇，一处安放。</h1><p>把散落的资料收进书架，让每一次理解都有来处。</p><small class="hero-theme-note">${esc(window.ExplorerThemes[theme()].elements)}</small></div><img class="hero-art" data-theme-image="scene" src="${window.ExplorerThemes[theme()].scene}" alt=""><button class="primary" data-action="import">${icon('plus')} 添加资料</button></section><div class="shelf-summary"><span>${icon('archive')}<strong>${state.articles.length}</strong>份资料</span><button class="text-button" data-action="url-import">${icon('link')}保存网页</button><button class="text-button" data-action="paste">${icon('note')}粘贴正文</button><button class="text-button" data-action="external-mode">${icon('arrow-up-right')}在原文章中阅读</button></div><div class="shelf-layout"><aside class="topic-sidebar"><div class="topic-title">我的资料</div>${[['all','全部资料','archive'],['web','网页文章','link'],['local','本机资料','file'],['image','含图文章','image']].map(([id,label,i])=>`<button class="topic-item ${state.bookFilter===id?'active':''}" data-book-filter="${id}">${icon(i)}${label}</button>`).join('')}<div class="shelf-quiet-note"><img data-theme-image="avatar" src="${avatar()}" alt="小探"><p>不必一次读完。<br>把想读的，先留下。</p></div></aside><section class="shelf-main"><div class="shelf-tools"><label class="book-search">${icon('search')}<input id="book-search" placeholder="搜索文章标题" aria-label="搜索文章标题"></label><div class="view-options"><button class="icon-button ${!state.bookList?'active':''}" data-action="books-grid" aria-label="卡片视图">${icon('grid')}</button><button class="icon-button ${state.bookList?'active':''}" data-action="books-list" aria-label="列表视图">${icon('list')}</button></div></div><div class="shelf-scope"><h2>我的文章</h2><span id="book-count"></span></div><div id="books"></div></section></div>`;
    renderBooks($('#global-search').value);$('#book-search').value=$('#global-search').value;$('#book-search').oninput=e=>renderBooks(e.target.value);syncPrototype();
};

const functionalRender=renderWork;
renderWork=function(){
    functionalRender();
    $('#main').classList.add('prototype-reading');
    const toolbar=$('.reading-toolbar');
    if(toolbar)toolbar.insertAdjacentHTML('afterend',`<div class="document-title-row"><div><h1>${esc(state.article.title)}</h1><p>${state.article.source_url?'网页文章':'我的资料'} · ${state.article.blocks.filter(b=>b.type==='image').length} 张图片</p></div><button class="secondary" data-action="article-info">${icon('settings')}资料信息</button></div>`);
    if(state.view!=='map'){
        const heading=$('.assistant-heading');if(heading)heading.innerHTML=`<img src="${avatar()}" alt="小探"><div><h2>小探，陪你读懂</h2><span>${esc(state.object?'当前内容':'文章问答')}</span></div><button class="icon-button" data-action="assistant-toggle" aria-label="收起小探">${icon('x')}</button>`;
        const controls=$('.assistant-controls');if(controls)controls.insertAdjacentHTML('beforeend',`<button class="object-browser-button" data-action="objects">${icon('grid')}探索记录 <span>${state.article.objects.length}</span>${icon('arrow-right')}</button>`);
        const conversation=$('.quiet-prompt');
        if(conversation&&!state.object)conversation.innerHTML=`<div class="assistant-welcome"><img class="assistant-welcome-portrait" data-theme-image="portrait" src="${window.ExplorerThemes[theme()].portrait}" alt="小探"><h3>从一个问题开始。</h3><p>先把问题留在资料旁，<br>再回到原文里找理解。</p><div class="sample-suggestions">${['这篇文章的核心观点是什么？','有哪些值得关注的概念？'].map(t=>`<button data-suggest="${t}">${t}${icon('arrow-up-right')}</button>`).join('')}</div></div>`;
        if($('.reading-desk'))$('.reading-desk').insertAdjacentHTML('beforebegin',`<div class="workspace-mobile-tabs"><button data-action="mobile-read" class="${!state.assistant?'active':''}">${icon('book')}原文</button><button data-action="objects">${icon('grid')}探索</button><button data-action="mobile-chat" class="${state.assistant?'active':''}">${icon('spark')}小探问答</button></div>`);
    }
    syncPrototype();
};
const functionalShelfSaved=savedPage;
savedPage=async function(){await functionalShelfSaved();const title=state.collectionKind==='term'?'概念收藏':'回顾与理解';setCrumb(title);if($('.page-heading h1'))$('.page-heading h1').textContent=title;syncPrototype();};
const functionalOpen=openArticle;
openArticle=async function(id,oid=null){await functionalOpen(id,oid);if(!oid&&matchMedia('(min-width:1000px)').matches){state.assistant=true;renderWork();}localStorage.setItem('reading-last-article',id);};
const functionalLocate=locate;
locate=async function(block){if(matchMedia('(max-width:720px)').matches)state.assistant=false;await functionalLocate(block);};
function showObjects(filter='all'){
    const rows=state.article.objects.filter(o=>filter==='all'||filter==='term'&&o.type==='term'||filter==='image'&&o.type!=='term');
    modal('探索记录',`<div class="object-filter">${[['all','全部'],['term','关键词'],['image','图片']].map(([id,t])=>`<button data-object-filter="${id}" class="${id===filter?'active':''}">${t}</button>`).join('')}</div><div class="exploration-list">${rows.map(o=>`<button class="exploration-item" data-explore="${o.id}">${o.asset?`<img src="${asset(o.asset)}" alt="">`:icon('concept')}<span><strong>${esc(o.title)}</strong><small>${o.type==='term'?'关键词':o.type==='region'?'图片局部':'整张图片'}${o.saved?' · 已收藏':''}</small></span>${icon('arrow-right')}</button>`).join('')||'<p class="modal-note">在原文中选词或点图，开始探索。</p>'}</div>`);
}
const functionalDispatch=dispatch;
dispatch=async function(action){
    if(action==='objects'){await saveDraft();showObjects();return;}
    if(action==='mobile-read'){await saveDraft();state.assistant=false;state.view='explore';renderWork();return;}
    if(action==='mobile-chat'){await saveDraft();state.assistant=true;state.assistantTab='understand';state.view='explore';renderWork();return;}
    if(action==='concepts'){state.collectionKind='term';await savedPage();return;}
    if(action==='saved')state.collectionKind='all';
    if(action==='resume-reading'){const id=state.article?.id||localStorage.getItem('reading-last-article');if(id&&state.articles.some(a=>a.id===id)){await openArticle(id);return;}await shelf();toast('选择一篇文章开始阅读');return;}
    if(action==='books-grid'||action==='books-list'){state.bookList=action==='books-list';renderBooks($('#book-search').value);document.querySelectorAll('.view-options button').forEach(b=>b.classList.toggle('active',b.dataset.action===action));return;}
    if(action==='character'){modal('小探',`<div class="xiaotan-character-layout"><figure class="xiaotan-portrait-frame"><img src="${window.ExplorerThemes[theme()].portrait}" alt="小探"></figure><div class="xiaotan-character-copy"><h3>陪你多理解一点。</h3><p>从一个词、一张图出发，<br>把问题放回原文里。</p>${button('settings','换一个阅读主题','primary')}</div></div>`);return;}
    if(action==='article-info'){modal('资料信息',`<h3>${esc(state.article.title)}</h3><p class="modal-note">${state.article.text.length.toLocaleString()} 字 · ${state.article.blocks.filter(b=>b.type==='image').length} 张图片</p><div class="actions">${sourceLink()}${button('export','导出笔记')}${button('append','插入图片')}${button('append-text','补充正文')}</div>`);return;}
    return functionalDispatch(action);
};
document.addEventListener('click',async e=>{const el=e.target.closest('[data-book-filter],[data-object-filter],[data-explore]');if(!el)return;try{
    if(el.dataset.bookFilter){state.bookFilter=el.dataset.bookFilter;document.querySelectorAll('[data-book-filter]').forEach(b=>b.classList.toggle('active',b===el));renderBooks($('#book-search').value);}
    if(el.dataset.objectFilter)showObjects(el.dataset.objectFilter);
    if(el.dataset.explore){$('#dialog').close();await chooseObject(el.dataset.explore);}
}catch(error){toast(error.message);}});
window.addEventListener('DOMContentLoaded',()=>{
    syncPrototype();$('#global-search').addEventListener('input',async e=>{if(state.page!=='shelf')await shelf();else renderBooks(e.target.value);});
    document.addEventListener('keydown',e=>{if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='k'){e.preventDefault();$('#global-search').focus();}});
});

const prototypeDrawMap=drawMap;drawMap=function(){prototypeDrawMap();const svg=$('#map-svg');if(svg&&state.article.map&&state.layout){svg.style.width=state.layout.width*state.zoom+'px';svg.style.height=state.layout.height*state.zoom+'px';}};
