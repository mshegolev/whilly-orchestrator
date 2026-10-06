const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function browser({saved = null, dark = false, blocked = false} = {}) {
  const file = 'whilly/api/static/whilly-theme.js';
  assert.ok(fs.existsSync(file), 'shared theme controller must exist');
  const handlers = {}, dom = {}, mediaEvents = {}, controlEvents = {};
  const root = {dataset: {}};
  let control = {value: '', matches: () => true, addEventListener: (name, fn) => controlEvents[name] = fn};
  const media = {matches: dark, addEventListener: (name, fn) => mediaEvents[name] = fn};
  const storage = new Map(saved === null ? [] : [['whilly-theme', saved]]);
  const window = {
    matchMedia: () => media,
    addEventListener: (name, fn) => handlers[name] = fn,
    localStorage: {
      getItem(key) { if (blocked) throw Error('blocked'); return storage.get(key) ?? null; },
      setItem(key, value) { if (blocked) throw Error('blocked'); storage.set(key, value); },
    },
  };
  const document = {
    documentElement: root, readyState: 'loading',
    querySelectorAll: () => [control],
    addEventListener: (name, fn) => dom[name] = fn,
  };
  vm.runInNewContext(fs.readFileSync(file, 'utf8'), {window, document});
  return {root, media, storage, control, handlers, mediaEvents,
    mount: () => dom.DOMContentLoaded(),
    choose(value) { control.value = value; (dom.change || controlEvents.change)({target: control}); },
    swap() {
      control = {value: 'system', matches: () => true};
      delete controlEvents.change;
      dom['htmx:afterSwap']?.();
      return control;
    },
    changeOther() { dom.change?.({target: {matches: () => false, value: 'system'}}); },
  };
}

test('system preference is applied before DOM content loads', () => {
  const b = browser({dark: true});
  assert.equal(b.root.dataset.theme, 'dark');
  b.mount();
  assert.equal(b.control.value, 'system');
  b.media.matches = false;
  b.mediaEvents.change();
  assert.equal(b.root.dataset.theme, 'light');
});

test('saved explicit theme overrides OS and survives reload', () => {
  const b = browser({saved: 'light', dark: true});
  assert.equal(b.root.dataset.theme, 'light');
  b.mount(); b.choose('dark');
  assert.equal(b.root.dataset.theme, 'dark');
  assert.equal(browser({saved: b.storage.get('whilly-theme')}).root.dataset.theme, 'dark');
  b.media.matches = false; b.mediaEvents.change();
  assert.equal(b.root.dataset.theme, 'dark');
});

test('returning to system mode follows OS changes', () => {
  const b = browser({saved: 'dark'});
  b.mount(); b.choose('system');
  assert.equal(b.root.dataset.theme, 'light');
  assert.equal(b.storage.get('whilly-theme'), 'system');
  b.media.matches = true; b.mediaEvents.change();
  assert.equal(b.root.dataset.theme, 'dark');
});

test('invalid saved values fall back to system', () => {
  const b = browser({saved: 'unexpected', dark: true});
  assert.equal(b.root.dataset.theme, 'dark');
  b.mount(); assert.equal(b.control.value, 'system');
});

test('unavailable storage does not prevent manual switching', () => {
  const b = browser({blocked: true});
  b.mount(); b.choose('dark');
  assert.equal(b.root.dataset.theme, 'dark');
  assert.equal(b.control.value, 'dark');
});

test('cross-tab changes and cleared preference synchronize controls', () => {
  const b = browser(); b.mount();
  b.handlers.storage({key: 'whilly-theme', newValue: 'dark'});
  assert.equal(b.root.dataset.theme, 'dark');
  assert.equal(b.control.value, 'dark');
  b.handlers.storage({key: 'unrelated', newValue: 'light'});
  assert.equal(b.root.dataset.theme, 'dark');
  b.handlers.storage({key: null, newValue: null});
  assert.equal(b.root.dataset.theme, 'light');
  assert.equal(b.control.value, 'system');
});

test('replacement controls reflect the preference and remain interactive after HTMX refresh', () => {
  const b = browser({saved: 'dark'}); b.mount();
  const replacement = b.swap();
  assert.equal(replacement.value, 'dark');
  b.choose('light');
  assert.equal(b.root.dataset.theme, 'light');
  assert.equal(b.storage.get('whilly-theme'), 'light');
  b.swap(); b.choose('dark');
  assert.equal(b.root.dataset.theme, 'dark');
  b.changeOther();
  assert.equal(b.root.dataset.theme, 'dark');
});
