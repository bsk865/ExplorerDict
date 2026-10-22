/* Shared navigation and control clarity for the existing desktop views. */
const pageTrail=[];
let currentRoute=null, routeDepth=0, restoringRoute=false;
function routeSnapshot(){
  return {page:state.page,studyPage:study.page,theme:study.theme,article:state.article?.id,object:state.object?.id,
    view:state.view,assistant:state.assistant,assistantTab:state.assistantTab,kind:contentManager.kind,
    query:$('#content-search')?.value??contentManager.query,articleFilter:$('#content-article')?.value??contentManager.article,planView:study.planView,weekOffset:study.weekOffset,
    ids:library.ids?[...library.ids]:null,includeNotes:library.includeNotes,search:$('#global-search').value,
    scroll:$('#main').scrollTop,sourceScroll:$('#source-body')?.scrollTop||0};
}
function routeKey(route){
  if(route.page==='study')return ['study',route.studyPage,route.theme].join(':');
  if(route.page==='work')return ['work',route.article,route.object,route.view].join(':');
  if(route.page==='management')return 'management:'+route.kind;
  return route.page;
}
function updateBackButton(){
  $('#page-back').disabled=routeDepth>0||!pageTrail.length;
}
function wrapPageNavigation(original){
  return async function(...args){
    const outer=routeDepth===0;
    if(outer&&currentRoute){
      const live=routeSnapshot();
      Object.assign(currentRoute,{scroll:live.scroll,sourceScroll:live.sourceScroll,search:live.search,query:live.query,articleFilter:live.articleFilter,planView:live.planView,weekOffset:live.weekOffset});
      if(currentRoute.page==='work'&&live.page==='work')Object.assign(currentRoute,{view:live.view,object:live.object,assistant:live.assistant,assistantTab:live.assistantTab});
      if(currentRoute.page==='library'&&live.page==='library')Object.assign(currentRoute,{ids:live.ids,includeNotes:live.includeNotes});
    }
    const previous=currentRoute;
    routeDepth++;updateBackButton();
    try{
      const result=await original(...args);
      if(outer&&result!==false){
        const next=routeSnapshot();
        if(!restoringRoute&&previous&&routeKey(previous)!==routeKey(next)){
          pageTrail.push(previous);if(pageTrail.length>50)pageTrail.shift();
        }
        currentRoute=next;
      }
      return result;
    }finally{routeDepth--;updateBackButton();}
  };
}
$('#crumb').insertAdjacentHTML('beforebegin',`<button id="page-back" type="button" title="返回上一页" aria-label="返回上一页" disabled>${icon('arrow-right')}</button>`);
for(const name of ['studyPage','libraryPage','ragPage','contentManagementPage','openArticle','savedPage']){
  // Function declarations are exposed on window; keep their existing adapters intact.
  window[name]=wrapPageNavigation(window[name]);
}
async function goBack(){
  if(routeDepth)return;
  if($('#dialog').open)return backDialog();
  const route=pageTrail.at(-1);if(!route)return;
  await saveDraft();preserveScroll();
  restoringRoute=true;
  try{
    $('#global-search').value=route.search;
    if(route.page==='study'){
      study.theme=route.theme;study.planView=route.planView;study.weekOffset=route.weekOffset;await studyPage(route.studyPage);
    }else if(route.page==='library'){
      library.ids=route.ids;library.includeNotes=route.includeNotes;await libraryPage();
    }else if(route.page==='rag')await ragPage();
    else if(route.page==='management'){
      contentManager.query=route.query;contentManager.article=route.articleFilter;await contentManagementPage(route.kind);
    }else if(route.page==='work'){
      if(!state.articles.some(a=>a.id===route.article)){pageTrail.pop();toast('这份资料已移除');return;}
      await openArticle(route.article,route.object);state.view=route.view;state.assistant=route.assistant;state.assistantTab=route.assistantTab;renderWork();
      if($('#source-body'))$('#source-body').scrollTop=route.sourceScroll;
    }else if(route.page==='saved')await savedPage();
    else await studyPage('themes');
    $('#main').scrollTop=route.scroll;
    currentRoute=routeSnapshot();pageTrail.pop();
  }finally{restoringRoute=false;updateBackButton();}
}
$('#page-back').onclick=()=>goBack().catch(e=>toast(e.message));
document.addEventListener('keydown',e=>{
  if(e.altKey&&e.key==='ArrowLeft'){e.preventDefault();goBack().catch(err=>toast(err.message));}
});

const dialogTrail=[];
const navigationModal=modal;
modal=function(title,content){
  const dialog=$('#dialog'),body=$('#dialog-body');
  const oldTitle=body.querySelector('.dialog-head h2')?.textContent;
  if(dialog.open&&oldTitle!==title){
    dialogTrail.push({nodes:[...body.childNodes],focus:document.activeElement,scroll:dialog.scrollTop});
  }
  navigationModal(title,content);
  const close=body.querySelector('.dialog-head [data-action="close"]');
  if(close){close.title='关闭';close.setAttribute('aria-label','关闭');close.dataset.action='dialog-dismiss';}
  if(dialogTrail.length){
    const back=document.createElement('button');back.type='button';back.className='dialog-back';back.title='返回';back.setAttribute('aria-label','返回上一层');back.innerHTML=icon('arrow-right');back.onclick=backDialog;
    body.querySelector('.dialog-head').prepend(back);
  }
};
function backDialog(){
  const previous=dialogTrail.pop();
  if(!previous){$('#dialog').close();return;}
  $('#dialog-body').replaceChildren(...previous.nodes);$('#dialog').scrollTop=previous.scroll;
  if(previous.focus?.isConnected)previous.focus.focus();
}
$('#dialog').addEventListener('close',()=>{dialogTrail.length=0;});
$('#dialog').addEventListener('cancel',e=>{if(dialogTrail.length){e.preventDefault();backDialog();}});
const navigationDispatch=dispatch;
dispatch=async function(action){
  if(action==='page-back')return goBack();
  if(action==='dialog-dismiss'){$('#dialog').close();return;}
  if(action==='close'&&dialogTrail.length)return backDialog();
  return navigationDispatch(action);
};

const compactLibraryRender=renderLibrary;
renderLibrary=function(){
  compactLibraryRender();if(state.page!=='library')return;
  const heading=$('.library-heading'),actions=heading.querySelector('.actions');
  heading.querySelector('h1').textContent='资料库问答';heading.querySelector('.chat-title p')?.remove();
  const more=document.createElement('details');more.className='library-more';
  more.innerHTML='<summary>更多</summary><div class="library-more-panel"></div>';
  const panel=more.querySelector('div');
  actions.querySelectorAll('button:not([data-action="library-scope"])').forEach(el=>panel.append(el));
  const meter=$('.library-conversation .context-meter');if(meter){meter.textContent='对话用量';panel.append(meter);}
  const rag=$('.rag-inline');if(rag)panel.append(rag);
  actions.append(more);
  panel.addEventListener('click',e=>{if(e.target.closest('button'))more.open=false;});
  $('.library-heading [data-action="study-compare"]').disabled=library.ids.length<2;
  $('.library-heading [data-action="library-clear"]').disabled=!library.session.messages.length;
  $('.library-scope-summary>span').title=$('.library-scope-summary>span').textContent;
  $('.library-privacy').textContent='回答时，相关资料和对话会发送给你配置的模型服务。';
  $('.library-conversation .actions>.muted')?.remove();
  const welcome=$('.library-welcome');
  if(welcome){
    welcome.innerHTML=library.ids.length?'<h2>想了解哪一处？</h2>':`<h2>选择资料，开始提问</h2>${button(state.articles.length?'library-scope':'import',state.articles.length?'选择资料':'添加资料','primary')}`;
  }
  $('#library-question').placeholder=library.ids.length?'输入你的问题…':'选择资料后即可提问';
  for(const action of ['library-send','library-search'])$(`[data-action="${action}"]`).disabled=!library.ids.length||Boolean(library.session.closed);
};
document.addEventListener('click',e=>{const more=$('.library-more[open]');if(more&&!more.contains(e.target))more.open=false;});
document.addEventListener('keydown',e=>{if(e.key==='Escape'){const more=$('.library-more[open]');if(more){more.open=false;more.querySelector('summary').focus();}}});
