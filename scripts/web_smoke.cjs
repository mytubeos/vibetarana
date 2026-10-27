const {chromium}=require(process.env.PLAYWRIGHT_PACKAGE || 'playwright');
const fs=require('fs');
const root=require('path').join(__dirname,'../bot/web/');
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.CHROME_BIN || undefined,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:390,height:844},deviceScaleFactor:1});
 const errors=[];page.on('pageerror', e=>errors.push(e.message));
 await page.route('https://telegram.org/**', r=>r.fulfill({contentType:'text/javascript',body:'window.Telegram={WebApp:{initData:"test",ready(){},expand(){}}}'}));
 const actions=[];
 await page.route('https://vibetarana.test/**',async r=>{
   const url=new URL(r.request().url());
   if(url.pathname==='/api'){
      const req=r.request().postDataJSON();actions.push(req);
      let payload=req.action==='state'?{chat:'Evening music club',paused:false,needs_resume:false,playback_id:1,revision:0,can_control:true,can_request:true,queue:[{title:'A quiet evening',duration:'3:42',requester:'Raj'},{title:'<img src=x onerror=alert(1)>',duration:'4:05',requester:'Asha'}],playlists:['favourites','late_night']}:{ok:true};
      return r.fulfill({contentType:'application/json',body:JSON.stringify(payload)});
   }
   const file=url.pathname==='/app'?'index.html':url.pathname.split('/').pop();
   await r.fulfill({contentType:file.endsWith('js')?'text/javascript':file.endsWith('css')?'text/css':'text/html',body:fs.readFileSync(root+file)});
 });
 await page.goto('https://vibetarana.test/app?chat=-123');
 await page.getByText('A quiet evening',{exact:true}).waitFor();
 if(await page.locator('#queue img').count())throw Error('Unsafe queue HTML');
 await page.fill('#query','test song');await page.click('#playButton');
 await page.waitForFunction(()=>document.getElementById('notice').textContent==='Queue is up to date.');
 if(!actions.some(x=>x.action==='play'&&x.query==='test song'&&x.request_id))throw Error('Play not sent');
 const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth);
 if(overflow)throw Error('Mobile horizontal overflow');
 if(process.env.UI_SCREENSHOT_DIR) await page.screenshot({path:process.env.UI_SCREENSHOT_DIR+'/mobile.png',fullPage:true});
 await page.setViewportSize({width:1100,height:900});if(process.env.UI_SCREENSHOT_DIR) await page.screenshot({path:process.env.UI_SCREENSHOT_DIR+'/desktop.png',fullPage:true});
 if(errors.length)throw Error(errors.join('\n'));
 console.log('Browser smoke passed: mobile/desktop render, safe titles, authenticated play payload, no JS errors.');
 await browser.close();
})();
