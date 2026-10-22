const {chromium}=require('C:/Users/张哲铭/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const fs=require('fs'),assert=require('assert');
(async()=>{
 const info=JSON.parse(fs.readFileSync(process.env.ADAPTIVE_UI_INFO,'utf8'));
 const browser=await chromium.launch({executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless:true});
 try{
 const p=await browser.newPage({viewport:{width:1280,height:900}}),errors=[];p.on('pageerror',e=>errors.push(e.message));
 await p.goto(info.url);await p.waitForSelector('.managed-article-row');
 await p.evaluate(async()=>{acceptPreferences(await api('preferences',{palette:'ocean',mode:'light'}));});
 await p.locator('[data-action="manage-notes"]').click();await p.locator('[data-action="manage-note-new"]').click();
 await p.locator('#study-managed-note [name="title"]').fill('读完第一节后的疑问');await p.locator('#study-managed-note [name="content"]').fill('需要核对检索到的图片与文字是否属于同一个论点。');await p.locator('#study-managed-note [type="submit"]').click();await p.waitForSelector('.content-row');
 await p.locator('[data-content-edit]').first().click();await p.locator('#study-managed-note [name="title"]').fill('多模态证据对齐');await p.locator('#study-managed-note [type="submit"]').click();await p.waitForFunction(()=>document.querySelector('.content-row strong')?.textContent==='多模态证据对齐');
 await p.evaluate(async()=>{const a=state.articles[0];for(const [title,content] of [['方法比较','检索与生成需要分开评价，先记录各阶段的误差。'],['下一次阅读','回到实验部分核对评价指标与样本。']])await api('study/notes',{article_id:a.id,title,content});await contentManagementPage('notes');});
 await p.screenshot({path:info.root+'/notes-manager.png'});
 const controls=await p.locator('.content-toolbar input:not(.choice-native),.content-toolbar .choice-select-button').evaluateAll(es=>es.map(e=>e.getBoundingClientRect().bottom));assert(Math.max(...controls)-Math.min(...controls)<2,'filter controls must align');
 await p.locator('#content-search').fill('证据对齐');assert.equal(await p.locator('.content-row').count(),1);
 await p.locator('[data-content-delete]').click();await p.locator('#content-confirm-delete').click();await p.waitForFunction(()=>!document.querySelector('#dialog').open&&document.querySelectorAll('.content-row').length===0);
 assert.equal(await p.evaluate(()=>state.articles.length),1);
 await p.locator('#content-search').fill('');await p.setViewportSize({width:390,height:844});await p.screenshot({path:info.root+'/notes-mobile.png'});assert(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));await p.setViewportSize({width:1280,height:900});
 await p.locator('[data-action="study-plans"]').click();await p.locator('[data-action="study-new-task"]').click();await p.locator('#study-task-form [name="title"]').fill('阅读方法部分');await p.locator('#study-task-form [type="submit"]').click();await p.waitForSelector('.study-task');
 await p.evaluate(async()=>{for(const title of ['整理比较表','写下本周阅读心得'])await api('study/tasks',{title,date:studyDate(),minutes:30});await studyPage('plans');});
 assert.equal(await p.locator('.study-date-group').count(),1);await p.screenshot({path:info.root+'/plans-manager.png'});
 await p.locator('#plan-search').fill('方法');assert.equal(await p.locator('.study-task:visible').count(),1);
 await p.locator('.study-task:visible [data-content-task-delete]').click();await p.locator('#content-confirm-delete').click();await p.waitForFunction(()=>!document.querySelector('#dialog').open&&document.querySelectorAll('.study-task').length===2);
 await p.locator('[data-study-complete]').first().check();await p.waitForFunction(()=>!document.querySelector('[data-study-complete]').disabled);await p.locator('[data-select-for="plan-status"]').click();await p.getByRole('option',{name:'已完成',exact:true}).click();assert.equal(await p.locator('.study-task:visible').count(),1);
 await p.setViewportSize({width:390,height:844});await p.screenshot({path:info.root+'/plans-mobile.png'});assert(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));await p.setViewportSize({width:1280,height:900});
 await p.evaluate(async()=>{const a=state.articles[0],o=await api('object',{article_id:a.id,type:'term',title:'检索',block:'b0'});await api('object/update',{id:o.id,note:'旧笔记也应该可以管理'});await contentManagementPage('legacy_notes');});
 await p.locator('[data-content-edit]').click();await p.locator('#study-managed-note [name="content"]').fill('已更新旧笔记');await p.locator('#study-managed-note [type="submit"]').click();await p.waitForFunction(()=>document.querySelector('.content-row')?.textContent.includes('已更新旧笔记'));
 await p.locator('[data-content-delete]').click();await p.locator('#content-confirm-delete').click();await p.waitForFunction(()=>!document.querySelector('#dialog').open);await p.evaluate(()=>contentManagementPage('objects'));
 await p.locator('[data-content-delete]').first().click();await p.locator('#content-confirm-delete').click();await p.waitForFunction(()=>!document.querySelector('#dialog').open&&document.querySelectorAll('.content-row').length===0);
 await p.locator('[data-select-for="content-kind"]').click();await p.getByRole('option',{name:/文章资料/}).click();await p.locator('[data-content-delete]').click();await p.locator('#content-confirm-delete').click();await p.waitForFunction(()=>!document.querySelector('#dialog').open&&state.articles.length===0);
 const remaining=await p.evaluate(()=>api('content-management'));assert.equal(remaining.notes.length,0);assert.equal(remaining.tasks.length,2);assert(remaining.tasks.every(t=>t.article_ids.length===0));
 assert.deepEqual(errors,[]);console.log('PASS note create/edit/search/delete, plan create/group/filter/delete, object deletion, article cascade preserving plans, desktop/mobile layout');
 if(process.env.ADAPTIVE_EXE)await p.evaluate(()=>api('shutdown',{}));
 }finally{await browser.close();}
})().catch(e=>{console.error(String(e.stack||e).replace(/token=[^\s]+/g,'token=hidden'));process.exitCode=1;});
