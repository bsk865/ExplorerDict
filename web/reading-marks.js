/* Anchors use Unicode code points; DOM ranges use UTF-16 offsets. */
const readingMarks = {article:null, items:[], selection:null, loading:false, saving:false};
const markLabels = {yellow:'黄色荧光',green:'绿色荧光',pink:'粉色荧光',underline:'下划线'};
contentKinds.annotations='原文标注';
contentDeleteScope.annotations='只删除这处荧光或下划线，文章原文和笔记保留。';
function markRoot(block){return block.querySelector('.article-rich')||block.querySelector(':scope > p');}
function markTextNodes(root){const walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT),nodes=[];while(walker.nextNode())nodes.push(walker.currentNode);return nodes;}
function positionMarkToolbar(range){
  const bar=$('#selection-bar');if(!bar)return;
  const rect=range.getBoundingClientRect(),width=bar.offsetWidth,height=bar.offsetHeight;
  const left=Math.max(8,Math.min(rect.left+(rect.width-width)/2,innerWidth-width-8));
  let top=rect.bottom+10;
  if(top+height>innerHeight-8)top=rect.top-height-10;
  bar.style.left=left+'px';bar.style.top=Math.max(8,top)+'px';
}
function markStyleButtons(item){return Object.entries(markLabels).map(([style,label])=>`<button class="mark-tool ${style==='underline'?'':'mark-swatch mark-'+style}" data-mark-edit="${item.id}" data-style="${style}" aria-label="改为${label}" title="改为${label}" aria-pressed="${item.style===style}">${style==='underline'?'<u>U</u>':''}</button>`).join('');}
function captureMarkSelection(source){
  readingMarks.selection=null;
  const selection=window.getSelection();
  if(!selection?.rangeCount||selection.isCollapsed){$('#selection-bar').hidden=true;return;}
  const range=selection.getRangeAt(0);
  if(!source.contains(range.startContainer)||!source.contains(range.endContainer))return;
  const segments=[];
  source.querySelectorAll('[data-block]').forEach(block=>{
    const root=markRoot(block);if(!root||!range.intersectsNode(root))return;
    let offset=0,start=null,end=null;
    for(const node of markTextNodes(root)){
      if(range.intersectsNode(node)){
        const from=node===range.startContainer?range.startOffset:0,to=node===range.endContainer?range.endOffset:node.length;
        if(to>from){if(start===null)start=offset+from;end=offset+to;}
      }
      offset+=node.length;
    }
    if(start!==null&&end>start){const text=root.textContent;segments.push({block:block.dataset.block,start:Array.from(text.slice(0,start)).length,end:Array.from(text.slice(0,end)).length,quote:text.slice(start,end)});}
  });
  if(!segments.length)return;
  readingMarks.selection={article_id:state.article.id,version:state.article.version,segments};
  const bar=$('#selection-bar');bar.hidden=false;
  bar.querySelectorAll('[data-mark-style]').forEach(el=>el.setAttribute('aria-pressed',String(readingMarks.items.some(m=>m.style===el.dataset.markStyle&&JSON.stringify(m.segments)===JSON.stringify(segments)))));
  $('#selected-term').textContent=segments.map(s=>s.quote).join(' ').slice(0,70);
  if(segments.length>1){bar.querySelector('[data-action="selection"]').hidden=true;bar.querySelector('[data-action="study-note-selection"]').hidden=true;}
  else{bar.querySelector('[data-action="study-note-selection"]').hidden=false;}
  positionMarkToolbar(range);
}
function paintReadingMarks(source){
  source.querySelectorAll('[data-reading-mark]').forEach(el=>el.replaceWith(...el.childNodes));
  source.normalize();
  for(const block of source.querySelectorAll('[data-block]')){
    const root=markRoot(block);if(!root)continue;
    const points=Array.from(root.textContent),ranges=[];
    for(const item of readingMarks.items)for(const s of item.segments){
      if(s.block===block.dataset.block&&points.slice(s.start,s.end).join('')===s.quote)ranges.push({start:points.slice(0,s.start).join('').length,end:points.slice(0,s.end).join('').length,item});
    }
    if(!ranges.length)continue;
    let offset=0;
    for(const node of markTextNodes(root)){
      const start=offset,end=offset+node.length;offset=end;
      const overlaps=ranges.filter(r=>r.start<end&&r.end>start);if(!overlaps.length)continue;
      const cuts=[...new Set([start,end,...overlaps.flatMap(r=>[Math.max(start,r.start),Math.min(end,r.end)])])].sort((a,b)=>a-b),fragment=document.createDocumentFragment();
      for(let i=0;i<cuts.length-1;i++){
        const active=overlaps.filter(r=>r.start<=cuts[i]&&r.end>=cuts[i+1]),text=node.data.slice(cuts[i]-start,cuts[i+1]-start);
        if(!active.length){fragment.append(document.createTextNode(text));continue;}
        const span=document.createElement('span');span.dataset.readingMark=active.map(r=>r.item.id).join(' ');
        const color=active.filter(r=>r.item.style!=='underline').at(-1)?.item.style;
        span.className='reading-mark'+(color?' mark-'+color:'')+(active.some(r=>r.item.style==='underline')?' mark-underline':'');
        span.textContent=text;span.title='查看或删除标注';fragment.append(span);
      }
      node.replaceWith(fragment);
    }
  }
  const button=$('#reading-marks-list');if(button)button.textContent=`标注 ${readingMarks.items.length}`;
}
async function fetchReadingMarks(source,article){
  readingMarks.loading=true;
  try{
    const result=await api('reading-annotations/'+article.id);
    if(!source.isConnected||state.article?.id!==article.id)return;
    readingMarks.article=article.id;readingMarks.items=result.items;paintReadingMarks(source);
  }catch(err){toast('标注加载失败：'+err.message);}
  finally{if(source.isConnected)readingMarks.loading=false;}
}
function showReadingMarks(ids=null){
  const rows=readingMarks.items.filter(item=>!ids||ids.includes(item.id));
  modal('原文标注',`<div class="reading-mark-list">${rows.map(item=>`<article><span class="mark-chip mark-${item.style}">${markLabels[item.style]}</span><blockquote>${esc(item.segments.map(s=>s.quote).join('\n'))}</blockquote><div class="actions"><button data-mark-locate="${item.id}">定位原文</button><button data-mark-delete="${item.id}" title="删除标注" aria-label="删除标注">${studyIcon('trash')}</button></div></article>`).join('')||'<p class="empty">还没有标注。</p>'}</div>${button('close','关闭')}`);
  document.querySelectorAll('[data-mark-locate]').forEach(el=>el.onclick=()=>{$('#dialog').close();$('#source-body')?.querySelector(`[data-reading-mark~="${el.dataset.markLocate}"]`)?.scrollIntoView({block:'center',behavior:'smooth'});});
  document.querySelectorAll('.reading-mark-list article').forEach((el,i)=>el.querySelector('.actions').insertAdjacentHTML('afterbegin',`<div class="mark-edit-styles" role="group" aria-label="修改标注样式">${markStyleButtons(rows[i])}</div>`));
  document.querySelectorAll('[data-mark-edit]').forEach(el=>el.onclick=async()=>{
    const old=readingMarks.items.find(m=>m.id===el.dataset.markEdit);if(!old)return;
    const buttons=[...el.closest('article').querySelectorAll('button')];buttons.forEach(b=>b.disabled=true);
    try{const item=await api('reading-annotation',{...old,style:el.dataset.style});readingMarks.items=readingMarks.items.map(m=>m.id===item.id?item:m);const source=$('#source-body');if(source)paintReadingMarks(source);showReadingMarks(ids);toast('标注样式已更新');}catch(err){buttons.forEach(b=>b.disabled=false);toast(err.message);}
  });
  document.querySelectorAll('[data-mark-delete]').forEach(el=>el.onclick=async()=>{
    el.disabled=true;try{await api('reading-annotation/delete',{article_id:readingMarks.article,id:el.dataset.markDelete});readingMarks.items=readingMarks.items.filter(m=>m.id!==el.dataset.markDelete);const source=$('#source-body');if(source)paintReadingMarks(source);showReadingMarks(ids);toast('标注已删除');}catch(err){el.disabled=false;toast(err.message);}
  });
}
const marksRenderWork=renderWork;
renderWork=function(){
  marksRenderWork();const source=$('#source-body'),bar=$('#selection-bar');if(!source||!bar)return;
  readingMarks.selection=null;readingMarks.items=[];readingMarks.article=state.article.id;
  bar.insertAdjacentHTML('beforeend',`<div class="mark-tools" role="group" aria-label="原文标注">${Object.entries(markLabels).map(([style,label])=>`<button class="mark-tool ${style==='underline'?'':'mark-swatch mark-'+style}" data-mark-style="${style}" aria-label="${label}" title="${label}">${style==='underline'?'<u>U</u>':''}</button>`).join('')}</div>`);
  for(const [action,label,icon] of [['selection','理解这一处','search'],['study-note-selection','记笔记','note'],['dismiss-selection','关闭标注工具','close']]){
    const el=bar.querySelector(`[data-action="${action}"]`);el.textContent='';el.innerHTML=icon==='close'?'×':studyIcon(icon);el.title=label;el.setAttribute('aria-label',label);el.classList.remove('primary');
  }
  bar.append(bar.querySelector('[data-action="dismiss-selection"]'));
  bar.setAttribute('role','toolbar');bar.setAttribute('aria-label','选文操作');
  bar.addEventListener('mousedown',e=>{if(e.target.closest('button'))e.preventDefault();});
  bar.querySelectorAll('[data-mark-style]').forEach(el=>el.onclick=async()=>{
    if(readingMarks.saving)return;
    if(readingMarks.loading){toast('正在读取已有标注，请稍后。');return;}
    const pending=readingMarks.selection;if(!pending){toast('请先选中正文。');return;}
    readingMarks.saving=true;bar.querySelectorAll('[data-mark-style]').forEach(b=>b.disabled=true);
    const existing=el.dataset.markStyle==='underline'?null:readingMarks.items.find(m=>m.style!=='underline'&&JSON.stringify(m.segments)===JSON.stringify(pending.segments));
    try{const item=await api('reading-annotation',{...pending,id:existing?.id,style:el.dataset.markStyle});if(source.isConnected&&state.article?.id===pending.article_id){readingMarks.items=[...readingMarks.items.filter(m=>m.id!==item.id),item];window.getSelection()?.removeAllRanges();paintReadingMarks(source);bar.hidden=true;}toast('标注已保存');}
    catch(err){toast(err.message);}finally{readingMarks.saving=false;bar.querySelectorAll('[data-mark-style]').forEach(b=>b.disabled=false);}
  });
  $('.paper-tools').insertAdjacentHTML('beforeend','<button id="reading-marks-list" type="button">标注</button>');
  $('.paper-tools>details>summary').insertAdjacentHTML('beforeend',studyIcon('chevron-down'));
  $('#reading-marks-list').onclick=()=>showReadingMarks();
  source.addEventListener('mouseup',()=>captureMarkSelection(source));
  source.addEventListener('keyup',()=>captureMarkSelection(source));
  source.addEventListener('scroll',()=>{bar.hidden=true;},{passive:true});
  source.addEventListener('click',e=>{const mark=e.target.closest('[data-reading-mark]');if(mark&&window.getSelection()?.isCollapsed)showReadingMarks(mark.dataset.readingMark.split(' '));});
  fetchReadingMarks(source,state.article);
};
window.addEventListener('resize',()=>{const bar=$('#selection-bar');if(bar)bar.hidden=true;});
document.addEventListener('keydown',e=>{if(e.key==='Escape'){const bar=$('#selection-bar');if(bar)bar.hidden=true;}});
document.addEventListener('pointerdown',e=>{if(!e.target.closest('#selection-bar,#source-body')){const bar=$('#selection-bar');if(bar)bar.hidden=true;}});
