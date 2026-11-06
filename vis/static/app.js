/**
 * GeoAgentBench v2 — Interactive City World Viewer
 * Leaflet.js + OSM/Google basemaps + REST API backend
 */

// ============================================================
// Extension points — reserved for future modules
// ============================================================
const VIS_EXTENSIONS = {
    layers: {},
    registerLayer(name, geoJsonLayer) { this.layers[name] = geoJsonLayer; },
    addDataSource(url, callback) { fetch(url).then(r => r.json()).then(callback).catch(console.error); },
};

// ============================================================
// Global state
// ============================================================
let map, layerPlaces, layerActors, layerDistricts, layerCorridors, actorClusterLayer, worldOverviewLayer;
let allPlacesGeoJSON = null;
let districtLookup = {};
let corridorLayersById = {};
let corridorGeometryIndex = [];
let activeCorridorLayer = null;
let activeDistrictArea = null;
let placeMarkers = [];
let actorMarkers = [];
let worldOverviewCities = [];
let hasAutoCentered = false;
let mapLoadGeneration = 0;
let viewerConfig = { google_maps_api_key: "", preferred_basemap: "google" };
let baseLayers = {};
let activeBasemap = "osm";
let currentMapScope = "city";
// The same UI is served directly by vis.server and under /world/ by the
// unified task console. Only the latter has the /world-api proxy.
const IS_UNIFIED_WORLD_STUDIO =
    window.location.pathname === "/world" || window.location.pathname.startsWith("/world/");
const WORLD_API_PREFIX = IS_UNIFIED_WORLD_STUDIO ? "/world-api" : "/api";

const GOOGLE_MUTANT_SCRIPT =
    "https://unpkg.com/leaflet.gridlayer.googlemutant@0.15.0/dist/Leaflet.GoogleMutant.js";

// ============================================================
// Init map
// ============================================================
async function initMap() {
    map = L.map("map").setView([35.68, 139.75], 13);

    baseLayers.osm = L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 19,
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a>',
    });
    baseLayers.osm.addTo(map);

    await setupGoogleBasemap();
    setBasemap(
        viewerConfig.preferred_basemap === "google" && baseLayers.google
            ? "google"
            : "osm"
    );

    map.on("zoomend", () => {
        refreshPlaceMarkerLayout();
        refreshActorMarkerLayout();
        refreshWorldScale();
    });
}

async function loadViewerConfig() {
    try {
        viewerConfig = await api("/api/config");
    } catch (err) {
        console.warn("Viewer config unavailable; using OSM basemap.", err);
        viewerConfig = { google_maps_api_key: "", preferred_basemap: "google" };
    }
    window.VIEWER_CONFIG = viewerConfig;
}

async function setupGoogleBasemap() {
    const select = document.getElementById("basemap-provider");
    const googleOption = select?.querySelector('option[value="google"]');
    const key = viewerConfig.google_maps_api_key || "";
    if (!key) {
        if (googleOption) googleOption.disabled = true;
        setBasemapStatus("未配置 Google Maps API key，已回退 OSM。请在 config.yaml 填写 google_maps.api_key，或设置 GOOGLE_MAPS_API_KEY。");
        return;
    }

    try {
        await loadScript(`https://maps.googleapis.com/maps/api/js?key=${encodeURIComponent(key)}&v=weekly`);
        await loadScript(GOOGLE_MUTANT_SCRIPT);
        if (!L.gridLayer?.googleMutant) {
            throw new Error("Leaflet GoogleMutant plugin did not register.");
        }
        baseLayers.google = L.gridLayer.googleMutant({
            type: "roadmap",
            maxZoom: 21,
        });
        if (googleOption) googleOption.disabled = false;
        setBasemapStatus("Google Maps 可用。可切换 OSM/Google 对比坐标偏移。");
    } catch (err) {
        console.error("Failed to initialize Google basemap:", err);
        if (googleOption) googleOption.disabled = true;
        setBasemapStatus("Google Maps 加载失败，已回退 OSM。请检查 API key、账单和 Maps JavaScript API。");
    }
}

function setBasemap(provider) {
    const next = provider === "google" && baseLayers.google ? "google" : "osm";
    for (const layer of Object.values(baseLayers)) {
        if (map.hasLayer(layer)) map.removeLayer(layer);
    }
    baseLayers[next].addTo(map);
    activeBasemap = next;
    const select = document.getElementById("basemap-provider");
    if (select) select.value = next;
    if (next === "google") {
        setBasemapStatus("当前底图：Google Maps。后端 Google 坐标应优先用它核对。");
    } else if (viewerConfig.google_maps_api_key) {
        setBasemapStatus("当前底图：OpenStreetMap。可切换 Google 检查中国区坐标偏移。");
    }
    bringMapLayersToReadableOrder();
}

function bindBasemapControls() {
    const select = document.getElementById("basemap-provider");
    if (!select) return;
    select.addEventListener("change", () => setBasemap(select.value));
}

function setBasemapStatus(message) {
    const status = document.getElementById("basemap-status");
    if (status) status.textContent = message;
}

function loadScript(src) {
    const existing = document.querySelector(`script[src="${src}"]`);
    if (existing) return Promise.resolve();
    return new Promise((resolve, reject) => {
        const script = document.createElement("script");
        script.src = src;
        script.async = true;
        script.defer = true;
        script.onload = resolve;
        script.onerror = () => reject(new Error(`Failed to load ${src}`));
        document.head.appendChild(script);
    });
}

// ============================================================
// API helpers
// ============================================================
async function api(path) {
    const worldPath = path.startsWith("/api/")
        ? WORLD_API_PREFIX + path.slice(4)
        : path;
    const resp = await fetch(worldPath);
    if (!resp.ok) throw new Error(`${path}: ${resp.status}`);
    return resp.json();
}

async function mapApiPost(path, payload) {
    const resp = await fetch(WORLD_API_PREFIX + path.slice(4), {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || `${path}: ${resp.status}`);
    return data;
}

async function initializeMapScopeControls() {
    const scopeSelect = document.getElementById("map-scope-select");
    if (!scopeSelect) return;
    const registry = await api("/api/world-registry");
    const activeWorld = (registry.worlds || []).find((world) => world.world_id === registry.active_world_id)
        || (registry.worlds || [])[0];
    const worldId = activeWorld?.world_id || "";
    const cities = activeWorld?.cities || (activeWorld?.city_ids || []).map((city_id) => ({ city_id, city_name: city_id }));
    scopeSelect.innerHTML = [
        '<option value="__world__" data-ui-text>世界总览</option>',
        ...cities.filter((city) => city.lifecycle_status !== "archived").map((city) =>
            `<option value="${escapeHtml(city.city_id)}">${escapeHtml(city.city_name || city.city_id)}</option>`
        ),
    ].join("");
    const currentScope = await api("/api/map-scope").catch(() => ({ scope: "city" }));
    scopeSelect.value = currentScope.scope === "world" ? "__world__" : (currentScope.city_id || scopeSelect.value);
    const initialHint = document.getElementById("map-scope-hint");
    if (initialHint) {
        initialHint.textContent = currentScope.scope === "world"
            ? `当前世界：${currentScope.world_id || worldId} · 世界总览`
            : `当前世界：${currentScope.world_id || worldId} · ${scopeSelect.options[scopeSelect.selectedIndex]?.text || "城市"}`;
    }
    const applyScope = async () => {
        const isWorld = scopeSelect.value === "__world__";
        const payload = { world_id: worldId, scope: isWorld ? "world" : "city", city_id: isWorld ? "" : scopeSelect.value };
        if (!payload.world_id || (!isWorld && !payload.city_id)) return;
        try {
            const applied = await mapApiPost("/api/worlds/select-scope", payload);
            hasAutoCentered = false;
            await loadAll();
            const hint = document.getElementById("map-scope-hint");
            if (hint) hint.textContent = applied.scope === "world"
                ? `当前世界：${applied.world_id} · 世界总览`
                : `当前世界：${applied.world_id} · ${scopeSelect.options[scopeSelect.selectedIndex].text}`;
        } catch (err) {
            console.error("Map scope switch failed:", err);
            const hint = document.getElementById("map-scope-hint");
            if (hint) hint.textContent = `切换失败，保留当前地图：${err.message || err}`;
        }
    };
    scopeSelect.onchange = applyScope;
}

// ============================================================
// Load all data
// ============================================================
async function loadAll() {
    const loadGeneration = ++mapLoadGeneration;
    try {
        clearTransientMapOverlays();
        for (const layer of [layerDistricts, layerCorridors, layerPlaces, layerActors, actorClusterLayer, worldOverviewLayer]) {
            if (layer && map.hasLayer(layer)) map.removeLayer(layer);
        }
        layerDistricts = null;
        layerCorridors = null;
        layerPlaces = null;
        layerActors = null;
        actorClusterLayer = null;
        worldOverviewLayer = null;
        worldOverviewCities = [];
        corridorLayersById = {};
        corridorGeometryIndex = [];
        placeMarkers = [];
        actorMarkers = [];
        districtLookup = {};
        const districtSelect = document.getElementById("filter-district");
        if (districtSelect) districtSelect.innerHTML = '<option value="">All districts</option>';
        const scopeState = await api("/api/map-scope");
        if (loadGeneration !== mapLoadGeneration) return;
        currentMapScope = scopeState.scope || "city";
        // Summary
        const summary = await api("/api/summary");
        const cityLabel = currentMapScope === "world" ? "世界总览" : (summary.city?.name || "城市地图");
        document.getElementById("city-name").textContent = cityLabel;
        const topbarCity = document.getElementById("topbar-city");
        if (topbarCity) {
            topbarCity.textContent = currentMapScope === "world"
                ? `${scopeState.world_id || "world"} · 世界总览`
                : cityLabel;
        }
        document.getElementById("count-places").textContent = summary.counts.places;
        document.getElementById("count-actors").textContent = summary.counts.actors;
        document.getElementById("count-districts").textContent = summary.counts.districts;
        document.getElementById("count-corridors").textContent = summary.counts.corridors;

        if (currentMapScope === "world") {
            await Promise.all([
                loadWorldOverview(loadGeneration),
                loadStats(loadGeneration),
            ]);
            if (loadGeneration !== mapLoadGeneration) return;
            fitMapToWorldOverview();
            refreshWorldScale();
            return;
        }

        centerMapOnSummary(summary);
        populateCategoryFilter(summary.categories);
        await populateDistrictFilter(loadGeneration);
        if (loadGeneration !== mapLoadGeneration) return;

        // Load GeoJSON layers
        await Promise.all([
            loadDistricts(loadGeneration),
            loadCorridors(loadGeneration),
            loadPlaces({}, loadGeneration),
            loadActors(loadGeneration),
            loadStats(loadGeneration),
        ]);
        if (loadGeneration !== mapLoadGeneration) return;
        fitMapToLoadedWorld();
        refreshWorldScale();

    } catch (err) {
        console.error("Failed to load world data:", err);
        alert("Failed to load world data. Is the server running?\n\n" + err.message);
    }
}

function centerMapOnSummary(summary) {
    const center = summary.city?.geometry?.data;
    if (!map || !center || center.lat === undefined || center.lng === undefined) return;
    map.setView([center.lat, center.lng], 12);
}

function fitMapToLoadedWorld() {
    if (!map || hasAutoCentered || !layerPlaces) return;
    const bounds = layerPlaces.getBounds();
    if (!bounds.isValid()) return;
    hasAutoCentered = true;
    map.fitBounds(bounds, { padding: [36, 36], maxZoom: 14 });
}

function fitMapToWorldOverview() {
    if (!map || hasAutoCentered || !worldOverviewCities.length) return;
    const bounds = L.latLngBounds(worldOverviewCities.map((city) => [city.lat, city.lng]));
    if (!bounds.isValid()) return;
    hasAutoCentered = true;
    map.fitBounds(bounds, { padding: [36, 36], maxZoom: 5, animate: false });
}

async function loadWorldOverview(loadGeneration = null) {
    if (currentMapScope !== "world") return;
    const data = await api("/api/world-overview");
    if (loadGeneration !== null && loadGeneration !== mapLoadGeneration) return;
    if (worldOverviewLayer) map.removeLayer(worldOverviewLayer);
    worldOverviewLayer = L.layerGroup().addTo(map);
    worldOverviewCities = data.cities || [];
    renderWorldOverview();
}

function refreshWorldScale() {
    if (!map) return;
    const macro = isWorldMacroView();
    setMacroWorldInteractionLock(macro);
    if (macro) clearMacroWorldResidue();
    const layers = [
        [layerPlaces, "layer-places"], [layerDistricts, "layer-districts"], [layerCorridors, "layer-corridors"],
        [layerActors, "layer-actors"], [actorClusterLayer, "layer-actors"],
    ];
    for (const [layer, checkboxId] of layers) {
        if (!layer) continue;
        if (macro && map.hasLayer(layer)) map.removeLayer(layer);
        if (!macro && document.getElementById(checkboxId)?.checked && !map.hasLayer(layer)) map.addLayer(layer);
    }
    if (worldOverviewLayer && currentMapScope === "world" && !map.hasLayer(worldOverviewLayer)) {
        worldOverviewLayer.addTo(map);
    }
    renderWorldOverview();
    if (!macro) refreshActorMarkerLayout();
}

function isWorldMacroView() {
    return currentMapScope === "world" && map && map.getZoom() <= 7;
}

function setMacroWorldInteractionLock(locked) {
    const hitPane = map?.getPane("corridor-hit");
    if (!hitPane) return;
    hitPane.style.display = locked ? "none" : "";
    hitPane.style.pointerEvents = locked ? "none" : "";
}

function clearLayerTooltips(layer) {
    if (!layer) return;
    layer.closeTooltip?.();
    layer.eachLayer?.((child) => clearLayerTooltips(child));
}

function clearTransientMapOverlays() {
    if (map) map.closePopup();
    for (const layer of [layerDistricts, layerCorridors, layerPlaces, layerActors, actorClusterLayer]) {
        clearLayerTooltips(layer);
    }
    const toast = document.getElementById("corridor-toast");
    toast?.classList.remove("visible");
    closeDetail();
}

function clearMacroWorldResidue() {
    clearTransientMapOverlays();
    // Leaflet can retain a tooltip DOM element when a just-removed nested
    // layer was hovered during a city-to-world scope switch.
    document.querySelectorAll(".leaflet-tooltip.corridor-tooltip").forEach((element) => element.remove());
}

function renderWorldOverview() {
    if (!map || !worldOverviewLayer) return;
    worldOverviewLayer.clearLayers();
    if (currentMapScope !== "world" || !worldOverviewCities.length) return;

    const zoom = map.getZoom();
    const bucketSize = zoom <= 3 ? 84 : zoom <= 7 ? 34 : 16;
    const buckets = new Map();
    for (const city of worldOverviewCities) {
        const point = map.latLngToLayerPoint([city.lat, city.lng]);
        const key = `${Math.round(point.x / bucketSize)}:${Math.round(point.y / bucketSize)}`;
        if (!buckets.has(key)) buckets.set(key, []);
        buckets.get(key).push(city);
    }

    for (const cities of buckets.values()) {
        if (cities.length === 1) {
            worldOverviewLayer.addLayer(createWorldCityMarker(cities[0]));
        } else {
            worldOverviewLayer.addLayer(createWorldCityCluster(cities));
        }
    }
}

function createWorldCityMarker(city) {
    const marker = L.circleMarker([city.lat, city.lng], {
        radius: 6,
        color: "#174a5c",
        weight: 2,
        fillColor: "#f5c96b",
        fillOpacity: 1,
    });
    marker.bindPopup(`
        <div class="world-city-popup">
          <strong>${escapeHtml(city.city_name || city.city_id)}</strong>
          <div data-ui-output>${city.places || 0} POI · ${city.characters || 0} 人物</div>
          <div class="world-city-status">${escapeHtml(city.readiness || "")}</div>
          <button type="button" class="js-open-world-city" data-city-id="${escapeHtml(city.city_id)}" data-ui-text>进入城市</button>
        </div>
    `, { closeButton: true, maxWidth: 220 });
    marker.on("popupopen", (event) => {
        const button = event.popup.getElement()?.querySelector(".js-open-world-city");
        button?.addEventListener("click", () => selectWorldCity(city.city_id));
    });
    // A city anchor can share coordinates with a city corridor. Its click is
    // an overview action and must not fall through to the map-wide corridor hit test.
    marker.on("click", (event) => {
        if (event.originalEvent) L.DomEvent.stop(event.originalEvent);
    });
    return marker;
}

function createWorldCityCluster(cities) {
    const lat = cities.reduce((sum, city) => sum + city.lat, 0) / cities.length;
    const lng = cities.reduce((sum, city) => sum + city.lng, 0) / cities.length;
    const marker = L.circleMarker([lat, lng], {
        radius: Math.min(15, 6 + Math.log2(cities.length + 1) * 3),
        color: "#174a5c",
        weight: 2,
        fillColor: "#5aa6a5",
        fillOpacity: 0.95,
    }).bindTooltip(String(cities.length), { permanent: true, direction: "center", className: "world-cluster-count" });
    marker.on("click", () => {
        const bounds = L.latLngBounds(cities.map((city) => [city.lat, city.lng]));
        if (bounds.isValid()) map.fitBounds(bounds.pad(0.35), { maxZoom: Math.min(9, map.getZoom() + 3) });
    });
    return marker;
}

function selectWorldCity(cityId) {
    const select = document.getElementById("map-scope-select");
    if (!select || !cityId) return;
    select.value = cityId;
    select.dispatchEvent(new Event("change"));
}

function populateCategoryFilter(cats) {
    const sel = document.getElementById("filter-category");
    for (const [key, label] of Object.entries(cats)) {
        const opt = document.createElement("option");
        opt.value = key;
        opt.setAttribute("data-ui-text", "");
        opt.textContent = label;
        sel.appendChild(opt);
    }
}

async function populateDistrictFilter(loadGeneration = null) {
    const sel = document.getElementById("filter-district");
    try {
        const data = await api("/api/districts");
        if (loadGeneration !== null && loadGeneration !== mapLoadGeneration) return;
        for (const f of data.features) {
            const opt = document.createElement("option");
            opt.value = f.properties.id;
            opt.textContent = f.properties.name;
            sel.appendChild(opt);
            districtLookup[f.properties.id] = f.properties;
        }
    } catch (err) { console.error("Failed to load districts:", err); }
}

// ============================================================
// Districts layer
// ============================================================
async function loadDistricts(loadGeneration = null) {
    const data = await api("/api/districts");
    if (loadGeneration !== null && loadGeneration !== mapLoadGeneration) return;
    layerDistricts = L.geoJSON(data, {
        pointToLayer: (feature, latlng) => {
            const p = feature.properties || {};

            const area = L.circle(latlng, {
                radius: p.radius_m || 900,
                fillColor: "#8b8b8b",
                color: "#555",
                weight: 1,
                opacity: 0.35,
                fillOpacity: 0.12,
                interactive: false,
            });
            area._defaultStyle = {
                fillColor: "#8b8b8b",
                color: "#555",
                weight: 1,
                opacity: 0.35,
                fillOpacity: 0.12,
            };

            const anchor = L.marker(latlng, {
                icon: L.divIcon({
                    className: "district-anchor",
                    html: '<div class="district-pin"></div>',
                    iconSize: [16, 16],
                    iconAnchor: [8, 8],
                }),
                riseOnHover: true,
                zIndexOffset: -100,
            });

            const radiusKm = ((p.radius_m || 0) / 1000).toFixed(1);
            const roleTags = (p.role_tags || []).slice(0, 5)
                .map(r => `<span class="tag">${escapeHtml(r)}</span>`).join(" ");
            const anchorList = (p.anchor_places || []).slice(0, 5).map(a =>
                `<div class="tag">${escapeHtml(a.name || "")} ⭐${a.rating ?? "?"}</div>`
            ).join("");

            anchor.bindPopup(`
                <strong>📍 ${escapeHtml(p.name || "")}</strong><br>
                <small data-ui-output>覆盖半径 ≈ ${radiusKm} km · 商户 ${p.place_count || 0}</small><br>
                ${roleTags ? `${roleTags}<br>` : ""}
                ${p.evidence_summary ? `<em>${escapeHtml(p.evidence_summary)}</em><br>` : ""}
                ${anchorList ? `<br><strong>Anchor Places:</strong><br>${anchorList}` : ""}
            `, { maxWidth: 320 });

            if (p.name) {
                anchor.bindTooltip(escapeHtml(p.name), {
                    direction: "top",
                    offset: [0, -8],
                    opacity: 0.9,
                });
            }

            anchor.on("popupopen", () => {
                highlightDistrictArea(area, true);
            });
            anchor.on("popupclose", () => {
                area._pinned = false;
                resetDistrictArea(area);
            });
            anchor.on("mouseover", () => {
                highlightDistrictArea(area, false);
            });
            anchor.on("mouseout", () => {
                if (!area._pinned) {
                    resetDistrictArea(area);
                }
            });

            return L.featureGroup([area, anchor]);
        },
    }).addTo(map);
    if (layerDistricts.bringToBack) layerDistricts.bringToBack();
    VIS_EXTENSIONS.registerLayer("districts", layerDistricts);
    updateLayerVisibility();
}

function escapeHtml(value) {
    return String(value ?? "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
}

function highlightDistrictArea(area, pin) {
    if (activeDistrictArea && activeDistrictArea !== area) {
        activeDistrictArea._pinned = false;
        resetDistrictArea(activeDistrictArea);
    }
    activeDistrictArea = area;
    if (pin) area._pinned = true;
    area.setStyle({
        fillColor: "#f9e2af",
        color: "#fab387",
        weight: 4,
        opacity: 1,
        fillOpacity: 0.28,
        dashArray: "6 4",
    });
    if (area.bringToFront) area.bringToFront();
    if (layerPlaces && map.hasLayer(layerPlaces) && layerPlaces.bringToFront) {
        layerPlaces.bringToFront();
    }
    if (layerActors && map.hasLayer(layerActors) && layerActors.bringToFront) {
        layerActors.bringToFront();
    }
}

function resetDistrictArea(area) {
    if (!area) return;
    const def = area._defaultStyle || {
        fillColor: "#8b8b8b",
        color: "#555",
        weight: 1,
        opacity: 0.35,
        fillOpacity: 0.12,
    };
    area.setStyle({ ...def, dashArray: null });
    if (activeDistrictArea === area) {
        activeDistrictArea = null;
    }
    if (layerDistricts && map.hasLayer(layerDistricts) && layerDistricts.bringToBack) {
        layerDistricts.bringToBack();
    }
    if (layerPlaces && map.hasLayer(layerPlaces) && layerPlaces.bringToFront) {
        layerPlaces.bringToFront();
    }
    if (layerActors && map.hasLayer(layerActors) && layerActors.bringToFront) {
        layerActors.bringToFront();
    }
}

// ============================================================
// Corridors layer
// ============================================================
async function loadCorridors(loadGeneration = null) {
    const data = await api("/api/corridors");
    if (loadGeneration !== null && loadGeneration !== mapLoadGeneration) return;
    corridorLayersById = {};
    corridorGeometryIndex = [];

    // Create a dedicated high-z pane so corridor hit areas always capture
    // mouse events even when place markers visually overlap.
    if (!map.getPane("corridor-hit")) {
        map.createPane("corridor-hit");
        map.getPane("corridor-hit").style.zIndex = 625;
    }
    setMacroWorldInteractionLock(isWorldMacroView());

    const outlineLayer = L.geoJSON(data, {
        style: (feature) => corridorOutlineStyle(feature.properties),
        interactive: false,
    });

    layerCorridors = L.layerGroup();
    outlineLayer.addTo(layerCorridors);

    const mainLayer = L.geoJSON(data, {
        style: (feature) => corridorStyle(feature.properties),
        onEachFeature: (feature, layer) => {
            const p = feature.properties || {};
            layer._corridorId = p.id;
            layer._defaultStyle = corridorStyle(p);
            corridorLayersById[p.id] = layer;

            setPathCursor(layer);
            layer.on("mouseover", () => highlightCorridor(layer, false));
            layer.on("mouseout", () => {
                layer.closeTooltip?.();
                if (!layer._pinned) resetCorridor(layer);
            });
            layer.on("click", (event) => {
                selectCorridor(p, layer, event);
            });
        },
    });
    mainLayer.addTo(layerCorridors);

    const hitLayer = L.geoJSON(data, {
        style: () => corridorHitStyle(),
        pane: "corridor-hit",
        interactive: true,
        onEachFeature: (feature, layer) => {
            const p = feature.properties || {};
            setPathCursor(layer);
            layer.bindTooltip(corridorTooltipHtml(p), {
                sticky: true,
                direction: "top",
                opacity: 1,
                className: "corridor-tooltip",
            });
            layer.on("mouseover", () => {
                const visibleLayer = corridorLayersById[p.id];
                if (visibleLayer) highlightCorridor(visibleLayer, false);
            });
            layer.on("mouseout", () => {
                layer.closeTooltip?.();
                const visibleLayer = corridorLayersById[p.id];
                if (visibleLayer && !visibleLayer._pinned) resetCorridor(visibleLayer);
            });
            layer.on("click", (event) => {
                const visibleLayer = corridorLayersById[p.id];
                selectCorridor(p, visibleLayer, event);
            });
        },
    });
    hitLayer.addTo(layerCorridors);

    const nodeLayer = L.layerGroup();
    for (const feature of data.features || []) {
        const p = feature.properties || {};
        indexCorridorGeometry(feature.geometry, p);
        const nodeNames = p.node_names || p.node_ids || [];
        const nodePoints = p.node_points || [];
        const color = corridorColor(p);
        // Only mark logical corridor nodes (stations / intersections), not every
        // Routes API polyline vertex — verified routes can have 100+ shape points.
        nodePoints.forEach((pt, i) => {
            if (pt?.lat == null || pt?.lng == null) return;
            const latlng = [pt.lat, pt.lng];
            const label = nodeNames[i] || "";
            const marker = L.circleMarker(latlng, {
                radius: 5,
                fillColor: color,
                color: "#1e1e2e",
                weight: 2,
                fillOpacity: 1,
                interactive: true,
            });
            const nodeTooltip = label
                ? `<div class="corridor-tooltip-body"><div class="ct-title">${escapeHtml(String(label))}</div><div class="ct-hint">${escapeHtml(String(p.name || ""))} · <span data-ui-text>点击查看通道详情 →</span></div></div>`
                : corridorTooltipHtml(p);
            marker.bindTooltip(nodeTooltip, {
                direction: "top",
                offset: [0, -6],
                opacity: 1,
                className: "corridor-tooltip",
            });
            marker.on("click", (event) => {
                const visibleLayer = corridorLayersById[p.id];
                selectCorridor(p, visibleLayer, event);
            });
            marker.addTo(nodeLayer);
        });
    }
    nodeLayer.addTo(layerCorridors);

    layerCorridors.addTo(map);
    VIS_EXTENSIONS.registerLayer("corridors", layerCorridors);
    updateLayerVisibility();
    bringLayerGroupToFront(layerCorridors);
}

function corridorStyle(p = {}) {
    const color = corridorColor(p);
    const category = corridorCategory(p);
    const isRail = category === "rail" || category === "intermodal";
    const corridorClass = p.corridor_class || "transport_corridor";
    const verified = !!p.is_route_verified;
    if (corridorClass === "service_axis") {
        return {
            color,
            weight: 3,
            opacity: 0.7,
            dashArray: "2 6",
            lineCap: "round",
            lineJoin: "round",
        };
    }
    return {
        color,
        weight: isRail ? 5 : 4,
        opacity: verified ? 0.95 : 0.7,
        dashArray: verified ? null : "8 6",
        lineCap: "round",
        lineJoin: "round",
    };
}

function corridorOutlineStyle(p = {}) {
    const category = corridorCategory(p);
    const isRail = category === "rail" || category === "intermodal";
    const corridorClass = p.corridor_class || "transport_corridor";
    if (corridorClass === "service_axis") {
        return {
            color: "#1e1e2e",
            weight: 6,
            opacity: 0.18,
            lineCap: "round",
            lineJoin: "round",
        };
    }
    return {
        color: "#1e1e2e",
        weight: isRail ? 9 : 7,
        opacity: p.is_route_verified ? 0.4 : 0.25,
        lineCap: "round",
        lineJoin: "round",
    };
}

function corridorHitStyle() {
    return {
        color: "#000",
        weight: 26,
        // SVG paths with opacity:0 can become unreliable click targets in
        // some browsers; a near-transparent stroke keeps the corridor hit
        // layer interactive without changing the visual appearance.
        opacity: 0.01,
        fillOpacity: 0,
        lineCap: "round",
        lineJoin: "round",
    };
}

function setPathCursor(layer) {
    layer.on("add", () => {
        const el = layer.getElement?.();
        if (el) el.style.cursor = "pointer";
    });
}

function corridorCategory(p = {}) {
    const type = String(p.corridor_type || "").toLowerCase();
    const mode = String(p.mode || "").toLowerCase();
    const name = String(p.name || "").toLowerCase();
    if (type.includes("intermodal") || type.includes("interchange") || type.includes("transfer")) return "intermodal";
    if (type.includes("metro") || type.includes("rail") || type.includes("trunk") || mode.includes("metro") || mode.includes("rail") || name.includes("metro")) return "rail";
    if (type.includes("pedestrian") || type.includes("commercial")) return "pedestrian";
    if (type.includes("waterfront") || type.includes("river")) return "waterfront";
    if (type.includes("airport") || type.includes("road") || type.includes("bus") || mode.includes("bus") || mode.includes("drive")) return "road";
    return "other";
}

function corridorColor(p = {}) {
    const category = typeof p === "string" ? corridorCategory({ corridor_type: p }) : corridorCategory(p);
    if (category === "rail") return "#89b4fa";
    if (category === "intermodal") return "#cba6f7";
    if (category === "pedestrian") return "#f9e2af";
    if (category === "waterfront") return "#94e2d5";
    if (category === "road") return "#fab387";
    return "#a6adc8";
}

function corridorCategoryLabel(p = {}) {
    return {
        rail: "轨道",
        intermodal: "换乘",
        pedestrian: "步行/商业",
        waterfront: "滨水",
        road: "公路/巴士",
        other: "其他",
    }[corridorCategory(p)] || "其他";
}

function corridorTooltipHtml(p = {}) {
    const name = p.name || p.id || "Corridor";
    const type = p.corridor_type || "corridor";
    const categoryLabel = corridorCategoryLabel(p);
    const color = corridorColor(p);
    const distanceKm = (p.route_distance_m || 0) / 1000;
    const durationMin = (p.route_duration_s || 0) / 60;
    const verified = p.is_route_verified
        ? '<span class="ct-tag ct-ok" data-ui-text>Routes API 已验证</span>'
        : (p.corridor_class === "service_axis"
            ? '<span class="ct-tag" data-ui-text>语义轴线</span>'
            : '<span class="ct-tag ct-warn" data-ui-text>未验证</span>');
    const servedCount = (p.served_districts || p.served_district_ids || []).length;

    let meta = "";
    if (distanceKm > 0 || durationMin > 0) {
        meta += `<span class="ct-tag">${distanceKm.toFixed(1)} km · ${durationMin.toFixed(0)} min</span>`;
    }
    if (servedCount) meta += `<span class="ct-tag" data-ui-output>连接 ${servedCount} 区域</span>`;
    if (p.place_count) meta += `<span class="ct-tag" data-ui-output>附近 ${p.place_count} 地点</span>`;

    return `
        <div class="corridor-tooltip-body">
            <div class="ct-title">${escapeHtml(String(name))}</div>
            <div class="ct-row">
                <span class="ct-tag" style="background:${color};color:#1e1e2e">${escapeHtml(categoryLabel)}</span>
                <span class="ct-tag">${escapeHtml(String(type))}</span>
                ${verified}
            </div>
            ${meta ? `<div class="ct-row">${meta}</div>` : ""}
            <div class="ct-hint" data-ui-text>点击查看完整通道详情 →</div>
        </div>
    `;
}

function highlightCorridor(layer, pin) {
    if (!layer) return;
    if (activeCorridorLayer && activeCorridorLayer !== layer) {
        activeCorridorLayer._pinned = false;
        resetCorridor(activeCorridorLayer);
    }
    activeCorridorLayer = layer;
    if (pin) layer._pinned = true;
    layer.setStyle({
        color: "#f38ba8",
        weight: 8,
        opacity: 1,
        dashArray: null,
    });
    // Lift only the visible line within its OWN pane, and only when the
    // corridor is pinned via click. Reordering the whole corridor layer group
    // (which contains the hit layer) during hover re-inserts the hit layer's
    // DOM node and kills Leaflet's mouseout tracking — that is what made the
    // hover tooltip linger and the next click stop firing.
    if (pin && layer.bringToFront) layer.bringToFront();
}

function resetCorridor(layer) {
    if (!layer) return;
    layer.setStyle(layer._defaultStyle || corridorStyle({}));
    if (activeCorridorLayer === layer) activeCorridorLayer = null;
}

function selectCorridor(properties = {}, layer, event) {
    if (event && event.originalEvent) {
        try {
            event.originalEvent.stopPropagation();
            event.originalEvent.preventDefault();
        } catch (_) { /* defensive */ }
    }
    const corridorId = properties && properties.id;
    console.log("[corridor] click", corridorId, properties && properties.name);
    if (layer) highlightCorridor(layer, true);
    showCorridorToast(properties);
    if (!corridorId) {
        showCorridorInspectorError("该通道缺少 corridor_id，无法加载详情。");
        return;
    }
    showCorridorInspectorLoading(properties);
    showCorridorDetail(corridorId, properties);
}

function indexCorridorGeometry(geometry, properties) {
    if (!geometry || !Array.isArray(geometry.coordinates)) return;
    const toLatLngs = (coords) =>
        coords
            .filter((c) => Array.isArray(c) && c.length >= 2 && c[0] != null && c[1] != null)
            .map((c) => L.latLng(c[1], c[0]));
    if (geometry.type === "LineString") {
        corridorGeometryIndex.push({ id: properties.id, properties, latlngs: toLatLngs(geometry.coordinates) });
    } else if (geometry.type === "MultiLineString") {
        for (const line of geometry.coordinates) {
            if (Array.isArray(line)) {
                corridorGeometryIndex.push({ id: properties.id, properties, latlngs: toLatLngs(line) });
            }
        }
    }
}

function findCorridorNearLatLng(latlng, thresholdPx = 14) {
    if (!map || !corridorGeometryIndex.length) return null;
    const clickPoint = map.latLngToLayerPoint(latlng);
    let best = null;
    let bestDist = Infinity;
    for (const entry of corridorGeometryIndex) {
        const pts = entry.latlngs.map((ll) => map.latLngToLayerPoint(ll));
        for (let i = 0; i < pts.length - 1; i++) {
            const dist = L.LineUtil.pointToSegmentDistance(clickPoint, pts[i], pts[i + 1]);
            if (dist < bestDist) {
                bestDist = dist;
                best = entry;
            }
        }
    }
    return best && bestDist <= thresholdPx ? best : null;
}

function handleMapClickForCorridor(event) {
    if (isWorldMacroView() || !layerCorridors || !map.hasLayer(layerCorridors)) return;
    const hit = findCorridorNearLatLng(event.latlng);
    if (!hit) return;
    const layer = corridorLayersById[hit.id];
    selectCorridor(hit.properties, layer, event);
}

function showCorridorToast(properties = {}) {
    let toast = document.getElementById("corridor-toast");
    if (!toast) {
        toast = document.createElement("div");
        toast.id = "corridor-toast";
        toast.className = "corridor-toast";
        document.body.appendChild(toast);
    }
    const name = properties.name || properties.id || "Corridor";
    const type = properties.corridor_type || "corridor";
    toast.innerHTML = `<strong data-ui-text>已选中通道</strong><br>${escapeHtml(String(name))}<br><span class="muted">${escapeHtml(String(type))}</span>`;
    toast.classList.add("visible");
    clearTimeout(showCorridorToast._timer);
    showCorridorToast._timer = setTimeout(() => {
        toast.classList.remove("visible");
    }, 6000);
}

function bringLayerGroupToFront(layer) {
    if (!layer) return;
    if (layer.bringToFront) {
        layer.bringToFront();
        return;
    }
    if (layer.eachLayer) {
        layer.eachLayer((child) => bringLayerGroupToFront(child));
    }
}

function bringMapLayersToReadableOrder() {
    if (layerDistricts && map.hasLayer(layerDistricts) && layerDistricts.bringToBack) {
        layerDistricts.bringToBack();
    }
    // Corridors hitLayer is in its own high-z pane, so it always captures clicks.
    // Visual layers follow standard order: districts < corridors < places < actors.
    if (layerCorridors && map.hasLayer(layerCorridors)) {
        bringLayerGroupToFront(layerCorridors);
    }
    if (layerPlaces && map.hasLayer(layerPlaces) && layerPlaces.bringToFront) {
        layerPlaces.bringToFront();
    }
    if (layerActors && map.hasLayer(layerActors) && layerActors.bringToFront) {
        layerActors.bringToFront();
    }
}

// ============================================================
// Places layer
// ============================================================
let placeMarkerCluster = null;

async function loadPlaces(filters = {}, loadGeneration = null) {
    let url = "/api/places?";
    if (filters.q) url += `q=${encodeURIComponent(filters.q)}&`;
    if (filters.category) url += `category=${filters.category}&`;
    if (filters.min_rating > 0) url += `min_rating=${filters.min_rating}&`;
    if (filters.district_id) url += `district_id=${filters.district_id}&`;

    const data = await api(url);
    if (loadGeneration !== null && loadGeneration !== mapLoadGeneration) return;
    allPlacesGeoJSON = data;

    if (layerPlaces) { map.removeLayer(layerPlaces); }
    placeMarkers = [];

    layerPlaces = L.geoJSON(data, {
        pointToLayer: (feature, latlng) => {
            const marker = L.circleMarker(latlng, {
                radius: placeMarkerRadiusByZoom(),
                fillColor: "#3388ff",
                color: "#fff",
                weight: 1,
                opacity: 0.95,
                fillOpacity: 0.82,
            });
            marker._baseLatLng = latlng;
            marker._placeId = feature.properties?.id || "";
            placeMarkers.push(marker);
            return marker;
        },
        onEachFeature: (feature, layer) => {
            const p = feature.properties;
            const safeId = String(p.id || "").replace(/"/g, "&quot;");
            const safeName = String(p.name || "").replace(/</g, "&lt;");
            const safeAddr = String(p.address || "").replace(/</g, "&lt;");
            layer.bindPopup(`
                <strong>${safeName}</strong>
                <span class="tag" data-ui-text>${escapeHtml(p.category_label || "")}</span>
                ${p.rating ? `<span class="tag">⭐${p.rating}</span>` : ""}
                ${p.user_rating_count ? `<span class="tag">${p.user_rating_count} ratings</span>` : ""}<br>
                <small>${safeAddr}</small>
                <br><button type="button" class="js-place-detail" data-place-id="${safeId}" style="margin-top:4px;cursor:pointer;font-size:11px;" data-ui-text>查看详情 →</button>
            `);
            layer.on("popupopen", (e) => {
                const root = e.popup.getElement();
                if (!root) return;
                const btn = root.querySelector(".js-place-detail");
                if (!btn) return;
                btn.addEventListener("click", (ev) => {
                    ev.preventDefault();
                    ev.stopPropagation();
                    const pid = btn.getAttribute("data-place-id");
                    if (pid) showPlaceDetail(pid);
                });
            });
        },
    }).addTo(map);

    layerPlaces.bringToFront();
    VIS_EXTENSIONS.registerLayer("places", layerPlaces);
    updateLayerVisibility();
    refreshPlaceMarkerLayout();
}

function placeMarkerRadiusByZoom() {
    const zoom = map?.getZoom?.() || 13;
    if (zoom <= 11) return 2.5;
    if (zoom <= 13) return 3.5;
    if (zoom <= 15) return 4.5;
    if (zoom <= 17) return 5.5;
    return 6.5;
}

function placeSeparationByZoom() {
    const zoom = map?.getZoom?.() || 13;
    if (zoom <= 13) return 0;
    if (zoom <= 15) return 10;
    if (zoom <= 17) return 14;
    return 18;
}

function refreshPlaceMarkerLayout() {
    if (!map || !layerPlaces || !placeMarkers.length) return;

    const radius = placeMarkerRadiusByZoom();
    const separation = placeSeparationByZoom();
    const buckets = new Map();
    const gridSize = Math.max(10, radius * 2.8);

    for (const marker of placeMarkers) {
        if (!marker._baseLatLng) continue;
        if (marker.setRadius) marker.setRadius(radius);
        marker.setStyle({ weight: map.getZoom() >= 16 ? 1.5 : 1 });

        const point = map.latLngToLayerPoint(marker._baseLatLng);
        const key = `${Math.round(point.x / gridSize)}:${Math.round(point.y / gridSize)}`;
        if (!buckets.has(key)) buckets.set(key, []);
        buckets.get(key).push({ marker, point });
    }

    for (const bucket of buckets.values()) {
        bucket.sort((a, b) => String(a.marker._placeId).localeCompare(String(b.marker._placeId)));
        if (bucket.length === 1 || separation === 0) {
            for (const item of bucket) {
                item.marker.setLatLng(item.marker._baseLatLng);
            }
            continue;
        }

        const count = bucket.length;
        const ringRadius = Math.min(34, separation + Math.min(16, count * 1.8));
        for (let i = 0; i < count; i++) {
            const angle = (Math.PI * 2 * i) / count;
            const offset = L.point(
                Math.cos(angle) * ringRadius,
                Math.sin(angle) * ringRadius
            );
            const displayPoint = bucket[i].point.add(offset);
            bucket[i].marker.setLatLng(map.layerPointToLatLng(displayPoint));
        }
    }

    if (layerPlaces.bringToFront) layerPlaces.bringToFront();
    if (layerActors && map.hasLayer(layerActors) && layerActors.bringToFront) {
        layerActors.bringToFront();
    }
}

// ============================================================
// Actors layer
// ============================================================
async function loadActors(loadGeneration = null) {
    const data = await api("/api/actors");
    if (loadGeneration !== null && loadGeneration !== mapLoadGeneration) return;
    if (layerActors) { map.removeLayer(layerActors); }
    if (actorClusterLayer) { map.removeLayer(actorClusterLayer); }
    actorMarkers = [];
    actorClusterLayer = L.layerGroup().addTo(map);
    layerActors = L.geoJSON(data, {
        pointToLayer: (feature, latlng) => {
            const icon = L.divIcon({
                className: "actor-icon",
                html: '<div style="background:#e74c3c;color:#fff;width:20px;height:20px;border-radius:50%;text-align:center;line-height:20px;font-size:12px;font-weight:bold;">👤</div>',
                iconSize: [20, 20],
                iconAnchor: [10, 10],
            });
            const marker = L.marker(latlng, { icon });
            marker._baseLatLng = latlng;
            marker._actorId = feature.properties?.id || "";
            actorMarkers.push(marker);
            return marker;
        },
        onEachFeature: (feature, layer) => {
            const p = feature.properties;
            const prefs = p.preferences || {};
            layer.bindPopup(`
                <strong>👤 ${p.name}</strong><br>
                职业: ${p.occupation || p.role || "—"}<br>
                关系: ${p.relationship || (p.relationship_count ? `${p.relationship_count} 条关系` : "—")}<br>
                位置: ${p.current_district || "—"}<br>
                ❤️ 偏好: ${(prefs.prefer || []).slice(0,3).join(", ")}<br>
                ✋ 避讳: ${(prefs.avoid || []).slice(0,3).join(", ")}<br>
                🚇 最大步行: ${p.mobility?.max_walk_m || "—"} m
            `);
        },
    }).addTo(map);
    VIS_EXTENSIONS.registerLayer("actors", layerActors);
    updateLayerVisibility();
    refreshActorMarkerLayout();
}

function refreshActorMarkerLayout() {
    if (!map || !layerActors || !actorMarkers.length) return;

    const icon = actorIconByZoom();
    const zoom = map.getZoom();
    if (actorClusterLayer) actorClusterLayer.clearLayers();
    for (const marker of actorMarkers) {
        marker.setIcon(icon);
        marker.setLatLng(marker._baseLatLng);
        marker.setOpacity(0);
    }
    // At regional/world scale people are represented by their city, never as
    // a radial ring.  This prevents screen-space overlap from inventing a
    // false geographic pattern.
    if (zoom <= 7) return;
    const buckets = new Map();
    for (const marker of actorMarkers) {
        if (!marker._baseLatLng) continue;
        const point = map.latLngToLayerPoint(marker._baseLatLng);
        const bucketPixels = zoom < 13 ? 20 : 10;
        const key = `${Math.round(point.x / bucketPixels)}:${Math.round(point.y / bucketPixels)}`;
        if (!buckets.has(key)) buckets.set(key, []);
        buckets.get(key).push({ marker, point });
    }

    for (const bucket of buckets.values()) {
        if (bucket.length === 1) {
            bucket[0].marker.setOpacity(1);
            continue;
        }
        const cluster = L.circleMarker(bucket[0].marker._baseLatLng, {
            radius: Math.min(12, 5 + Math.log2(bucket.length + 1) * 2),
            color: "#9b2226", weight: 2, fillColor: "#e74c3c", fillOpacity: 0.9,
        }).bindTooltip(`${bucket.length} 位人物`, { direction: "top" });
        actorClusterLayer?.addLayer(cluster);
    }

    if (layerActors && map.hasLayer(layerActors) && layerActors.bringToFront) {
        layerActors.bringToFront();
    }
    if (actorClusterLayer && map.hasLayer(actorClusterLayer)) actorClusterLayer.bringToFront?.();
}

function actorIconByZoom() {
    const zoom = map?.getZoom?.() || 13;
    const size = zoom <= 5 ? 5 : zoom <= 8 ? 7 : zoom <= 11 ? 10 : zoom <= 14 ? 14 : 20;
    const glyph = size >= 14 ? "&#128100;" : "";
    return L.divIcon({
        className: "actor-icon",
        html: `<div style="background:#e74c3c;color:#fff;width:${size}px;height:${size}px;border-radius:50%;text-align:center;line-height:${size}px;font-size:${Math.max(7, Math.round(size * 0.58))}px;font-weight:bold;">${glyph}</div>`,
        iconSize: [size, size], iconAnchor: [size / 2, size / 2],
    });
}

// ============================================================
// Stats
// ============================================================
async function loadStats(loadGeneration = null) {
    try {
        const data = await api("/api/stats");
        if (loadGeneration !== null && loadGeneration !== mapLoadGeneration) return;
        const cats = data.category_distribution || {};
        const panel = document.getElementById("stats-panel");
        let html = `<b data-ui-text>商户总数:</b> ${data.total_places}<br>`;
        html += `<b data-ui-text>平均评分:</b> ${data.avg_rating}<br><br>`;
        html += `<b data-ui-text>类别分布:</b><br>`;
        for (const [cat, count] of Object.entries(cats)) {
            html += `  ${cat}: ${count}<br>`;
        }
        html += `<br><b data-ui-text>评分分布:</b><br>`;
        for (const [bin, count] of Object.entries(data.rating_distribution || {})) {
            html += `  ${bin}: ${count}<br>`;
        }
        panel.innerHTML = html;
    } catch (err) {
        document.getElementById("stats-panel").innerHTML =
            '<span class="loading">Statistics unavailable</span>';
    }
}

// ============================================================
// Corridor detail (right panel)
// ============================================================
async function showCorridorDetail(corridorId, fallbackProperties = {}) {
    const panel = document.getElementById("detail-panel");
    const content = document.getElementById("detail-content");
    const title = document.getElementById("detail-title");

    if (!panel || !content || !title) return;
    if (!corridorId || corridorId === "undefined" || corridorId === "null") {
        showCorridorInspectorError("无效的 corridor_id，无法加载详情。");
        title.textContent = "Corridor 详情";
        content.innerHTML = '<div class="empty" data-ui-text>无效的 corridor_id，无法加载详情。</div>';
        showDetailPanel(panel);
        return;
    }
    title.textContent = corridorId;
    content.innerHTML = '<div class="loading" data-ui-text>加载中…</div>';
    showDetailPanel(panel);
    showCorridorInspectorLoading(fallbackProperties);

    let data;
    try {
        data = await api(`/api/corridors/${encodeURIComponent(corridorId)}/detail`);
    } catch (err) {
        console.error("Failed to load corridor detail:", err);
        content.innerHTML = `<div class="empty"><span data-ui-text>加载失败：</span> ${err.message || err}</div>`;
        showCorridorInspectorError(`加载失败：${err.message || err}`);
        return;
    }

    const layer = corridorLayersById[corridorId];
    if (layer) highlightCorridor(layer, true);

    title.textContent = data.name || corridorId;
    const rendered = renderCorridorDetail(data, corridorId);
    content.innerHTML = rendered.fullHtml;
    updateCorridorInspector(rendered.sidebarHtml);
    bindCorridorDetailLinks(content);
}

function renderCorridorDetail(data, corridorId = "") {
    const served = data.served_districts || [];
    const nodes = data.node_names?.length ? data.node_names : data.node_ids || [];
    const risks = data.risk_hints || [];
    const bands = data.service_bands || [];
    const route = data.route_polyline || {};
    const runtime = data.route_runtime_ref || {};
    const infos = data.information_items || [];
    const nearbyPlaces = data.nearby_places || [];

    const corridorClass = data.corridor_class || "transport_corridor";
    const verified = !!data.is_route_verified;
    const verifiedSegments = data.verified_segment_count || 0;
    const totalSegments = data.segment_count || 0;
    const distanceKm = (data.route_distance_m || 0) / 1000;
    const durationMin = (data.route_duration_s || 0) / 60;
    const evidence = data.served_district_evidence || [];
    const impact = data.impact_model || {};
    const classBadge = corridorClass === "service_axis"
        ? '<span class="tag" style="background:#3a3a4d;color:#e0c98b">service_axis</span>'
        : '<span class="tag" style="background:#2c4a3a;color:#a6e3a1">transport_corridor</span>';
    const verifiedBadge = verified
        ? '<span class="tag" style="background:#2c4a3a;color:#a6e3a1" data-ui-text>Routes API 已验证</span>'
        : (corridorClass === "service_axis"
            ? '<span class="tag" style="background:#3a3a4d;color:#cdd6f4" data-ui-text>语义轴线</span>'
            : '<span class="tag" style="background:#4a3a3a;color:#f38ba8" data-ui-text>未验证（直连节点）</span>');

    const category = corridorCategory(data);
    const categoryLabel = {
        rail: "轨道",
        intermodal: "换乘",
        pedestrian: "步行/商业",
        waterfront: "滨水",
        road: "公路/巴士",
        other: "其他",
    }[category] || "其他";

    let html = "";
    html += `<div class="row"><span class="label">ID</span> <span class="val">${escapeHtml(data.corridor_id || corridorId)}</span></div>`;
    html += `<div class="row"><span class="label" data-ui-text>类型</span> <span class="tag corridor-type">${escapeHtml(data.corridor_type || "corridor")}</span> ${classBadge} ${verifiedBadge}</div>`;
    html += `<div class="row"><span class="label" data-ui-text>图例分类</span> <span class="tag" style="background:${corridorColor(data)};color:#1e1e2e">${escapeHtml(categoryLabel)}</span></div>`;
    html += `<div class="row"><span class="label" data-ui-text>出行模式</span> <span class="tag">${escapeHtml(data.mode || "mixed")}</span></div>`;
    html += `<div class="row"><span class="label" data-ui-text>语义角色</span> <span class="val">${escapeHtml(data.semantic_role || "-")}</span></div>`;
    if (totalSegments) {
        html += `<div class="row"><span class="label" data-ui-text>路段验证</span> <span class="val" data-ui-output>${verifiedSegments} / ${totalSegments} 段使用 Google Routes API 真实折线</span></div>`;
    }
    if (data.route_provider) {
        html += `<div class="row"><span class="label" data-ui-text>路线来源</span> <span class="val">${escapeHtml(data.route_provider)}</span></div>`;
    }
    if (distanceKm > 0 || durationMin > 0) {
        html += `<div class="row"><span class="label" data-ui-text>距离 / 时长</span> <span class="val">${distanceKm.toFixed(1)} km · ${durationMin.toFixed(0)} min</span></div>`;
    }
    html += `<div class="row"><span class="label" data-ui-text>附近地点</span> <span class="val" data-ui-output>${data.nearby_place_count || 0} 个 Place 关联到该 corridor</span></div>`;
    if (route.note || runtime.note) {
        html += `<div class="info-card">${escapeHtml(route.note || runtime.note)}</div>`;
    }

    if (Object.keys(impact).length) {
        html += `<div class="section-title" data-ui-text>任务影响模型</div>`;
        html += `<div class="info-card">`;
        if (impact.peak_sensitivity) html += `<span class="tag"><span data-ui-text>高峰敏感:</span> ${escapeHtml(impact.peak_sensitivity)}</span>`;
        if (impact.transfer_complexity) html += `<span class="tag"><span data-ui-text>换乘复杂度:</span> ${escapeHtml(impact.transfer_complexity)}</span>`;
        if (Array.isArray(impact.disruption_scope) && impact.disruption_scope.length) {
            html += `<br><span class="label" data-ui-text>中断影响</span> ${impact.disruption_scope.map(escapeHtml).join(", ")}`;
        }
        if (impact.task_relevance) html += `<br><span class="label" data-ui-text>Bench 用途</span> ${escapeHtml(impact.task_relevance)}`;
        html += `</div>`;
    }

    html += `<div class="section-title" data-ui-output>连接 / 服务区域 (${served.length})</div>`;
    if (served.length) {
        const evByDistrict = {};
        for (const ev of evidence) {
            if (ev.district_id) evByDistrict[ev.district_id] = ev;
        }
        for (const d of served) {
            const ev = evByDistrict[d.id] || {};
            const verifiedTag = ev.verified
                ? '<span class="tag" style="background:#2c4a3a;color:#a6e3a1">geometry✓</span>'
                : '<span class="tag" style="background:#4a3a3a;color:#f38ba8" data-ui-text>仅LLM声明</span>';
            const distanceTag = ev.min_distance_m != null ? `<span class="tag">${ev.min_distance_m}m</span>` : "";
            const claim = ev.match_method === "geometry_only"
                ? '<span class="tag" data-ui-text>几何反推</span>'
                : "";
            html += `<div class="row"><span class="tag">${escapeHtml(d.name || d.id)}</span> ${verifiedTag} ${distanceTag} ${claim}</div>`;
        }
    } else {
        html += `<div class="empty" data-ui-text>没有声明 served_district_ids。</div>`;
    }

    html += `<div class="section-title" data-ui-output>节点 (${nodes.length})</div>`;
    html += nodes.length
        ? `<div class="corridor-node-list">${nodes.map(escapeHtml).join(" <span class=\"muted\">→</span> ")}</div>`
        : `<div class="empty" data-ui-text>没有节点信息。</div>`;

    html += `<div class="section-title" data-ui-output>动态风险 (${risks.length})</div>`;
    if (risks.length) {
        for (const r of risks) {
            html += `<div class="info-card">`;
            html += `<span class="tag">${escapeHtml(r.time_window || "time_window")}</span>`;
            html += `<span class="tag">${escapeHtml(r.risk || "risk")}</span><br>`;
            if (r.segment_hint) html += `<span class="label" data-ui-text>区段</span> ${escapeHtml(r.segment_hint)}<br>`;
            html += `${escapeHtml(r.task_consequence || "")}`;
            html += `</div>`;
        }
    } else {
        html += `<div class="empty" data-ui-text>没有 corridor 级 risk_hints。</div>`;
    }

    html += `<div class="section-title" data-ui-output>沿线服务带 (${bands.length})</div>`;
    if (bands.length) {
        for (const band of bands) {
            html += `<div class="info-card">`;
            html += `<span class="tag">${escapeHtml(band.band_role || "service_band")}</span>`;
            if (band.density_expectation) html += `<span class="tag"><span data-ui-text>密度:</span> ${escapeHtml(band.density_expectation)}</span>`;
            if (Array.isArray(band.place_types)) html += `<br><span class="label" data-ui-text>类型</span> ${band.place_types.map(escapeHtml).join(", ")}`;
            if (Array.isArray(band.text_queries) && band.text_queries.length) {
                html += `<br><span class="label" data-ui-text>搜索词</span> ${band.text_queries.slice(0, 3).map(escapeHtml).join(" / ")}`;
            }
            if (band.task_value) html += `<br>${escapeHtml(band.task_value)}`;
            html += `</div>`;
        }
    } else {
        html += `<div class="empty" data-ui-text>没有 service_bands。</div>`;
    }

    html += `<div class="section-title" data-ui-output>附近地点样例 (${nearbyPlaces.length}/${data.nearby_place_count || 0})</div>`;
    if (nearbyPlaces.length) {
        for (const place of nearbyPlaces.slice(0, 8)) {
            const safePlaceId = escapeHtml(place.place_id || "");
            html += `<div class="info-card">`;
            html += `<button type="button" class="link-button js-place-detail" data-place-id="${safePlaceId}">${escapeHtml(place.name || safePlaceId)}</button>`;
            html += `<br><span class="tag">${escapeHtml(place.primary_type || "place")}</span>`;
            if (place.district_name) html += `<span class="tag">${escapeHtml(place.district_name)}</span>`;
            if (place.rating) html += `<span class="tag">⭐${place.rating}</span>`;
            html += `</div>`;
        }
    } else {
        html += `<div class="empty" data-ui-text>没有 Place 通过 near_corridor_ids 关联到该通道。</div>`;
    }

    html += `<div class="section-title" data-ui-output>通道信息项 (${infos.length})</div>`;
    if (infos.length) {
        for (const info of infos.slice(0, 4)) {
            html += `<div class="info-card"><span class="tag">${escapeHtml(info.content_type || "info")}</span><br>${escapeHtml(info.content || "")}</div>`;
        }
    } else {
        html += `<div class="empty" data-ui-text>没有 about.corridor_id 的信息项。</div>`;
    }

    const sidebarHtml = renderCorridorSidebarSummary({
        data,
        corridorId,
        categoryLabel,
        served,
        nodes,
        risks,
        bands,
        nearbyPlaces,
        distanceKm,
        durationMin,
        verifiedSegments,
        totalSegments,
        verified,
    });

    return { fullHtml: html, sidebarHtml };
}

function renderCorridorSidebarSummary(ctx) {
    const { data, corridorId, categoryLabel, served, nodes, risks, bands, nearbyPlaces, distanceKm, durationMin, verifiedSegments, totalSegments, verified } = ctx;
    let html = "";
    html += `<div class="title">${escapeHtml(data.name || corridorId)}</div>`;
    html += `<div class="row"><span class="label">ID</span><br><span class="muted">${escapeHtml(data.corridor_id || corridorId)}</span></div>`;
    html += `<div class="row"><span class="tag" style="background:${corridorColor(data)};color:#1e1e2e">${escapeHtml(categoryLabel)}</span><span class="tag">${escapeHtml(data.corridor_type || "corridor")}</span><span class="tag">${escapeHtml(data.mode || "mixed")}</span></div>`;
    html += `<div class="row"><span class="label" data-ui-text>验证</span> ${verified ? "Routes API 已验证" : "未验证 / 语义通道"}${totalSegments ? ` · ${verifiedSegments}/${totalSegments} 段` : ""}</div>`;
    if (distanceKm > 0 || durationMin > 0) {
        html += `<div class="row"><span class="label" data-ui-text>距离 / 时长</span> ${distanceKm.toFixed(1)} km · ${durationMin.toFixed(0)} min</div>`;
    }
    html += `<div class="row"><span class="label" data-ui-text>连接区域</span> ${served.length ? served.slice(0, 6).map(d => `<span class="tag">${escapeHtml(d.name || d.id)}</span>`).join("") : "<span class=\"muted\" data-ui-text>无</span>"}</div>`;
    html += `<div class="section-title" data-ui-text>节点</div>`;
    html += nodes.length ? `<div>${nodes.slice(0, 8).map(escapeHtml).join(" <span class=\"muted\">→</span> ")}</div>` : `<div class="muted" data-ui-text>没有节点信息</div>`;
    html += `<div class="section-title" data-ui-text>数据概览</div>`;
    html += `<div class="row" data-ui-output>风险 ${risks.length} · 服务带 ${bands.length} · 附近地点 ${nearbyPlaces.length}/${data.nearby_place_count || 0}</div>`;
    html += `<button type="button" class="link-button js-open-floating-corridor" data-ui-text>打开右侧完整详情</button>`;
    return html;
}

function bindCorridorDetailLinks(root) {
    root.querySelectorAll(".js-place-detail").forEach((btn) => {
        btn.addEventListener("click", (ev) => {
            ev.preventDefault();
            ev.stopPropagation();
            const pid = btn.getAttribute("data-place-id");
            if (pid) showPlaceDetail(pid);
        });
    });
}

function updateCorridorInspector(html) {
    const inspector = document.getElementById("corridor-inspector");
    if (!inspector) return;
    inspector.classList.remove("empty");
    inspector.innerHTML = html;
    const openBtn = inspector.querySelector(".js-open-floating-corridor");
    if (openBtn) {
        openBtn.addEventListener("click", (ev) => {
            ev.preventDefault();
            ev.stopPropagation();
            showDetailPanel(document.getElementById("detail-panel"));
        });
    }
}

function showCorridorInspectorLoading(properties = {}) {
    const name = properties.name || properties.id || "Corridor";
    updateCorridorInspector(`<div class="title">${escapeHtml(name)}</div><div class="loading" data-ui-text>正在加载通道详情…</div>`);
}

function showCorridorInspectorError(message) {
    const inspector = document.getElementById("corridor-inspector");
    if (!inspector) return;
    inspector.classList.remove("empty");
    inspector.innerHTML = `<div class="title" data-ui-text>通道详情</div><div class="empty">${escapeHtml(message)}</div>`;
}

window.showCorridorDetail = showCorridorDetail;

// ============================================================
// Place detail (right panel)
// ============================================================
async function showPlaceDetail(placeId) {
    const panel = document.getElementById("detail-panel");
    const content = document.getElementById("detail-content");
    const title = document.getElementById("detail-title");

    if (!panel || !content || !title) {
        console.error("Detail panel DOM nodes missing");
        return;
    }

    title.textContent = placeId;
    content.innerHTML = '<div class="loading" data-ui-text>加载中…</div>';
    showDetailPanel(panel);

    if (!placeId || placeId === "undefined" || placeId === "null") {
        content.innerHTML = '<div class="empty" data-ui-text>无效的 place_id，无法加载详情。</div>';
        return;
    }

    let data;
    try {
        data = await api(`/api/places/${encodeURIComponent(placeId)}/detail`);
    } catch (err) {
        console.error("Failed to load place detail:", err);
        content.innerHTML = `<div class="empty"><span data-ui-text>加载失败：</span> ${err.message || err}</div>`;
        return;
    }

    try {
        title.textContent = data.name || placeId;

        const mp = data.map_projection || {};
        const googleDetails = mp.google_place_details || {};
        const googleReviews = Array.isArray(googleDetails.reviews) ? googleDetails.reviews : [];

        let html = '<div class="section-title" data-ui-text>地点基础事实</div>';
        if (data.primary_type || data.category) {
            const primaryType = data.primary_type || "not_provided";
            html += `<div class="row"><span class="label" data-ui-text>主要类型</span> <span class="val"><code>${escapeHtml(primaryType)}</code></span></div>`;
        }
        if (Array.isArray(mp.types) && mp.types.length) {
            const remainingTypes = mp.types.filter((type) => type !== data.primary_type);
            if (remainingTypes.length) {
                html += `<div class="row"><span class="label" data-ui-text>其他 Google types</span> <span class="val">${remainingTypes.map((type) => `<code>${escapeHtml(type)}</code>`).join(" · ")}</span></div>`;
            }
        }
        if (data.address) {
            html += `<div class="row"><span class="label" data-ui-text>地址</span> <span class="val">${escapeHtml(data.address)}</span></div>`;
        }
        html += `<div class="row"><span class="label" data-ui-text>坐标</span> <span class="val">${formatCoordinates(data.location)}</span></div>`;
        if (data.district_name) {
            html += `<div class="row"><span class="label" data-ui-text>所属区域</span> <span class="val">${escapeHtml(data.district_name)}</span></div>`;
        }
        if (Array.isArray(data.near_corridors) && data.near_corridors.length) {
            html += `<div class="row"><span class="label" data-ui-text>附近通道</span> `;
            html += data.near_corridors.map(c => (
                `<button type="button" class="link-button js-corridor-detail" data-corridor-id="${escapeHtml(c.id)}">${escapeHtml(c.name || c.id)}</button>`
            )).join(" ");
            html += `</div>`;
        }
        html += `<div class="row"><span class="label" data-ui-text>Google 评分</span> <span class="val" data-ui-output>${formatRating(mp.rating, mp.user_rating_count)}</span></div>`;
        html += `<div class="row"><span class="label" data-ui-text>营业状态</span> <span class="val" data-ui-output>${formatRawGoogleValue(mp.business_status)}</span></div>`;
        html += `<div class="row"><span class="label" data-ui-text>已抓取评论</span> <span class="val" data-ui-output>${googleReviews.length} 条 Google Review 正文</span></div>`;

        const effectiveHours = data.opening_hours;
        html += `<div class="row"><span class="label" data-ui-text>常规营业时间</span> <span class="val" data-ui-output>${formatRegularOpeningHours(effectiveHours, effectiveHours && Object.keys(effectiveHours).length ? "provided" : "not_provided")}</span></div>`;
        html += `<div class="section-title" data-ui-text>Google 地点详情</div>`;
        html += `<div class="row"><span class="label" data-ui-text>详情获取状态</span> <span class="val" data-ui-output>${formatDetailsStatus(googleDetails.details_status)}</span></div>`;
        if (googleDetails.details_status === "queried") {
            html += `<div class="row"><span class="label" data-ui-text>网站</span> ${formatGoogleWebsite(googleDetails.website_uri, googleDetails.field_status?.website_uri)}</div>`;
            html += `<div class="row"><span class="label" data-ui-text>电话</span> <span class="val">${formatGoogleField(googleDetails.national_phone_number, googleDetails.field_status?.national_phone_number)}</span></div>`;
            if (hasGooglePrice(googleDetails)) html += `<div class="row"><span class="label" data-ui-text>Google 价格</span> <span class="val" data-ui-output>${formatGooglePrice(googleDetails.price_level, googleDetails.price_range)}</span></div>`;
            for (const [key, label] of [["reservable", "可预约"], ["dine_in", "堂食"], ["takeout", "外带"], ["delivery", "配送"], ["curbside_pickup", "路边取餐"]]) {
                if (googleDetails.field_status?.[key] === "provided") {
                    html += `<div class="row"><span class="label" data-ui-text>${label}</span> <span class="val" data-ui-text>${googleDetails[key] ? "是" : "否"}</span></div>`;
                }
            }
        }

        const infos = data.information_items || [];
        const reviewInfos = infos.filter((i) => i.content_type === "review");
        const displayReviews = googleReviews.length ? googleReviews : reviewInfos;
        html += `<div class="section-title" data-ui-output>💬 Google 评论正文 (${displayReviews.length}条)</div>`;
        if (displayReviews.length) {
            for (const review of displayReviews.slice(0, 4)) {
                html += renderReviewCard(review);
            }
        } else {
            html += `<div class="empty" data-ui-text>Google 未返回可展示的评论正文。</div>`;
        }

        if (!html) {
            html = '<div class="empty" data-ui-text>这个地点没有更多详情信息。</div>';
        }
        content.innerHTML = html;
        content.querySelectorAll(".js-corridor-detail").forEach((btn) => {
            btn.addEventListener("click", (ev) => {
                ev.preventDefault();
                const cid = btn.getAttribute("data-corridor-id");
                if (cid) showCorridorDetail(cid);
            });
        });
    } catch (err) {
        console.error("Failed to render place detail:", err);
        content.innerHTML = `<div class="empty" data-ui-output>渲染失败：${err.message || err}</div>`;
    }
}

window.showPlaceDetail = showPlaceDetail;

function renderReviewCard(review) {
    const rating = review.rating ? `<span class="tag">⭐${review.rating}</span>` : "";
    const author = review.author_display_name ? `<span class="tag">${escapeHtml(review.author_display_name)}</span>` : "";
    const time = review.relative_publish_time || review.publish_time;
    const timeTag = time ? `<span class="tag">${escapeHtml(time)}</span>` : "";
    const content = review.text || review.original_text || review.content || "";
    return `<div class="info-card"><span class="tag">google_review</span>${rating}${author}${timeTag}<br>${escapeHtml(content)}</div>`;
}

function formatCoordinates(location) {
    const lat = Number(location?.lat);
    const lng = Number(location?.lng);
    return Number.isFinite(lat) && Number.isFinite(lng)
        ? `${lat.toFixed(6)}, ${lng.toFixed(6)}`
        : "未提供";
}

function formatRating(rating, count) {
    if (rating === undefined || rating === null) {
        return count === undefined || count === null ? "Google 未返回" : `未返回评分 · ${escapeHtml(count)} 条评分`;
    }
    const countText = count === undefined || count === null ? "Google 未返回评分数" : `${escapeHtml(count)} 条评分`;
    return `⭐ ${escapeHtml(rating)} · ${countText}`;
}

function formatRawGoogleValue(value) {
    return value ? `<code>${escapeHtml(value)}</code>` : "Google 未返回";
}

function formatDetailsStatus(status) {
    return status ? `<code>${escapeHtml(status)}</code>` : "历史产物未记录 Google Details 状态";
}

function formatGoogleField(value, fieldStatus) {
    if (value !== undefined && value !== null && value !== "") return escapeHtml(value);
    return fieldStatus === "not_provided" ? "Google 未返回" : "未提供";
}

function formatGoogleWebsite(value, fieldStatus) {
    if (value) {
        const safeValue = escapeHtml(value);
        return `<a class="link-button" href="${safeValue}" target="_blank" rel="noopener noreferrer">${safeValue}</a>`;
    }
    return `<span class="val" data-ui-output>${fieldStatus === "not_provided" ? "Google 未返回" : "未提供"}</span>`;
}

function formatRegularOpeningHours(hours, fieldStatus) {
    if (!hours || !Object.keys(hours).length) {
        return fieldStatus === "not_provided" ? "Google 未返回" : "未提供";
    }
    const descriptions = Array.isArray(hours.weekdayDescriptions) ? hours.weekdayDescriptions : [];
    const days = descriptions.map((description) => {
        const separator = String(description).indexOf(":");
        return separator > 0
            ? {day: String(description).slice(0, separator).trim(), hours: String(description).slice(separator + 1).trim()}
            : null;
    });
    if (days.length !== 7 || days.some((entry) => !entry?.day || !entry.hours)) {
        return escapeHtml(descriptions.join("；") || "Google 未返回可展示的营业时间");
    }

    const groups = [];
    for (const day of days) {
        const last = groups[groups.length - 1];
        if (last && last.hours === day.hours) {
            last.endDay = day.day;
        } else {
            groups.push({startDay: day.day, endDay: day.day, hours: day.hours});
        }
    }
    return groups.map((group) => {
        const dayLabel = group.startDay === group.endDay ? group.startDay : `${group.startDay}\u2013${group.endDay}`;
        return `${escapeHtml(dayLabel)}: ${escapeHtml(group.hours)}`;
    }).join("；");
}

function hasGooglePrice(details) {
    const range = details.price_range || {};
    return Boolean(details.price_level || range.startPrice || range.endPrice);
}

function formatGooglePrice(level, range) {
    const normalizedLevel = String(level || "").replace(/^PRICE_LEVEL_/, "");
    const start = formatGoogleMoney(range?.startPrice);
    const end = formatGoogleMoney(range?.endPrice);
    const rangeText = start && end ? (start === end ? start : `${start}\u2013${end}`) : (start || end);
    if (normalizedLevel && rangeText) return `${escapeHtml(normalizedLevel)}: ${escapeHtml(rangeText)}`;
    return escapeHtml(normalizedLevel || rangeText || "Google 未返回");
}

function formatGoogleMoney(money) {
    if (!money || !money.currencyCode) return "";
    const units = money.units === undefined || money.units === null ? "0" : String(money.units);
    const nanos = Number(money.nanos || 0);
    const fraction = nanos ? `.${String(Math.abs(nanos)).padStart(9, "0").replace(/0+$/, "")}` : "";
    return `${money.currencyCode} ${units}${fraction}`;
}

function escapeHtml(value) {
    return String(value)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}

function closeDetail() {
    const panel = document.getElementById("detail-panel");
    if (panel) {
        panel.classList.add("hidden");
        panel.style.display = "none";
    }
    if (activeCorridorLayer) {
        activeCorridorLayer._pinned = false;
        resetCorridor(activeCorridorLayer);
    }
}

function showDetailPanel(panel) {
    if (!panel) return;
    panel.classList.remove("hidden");
    panel.style.display = "block";
    panel.style.zIndex = "10000";
    panel.scrollTop = 0;
    console.log("[corridor] detail panel shown");
}

// ============================================================
// Layer visibility
// ============================================================
function updateLayerVisibility() {
    const checks = {
        "layer-places": layerPlaces,
        "layer-actors": layerActors,
        "layer-districts": layerDistricts,
        "layer-corridors": layerCorridors,
    };
    for (const [id, layer] of Object.entries(checks)) {
        const cb = document.getElementById(id);
        if (!cb || !layer) continue;
        if (cb.checked) {
            if (!map.hasLayer(layer)) map.addLayer(layer);
        } else {
            if (map.hasLayer(layer)) map.removeLayer(layer);
        }
    }
    const actorToggle = document.getElementById("layer-actors");
    if (actorClusterLayer) {
        if (actorToggle?.checked) {
            if (!map.hasLayer(actorClusterLayer)) map.addLayer(actorClusterLayer);
        } else if (map.hasLayer(actorClusterLayer)) {
            map.removeLayer(actorClusterLayer);
        }
    }
    refreshWorldScale();
    bringMapLayersToReadableOrder();
}

// ============================================================
// Event bindings
// ============================================================
document.addEventListener("DOMContentLoaded", async () => {
    // Navigation must remain usable even when a map/backend request fails.
    setupTabs();
    await loadViewerConfig();
    await initMap();
    bindBasemapControls();
    try {
        await initializeMapScopeControls();
    } catch (err) {
        console.warn("Map scope controls unavailable; keeping the loaded city view.", err);
        const hint = document.getElementById("map-scope-hint");
        if (hint) hint.textContent = "世界范围接口暂不可用；请重启 World Studio 后端后刷新此页。";
    }
    loadAll();

    // Activate tab from ?view= param (e.g. after world switch via WorldManager)
    const urlParams = new URLSearchParams(window.location.search);
    const viewParam = urlParams.get("view");
    if (viewParam && ["map", "graph", "manager"].includes(viewParam)) {
        activateTab(viewParam);
        // Clean the URL so refreshes don't force the tab
        window.history.replaceState({}, "", IS_UNIFIED_WORLD_STUDIO ? "/world/" : "/");
    }

    // Layer toggles
    ["layer-places", "layer-actors", "layer-districts", "layer-corridors"].forEach(id => {
        document.getElementById(id).addEventListener("change", updateLayerVisibility);
    });

    // Search
    document.getElementById("filter-search").addEventListener("input", debounce(applyFilters, 300));

    // Category filter
    document.getElementById("filter-category").addEventListener("change", applyFilters);

    // Rating filter
    document.getElementById("filter-rating").addEventListener("input", function() {
        document.getElementById("rating-val").textContent = this.value;
        applyFilters();
    });

    // District filter
    document.getElementById("filter-district").addEventListener("change", applyFilters);

    // Reset
    document.getElementById("btn-reset-filters").addEventListener("click", resetFilters);

    // Close detail
    document.getElementById("btn-close-detail").addEventListener("click", closeDetail);

    // Map click outside detail → close panel
    // Proximity hit-test: clicking on or near a corridor line selects it,
    // even when the thin polyline is hidden under place markers / basemap.
    map.on("click", handleMapClickForCorridor);

});

document.addEventListener("world:changed", async () => {
    try {
        await initializeMapScopeControls();
        hasAutoCentered = false;
        await loadAll();
        await window.CharacterGraph?.refreshCurrentWorld?.();
    } catch (err) {
        console.error("Current world refresh failed:", err);
    }
});

function setupTabs() {
    const buttons = document.querySelectorAll("#topbar .tab-btn");
    if (!buttons.length) return;
    buttons.forEach((btn) => {
        btn.addEventListener("click", () => activateTab(btn.getAttribute("data-view")));
    });
}

function activateTab(view) {
    const buttons = document.querySelectorAll("#topbar .tab-btn");
    buttons.forEach((btn) => {
        btn.classList.toggle("active", btn.getAttribute("data-view") === view);
    });
    document.querySelectorAll(".view").forEach((el) => el.classList.remove("active"));
    const target = document.getElementById("view-" + view);
    if (target) target.classList.add("active");

    if (view === "map" && map) {
        setTimeout(() => map.invalidateSize(), 50);
    }
    if (view === "graph" && window.CharacterGraph) {
        if (!window.CharacterGraph._inited) {
            window.CharacterGraph._inited = true;
            window.CharacterGraph.init();
        } else {
            window.CharacterGraph.ensureSize();
        }
    }
    if (view === "manager" && window.WorldManager) {
        window.WorldManager.init();
    }
}

window.addEventListener("resize", () => {
    if (window.CharacterGraph) window.CharacterGraph.ensureSize?.();
});

// Exposed for WorldManager to reset map view after loading a new world
window.resetMapView = function () {
    hasAutoCentered = false;
};

function getFilters() {
    return {
        q: document.getElementById("filter-search").value.trim() || null,
        category: document.getElementById("filter-category").value || null,
        min_rating: parseFloat(document.getElementById("filter-rating").value) || 0,
        district_id: document.getElementById("filter-district").value || null,
    };
}

function applyFilters() {
    if (currentMapScope !== "city") return;
    loadPlaces(getFilters());
}

function resetFilters() {
    document.getElementById("filter-search").value = "";
    document.getElementById("filter-category").value = "";
    document.getElementById("filter-rating").value = "0";
    document.getElementById("rating-val").textContent = "0";
    document.getElementById("filter-district").value = "";
    applyFilters();
}

function debounce(fn, delay) {
    let timer;
    return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), delay); };
}
