/* Product cockpit: server is the source of truth; this file only renders responses. */
(function (root) {
  const apiRoot = '/api/v1/swarm';
  const request = async (path, options = {}) => {
    const response = await fetch(`${apiRoot}${path}`, {
      headers: {'Content-Type': 'application/json'}, ...options,
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail || body.error || `Request failed (${response.status})`);
    return body;
  };
  const text = (node, value) => { node.textContent = value == null ? '—' : String(value); };
  const showError = (error) => text(document.getElementById('product-error'), error.message || String(error));
  const featureId = () => document.getElementById('feature-list').value;
  const updatePlanAvailability = () => {
    const profile = document.getElementById('planner-profile');
    const selected = profile.selectedOptions?.[0];
    const ready = Boolean(featureId() && selected?.value && !selected.disabled);
    document.getElementById('plan-btn').disabled = !ready;
    document.getElementById('discuss-btn').disabled = !(selected?.value && !selected.disabled);
  };
  let featureGeneration = 0;
  let latestMessageText = '';
  async function loadFeatures() {
    const data = await request('/features');
    const select = document.getElementById('feature-list');
    const preferred = select.value || (root.location?.search ? new URLSearchParams(root.location.search).get('feature') : '');
    select.replaceChildren();
    const empty = document.createElement('option'); empty.value = ''; empty.textContent = '— new task —'; select.append(empty);
    for (const feature of data.features || []) {
      const option = document.createElement('option'); option.value = feature.id; option.textContent = feature.title || feature.id;
      select.append(option);
    }
    if ((data.features || []).some(feature => feature.id === preferred)) {
      select.value = preferred;
    } else if (preferred) {
      // Do not keep retrying a deleted/archived feature deep link.
      const url = new URL(root.location.href);
      url.searchParams.delete('feature');
      root.history?.replaceState({}, '', `${url.pathname}${url.search}${url.hash}`);
      text(document.getElementById('product-error'), '');
    }
    if (select.value) {
      await loadFeature(select.value);
    } else {
      text(document.getElementById('feature-status'), 'new');
      text(document.getElementById('feature-spec'), 'Создайте новую задачу.');
      text(document.getElementById('feature-blocker'), '');
    }
    updatePlanAvailability();
  }
  async function loadFeature(id) {
    const generation = ++featureGeneration;
    const feature = await request(`/features/${encodeURIComponent(id)}`);
    if (generation !== featureGeneration || id !== featureId()) return;
    text(document.getElementById('feature-status'), feature.status);
    const runStatus = document.getElementById('run-status');
    if (runStatus) text(runStatus, feature.status);
    const spec = feature.spec;
    text(document.getElementById('feature-spec'), spec == null ? 'No specification recorded.' : typeof spec === 'string' ? spec : JSON.stringify(spec, null, 2));
    text(document.getElementById('feature-blocker'), feature.blocker);
    const budget = feature.budget || feature;
    const callsInput = document.getElementById('budget-max-calls');
    const elapsedInput = document.getElementById('budget-max-elapsed');
    if (document.activeElement !== callsInput) callsInput.value = budget.max_calls ?? '';
    if (document.activeElement !== elapsedInput) elapsedInput.value = budget.max_elapsed_seconds ?? '';
    text(document.getElementById('approval-digest'), feature.approval_digest);
    const sessionLink = document.getElementById('feature-session-link');
    if (sessionLink) {
      sessionLink.href = `/swarm?session=${encodeURIComponent(feature.session_id || '')}`;
      sessionLink.hidden = !feature.session_id;
    }
    document.getElementById('approval-revision').value = feature.revision || '';
    await loadPublications(id, generation);
    return feature;
  }
  async function sendMessage() {
    const input = document.getElementById('message-input'); const value = input.value.trim();
    if (!value) return;
    await request('/products/default/messages', {method: 'POST', body: JSON.stringify({body: value})});
    if (!featureId()) {
      const firstLine = value.split(/\r?\n/, 1)[0].trim();
      const title = (firstLine || value).slice(0, 200);
      const created = await request('/products/default/features', {
        method: 'POST', body: JSON.stringify({title, intent: value}),
      });
      await loadFeatures();
      document.getElementById('feature-list').value = created.id;
      await loadFeature(created.id);
    }
    latestMessageText = value;
    input.value = ''; text(document.getElementById('planning-note'), 'Message saved. Planning runs only via the separate Plan action.');
    updatePlanAvailability();
    await loadMessages();
  }
  function startNewTask() {
    ++featureGeneration;
    latestMessageText = '';
    document.getElementById('feature-list').value = '';
    const input = document.getElementById('message-input');
    input.value = '';
    text(document.getElementById('feature-status'), 'new');
    text(document.getElementById('run-status'), 'Awaiting task');
    text(document.getElementById('feature-spec'), 'Создайте новую задачу.');
    text(document.getElementById('feature-blocker'), '');
    text(document.getElementById('approval-digest'), '');
    document.getElementById('approval-revision').value = '';
    document.getElementById('feature-session-link').hidden = true;
    updatePlanAvailability();
    input.focus();
  }
  async function plan() {
    const id = featureId(); const profile = document.getElementById('planner-profile').value;
    if (!id) throw new Error('Сначала сохраните задачу — Whilly создаст feature автоматически.');
    const textValue = document.getElementById('message-input').value.trim() || latestMessageText;
    if (!textValue) throw new Error('Сначала вставьте и сохраните текст задачи.');
    await request(`/features/${encodeURIComponent(id)}/plan`, {method: 'POST', body: JSON.stringify({text: textValue, planner_profile: profile})});
    await loadFeature(id);
  }
  async function run() {
    const id = featureId();
    await request(`/features/${encodeURIComponent(id)}/run`, {method: 'POST', body: JSON.stringify({workers: Math.min(5, Number(document.getElementById('workers').value))})});
    await loadFeature(id);
  }
  async function saveBudget() {
    const id = featureId();
    await request(`/features/${encodeURIComponent(id)}/budget`, {method: 'POST', body: JSON.stringify({max_calls: Number(document.getElementById('budget-max-calls').value), max_elapsed_seconds: Number(document.getElementById('budget-max-elapsed').value)})});
  }
  async function loadMessages() {
    const data = await request('/products/default/messages');
    const list = document.getElementById('message-history'); list.replaceChildren();
    for (const message of data.messages || []) { const item = document.createElement('li'); item.textContent = `${message.sender || 'unknown'}: ${message.body || ''}`; list.append(item); }
    const messages = data.messages || [];
    const latest = [...messages].reverse().find(message => message.body && message.sender !== 'assistant');
    if (latest) latestMessageText = latest.body;
  }
  async function discuss() {
    await request('/products/default/discuss', {method: 'POST', body: JSON.stringify({body: document.getElementById('message-input').value.trim(), planner_profile: document.getElementById('planner-profile').value})}); await loadMessages();
  }
  async function approve() {
    const id = featureId();
    await request(`/features/${encodeURIComponent(id)}/approve`, {method: 'POST', body: JSON.stringify({revision: Number(document.getElementById('approval-revision').value), digest: document.getElementById('approval-digest').textContent})}); await loadFeature(id);
  }
  async function publish() {
    const id = featureId(); if (!id) return;
    await request(`/features/${encodeURIComponent(id)}/publish`, {method: 'POST', body: JSON.stringify({})}); await loadPublications(id);
  }
  async function loadPublications(id, generation = featureGeneration) {
    const data = await request(`/features/${encodeURIComponent(id)}/publications`);
    if (generation !== featureGeneration || id !== featureId()) return;
    const list = document.getElementById('publication-receipts'); list.replaceChildren();
    for (const receipt of data.publications || []) {
      const item = document.createElement('li'); item.textContent = JSON.stringify(receipt);
      if (typeof receipt.mr_url === 'string' && receipt.mr_url.startsWith('https://')) {
        const link = document.createElement('a'); link.href = receipt.mr_url; link.textContent = ' Open publication'; link.target = '_blank'; link.rel = 'noopener noreferrer'; item.append(link);
      }
      list.append(item);
    }
  }
  async function stop() {
    const id = featureId(); if (!id) return;
    await request(`/features/${encodeURIComponent(id)}/stop`, {method: 'POST', body: JSON.stringify({})}); await loadFeature(id);
  }
  async function loadKnowledge() {
    const project = document.getElementById('knowledge-project').value.trim();
    if (!project) throw new Error('Enter a registered project id.');
    const data = await request(`/knowledge?project_id=${encodeURIComponent(project)}&max_chars=12000`);
    text(document.getElementById('knowledge-result'), JSON.stringify(data, null, 2));
  }
  async function loadKnowledgeStatus() {
    const data = await request('/knowledge/status');
    const state = data.ready ? 'ready' : 'unavailable';
    const detail = data.error_code ? ` (${data.error_code})` : '';
    text(document.getElementById('knowledge-status'), `${data.backend}: ${state}${detail}; last_elapsed_ms=${data.last_elapsed_ms ?? '—'}`);
    return data;
  }
  let collaborationGeneration = 0;
  async function loadCollaboration() {
    const generation = ++collaborationGeneration;
    const [status, history] = await Promise.all([request('/collaboration/status'), request('/collaboration')]);
    if (generation !== collaborationGeneration) return;
    text(document.getElementById('collaboration-status'), status.configured ? 'Delivery policy configured. Messages do not authorize execution.' : (status.blocker || 'Delivery not configured.'));
    text(document.getElementById('collaboration-result'), JSON.stringify(history, null, 2));
  }
  let proposalListGeneration = 0;
  let proposalGeneration = 0;
  let activeProposal = null;
  function disableProposalActions() {
    document.getElementById('proposal-accept-btn').disabled = true;
    document.getElementById('proposal-reject-btn').disabled = true;
  }
  async function loadProposals() {
    const select = document.getElementById('proposal-list');
    if (!select) return;
    const generation = ++proposalListGeneration;
    ++proposalGeneration; activeProposal = null; disableProposalActions();
    const data = await request('/proposals');
    if (generation !== proposalListGeneration) return;
    const previous = select.value;
    select.replaceChildren();
    for (const proposal of data.proposals || []) {
      const option = document.createElement('option'); option.value = proposal.id;
      option.textContent = `${proposal.target_project}: ${proposal.outcome} [${proposal.status}]`;
      select.append(option);
    }
    if ((data.proposals || []).some(proposal => proposal.id === previous)) select.value = previous;
    if (select.value) await loadProposal(select.value);
    else {
      text(document.getElementById('proposal-status'), 'No proposals recorded.');
      text(document.getElementById('proposal-detail'), 'No proposal selected.');
      document.getElementById('proposal-feature-link').hidden = true;
    }
  }
  async function loadProposal(id) {
    const generation = ++proposalGeneration;
    activeProposal = null; disableProposalActions();
    text(document.getElementById('proposal-status'), 'Loading proposal…');
    const data = await request(`/proposals/${encodeURIComponent(id)}`);
    if (generation !== proposalGeneration || id !== document.getElementById('proposal-list').value) return;
    activeProposal = data;
    text(document.getElementById('proposal-detail'), JSON.stringify(data, null, 2));
    text(document.getElementById('proposal-status'), `${data.proposal.status}: ${data.evaluation.reason || data.evaluation.status}. Accepting only changes the plan; execution requires fresh approval.`);
    const pending = ['proposed', 'triaged'].includes(data.proposal.status);
    document.getElementById('proposal-accept-btn').disabled = !pending || data.evaluation.status !== 'eligible' || !Number.isInteger(data.feature_revision);
    document.getElementById('proposal-reject-btn').disabled = !pending;
    if (data.proposal.status === 'awaiting_approval') {
      text(document.getElementById('proposal-status'), 'Accepted for planning, not execution. To withdraw planned work, edit or stop the originating feature and review its new revision.');
    }
    const link = document.getElementById('proposal-feature-link');
    link.href = `/swarm/product?feature=${encodeURIComponent(data.proposal.origin_feature_id)}`;
    link.hidden = false;
  }
  async function decideProposal(action) {
    if (!['accept', 'reject'].includes(action)) throw new Error('Invalid proposal decision.');
    const current = activeProposal;
    const id = document.getElementById('proposal-list').value;
    if (!current || current.proposal.id !== id) throw new Error('Refresh the selected proposal first.');
    const reason = document.getElementById('proposal-reason').value.trim();
    if (!reason || reason.length > 256) throw new Error('Decision reason must contain 1–256 characters.');
    if (document.getElementById(`proposal-${action}-btn`).disabled) throw new Error('Proposal decision is blocked.');
    disableProposalActions();
    const body = action === 'accept' ? {expected_revision: current.feature_revision, reason} : {reason};
    try {
      await request(`/proposals/${encodeURIComponent(id)}/${action}`, {method: 'POST', body: JSON.stringify(body)});
      if (featureId() === current.proposal.origin_feature_id) await loadFeature(featureId());
    } finally {
      await loadProposals();
    }
  }
  async function loadLearningStatus() {
    const data = await request('/learning/status');
    const blockers = data.blockers || [];
    text(document.getElementById('learning-status'), blockers.length ? blockers.join(', ') : 'Learning controls ready.');
    return data;
  }
  async function loadResearchReport() {
    const runId = document.getElementById('learning-report-id').value.trim();
    if (!runId) throw new Error('Enter a research run id.');
    const data = await request(`/learning/research/${encodeURIComponent(runId)}/report`);
    text(document.getElementById('learning-evidence'), JSON.stringify(data, null, 2));
    return data;
  }
  async function loadExperiment() {
    const experimentId = document.getElementById('learning-experiment-id').value.trim();
    if (!experimentId) throw new Error('Enter an experiment id.');
    const data = await request(`/learning/experiments/${encodeURIComponent(experimentId)}`);
    text(document.getElementById('learning-evidence'), JSON.stringify(data, null, 2));
    return data;
  }
  function bind() {
    document.getElementById('planner-profile').addEventListener('change', event => {
      updatePlanAvailability();
    });
    document.getElementById('knowledge-load-btn')?.addEventListener('click', () => loadKnowledge().catch(showError));
    document.getElementById('knowledge-status-btn')?.addEventListener('click', () => loadKnowledgeStatus().catch(showError));
    document.getElementById('collaboration-load-btn')?.addEventListener('click', () => loadCollaboration().catch(showError));
    document.getElementById('proposal-load-btn')?.addEventListener('click', () => loadProposals().catch(showError));
    document.getElementById('proposal-list')?.addEventListener('change', event => loadProposal(event.target.value).catch(showError));
    document.getElementById('proposal-accept-btn')?.addEventListener('click', () => decideProposal('accept').catch(showError));
    document.getElementById('proposal-reject-btn')?.addEventListener('click', () => decideProposal('reject').catch(showError));
    document.getElementById('learning-status-btn')?.addEventListener('click', () => loadLearningStatus().catch(showError));
    document.getElementById('learning-report-btn')?.addEventListener('click', () => loadResearchReport().catch(showError));
    document.getElementById('learning-experiment-btn')?.addEventListener('click', () => loadExperiment().catch(showError));
    document.getElementById('feature-list').addEventListener('change', () => {
      updatePlanAvailability();
      const id = featureId();
      if (id) loadFeature(id).catch(showError);
    });
    document.getElementById('message-btn').addEventListener('click', () => sendMessage().catch(showError));
    document.getElementById('plan-btn').addEventListener('click', () => plan().catch(showError));
    document.getElementById('run-btn').addEventListener('click', () => run().catch(showError));
    document.getElementById('budget-btn').addEventListener('click', () => saveBudget().catch(showError));
    document.getElementById('discuss-btn').addEventListener('click', () => discuss().catch(showError));
    document.getElementById('approve-btn').addEventListener('click', () => approve().catch(showError));
    document.getElementById('publish-btn').addEventListener('click', () => publish().catch(showError));
    document.getElementById('stop-btn').addEventListener('click', () => stop().catch(showError));
    document.getElementById('create-feature-btn').addEventListener('click', startNewTask);
    Promise.all([loadFeatures(), loadMessages(), loadKnowledgeStatus()]).catch(showError);
    loadCollaboration().catch(showError);
    loadProposals().catch(showError);
    loadLearningStatus().catch(showError);
    setInterval(() => { loadMessages().catch(showError); const id = featureId(); if (id) loadFeature(id).catch(showError); }, 3000);
  }
  root.ProductSwarm = {apiRoot, request, bind, saveTask: sendMessage, startNewTask, loadKnowledge, loadKnowledgeStatus, loadCollaboration, loadProposals, loadProposal, decideProposal, loadLearningStatus, loadResearchReport, loadExperiment, messageAction: 'message', planningAction: 'separate'};
  if (typeof document !== 'undefined') document.addEventListener('DOMContentLoaded', bind);
})(window);
