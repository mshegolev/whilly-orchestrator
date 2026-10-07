const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const context = {window: {}};
const source = 'whilly/api/static/swarm-gantt.js';
if (fs.existsSync(source)) vm.runInNewContext(fs.readFileSync(source, 'utf8'), context);
const data = {
  status: {session: {applied_revision: 1}, tasks: [
    {task: 'one', task_id: 't1', revision: 1, project: 'demo', role: 'dev', status: 'DONE'},
    {task: 'two', task_id: 't2', revision: 1, project: 'demo', role: 'qa', status: 'TODO'}]},
  revisions: [{revision: 1, plan: {tasks: [{id: 'one'}, {id: 'two', depends_on: ['one']}]}}]
};
test('attempt intervals retain retries and dependencies; queued tasks have no invented dates', () => {
  assert.ok(context.window.SwarmGantt, 'Gantt implementation must be available');
  const result = context.window.SwarmGantt.model(data, {attempts: [
    {task_id: 't1', attempt: 1, status: 'failed', started_at: '2026-01-01T00:00:00Z', finished_at: '2026-01-01T00:01:00Z'},
    {task_id: 't1', attempt: 2, status: 'accepted', started_at: '2026-01-01T00:02:00Z', finished_at: '2026-01-01T00:03:00Z'}]}, 1, 0);
  assert.equal(result.rows[0].intervals.length, 2);
  assert.equal(result.rows[0].intervals[0].end - result.rows[0].intervals[0].start, 60000);
  assert.equal(result.rows[1].intervals.length, 0);
  assert.equal(result.rows[1].dependencies[0], 'one');
});
test('proposed revision never inherits timings from an applied revision', () => {
  assert.ok(context.window.SwarmGantt);
  const changed = {...data, revisions: [...data.revisions, {revision: 2, plan: {tasks: [{id: 'one', project: 'other', role: 'dev'}]}}]};
  const result = context.window.SwarmGantt.model(changed, {attempts: []}, 2, 0);
  assert.equal(result.rows.length, 1);
  assert.equal(result.rows[0].status, 'PROPOSED');
  assert.equal(result.rows[0].intervals.length, 0);
});
test('only running attempts extend to now; incomplete finished attempts stay undated', () => {
  assert.ok(context.window.SwarmGantt);
  const result = context.window.SwarmGantt.model(data, {attempts: [
    {task_id: 't1', status: 'running', started_at: '2026-01-01T00:00:00Z'},
    {task_id: 't1', status: 'failed', started_at: '2026-01-01T00:00:00Z'},
    {task_id: 't1', status: 'accepted', started_at: 'invalid', finished_at: 'invalid'}]}, 1, Date.parse('2026-01-01T00:01:00Z'));
  assert.equal(result.rows[0].intervals.length, 1);
  assert.equal(result.rows[0].intervals[0].end - result.rows[0].intervals[0].start, 60000);
  assert.equal(result.rows[0].missingTimes, 2);
});

test('normalizes task, task_id and id across status and attempt payloads', () => {
  const fallback = {
    status: {session: {applied_revision: 1}, tasks: [
      {task: 'one', revision: 1, project: 'demo', role: 'dev', status: 'DONE'},
    ]},
    revisions: [{revision: 1, plan: {tasks: [{id: 'one'}]}}],
  };
  const result = context.window.SwarmGantt.model(fallback, {attempts: [
    {task_id: 'one', attempt: 1, status: 'accepted', started_at: '2026-01-01T00:00:00Z', finished_at: '2026-01-01T00:01:00Z'},
  ]}, 1, 0);
  assert.equal(result.rows[0].intervals.length, 1);
  assert.equal(result.rows[0].taskId, 'one');
});
