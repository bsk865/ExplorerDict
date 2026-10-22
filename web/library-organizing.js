let libraryDraggedArticle=null;
let libraryDragTarget=null;
const libraryAssignments=new Set();
function clearLibraryDrag(){
  libraryDraggedArticle=null;libraryDragTarget=null;
  document.querySelectorAll('.article-dragging,.theme-drop-over').forEach(el=>el.classList.remove('article-dragging','theme-drop-over'));
  document.body.classList.remove('article-drag-active');
  document.querySelectorAll('.article-drag-preview,.article-drop-hint').forEach(el=>el.remove());
}
function focusLibraryDrop(target){
  if(target===libraryDragTarget)return;
  libraryDragTarget?.classList.remove('theme-drop-over');libraryDragTarget=target;
  target?.classList.add('theme-drop-over');
  const hint=$('.article-drop-hint');if(!hint)return;
  const id=target?.dataset.themeDrop,t=study.data?.themes.find(t=>t.id===id);
  const assigned=study.data?.memberships.some(m=>m.theme_id===id&&m.article_id===libraryDraggedArticle);
  hint.textContent=t?(assigned?'已在「'+t.title+'」中':'松开归入「'+t.title+'」'):'拖到一个主题 · Esc 取消';
  hint.classList.toggle('has-target',!!t);
}
document.addEventListener('dragover',e=>{
  if(!libraryDraggedArticle)return;
  const target=e.target.closest('[data-theme-drop]');focusLibraryDrop(target);
  if(target){e.preventDefault();e.dataTransfer.dropEffect='copy';}
});
document.addEventListener('dragend',clearLibraryDrag);
window.addEventListener('blur',clearLibraryDrag);
new ResizeObserver(entries=>document.documentElement.style.setProperty('--reading-rail-height',entries[0].target.getBoundingClientRect().height+'px')).observe($('.sidebar'));
async function addArticleToTheme(articleId,themeId){
  const key=articleId+':'+themeId;
  if(libraryAssignments.has(key))return;
  if(study.data.memberships.some(m=>m.theme_id===themeId&&m.article_id===articleId)){toast('文章已在这个主题中');return;}
  libraryAssignments.add(key);
  const canvas=$('.study-library');
  const targets=[...document.querySelectorAll(`[data-theme-drop="${themeId}"]`)];
  targets.forEach(el=>{el.classList.add('theme-drop-saving');el.setAttribute('aria-busy','true');});
  try{
    await api('study/assign',{theme_id:themeId,article_ids:[articleId],mode:'add'});
    if(state.page==='study'&&study.page==='themes'&&canvas?.isConnected){
      if(!study.data.memberships.some(m=>m.theme_id===themeId&&m.article_id===articleId))study.data.memberships.push({theme_id:themeId,article_id:articleId});
      const count=study.data.memberships.filter(m=>m.theme_id===themeId).length;
      targets.forEach(el=>{const label=el.querySelector('small')||el.querySelector('span');if(label)label.textContent=el.closest('.study-rooms')?count+' 篇文章':String(count);el.classList.remove('theme-drop-saved');void el.offsetWidth;el.classList.add('theme-drop-saved');setTimeout(()=>el.classList.remove('theme-drop-saved'),700);});
      const unfiled=$('[data-study-theme="unfiled"] small');if(unfiled)unfiled.textContent=String(state.articles.filter(a=>!study.data.memberships.some(m=>m.article_id===a.id)).length);
      if(study.theme==='unfiled'){
        const row=canvas.querySelector(`.managed-article-row [data-open="${articleId}"]`)?.closest('.managed-article-row');
        if(row){const motion=matchMedia('(prefers-reduced-motion: reduce)').matches?0:180;const animation=row.animate([{opacity:1},{opacity:0,transform:'translateX(12px)'}],{duration:motion});animation.finished.then(()=>{row.remove();const list=canvas.querySelector('.study-article-list');if(list&&!list.children.length)list.innerHTML='<p class="empty">这里还没有文章。</p>';}).catch(()=>{});}
      }
    }
    toast('已归入「'+(study.data.themes.find(t=>t.id===themeId)?.title||'主题')+'」');
  }finally{libraryAssignments.delete(key);targets.forEach(el=>{el.classList.remove('theme-drop-saving');el.removeAttribute('aria-busy');});}
}
function articleThemePicker(articleId){
  modal('归入主题',`<div class="theme-picker">${study.data.themes.map(t=>`<button data-assign-theme="${t.id}">${studyIcon('book')}${esc(t.title)}${study.data.memberships.some(m=>m.theme_id===t.id&&m.article_id===articleId)?'<small>已归入</small>':''}</button>`).join('')||'<p>请先新建一个主题。</p>'}</div>${button('close','关闭')}`);
  document.querySelectorAll('[data-assign-theme]').forEach(el=>el.onclick=async()=>{el.disabled=true;try{await addArticleToTheme(articleId,el.dataset.assignTheme);$('#dialog').close();}catch(err){el.disabled=false;toast(err.message);}});
}
const organizingStudyPage=studyPage;
studyPage=async function(page='themes'){
  const result=await organizingStudyPage(page);if(result===false)return false;
  if(state.page!=='study'||study.page!=='themes'||page!=='themes')return;
  document.querySelectorAll('.managed-article-row').forEach(row=>{
    const open=row.querySelector('[data-open]');open.draggable=true;open.title='拖入主题归类';
    open.addEventListener('dragstart',e=>{
      clearLibraryDrag();libraryDraggedArticle=open.dataset.open;
      e.dataTransfer.setData('application/x-explorer-article',open.dataset.open);e.dataTransfer.effectAllowed='copy';
      const preview=document.createElement('div');preview.className='article-drag-preview';
      preview.innerHTML=`${studyIcon('file')}<span>${esc(open.querySelector('strong').textContent)}</span><b>+</b>`;
      const rect=open.getBoundingClientRect();preview.style.left=Math.max(8,Math.min(rect.left,innerWidth-290))+'px';preview.style.top=Math.max(8,Math.min(rect.top,innerHeight-64))+'px';
      document.body.append(preview);e.dataTransfer.setDragImage(preview,24,24);requestAnimationFrame(()=>preview.remove());
      row.classList.add('article-dragging');document.body.classList.add('article-drag-active');
      const hint=document.createElement('div');hint.className='article-drop-hint';hint.setAttribute('role','status');hint.textContent='拖到一个主题 · Esc 取消';document.body.append(hint);
    });
    const classify=document.createElement('button');classify.innerHTML=studyIcon('archive');classify.title='归入主题';classify.setAttribute('aria-label','归入主题');classify.onclick=()=>articleThemePicker(open.dataset.open);row.insertBefore(classify,row.lastElementChild);
  });
  document.querySelectorAll('[data-study-theme]').forEach(target=>{
    const id=target.dataset.studyTheme;if(!study.data.themes.some(t=>t.id===id))return;
    target.dataset.themeDrop=id;
    target.addEventListener('drop',async e=>{e.preventDefault();const aid=e.dataTransfer.getData('application/x-explorer-article');if(!libraryDraggedArticle||aid!==libraryDraggedArticle)return;clearLibraryDrag();try{await addArticleToTheme(aid,id);}catch(err){toast('归类未保存：'+err.message);}});
  });
};
