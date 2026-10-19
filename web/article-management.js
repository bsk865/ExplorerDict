/* Article management uses the same deletion path as the reading workspace. */
const managedSelection = new Set();
const managementBooks = renderBooks;
renderBooks = function(query='') {
  managementBooks(query);
  document.querySelectorAll('.document-card').forEach(card => {
    const id=card.querySelector('[data-open]').dataset.open;
    card.insertAdjacentHTML('beforeend',`<div class="document-management"><label><input type="checkbox" data-select-article="${id}" ${managedSelection.has(id)?'checked':''}>选择</label><button type="button" data-delete-article="${id}">删除文章</button></div>`);
  });
  updateManagementCount();
};
function updateManagementCount(){
  const control=document.querySelector('[data-action="delete-selected"]');
  if(control){control.disabled=!managedSelection.size;control.textContent=`删除所选${managedSelection.size?'（'+managedSelection.size+'）':''}`;}
}
const managementShelf=shelf;
shelf=async function(){
  await managementShelf();
  for(const id of managedSelection)if(!state.articles.some(a=>a.id===id))managedSelection.delete(id);
  $('.shelf-scope').insertAdjacentHTML('afterend',`<div class="article-management-bar"><div class="actions">${button('import','添加文件','primary')}${button('paste','粘贴文章')}${button('url-import','保存网页')}</div><div class="actions">${button('select-visible','选择当前结果')}${button('clear-selected','取消选择')}${button('delete-selected','删除所选')}</div></div>`);
  updateManagementCount();
};
function confirmArticleDeletion(ids){
  const articles=state.articles.filter(a=>ids.includes(a.id));
  if(!articles.length)return;
  modal('删除文章',`<p>将删除以下 ${articles.length} 篇文章及各自的词语、笔记、导图、对话和历史版本。此操作无法撤销。</p><ul class="delete-article-list">${articles.map(a=>`<li>${esc(a.title)}</li>`).join('')}</ul><div class="actions">${button('close','取消')}<button class="primary" id="confirm-managed-delete">确认删除</button></div>`);
  $('#confirm-managed-delete').onclick=async()=>{
    const control=$('#confirm-managed-delete');control.disabled=true;
    try{
      for(const a of articles){
        await api('article/delete',{article_id:a.id});
        managedSelection.delete(a.id);
        for(const key of drafts.keys())if(key.startsWith(a.id+':'))drafts.delete(key);
        if(localStorage.getItem('reading-last-article')===a.id)localStorage.removeItem('reading-last-article');
      }
      $('#dialog').close();state.article=null;state.object=null;state.session={messages:[],draft:''};
      await shelf();toast('所选文章已删除');
    }catch(error){control.disabled=false;await refresh();toast(error.message);}
  };
}
const managementDispatch=dispatch;
dispatch=async function(action){
  if(action==='delete-selected'){confirmArticleDeletion([...managedSelection]);return;}
  if(action==='select-visible'||action==='clear-selected'){
    if(action==='clear-selected')managedSelection.clear();
    else document.querySelectorAll('[data-select-article]').forEach(el=>managedSelection.add(el.dataset.selectArticle));
    renderBooks($('#book-search').value);return;
  }
  return managementDispatch(action);
};
document.addEventListener('change',e=>{
  const el=e.target.closest('[data-select-article]');if(!el)return;
  if(el.checked)managedSelection.add(el.dataset.selectArticle);else managedSelection.delete(el.dataset.selectArticle);
  updateManagementCount();
});
document.addEventListener('click',e=>{
  const el=e.target.closest('[data-delete-article]');if(el)confirmArticleDeletion([el.dataset.deleteArticle]);
});
