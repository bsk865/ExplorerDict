const {chromium}=require('C:/Users/张哲铭/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const fs=require('fs'),assert=require('assert');
(async()=>{
 const info=JSON.parse(fs.readFileSync(process.env.ADAPTIVE_UI_INFO,'utf8'));
 const browser=await chromium.launch({executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless:true});
 try{
  const page=await browser.newPage({viewport:{width:1280,height:900}}), errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.goto(info.url);await page.locator('#navigation [data-action="study-themes"]').click();await page.waitForSelector('.study-article-list');
  assert((await page.title()).includes('探索'));
  await page.locator('.managed-article-row [data-open]').first().click();
  await page.waitForFunction(()=>state.page==='work'&&routeDepth===0);
  await page.evaluate(()=>{window.backgroundBusySeen=false;window.busyWatcher=new MutationObserver(()=>{if(!document.querySelector('#busy').hidden)backgroundBusySeen=true;});busyWatcher.observe(document.querySelector('#busy'),{attributes:true,childList:true,subtree:true});});
  await page.locator('[data-action="mapview"]').click();
  await page.waitForFunction(()=>Boolean(state.article?.map),null,{timeout:15000});
  assert(!(await page.evaluate(()=>backgroundBusySeen)));
  assert(await page.locator('#busy').isHidden());
  assert((await page.locator('#main').innerText()).includes('资源约束'));
  await page.screenshot({path:info.root+'/prepared-map.png'});
  const id=await page.evaluate(()=>state.article.id);
  await page.reload();await page.locator('#navigation [data-action="study-themes"]').click();await page.waitForSelector('.study-article-list');
  await page.evaluate(id=>openArticle(id),id);
  const began=Date.now();
  await page.locator('[data-action="mapview"]').click();
  await page.waitForFunction(()=>Boolean(document.querySelector('#map-svg')));
  console.log('Cached map view ready in '+(Date.now()-began)+' ms (controlled local fixture)');
  const second=await page.evaluate(()=>api('import',{title:'接续任务验证',text:'另一份原文，同样需要减少计算量。'}));
  await page.evaluate(id=>openArticle(id),second.id);
  await page.locator('[data-action="mapview"]').click();
  await page.waitForTimeout(1900);
  assert(await page.locator('#busy').isHidden());
  await page.locator('[data-action="generate"]').first().click();
  await page.waitForFunction(()=>Boolean(state.article?.map)&&!state.job,null,{timeout:10000});
  assert(await page.locator('#busy').isHidden());
  await page.setViewportSize({width:390,height:844});
  await page.screenshot({path:info.root+'/prepared-map-mobile.png'});
  assert.deepEqual(errors,[]);
  console.log('PASS silent background, automatic completed-map adoption, cached reopen, foreground join, no page errors');
  if(process.env.ADAPTIVE_EXE){
   const audit=await page.evaluate(()=>api('request-status'));
   assert.equal(audit.active,0);assert.equal(audit.queued,0);
   assert.equal(audit.history.length,6);
   assert(audit.history.every(row=>row.status==='done'&&row.elapsed_ms>=0));
   assert(!JSON.stringify(audit).includes('另一份原文'));
   console.log('PASS packaged request supervision: six completed calls, no active/queued requests');
   await page.evaluate(()=>api('shutdown',{}));
  }
 }finally{await browser.close();}
})().catch(error=>{console.error(String(error).replace(/token=[^\s]+/g,'token=hidden'));process.exitCode=1;});
