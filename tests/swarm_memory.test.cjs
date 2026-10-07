const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

test('memory inspector performs read-only scoped retrieval and renders provenance as text', async () => {
  const calls = [];
  const nodes = {
    'knowledge-project': {value: 'demo/api'},
    'knowledge-result': {textContent: ''},
  };
  const context = {
    window: {},
    document: {getElementById: id => nodes[id], addEventListener() {}},
    fetch: async (url, options) => {
      calls.push({url, options});
      return {ok: true, json: async () => ({items: [{id: 'r1', status: 'candidate', body: '<script>bad()</script>'}], revision_manifest: ['r1'], omissions: []})};
    },
  };
  vm.runInNewContext(fs.readFileSync('whilly/api/static/product-swarm.js', 'utf8'), context);
  assert.equal(typeof context.window.ProductSwarm.loadKnowledge, 'function');
  await context.window.ProductSwarm.loadKnowledge();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, '/api/v1/swarm/knowledge?project_id=demo%2Fapi&max_chars=12000');
  assert.equal(calls[0].options.method, undefined);
  const rendered = JSON.parse(nodes['knowledge-result'].textContent);
  assert.equal(rendered.items[0].status, 'candidate');
  assert.equal(rendered.items[0].body, '<script>bad()</script>');
  assert.deepEqual(rendered.revision_manifest, ['r1']);
});

test('memory inspector renders empty, unavailable and ready backend status as text', async () => {
  const nodes = {
    'knowledge-project': {value: 'demo/api'},
    'knowledge-result': {textContent: ''},
    'knowledge-status': {textContent: ''},
  };
  const statuses = [
    {backend: 'l1', ready: true, last_elapsed_ms: null, error_code: null},
    {backend: 'cognee', ready: false, last_elapsed_ms: null, error_code: 'models_missing'},
    {backend: 'cognee', ready: true, last_elapsed_ms: 4, error_code: null},
  ];
  let index = 0;
  const context = {
    window: {},
    document: {getElementById: id => nodes[id], addEventListener() {}},
    fetch: async url => ({ok: true, json: async () => url.endsWith('/status') ? statuses[index++] : {items: []}}),
  };
  vm.runInNewContext(fs.readFileSync('whilly/api/static/product-swarm.js', 'utf8'), context);
  await context.window.ProductSwarm.loadKnowledgeStatus();
  assert.match(nodes['knowledge-status'].textContent, /ready/);
  await context.window.ProductSwarm.loadKnowledgeStatus();
  assert.match(nodes['knowledge-status'].textContent, /models_missing/);
  await context.window.ProductSwarm.loadKnowledgeStatus();
  assert.match(nodes['knowledge-status'].textContent, /4/);
});
