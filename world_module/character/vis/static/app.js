/**
 * Character Relationship Graph — D3.js force-directed graph.
 *
 * Loads characters.json via /api/data, renders nodes (characters)
 * and edges (relationships). Click a node to see full details.
 */

let allData = null;
let simulation = null;
let svg, gMain, gEdges, gNodes, gLabels;

// ============================================================
// Init
// ============================================================
document.addEventListener("DOMContentLoaded", async () => {
    try {
        const resp = await fetch("/api/data");
        allData = await resp.json();
        document.getElementById("info-bar").textContent =
            `${allData.city_name || allData.city_id || ""} — ${allData.characters.length}人, ${allData.relationships.length}条关系`;
        renderStats();
        renderGraph();
        document.getElementById("filter-search").addEventListener("input", onSearch);
        document.getElementById("btn-close-detail").addEventListener("click", closeDetail);
    } catch (err) {
        document.getElementById("info-bar").textContent = "Failed to load data: " + err.message;
    }
});

window.addEventListener("resize", () => {
    if (simulation) renderGraph();
});

// ============================================================
// Render graph
// ============================================================
function renderGraph() {
    const container = document.getElementById("graph-container");
    const W = container.clientWidth;
    const H = container.clientHeight;

    d3.select("#graph-svg").selectAll("*").remove();
    svg = d3.select("#graph-svg")
        .attr("width", W)
        .attr("height", H);

    // Zoom
    const g = svg.append("g");
    svg.call(d3.zoom().scaleExtent([0.15, 4]).on("zoom", (e) => g.attr("transform", e.transform)));
    gMain = g;

    const chars = allData.characters;
    const rels = allData.relationships;

    // Build node map
    const charById = {};
    chars.forEach(c => { charById[c.character_id] = c; });

    // Nodes
    const nodes = chars.map(c => ({
        id: c.character_id,
        name: c.name,
        age: c.age,
        gender: c.gender,
        occupation: c.occupation,
        ...c,
    }));

    // Edges — only those where both ends exist
    const edges = [];
    const edgeSet = new Set();
    for (const r of rels) {
        if (!charById[r.character_id] || !charById[r.related_id]) continue;
        const key = [r.character_id, r.related_id].sort().join("|");
        if (edgeSet.has(key)) continue;
        edgeSet.add(key);
        edges.push({
            source: r.character_id,
            target: r.related_id,
            type: r.type,
            subtype: r.subtype,
            context: r.context,
        });
    }

    // Edge group
    gEdges = g.append("g").attr("class", "edges");

    const link = gEdges.selectAll("line")
        .data(edges)
        .join("line")
        .attr("class", d => "edge " + d.type)
        .attr("stroke-width", d => d.type === "spouse" ? 3 : d.type === "parent" || d.type === "child" ? 2 : 1.2)
        .attr("stroke-opacity", 0.6);

    // Edge labels
    const edgeLabels = g.append("g").attr("class", "edge-labels");
    const el = edgeLabels.selectAll("text")
        .data(edges)
        .join("text")
        .attr("class", "edge-label")
        .attr("text-anchor", "middle")
        .attr("dy", -4)
        .attr("font-size", 9)
        .attr("fill", "#a6adc8")
        .text(d => d.type);

    // Node group
    gNodes = g.append("g").attr("class", "nodes");

    const node = gNodes.selectAll("g")
        .data(nodes)
        .join("g")
        .attr("class", "node")
        .call(d3.drag()
            .on("start", (e, d) => { if (!e.active) simulation.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; })
            .on("drag", (e, d) => { d.fx = e.x; d.fy = e.y; })
            .on("end", (e, d) => { if (!e.active) simulation.alphaTarget(0); d.fx = null; d.fy = null; })
        )
        .on("click", (e, d) => { e.stopPropagation(); showDetail(d); });

    node.append("circle")
        .attr("r", d => Math.max(14, Math.min(22, d.age / 3 + 8)))
        .attr("class", d => "node-circle " + (d.gender === "female" ? "female" : "male"));

    node.append("text")
        .attr("dy", d => -Math.max(16, Math.min(24, d.age / 3 + 10)))
        .attr("text-anchor", "middle")
        .attr("font-size", 10)
        .attr("fill", "#cdd6f4")
        .text(d => d.name.length > 6 ? d.name.slice(0, 6) + ".." : d.name);

    // Simulation
    simulation = d3.forceSimulation(nodes)
        .force("link", d3.forceLink(edges).id(d => d.id).distance(d => d.type === "spouse" ? 60 : d.type === "parent" || d.type === "child" ? 70 : 100))
        .force("charge", d3.forceManyBody().strength(-300))
        .force("center", d3.forceCenter(W / 2, H / 2))
        .force("collision", d3.forceCollide(35))
        .on("tick", () => {
            link.attr("x1", d => d.source.x).attr("y1", d => d.source.y)
                .attr("x2", d => d.target.x).attr("y2", d => d.target.y);
            el.attr("x", d => (d.source.x + d.target.x) / 2)
              .attr("y", d => (d.source.y + d.target.y) / 2);
            node.attr("transform", d => `translate(${d.x},${d.y})`);
        });

    // Click background to close detail
    svg.on("click", () => closeDetail());
}

// ============================================================
// Detail panel
// ============================================================
function showDetail(c) {
    document.getElementById("detail-name").textContent = c.name;
    document.getElementById("detail-panel").classList.remove("hidden");

    const prefs = c.preferences || {};
    const mob = c.mobility || {};
    const avail = c.availability || {};
    const comm = c.communication || {};

    let html = "";
    html += row("ID", c.character_id || c.id);
    if (c.name_romanized) html += row("罗马音", c.name_romanized);
    html += row("年龄", c.age);
    html += row("性别", `<span data-ui-text>${escapeHtml(c.gender === "female" ? "女" : c.gender === "male" ? "男" : c.gender || "")}</span>`);
    html += row("职业", c.occupation);
    html += sec("个性");
    html += row("", (c.personality_traits || []).map(t => `<span class="tag">${t}</span>`).join(" "));
    html += sec("偏好");
    html += row("喜好", (prefs.prefer || []).join(" · "));
    html += row("避讳", (prefs.avoid || []).join(" · "));
    if (prefs.dietary) html += row("饮食", prefs.dietary);
    if (prefs.budget) html += row("预算", prefs.budget);
    html += sec("出行");
    html += row("居住", mob.home_district_name || mob.home_district_id || "—");
    html += row("工作", mob.work_district_name || mob.work_district_id || "—");
    if (mob.lat && mob.lng) html += row("坐标", `${mob.lat.toFixed(4)}, ${mob.lng.toFixed(4)}`);
    html += row("出行工具", (mob.preferred_modes || []).join(", "));
    html += row("最大步行", mob.max_walk_m + "m");
    html += sec("可用性");
    html += row("空闲", (avail.typical_free_windows || []).join(" · "));
    html += row("守时", avail.punctuality);
    html += sec("沟通");
    html += row("风格", comm.style);
    html += row("速度", comm.response_latency);
    if (comm.preferred_channels && comm.preferred_channels.length) html += row("渠道", (comm.preferred_channels || []).join(", "));
    html += sec("约束");
    const constraints = c.constraints || [];
    for (const con of constraints) {
        const isHidden = con.constraint_type === "hidden";
        html += `<div class="row constraint ${isHidden ? 'hidden-constraint' : ''}">`;
        html += `<span class="tag">${isHidden ? '隐藏' : '显式'}</span>`;
        html += con.content;
        if (con.reveal_trigger) html += `<br><small>触发: ${con.reveal_trigger}</small>`;
        html += "</div>";
    }
    html += sec("关系");
    const rels = allData.relationships || [];
    const myRels = rels.filter(r => r.character_id === c.id || r.related_id === c.id);
    if (myRels.length) {
        const charById = {};
        (allData.characters || []).forEach(ch => { charById[ch.character_id] = ch; });
        for (const r of myRels) {
            const otherId = r.character_id === c.id ? r.related_id : r.character_id;
            const other = charById[otherId] || {};
            html += `<div class="row">`;
            html += `<span class="tag">${r.type}</span>`;
            html += ` ${r.subtype || ''} → <b style="color:#89b4fa;cursor:pointer" onclick="event.stopPropagation();highlightNode('${otherId}')">${other.name || otherId}</b>`;
            if (r.context) html += `<br><small>${r.context}</small>`;
            html += "</div>";
        }
    } else {
        html += '<div class="row"><span class="empty" data-ui-text>无关系记录</span></div>';
    }
    html += sec("来源");
    html += row("", c.source || "unknown");

    document.getElementById("detail-content").innerHTML = html;
}

function closeDetail() {
    document.getElementById("detail-panel").classList.add("hidden");
}

function row(label, value) {
    if (!value && value !== 0) return "";
    return `<div class="row"><span class="label" data-ui-text>${label}</span><span class="val">${value}</span></div>`;
}
function sec(title) {
    return `<div class="section-title" data-ui-text>${title}</div>`;
}

function highlightNode(id) {
    for (const c of (allData.characters || [])) {
        if (c.character_id === id || c.id === id) {
            showDetail(c);
            return;
        }
    }
}

// ============================================================
// Stats
// ============================================================
function renderStats() {
    const chars = allData.characters || [];
    const rels = allData.relationships || [];
    const ages = chars.map(c => c.age).filter(a => a);

    const relTypeCount = {};
    for (const r of rels) {
        relTypeCount[r.type] = (relTypeCount[r.type] || 0) + 1;
    }

    const genderCount = {};
    for (const c of chars) {
        const g = c.gender === "female" ? "女" : c.gender === "male" ? "男" : "其他";
        genderCount[g] = (genderCount[g] || 0) + 1;
    }

    const occCount = {};
    for (const c of chars) {
        const o = c.occupation || "未知";
        occCount[o] = (occCount[o] || 0) + 1;
    }

    let html = "";
    html += `<b data-ui-text>总人数:</b> ${chars.length}<br>`;
    html += `<b data-ui-text>关系边:</b> ${rels.length}<br>`;
    if (ages.length) {
        html += `<b data-ui-text>年龄:</b> ${Math.min(...ages)}–${Math.max(...ages)} (avg ${(ages.reduce((a,b)=>a+b,0)/ages.length).toFixed(0)})<br>`;
    }
    html += `<br><b data-ui-text>性别:</b><br>`;
    for (const [g, n] of Object.entries(genderCount)) {
        html += `  ${g}: ${n}<br>`;
    }
    html += `<br><b data-ui-text>关系类型:</b><br>`;
    for (const [t, n] of Object.entries(relTypeCount).sort((a,b) => b[1]-a[1])) {
        html += `  ${t}: ${n}<br>`;
    }
    html += `<br><b data-ui-text>职业:</b><br>`;
    for (const [o, n] of Object.entries(occCount).sort((a,b) => b[1]-a[1]).slice(0, 8)) {
        html += `  ${n}× ${o}<br>`;
    }
    document.getElementById("stats-panel").innerHTML = html;
}

// ============================================================
// Search
// ============================================================
function onSearch() {
    const q = document.getElementById("filter-search").value.trim().toLowerCase();
    const results = document.getElementById("filter-results");

    if (!q) {
        results.innerHTML = "";
        // Reset all nodes
        gNodes?.selectAll(".node").style("opacity", 1);
        gEdges?.selectAll("line").style("opacity", 0.6);
        return;
    }

    const chars = allData.characters || [];
    const matches = chars.filter(c => c.name.toLowerCase().includes(q));

    results.innerHTML = matches.slice(0, 8).map(c =>
        `<div class="search-result" onclick="highlightNode('${c.character_id}')">${c.name} (${c.occupation})</div>`
    ).join("");

    // Dim non-matching nodes
    const matchIds = new Set(matches.map(c => c.character_id));
    gNodes?.selectAll(".node").style("opacity", d => matchIds.has(d.id) ? 1 : 0.15);
    gEdges?.selectAll("line").style("opacity", d =>
        matchIds.has(d.source?.id) || matchIds.has(d.target?.id) ? 0.9 : 0.08
    );
}
