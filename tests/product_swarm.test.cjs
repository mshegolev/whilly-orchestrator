const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('whilly/api/static/product-swarm.js', 'utf8');

test('product cockpit exposes explicit request contracts and neutral text rendering', () => {
  assert.match(source, /method: 'POST'/);
  assert.match(source, /features\/\$\{encodeURIComponent\(id\)\}\/plan/);
  assert.match(source, /planner_profile/);
  assert.match(source, /features\/\$\{encodeURIComponent\(id\)\}\/run/);
  assert.match(source, /max_calls/);
  assert.match(source, /max_elapsed_seconds/);
  assert.match(source, /\/products\/default\/features/);
  assert.match(source, /\/products\/default\/messages/);
  assert.match(source, /\/products\/default\/discuss/);
  assert.match(source, /approval_digest/);
  assert.match(source, /features\/\$\{encodeURIComponent\(id\)\}\/publish/);
  assert.match(source, /features\/\$\{encodeURIComponent\(id\)\}\/publications/);
  assert.match(source, /startsWith\('https:\/\/'\)/);
  assert.match(source, /textContent/);
  assert.match(source, /JSON\.stringify\(spec, null, 2\)/);
  assert.match(source, /receipt\.mr_url/);
  assert.match(source, /feature\.session_id/);
  assert.match(source, /loadPublications\(id\)/);
  assert.doesNotMatch(source, /innerHTML\s*=/);
});

test('planning keeps the task text after Save message clears the editor', () => {
  assert.match(source, /let latestMessageText/);
  assert.match(source, /value\.trim\(\) \|\| latestMessageText/);
  assert.match(source, /if \(!id\) throw new Error\('Сначала сохраните задачу/);
});

test('saving the first task creates and selects a feature before planning', async () => {
  const elements = {
    'message-input': {value: 'Prepare all IC projects'},
    'message-history': {replaceChildren() {}, append() {}},
    'planning-note': {textContent: ''},
    'feature-list': {
      value: '',
      options: [],
      replaceChildren() { this.options = []; this.value = ''; },
      append(option) { this.options.push(option); if (!this.value && option.value) this.value = option.value; },
    },
    'feature-status': {textContent: ''},
    'run-status': {textContent: ''},
    'feature-spec': {textContent: ''},
    'feature-blocker': {textContent: ''},
    'budget-max-calls': {value: ''},
    'budget-max-elapsed': {value: ''},
    'approval-digest': {textContent: ''},
    'approval-revision': {value: ''},
    'feature-session-link': {href: '', hidden: true},
    'publication-receipts': {replaceChildren() {}, append() {}},
    'planner-profile': {value: 'planner-strong-claude', selectedOptions: [{value: 'planner-strong-claude', disabled: false}]},
    'plan-btn': {disabled: true},
    'discuss-btn': {disabled: true},
  };
  const events = [];
  const fetch = async (url, options = {}) => {
    events.push([url, options]);
    if (url.endsWith('/products/default/messages')) return {ok: true, json: async () => ({})};
    if (url.endsWith('/products/default/features')) return {ok: true, json: async () => ({id: 'f-new'})};
    if (url.endsWith('/features')) return {ok: true, json: async () => ({features: [{id: 'f-new', title: 'Prepare all IC projects'}]})};
    if (url.endsWith('/features/f-new/publications')) return {ok: true, json: async () => ({publications: []})};
    if (url.endsWith('/features/f-new')) return {ok: true, json: async () => ({id: 'f-new', status: 'draft', revision: 1, budget: {max_calls: 60, max_elapsed_seconds: 7200}})};
    throw new Error(`unexpected request: ${url}`);
  };
  const document = {
    activeElement: null,
    getElementById: id => elements[id],
    createElement: () => ({value: '', textContent: ''}),
    addEventListener() {},
  };
  const context = {window: {location: {search: '', href: 'http://localhost/swarm/product'}}, document, fetch};
  vm.runInNewContext(source, context);

  assert.equal(typeof context.window.ProductSwarm.saveTask, 'function');
  await context.window.ProductSwarm.saveTask();

  const create = events.find(([url]) => url.endsWith('/products/default/features'));
  assert.ok(create, 'the first saved task must create its feature');
  assert.deepEqual(JSON.parse(create[1].body), {
    title: 'Prepare all IC projects',
    intent: 'Prepare all IC projects',
  });
  assert.equal(elements['feature-list'].value, 'f-new');
  assert.equal(elements['plan-btn'].disabled, false);
});

test('new task resets the cockpit without creating an empty backend feature', () => {
  const elements = {
    'feature-list': {value: 'f-old'},
    'message-input': {value: 'old task', focusCalled: false, focus() { this.focusCalled = true; }},
    'feature-status': {textContent: ''},
    'run-status': {textContent: ''},
    'feature-spec': {textContent: ''},
    'feature-blocker': {textContent: ''},
    'approval-digest': {textContent: ''},
    'approval-revision': {value: '2'},
    'feature-session-link': {hidden: false},
    'planner-profile': {selectedOptions: [{value: 'planner-strong-claude', disabled: false}]},
    'plan-btn': {disabled: false},
    'discuss-btn': {disabled: false},
  };
  const document = {getElementById: id => elements[id], addEventListener() {}};
  const context = {window: {}, document, fetch: () => { throw new Error('new task must not call the backend'); }};
  vm.runInNewContext(source, context);

  assert.equal(typeof context.window.ProductSwarm.startNewTask, 'function');
  context.window.ProductSwarm.startNewTask();

  assert.equal(elements['feature-list'].value, '');
  assert.equal(elements['message-input'].value, '');
  assert.equal(elements['message-input'].focusCalled, true);
  assert.equal(elements['plan-btn'].disabled, true);
  assert.equal(elements['feature-session-link'].hidden, true);
});

test('DOM actions call the backend paths and payloads', async () => {
  const events = [];
  const elements = {};
  for (const id of ['feature-list', 'message-input', 'planner-profile', 'workers', 'budget-max-calls', 'budget-max-elapsed', 'new-feature', 'new-intent', 'product-error', 'approval-digest', 'approval-revision']) {
    elements[id] = {value: id === 'feature-list' ? 'f1' : id === 'planner-profile' ? 'planner-strong' : '', textContent: '', replaceChildren() {}, addEventListener(type, fn) { this[type] = fn; }};
  }
  for (const id of ['message-btn', 'discuss-btn', 'plan-btn', 'run-btn', 'budget-btn', 'approve-btn', 'stop-btn', 'publish-btn', 'create-feature-btn']) elements[id] = {addEventListener(type, fn) { this[type] = fn; }};
  elements['planner-profile'].querySelector = () => ({disabled: false}); elements['strong-profile-note'] = {hidden: false}; elements['feature-status'] = {textContent: ''}; elements['feature-spec'] = {textContent: ''}; elements['feature-blocker'] = {textContent: ''}; elements['approval-digest'] = {textContent: ''}; elements['approval-revision'] = {value: ''}; elements['budget-max-calls'] = {value: ''}; elements['budget-max-elapsed'] = {value: ''}; elements['planning-note'] = {textContent: ''}; elements['message-history'] = {replaceChildren() {}, append() {}}; elements['publication-receipts'] = {replaceChildren() {}, append() {}};
  const document = {getElementById: id => elements[id], addEventListener() {}};
  elements['message-input'].value = 'hello'; elements['new-feature'].value = 'Title'; elements['new-intent'].value = 'Intent';
  const fetch = async (url, options = {}) => { events.push([url, options]); return {ok: true, json: async () => ({features: [], messages: []})}; };
  const context = {window: {}, document, fetch, setInterval: () => 1}; vm.runInNewContext(source, context); context.window.ProductSwarm.bind();
  await elements['message-btn'].click(); await elements['discuss-btn'].click();
  const paths = events.map(([url]) => url);
  assert.ok(paths.includes('/api/v1/swarm/products/default/messages'));
  assert.ok(paths.includes('/api/v1/swarm/products/default/discuss'));
  const message = events.find(([url, options]) => url.endsWith('/products/default/messages') && options.method === 'POST');
  assert.deepEqual(JSON.parse(message[1].body), {body: 'hello'});
});

test('product cockpit does not silently run planning when a message is sent', () => {
  const context = {window: {}, document: {addEventListener: () => {}}, fetch: () => Promise.reject(new Error('not called'))};
  vm.runInNewContext(source, context);
  assert.equal(typeof context.window.ProductSwarm, 'object');
  assert.equal(context.window.ProductSwarm.messageAction, 'message');
  assert.equal(context.window.ProductSwarm.planningAction, 'separate');
});

test('product template contains cockpit controls and existing swarm link', () => {
  const html = fs.readFileSync('whilly/api/templates/product_swarm.html.j2', 'utf8');
  for (const id of ['feature-list', 'feature-status', 'feature-spec', 'message-input', 'plan-btn', 'run-btn', 'budget-max-calls', 'budget-max-elapsed', 'discuss-btn', 'approve-btn', 'publish-btn', 'approval-digest', 'publication-receipts']) {
    assert.match(html, new RegExp(`id=["']${id}["']`));
  }
  assert.match(html, /href=["']\/["']/);
  assert.match(html, /href=["']\/swarm\?session=/);
  assert.match(html, /planner_profiles/);
  assert.match(html, /available_planners/);
  assert.match(html, /planner_setup_required/);
});

test('learning panel names disabled activation and renders durable evidence as text', () => {
  const html = fs.readFileSync('whilly/api/templates/product_swarm.html.j2', 'utf8');
  for (const id of ['learning-status-btn', 'learning-status', 'learning-report-id', 'learning-report-btn', 'learning-evidence']) {
    assert.match(html, new RegExp(`id=["']${id}["']`));
  }
  assert.match(source, /request\('\/learning\/status'\)/);
  assert.match(source, /\/learning\/research\/\$\{encodeURIComponent\(runId\)\}\/report/);
  assert.match(source, /blockers\.join/);
  assert.doesNotMatch(source, /learning-evidence[^\n]*innerHTML/);
});

test('product cockpit clears stale feature deep links after cleanup', () => {
  assert.match(source, /searchParams\.delete\('feature'\)/);
  assert.match(source, /Создайте новую задачу/);
  const html = fs.readFileSync('whilly/api/templates/product_swarm.html.j2', 'utf8');
  assert.match(html, /product-swarm\.js\?v=20261007-learning/);
});
