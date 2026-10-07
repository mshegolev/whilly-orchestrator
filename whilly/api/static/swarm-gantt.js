/* Actual execution only: queued work deliberately has no estimated dates. */
window.SwarmGantt = (() => {
  function taskIds(task) {
    return [task?.task_id, task?.task, task?.id]
      .filter(value => value !== undefined && value !== null && value !== '')
      .map(String);
  }

  function sameTask(left, right) {
    const rightIds = new Set(taskIds(right));
    return taskIds(left).some(value => rightIds.has(value));
  }

  function model(data, report, revision, now = Date.now()) {
    const rev = revision ?? data?.status?.session?.applied_revision ?? data?.revisions?.at(-1)?.revision;
    const plan = data?.revisions?.find(r => r.revision === rev)?.plan?.tasks || [];
    const actual = (data?.status?.tasks || []).filter(t => t.revision === rev);
    const rows = (actual.length ? actual : plan.map(t => ({...t, task: t.id, status: 'PROPOSED'}))).map(t => {
      const spec = plan.find(p => sameTask(p, t)) || {};
      const attempts = (report?.attempts || []).filter(a => sameTask(a, t));
      const intervals = attempts.flatMap(a => {
        const start = Date.parse(a.started_at);
        const end = a.finished_at ? Date.parse(a.finished_at) : a.status === 'running' ? now : NaN;
        return Number.isFinite(start) && Number.isFinite(end) && end >= start ? [{...a, start, end}] : [];
      });
      return {...t, taskId: taskIds(t)[0] || null, dependencies: spec.depends_on || [], intervals, missingTimes: attempts.length - intervals.length};
    });
    rows.sort((a, b) => `${a.project}/${a.role}`.localeCompare(`${b.project}/${b.role}`));
    const times = rows.flatMap(t => t.intervals.flatMap(a => [a.start, a.end]));
    return {revision: rev, rows, min: times.length ? Math.min(...times) : null, max: times.length ? Math.max(...times) : null};
  }

  function render(root, data, report, revision, groupBy = 'project') {
    const view = model(data, report, revision);
    root.replaceChildren();
    const el = (tag, text, cls) => {
      const node = document.createElement(tag);
      if (text != null) node.textContent = text;
      if (cls) node.className = cls;
      return node;
    };
    root.append(el('p', `Ревизия ${view.revision ?? '—'} · Фактическое выполнение · Время браузера`));
    if (!view.rows.length) { root.append(el('p', 'Нет задач. Выберите сессию и ревизию плана.')); return; }
    root.append(el('p', 'Зелёный — принята · синий — выполняется · красный — ошибка · серый — отменена. Зависимости: «После».'));
    const groups = new Map();
    for (const row of view.rows) {
      const key = row[groupBy] || 'Не указан';
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(row);
    }
    const span = Math.max(1000, view.max - view.min);
    const stamp = time => new Date(time).toLocaleString();
    for (const [name, rows] of groups) {
      root.append(el('h3', name));
      if (view.min !== null) {
        const axis = el('div', null, 'gantt-axis');
        axis.append(el('span', stamp(view.min)), el('span', stamp(view.max)));
        root.append(axis);
      }
      for (const row of rows) {
        const line = el('div', null, 'gantt-row');
        line.append(el('strong', `${row.task} · ${row.status}`));
        line.append(el('div', `${row.project || '—'} / ${row.role || '—'} · После: ${row.dependencies.join(', ') || '—'}`));
        if (row.blocker) line.append(el('div', `Блокировка: ${row.blocker}`, 'gantt-blocker'));
        if (!row.intervals.length) line.append(el('div', row.status === 'PROPOSED' ? 'Предложена · сроки не заданы' : 'Нет временных данных / не начата'));
        for (const a of row.intervals) {
          const track = el('div', null, 'gantt-track');
          const kind = ['accepted', 'running', 'cancelled'].includes(a.status) ? a.status : 'failed';
          const bar = el('div', null, `gantt-bar ${kind}`);
          bar.style.left = `${100 * (a.start - view.min) / span}%`;
          bar.style.width = `${Math.max(.3, 100 * (a.end - a.start) / span)}%`;
          const description = `Попытка ${a.attempt ?? '—'} · ${a.status} · ${stamp(a.start)} → ${stamp(a.end)} · ${Math.round((a.end - a.start) / 1000)} с`;
          bar.title = description;
          track.append(bar);
          line.append(track, el('small', description));
        }
        if (row.missingTimes) line.append(el('div', `Нет корректных временных меток: ${row.missingTimes}`));
        root.append(line);
      }
    }
  }
  return {model, render};
})();
