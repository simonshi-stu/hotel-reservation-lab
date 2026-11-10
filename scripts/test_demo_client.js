/**
 * Targeted hotel demo client logic checks. Run: node scripts/test_demo_client.js
 * Uses Node built-ins only. Runs the actual app.js in a VM with a minimal DOM
 * stub and controlled HTTP responses. This is NOT a real-browser E2E, visual,
 * accessibility, network, concurrency, or performance test.
 * The test-only VM export replaces the final startup calls in memory; source
 * files and databases are not modified. Requires Node.js 18+.
 */
const fs = require('fs');
const vm = require('vm');
const assert = require('assert/strict');
const {webcrypto} = require('crypto');
const path = require('path');
const root = path.join(__dirname, '..', 'hotel_demo', 'static') + path.sep;
console.log(`Hotel demo client logic checks | Node ${process.version}`);
console.log('Scope: actual app.js + minimal DOM stub + mocked HTTP; NOT real-browser E2E.');
const html = fs.readFileSync(root+'index.html','utf8');
const original = fs.readFileSync(root+'app.js','utf8');
const source = original.replace(/  updateControls\(\);\n  readState\(\);\n\}\)\(\);\s*$/, `
  globalThis.testAPI = {
    perform, readState, updateControls, renderReceipt,
    recoverArchived: index => recoverArchived(remembered.archived[index]),
    snapshot: () => JSON.parse(JSON.stringify({remembered, mode, state, busy, loading, stale})),
    setMode: value => { mode = value; remembered.selectedMode = value; persist(); },
    setState: value => { state = value; stale = false; },
    busy: () => busy,
    storageKey
  };
})();`);
assert.notEqual(source, original);
class Element {
 constructor(tag='div') {this.tag=tag;this.children=[];this.attrs={};this.dataset={};this.listeners={};this.className='';this._value='';this._text='';this.checked=false;this.hidden=false;this.disabled=false;this.classList={toggle:(name,value)=>{const parts=new Set(this.className.split(' ').filter(Boolean));if(value)parts.add(name);else parts.delete(name);this.className=[...parts].join(' ');}};}
 get value(){return this._value;}
 set value(v){this._value=String(v);}
 set textContent(value){this._text=String(value);this.children=[];}
 get textContent(){return this._text+this.children.map(n=>n.textContent).join('');}
 append(...children){children.forEach(c=>{if(typeof c==='string'){const n=new Element('#text');n.textContent=c;c=n;}c.parentElement=this;this.children.push(c);});}
 replaceChildren(...children){this.children=[];this._text='';this.append(...children);if(this.tag==='select'&&children.length)this.value=children[0].value;}
 setAttribute(key,value){this.attrs[key]=String(value);}
 addEventListener(kind,fn){(this.listeners[kind]??=[]).push(fn);}
 async emit(kind,event={}){for(const fn of this.listeners[kind]||[])await fn({preventDefault(){},...event});}
 showModal(){this.open=true;}
}
const seed = selectedMode => ({generation:'round-1',mode:selectedMode,catalog:{horizon_start:'2026-12-07',horizon_end:'2026-12-09',cot_age_limit:2,room_types:[{code:'standard',rank:0,max_occupants:3,max_adults:2,base_rate_cents:18000,max_cots:1,max_extra_beds:1}],rooms:[{room_id:'S101',room_type:'standard',bed_type:'king',fixed_bed_places:2,blocked_dates:[]}]},inventory:{occupied_room_nights:0,occupancy:[],daily:['2026-12-07','2026-12-08'].map(date=>({date,available_by_type:{standard:1},cots_used:0,cots_capacity:2,extra_beds_used:0,extra_beds_capacity:2}))},bookings:[],audit:{healthy:true,verification_complete:true,issue_count:0,active_room_nights:0,events:0,sqlite_version:'test'}});
const payload = selectedMode => ({mode:selectedMode,generation:'round-1',request:{request_id:'DEMO-original-'+selectedMode,check_in:'2026-12-07',check_out:'2026-12-08',requested_type:'standard',accepted_bed_types:['king'],adults:2,child_ages:[],allow_upgrade:true,allow_extra_bed:true,max_upgrade_cost_cents:60000},simulate_failure:false});
const success = (p,status='BOOKED',kind='reserve')=>({generation:p.generation,mode:p.mode,request_id:p.request_id||p.request.request_id,status,record:{request_id:p.request_id||p.request.request_id,status,original_decision:{accepted:true,room_id:'S101',assigned_type:'standard',bed_type:'king',nights:['2026-12-07'],cots:0,extra_beds:0,upgraded:false}},trace:[{code:'COMMIT',label:'提交'}],audit:seed(p.mode).audit,inventory_delta_nights:kind==='cancel'?-1:1,replayed:true});
const response=(body,status=200)=>({ok:status<400,status,json:async()=>structuredClone(body)});
function setup(storage=new Map()) {
 const ids=new Map(),all=[];
 for(const match of html.matchAll(/<([a-z0-9-]+)\b([^>]*)>/g)){
  const tag=match[1],attrs=match[2],n=new Element(tag);all.push(n);
  for(const a of attrs.matchAll(/([a-z0-9_-]+)(?:="([^"]*)")?/g)){
   const key=a[1],v=a[2]??'';n.attrs[key]=v;
   if(key==='id'){n.id=v;ids.set(v,n);}if(key==='class')n.className=v;if(key==='name')n.name=v;if(key==='value')n.value=v;if(key==='content')n.content=v;if(key==='checked')n.checked=true;if(key==='hidden')n.hidden=true;if(key==='disabled')n.disabled=true;
  }
 }
 ids.forEach(n=>{n.parentElement=new Element();});
 const flatten=()=>{const out=[];const seen=new Set();function walk(n){if(seen.has(n))return;seen.add(n);out.push(n);n.children.forEach(walk);}all.forEach(walk);return out;};
 const doc={body:new Element('body'),getElementById:id=>{assert(ids.has(id),'missing id '+id);return ids.get(id);},createElement:tag=>new Element(tag),createTextNode:text=>{const n=new Element('#text');n.textContent=text;return n;},querySelector:sel=>{if(sel==='meta[name="demo-csrf"]')return all.find(n=>n.tag==='meta'&&n.name==='demo-csrf');throw Error('unsupported '+sel);},querySelectorAll:sel=>{if(sel==='input[name="mode"]')return all.filter(n=>n.tag==='input'&&n.name==='mode');if(sel==='input[name="bed_type"]')return all.filter(n=>n.tag==='input'&&n.name==='bed_type');if(sel==='input[name="bed_type"]:checked')return all.filter(n=>n.tag==='input'&&n.name==='bed_type'&&n.checked);if(sel.startsWith('.'))return flatten().filter(n=>n.className.split(' ').includes(sel.slice(1)));throw Error('unsupported '+sel);}};
 let postHandler=async()=>{throw Error('missing post stub');},getHandler=async mode=>response(seed(mode));const calls=[];
 const context={console,Date,Map,Set,JSON,Number,String,Boolean,Array,Error,RegExp,AbortController,setTimeout,clearTimeout,crypto:webcrypto,document:doc,sessionStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v)},fetch:async(url,opts)=>{const call={url,opts,payload:opts.body?JSON.parse(opts.body):null};calls.push(call);return opts.method==='POST'?postHandler(call):getHandler(new URL(url,'http://localhost').searchParams.get('mode'),call);}};
 vm.createContext(context);vm.runInContext(source,context);return {api:context.testAPI,ids,doc,storage,calls,setPost:fn=>postHandler=fn,setGet:fn=>getHandler=fn};
}
const equal=(a,b)=>assert.deepEqual(JSON.parse(JSON.stringify(a)),JSON.parse(JSON.stringify(b)));
const tick=()=>new Promise(r=>setTimeout(r,0));
const watchdog = setTimeout(() => { console.error('FAIL: client logic checks exceeded 10 seconds.'); process.exit(1); }, 10000);
(async()=>{
 let t=setup();t.api.setState(seed('upgrade'));t.setPost(async()=>{throw Error('connection interrupted');});
 await t.api.perform('reserve',payload('upgrade'));
 equal(t.api.snapshot().remembered.pending.upgrade.payload,payload('upgrade'));
 assert(t.ids.get('request-fields').disabled);assert(t.ids.get('reset-button').disabled);
 let reloaded=setup(t.storage);assert.equal(reloaded.api.snapshot().mode,'upgrade');reloaded.api.setState(seed('upgrade'));
 reloaded.setPost(async c=>response(success(c.payload)));
 await reloaded.ids.get('pending-retry').emit('click');while(reloaded.api.busy())await tick();
 equal(reloaded.calls.find(c=>c.payload).payload,payload('upgrade'));assert(!reloaded.api.snapshot().remembered.pending.upgrade);
 console.log('PASS network failure -> stored original payload -> reload -> same-key recovery');
 t=setup();t.api.setMode('strict');t.api.setState(seed('strict'));t.setPost(async()=>response({error:{code:'RESULT_UNCONFIRMED',message:'unknown'}},500));
 await t.api.perform('reserve',payload('strict'));
 reloaded=setup(t.storage);assert.equal(reloaded.api.snapshot().mode,'strict');
 reloaded.api.setState(seed('strict'));reloaded.setPost(async()=>response({error:{code:'DATABASE_BUSY',message:'busy'}},503));
 await reloaded.api.perform('reserve',payload('strict'));assert.equal(reloaded.api.snapshot().remembered.pending.strict.uncertain,true);
 console.log('PASS 500 in strict -> reload selects strict; subsequent 503 does not erase unknown outcome');
 t=setup();t.api.setState(seed('upgrade'));t.setPost(async()=>response({error:{code:'DATABASE_BUSY',message:'busy'}},503));
 await t.api.perform('reserve',payload('upgrade'));assert.equal(t.api.snapshot().remembered.pending.upgrade.uncertain,false);assert(t.ids.get('request-fields').disabled);assert.match(t.ids.get('pending-title').textContent,/繁忙/);
 t.setPost(async c=>response(success(c.payload)));await t.ids.get('pending-retry').emit('click');while(t.api.busy())await tick();assert(!t.api.snapshot().remembered.pending.upgrade);
 console.log('PASS first 503 -> exact retry body retained, distinct known-retry status -> successful recovery');
 const cancel={mode:'upgrade',generation:'round-1',request_id:'DEMO-cancel-target'};
 t=setup();t.api.setState(seed('upgrade'));t.setPost(async()=>{throw Error('cancel lost');});await t.api.perform('cancel',cancel);reloaded=setup(t.storage);reloaded.api.setState(seed('upgrade'));reloaded.setPost(async c=>response(success(c.payload,'CANCELLED','cancel')));await reloaded.ids.get('pending-retry').emit('click');while(reloaded.api.busy())await tick();equal(reloaded.calls.find(c=>c.payload).payload,cancel);assert(!reloaded.api.snapshot().remembered.pending.upgrade);
 console.log('PASS unknown cancellation -> reload -> same cancel payload -> confirmed CANCELLED');
 t=setup();t.api.setState(seed('upgrade'));t.setPost(async()=>{throw Error('cancel unknown');});await t.api.perform('cancel',cancel);t.setPost(async()=>response({error:{code:'STALE_ROUND',message:'stale'}},409));t.setGet(async m=>response({...seed(m),generation:'round-2'}));await t.api.perform('cancel',cancel);assert(!t.api.snapshot().remembered.pending.upgrade);equal(t.api.snapshot().remembered.archived[0].payload,cancel);assert(!t.ids.get('archived-box').hidden);assert(!t.ids.get('request-fields').disabled);reloaded=setup(t.storage);equal(reloaded.api.snapshot().remembered.archived[0].payload,cancel);
 console.log('PASS unknown cancel + STALE_ROUND -> archived complete original body survives reload; active round recovers');
 const reset={mode:'strict',generation:'round-1'};
 t=setup();t.api.setMode('strict');t.api.setState(seed('strict'));t.setPost(async()=>{throw Error('reset unknown');});await t.api.perform('reset',reset);t.api.setMode('upgrade');t.api.setState(seed('upgrade'));t.api.updateControls();assert(t.ids.get('request-fields').disabled);assert(t.ids.get('replay-button').disabled);assert(t.ids.get('reset-button').disabled);assert.match(t.ids.get('pending-description').textContent,/严格房型/);
 reloaded=setup(t.storage);assert.equal(reloaded.api.snapshot().mode,'strict');
 t.setPost(async c=>response({error:{code:'STALE_ROUND',message:'stale'}},409));t.setGet(async m=>response({...seed(m),generation:'round-2'}));await t.ids.get('pending-retry').emit('click');while(t.api.busy())await tick();equal(t.calls.filter(c=>c.payload).at(-1).payload,reset);assert.equal(Object.keys(t.api.snapshot().remembered.pending).length,0);assert.equal(t.api.snapshot().state.mode,'upgrade');assert(!t.ids.get('request-fields').disabled);
 console.log('PASS reset unknown blocks all strategies; reload returns source; original-generation stale retry recovers current view');
 t=setup();t.api.setState(seed('upgrade'));t.setPost(async()=>{throw Error('unknown');});await t.api.perform('reserve',payload('upgrade'));t.api.setMode('strict');t.setGet(async m=>response({...seed(m),generation:'round-2'}));await t.api.readState();assert.equal(t.api.snapshot().remembered.archived.length,1);t.api.setMode('upgrade');await t.api.readState();assert(!t.ids.get('archived-box').hidden);assert(t.ids.get('replay-button').disabled);
 console.log('PASS state refresh in another strategy reconciles global generation without reusing archived request');
 t=setup();let oldResolve;t.setGet((m)=>m==='upgrade'?new Promise(r=>oldResolve=r):Promise.resolve(response(seed(m))));const old=t.api.readState();t.api.setMode('strict');await t.api.readState();oldResolve(response(seed('upgrade')));await old;assert.equal(t.api.snapshot().state.mode,'strict');
 console.log('PASS late old GET cannot overwrite newer selected strategy');
 t=setup();t.api.setState(seed('upgrade'));let resolvePost;t.setPost(()=>new Promise(r=>resolvePost=r));const inflight=t.api.perform('reserve',payload('upgrade'));await t.api.perform('reserve',{...payload('upgrade'),request:{...payload('upgrade').request,request_id:'DEMO-second'}});assert.equal(t.calls.filter(c=>c.payload).length,1);resolvePost(response(success(payload('upgrade'))));await inflight;
 console.log('PASS repeated submission while in flight sends one POST');
 t=setup();t.api.setState(seed('upgrade'));t.setPost(async()=>response({error:{code:'STALE_ROUND',message:'stale'}},409));t.setGet(async m=>response({...seed(m),generation:'round-2'}));await t.api.perform('reserve',payload('upgrade'));assert(!t.api.snapshot().remembered.pending.upgrade);assert.equal(t.api.snapshot().remembered.archived.length,0);equal(t.api.snapshot().remembered.last.upgrade,payload('upgrade'));assert(t.ids.get('replay-button').disabled);assert(!t.ids.get('request-fields').disabled);
 console.log('PASS first known STALE_ROUND -> no false unknown archive, original reserve retained, new generation usable');
 t=setup();t.api.setMode('strict');t.api.setState(seed('strict'));t.setPost(async()=>{throw Error('reset not delivered');});await t.api.perform('reset',reset);t.api.setMode('upgrade');t.api.setState(seed('upgrade'));t.setPost(async c=>response({...seed(c.payload.mode),generation:'round-2'}));t.setGet(async m=>response({...seed(m),generation:'round-2'}));await t.ids.get('pending-retry').emit('click');while(t.api.busy())await tick();equal(t.calls.filter(c=>c.payload).at(-1).payload,reset);assert.equal(t.api.snapshot().state.mode,'upgrade');assert.equal(t.api.snapshot().state.generation,'round-2');assert.equal(Object.keys(t.api.snapshot().remembered.pending).length,0);
 console.log('PASS uncommitted reset retry succeeds from another strategy without replacing its view with source strategy');
 t=setup();t.api.setState(seed('upgrade'));t.setPost(async()=>response({error:{code:'DATABASE_BUSY',message:'busy'}},503));await t.api.perform('cancel',cancel);equal(t.api.snapshot().remembered.pending.upgrade.payload,cancel);assert.equal(t.api.snapshot().remembered.pending.upgrade.uncertain,false);reloaded=setup(t.storage);reloaded.api.setState(seed('upgrade'));reloaded.setPost(async c=>response(success(c.payload,'CANCELLED','cancel')));await reloaded.ids.get('pending-retry').emit('click');while(reloaded.api.busy())await tick();equal(reloaded.calls.find(c=>c.payload).payload,cancel);
 console.log('PASS cancellation 503 retains exact body across reload and retries cancellation, not reservation');
 t=setup();t.api.setState(seed('upgrade'));t.setPost(async c=>response({...success(c.payload),request_id:'DEMO-wrong'}));await t.api.perform('reserve',payload('upgrade'));assert.equal(t.api.snapshot().remembered.pending.upgrade.uncertain,true);equal(t.api.snapshot().remembered.pending.upgrade.payload,payload('upgrade'));assert.equal(t.ids.get('receipt-status').textContent,'结果未确认');
 console.log('PASS mismatched receipt identity is unconfirmed; original payload retained');
 t=setup();t.api.setState(seed('upgrade'));t.setPost(async()=>response({error:{code:'CSRF_REQUIRED',message:'refresh'}},403));await t.api.perform('reserve',payload('upgrade'));assert(!t.api.snapshot().remembered.pending.upgrade);assert.equal(t.ids.get('receipt-status').textContent,'等待请求');
 console.log('PASS known rejected HTTP request does not leave stale processing status');

 const recoveryResult=(p,kind='reserve',status='BOOKED')=>({
   mode:p.mode,generation:p.generation,current_generation:'round-2',request_id:p.request_id||p.request?.request_id,
   operation:kind,status,outcome:status==='NOT_FOUND'?'NOT_RECORDED':kind==='reserve'?'RECORDED':status==='CANCELLED'?'CANCELLED':'NOT_CANCELLED',
   record:status==='NOT_FOUND'?null:{request_id:p.request_id||p.request?.request_id,status,original_decision:{accepted:status!=='REJECTED'}},
   audit:seed(p.mode).audit,read_only:true,snapshot_only:true,archived:true,observed_at:'2026-11-08T00:00:00.000Z'
 });
 const archiveStorage=(kind='reserve',p=payload('upgrade'))=>new Map([['hotel-room-lab:retry:v1',JSON.stringify({last:{},pending:{},selectedMode:'upgrade',archived:[{kind,payload:p,uncertain:true}]})]]);
 t=setup(archiveStorage());t.api.setState({...seed('upgrade'),generation:'round-2'});
 t.setPost(async c=>response(recoveryResult(c.payload.payload)));
 await t.api.recoverArchived(0);
 assert.equal(t.calls.length,1);assert.equal(t.calls[0].url,'/api/lookup');equal(t.calls[0].payload,{kind:'reserve',payload:payload('upgrade')});
 assert.equal(t.api.snapshot().state.generation,'round-2');assert.equal(t.api.snapshot().remembered.archived[0].recovery.outcome,'RECORDED');
 equal(t.api.snapshot().remembered.archived[0].payload,payload('upgrade'));
 reloaded=setup(t.storage);assert.equal(reloaded.api.snapshot().remembered.archived[0].recovery.status,'BOOKED');
 console.log('PASS archived reservation lookup sends only lookup with original identity; receipt persists without changing current round');
 t=setup(archiveStorage('cancel',cancel));t.api.setState({...seed('upgrade'),generation:'round-2'});
 t.setPost(async c=>response(recoveryResult(c.payload.payload,'cancel','CANCELLED')));
 await t.api.recoverArchived(0);assert.equal(t.calls[0].url,'/api/lookup');
 assert.equal(t.api.snapshot().remembered.archived[0].recovery.outcome,'CANCELLED');assert.match(t.ids.get('archived-list').textContent,/已取消/);
 console.log('PASS archived cancellation lookup displays confirmed status without issuing cancellation');
 t=setup(archiveStorage('cancel',cancel));t.setPost(async c=>response(recoveryResult(c.payload.payload,'cancel','BOOKED')));
 await t.api.recoverArchived(0);assert.equal(t.api.snapshot().remembered.archived[0].recovery.outcome,'NOT_CANCELLED');
 assert.match(t.ids.get('archived-list').textContent,/没有已提交的取消记录/);
 console.log('PASS still-booked cancellation target is not falsely called cancelled');
 t=setup(archiveStorage());t.setPost(async c=>response(recoveryResult(c.payload.payload,'reserve','NOT_FOUND')));
 await t.api.recoverArchived(0);assert.equal(t.api.snapshot().remembered.archived[0].recovery.outcome,'NOT_RECORDED');
 assert.match(t.ids.get('archived-list').textContent,/不证明请求从未执行/);assert.equal(t.calls.length,1);
 console.log('PASS missing record is a snapshot observation, never an automatic replacement reservation');
 t=setup(archiveStorage());t.setPost(async()=>response({error:{code:'RECOVERY_UNAVAILABLE',message:'archive missing'}},409));
 await t.api.recoverArchived(0);assert(!t.api.snapshot().remembered.archived[0].recovery);
 equal(t.api.snapshot().remembered.archived[0].payload,payload('upgrade'));assert.match(t.api.snapshot().remembered.archived[0].recoveryError,/原请求仍保留/);
 t.setPost(async()=>{throw Error('network interrupted');});await t.api.recoverArchived(0);
 assert(!t.api.snapshot().remembered.archived[0].recovery);assert.equal(t.calls.filter(c=>c.url!=='/api/lookup').length,0);
 console.log('PASS archive missing or network failure retains unresolved original request and never writes');
 for(const change of [{request_id:'wrong'},{generation:'wrong'},{outcome:'CANCELLED'},{read_only:false},{audit:{healthy:false,verification_complete:true}},{status:'NOT_FOUND',record:{request_id:'wrong'}}]){
   t=setup(archiveStorage());t.setPost(async c=>response({...recoveryResult(c.payload.payload),...change}));
   await t.api.recoverArchived(0);assert(!t.api.snapshot().remembered.archived[0].recovery);assert(t.api.snapshot().remembered.archived[0].recoveryError);
 }
 console.log('PASS mismatched identity, outcome, write flag or failed audit cannot become a recovery confirmation');
 t=setup(archiveStorage());let resolveLookup;t.setPost(()=>new Promise(r=>resolveLookup=r));
 const first=t.api.recoverArchived(0);await t.api.recoverArchived(0);assert.equal(t.calls.length,1);
 resolveLookup(response(recoveryResult(payload('upgrade'))));await first;assert(!t.api.busy());
 console.log('PASS repeated recovery clicks send one read-only lookup and restore controls');
 t=setup(archiveStorage());t.setPost(async c=>response(recoveryResult(c.payload.payload)));await t.api.recoverArchived(0);
 t.setPost(async()=>{throw Error('later failure');});await t.api.recoverArchived(0);
 assert.equal(t.api.snapshot().remembered.archived[0].recovery.observed_at,'2026-11-08T00:00:00.000Z');
 assert.match(t.ids.get('archived-list').textContent,/本次查询未完成/);
 console.log('PASS failed refresh keeps timestamped prior snapshot and separately marks current query failure');

 console.log('ALL 22 TARGETED FRONTEND LOGIC CHECKS PASSED (DOM stub, not a visual browser test)');
})().catch(e=>{console.error(e);process.exitCode=1;}).finally(() => clearTimeout(watchdog));
