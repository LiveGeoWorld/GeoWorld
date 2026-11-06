// Test Results Visualizer — with map + expandable LLM trace

let allResults = [];
let runsMode = false;
let map = null;
let mapLayers = { gt: [], agent: [], core: [] };

const COLORS = { core: '#3388ff', waypoint: '#e67e22', gt: '#4ecca3', agent: '#e74c3c' };

async function init() {
  try {
    // Check for runs mode
    const runsResp = await fetch('/api/runs');
    const runsData = await runsResp.json();
    const runs = runsData.runs || [];

    if (runs.length > 0) {
      runsMode = true;
      renderRunSelector(runs);
      selectRun(runs[0].run_id);
    } else {
      await loadResults();
    }
  } catch (e) { console.error(e); }
}

function renderRunSelector(runs) {
  const area = document.getElementById('run-selector-area');
  area.classList.remove('hidden');
  let html = '<select id="run-select" class="run-select" onchange="selectRun(this.value)">';
  for (const r of runs) {
    html += `<option value="${r.run_id}">${r.run_id.slice(0,15)}... | ${r.model_name} | ${r.passed}/${r.task_count} PASS</option>`;
  }
  html += '</select>';
  area.innerHTML = html;
}

function selectRun(runId) {
  fetch(`/api/run/${encodeURIComponent(runId)}`)
    .then(r => r.json())
    .then(data => {
      allResults = data.results || [];
      document.getElementById('result-count').textContent = `共 ${allResults.length} 个`;
      const passed = allResults.filter(r => r.grade === 'PASS').length;
      document.getElementById('summary-bar').textContent = `${passed}/${allResults.length} PASS`;
      renderList();
    });
}

async function loadResults() {
  const resp = await fetch('/api/results');
  const data = await resp.json();
  allResults = data.results || [];
  document.getElementById('result-count').textContent = `共 ${allResults.length} 个`;
  const passed = allResults.filter(r => r.grade === 'PASS').length;
  document.getElementById('summary-bar').textContent = `${passed}/${allResults.length} PASS`;
  renderList();
}

function renderList() {
  const typeLabels = {
    route_planning: '路线规划', place_grounding: '地理感知', place_discovery: '地点发现',
    multi_entity: '多主体协同', itinerary: '行程规划', safety_planning: '安全规划',
    region_aware: '地域感知', dynamic_replanning: '重规划',
  };
  document.getElementById('result-list').innerHTML = allResults.map(r => {
    const folder = r.task_folder || '';
    let shortName = folder;
    if (!shortName) {
      const m = (r.task_id || '').match(/_(route_planning|place_grounding|place_discovery|multi_entity|itinerary|safety_planning|region_aware|dynamic_replanning)_(\d{4})$/);
      const type = m ? m[1] : '';
      const idx = m ? m[2] : '';
      shortName = (typeLabels[type] || type) + '_' + idx;
    }
    const prompt = (r.user_prompt || '').substring(0, 60);
    return `<div class="r-item" data-task-id="${r.task_id}" onclick="selectResult('${r.task_id}')">
      <div class="score">
        <span class="${r.grade === 'PASS' ? 'pass' : r.grade === 'PARTIAL' ? 'partial' : 'fail'}">
          ${r.grade} · ${r.passed}/${r.total} (${r.total_score}%)
        </span>
        <span class="r-type">${shortName || r.task_id}</span>
      </div>
      <div class="preview">${esc(prompt || '')}</div>
    </div>`;
  }).join('');
}

// ========== Result Selection ==========

function selectResult(taskId) {
  document.querySelectorAll('.r-item').forEach(el => el.classList.remove('active'));
  const item = document.querySelector(`.r-item[data-task-id="${taskId}"]`);
  if (item) item.classList.add('active');
  for (const r of allResults) {
    if (r.task_id === taskId) { renderDetail(r); loadMapCompare(taskId); return; }
  }
}

// ========== Map ==========

function initMap() {
  if (map) return;
  const el = document.getElementById('compare-map');
  el.classList.remove('hidden');
  map = L.map(el).setView([35.68, 139.76], 12);
  const googleTile = L.tileLayer('https://mt{s}.google.com/vt/lyrs=m&x={x}&y={y}&z={z}', {
    subdomains: ['0','1','2','3'], attribution: '&copy; Google', maxZoom: 20,
  }).addTo(map);
  const osmTile = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '&copy; OpenStreetMap contributors', maxZoom: 19,
  });
  googleTile.on('tileerror', function() { map.removeLayer(googleTile); osmTile.addTo(map); });
}

function clearMap(keys) {
  for (const k of keys) {
    for (const l of mapLayers[k]) l.remove();
    mapLayers[k] = [];
  }
}

async function loadMapCompare(taskId) {
  try {
    const resp = await fetch(`/api/route_compare?task_id=${encodeURIComponent(taskId)}`);
    const data = await resp.json();
    initMap();
    renderMapCompare(data);
  } catch (e) { /* no map data available */ }
}

function renderMapCompare(data) {
  clearMap(['gt', 'agent', 'core']);
  const bounds = [];

  // Core POI
  for (const p of (data.core_places || [])) {
    if (!p.lat) continue;
    const isWP = p.role === 'waypoint';
    const c = isWP ? COLORS.waypoint : COLORS.core;
    const m = L.circleMarker([p.lat, p.lng], { radius: isWP ? 6 : 8, fillColor: c, color: '#fff', weight: 2, fillOpacity: 1 }).addTo(map);
    m.bindTooltip(`<b>${p.name}</b> (${p.role})`, {permanent: false});
    mapLayers.core.push(m);
    bounds.push([p.lat, p.lng]);

    const labelText = p.role === 'origin' ? '起' : p.role === 'destination' ? '终' : p.role === 'waypoint' ? '途经' : '';
    if (labelText) {
      const lbl = L.marker([p.lat, p.lng], {
        icon: L.divIcon({className: 'poi-label', html: `<div style="background:${c};color:#fff;padding:1px 5px;border-radius:3px;font-size:10px;white-space:nowrap;position:absolute;left:50%;bottom:10px;transform:translateX(-50%);pointer-events:none">${labelText}</div>`, iconSize:[0,0]}),
      }).addTo(map);
      mapLayers.core.push(lbl);
    }
  }

  // GT route segments (may include waypoint legs)
  const segments = data.gt_route_segments || [];
  for (const seg of segments) {
    const pts = decodeRoutePolyline(seg);
    if (pts.length >= 2) {
      const poly = L.polyline(pts, { color: COLORS.gt, weight: 4, opacity: 1 }).addTo(map);
      const legLabel = seg.leg && seg.leg !== 'direct' ? ` [${seg.leg}]` : '';
      poly.bindTooltip(`✅ GT${legLabel}: ${seg.origin_name||'?'} → ${seg.dest_name||'?'}<br>${seg.mode||''} | ${((seg.distance_m||0)/1000).toFixed(1)}km | ${Math.round((seg.duration_s||0)/60)}min`);
      mapLayers.gt.push(poly);
      bounds.push(pts[0], pts[pts.length - 1]);
    }
  }

  // Agent route(s) — may be list
  const agentRoutes = data.agent_routes || [];
  if (agentRoutes.length === 0 && data.agent_route) {
    agentRoutes.push(data.agent_route);
  }
  for (const ag of agentRoutes) {
    if (ag.polyline_encodings) {
      const pts = decodeRoutePolyline(ag);
      if (pts.length >= 2) {
        const poly = L.polyline(pts, { color: COLORS.agent, weight: 3, opacity: 0.8, dashArray: '8,4' }).addTo(map);
        poly.bindTooltip(`🤖 Agent: ${ag.origin_name} → ${ag.dest_name}<br>${ag.mode} | ${(ag.distance_m/1000).toFixed(1)}km | ${Math.round(ag.duration_s/60)}min`);
        mapLayers.agent.push(poly);
        bounds.push(pts[0], pts[pts.length - 1]);
      }
    }
  }

  if (bounds.length > 0) map.fitBounds(bounds, { padding: [30, 30], maxZoom: 15 });
}

// ========== Detail Panel ==========

function renderDetail(r) {
  let h = '';

  h += `<div class="section"><div class="section-title">📋 Task Prompt</div>
    <div class="prompt-box">${esc(r.user_prompt || '')}</div></div>`;

  const cls = r.grade === 'PASS' ? 'grade-PASS' : r.grade === 'PARTIAL' ? 'grade-PARTIAL' : 'grade-FAIL';
  h += `<div class="score-bar">
    <span class="score-num">${r.total_score}%</span>
    <span class="score-grade ${cls}">${r.grade}</span>
    <span style="font-size:12px">${r.passed}/${r.total}</span>
    <span style="font-size:11px;color:var(--text2);margin-left:auto">${(r.duration_s||0).toFixed(1)}s · ${(r.tokens_used||{}).total||0} tokens</span>
  </div>`;

  h += `<div class="section"><div class="section-title" data-ui-text>📊 指标详情</div>
    <table class="metrics-table"><tr><th data-ui-text>指标</th><th data-ui-text>结果</th><th data-ui-text>详情</th></tr>`;
  for (const m of (r.metrics || [])) {
    h += `<tr><td>${m.label}</td><td class="${m.pass?'pass':'fail'}">${m.pass?'✅':'❌'}</td><td style="font-size:11px">${esc(m.detail)}</td></tr>`;
  }
  h += '</table></div>';

  // GT vs Agent comparison
  h += `<div class="section"><div class="section-title" data-ui-text>🔍 GT 对比 Agent</div><div class="comp-grid">`;
  const fa = r.final_answer || {};
  const gs = r.gt_route_summary || {};
  const routeIds = fa.route_ids || (fa.route_id ? [fa.route_id] : []);
  const rows = [['出行方式', gs.mode, fa.mode]];
  if (routeIds.length > 1) {
    rows.push(['路线段数', String(routeIds.length), String(routeIds.length)]);
  }
  rows.push(['时间限制', (gs.time_limit_min||'?')+'min', '']);
  for (const [l, gv, av] of rows) {
    const match = av !== undefined && av !== '' && av === gv;
    h += `<div class="comp-card gt"><div class="label">GT ${l}</div><div class="value">${esc(String(gv))}</div></div>`;
    if (av !== undefined && av !== '') h += `<div class="comp-card agent"><div class="label">Agent ${l}</div><div class="value ${match?'match':'mismatch'}">${esc(String(av))}</div></div>`;
  }
  h += '</div></div>';

  // Map legend
  if (document.getElementById('compare-map').classList.contains('hidden') === false) {
    h += `<div class="map-legend">
      <span><span class="sw" style="background:${COLORS.gt}"></span>GT</span>
      <span><span class="sw" style="background:${COLORS.agent}"></span>Agent</span>
      <span><span class="sw" style="background:${COLORS.core}"></span>起/终</span>
      <span><span class="sw" style="background:${COLORS.waypoint}"></span>途经</span>
    </div>`;
  }

  // Trace
  h += `<div class="section"><div class="section-title" data-ui-text>📜 Agent 决策轨迹</div>`;
  const trace = r.trace || [];
  for (let i = 0; i < trace.length; i++) {
    const t = trace[i];
    const isFinal = t.tool === 'final_answer';
    h += `<div class="trace-step${isFinal?' final':''}">
      <div class="step-hd">
        <span class="step-num">Step ${t.seq}</span>
        <span class="step-tool">${t.tool}</span>
        <span class="step-ms">${t.duration_ms}ms</span>
        <span class="step-toggle" onclick="toggleExpand('trace-${r.task_id}-${i}')" data-ui-text>▶ 详情</span>
      </div>
      <div class="step-detail">${formatResult(t.tool, t.result)}</div>
      <div class="step-args"><code>${JSON.stringify(t.args)}</code></div>
      <div class="trace-expand hidden" id="trace-${r.task_id}-${i}">
        ${t.llm_output ? renderLlmInfo(t) : ''}
      </div>
    </div>`;
  }
  if (trace.length === 0) h += '<div style="color:var(--text2);font-size:12px" data-ui-text>无工具调用记录</div>';
  h += '</div>';

  document.getElementById('detail').innerHTML = h;
}

function renderLlmInfo(t) {
  let h = '';
  if (t.llm_output) {
    const out = t.llm_output;
    h += `<div class="llm-block"><div class="llm-title">LLM Output</div>`;
    h += `<div>tool: <b>${out.tool||''}</b> · finish: ${out.finish_reason||''}</div>`;
    h += `<div class="llm-json"><code>${esc(JSON.stringify(out.arguments||{}, null, 2))}</code></div>`;
    if (out.usage) h += `<div style="font-size:10px;color:var(--text2)">tokens: prompt=${out.usage.prompt_tokens||0}, completion=${out.usage.completion_tokens||0}</div>`;
    h += `</div>`;
  }
  if (t.llm_input) {
    h += `<div class="llm-block"><div class="llm-title">LLM Input (system prompt)</div>`;
    h += `<div class="llm-text">${esc((t.llm_input.system_prompt||'').slice(0,500))}</div>`;
    h += `</div>`;
  }
  return h;
}

function toggleExpand(id) {
  const el = document.getElementById(id);
  if (!el) return;
  const isHidden = el.classList.contains('hidden');
  el.classList.toggle('hidden');
  const btn = el.parentElement.querySelector('.step-toggle');
  if (btn) btn.textContent = isHidden ? '▼ 收起' : '▶ 详情';
}

function formatResult(tool, result) {
  if (tool === 'search_places') {
    const names = result.names || [];
    return `找到 ${result.count||0} 个地点: ${names.slice(0,5).join(', ')}${names.length>5?'...':''}`;
  }
  if (tool === 'query_routes') {
    const s = result.summary || [];
    return `返回 ${result.count||0} 条路线: ${s.map(x=>`${x.mode} ${(x.distance_m/1000).toFixed(1)}km/${Math.round(x.duration_s/60)}min`).join(' | ')}`;
  }
  if (tool === 'get_place_detail') return `地点: ${result.name||(result.found?'找到':'未找到')}`;
  if (tool === 'final_answer') return `提交答案: ${result.status||''}`;
  return JSON.stringify(result).slice(0, 80);
}

function esc(s) { if (!s) return ''; return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

init();
