const readingProgressOpen=openArticle;
openArticle=async function(id,oid=null){await readingProgressOpen(id,oid);try{const record=await api('study-visit',{article_id:id});if(state.article?.id===id){state.readingProgress=record;renderReadingProgress();}}catch(err){toast(err.message);}};
function renderReadingProgress(){
  if(state.page!=='work'||!state.article)return;
  const toolbar=$('.reading-toolbar .actions:last-child');if(!toolbar)return;
  let control=$('[data-action="study-read-complete"]');if(!control){toolbar.insertAdjacentHTML('beforeend',button('study-read-complete','标记读完'));control=$('[data-action="study-read-complete"]');}
  const completed=state.readingProgress?.article_id===state.article.id&&state.readingProgress.completed;
  control.textContent=completed?'已读完':'标记读完';control.setAttribute('aria-pressed',String(Boolean(completed)));
}
const progressRenderWork=renderWork;
renderWork=function(){progressRenderWork();renderReadingProgress();};
const progressDispatch=dispatch;
dispatch=async function(action){if(action==='study-read-complete'){state.readingProgress=await api('study-visit',{article_id:state.article.id,completed:!state.readingProgress?.completed});renderReadingProgress();toast(state.readingProgress.completed?'已记下这次阅读成果':'已恢复为阅读中');return;}if(action==='sync'&&state.page==='study'){const page=study.page;await progressDispatch(action);await studyPage(page);return;}return progressDispatch(action);};
const progressPage=studyPage;
studyPage=async function(page='themes'){
  if(await progressPage(page)===false)return false;if(!['stats','today','themes'].includes(page))return;
  const activity=await api('study-activity');if(state.page!=='study'||study.page!==page)return;
  if(page==='today'){
    const recent=activity.readings.filter(r=>state.articles.some(a=>a.id===r.article_id)).sort((a,b)=>b.last_opened.localeCompare(a.last_opened)).slice(0,3);
    if(recent.length)$('#main').insertAdjacentHTML('beforeend',`<section class="study-recent"><h2>继续阅读</h2><div class="study-article-list">${recent.map(r=>`<button data-open="${r.article_id}">${studyIcon('book')}<span><strong>${esc(state.articles.find(a=>a.id===r.article_id).title)}</strong><small>${r.completed?'已读完 · 再次回顾':'阅读中'}</small></span>${studyIcon('arrow-right')}</button>`).join('')}</div></section>`);
  }
  if(page==='themes'){
    const done=new Set(activity.readings.filter(r=>r.completed).map(r=>r.article_id));
    document.querySelectorAll('.study-article-list [data-open]').forEach(el=>{if(done.has(el.dataset.open))el.querySelector('small')?.append(' · 已读完');});
  }
  if(page==='stats'){
    const since=activity.days[0].date;
    const notes=study.data.notes.filter(n=>n.created_at.slice(0,10)>=since);
    const tasks=study.data.tasks.filter(t=>t.completed_at?.slice(0,10)>=since&&t.status==='done');
    const completed=activity.readings.filter(r=>r.completed);
    $('.study-metrics').insertAdjacentHTML('beforeend',`<div><strong>${completed.length}</strong><span>已标记读完</span></div>`);
    $('#main').insertAdjacentHTML('beforeend',`<section class="study-weekly"><h2>最近七天</h2><p>新增 ${notes.length} 篇心得，完成 ${tasks.length} 项计划。</p><div class="study-activity-bars" role="img" aria-label="最近七天每日打开文章数">${activity.days.map(d=>`<div><strong>${d.articles_opened}</strong><span style="height:${Math.max(3,d.articles_opened/Math.max(1,...activity.days.map(v=>v.articles_opened))*80)}px"></span><small>${d.date.slice(5)}</small></div>`).join('')}</div><p class="muted">每日打开的文章数 · 同一文章每天计一次</p><h2>主题积累</h2><div class="study-topic-progress">${study.data.themes.map(t=>{const ids=study.data.memberships.filter(m=>m.theme_id===t.id).map(m=>m.article_id);const count=completed.filter(r=>ids.includes(r.article_id)).length;return `<div><strong>${esc(t.title)}</strong><progress max="${Math.max(1,ids.length)}" value="${count}" aria-label="${esc(t.title)}阅读进度"></progress><span>已读完 ${count} / ${ids.length} 篇</span></div>`;}).join('')||'<p class="muted">还没有主题分类。</p>'}</div></section>`);
  }
};
