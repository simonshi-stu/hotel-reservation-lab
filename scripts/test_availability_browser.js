// Real headless Chromium smoke test. Requires preinstalled Playwright + Chromium.
const {chromium}=require('playwright');
const {spawn}=require('child_process');
const fs=require('fs'), os=require('os'), path=require('path'), assert=require('assert/strict');
(async()=>{
 const state=fs.mkdtempSync(path.join(os.tmpdir(),'hotel-availability-'));
 const server=spawn(process.env.PYTHON||'python',['-B','-m','hotel_demo.server','--port','0','--state-dir',state],{stdio:['ignore','pipe','inherit']});
 let browser;
 try {
  const url=await new Promise((resolve,reject)=>{let output='';server.stdout.on('data',data=>{output+=data;const m=output.match(/http:\/\/127.0.0.1:\d+/);if(m)resolve(m[0]);});server.on('exit',code=>reject(Error('server exited '+code)));});
  browser=await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_PATH||'/usr/bin/chromium',args:['--no-sandbox']});
  const page=await browser.newPage({viewport:{width:1280,height:900}});
  const errors=[], writes=[];page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>{if(r.method()!=='GET')writes.push(r.url());});
  await page.goto(url+'/availability');await page.locator('#days tr').nth(13).waitFor();
  assert.equal(await page.locator('#days tr').count(),14);
  assert.equal(await page.locator('#days tr').filter({hasText:'无库存数据'}).count(),4);
  await page.locator('[name=mode]').selectOption('protected_upgrade');
  assert.equal(await page.locator('#days tr').count(),0);
  await page.locator('#view-form button').click();await page.locator('#days tr').nth(13).waitFor();
  assert.match(await page.locator('#status').innerText(),/protected_upgrade/);
  await page.locator('[name=check_in]').fill('2026-12-07');await page.locator('[name=check_out]').fill('2026-12-09');
  await page.locator('#preview-form button').click();await page.waitForFunction(()=>document.getElementById('preview').textContent.includes('当前快照可分配'));
  await page.locator('[name=adults]').fill('99');await page.locator('#preview-form button').click();await page.waitForFunction(()=>document.getElementById('preview').textContent.includes('当前不可分配'));
  assert.match(await page.locator('#preview').innerText(),/人数或儿童/);
  // An older delayed response cannot repaint after a newer input change.
  let release;const gate=new Promise(resolve=>{release=resolve;});
  await page.route('**/api/availability?**',async route=>{await gate;await route.continue();});
  await page.locator('#view-form button').click();await page.locator('[name=start]').fill('2027-01-01');release();await page.unrouteAll({behavior:'wait'});
  assert.equal(await page.locator('#days tr').count(),0);
  await page.locator('#view-form button').click();await page.locator('#days tr').nth(13).waitFor();
  assert.equal(await page.locator('#days tr').filter({hasText:'无库存数据'}).count(),14);
  assert.deepEqual(writes,[]);assert.deepEqual(errors,[]);
  fs.mkdirSync('results',{recursive:true});await page.screenshot({path:'results/availability-browser.png',fullPage:true});
  console.log('PASS real Chromium: 14-day/unknown inventory, strategy refresh, accepted/rejected preview, stale-response guard, outside window, zero POSTs, zero JS errors');
 } finally {if(browser)await browser.close();server.kill();}
})().catch(error=>{console.error(error);process.exitCode=1;});
