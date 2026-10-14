/* Reading actions, durable notes, and recoverable conversation state. */
const noteWrites = new Map();
const activity = new Map();
let noteTimer;
const noteKey = () => 'reading-note:' + sessionKey();

async function persistNote() {
    clearTimeout(noteTimer);
    const input = $('#object-note');
    if (!input || !state.article) return;
    const key = input.dataset.key, oid = input.dataset.object;
    const value = input.value, storageKey = 'reading-note:' + key;
    localStorage.setItem(storageKey, value);
    const last = noteWrites.get(key) || Promise.resolve();
    const write = last.catch(()=>{}).then(async () => {
        if (oid) {
            const obj = await api('object/update', {id:oid,note:value});
            if (state.object?.id === oid) state.object = obj;
            if (state.article) state.article.objects = state.article.objects.map(o=>o.id===oid?obj:o);
        } else {
            await api('session', {key,note:value});
            if (state.article && sessionKey()===key) state.session.note=value;
        }
        if (localStorage.getItem(storageKey)===value) localStorage.removeItem(storageKey);
        if ($('#object-note')?.dataset.key===key && $('#object-note').value===value) $('#note-status').textContent='已保存';
    });
    noteWrites.set(key,write);
    try { await write; }
    catch (error) { if ($('#note-status')) $('#note-status').textContent='未保存，请重试'; throw error; }
}
const saveQuestionDraft=saveDraft;
saveDraft=async function(){await persistNote();await saveQuestionDraft();};
const renderReading=renderWork;
renderWork=function(){
    renderReading();
    const input=$('#object-note');
    if(input){
        const cached=localStorage.getItem('reading-note:'+input.dataset.key);
        if(cached!==null){input.value=cached;$('#note-status').textContent='待保存';}
        input.addEventListener('input',()=>{localStorage.setItem('reading-note:'+input.dataset.key,input.value);$('#note-status').textContent='正在保存…';clearTimeout(noteTimer);noteTimer=setTimeout(()=>persistNote().catch(e=>toast(e.message)),500);});
    }
};
window.addEventListener('beforeunload',()=>{
    const input=$('#object-note');if(!input)return;
    localStorage.setItem('reading-note:'+input.dataset.key,input.value);
    const body=input.dataset.object?{id:input.dataset.object,note:input.value}:{key:input.dataset.key,note:input.value};
    navigator.sendBeacon('/api/'+(input.dataset.object?'object/update':'session'),new Blob([JSON.stringify(body)],{type:'application/json'}));
});

const readingChat=chatHTML;
chatHTML=function(){
    if(state.assistantTab==='notes')return `<div class="assistant-notes"><h3>${esc(state.object?.title||'文章笔记')}</h3><textarea id="object-note" aria-label="我的笔记" data-key="${esc(sessionKey())}" data-object="${state.object?.id||''}" maxlength="10000" placeholder="记下你的理解…">${esc(state.object?.note??state.session.note??'')}</textarea><div class="note-footer"><small id="note-status" role="status">自动保存</small>${button('save-note','保存')}${state.object?button('save-object',state.object.saved?'取消收藏':'收藏'):''}</div></div>`;
    let html=readingChat();
    const job=activity.get(sessionKey());
    if(job)html=html.replace('<div class="composer">',`<div class="conversation-status" role="status">${job.pending?'正在回答… '+button('cancel','停止'):`${esc(job.error)}<div>${button('retry-answer','重试')}${button('settings','检查设置')}${button('dismiss-error','关闭')}</div>`}</div><div class="composer">`);
    if(job?.pending)html=html.replace('data-action="send"','disabled data-action="send"');
    return html;
};

messagesHTML=function(){
    return (state.session.messages||[]).map(m=>{
        let content=m.content;
        if(m.role==='user'&&/^请结合本文，解释当前(?:关键词|图片选区|整张图片)。请引用原文依据，区分补充理解与不确定之处。$/.test(content))content='解释这一处';
        if(m.role==='assistant')content=content.replace(/\[b\d+\]/g,'');
        const refs=(m.evidence||[]).filter(e=>m.content.includes('['+e.block+']'));
        return `<div class="message ${m.role}">${m.role==='assistant'?'<small>小探</small>':''}${esc(content).replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>')}${refs.length?`<div class="citations">${refs.map((e,i)=>`<button data-cite="${e.block}" title="${esc(e.quote)}">原文 ${i+1}</button>`).join('')}</div>`:''}</div>`;
    }).join('');
};
const sendQuestion=send;
send=async function(question){
    const text=question||$('#question')?.value.trim();
    if(!text){$('#question')?.focus();return;}
    if(state.job||activity.get(sessionKey())?.pending) {toast('请等待当前回答完成，或点击停止。');return;}
    if(!state.config.has_key){settings();toast('先填写模型设置，再开始提问。');return;}
    if(state.object?.type&&state.object.type!=='term'&&state.config.vision?.status!=='supported'){settings();toast('请先检测图片理解，完成后返回图片提问。');return;}
    await saveDraft();
    const key=sessionKey();activity.set(key,{pending:true,question:text});renderWork();
    try {await sendQuestion(text);activity.delete(key);}
    catch(e){activity.set(key,{pending:false,question:text,error:e.message.includes('已停止')?'已停止回答。':e.message});}
    finally{if(state.article&&state.page==='work'&&sessionKey()===key){await persistNote();await loadSession();renderWork();}}
};

savedPage=async function(){
    await saveDraft();document.body.dataset.page='shelf';state.page='saved';setCrumb('笔记与收藏');
    const rows=(await api('saved')).filter(o=>state.collectionKind!=='term'||o.type==='term');
    $('#main').innerHTML=`<div class="page-heading"><h1>笔记与收藏</h1>${button('shelf','我的文章')}</div><div class="filter-row"><input id="note-search" aria-label="搜索笔记" placeholder="搜索笔记或文章"></div><div id="saved-items"></div>`;
    const paint=query=>{$('#saved-items').innerHTML=rows.filter(o=>(o.title+' '+o.note+' '+o.article_title).toLowerCase().includes(query.toLowerCase())).map(o=>`<div class="saved-row">${o.asset?`<img src="${asset(o.asset)}" alt="">`:`<span class="book-symbol">${o.type==='article'?'▤':'☆'}</span>`}<div><h3>${esc(o.title)}</h3><small>${esc(o.article_title)}${o.archived?' · 旧版原文':''}</small><p>${esc(o.note||'已收藏')}</p></div><div class="actions">${o.type==='article'?`<button data-article-note="${o.article_id}">打开笔记</button>`:`<button data-review="${o.id}" data-article="${o.article_id}">${o.understood?'✓ 已理解':'标记已理解'}</button><button data-return="${o.id}" data-article="${o.article_id}">回到原文</button>`}</div></div>`).join('')||'<div class="empty">暂无笔记或收藏。阅读时可以随手记录。</div>';};
    paint('');$('#note-search').oninput=e=>paint(e.target.value);
};
document.addEventListener('click',async e=>{const el=e.target.closest('[data-article-note]');if(!el)return;try{await openArticle(el.dataset.articleNote);state.assistant=true;state.assistantTab='notes';renderWork();}catch(error){toast(error.message);}});

const readingMap=mapHTML;
mapHTML=function(){
    if(state.article.text.length>30000&&!state.article.map){
        const headings=state.article.blocks.filter(b=>b.heading>1);
        return `<section class="article-outline"><h2>文章目录</h2><p>这篇文章较长，暂不支持全文导图。可从目录跳到对应章节。</p>${headings.length?headings.map(b=>`<button data-cite="${b.id}" style="padding-left:${16+Math.min(b.heading-2,3)*18}px">${esc(b.text)}</button>`).join(''):button('reader-source','返回原文')}</section>`;
    }
    if(!state.article.map)return `<section class="map-empty"><h2>梳理文章脉络</h2><p>生成导图后，点击节点可回到原文。</p>${button('generate','生成导图','primary')}</section>`;
    return readingMap().replace(/<p class="map-foot">[\s\S]*?<\/p>/,'<p class="map-foot">点击节点查看原文 · 拖动空白移动 · Ctrl + 滚轮缩放</p>').replace('已通过模型复核 · 仍需结合原文判断','文章导图').replace('自编示例图 · 用于体验布局','示例导图');
};
const drawReadingMap=drawMap;
drawMap=function(){if($('#map-svg'))drawReadingMap();};

const readingDispatch=dispatch;
dispatch=async function(action){
    if(action==='notes-tab')state.assistant=true;
    if(action==='selection'){await readingDispatch(action);if(state.object)await send('解释这个词');return;}
    if(action==='save-note'){await persistNote();toast('笔记已保存');return;}
    if(action==='explain'){await send(state.object?.type==='term'?'解释这个词':state.object?.type==='region'?'解释这处图片':'解释这张图片');return;}
    if(action==='retry-answer'){const item=activity.get(sessionKey());if(item)await send(item.question);return;}
    if(action==='dismiss-error'){activity.delete(sessionKey());renderWork();return;}
    if(action==='detect'){
        const base=$('#api-base'),model=$('#api-model'),key=$('#api-key');
        if(base&&(base.value!==state.config.base_url||model.value!==state.config.model||key.value)){
            state.config=await api('settings',{base_url:base.value,model:model.value,key:key.value});
            await refresh();
        }
    }
    if(action==='generate'&&!state.config.has_key){settings();toast('先填写模型设置，再生成导图。');return;}
    if(action==='external-mode'){await readingDispatch('legacy');return;}
    if(action==='capture-url'){
        const input=$('#source-url');try{const u=new URL(input.value);if(!['http:','https:'].includes(u.protocol))throw Error();}catch{input.setCustomValidity('请输入完整的网页地址');input.reportValidity();input.oninput=()=>input.setCustomValidity('');return;}
    }
    // Save notes before actions that replace the current surface.
    if(['save-object','mapview','reader-source','assistant-toggle','notes-tab','understand-tab','export'].includes(action))await persistNote();
    return readingDispatch(action);
};
