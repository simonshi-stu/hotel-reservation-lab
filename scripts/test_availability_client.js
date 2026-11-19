// Actual availability.js with minimal DOM and controlled HTTP, NOT browser/visual QA.
const vm=require('vm'),fs=require('fs'),assert=require('assert/strict');
class Element {
 constructor(){this.children=[];this.listeners={};this.value='';this._text='';this.elements={};}
 addEventListener(name,fn){this.listeners[name]=fn;}
 replaceChildren(){this.children=[];}
 append(child){this.children.push(child);}
 set textContent(value){this._text=value;this.children=[];}
 get textContent(){return this._text;}
}
const ids=Object.fromEntries(['view-form','preview-form','status','preview','days'].map(id=>[id,new Element()]));
const form=ids['view-form'],proposal=ids['preview-form'];
for(const [name,value] of Object.entries({mode:'strict',start:''})){form.elements[name]=new Element();form.elements[name].value=value;}
for(const name of ['room_type','bed','check_in','check_out','adults'])proposal.elements[name]=new Element();
const pending=[];
const context={document:{getElementById:id=>ids[id],createElement:()=>new Element()},URLSearchParams,
 FormData:class{constructor(form){this.entries=Object.entries(form.elements).map(([key,v])=>[key,v.value]);}[Symbol.iterator](){return this.entries[Symbol.iterator]();}},
 fetch:url=>new Promise(resolve=>pending.push({url,resolve}))};
vm.runInNewContext(fs.readFileSync('hotel_demo/static/availability.js','utf8'),context);
const sample={snapshot_at:'2026-11-19',mode:'strict',horizon_start:'2026-12-07',horizon_end:'2026-12-17',room_types:['standard'],bed_types:['king'],days:Array.from({length:14},(_,i)=>({date:String(i),available:i<10,types:[{room_type:'standard',remaining:2,active_protection:0,configured_protection:0}],extra_beds_remaining:1,cots_remaining:1})),preview:null};
async function flush(){for(let i=0;i<10;i++)await Promise.resolve();}
async function respond(item,data=sample,ok=true){item.resolve({ok,json:async()=>data});await flush();}
function submit(form){form.listeners.submit({preventDefault(){}});}
(async()=>{
 await respond(pending.shift());assert.equal(ids.days.children.length,14);assert.equal(ids.days.children[10].children[1].textContent,'无库存数据');
 submit(form);const old=pending.shift();form.elements.mode.value='upgrade';form.listeners.input();submit(form);const current=pending.shift();
 await respond(current,{...sample,mode:'upgrade'});await respond(old);assert.match(ids.status.textContent,/upgrade/);
 proposal.elements.adults.value='99';submit(proposal);await respond(pending.shift(),{...sample,preview:{accepted:false,rejection_reasons:['PARTY_OR_CHILD_REQUIREMENTS_UNSUPPORTED']}});assert.match(ids.preview.textContent,/人数或儿童/);
 submit(form);await respond(pending.shift(),{error:{message:'invalid date'}},false);assert.equal(ids.days.children.length,0);assert.match(ids.status.textContent,/invalid date/);assert.equal(ids.preview.textContent,'无有效试算结果');
 assert.ok(pending.length===0);console.log('PASS 4 DOM-stub cases: 14-day unknown cells, stale-response guard, rejection translation, error clears stale data');
})().catch(error=>{console.error(error);process.exitCode=1;});
