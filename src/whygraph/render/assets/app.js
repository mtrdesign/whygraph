// WhyGraph viewer — boot Cytoscape, wire tabs, render details on click.
(function () {
  'use strict';

  const dataEl = document.getElementById('whygraph-data');
  if (!dataEl) {
    console.error('whygraph-data script tag missing');
    return;
  }
  const DATA = JSON.parse(dataEl.textContent);
  const RUNTIME = (DATA.meta && DATA.meta.runtime) || 'static';

  // ---------------------------------------------------------------------
  // Top bar — meta line + tabs
  // ---------------------------------------------------------------------
  const meta = DATA.meta || {};
  const cov = meta.rationale_coverage || { covered: 0, total: 0 };
  const RENDERED_DEPTH = typeof meta.depth === 'number' ? meta.depth : 4;
  const metaEl = document.getElementById('meta');
  function updateMeta(visibleNodes, visibleEdges) {
    const total = (DATA.nodes || []).length;
    metaEl.textContent =
      `Showing ${visibleNodes} of ${total} nodes · ${visibleEdges} edges · ` +
      `depth: ${RENDERED_DEPTH} · ` +
      `rationale ${cov.covered}/${cov.total} · runtime: ${RUNTIME}`;
  }

  const tabs = document.querySelectorAll('.tab');
  const views = document.querySelectorAll('.view');
  tabs.forEach(t => t.addEventListener('click', () => {
    tabs.forEach(x => x.classList.remove('active'));
    views.forEach(v => v.classList.remove('active'));
    t.classList.add('active');
    document.getElementById('view-' + t.dataset.tab).classList.add('active');
    if (t.dataset.tab === 'graph') cy && cy.resize();
  }));

  // ---------------------------------------------------------------------
  // Graph + level-slider filter
  // ---------------------------------------------------------------------
  const NODES_BY_ID = Object.fromEntries((DATA.nodes || []).map(n => [n.id, n]));
  const ALL_EDGES = DATA.edges || [];

  function nodeLevel(n) { return n && typeof n.level === 'number' ? n.level : 4; }

  /** Walk parent_id chain from `id` until we hit a node in `visible` (or null). */
  function anchorOf(id, visible, memo) {
    if (memo.has(id)) return memo.get(id);
    let cur = id;
    const path = [];
    while (cur && !visible.has(cur)) {
      path.push(cur);
      const n = NODES_BY_ID[cur];
      cur = n && n.parent_id ? n.parent_id : null;
      if (cur && path.includes(cur)) { cur = null; break; } // cycle guard
    }
    const anchor = cur && visible.has(cur) ? cur : null;
    path.forEach(p => memo.set(p, anchor));
    memo.set(id, anchor);
    return anchor;
  }

  function elementsForLevel(level) {
    const visible = new Set();
    (DATA.nodes || []).forEach(n => {
      if (nodeLevel(n) <= level) visible.add(n.id);
    });
    const memo = new Map();
    visible.forEach(id => memo.set(id, id));

    const cyNodes = [];
    visible.forEach(id => {
      const n = NODES_BY_ID[id];
      cyNodes.push({
        data: {
          id: n.id,
          label: n.name || n.qualified_name,
          kind: n.kind,
          level: nodeLevel(n),
          degree: n.degree || 1,
          hasRationale: n.has_rationale ? 1 : 0,
        },
      });
    });

    // Aggregate edges by (s_anchor, t_anchor, kind).
    const aggregated = new Map();
    ALL_EDGES.forEach(e => {
      const sa = anchorOf(e.source, visible, memo);
      const ta = anchorOf(e.target, visible, memo);
      if (!sa || !ta || sa === ta) return;
      const key = sa + '' + ta + '' + (e.kind || '');
      const slot = aggregated.get(key);
      if (slot) slot.weight += 1;
      else aggregated.set(key, { source: sa, target: ta, kind: e.kind, weight: 1 });
    });
    const cyEdges = [];
    aggregated.forEach(v => {
      cyEdges.push({
        data: {
          id: 'e:' + v.source + ':' + v.target + ':' + (v.kind || ''),
          source: v.source,
          target: v.target,
          kind: v.kind,
          weight: v.weight,
        },
      });
    });
    return { cyNodes, cyEdges };
  }

  // Read level palette from CSS variables so :root governs both DOM dots
  // and Cytoscape node fills — change once, propagates everywhere.
  const cssVar = name =>
    getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const LEVEL_FILL = {
    1: cssVar('--node-l1') || '#58a6ff',
    2: cssVar('--node-l2') || '#d2a8ff',
    3: cssVar('--node-l3') || '#f0883e',
    4: cssVar('--node-l4') || '#8b949e',
  };
  const RATIONALE_BORDER = cssVar('--node-rationale') || '#3fb950';

  // Layout knobs — modest spread vs the v1.4.1 baseline (4000 / 80).
  // Higher numbers look airier but make cose spend much longer
  // converging on the main thread (cose isn't web-workered), which
  // shows up as a "freeze" at boot. These settings keep first paint
  // under ~1s on 50–100 nodes while still giving room to read labels.
  const COSE_OPTS = {
    name: 'cose',
    animate: false,
    fit: true,
    padding: 50,
    nodeRepulsion: 8000,
    idealEdgeLength: 130,
    nodeOverlap: 10,
    gravity: 0.5,
    coolingFactor: 0.95,  // default 0.99 — faster cooling = fewer effective iterations
    numIter: 600,         // default 1000; we stay well under
    componentSpacing: 80,
  };

  const initial = elementsForLevel(1);
  const cy = cytoscape({
    container: document.getElementById('cy'),
    elements: [...initial.cyNodes, ...initial.cyEdges],
    style: [
      {
        selector: 'node',
        style: {
          'background-color': LEVEL_FILL[4],
          'label': 'data(label)',
          'color': '#c9d1d9',
          'font-size': 9,
          'text-valign': 'bottom',
          'text-margin-y': 4,
          'width': 'mapData(degree, 0, 30, 12, 38)',
          'height': 'mapData(degree, 0, 30, 12, 38)',
          'text-wrap': 'ellipsis',
          'text-max-width': 100,
          'border-width': 0,
          'border-color': '#0e1117',
        },
      },
      { selector: 'node[level = 1]', style: { 'background-color': LEVEL_FILL[1] } },
      { selector: 'node[level = 2]', style: { 'background-color': LEVEL_FILL[2] } },
      { selector: 'node[level = 3]', style: { 'background-color': LEVEL_FILL[3] } },
      { selector: 'node[level = 4]', style: { 'background-color': LEVEL_FILL[4] } },
      // Rationale signal moves from fill to border so we keep the kind colour.
      {
        selector: 'node[hasRationale = 1]',
        style: { 'border-width': 3, 'border-color': RATIONALE_BORDER },
      },
      {
        selector: 'node:selected',
        style: { 'border-width': 4, 'border-color': '#79b8ff' },
      },
      {
        selector: 'edge',
        style: {
          'curve-style': 'bezier',
          'target-arrow-shape': 'triangle',
          'line-color': '#30363d',
          'target-arrow-color': '#30363d',
          'width': 'mapData(weight, 1, 20, 1, 4)',
          'arrow-scale': 0.8,
          'opacity': 0.7,
        },
      },
      { selector: 'node.dim', style: { 'opacity': 0.15 } },
      { selector: 'edge.dim', style: { 'opacity': 0.05 } },
    ],
    layout: COSE_OPTS,
    wheelSensitivity: 0.2,
  });
  updateMeta(initial.cyNodes.length, initial.cyEdges.length);

  function applyLevel(level) {
    const { cyNodes, cyEdges } = elementsForLevel(level);
    cy.batch(() => {
      cy.elements().remove();
      cy.add([...cyNodes, ...cyEdges]);
    });
    cy.layout(COSE_OPTS).run();
    updateMeta(cyNodes.length, cyEdges.length);
    // Re-apply current search filter against the new node set.
    applySearchFilter();
  }

  // Wire the slider buttons.
  const levelButtons = document.querySelectorAll('.level-btn');
  levelButtons.forEach(btn => btn.addEventListener('click', () => {
    levelButtons.forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    applyLevel(parseInt(btn.dataset.level, 10));
  }));

  cy.on('tap', 'node', evt => renderDetail(evt.target.data('id')));
  cy.on('tap', evt => { if (evt.target === cy) clearDetail(); });

  // Edge tooltip — title via DOM cursor doesn't apply to cytoscape canvas,
  // so use a transient title attribute on the parent container.
  cy.on('mouseover', 'edge', evt => {
    const w = evt.target.data('weight') || 1;
    document.getElementById('cy').title =
      w === 1 ? '1 underlying call' : `${w} underlying calls`;
  });
  cy.on('mouseout', 'edge', () => { document.getElementById('cy').title = ''; });

  // Search filter (re-uses node visibility within the current level).
  const search = document.getElementById('search');
  function applySearchFilter() {
    const q = search.value.trim().toLowerCase();
    cy.batch(() => {
      cy.nodes().forEach(n => {
        const d = n.data();
        const match = !q || (d.label && d.label.toLowerCase().includes(q));
        n.toggleClass('dim', !match);
      });
      cy.edges().forEach(e => {
        const sd = cy.getElementById(e.data('source')).hasClass('dim');
        const td = cy.getElementById(e.data('target')).hasClass('dim');
        e.toggleClass('dim', sd || td);
      });
    });
  }
  search.addEventListener('input', applySearchFilter);

  // ---------------------------------------------------------------------
  // Detail panel
  // ---------------------------------------------------------------------
  const detail = document.getElementById('detail');

  function clearDetail() {
    detail.classList.add('empty');
    detail.innerHTML = '<div class="empty-state">Click a node to see contributors, evidence, and rationale.</div>';
  }

  function renderDetail(nodeId) {
    const node = (DATA.nodes || []).find(n => n.id === nodeId);
    if (!node) { clearDetail(); return; }
    const d = (DATA.node_details || {})[nodeId];
    detail.classList.remove('empty');
    detail.innerHTML = '';
    detail.appendChild(buildDetailHeader(node));

    // Detail wasn't computed for this node at the rendered depth.
    // Show what we have from the nodes[] entry (header is already
    // rendered above) plus a clear placeholder. In serve mode the
    // "Generate rationale" button still works because it goes through
    // the live API.
    if (d == null) {
      detail.appendChild(buildDepthPlaceholder(node));
      // Rationale section always shows: in serve mode the button
      // still works; in static mode it's the standard "no rationale"
      // placeholder.
      detail.appendChild(buildRationale(node, null));
      return;
    }

    detail.appendChild(buildContributors(d.contributors || []));
    detail.appendChild(buildActivity(d.activity || {}));
    detail.appendChild(buildEvidence(d.evidence || []));
    detail.appendChild(buildRationale(node, d.rationale));
  }

  function buildDepthPlaceholder(node) {
    const wrap = document.createElement('div');
    wrap.appendChild(el('div', 'section-title', 'Detail not loaded'));
    const box = el('div', 'no-rationale');
    const need = Math.min(4, Math.max(RENDERED_DEPTH + 1, (node.level || 4)));
    box.appendChild(el(
      'div',
      '',
      `This node sits at level ${node.level || '?'}, but the page was ` +
      `rendered with --depth ${RENDERED_DEPTH}. Contributors, activity, ` +
      `and evidence aren't in this artifact.`,
    ));
    const cmd = document.createElement('code');
    cmd.textContent = `whygraph render --depth ${need}`;
    box.appendChild(cmd);
    wrap.appendChild(box);
    return wrap;
  }

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function buildDetailHeader(n) {
    const wrap = document.createElement('div');
    const h = el('h2', '', n.qualified_name);
    wrap.appendChild(h);
    const meta = el(
      'div',
      'meta-line',
      `${n.kind || 'symbol'} · ${n.file_path || '?'}:${n.start_line || '?'}-${n.end_line || '?'} · ${n.language || ''}`,
    );
    wrap.appendChild(meta);
    if (n.signature) wrap.appendChild(el('div', 'signature', n.signature));
    if (n.docstring) {
      const d = document.createElement('details');
      d.appendChild(el('summary', '', 'Docstring'));
      d.appendChild(el('div', 'docstring', n.docstring));
      wrap.appendChild(d);
    }
    return wrap;
  }

  function buildContributors(contribs) {
    const wrap = document.createElement('div');
    wrap.appendChild(el('div', 'section-title', 'Top contributors'));
    if (!contribs.length) {
      wrap.appendChild(el('div', 'empty-state', 'No blame data.'));
      return wrap;
    }
    const ul = el('ul', 'contributors');
    contribs.forEach(c => {
      const li = document.createElement('li');
      li.appendChild(el('span', 'name', c.name || c.email || c.login || 'unknown'));
      const bar = el('span', 'bar');
      const fill = document.createElement('span');
      fill.style.width = (c.percent || 0) + '%';
      bar.appendChild(fill);
      li.appendChild(bar);
      li.appendChild(el('span', 'pct', (c.percent || 0).toFixed(1) + '%'));
      ul.appendChild(li);
    });
    wrap.appendChild(ul);
    return wrap;
  }

  function buildActivity(activity) {
    const wrap = document.createElement('div');
    wrap.appendChild(el('div', 'section-title', 'Activity (commits per month)'));
    const months = Object.keys(activity).sort();
    if (!months.length) {
      wrap.appendChild(el('div', 'empty-state', 'No activity recorded.'));
      return wrap;
    }
    const max = Math.max(...months.map(m => activity[m]));
    const chart = el('div', 'activity-chart');
    months.forEach(m => {
      const bar = el('div', 'bar');
      bar.style.height = (100 * activity[m] / max) + '%';
      bar.title = `${m}: ${activity[m]} commit${activity[m] === 1 ? '' : 's'}`;
      chart.appendChild(bar);
    });
    wrap.appendChild(chart);
    const axis = el('div', 'activity-axis');
    axis.appendChild(el('span', '', months[0]));
    axis.appendChild(el('span', '', months[months.length - 1]));
    wrap.appendChild(axis);
    return wrap;
  }

  function buildEvidence(items) {
    const wrap = document.createElement('div');
    wrap.appendChild(el('div', 'section-title', 'Recent commits'));
    if (!items.length) {
      wrap.appendChild(el('div', 'empty-state', 'No evidence in scan DB.'));
      return wrap;
    }
    const ul = el('ul', 'evidence');
    items.forEach(ev => {
      const li = document.createElement('li');
      const sha = (ev.sha || '').slice(0, 8);
      const head = document.createElement('div');
      head.appendChild(el('span', 'sha', sha));
      head.appendChild(document.createTextNode(' '));
      head.appendChild(el('span', 'when', ev.committed_at || ''));
      head.appendChild(document.createTextNode(' '));
      head.appendChild(el(
        'span',
        'author',
        '· ' + ((ev.author && (ev.author.name || ev.author.email)) || 'unknown'),
      ));
      li.appendChild(head);
      const narratives = ev.narratives || {};
      ['llm_description', 'subject', 'body', 'git_blame_summary'].forEach(k => {
        if (narratives[k]) {
          li.appendChild(el('div', 'narrative-label', labelFor(k)));
          li.appendChild(el('div', 'narrative', narratives[k]));
        }
      });
      (ev.prs || []).forEach(pr => {
        const line = el('div', 'pr-line');
        if (pr.html_url) {
          const a = document.createElement('a');
          a.href = pr.html_url;
          a.target = '_blank';
          a.textContent = `#${pr.number}`;
          line.appendChild(a);
        } else {
          line.appendChild(el('span', '', `#${pr.number}`));
        }
        line.appendChild(document.createTextNode(' ' + (pr.title || '')));
        li.appendChild(line);
      });
      (ev.issues || []).forEach(iss => {
        const line = el('div', 'issue-line');
        if (iss.html_url) {
          const a = document.createElement('a');
          a.href = iss.html_url;
          a.target = '_blank';
          a.textContent = `closes #${iss.number}`;
          line.appendChild(a);
        } else {
          line.appendChild(el('span', '', `closes #${iss.number}`));
        }
        line.appendChild(document.createTextNode(' ' + (iss.title || '')));
        li.appendChild(line);
      });
      ul.appendChild(li);
    });
    wrap.appendChild(ul);
    return wrap;
  }

  function labelFor(k) {
    return ({
      llm_description: 'LLM diff summary',
      subject: 'Subject',
      body: 'Body',
      git_blame_summary: 'Blame summary',
    })[k] || k;
  }

  function buildRationale(node, rationale) {
    const wrap = document.createElement('div');
    wrap.appendChild(el('div', 'section-title', 'Rationale'));
    if (rationale) {
      wrap.appendChild(renderRationaleCard(rationale));
      return wrap;
    }
    if (RUNTIME === 'serve') {
      const box = el('div', 'no-rationale');
      box.appendChild(el(
        'div',
        '',
        'No rationale yet. Generate it on demand — calls Claude (~30s).',
      ));
      const btn = document.createElement('button');
      btn.className = 'generate-btn';
      btn.textContent = 'Generate rationale';
      const errEl = el('div', 'generate-error');
      btn.addEventListener('click', async () => {
        btn.disabled = true;
        btn.textContent = 'Generating… (~30s)';
        errEl.textContent = '';
        try {
          const r = await fetch(
            '/api/rationale?qualified_name=' + encodeURIComponent(node.qualified_name),
          );
          if (!r.ok) {
            const err = await r.json().catch(() => ({}));
            throw new Error(err.error || `HTTP ${r.status}`);
          }
          const card = await r.json();
          // Patch the in-memory data so re-clicks hit the cached view.
          (DATA.node_details[node.id] = DATA.node_details[node.id] || {}).rationale = card;
          // Mark the node as having rationale.
          node.has_rationale = true;
          cy.getElementById(node.id).data('hasRationale', 1);
          renderDetail(node.id);
        } catch (e) {
          errEl.textContent = String(e.message || e);
          btn.disabled = false;
          btn.textContent = 'Generate rationale';
        }
      });
      box.appendChild(btn);
      box.appendChild(errEl);
      wrap.appendChild(box);
      return wrap;
    }
    const placeholder = el('div', 'no-rationale');
    placeholder.appendChild(el('div', '',
      'No rationale yet. Run /whygraph-plan in Claude Code, or start `whygraph serve` for on-demand generation, then re-render.',
    ));
    const cmd = document.createElement('code');
    cmd.textContent = '/whygraph-plan add rationale for ' + node.qualified_name;
    placeholder.appendChild(cmd);
    wrap.appendChild(placeholder);
    return wrap;
  }

  function renderRationaleCard(r) {
    const box = el('div', 'rationale');
    if (r.purpose) box.appendChild(field('Purpose', r.purpose));
    if (r.why) box.appendChild(field('Why', r.why));
    if (r.constraints && r.constraints.length) box.appendChild(listField('Constraints', r.constraints));
    if (r.tradeoffs && r.tradeoffs.length) box.appendChild(listField('Tradeoffs', r.tradeoffs));
    if (r.risks && r.risks.length) box.appendChild(listField('Risks', r.risks));
    box.appendChild(el(
      'div',
      'confidence',
      `Confidence: ${r.confidence != null ? r.confidence : '?'}` + (r.model ? ` · ${r.model}` : ''),
    ));
    return box;
  }
  function field(label, text) {
    const f = el('div', 'field');
    const s = el('strong', '', label + ': ');
    f.appendChild(s);
    f.appendChild(document.createTextNode(text));
    return f;
  }
  function listField(label, items) {
    const f = el('div', 'field');
    const s = el('strong', '', label + ':');
    f.appendChild(s);
    const ul = document.createElement('ul');
    items.forEach(i => ul.appendChild(el('li', '', i)));
    f.appendChild(ul);
    return f;
  }

  // ---------------------------------------------------------------------
  // Dashboard
  // ---------------------------------------------------------------------
  function renderDashboard() {
    const root = document.querySelector('.dashboard');
    root.innerHTML = '';
    const dash = DATA.dashboard || {};
    const overview = dash.repo_overview || {};

    const summary = el('div', 'card');
    summary.appendChild(el('h3', '', 'Repo overview'));
    const grid = el('div', 'stat-grid');
    [
      ['Commits', overview.commits],
      ['Pull requests', overview.pull_requests],
      ['Issues', overview.issues],
      ['LLM described', overview.llm_described_commits],
    ].forEach(([k, v]) => {
      const cell = el('div', 'stat');
      cell.appendChild(el('div', 'label', k));
      cell.appendChild(el('div', 'value', v != null ? v : '—'));
      grid.appendChild(cell);
    });
    summary.appendChild(grid);
    root.appendChild(summary);

    const tc = el('div', 'card');
    tc.appendChild(el('h3', '', 'Top contributors (90d)'));
    tc.appendChild(table(
      ['Author', 'Window commits', 'All-time', 'Files', 'PRs'],
      (dash.top_contributors_90d || []).map(c => [
        c.author_name || c.author_email || 'unknown',
        c.window_commits, c.all_time_commits, c.window_files_changed, c.window_prs_authored,
      ]),
    ));
    root.appendChild(tc);

    const hp = el('div', 'card');
    hp.appendChild(el('h3', '', 'Hot path-prefixes (90d)'));
    hp.appendChild(table(
      ['Prefix', 'File touches', 'Distinct commits'],
      (dash.hot_paths_90d || []).map(p => [p.path_prefix, p.file_touches, p.distinct_commits]),
    ));
    root.appendChild(hp);

    const act = el('div', 'card');
    act.appendChild(el('h3', '', 'Activity over time (commits per month)'));
    const months = Object.keys(dash.activity_overall || {}).sort();
    if (months.length) {
      const max = Math.max(...months.map(m => dash.activity_overall[m]));
      const chart = el('div', 'activity-overall');
      months.forEach(m => {
        const bar = el('div', 'bar');
        bar.style.height = (100 * dash.activity_overall[m] / max) + '%';
        bar.title = `${m}: ${dash.activity_overall[m]}`;
        chart.appendChild(bar);
      });
      act.appendChild(chart);
      const axis = el('div', 'activity-axis');
      axis.appendChild(el('span', '', months[0]));
      axis.appendChild(el('span', '', months[months.length - 1]));
      act.appendChild(axis);
    } else {
      act.appendChild(el('div', 'empty-state', 'No activity recorded.'));
    }
    root.appendChild(act);
  }

  function table(headers, rows) {
    const t = document.createElement('table');
    const thead = document.createElement('thead');
    const tr = document.createElement('tr');
    headers.forEach(h => tr.appendChild(el('th', '', h)));
    thead.appendChild(tr);
    t.appendChild(thead);
    const tbody = document.createElement('tbody');
    if (!rows.length) {
      const tr2 = document.createElement('tr');
      const td = el('td', '', '— no data —');
      td.colSpan = headers.length;
      td.style.color = '#8b949e';
      tr2.appendChild(td);
      tbody.appendChild(tr2);
    } else {
      rows.forEach(row => {
        const tr2 = document.createElement('tr');
        row.forEach(cell => tr2.appendChild(el('td', '', cell != null ? String(cell) : '—')));
        tbody.appendChild(tr2);
      });
    }
    t.appendChild(tbody);
    return t;
  }

  // ---------------------------------------------------------------------
  // Authors
  // ---------------------------------------------------------------------
  function renderAuthors() {
    const list = document.getElementById('authors-list');
    list.innerHTML = '';
    (DATA.authors || [])
      .slice()
      .sort((a, b) => (b.commit_count || 0) - (a.commit_count || 0))
      .forEach(a => {
        const li = el('li', 'author-row');
        li.dataset.id = a.id;
        const name = a.primary_login || a.primary_name || a.primary_email || 'unknown';
        li.appendChild(el('div', 'name', name));
        li.appendChild(el(
          'div',
          'stats',
          `${a.commit_count} commits · ${a.pr_count} PRs · ${a.issue_count} issues`,
        ));
        li.addEventListener('click', () => {
          document.querySelectorAll('.author-row.selected').forEach(x => x.classList.remove('selected'));
          li.classList.add('selected');
          renderAuthorDetail(a);
        });
        list.appendChild(li);
      });
  }

  function renderAuthorDetail(a) {
    const root = document.getElementById('author-detail');
    root.classList.remove('empty');
    root.innerHTML = '';
    const name = a.primary_login || a.primary_name || a.primary_email || 'unknown';
    root.appendChild(el('h2', '', name));
    const sub = el(
      'div',
      'meta-line',
      [a.primary_email, a.primary_name].filter(Boolean).join(' · ') ||
        '(no email/name on record)',
    );
    root.appendChild(sub);

    const grid = el('div', 'stat-grid');
    [
      ['Commits', a.commit_count],
      ['PRs authored', a.pr_count],
      ['Issues raised', a.issue_count],
      ['First seen', (a.first_seen || '—').slice(0, 10)],
      ['Last seen', (a.last_seen || '—').slice(0, 10)],
    ].forEach(([k, v]) => {
      const cell = el('div', 'stat');
      cell.appendChild(el('div', 'label', k));
      cell.appendChild(el('div', 'value', v != null ? v : '—'));
      grid.appendChild(cell);
    });
    root.appendChild(grid);

    root.appendChild(el('div', 'section-title', 'Recent activity (180d)'));
    if (!(a.recent_activity || []).length) {
      root.appendChild(el('div', 'empty-state', 'No recent activity in window.'));
      return;
    }
    a.recent_activity.forEach(r => {
      const row = el('div', 'activity-row');
      row.appendChild(el('span', 'kind', (r.kind || '').toUpperCase()));
      row.appendChild(document.createTextNode(' #' + (r.id || '')));
      row.appendChild(document.createTextNode(' ' + (r.title || r.subject || '')));
      row.appendChild(el('div', 'at', r.at || ''));
      root.appendChild(row);
    });
  }

  renderDashboard();
  renderAuthors();
})();
