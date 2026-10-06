const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');

const source = fs.readFileSync('whilly/api/templates/swarm.html.j2', 'utf8');

test('swarm polling has an explicit lifecycle and stops after the job settles', () => {
  assert.match(source, /function stopPolling\(\)/);
  assert.match(source, /pollTimer = setTimeout\(/);
  assert.match(source, /job\?\.active/);
  assert.doesNotMatch(source, /pollTimer = setInterval\(/);
});
