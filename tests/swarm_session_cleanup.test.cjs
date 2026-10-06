const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');

const source = fs.readFileSync('whilly/api/templates/swarm.html.j2', 'utf8');

test('swarm exposes reversible bulk archive for starting a fresh project session', () => {
  assert.match(source, /id="archive-visible-btn"/);
  assert.match(source, /function archiveVisibleSessions\(\)/);
  assert.match(source, /include_test=\$\{includeTest\}&include_archived=false/);
  assert.match(source, /JSON\.stringify\(\{archived: true\}\)/);
});
