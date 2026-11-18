'use strict';
// Production composer, synthetic DOM/FileReader/canvas only; no upload/model call.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const fields = ['form', 'file', 'choose', 'zone', 'list', 'documentFile', 'documentChoose', 'documentList', 'evidence', 'links', 'vision', 'help', 'status'];
class Element {
  constructor() { this.children=[]; this.listeners={}; this.value=''; this.files=[]; }
  set textContent(value) { this.text=String(value); this.children=[]; }
  get textContent() { return (this.text || '') + this.children.map(c=>c.textContent || '').join(''); }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children=nodes; }
  addEventListener(name,fn) { this.listeners[name]=fn; }
  setAttribute(name,value) { this[name]=value; }
  querySelectorAll() { return []; }
  focus() {}
  get classList() { return { toggle() {},add() {},remove() {} }; }
}
function boot(cap={attachments:true,vision_available:true}) {
  const nodes=new Map(fields.map(k=>[k,new Element()])); let reads=0;
  const jpeg='data:image/jpeg;base64,'+Buffer.concat([Buffer.from([255,216,255]),Buffer.alloc(100*1024-3)]).toString('base64');
  const window={};
  const sandbox={window,TextEncoder,document:{getElementById:id=>nodes.get(id),createElement:tag=>tag==='canvas' ?
    {getContext:()=>({fillRect(){},drawImage(){}}),toDataURL:()=>jpeg}:new Element()},
    FileReader:class { readAsDataURL(file) { reads++; this.result='data:application/octet-stream;base64,'+Buffer.alloc(file.size,65).toString('base64'); this.onload(); } },
    Image:class { naturalWidth=100; naturalHeight=100; set src(_){this.onload();} }};
  vm.runInNewContext(fs.readFileSync(`${__dirname}/studio-intake.js`,'utf8'),sandbox);
  const composer=window.TravelIntake.createComposer({...Object.fromEntries(fields.map(k=>[k,k])),onChange(){},safeURL:v=>v});
  composer.update({busy:false,cap});
  return {composer,nodes,get reads(){return reads;},async add(files){nodes.get('documentFile').files=files; nodes.get('documentFile').listeners.change(); await new Promise(resolve=>setImmediate(resolve));}};
}
const file=(size,name='trip.txt',type='text/plain')=>({size,name,type});
test('100 KiB, 300 KiB and 2.5 MiB files survive base64 JSON without silent loss',async()=>{
  for(const size of [100*1024,300*1024,2.5*1024*1024]){
    const ui=boot(); await ui.add([file(size)]);
    const body=ui.composer.payload('travel brief');
    assert.equal(body.documents.length,1); assert.equal(Buffer.from(body.documents[0].data_url.split(',')[1],'base64').length,size);
  }
});
test('ordinary over-limit retains existing draft and rejects whole new batch',async()=>{
  const ui=boot(); await ui.add([file(100*1024)]); ui.nodes.get('evidence').value='retain note';
  await ui.add([file(2.5*1024*1024+1,'large.txt')]);
  assert.equal(ui.composer.payload().documents.length,1); assert.equal(ui.composer.payload().evidence_text,'retain note');
  assert.match(ui.nodes.get('status').textContent,/已有材料保留/);
});
test('combined data URL JSON budget rejects before replacing the draft',async()=>{
  const ui=boot(); await ui.add([file(100*1024)]);
  await ui.add(Array.from({length:4},(_,i)=>file(8*1024*1024,`${i}.zip`)));
  assert.equal(ui.composer.payload().documents.length,1);
  assert.match(ui.nodes.get('status').textContent,/总|编码|请求/);
});
test('three compressed images allowed; fourth rejected with existing images retained',async()=>{
  const ui=boot(); await ui.add([file(200000,'1.jpg','image/jpeg'),file(200000,'2.png','image/png'),file(200000,'3.webp','image/webp')]);
  assert.equal(ui.composer.payload().images.length,3);
  await ui.add([file(200000,'4.jpg','image/jpeg')]); assert.equal(ui.composer.payload().images.length,3);
});
test('attachments false blocks selection and queued event before reading bytes',async()=>{
  const ui=boot({attachments:false,vision_available:true});
  assert.equal(ui.nodes.get('documentChoose').disabled,true); assert.equal(ui.nodes.get('choose').disabled,true);
  await ui.add([file(100*1024)]); assert.equal(ui.reads,0); assert.equal(ui.composer.payload().documents.length,0);
  assert.match(ui.nodes.get('status').textContent,/附件|材料/);
});
test('server budget refresh cannot send an existing oversized draft',async()=>{
  const ui=boot(); await ui.add([file(300*1024)]);
  ui.composer.update({busy:false,cap:{attachments:true,vision_available:true,upload_limits:{request_body_bytes:256*1024}}});
  assert.throws(()=>ui.composer.payload(),/总|编码|请求/);
  ui.composer.update({busy:false,cap:{attachments:true,vision_available:true}});
  assert.equal(ui.composer.payload().documents.length,1);
});
test('unsupported attachment capability preserves an existing draft and blocks sending',async()=>{
  const ui=boot(); await ui.add([file(100*1024)]);
  ui.composer.update({busy:false,cap:{attachments:false,vision_available:false}});
  assert.throws(()=>ui.composer.payload('keep brief'),/已有材料草稿已保留/);
  ui.composer.update({busy:false,cap:{attachments:true,vision_available:true}});
  assert.equal(ui.composer.payload().documents.length,1);
});
