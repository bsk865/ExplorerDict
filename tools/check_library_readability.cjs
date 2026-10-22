const {chromium}=require('C:/Users/张哲铭/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const fs=require('fs'),assert=require('assert');
(async()=>{
 const info=JSON.parse(fs.readFileSync(process.env.TEMP+'/'+(process.env.LIBRARY_PACKAGE_CHECK?'study-package-current.json':'study-qa-current.json'),'utf8'));
 const browser=await chromium.launch({executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless:true});
 try {
 const p=await browser.newPage({viewport:{width:1440,height:960}}),errors=[];
 p.on('pageerror',e=>errors.push(e.message));await p.goto(info.url);
 await p.locator('#navigation [data-action="library"]').click();await p.waitForSelector('#library-question');
 await p.locator('#library-question').fill('你好');await p.locator('[data-action="library-send"]').click();
 await p.waitForFunction(()=>document.querySelector('#library-messages')?.textContent.includes('今天想一起读'));
 assert.equal(await p.locator('[data-study-save-answer]').count(),0);
 await p.evaluate(()=>{const a=state.articles[0];library.session.messages.push({role:'user',content:'请解释控制约束。'},{role:'assistant',content:'## 控制约束是什么\n简单说，**约束就是控制不能越过的范围**。[S1]\n\n- 限制控制输入，避免超出允许范围。[S1]\n- 具体阈值还需要核对原文。\n\n<img src=x onerror=alert(1)>',evidence:[{citation:'S1',article_id:a.id,article_title:a.title,version:a.version,block:0,quote:'约束条件用于限制控制输入。'}]});renderLibrary();});
 assert.equal(await p.locator('.library-answer strong').count(),1);assert.equal(await p.locator('.library-answer li').count(),2);
 assert.equal(await p.locator('.library-answer img').count(),0);assert.equal(await p.locator('[data-study-save-answer]').count(),1);
 await p.locator('.library-cite').first().click();await p.waitForSelector('#library-open-source');assert((await p.locator('#dialog').innerText()).includes('约束条件'));
 await p.evaluate(()=>document.querySelector('#dialog').close());
 await p.screenshot({path:info.root+'/readability-desktop.png'});
 await p.locator('[data-study-save-answer]').click();assert((await p.locator('#study-answer-note-form textarea').inputValue()).includes('**约束'));await p.evaluate(()=>document.querySelector('#dialog').close());
 await p.setViewportSize({width:390,height:844});await p.screenshot({path:info.root+'/readability-mobile.png'});
 assert(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));assert.deepEqual(errors,[]);
 console.log('PASS greeting, safe formatting, citations, note source preservation, mobile layout. Screenshots: '+info.root);
 if(process.env.LIBRARY_PACKAGE_CHECK)await p.evaluate(()=>api('shutdown',{}));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
