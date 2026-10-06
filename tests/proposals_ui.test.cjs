const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('whilly/api/static/product-swarm.js', 'utf8');

function harness(fetcher) {
  const nodes = {};
  function node() {
    return {value: '', textContent: '', disabled: false, children: [],
      replaceChildren() { this.children = []; this.value = ''; },
      append(child) { this.children.push(child); if (!this.value) this.value = child.value; },
      addEventListener() {}};
  }
  for (const id of ['proposal-list', 'proposal-detail', 'proposal-status', 'proposal-reason', 'proposal-accept-btn', 'proposal-reject-btn', 'proposal-feature-link', 'product-error', 'feature-list']) nodes[id] = node();
  const calls = [];
  const context = {window: {}, document: {getElementById: id => nodes[id], createElement: node, addEventListener() {}},
    fetch: async (url, options = {}) => { calls.push([url, options]); return {ok: true, json: async () => fetcher(url, options)}; }};
  vm.runInNewContext(source, context);
  return {api: context.window.ProductSwarm, nodes, calls};
}
const proposal = {id: 'p1', status: 'proposed', origin_feature_id: 'f1', target_project: 'demo', outcome: '<img src=x onerror=alert(1)>'};
const detail = {proposal, feature_revision: 4, evaluation: {status: 'eligible', reason: null}, events: []};

test('proposal inspector only reads and renders hostile content literally', async () => {
  const h = harness(url => url.endsWith('/proposals') ? {proposals: [proposal]} : detail);
  await h.api.loadProposals();
  assert.equal(h.calls.every(([, options]) => !options.method || options.method === 'GET'), true);
  assert.match(h.nodes['proposal-detail'].textContent, /<img src=x onerror=alert\(1\)>/);
  assert.equal(h.nodes['proposal-accept-btn'].disabled, false);
  assert.equal(h.nodes['proposal-feature-link'].href, '/swarm/product?feature=f1');
  assert.doesNotMatch(source, /innerHTML\s*=/);
});

test('accept for planning uses a bounded reason and displayed revision, never a run endpoint', async () => {
  const h = harness(url => url.endsWith('/proposals') ? {proposals: [proposal]} : detail);
  await h.api.loadProposals();
  h.nodes['proposal-reason'].value = 'Reviewed evidence';
  await h.api.decideProposal('accept');
  const mutations = h.calls.filter(([, options]) => options.method === 'POST');
  assert.equal(mutations.length, 1);
  assert.equal(mutations[0][0], '/api/v1/swarm/proposals/p1/accept');
  assert.deepEqual(JSON.parse(mutations[0][1].body), {expected_revision: 4, reason: 'Reviewed evidence'});
  h.nodes['proposal-reason'].value = '   ';
  await assert.rejects(() => h.api.decideProposal('accept'), /reason/i);
});

test('stale proposal response cannot enable decisions on a different proposal', async () => {
  let release;
  const h = harness(url => url.endsWith('/p1') ? new Promise(resolve => {release = resolve;}) : {
    ...detail, proposal: {...proposal, id: 'p2'}, evaluation: {status: 'blocked', reason: 'consumer_check_required'},
  });
  h.nodes['proposal-list'].value = 'p1';
  const stale = h.api.loadProposal('p1');
  await new Promise(resolve => setImmediate(resolve));
  h.nodes['proposal-list'].value = 'p2';
  await h.api.loadProposal('p2');
  release(detail); await stale;
  assert.match(h.nodes['proposal-status'].textContent, /consumer_check_required/);
  assert.equal(h.nodes['proposal-accept-btn'].disabled, true);
  assert.match(h.nodes['proposal-detail'].textContent, /p2/);
});

test('already planned work cannot be silently rejected outside feature revision workflow', async () => {
  const h = harness(() => ({...detail, proposal: {...proposal, status: 'awaiting_approval'}}));
  h.nodes['proposal-list'].value = 'p1';
  await h.api.loadProposal('p1');
  assert.equal(h.nodes['proposal-reject-btn'].disabled, true);
  assert.equal(h.nodes['proposal-accept-btn'].disabled, true);
});

test('terminal cockpit visibly distinguishes disabled actions', () => {
  const template = fs.readFileSync('whilly/api/templates/product_swarm.html.j2', 'utf8');
  assert.match(template, /button:disabled\s*\{[^}]*opacity:\s*\.5/);
});
