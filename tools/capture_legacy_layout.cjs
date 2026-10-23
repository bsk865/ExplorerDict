// Reference-only browser render; the native application does not use this runtime.
const {chromium} = require('C:/Users/张哲铭/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const fs = require('fs');

(async () => {
  const info = JSON.parse(fs.readFileSync(process.env.ADAPTIVE_UI_INFO, 'utf8'));
  const browser = await chromium.launch({executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless:true});
  try {
    const page = await browser.newPage({viewport:{width:1380,height:880}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(info.url);
    await page.waitForSelector('#navigation [data-action="study-today"]');
    await page.waitForFunction(() => document.querySelector('#main').textContent.trim().length > 0);
    await page.locator('#navigation [data-action="study-today"]').click();
    await page.waitForSelector('.study-day-strip');
    const metrics = {};
    for (const [name,width,height] of [['wide',1380,880],['compact',900,620]]) {
      await page.setViewportSize({width,height});
      await page.screenshot({path:info.root+'/legacy-'+name+'.png'});
      metrics[name] = await page.evaluate(() => {
        const measure = selector => {
          const element = document.querySelector(selector), style = getComputedStyle(element), box = element.getBoundingClientRect();
          return {x:box.x,y:box.y,width:box.width,height:box.height,padding:style.padding,gap:style.gap,
            font:style.font,fontSize:style.fontSize,color:style.color,background:style.backgroundColor,
            border:style.border,display:style.display};
        };
        return {sidebar:measure('.sidebar'),brand:measure('.brand'),workspace:measure('.workspace-name'),
          nav:measure('#navigation'),button:measure('#navigation .nav-item'),bottom:measure('.sidebar-bottom'),
          recent:measure('#recent'),top:measure('.topbar'),footer:measure('.app-footer'),
          labels:[...document.querySelectorAll('#navigation button')].map(e=>({text:e.textContent.trim(),action:e.dataset.action})),
          theme:{accent:getComputedStyle(document.documentElement).getPropertyValue('--accent-soft'),
            accentText:getComputedStyle(document.documentElement).getPropertyValue('--accent-text')}};
      });
    }
    await page.locator('#navigation [data-action="library"]').click();
    await page.waitForSelector('.library-assistant');
    for (const [name,width,height] of [['wide',1380,880],['compact',900,620]]) {
      await page.setViewportSize({width,height});
      await page.screenshot({path:info.root+'/legacy-library-'+name+'.png'});
      metrics['library-'+name] = await page.evaluate(() => Object.fromEntries(
        ['.library-assistant','.library-heading','.library-scope-summary','.library-conversation',
         '#library-messages','.composer','#library-question'].map(selector => {
          const element=document.querySelector(selector), style=getComputedStyle(element), box=element.getBoundingClientRect();
          return [selector,{x:box.x,y:box.y,width:box.width,height:box.height,padding:style.padding,gap:style.gap,
            background:style.backgroundColor,border:style.border,font:style.font}];
        })));
    }
    await page.locator('[data-action="library-scope"]').first().click();
    await page.waitForSelector('#dialog[open] .library-picker');
    await page.screenshot({path:info.root+'/legacy-library-scope.png'});
    await page.locator('#dialog [data-action="close"]').last().click();
    await page.locator('#library-question').fill('检索增强生成');
    await page.locator('[data-action="library-search"]').click();
    await page.waitForSelector('#dialog[open] .library-results');
    await page.screenshot({path:info.root+'/legacy-library-search.png'});
    await page.locator('#dialog [data-action="close"]').last().click();
    await page.locator('#navigation [data-action="study-themes"]').click();
    await page.waitForSelector('.study-library');
    for (const [name,width,height] of [['wide',1380,880],['compact',900,620]]) {
      await page.setViewportSize({width,height});
      await page.screenshot({path:info.root+'/legacy-shelf-'+name+'.png'});
    }
    await page.locator('.study-rooms [data-study-theme]').first().click();
    await page.waitForSelector('[data-action="study-edit-theme"]');
    await page.screenshot({path:info.root+'/legacy-theme-compact.png'});
    fs.writeFileSync(info.root+'/legacy-layout.json',JSON.stringify({metrics,errors},null,2));
    if (errors.length) throw Error(errors.join('\n'));
    console.log('PASS legacy layout reference, desktop and compact navigation, no page errors');
  } finally {
    await browser.close();
  }
})().catch(error=>{console.error(String(error).replace(/token=[^\s]+/g,'token=hidden'));process.exitCode=1;});
