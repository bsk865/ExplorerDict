/* Reading is the workspace. One optional assistant, one source of truth. */
state.assistant=false;
state.assistantTab='understand';
const previousShelf=shelf;
shelf=async function(){document.body.dataset.page='shelf';await previousShelf();const welcome=$('.welcome');if(welcome)welcome.outerHTML=`<section class="reading-choice"><div><h2>从原文开始</h2><p>打开一篇文章，边读边问，随手记下理解。</p></div><div class="actions">${button('external-mode','在原文章中阅读')}${button('url-import','提取网页文章','primary')}</div></section>`;};

function sourceHasMarkupProblem(a){return a.source_status!=='readable'&&!a.blocks.some(b=>b.html)&&/\\(?:frac|min|max|theta|lambda|mathcal|begin|end|left|right|in|sum|xi)\b/.test(a.text);}
function sourceLink(){const url=state.article?.source_url||'';return /^https?:\/\//.test(url)?`<a class="button-link" href="${esc(url)}" target="_blank" rel="noopener noreferrer">在原网页阅读 ↗</a>`:'';}

renderWork=function(){
    preserveScroll();document.body.dataset.page='work';const a=state.article;
    setCrumb(a.title);
    $('#main').innerHTML=`<div class="reading-toolbar"><div class="actions">${button('shelf','‹ 我的文章')}<span class="reading-mode">${state.view==='map'?'文章导图':'原文工作台'}</span></div><div class="actions">${sourceLink()}${button('reader-source','原文',state.view!=='map'?'active':'')}${button('mapview','导图',state.view==='map'?'active':'')}${button('notes-tab','笔记')}${button('assistant-toggle',state.assistant?'收起小探':'打开小探')}</div></div>${state.view==='map'?`<h1 class="map-article-title">${esc(a.title)}</h1>${mapHTML()}`:workspaceHTML()}`;
    if(state.view==='map'){drawMap();return;}
    wireReader();wireImage();wireChat();
    const picker=$('#object-picker');if(picker)picker.onchange=()=>chooseObject(picker.value||null).catch(e=>toast(e.message));
    const tab=$('#assistant-tab');if(tab)tab.onchange=async()=>{await saveDraft();state.assistantTab=tab.value;renderWork();};
};

workspaceHTML=function(){return `<div class="reading-desk ${state.assistant?'with-assistant':''}">${sourceHTML()}${state.assistant?`<aside class="assistant-drawer"><div class="assistant-heading"><img src="${avatar()}" alt="小探"><strong>小探</strong><span>阅读助手</span>${button('assistant-toggle','×')}</div><div class="assistant-controls"><label for="object-picker">当前内容</label><select id="object-picker" aria-label="选择当前理解的内容"><option value="">文章问答</option>${state.article.objects.map(o=>`<option value="${o.id}" ${state.object?.id===o.id?'selected':''}>${o.type==='term'?'词语':o.type==='region'?'局部':'图片'} · ${esc(o.title)}${o.saved?' ☆':''}</option>`).join('')}</select><div class="assistant-tab-row"><button data-action="understand-tab" class="${state.assistantTab==='understand'?'active':''}">理解与追问</button><button data-action="notes-tab" class="${state.assistantTab==='notes'?'active':''}">我的笔记</button></div></div>${chatHTML()}</aside>`:''}</div>`;};

sourceHTML=function(){const a=state.article,broken=sourceHasMarkupProblem(a);return `<section class="reading-paper"><div class="paper-tools"><span>${a.source_status==='readable'?'原文':a.origin==='legacy'?'历史提取内容':a.origin==='demo'?'自编示例':'本机文章'}</span><details><summary>文章操作</summary><div class="paper-menu">${button('append','插入图片')}${button('append-text','补充正文')}${button('export','导出笔记')}${a.previous_id?`<button data-open="${a.previous_id}">查看旧收藏版本</button>`:''}</div></details></div><div class="source-body" id="source-body"><header class="paper-title"><h1>${esc(a.title)}</h1>${a.source_url&&/^https?:/.test(a.source_url)?`<div class="paper-origin">${esc(new URL(a.source_url).hostname)} · 原文阅读</div>`:''}</header>${broken?`<div class="source-repair"><h2>原文需要重新提取</h2><p>此版本的文字和公式不完整。重新提取后，旧笔记仍会保留。</p><div class="actions">${a.source_url?button('reextract','从原网页重新提取','primary'):button('url-import','提供原网页地址','primary')}${sourceLink()}</div></div>`:a.blocks.map((b,i)=>{
        if(b.heading===1&&b.text.replace(/\s+/g,' ').trim()===a.title.replace(/\s+/g,' ').trim())return '';
        const body=b.type==='text'?(b.html?`<div class="article-rich">${b.html}</div>`:`<p>${esc(b.text)}</p>`):`<figure><button class="source-image" data-image-block="${b.id}" aria-label="探索${esc(b.caption||'图片')}" style="border:0;padding:0;position:relative"><img src="${asset(b.asset)}" alt="${esc(b.caption||'文章图片')}" loading="lazy">${state.object?.block===b.id&&state.object?.region?`<span class="region" style="left:${state.object.region[0]*100}%;top:${state.object.region[1]*100}%;width:${state.object.region[2]*100}%;height:${state.object.region[3]*100}%"></span>`:''}</button><figcaption>${esc(b.caption||'')}${b.page?' · 第 '+b.page+' 页':''}</figcaption></figure>`;
        return `<div class="source-block ${b.heading?'heading-block':''}" id="source-${b.id}" data-block="${b.id}">${body}<button class="insert" data-insert="${i}">＋ 在此后插入图片</button></div>`;
    }).join('')}</div><div class="selection-bar" id="selection-bar" hidden><span id="selected-term"></span>${button('selection','理解这一处','primary')}${button('dismiss-selection','×')}</div></section>`;};

function selectedQuote(){const o=state.object;if(!o)return '';const b=state.article.blocks.find(b=>b.id===o.block);if(!b)return '';if(sourceHasMarkupProblem(state.article))return '旧引用的公式格式有误，请回原网页核对。';const t=b.text||b.caption||'',at=Math.max(0,t.indexOf(o.title));return (at>70?'…':'')+t.slice(Math.max(0,at-70),at+o.title.length+100)+(t.length>at+o.title.length+100?'…':'');}

detailHTML=function(){const o=state.object;if(!o)return `<div class="current-focus"><p>在原文里选一个词，或点一张图。也可以直接提问。</p></div>`;return `<div class="current-focus"><div class="focus-title"><h2>${esc(o.title)}</h2>${button('save-object',o.saved?'★':'☆')}</div>${o.type!=='term'?`<div class="image-scroll"><div class="image-stage" id="image-stage" style="width:${state.imageZoom*100}%"><img id="object-image" src="${asset(o.asset)}" alt="${esc(o.title)}"><div id="region" class="region" hidden></div></div></div><div class="image-tools"><span>拖动图片选择局部</span>${button('zoom-image-out','−')}${button('zoom-image-in','＋')}</div><div class="detail-actions">${button('region-object','理解选区')}${button('clear-region','整张图')}</div>`:''}<details class="focus-evidence"><summary>原文依据</summary><p>${esc(selectedQuote())}</p>${button('locate','在原文定位')}</details></div>`;};

chatHTML=function(){if(state.assistantTab==='notes')return `<div class="assistant-notes">${state.object?`<h3>${esc(state.object.title)}</h3><label class="form-field">我的理解<textarea id="object-note" placeholder="用自己的话，留下理解…">${esc(state.object.note||'')}</textarea></label><div class="actions">${button('save-note','保存笔记','primary')}${button('save-object',state.object.saved?'取消收藏':'收藏')}</div>`:'<p class="muted">先在原文选择一个词或一张图，再为它留下笔记。</p>'}</div>`;return `<div class="assistant-conversation">${detailHTML()}<div class="chat-body" id="chat-body">${(state.session.messages||[]).length?messagesHTML():`<div class="quiet-prompt"><p>${state.object?'':''}</p>${state.object?button('explain',state.object.type==='term'?'解释这个词':state.object.type==='region'?'解释选区':'结合本文解释图片','primary'):''}</div>`}</div><div class="composer"><textarea id="question" aria-label="向小探提问" placeholder="${state.object?'继续问这一处…':'围绕这篇文章提问…'}">${esc(state.session.draft||'')}</textarea><div class="actions"><small>${esc(state.object?.title||'文章问答')}</small>${button('send','发送','primary')}</div></div></div>`;};

const previousChoose=chooseObject;
chooseObject=async function(id){state.assistant=true;state.assistantTab='understand';await previousChoose(id);};
const previousOpen=openArticle;
openArticle=async function(id,oid=null){state.assistant=!!oid;state.assistantTab='understand';await previousOpen(id,oid);};
const previousDispatch=dispatch;
dispatch=async function(action){
    if(action==='assistant-toggle'){if(state.view==='map')state.view='explore';await saveDraft();state.assistant=!state.assistant;renderWork();return;}
    if(action==='understand-tab'||action==='notes-tab'){await saveDraft();state.assistantTab=action==='notes-tab'?'notes':'understand';renderWork();return;}
    if(action==='reader-source'){await saveDraft();state.view='explore';renderWork();return;}
    if(action==='dismiss-selection'){$('#selection-bar').hidden=true;window.getSelection()?.removeAllRanges();return;}
    if(action==='external-mode'){modal('在原文章中阅读',`<p>留在你正在阅读的网页、PDF 或文档里，用浮动取词助手选择内容、查看解释和继续追问。</p><p class="modal-note">需要把原文带进应用时，再选择「提取网页文章」或导入原文件；不必同时开着两套工作台。</p><div class="actions">${button('legacy','打开取词助手','primary')}${button('close','继续留在这里')}</div>`);return;}
    if(action==='url-import'){modal('把原文带进工作台',`<p class="modal-note">输入网页地址，将文章保存到这里。</p><label class="form-field">文章原网页地址<input id="source-url" type="url" placeholder="https://…"></label><div class="actions">${button('close','取消')}${button('capture-url','提取原文','primary')}</div>`);return;}
    if(action==='capture-url'||action==='reextract'){
        const btn=document.querySelector(`[data-action="${action}"]`);if(btn){btn.disabled=true;btn.textContent='正在保存原文和图片…';}
        try{const a=await api('extract',action==='reextract'?{article_id:state.article.id}:{url:$('#source-url').value.trim()});$('#dialog').close();await refresh();await openArticle(a.id);toast('文章已保存');}finally{if(btn){btn.disabled=false;btn.textContent='提取原文';}}return;
    }
    if(action==='legacy'){await api('legacy',{});$('#dialog').close();toast('取词助手已打开');return;}
    return previousDispatch(action);
};

const previousSaved=savedPage; savedPage=async function(){document.body.dataset.page="shelf";await previousSaved();};

const originalMapHTML=mapHTML;
mapHTML=function(){return originalMapHTML();};
const originalChatHTML=chatHTML;
chatHTML=function(){let html=originalChatHTML();if(!state.object&&state.assistantTab==='understand'&&state.article.text.length>26000)html=html.replace('<div class="chat-body"','<p class="context-limit">长文提问建议先选中相关段落，回答会更准确。</p><div class="chat-body"');return html;};
