const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function browser(count = 3) {
  const listeners = {}, nodes = [];
  let focused = null;
  const controls = Array.from({length:count}, (_, i) => ({
    dataset: {}, tabIndex: 0, hidden: false, isConnected: true,
    getBoundingClientRect: () => ({left:10, right:40, top:20+i, bottom:40+i, width:30, height:20}),
    matches() { return this.disabled || false; }, closest() { return this.hidden; },
    focus() { focused = this; }, click() { throw Error('Hints must never activate controls'); },
  }));
  const body = {append(node) {nodes.push(node);}};
  const document = {
    body, activeElement: body,
    querySelector: () => null, querySelectorAll: () => controls,
    createElement: () => ({dataset:{},style:{},setAttribute(){},remove(){nodes.splice(nodes.indexOf(this),1);}}),
    addEventListener: (name, fn, capture) => {listeners[name] = fn; if(name==='keydown') assert.equal(capture,true);},
  };
  vm.runInNewContext(fs.readFileSync('whilly/api/static/whilly-navigation.js','utf8'), {
    window:{addEventListener(){}}, document, innerWidth:1000, innerHeight:1000,
    getComputedStyle: () => ({visibility:'visible'}),
  });
  function key(key, extra={}) {
    const event = {key, target:{closest:()=>null}, prevented:false, stopped:false,
      preventDefault(){this.prevented=true;},stopImmediatePropagation(){this.stopped=true;},...extra};
    listeners.keydown(event); return event;
  }
  return {key,nodes,controls,listeners,focus:()=>focused};
}
test('hint selection captures action keys and only focuses, never clicks',()=>{
  const b=browser(); b.key('f'); assert.equal(b.nodes.length,3);
  const event=b.key('a'); assert.equal(b.focus(),b.controls[0]);
  assert.equal(event.stopped,true); assert.equal(event.prevented,true); assert.equal(b.nodes.length,0);
});
test('text entry, browser modifiers, IME and repeat remain untouched',()=>{
  for(const extra of [{ctrlKey:true},{altKey:true},{metaKey:true},{isComposing:true},{repeat:true},
    {target:{closest:()=>({})}}]) {
    const b=browser(); assert.equal(b.key('f',extra).prevented,false); assert.equal(b.nodes.length,0);
  }
});
test('hidden and disabled controls are not labeled; escape and swap clear hints',()=>{
  const b=browser(); b.controls[0].disabled=true; b.controls[1].hidden=true;
  b.key('f'); assert.equal(b.nodes.length,1); b.key('Escape'); assert.equal(b.nodes.length,0);
  b.key('f'); b.listeners['htmx:beforeSwap'](); assert.equal(b.nodes.length,0);
  b.key('f'); b.key('a'); assert.equal(b.focus(),b.controls[2]);
});
test('large pages get unique fixed-width labels and consume partial labels safely',()=>{
  const b=browser(40); b.key('f'); const labels=b.nodes.map(n=>n.textContent);
  assert.equal(new Set(labels).size,40); assert.ok(labels.every(label=>label.length===2));
  assert.equal(b.key('a').stopped,true); assert.equal(b.focus(),null);
  b.key('a'); assert.equal(b.focus(),b.controls[0]);
});

test('Tab and explicit activation keys close hints without swallowing native behavior',()=>{
  for (const key of ['Tab','Enter',' ']) {
    const b=browser(); b.key('f'); const event=b.key(key);
    assert.equal(b.nodes.length,0); assert.equal(event.prevented,false);
    assert.equal(b.focus(),null);
    if (key === 'Tab') assert.equal(event.stopped,false);
    else assert.equal(event.stopped,true);
  }
});
