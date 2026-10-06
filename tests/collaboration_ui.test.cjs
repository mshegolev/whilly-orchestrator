const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

test('collaboration inspector renders data as text and performs only reads', async () => {
  const nodes = {'collaboration-status': {}, 'collaboration-result': {}};
  const calls = [];
  const window = {};
  const body = '<img src=x onerror=alert(1)>';
  vm.runInNewContext(fs.readFileSync('whilly/api/static/product-swarm.js', 'utf8'), {
    window, document: {addEventListener() {}, getElementById(id) {return nodes[id];}},
    fetch: async (path, options) => {
      calls.push([path, options.method || 'GET']);
      return {ok: true, json: async () => path.endsWith('/status')
        ? {configured: false, blocker: 'message_delivery_policy_required'}
        : {messages: [{recipient_project: 'library', recipient_role: 'reviewer', state: 'persisted', payload: {body}, evidence_refs: ['git:contract']}]}};
    },
  });
  await window.ProductSwarm.loadCollaboration();
  assert.match(nodes['collaboration-status'].textContent, /message_delivery_policy_required/);
  assert.match(nodes['collaboration-result'].textContent, /persisted/);
  assert.match(nodes['collaboration-result'].textContent, /git:contract/);
  assert.ok(nodes['collaboration-result'].textContent.includes(body));
  assert.equal(nodes['collaboration-result'].innerHTML, undefined);
  assert.ok(calls.every(([, method]) => method === 'GET'));
});
