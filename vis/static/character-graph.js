/**
 * Character Relationship Graph — D3 force-directed graph.
 *
 * Reads the direct /api endpoint or the unified console's /world-api gateway.
 * and renders nodes + relationship edges inside the #graph-svg of the
 * "人物关系图" tab.
 */

(function () {
    let allData = null;
    let svg, gMain, gEdges, gNodes;
    let simulation = null;
    let initialized = false;
    const IS_UNIFIED_WORLD_STUDIO =
        window.location.pathname === "/world" || window.location.pathname.startsWith("/world/");
    const WORLD_API_PREFIX = IS_UNIFIED_WORLD_STUDIO ? "/world-api" : "/api";

    async function init() {
        if (initialized) return;
        initialized = true;

        const input = document.getElementById("graph-filter-search");
        if (input) input.addEventListener("input", onSearch);
        const closeBtn = document.getElementById("btn-close-char-detail");
        if (closeBtn) closeBtn.addEventListener("click", closeDetail);
        const scope = document.getElementById("graph-scope");
        if (scope) scope.addEventListener("change", reload);
        try { await initializeScopeControls(); } catch (err) { console.warn("Graph scope controls unavailable.", err); }
        await reload();
    }

    let activeWorldId = "";

    async function initializeScopeControls() {
        const scope = document.getElementById("graph-scope");
        if (!scope) return;
        const resp = await fetch(`${WORLD_API_PREFIX}/world-registry`);
        if (!resp.ok) throw new Error(`world registry: ${resp.status}`);
        const data = await resp.json();
        const activeWorld = (data.worlds || []).find((item) => item.world_id === data.active_world_id);
        activeWorldId = activeWorld?.world_id || "";
        const cities = activeWorld?.cities || (activeWorld?.city_ids || []).map((city_id) => ({ city_id, city_name: city_id }));
        scope.innerHTML = [
            '<option value="__world__" data-ui-text>整个当前世界</option>',
            ...cities.filter((city) => city.lifecycle_status !== "archived").map((city) =>
                `<option value="${escapeHtml(city.city_id)}">${escapeHtml(city.city_name || city.city_id)}</option>`
            ),
        ].join("");
    }

    async function reload() {
        if (simulation) simulation.stop();
        const empty = document.getElementById("graph-empty");
        try {
            const selectedScope = document.getElementById("graph-scope")?.value || "__world__";
            const params = new URLSearchParams(activeWorldId
                ? { scope: "world", world_id: activeWorldId }
                : { scope: "current_city" });
            if (selectedScope !== "__world__") params.set("city_id", selectedScope);
            const resp = await fetch(`${WORLD_API_PREFIX}/characters_data?${params.toString()}`);
            allData = resp.ok ? await resp.json() : null;
        } catch (err) {
            allData = null;
        }

        if (!allData || !Array.isArray(allData.characters) || allData.characters.length === 0) {
            if (empty) empty.classList.remove("hidden");
            const info = document.getElementById("graph-info-bar");
            info.setAttribute("data-ui-output", "");
            info.textContent = "当前范围没有可用人物数据";
            document.getElementById("graph-stats").innerHTML = "<i data-ui-output>无数据</i>";
            d3.select("#graph-svg").selectAll("*").remove();
            return;
        }
        if (empty) empty.classList.add("hidden");

        const name = allData.scope === "world"
            ? `世界 ${allData.world_id || ""}`
            : (allData.city_name || allData.city_id || "characters");
        const chars = allData.characters || [];
        const rels = allData.relationships || [];
        document.getElementById("graph-info-bar").textContent =
            `${name} — ${chars.length}人, ${rels.length}条关系`;

        renderStats();
        renderGraph();

    }

    async function refreshCurrentWorld() {
        await initializeScopeControls();
        await reload();
    }

    function ensureSize() {
        if (!simulation) return;
        const container = document.getElementById("graph-container");
        const W = container.clientWidth || 800;
        const H = container.clientHeight || 600;
        simulation.force("center", d3.forceCenter(W / 2, H / 2));
        simulation.alpha(0.3).restart();
    }

    function renderGraph() {
        const container = document.getElementById("graph-container");
        const W = container.clientWidth || 800;
        const H = container.clientHeight || 600;

        d3.select("#graph-svg").selectAll("*").remove();
        svg = d3.select("#graph-svg").attr("width", W).attr("height", H);

        const g = svg.append("g");
        svg.call(d3.zoom().scaleExtent([0.15, 4]).on("zoom", (e) => g.attr("transform", e.transform)));
        gMain = g;

        const chars = allData.characters || [];
        const rels = allData.relationships || [];

        const charById = {};
        chars.forEach((c) => { charById[c.character_id] = c; });

        const nodes = chars.map((c) => ({
            id: c.character_id,
            name: c.name,
            age: c.age,
            gender: c.gender,
            occupation: c.occupation,
            ...c,
        }));

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

        gEdges = g.append("g").attr("class", "edges");
        const link = gEdges.selectAll("line").data(edges).join("line")
            .attr("class", (d) => "edge " + d.type)
            .attr("stroke-width", (d) => d.type === "spouse" ? 3 : (d.type === "parent" || d.type === "child") ? 2 : 1.2)
            .attr("stroke-opacity", 0.6);

        const edgeLabels = g.append("g").attr("class", "edge-labels");
        const el = edgeLabels.selectAll("text").data(edges).join("text")
            .attr("text-anchor", "middle")
            .attr("dy", -4)
            .attr("font-size", 9)
            .attr("fill", "#a6adc8")
            .text((d) => d.type);

        gNodes = g.append("g").attr("class", "nodes");
        const node = gNodes.selectAll("g").data(nodes).join("g")
            .attr("class", "node")
            .call(d3.drag()
                .on("start", (e, d) => { if (!e.active) simulation.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; })
                .on("drag", (e, d) => { d.fx = e.x; d.fy = e.y; })
                .on("end", (e, d) => { if (!e.active) simulation.alphaTarget(0); d.fx = null; d.fy = null; }))
            .on("click", (e, d) => { e.stopPropagation(); showDetail(d); });

        node.append("circle")
            .attr("r", (d) => Math.max(14, Math.min(22, (d.age || 30) / 3 + 8)))
            .attr("class", (d) => "node-circle " + (d.gender === "female" ? "female" : "male"));

        node.append("text")
            .attr("dy", (d) => -Math.max(16, Math.min(24, (d.age || 30) / 3 + 10)))
            .attr("text-anchor", "middle")
            .attr("font-size", 10)
            .attr("fill", "#cdd6f4")
            .text((d) => (d.name || "").length > 6 ? d.name.slice(0, 6) + ".." : (d.name || ""));

        simulation = d3.forceSimulation(nodes)
            .force("link", d3.forceLink(edges).id((d) => d.id).distance((d) => d.type === "spouse" ? 60 : (d.type === "parent" || d.type === "child") ? 70 : 100))
            .force("charge", d3.forceManyBody().strength(-300))
            .force("center", d3.forceCenter(W / 2, H / 2))
            .force("collision", d3.forceCollide(35))
            .on("tick", () => {
                link.attr("x1", (d) => d.source.x).attr("y1", (d) => d.source.y)
                    .attr("x2", (d) => d.target.x).attr("y2", (d) => d.target.y);
                el.attr("x", (d) => (d.source.x + d.target.x) / 2)
                  .attr("y", (d) => (d.source.y + d.target.y) / 2);
                node.attr("transform", (d) => `translate(${d.x},${d.y})`);
            });

        svg.on("click", () => closeDetail());
    }

    function renderStats() {
        const chars = allData.characters || [];
        const rels = allData.relationships || [];
        const ages = chars.map((c) => c.age).filter((a) => a);

        const relTypeCount = {};
        for (const r of rels) {
            relTypeCount[r.type] = (relTypeCount[r.type] || 0) + 1;
        }
        const genderCount = {};
        for (const c of chars) {
            const g = c.gender === "female" ? "女" : c.gender === "male" ? "男" : "其他";
            genderCount[g] = (genderCount[g] || 0) + 1;
        }
        const districtCount = {};
        for (const c of chars) {
            const d = (c.mobility || {}).home_district_name || "未知";
            districtCount[d] = (districtCount[d] || 0) + 1;
        }

        let html = "";
        html += `<b data-ui-text>总人数:</b> ${chars.length}<br>`;
        html += `<b data-ui-text>关系边:</b> ${rels.length}<br>`;
        if (ages.length) {
            html += `<b data-ui-text>年龄:</b> ${Math.min(...ages)}–${Math.max(...ages)} (avg ${(ages.reduce((a,b)=>a+b,0)/ages.length).toFixed(0)})<br>`;
        }
        html += "<br><b data-ui-text>性别:</b><br>";
        for (const [g, n] of Object.entries(genderCount)) html += `  ${g}: ${n}<br>`;
        html += "<br><b data-ui-text>关系类型:</b><br>";
        const relSorted = Object.entries(relTypeCount).sort((a, b) => b[1] - a[1]);
        if (relSorted.length === 0) {
            html += "  <i data-ui-output>关系列表为空，请检查 LLM 是否完成关系生成</i><br>";
        } else {
            for (const [t, n] of relSorted) html += `  ${t}: ${n}<br>`;
        }
        html += "<br><b data-ui-text>居住区域:</b><br>";
        for (const [d, n] of Object.entries(districtCount).sort((a, b) => b[1] - a[1]).slice(0, 8)) {
            html += `  ${n}× <span data-ui-text>${escapeHtml(d)}</span><br>`;
        }
        document.getElementById("graph-stats").innerHTML = html;
    }

    function onSearch() {
        const q = document.getElementById("graph-filter-search").value.trim().toLowerCase();
        const results = document.getElementById("graph-filter-results");

        if (!q) {
            results.innerHTML = "";
            gNodes?.selectAll(".node").style("opacity", 1);
            gEdges?.selectAll("line").style("opacity", 0.6);
            return;
        }
        const chars = allData.characters || [];
        const matches = chars.filter((c) => (c.name || "").toLowerCase().includes(q));
        results.innerHTML = matches.slice(0, 8).map((c) =>
            `<div class="search-result" data-id="${c.character_id}">${c.name} (${c.occupation || ""})</div>`
        ).join("");
        results.querySelectorAll(".search-result").forEach((el) => {
            el.addEventListener("click", () => highlightNode(el.dataset.id));
        });
        const matchIds = new Set(matches.map((c) => c.character_id));
        gNodes?.selectAll(".node").style("opacity", (d) => matchIds.has(d.id) ? 1 : 0.15);
        gEdges?.selectAll("line").style("opacity", (d) =>
            matchIds.has(d.source?.id) || matchIds.has(d.target?.id) ? 0.9 : 0.08
        );
    }

    function showDetail(c) {
        document.getElementById("char-detail-name").textContent = c.name || c.character_id || "";
        document.getElementById("char-detail-panel").classList.remove("hidden");

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
        html += row("", (c.personality_traits || []).map((t) => `<span class="tag">${escapeHtml(t)}</span>`).join(" "));
        html += sec("偏好");
        html += row("喜好", (prefs.prefer || []).map(escapeHtml).join(" · "));
        html += row("避讳", (prefs.avoid || []).map(escapeHtml).join(" · "));
        if (prefs.dietary) html += row("饮食", escapeHtml(prefs.dietary));
        if (prefs.budget) html += row("预算", escapeHtml(prefs.budget));
        html += sec("出行");
        html += row("居住", escapeHtml(mob.home_district_name || mob.home_district_id || "—"));
        html += row("工作", escapeHtml(mob.work_district_name || mob.work_district_id || "—"));
        if (typeof mob.lat === "number" && typeof mob.lng === "number") {
            html += row("坐标", `${mob.lat.toFixed(4)}, ${mob.lng.toFixed(4)}`);
        }
        if (mob.preferred_modes) html += row("出行工具", (mob.preferred_modes || []).join(", "));
        if (mob.max_walk_m !== undefined) html += row("最大步行", mob.max_walk_m + "m");
        html += sec("可用性");
        html += row("空闲", (avail.typical_free_windows || []).join(" · "));
        html += row("守时", avail.punctuality);
        html += sec("沟通");
        html += row("风格", escapeHtml(comm.style || ""));
        html += row("速度", comm.response_latency);
        if (Array.isArray(comm.preferred_channels) && comm.preferred_channels.length) {
            html += row("渠道", (comm.preferred_channels || []).join(", "));
        }
        html += sec("约束");
        const constraints = c.constraints || [];
        if (Array.isArray(constraints) && constraints.length) {
            for (const con of constraints) {
                const isHidden = con.constraint_type === "hidden";
                html += `<div class="row constraint ${isHidden ? "hidden-constraint" : ""}">`;
                html += `<span class="tag" data-ui-text>${isHidden ? "隐藏" : "显式"}</span>`;
                html += escapeHtml(con.content || "");
                if (con.reveal_trigger) html += `<br><small><span data-ui-text>触发:</span> ${escapeHtml(con.reveal_trigger)}</small>`;
                html += "</div>";
            }
        } else {
            html += `<div class="row"><span class="empty" data-ui-text>无约束</span></div>`;
        }
        html += sec("关系");
        const rels = (allData && allData.relationships) || [];
        const cid = c.character_id || c.id;
        const myRels = rels.filter((r) => r.character_id === cid || r.related_id === cid);
        if (myRels.length) {
            const charById = {};
            (allData.characters || []).forEach((ch) => { charById[ch.character_id] = ch; });
            for (const r of myRels) {
                const otherId = r.character_id === cid ? r.related_id : r.character_id;
                const other = charById[otherId] || {};
                html += `<div class="row">`;
                html += `<span class="tag">${escapeHtml(r.type || "")}</span>`;
                html += ` ${escapeHtml(r.subtype || "")} → <b style="color:#89b4fa;cursor:pointer" data-id="${otherId}" class="rel-link">${escapeHtml(other.name || otherId)}</b>`;
                if (r.context) html += `<br><small>${escapeHtml(r.context)}</small>`;
                html += "</div>";
            }
        } else {
            html += '<div class="row"><span class="empty" data-ui-text>无关系记录</span></div>';
        }
        html += sec("来源");
        html += row("", escapeHtml(c.source || "unknown"));

        const detailContent = document.getElementById("char-detail-content");
        detailContent.innerHTML = html;
        detailContent.querySelectorAll(".rel-link").forEach((el) => {
            el.addEventListener("click", (ev) => {
                ev.stopPropagation();
                highlightNode(el.dataset.id);
            });
        });
    }

    function closeDetail() {
        document.getElementById("char-detail-panel").classList.add("hidden");
    }

    function row(label, value) {
        if (value === undefined || value === null || value === "") return "";
        return `<div class="row"><span class="label" data-ui-text>${label}</span><span class="val">${value}</span></div>`;
    }
    function sec(title) {
        return `<div class="section-title" data-ui-text>${title}</div>`;
    }
    function escapeHtml(value) {
        return String(value == null ? "" : value)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#039;");
    }
    function highlightNode(id) {
        for (const c of (allData.characters || [])) {
            if (c.character_id === id || c.id === id) {
                showDetail(c);
                return;
            }
        }
    }

    window.CharacterGraph = { init, ensureSize, reload, refreshCurrentWorld };
})();
