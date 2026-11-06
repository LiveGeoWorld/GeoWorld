/**
 * World Manager — create seed from form, generate world, browse existing worlds.
 */
(function () {
    "use strict";

    const Mgr = {
        worlds: [],
        currentJobId: null,
        pollTimer: null,
        initialized: false,
        lastGeneratedCityId: null,
        currentWorldPath: "",
        activeWorldId: "",
        registeredWorlds: [],
        inventoryScope: "target",
        expansionCity: null,
        selectedCity: null,
        citySearchSessionId: "",
        citySearchTimer: null,
        citySearchRequest: 0,
        placeTypeCatalog: [],
    };

    // Translate presentation text only; preserve dialog decisions and actions.
    function uiDialogText(message) {
        const translate = text => window.GeoUI?.t(text) || text;
        const whole = translate(String(message));
        return whole.split("\n").map(line => {
            const match = line.match(/^(\s*(?:•\s*)?)(.*?)(\s*)$/);
            return match ? match[1] + translate(match[2]) + match[3] : line;
        }).join("\n");
    }
    function uiAlert(message) { alert(uiDialogText(message)); }
    function uiConfirm(message) { return confirm(uiDialogText(message)); }

    // ============================================================
    // API helpers
    // ============================================================
    const IS_UNIFIED_WORLD_STUDIO =
        window.location.pathname === "/world" || window.location.pathname.startsWith("/world/");
    const WORLD_API_PREFIX = IS_UNIFIED_WORLD_STUDIO ? "/world-api" : "/api";

    async function apiGet(path) {
        const resp = await fetch(toWorldApi(path));
        const contentType = resp.headers.get("content-type") || "";
        if (!resp.ok) {
            if (contentType.includes("application/json")) {
                const err = await resp.json().catch(() => ({}));
                throw new Error(err.error || `${path}: ${resp.status}`);
            }
            throw new Error(`${path}: HTTP ${resp.status}`);
        }
        if (!contentType.includes("application/json")) {
            throw new Error(
                "服务器返回了 HTML 而非 JSON。请确认已用最新代码重启 vis.server（python -m vis.server --world ...），" +
                "不要直接用 Live Server 打开静态页面。"
            );
        }
        return resp.json();
    }

    async function apiPost(path, body) {
        const resp = await fetch(toWorldApi(path), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
        });
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            throw new Error(err.error || `${path}: ${resp.status}`);
        }
        return resp.json();
    }

    function toWorldApi(path) {
        return path.startsWith("/api/") ? WORLD_API_PREFIX + path.slice(4) : path;
    }

    function worldStudioMapUrl() {
        return IS_UNIFIED_WORLD_STUDIO ? "/world/?view=map" : "/?view=map";
    }

    function escapeHtml(value) {
        return String(value ?? "")
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#039;");
    }

    function normalizeQualityWarning(value) {
        const warning = String(value ?? "");
        if (warning.includes("too much information is visible at start")) {
            return "";
        }
        if (warning.includes("no real Google reviews were attached")) {
            return "未附加真实 Google 评论";
        }
        if (warning.includes("social_post count is low")) {
            return "实时信息帖数量偏低";
        }
        if (warning.includes("too much information is visible at start")) {
            return "初始可见信息过多";
        }
        if (warning.includes("few conflict groups")) {
            return "信息冲突组数量偏少";
        }
        return warning;
    }

    function renderStatChip(label, value) {
        const n = Number(value) || 0;
        return `<span class="mgr-stat-chip"><span class="mgr-stat-label" data-ui-output>${escapeHtml(label)}</span><span class="mgr-stat-num">${n}</span></span>`;
    }

    function $(id) { return document.getElementById(id); }
    function val(id) { return $(id)?.value?.trim() ?? ""; }
    function num(id, fallback) { const v = parseFloat(val(id)); return isNaN(v) ? fallback : v; }

    const SCALE_WINDOW = {
        "mgr-district-count": 32,
        "mgr-corridor-count": 32,
        "mgr-places-target": 120,
        "mgr-character-count": 30,
        "mgr-search-radius": 1500,
    };

    function ensureSliderCapacity(input, value) {
        if (!input) return;
        const step = Number(input.step) || 1;
        let max = Number(input.max) || 0;
        const windowSize = SCALE_WINDOW[input.id] || Math.max(step * 10, 20);
        while (value > max) {
            max = Math.ceil((Math.max(max + windowSize, value) / step)) * step;
        }
        input.max = String(max);
    }

    function setScaleValue(id, value) {
        const input = $(id);
        if (!input) return;
        const minimum = Number(input.min) || 0;
        const next = Math.max(minimum, Number(value) || minimum);
        ensureSliderCapacity(input, next);
        input.value = String(next);
    }

    function expandSliderWindow(input) {
        if (!input) return;
        const step = Number(input.step) || 1;
        const max = Number(input.max) || 0;
        const windowSize = SCALE_WINDOW[input.id] || Math.max(step * 10, 20);
        input.max = String(Math.ceil((max + windowSize) / step) * step);
    }

    function setSelectValue(id, value) {
        const sel = $(id);
        if (!sel || !value) return;
        if (![...sel.options].some((o) => o.value === value)) {
            const opt = document.createElement("option");
            opt.value = value;
            opt.textContent = value;
            sel.appendChild(opt);
        }
        sel.value = value;
    }

    function activeWorldRecord() {
        return Mgr.registeredWorlds.find((world) => world.world_id === Mgr.activeWorldId) || null;
    }

    function activeWorldName() {
        return activeWorldRecord()?.world_id || Mgr.activeWorldId || "未选择";
    }

    function selectedPlaceCategories() {
        return Array.from(document.querySelectorAll(".mgr-chip.active"))
            .map((chip) => chip.dataset.type)
            .filter(Boolean);
    }

    function queryTypesForCategories(categoryIds) {
        const selected = new Set(categoryIds || []);
        return Mgr.placeTypeCatalog
            .filter((item) => selected.has(item.id))
            .flatMap((item) => item.google_query_types || []);
    }

    function categoriesForSeed(seed) {
        if (Array.isArray(seed.place_categories) && seed.place_categories.length) {
            return seed.place_categories;
        }
        const queryTypes = new Set(seed.place_types || []);
        return Mgr.placeTypeCatalog
            .filter((item) => (item.google_query_types || []).some((type) => queryTypes.has(type)))
            .map((item) => item.id);
    }

    function setSelectedPlaceCategories(categoryIds) {
        const selected = new Set(categoryIds || []);
        document.querySelectorAll(".mgr-chip").forEach((chip) => {
            chip.classList.toggle("active", selected.has(chip.dataset.type));
        });
    }

    function renderPlaceTypeCatalog(catalog) {
        const container = $("mgr-place-types");
        if (!container) return;
        Mgr.placeTypeCatalog = catalog || [];
        container.replaceChildren();
        for (const item of Mgr.placeTypeCatalog) {
            const chip = document.createElement("button");
            chip.type = "button";
            chip.className = `mgr-chip${item.default_selected ? " active" : ""}`;
            chip.dataset.type = item.id;
            chip.textContent = item.label;
            chip.setAttribute('data-ui-output', '');
            container.appendChild(chip);
        }
    }

    async function loadPlaceTypeCatalog() {
        const data = await apiGet("/api/place-type-catalog");
        const categories = Array.isArray(data.categories) ? data.categories : [];
        if (!categories.length) throw new Error("地点类型目录为空");
        renderPlaceTypeCatalog(categories);
    }

    function generationActionLabel() {
        if (!Mgr.activeWorldId) return "选择目标世界后构建";
        if (Mgr.expansionCity) return `扩增 ${Mgr.expansionCity.city_name} 并加入 ${Mgr.activeWorldId}`;
        const cityName = val("mgr-city-name");
        return cityName
            ? `构建 ${cityName} 并加入 ${Mgr.activeWorldId}`
            : `选择城市后构建并加入 ${Mgr.activeWorldId}`;
    }

    function refreshBuildContext() {
        const context = $("mgr-build-context");
        const button = $("mgr-btn-generate");
        if (context) {
            context.innerHTML = Mgr.activeWorldId
                ? `<div><span data-ui-text>当前世界</span><strong>${escapeHtml(activeWorldName())}</strong></div>`
                : '<div><span data-ui-text>当前世界</span><strong data-ui-text>尚未选择</strong></div>';
        }
        if (button && !button.disabled) button.textContent = generationActionLabel();
    }

    function visibleWorldAssets() {
        if (Mgr.inventoryScope === "unregistered") {
            return Mgr.worlds.filter((world) => !world.world_id || world.registration_status === "unregistered_legacy");
        }
        return Mgr.worlds.filter((world) => world.world_id === Mgr.activeWorldId);
    }

    function refreshWorldAssetsHeader(assets = visibleWorldAssets()) {
        const title = $("mgr-worlds-title");
        const count = $("mgr-worlds-count");
        const subtitle = $("mgr-assets-subtitle");
        const scopeSelect = $("mgr-inventory-scope");
        const targetOption = scopeSelect?.querySelector('option[value="target"]');
        if (targetOption) {
            targetOption.textContent = Mgr.activeWorldId
                ? `${Mgr.activeWorldId} 的已登记城市`
                : "当前世界的已登记城市";
        }
        if (title) {
            if (Mgr.inventoryScope === "unregistered") title.textContent = "未登记资产";
            else title.textContent = `${activeWorldName()} 的已构建城市`;
        }
        if (subtitle) {
            if (Mgr.inventoryScope === "unregistered") {
                subtitle.textContent = "仅显示尚未绑定到任何世界的历史资产；其他世界的城市不会出现在此处。";
            } else {
                subtitle.textContent = Mgr.activeWorldId
                    ? `仅显示当前世界 ${Mgr.activeWorldId} 已登记的城市；选择城市后可在左侧继续扩增。`
                    : "请先选择一个当前世界。";
            }
        }
        if (count) count.textContent = assets.length;
    }

    function setCityFieldsLocked(locked) {
        ["mgr-city-name", "mgr-city-id", "mgr-country", "mgr-lat", "mgr-lng",
            "mgr-bbox-n", "mgr-bbox-s", "mgr-bbox-e", "mgr-bbox-w"].forEach((id) => {
            const el = $(id);
            if (el) el.readOnly = locked;
        });
        ["mgr-language", "mgr-timezone", "mgr-auto-bbox"].forEach((id) => {
            const el = $(id);
            if (el) el.disabled = locked;
        });
    }

    function selectedCityId(geo) {
        if (geo.city_id) return geo.city_id;
        const slug = (value) => String(value || "")
            .toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
        return slug(geo.city_name);
    }

    function applyGeocodeToForm(geo, { lock = true } = {}) {
        if (geo.city_name) $("mgr-city-name").value = geo.city_name;
        $("mgr-city-id").value = selectedCityId(geo);
        if (geo.country) $("mgr-country").value = geo.country;
        if (geo.timezone) setSelectValue("mgr-timezone", geo.timezone);
        if (geo.language) setSelectValue("mgr-language", geo.language);
        if (geo.lat !== undefined) $("mgr-lat").value = Number(geo.lat).toFixed(6);
        if (geo.lng !== undefined) $("mgr-lng").value = Number(geo.lng).toFixed(6);
        if (geo.bbox) {
            $("mgr-auto-bbox").checked = false;
            $("mgr-bbox-fields").classList.remove("hidden");
            $("mgr-bbox-n").value = Number(geo.bbox.north).toFixed(4);
            $("mgr-bbox-s").value = Number(geo.bbox.south).toFixed(4);
            $("mgr-bbox-e").value = Number(geo.bbox.east).toFixed(4);
            $("mgr-bbox-w").value = Number(geo.bbox.west).toFixed(4);
        }
        Mgr.selectedCity = { ...geo, city_id: selectedCityId(geo) };
        setCityFieldsLocked(lock);
        refreshBuildContext();
    }

    // ============================================================
    // Presets
    // ============================================================
    const PRESETS = {
        smoke: {
            district_count: 4, corridor_count: 4, places_target: 72, character_count: 15,
            search_radius: 2200, google_reviews: 3,
            google_workers: 8, corridor_nodes: 3, corridor_bands: 2, corridor_radius: 1200,
        },
        standard: {
            district_count: 8, corridor_count: 8, places_target: 224, character_count: 20,
            search_radius: 2200, google_reviews: 3,
            google_workers: 8, corridor_nodes: 3, corridor_bands: 2, corridor_radius: 1200,
        },
        large: {
            district_count: 22, corridor_count: 28, places_target: 1344, character_count: 40,
            search_radius: 2600, google_reviews: 3,
            google_workers: 20, corridor_nodes: 5, corridor_bands: 3, corridor_radius: 1500,
        },
    };

    function applyPreset(name) {
        const p = PRESETS[name];
        if (!p) return;
        setScaleValue("mgr-district-count", p.district_count);
        setScaleValue("mgr-corridor-count", p.corridor_count);
        setScaleValue("mgr-places-target", p.places_target);
        setScaleValue("mgr-character-count", p.character_count);
        setScaleValue("mgr-search-radius", p.search_radius);
        $("mgr-google-reviews").value = p.google_reviews;
        $("mgr-google-workers").value = p.google_workers;
        $("mgr-corridor-nodes").value = p.corridor_nodes;
        $("mgr-corridor-bands").value = p.corridor_bands;
        $("mgr-corridor-radius").value = p.corridor_radius;
        updateSliderLabels();
        updateAutoBbox();
        updatePreviewMap();
        updateTotalPlaces();
        if (name === "large") {
            // Also enable bar, pharmacy, university for large worlds
            toggleChip("bar", true);
            toggleChip("pharmacy", true);
            toggleChip("university", true);
        }
    }

    function toggleChip(type, forceActive) {
        const chip = document.querySelector(`.mgr-chip[data-type="${type}"]`);
        if (!chip) return;
        if (forceActive === true) chip.classList.add("active");
        else if (forceActive === false) chip.classList.remove("active");
        else chip.classList.toggle("active");
    }

    // ============================================================
    // Form bindings
    // ============================================================
    function bindForm() {
        // Slider labels
        const sliders = [
            ["mgr-district-count", "mgr-district-count-val"],
            ["mgr-corridor-count", "mgr-corridor-count-val"],
            ["mgr-places-target", "mgr-places-target-val"],
            ["mgr-character-count", "mgr-character-count-val"],
            ["mgr-search-radius", "mgr-search-radius-val"],
        ];
        for (const [inputId, labelId] of sliders) {
            $(inputId).addEventListener("input", () => {
                ensureSliderCapacity($(inputId), Number($(inputId).value));
                if (Number($(inputId).value) >= Number($(inputId).max)) expandSliderWindow($(inputId));
                $(labelId).textContent = $(inputId).value;
                if (inputId === "mgr-search-radius") {
                    updateAutoBbox();
                    updatePreviewMap();
                }
                updateTotalPlaces();
                if (Mgr.expansionCity) refreshExpansionContext();
            });
        }
        document.querySelectorAll(".mgr-slider-step").forEach((button) => {
            button.addEventListener("click", () => {
                const input = $(button.dataset.target);
                if (!input) return;
                const step = Number(input.step) || 1;
                const direction = Number(button.dataset.direction) || 0;
                let next = Number(input.value) + (direction * step);
                if (direction > 0 && next > Number(input.max)) expandSliderWindow(input);
                next = Math.max(Number(input.min) || 0, next);
                setScaleValue(input.id, next);
                input.dispatchEvent(new Event("input", { bubbles: true }));
            });
        });

        // Auto bbox toggle
        $("mgr-auto-bbox").addEventListener("change", () => {
            const checked = $("mgr-auto-bbox").checked;
            $("mgr-bbox-fields").classList.toggle("hidden", checked);
            if (checked) updateAutoBbox();
            updatePreviewMap();
        });
        $("mgr-lat").addEventListener("input", () => { updateAutoBbox(); updatePreviewMap(); });
        $("mgr-lng").addEventListener("input", () => { updateAutoBbox(); updatePreviewMap(); });
        ["mgr-bbox-n", "mgr-bbox-s", "mgr-bbox-e", "mgr-bbox-w"].forEach((id) => {
            $(id).addEventListener("input", updatePreviewMap);
        });

        // Place type chips are rendered from the backend's shared catalog.
        $("mgr-place-types").addEventListener("click", (e) => {
            const chip = e.target.closest(".mgr-chip");
            if (!chip) return;
            e.preventDefault();
            chip.classList.toggle("active");
        });

        // Preset buttons
        document.querySelectorAll(".mgr-preset-btn").forEach((btn) => {
            btn.addEventListener("click", () => {
                document.querySelectorAll(".mgr-preset-btn").forEach((b) => b.classList.remove("active"));
                btn.classList.add("active");
                applyPreset(btn.dataset.preset);
                updateTotalPlaces();
            });
        });
        // Default active preset
        document.querySelector('.mgr-preset-btn[data-preset="smoke"]')?.classList.add("active");

        // City search
        $("mgr-btn-search").addEventListener("click", () => doCitySearch({ manual: true }));
        $("mgr-city-search").addEventListener("input", () => {
            const query = val("mgr-city-search").trim();
            if (Mgr.citySearchTimer) clearTimeout(Mgr.citySearchTimer);
            if (!shouldSuggestCity(query)) {
                $("mgr-search-result").classList.add("hidden");
                return;
            }
            Mgr.citySearchTimer = setTimeout(() => doCitySearch(), 420);
        });
        $("mgr-city-search").addEventListener("keydown", (e) => {
            if (e.key === "Enter") { e.preventDefault(); doCitySearch({ manual: true }); }
        });

        // Seed import
        $("mgr-import-file").addEventListener("change", handleSeedImport);

        // Generate button
        $("mgr-btn-generate").addEventListener("click", startGeneration);
        $("mgr-btn-cancel-job").addEventListener("click", cancelCurrentJob);
        $("mgr-btn-retry-reuse").addEventListener("click", () => retryBuild("continue"));
        $("mgr-btn-retry-refresh").addEventListener("click", () => retryBuild("rebuild"));
        $("mgr-btn-register-actual").addEventListener("click", registerActualCandidate);
        $("mgr-btn-create-world").addEventListener("click", createEmptyWorld);
        $("mgr-btn-cancel-expansion").addEventListener("click", exitExpansionMode);
        $("mgr-active-world").addEventListener("change", async (event) => {
            const select = event.target;
            const requestedWorldId = select.value;
            select.disabled = true;
            try {
                await activateWorld(requestedWorldId);
            } catch (err) {
                select.value = Mgr.activeWorldId;
                uiAlert((window.GeoUI?.t("切换当前世界失败: ") || "切换当前世界失败: ") + (err.message || err));
            } finally {
                select.disabled = false;
            }
        });
        $("mgr-inventory-scope").addEventListener("change", (event) => {
            Mgr.inventoryScope = event.target.value;
            renderWorldsList();
        });
    }

    // ============================================================
    // City search → select suggestion → resolve one city
    // ============================================================
    function shouldSuggestCity(query) {
        const chineseCharacters = (query.match(/[\u3400-\u9fff]/g) || []).length;
        const latinCharacters = (query.match(/[A-Za-z]/g) || []).length;
        return chineseCharacters >= 2 || latinCharacters >= 3;
    }

    async function doCitySearch({ manual = false } = {}) {
        const query = val("mgr-city-search").trim();
        if (!query) return;
        if (!manual && !shouldSuggestCity(query)) return;
        const btn = $("mgr-btn-search");
        const result = $("mgr-search-result");
        const requestId = ++Mgr.citySearchRequest;
        if (!Mgr.citySearchSessionId) Mgr.citySearchSessionId = crypto.randomUUID?.() || `city_${Date.now()}_${Math.random().toString(16).slice(2)}`;
        btn.disabled = true;
        btn.textContent = "查找中...";
        result.classList.remove("hidden");
        result.innerHTML = '<span class="mgr-geo-addr" data-ui-text>正在查找城市...</span>';
        try {
            const data = await apiGet(`/api/city-search/suggest?q=${encodeURIComponent(query)}&session_id=${encodeURIComponent(Mgr.citySearchSessionId)}`);
            if (requestId !== Mgr.citySearchRequest) return;
            const suggestions = data.suggestions || [];
            if (!suggestions.length) throw new Error("没有找到可选城市，请补充国家或调整输入");
            result.innerHTML = `<div class="mgr-geo-addr"><span data-ui-text>选择一个具体城市</span>${data.fallback_used ? '<span data-ui-text>（备用数据源）</span>' : ""}</div>`;
            const candidatesList = document.createElement("div");
            candidatesList.className = "mgr-geocode-list";
            result.appendChild(candidatesList);
            for (const suggestion of suggestions) {
                const candidate = document.createElement("button");
                candidate.type = "button";
                candidate.className = "mgr-geocode-candidate";
                candidate.innerHTML = `${escapeHtml(suggestion.primary_text || suggestion.label)}<small>${escapeHtml(suggestion.secondary_text || suggestion.source || "")}</small>`;
                candidate.addEventListener("click", async () => {
                    candidate.disabled = true;
                    candidate.textContent = "正在确认城市...";
                    try {
                        const geo = suggestion.requires_resolve
                            ? await apiPost("/api/city-search/resolve", { place_id: suggestion.place_id, session_id: Mgr.citySearchSessionId })
                            : suggestion.resolved;
                        if (!geo) throw new Error("城市解析未返回有效坐标");
                        Mgr.citySearchSessionId = "";
                        Mgr.citySearchRequest += 1;
                        Mgr.expansionCity = null;
                        applyGeocodeToForm(geo);
                        result.innerHTML = `<div class="mgr-geo-addr"><span data-ui-text>已选择：</span> ${escapeHtml(geo.formatted_address)}</div><div class="mgr-geo-locale"><span data-ui-text>来源：</span> ${escapeHtml(geo.source || "")}</div>`;
                        updatePreviewMap();
                        updateTotalPlaces();
                        refreshExpansionContext();
                    } catch (err) {
                        candidate.disabled = false;
                        candidate.textContent = suggestion.primary_text || suggestion.label;
                        result.insertAdjacentHTML("beforeend", `<span class="mgr-error"><span data-ui-text>无法确认该城市：</span> ${escapeHtml(err.message || err)}</span>`);
                    }
                });
                candidatesList.appendChild(candidate);
            }
        } catch (err) {
            if (requestId === Mgr.citySearchRequest) result.innerHTML = `<span class="mgr-error"><span data-ui-text>城市查找暂不可用：</span> ${escapeHtml(err.message || "请稍后重试")}</span>`;
        } finally {
            if (requestId === Mgr.citySearchRequest) {
                btn.disabled = false;
                btn.textContent = "查找";
            }
        }
    }

    // ============================================================
    // Mini preview map (interactive: draggable marker + click to set center)
    // ============================================================
    let previewMap = null;
    let previewMarker = null;
    let previewRect = null;
    let previewRadiusCircle = null;
    let previewRadiusHalo = null;
    let previewBboxOutline = null;

    const centerPinIcon = L.divIcon({
        className: "mgr-center-pin-wrap",
        html: '<div class="mgr-center-pin"></div>',
        iconSize: [36, 36],
        iconAnchor: [18, 32],
    });

    function initPreviewMap() {
        const container = $("mgr-preview-map");
        if (!container) return;
        previewMap = L.map("mgr-preview-map", {
            zoomControl: false,
            attributionControl: false,
            dragging: true,
            scrollWheelZoom: false,
            doubleClickZoom: false,
        }).setView([35.6764, 139.6500], 4);

        if (window.VIEWER_CONFIG?.google_maps_api_key) {
            L.tileLayer(
                `https://mt{s}.google.com/vt/lyrs=m&x={x}&y={y}&z={z}&key=${window.VIEWER_CONFIG.google_maps_api_key}`,
                { subdomains: "0123", maxZoom: 20 }
            ).addTo(previewMap);
        } else {
            L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 19 }).addTo(previewMap);
        }

        L.control.zoom({ position: "bottomright" }).addTo(previewMap);

        previewMap.on("click", (e) => {
            const { lat, lng } = e.latlng;
            $("mgr-lat").value = lat.toFixed(6);
            $("mgr-lng").value = lng.toFixed(6);
            updateMarkerPosition(lat, lng);
            updateAutoBbox();
            updateBboxRect();
        });

        setTimeout(() => { if (previewMap) previewMap.invalidateSize(); }, 200);
    }

    function ensurePreviewMap() {
        if (previewMap) return true;
        initPreviewMap();
        return previewMap !== null;
    }

    function createMarker(lat, lng) {
        previewMarker = L.marker([lat, lng], {
            draggable: true,
            icon: centerPinIcon,
            zIndexOffset: 1000,
        }).addTo(previewMap);

        previewMarker.on("drag", (e) => {
            const pos = e.target.getLatLng();
            $("mgr-lat").value = pos.lat.toFixed(6);
            $("mgr-lng").value = pos.lng.toFixed(6);
            updateBboxRect();
        });

        previewMarker.on("dragend", (e) => {
            const pos = e.target.getLatLng();
            $("mgr-lat").value = pos.lat.toFixed(6);
            $("mgr-lng").value = pos.lng.toFixed(6);
            updateAutoBbox();
            updateBboxRect();
        });
    }

    function updateMarkerPosition(lat, lng) {
        if (!previewMap) return;
        if (previewMarker) {
            previewMarker.setLatLng([lat, lng]);
        } else {
            createMarker(lat, lng);
        }
    }

    function updateBboxRect() {
        if (!previewMap) return;
        const lat = num("mgr-lat", 0);
        const lng = num("mgr-lng", 0);
        if (!lat || !lng) return;

        const radius = num("mgr-search-radius", 2200);
        const bboxActive = $("mgr-auto-bbox").checked;
        let n, s, e, w;
        if (bboxActive) {
            const dLat = radius / 111000;
            const dLng = radius / (111000 * Math.cos((lat * Math.PI) / 180));
            n = lat + dLat; s = lat - dLat; e = lng + dLng; w = lng - dLng;
        } else {
            n = num("mgr-bbox-n", 0); s = num("mgr-bbox-s", 0);
            e = num("mgr-bbox-e", 0); w = num("mgr-bbox-w", 0);
        }

        // Clear previous overlays
        if (previewRect) { previewMap.removeLayer(previewRect); previewRect = null; }
        if (previewBboxOutline) { previewMap.removeLayer(previewBboxOutline); previewBboxOutline = null; }
        if (previewRadiusCircle) { previewMap.removeLayer(previewRadiusCircle); previewRadiusCircle = null; }
        if (previewRadiusHalo) { previewMap.removeLayer(previewRadiusHalo); previewRadiusHalo = null; }

        if (n && s && e && w && n > s && e > w) {
            // Subtle dark halo for crisp edges against bright tiles
            previewBboxOutline = L.rectangle([[s, w], [n, e]], {
                color: "#11111b",
                weight: 4,
                opacity: 0.55,
                fill: false,
                interactive: false,
            }).addTo(previewMap);
            // Bbox — soft lavender, solid line + faint fill
            previewRect = L.rectangle([[s, w], [n, e]], {
                color: "#cba6f7",
                weight: 2,
                opacity: 0.95,
                fillColor: "#cba6f7",
                fillOpacity: 0.1,
                interactive: false,
            }).addTo(previewMap);
        }
    }

    function updatePreviewMap() {
        const lat = num("mgr-lat", 0);
        const lng = num("mgr-lng", 0);
        if (!ensurePreviewMap()) return;
        if (!lat || !lng) return;

        previewMap.invalidateSize();
        requestAnimationFrame(() => {
            if (!previewMap) return;
            previewMap.invalidateSize();
            updateMarkerPosition(lat, lng);
            updateBboxRect();

            const bboxActive = $("mgr-auto-bbox").checked;
            let n, s, e, w;
            if (bboxActive) {
                const radius = num("mgr-search-radius", 2200);
                const dLat = radius / 111000;
                const dLng = radius / (111000 * Math.cos((lat * Math.PI) / 180));
                n = lat + dLat; s = lat - dLat; e = lng + dLng; w = lng - dLng;
            } else {
                n = num("mgr-bbox-n", 0); s = num("mgr-bbox-s", 0);
                e = num("mgr-bbox-e", 0); w = num("mgr-bbox-w", 0);
            }
            if (n && s && e && w && n > s && e > w) {
                previewMap.fitBounds([[s, w], [n, e]], { padding: [20, 20] });
            } else {
                previewMap.setView([lat, lng], 11);
            }
        });
    }

    // ============================================================
    // Total places estimation
    // ============================================================
    function updateTotalPlaces() {
        const total = parseInt($("mgr-places-target").value, 10) || 0;
        const totalElement = $("mgr-total-places");
        const detailElement = $("mgr-total-detail");
        if (totalElement) totalElement.textContent = String(total);
        if (detailElement) detailElement.textContent = Mgr.expansionCity
            ? "扩增完成后记录实际数量"
            : "构建完成后记录实际数量";
    }

    // ============================================================
    // Seed file import
    // ============================================================
    function handleSeedImport(e) {
        const file = e.target.files[0];
        if (!file) return;
        const reader = new FileReader();
        reader.onload = (ev) => {
            try {
                const seed = JSON.parse(ev.target.result);
                fillFormFromSeed(seed);
            } catch (err) {
                uiAlert((window.GeoUI?.t("JSON 解析失败: ") || "JSON 解析失败: ") + err.message);
            }
        };
        reader.readAsText(file);
        // Reset input so same file can be re-selected
        e.target.value = "";
    }

    function fillFormFromSeed(seed) {
        const set = (id, v) => { const el = $(id); if (el && v !== undefined) el.value = v; };
        set("mgr-city-name", seed.city_name);
        set("mgr-city-id", seed.city_id);
        set("mgr-country", seed.country);
        set("mgr-timezone", seed.timezone);
        set("mgr-language", seed.language);
        if (seed.center) {
            set("mgr-lat", seed.center.lat);
            set("mgr-lng", seed.center.lng);
        }
        if (seed.bbox) {
            $("mgr-auto-bbox").checked = false;
            $("mgr-bbox-fields").classList.remove("hidden");
            set("mgr-bbox-n", seed.bbox.north);
            set("mgr-bbox-s", seed.bbox.south);
            set("mgr-bbox-e", seed.bbox.east);
            set("mgr-bbox-w", seed.bbox.west);
        }
        set("mgr-district-count", seed.district_count);
        set("mgr-corridor-count", seed.corridor_count);
        const seedDistricts = Number(seed.district_count) || 0;
        const seedCorridors = Number(seed.corridor_count) || 0;
        const seedPlaces = Number(seed.target_place_count) || (
            seedDistricts * (Number(seed.places_per_district) || 0)
            + seedCorridors * (Number(seed.corridor_band_limit) || 2) * 5
        );
        set("mgr-places-target", seedPlaces || 1);
        set("mgr-character-count", 15);
        set("mgr-search-radius", seed.search_radius_m);
        set("mgr-notes", seed.notes);
        set("mgr-google-reviews", seed.google_reviews_per_place);
        set("mgr-google-workers", seed.google_max_workers);
        set("mgr-corridor-nodes", seed.corridor_node_sample_limit);
        set("mgr-corridor-bands", seed.corridor_band_limit);
        set("mgr-corridor-radius", seed.corridor_place_radius_m);

        applyGeocodeToForm({
            city_id: seed.city_id,
            city_name: seed.city_name,
            country: seed.country,
            timezone: seed.timezone,
            language: seed.language,
            lat: seed.center?.lat,
            lng: seed.center?.lng,
            bbox: seed.bbox,
            source: "seed",
        });

        setSelectedPlaceCategories(categoriesForSeed(seed));

        updateSliderLabels();
        updateAutoBbox();
        updatePreviewMap();
        updateTotalPlaces();
        refreshBuildContext();
    }

    function refreshExpansionContext() {
        const context = $("mgr-expansion-context");
        const mode = $("mgr-scale-mode");
        const cancel = $("mgr-btn-cancel-expansion");
        if (!context) return;
        const current = Mgr.expansionCity;
        if (!current) {
            context.textContent = "";
            context.classList.add("hidden");
            mode.textContent = "";
            cancel?.classList.add("hidden");
            setScaleMinimums({ districts: 2, corridors: 2, places: 1, characters: 1 });
            document.querySelectorAll(".mgr-preset-btn").forEach((button) => { button.disabled = false; });
            return;
        }
        const counts = current.counts || {};
        context.classList.remove("hidden");
        mode.setAttribute("data-ui-output", "");
        mode.textContent = `（追加式扩增：${current.city_name}）`;
        cancel?.classList.remove("hidden");
        setScaleMinimums({
            districts: counts.districts || 0,
            corridors: counts.corridors || 0,
            places: counts.places || 0,
            characters: current.characters || 0,
        });
        document.querySelectorAll(".mgr-preset-btn").forEach((button) => { button.disabled = true; });
        const target = expansionTargets();
        const delta = {
            districts: target.districts - (counts.districts || 0),
            corridors: target.corridors - (counts.corridors || 0),
            places: target.places - (counts.places || 0),
            characters: target.characters - (current.characters || 0),
        };
        const signed = value => value >= 0 ? "+" + value : String(value);
        context.setAttribute("data-ui-output", "");
        context.textContent = `正在扩增：${Mgr.activeWorldId} / ${current.city_name}。当前 ${counts.districts || 0} 区域、${counts.corridors || 0} 通道、${counts.places || 0} POI、${current.characters || 0} 人物。计划新增：${signed(delta.districts)} 区域、${signed(delta.corridors)} 通道、${signed(delta.places)} POI、${signed(delta.characters)} 人物。`;
    }

    function expansionTargets() {
        return {
            districts: parseInt($("mgr-district-count")?.value, 10) || 0,
            corridors: parseInt($("mgr-corridor-count")?.value, 10) || 0,
            places: parseInt($("mgr-places-target")?.value, 10) || 0,
            characters: parseInt($("mgr-character-count")?.value, 10) || 0,
        };
    }

    function setScaleMinimums(minimums) {
        const controls = {
            districts: $("mgr-district-count"),
            corridors: $("mgr-corridor-count"),
            places: $("mgr-places-target"),
            characters: $("mgr-character-count"),
        };
        Object.entries(controls).forEach(([key, input]) => {
            if (!input) return;
            const minimum = Number(minimums[key]) || 0;
            input.min = String(minimum);
            // The max is a working window, not a business constraint.  A
            // 28-district city begins with room to reach 60 and expands again.
            const windowSize = SCALE_WINDOW[input.id] || 20;
            ensureSliderCapacity(input, minimum + windowSize);
            if (Number(input.value) < minimum) input.value = String(minimum);
        });
    }

    function setExpansionTargets(current) {
        const counts = current.counts || {};
        const setTarget = (id, value) => {
            const input = $(id);
            input.min = value;
            ensureSliderCapacity(input, Number(value) + (SCALE_WINDOW[id] || 20));
            input.value = value;
        };
        setTarget("mgr-district-count", counts.districts || 0);
        setTarget("mgr-corridor-count", counts.corridors || 0);
        setTarget("mgr-places-target", counts.places || 0);
        setTarget("mgr-character-count", current.characters || 0);
    }

    function updateSliderLabels() {
        $("mgr-district-count-val").textContent = $("mgr-district-count").value;
        $("mgr-corridor-count-val").textContent = $("mgr-corridor-count").value;
        $("mgr-places-target-val").textContent = $("mgr-places-target").value;
        $("mgr-character-count-val").textContent = $("mgr-character-count").value;
        $("mgr-search-radius-val").textContent = $("mgr-search-radius").value;
    }

    function updateAutoBbox() {
        if (!$("mgr-auto-bbox").checked) return;
        const lat = num("mgr-lat", 0);
        const lng = num("mgr-lng", 0);
        const radius = num("mgr-search-radius", 2200);
        if (!lat || !lng) return;
        // Approximate: 1 degree lat ≈ 111km
        const dLat = radius / 111000;
        const dLng = radius / (111000 * Math.cos((lat * Math.PI) / 180));
        $("mgr-bbox-n").value = (lat + dLat).toFixed(4);
        $("mgr-bbox-s").value = (lat - dLat).toFixed(4);
        $("mgr-bbox-e").value = (lng + dLng).toFixed(4);
        $("mgr-bbox-w").value = (lng - dLng).toFixed(4);
    }

    // ============================================================
    // Collect form → seed JSON
    // ============================================================
    function collectSeed() {
        const placeCategories = selectedPlaceCategories();
        const placeTypes = queryTypesForCategories(placeCategories);

        const lat = num("mgr-lat", 0);
        const lng = num("mgr-lng", 0);
        const radius = num("mgr-search-radius", 2200);
        const dLat = radius / 111000;
        const dLng = radius / (111000 * Math.cos((lat * Math.PI) / 180));
        const districtCount = parseInt($("mgr-district-count").value, 10) || 1;
        const corridorCount = parseInt($("mgr-corridor-count").value, 10) || 0;
        const desiredPlaces = parseInt($("mgr-places-target").value, 10) || 1;
        const corridorPlaces = corridorCount * (parseInt($("mgr-corridor-bands").value, 10) || 2) * 5;

        const seed = {
            city_id: val("mgr-city-id"),
            city_name: val("mgr-city-name"),
            country: val("mgr-country"),
            center: { lat, lng },
            timezone: val("mgr-timezone"),
            language: val("mgr-language"),
            bbox: $("mgr-auto-bbox").checked
                ? { north: +(lat + dLat).toFixed(4), south: +(lat - dLat).toFixed(4), east: +(lng + dLng).toFixed(4), west: +(lng - dLng).toFixed(4) }
                : { north: num("mgr-bbox-n", 0), south: num("mgr-bbox-s", 0), east: num("mgr-bbox-e", 0), west: num("mgr-bbox-w", 0) },
            district_count: districtCount,
            corridor_count: corridorCount,
            places_per_district: Math.max(
                4,
                Math.ceil(
                    Math.max(1, desiredPlaces - corridorPlaces) / districtCount,
                ),
            ),
            search_radius_m: radius,
            google_max_workers: parseInt($("mgr-google-workers").value),
            corridor_node_sample_limit: parseInt($("mgr-corridor-nodes").value),
            corridor_band_limit: parseInt($("mgr-corridor-bands").value),
            corridor_place_types_per_band: 3,
            corridor_text_queries_per_band: 2,
            corridor_place_radius_m: parseInt($("mgr-corridor-radius").value),
            place_categories: placeCategories,
            place_types: placeTypes,
            notes: val("mgr-notes"),
            google_reviews_per_place: parseInt($("mgr-google-reviews").value),
            target_place_count: desiredPlaces,
            validate_places: $("mgr-validate-places").checked,
            validate_place_geocode: $("mgr-validate-places").checked,
            llm_semantic_shards: true,
            llm_semantic_max_workers: 8,
            llm_semantic_timeout_seconds: 120,
            llm_district_place_limit: 18,
            llm_corridor_place_limit: 18,
        };
        return seed;
    }

    function exitExpansionMode() {
        Mgr.expansionCity = null;
        Mgr.selectedCity = null;
        ["mgr-city-name", "mgr-city-id", "mgr-country", "mgr-lat", "mgr-lng",
            "mgr-bbox-n", "mgr-bbox-s", "mgr-bbox-e", "mgr-bbox-w", "mgr-notes"].forEach((id) => {
            const input = $(id);
            if (input) input.value = "";
        });
        $("mgr-city-search").value = "";
        $("mgr-search-result").classList.add("hidden");
        $("mgr-auto-bbox").checked = true;
        $("mgr-bbox-fields").classList.add("hidden");
        setCityFieldsLocked(false);
        applyPreset("smoke");
        refreshExpansionContext();
        refreshBuildContext();
        // Card state is derived during rendering, so clear its stale expansion label now.
        renderWorldsList();
        updatePreviewMap();
        $("mgr-city-search").focus();
    }

    function validateSeed(seed) {
        const errors = [];
        if (!seed.city_name) errors.push("城市名称不能为空");
        if (!seed.city_id) errors.push("城市 ID 不能为空");
        if (!seed.country) errors.push("国家不能为空");
        if (!seed.center.lat || !seed.center.lng) errors.push("中心坐标无效");
        if (!seed.bbox || seed.bbox.north <= seed.bbox.south) errors.push("边界框无效 (N 必须大于 S)");
        if (seed.bbox && seed.bbox.east <= seed.bbox.west) errors.push("边界框无效 (E 必须大于 W)");
        if (!seed.place_types || seed.place_types.length === 0) errors.push("至少选择一种地点类型");
        if (!Mgr.expansionCity && !Mgr.selectedCity) errors.push("请先从城市搜索结果中选择具体城市，或导入 Seed");
        return errors;
    }

    async function loadWorldRegistry() {
        const select = $("mgr-active-world");
        if (!select) return;
        const data = await apiGet("/api/world-registry");
        Mgr.registeredWorlds = data.worlds || [];
        const preferred = Mgr.activeWorldId || data.active_world_id;
        select.innerHTML = "";
        for (const world of Mgr.registeredWorlds) {
            const option = document.createElement("option");
            option.value = world.world_id;
            option.textContent = world.city_count === 1 ? `${world.world_id} (1 个城市)` : `${world.world_id} (${world.city_count} 个城市)`;
            option.setAttribute('data-ui-output', '');
            select.appendChild(option);
        }
        if (preferred && Mgr.registeredWorlds.some((world) => world.world_id === preferred)) {
            select.value = preferred;
        }
        Mgr.activeWorldId = select.value || "";
        $("mgr-btn-generate").disabled = !Mgr.activeWorldId;
        refreshBuildContext();
        refreshExpansionContext();
    }

    async function activateWorld(worldId) {
        if (!worldId) return;
        await apiPost("/api/worlds/open", { world_id: worldId });
        Mgr.activeWorldId = worldId;
        Mgr.currentJobId = null;
        if (Mgr.pollTimer) clearTimeout(Mgr.pollTimer);
        Mgr.expansionCity = null;
        const select = $("mgr-active-world");
        if (select) select.value = worldId;
        refreshBuildContext();
        refreshExpansionContext();
        await loadWorlds();
        await restoreLatestJob();
        document.dispatchEvent(new CustomEvent("world:changed", { detail: { world_id: worldId } }));
    }

    async function createEmptyWorld() {
        const input = $("mgr-new-world-id");
        const worldId = input.value.trim();
        if (!worldId) {
            input.focus();
            return;
        }
        const button = $("mgr-btn-create-world");
        button.disabled = true;
        button.textContent = "创建中...";
        try {
            await apiPost("/api/world-registry/create", { world_id: worldId });
            input.value = "";
            await loadWorldRegistry();
            await activateWorld(worldId);
        } catch (err) {
            uiAlert((window.GeoUI?.t("新建空世界失败: ") || "新建空世界失败: ") + (err.message || err));
        } finally {
            button.disabled = false;
            button.textContent = "新建空世界";
        }
    }

    // ============================================================
    // Worlds list (right panel)
    // ============================================================
    async function loadWorlds() {
        const list = $("mgr-worlds-list");
        if (!list) return;
        try {
            const data = await apiGet("/api/worlds");
            Mgr.worlds = data.worlds || [];
            renderWorldsList();
            updateCurrentWorldDisplay(data.current_world_path);
        } catch (err) {
            list.innerHTML = `<div class="mgr-error">加载世界列表失败: ${escapeHtml(err.message)}</div>`;
        }
    }

    function renderWorldsList() {
        const list = $("mgr-worlds-list");
        if (!list) return;
        const assets = visibleWorldAssets();
        refreshWorldAssetsHeader(assets);
        if (!assets.length) {
            const message = Mgr.inventoryScope === "target" && Mgr.activeWorldId
                ? `${escapeHtml(activeWorldName())} <span data-ui-text>还没有已构建城市。</span><br><span data-ui-text>从左侧选择城市后开始构建。</span>`
                : Mgr.inventoryScope === "unregistered"
                    ? '<span data-ui-text>没有未登记的历史城市资产。</span>'
                    : '<span data-ui-text>还没有可显示的城市资产。</span>';
            list.innerHTML = `<div class="mgr-empty-state">${message}</div>`;
            return;
        }
        list.innerHTML = "";
        for (const world of assets) {
            list.appendChild(renderWorldCard(world));
        }
        // Scroll newly generated world into view
        if (Mgr.lastGeneratedCityId) {
            const fresh = list.querySelector(".mgr-world-new");
            if (fresh) {
                fresh.scrollIntoView({ behavior: "smooth", block: "nearest" });
            }
        }
    }

    function renderWorldCard(world) {
        const card = document.createElement("div");
        const isNew = Mgr.lastGeneratedCityId && world.city_id === Mgr.lastGeneratedCityId;
        const isExpansionSelection = Mgr.expansionCity?.city_id === world.city_id && world.world_id === Mgr.activeWorldId;
        card.className = "mgr-world-card" + (isNew ? " mgr-world-new" : "") + (isExpansionSelection ? " mgr-world-selected" : "");
        const q = world.quality || {};
        const hasQuality = typeof q.ok === "boolean";
        const qualityOk = q.ok === true;
        const counts = world.counts || {};
        const mtime = world.mtime ? new Date(world.mtime * 1000).toLocaleString("zh-CN") : "";
        const missingLabels = {
            characters: "人物与关系",
            regional_knowledge: "地域资料",
        };
        const missingCapabilities = (world.missing_capabilities || [])
            .map((item) => missingLabels[item] || item);
        const state = !hasQuality
            ? { label: "状态：地图校验未记录", kind: "unknown" }
            : !qualityOk
            ? { label: "状态：地图校验未通过", kind: "blocked" }
            : missingCapabilities.length
            ? { label: "状态：待补齐", kind: "pending" }
            : { label: "状态：可用", kind: "ready" };
        const issueParts = [];
        if (missingCapabilities.length) issueParts.push(`${missingCapabilities.length} 项待补齐`);
        const issueDetails = issueParts.length
            ? `<details class="mgr-quality-details"><summary><span data-ui-text>质量与资产</span> · ${issueParts.map((issue) => `<span data-ui-output>${escapeHtml(issue)}</span>`).join(" · ")}</summary>
                ${missingCapabilities.length ? `<section class="mgr-issue-group"><h4 data-ui-text>待补齐</h4><ul>${missingCapabilities.map((item) => `<li data-ui-text>${escapeHtml(item)}</li>`).join("")}</ul></section>` : ""}
                ${hasQuality ? `<p class="mgr-quality-baseline" data-ui-output>地图基础校验：${qualityOk ? "通过" : "未通过"}</p>` : ""}
            </details>`
            : (hasQuality ? `<p class="mgr-quality-baseline" data-ui-output>地图基础校验：${qualityOk ? "通过" : "未通过"}</p>` : "");
        const isCurrent = Mgr.currentWorldPath === world.world_path;
        const sourceBadge = Mgr.inventoryScope === "target" ? ""
            : '<span class="mgr-badge mgr-badge-legacy" data-ui-text>未登记资产</span>';
        const versionBadge = world.world_id
            ? `<span class="mgr-badge mgr-badge-world">v${Number(world.active_version) || 1}</span>`
            : "";

        card.innerHTML = `
            <div class="mgr-card-header">
                <div class="mgr-card-title">
                    ${escapeHtml(world.city_name)}
                    ${isCurrent ? '<span class="mgr-badge mgr-badge-current" data-ui-text>当前</span>' : ""}
                    ${isNew ? '<span class="mgr-badge mgr-badge-new" data-ui-text>刚刚生成</span>' : ""}
                </div>
                ${world.lifecycle_status === "archived" ? '<div class="mgr-card-variant" data-ui-text>已归档</div>' : ""}
            </div>
            <div class="mgr-card-meta">
                <span class="mgr-mono mgr-small">${escapeHtml(world.city_id)}</span>
                    ${sourceBadge}
                    ${versionBadge}
                ${world.geo_provider ? `<span class="mgr-badge">${escapeHtml(world.geo_provider)}</span>` : ""}
                <span class="mgr-city-status mgr-city-status-${state.kind}" data-ui-output>${state.label}</span>
            </div>
            ${issueDetails}
            <div class="mgr-card-stats">
                ${renderStatChip("区域", counts.districts)}
                ${renderStatChip("通道", counts.corridors)}
                ${renderStatChip("地点", counts.places)}
                ${renderStatChip("服务", counts.services)}
                ${renderStatChip("信息", counts.information_items)}
                ${renderStatChip("事件", counts.event_templates)}
            </div>
            ${mtime ? `<div class="mgr-card-time">${escapeHtml(mtime)}</div>` : ""}
            <div class="mgr-card-actions">
                ${world.world_id
                    ? '<button class="mgr-btn mgr-btn-select-version" data-ui-text>选择版本</button>'
                    : `<button class="mgr-btn mgr-btn-load" data-ui-text data-world-path="${escapeHtml(world.world_path)}" ${!world.world_exists ? "disabled" : ""}>${world.world_exists ? "加载到地图" : "world.json 缺失"}</button>`}
                ${world.deletable === false
                    ? ''
                    : `<button class="mgr-btn mgr-btn-delete" data-manifest-path="${escapeHtml(world.manifest_path)}" data-city-name="${escapeHtml(world.city_name)}" data-ui-text>删除</button>`}
            </div>
        `;

        const loadBtn = card.querySelector(".mgr-btn-load");
        if (loadBtn && world.world_exists) {
            loadBtn.addEventListener("click", () => loadWorld(world.world_path, world.city_name, world.world_id, world.city_id));
        }
        const versionBtn = card.querySelector(".mgr-btn-select-version");
        if (versionBtn) versionBtn.addEventListener("click", () => openVersionDialog(world));
        const delBtn = card.querySelector(".mgr-btn-delete");
        if (delBtn) {
            delBtn.addEventListener("click", () => deleteWorld(world.manifest_path, world.city_name));
        }
        if (world.world_id) {
            const actions = card.querySelector(".mgr-card-actions");
            if (world.world_id === Mgr.activeWorldId) {
                const expand = document.createElement("button");
                expand.className = "mgr-btn mgr-btn-expand";
                expand.setAttribute('data-ui-output', '');
                const isHistoricalVersion = Number(world.active_version) < Number(world.latest_version || world.active_version);
                expand.textContent = isExpansionSelection ? "正在扩增" : (isHistoricalVersion ? "请先选择最新版本" : "选择扩增");
                expand.disabled = isExpansionSelection || isHistoricalVersion;
                if (isHistoricalVersion) { expand.title = "线性版本只允许基于最新版本扩增"; expand.setAttribute("data-ui-title", ""); }
                expand.addEventListener("click", () => selectCityForExpansion(world));
                actions?.append(expand);
            }
            const archive = document.createElement("button");
            archive.className = "mgr-btn mgr-btn-archive";
            archive.setAttribute('data-ui-output', '');
            archive.textContent = world.lifecycle_status === "archived" ? "恢复" : "归档";
            archive.addEventListener("click", () => setCityLifecycle(world, world.lifecycle_status === "archived" ? "active" : "archived"));
            const detach = document.createElement("button");
            detach.className = "mgr-btn mgr-btn-detach";
            detach.setAttribute('data-ui-output', '');
            detach.textContent = "从世界移除";
            detach.addEventListener("click", () => detachCity(world));
            actions?.append(archive, detach);
        }
        return card;
    }

    async function openVersionDialog(world) {
        try {
            const detail = await apiGet(
                `/api/world-registry/city-detail?world_id=${encodeURIComponent(world.world_id)}&city_id=${encodeURIComponent(world.city_id)}`,
            );
            const dialog = $("mgr-version-dialog");
            const options = $("mgr-version-options");
            const confirmButton = $("mgr-version-confirm");
            if (!dialog || !options || !confirmButton) return;
            const active = Number(detail.active_version) || 1;
            const versions = Array.isArray(detail.versions) ? detail.versions : [];
            if (!versions.length) {
                throw new Error("世界后端未返回城市版本数据。当前页面与后端版本不一致，请重启统一操作台后重试。");
            }
            $("mgr-version-title").innerHTML = `${escapeHtml(detail.city_name)} · <span data-ui-text>选择版本</span>`;
            options.innerHTML = "";
            for (const version of [...versions].sort((a, b) => b.version - a.version)) {
                const counts = version.counts || {};
                const label = document.createElement("label");
                label.className = "mgr-version-option";
                label.innerHTML = `
                    <span class="mgr-version-option-top"><input type="radio" name="mgr-city-version" value="${Number(version.version)}" ${Number(version.version) === active ? "checked" : ""}><span>v${Number(version.version)}</span>${Number(version.version) === active ? '<span class="mgr-version-current" data-ui-text>当前使用</span>' : ""}</span>
                    <span class="mgr-version-option-counts" data-ui-output>累计：${Number(counts.districts) || 0} 区域 · ${Number(counts.corridors) || 0} 通道 · ${Number(counts.places) || 0} POI · ${Number(counts.characters) || 0} 人物</span>`;
                options.appendChild(label);
            }
            confirmButton.onclick = async () => {
                const selected = options.querySelector('input[name="mgr-city-version"]:checked');
                if (!selected) return;
                confirmButton.disabled = true;
                try {
                    await apiPost("/api/world-registry/select-city-version", {
                        world_id: world.world_id, city_id: world.city_id, version: Number(selected.value),
                    });
                    dialog.close();
                    await loadWorldRegistry();
                    await loadWorlds();
                    document.dispatchEvent(new CustomEvent("world:changed", {
                        detail: { world_id: world.world_id, city_id: world.city_id, version: Number(selected.value) },
                    }));
                } catch (err) {
                    uiAlert((window.GeoUI?.t("切换城市版本失败: ") || "切换城市版本失败: ") + (err.message || err));
                } finally {
                    confirmButton.disabled = false;
                }
            };
            dialog.showModal();
        } catch (err) {
            uiAlert((window.GeoUI?.t("读取城市版本失败: ") || "读取城市版本失败: ") + (err.message || err));
        }
    }

    async function selectCityForExpansion(world) {
        try {
            const detail = await apiGet(
                `/api/world-registry/city-detail?world_id=${encodeURIComponent(Mgr.activeWorldId)}&city_id=${encodeURIComponent(world.city_id)}`,
            );
            if (!detail.seed || !detail.seed.city_id) {
                throw new Error("该城市没有可回填的构建 Seed，暂不能以界面参数继续扩增。");
            }
            Mgr.expansionCity = detail;
            fillFormFromSeed(detail.seed);
            setExpansionTargets(detail);
            refreshExpansionContext();
            refreshBuildContext();
            renderWorldsList();
            $("mgr-expansion-context")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
        } catch (err) {
            uiAlert((window.GeoUI?.t("选择扩增城市失败: ") || "选择扩增城市失败: ") + (err.message || err));
        }
    }

    async function setCityLifecycle(world, lifecycleStatus) {
        const action = lifecycleStatus === "archived" ? "归档" : "恢复";
        const message = `${action} ${world.city_name}？源资产、任务来源、审核和运行记录都会保留。`;
        if (!uiConfirm(window.GeoUI?.t(message) || message)) return;
        await apiPost("/api/world-registry/city-lifecycle", {
            world_id: world.world_id, city_id: world.city_id, lifecycle_status: lifecycleStatus,
        });
        await loadWorlds();
    }

    async function detachCity(world) {
        const message = `从 ${world.world_id} 移除 ${world.city_name}？源资产、任务来源、审核和运行记录都会保留。`;
        if (!uiConfirm(window.GeoUI?.t(message) || message)) return;
        await apiPost("/api/world-registry/detach-city", { world_id: world.world_id, city_id: world.city_id });
        await loadWorldRegistry();
        await loadWorlds();
    }

    async function updateCurrentWorldDisplay(serverPath) {
        const nameEl = $("mgr-current-name");
        const pathEl = $("mgr-current-path");
        if (!nameEl) return;
        Mgr.currentWorldPath = serverPath || "";
        nameEl.textContent = Mgr.activeWorldId || "未选择";
        pathEl.textContent = "";
        pathEl.title = "";
    }

    // ============================================================
    // Load world into server
    // ============================================================
    async function loadWorld(worldPath, cityName, worldId = "", cityId = "") {
        const btn = document.querySelector(`.mgr-btn-load[data-world-path="${CSS.escape(worldPath)}"]`);
        if (btn) {
            btn.disabled = true;
            btn.textContent = "加载中...";
        }
        try {
            if (worldId && cityId) {
                await apiPost("/api/worlds/open", { world_id: worldId });
                await apiPost("/api/worlds/select-scope", { world_id: worldId, scope: "city", city_id: cityId });
            } else {
                await apiPost("/api/worlds/load", { world_path: worldPath });
            }
            window.location.href = worldStudioMapUrl();
        } catch (err) {
            uiAlert((window.GeoUI?.t("加载世界失败: ") || "加载世界失败: ") + (err.message || err));
            if (btn) {
                btn.disabled = false;
                btn.textContent = "加载到地图";
            }
        }
    }

    async function deleteWorld(manifestPath, cityName) {
        if (!uiConfirm(`确定要删除「${cityName}」的已生成世界吗？\n此操作不可恢复，将删除整个世界输出目录。`)) return;
        const btn = document.querySelector(`.mgr-btn-delete[data-manifest-path="${CSS.escape(manifestPath)}"]`);
        if (btn) {
            btn.disabled = true;
            btn.textContent = "删除中...";
        }
        try {
            await apiPost("/api/worlds/delete", { manifest_path: manifestPath });
            await loadWorlds();
        } catch (err) {
            uiAlert((window.GeoUI?.t("删除失败: ") || "删除失败: ") + (err.message || err));
            if (btn) {
                btn.disabled = false;
                btn.textContent = "删除";
            }
        }
    }

    // ============================================================
    // Generation: save seed → start generation → poll
    // ============================================================
    async function startGeneration() {
        const seed = collectSeed();
        const targets = expansionTargets();
        const errors = validateSeed(seed);
        if (targets.characters < 1) errors.push("人物目标必须至少为 1");
        if (errors.length) {
            uiAlert("表单校验失败:\n\n" + errors.map((e) => "  • " + e).join("\n"));
            return;
        }

        const geoProvider = document.querySelector('input[name="mgr-geo"]:checked')?.value || "auto";
        const worldId = Mgr.activeWorldId;
        if (!worldId) {
            uiAlert("请先新建或选择一个目标世界。");
            return;
        }
        if (Mgr.expansionCity && seed.city_id !== Mgr.expansionCity.city_id) {
            uiAlert("扩增模式必须保留已登记城市的城市 ID。请重新选择该城市后再构建。");
            return;
        }

        const genBtn = $("mgr-btn-generate");
        const genPanel = $("mgr-gen-panel");
        const genStatus = $("mgr-gen-status");
        const genOutput = $("mgr-gen-output");
        const genElapsed = $("mgr-gen-elapsed");

        genBtn.disabled = true;
        genBtn.textContent = Mgr.expansionCity ? `正在扩增 ${seed.city_name}...` : `正在构建 ${seed.city_name}...`;
        genPanel.classList.remove("hidden");
        $("mgr-btn-cancel-job").disabled = false;
        $("mgr-btn-retry-reuse").disabled = true;
        $("mgr-btn-retry-refresh").disabled = true;
        $("mgr-btn-register-actual").disabled = true;
        $("mgr-gen-recovery-plan").textContent = "";
        genStatus.textContent = "正在保存 Seed...";
        genStatus.className = "mgr-gen-status mgr-gen-running";
        genOutput.textContent = "";

        try {
            // Step 1: Save seed
            const saveResult = await apiPost("/api/seeds/save", seed);
            Mgr.lastGeneratedCityId = seed.city_id;
            genOutput.textContent = `✓ Seed 已保存: ${saveResult.seed_path}\n${Mgr.expansionCity ? `✓ 增量目标: ${worldId} / ${Mgr.expansionCity.city_name}\n` : ""}\n`;

            // Step 2: Start generation
            genStatus.textContent = "正在启动生成进程...";
            const genResult = await apiPost("/api/generate", {
                seed: saveResult.seed_path,
                geo_provider: geoProvider,
                world_id: worldId,
                expansion_city_id: Mgr.expansionCity?.city_id || "",
                expansion_targets: Mgr.expansionCity ? expansionTargets() : null,
                generation_targets: targets,
            });
            Mgr.currentJobId = genResult.job_id;
            genOutput.textContent += `$ python -m world_module.map.generator --seed ${saveResult.seed_path} --geo-provider ${geoProvider}\n\n`;
            // Scroll gen panel into view
            genPanel.scrollIntoView({ behavior: "smooth", block: "nearest" });
            pollJobStatus();
        } catch (err) {
            genStatus.textContent = "启动失败: " + (err.message || err);
            genStatus.className = "mgr-gen-status mgr-gen-failed";
            genBtn.disabled = false;
            genBtn.textContent = generationActionLabel();
        }
    }

    function pollJobStatus() {
        if (!Mgr.currentJobId) return;
        if (Mgr.pollTimer) clearTimeout(Mgr.pollTimer);

        const poll = async () => {
            try {
                const job = await apiGet(`/api/jobs/${Mgr.currentJobId}`);
                updateGenUI(job);
                if (job.status === "running" || job.status === "cancelling") {
                    Mgr.pollTimer = setTimeout(poll, 1500);
                } else {
                    onGenerationDone(job);
                }
            } catch (err) {
                console.error("Poll failed:", err);
                Mgr.pollTimer = setTimeout(poll, 3000);
            }
        };
        poll();
    }

    function renderGenerationProgress(job) {
        const stages = job.progress?.stages || [];
        const current = job.progress?.current || {};
        const stageList = $("mgr-gen-stage-list");
        const currentStep = $("mgr-gen-current-step");
        const subprogress = $("mgr-gen-subprogress");
        const subprogressFill = $("mgr-gen-subprogress-fill");
        if (!stageList || !currentStep || !subprogress || !subprogressFill) return;
        stageList.innerHTML = "";
        for (const stage of stages) {
            const item = document.createElement("div");
            item.className = `mgr-gen-stage ${stage.status || "pending"}`;
            item.setAttribute('data-ui-output', '');
            item.textContent = stage.label || stage.id;
            stageList.appendChild(item);
        }
        const countText = Number.isFinite(current.completed) && Number.isFinite(current.total) && current.total > 0
            ? ` · ${current.completed}/${current.total}` : "";
        currentStep.innerHTML = `<span data-ui-text>${escapeHtml(current.stage_label || "构建")}</span> · <span data-ui-text>${escapeHtml(current.label || "等待状态更新")}</span>${countText}`;
        if (Number.isFinite(current.completed) && Number.isFinite(current.total) && current.total > 0) {
            subprogress.classList.remove("hidden");
            subprogressFill.style.width = `${Math.min(100, Math.max(0, current.completed / current.total * 100))}%`;
        } else {
            subprogress.classList.add("hidden");
            subprogressFill.style.width = "0";
        }
    }

    async function restoreLatestJob() {
        if (!Mgr.activeWorldId) return;
        try {
            const data = await apiGet(`/api/jobs/latest?world_id=${encodeURIComponent(Mgr.activeWorldId)}`);
            const job = data.job;
            if (!job?.id) return;
            Mgr.currentJobId = job.id;
            $("mgr-gen-panel").classList.remove("hidden");
            updateGenUI(job);
            if (job.status === "running" || job.status === "cancelling") pollJobStatus();
        } catch (err) {
            console.warn("Unable to restore the latest build job:", err);
        }
    }

    function updateGenUI(job) {
        const genStatus = $("mgr-gen-status");
        const genOutput = $("mgr-gen-output");
        const genElapsed = $("mgr-gen-elapsed");
        const genBtn = $("mgr-btn-generate");
        const cancelButton = $("mgr-btn-cancel-job");
        const continueButton = $("mgr-btn-retry-reuse");
        const rebuildButton = $("mgr-btn-retry-refresh");
        const registerActualButton = $("mgr-btn-register-actual");
        const recoveryPlan = $("mgr-gen-recovery-plan");
        genBtn.setAttribute("data-ui-output", "");
        recoveryPlan.setAttribute("data-ui-output", "");
        const elapsed = job.elapsed_s || 0;
        genElapsed.textContent = `${elapsed.toFixed(0)}s`;
        renderGenerationProgress(job);

        const tail = job.output_tail || [];
        // Preserve the header lines (seed save + command), only replace the tail output
        const headerLines = genOutput.textContent.split("\n\n").slice(0, 2).join("\n\n") + "\n\n";
        genOutput.textContent = headerLines + tail.join("\n");
        genOutput.scrollTop = genOutput.scrollHeight;

        if (job.status === "running" || job.status === "cancelling") {
            cancelButton.disabled = job.status === "cancelling";
            cancelButton.textContent = job.status === "cancelling" ? "正在停止..." : "停止本次构建";
            continueButton.disabled = true;
            rebuildButton.disabled = true;
            registerActualButton.disabled = true;
            const autoRepair = job.automatic_repair_state || {};
            recoveryPlan.textContent = autoRepair.status === "running"
                ? `自动补齐 ${autoRepair.round || 1}/${autoRepair.max_rounds || 1}\n当前缺口：${formatGapText(autoRepair.target_gaps || {})}`
                : "";
            genStatus.innerHTML = job.status === "cancelling"
                ? '<span data-ui-text>正在停止本次构建...</span>'
                : `<span data-ui-text>运行中</span> · <span data-ui-text>${escapeHtml(job.progress?.current?.stage_label || "正在准备")}</span>`;
            genStatus.className = "mgr-gen-status mgr-gen-running";
            genBtn.textContent = `正在构建 ${job.city_name || val("mgr-city-name") || "城市"}...`;
            genBtn.disabled = true;
        } else if (job.status === "completed") {
            continueButton.textContent = "继续构建";
            cancelButton.disabled = true;
            continueButton.disabled = true;
            rebuildButton.disabled = true;
            registerActualButton.disabled = true;
            recoveryPlan.textContent = "";
            const registration = job.registration || {};
            const registrationText = registration.status === "attached"
                ? ` · 已登记到 ${registration.world_id}`
                : registration.status === "attached_with_shortfall"
                    ? ` · 已按实际产物登记到 ${registration.world_id}`
                : registration.status === "failed"
                    ? " · 世界登记失败"
                    : "";
            genStatus.textContent = `完成 · 耗时 ${elapsed.toFixed(0)}s${registrationText}`;
            genStatus.className = "mgr-gen-status mgr-gen-completed";
            genBtn.textContent = generationActionLabel();
            genBtn.disabled = false;
        } else if (job.status === "cancelled") {
            continueButton.textContent = "继续构建";
            cancelButton.disabled = true;
            continueButton.disabled = false;
            rebuildButton.disabled = false;
            registerActualButton.disabled = true;
            recoveryPlan.textContent = recoveryPlanText(job.resume_plan);
            genStatus.textContent = `已停止 · 耗时 ${elapsed.toFixed(0)}s · 未登记本次候选版本`;
            genStatus.className = "mgr-gen-status mgr-gen-cancelled";
            genBtn.textContent = generationActionLabel();
            genBtn.disabled = false;
        } else if (job.status === "awaiting_registration_decision") {
            cancelButton.disabled = true;
            continueButton.disabled = false;
            rebuildButton.disabled = false;
            const report = job.gate_report || {};
            registerActualButton.disabled = !report.can_register_actual;
            const target = report.requested_targets || {};
            const actual = report.actual_counts || {};
            const metricLabels = { districts: "区域", corridors: "通道", places: "POI", characters: "人物" };
            const reasonLabels = {
                duplicate_character_identity_removed: "人物身份重复，已去重",
                generated_candidate_below_requested_target: "生成结果未达到设定目标",
            };
            const gaps = Object.fromEntries(Object.entries(report.target_gaps || {}).map(
                ([metric, value]) => [metricLabels[metric] || metric, value],
            ));
            const repairable = (report.repairable_gaps || []).map((item) => ({
                ...item,
                metric: metricLabels[item.metric] || item.metric,
                reason: reasonLabels[item.reason] || item.reason,
            }));
            const formatCounts = (counts) => `区域 ${counts.districts || 0} · 通道 ${counts.corridors || 0} · POI ${counts.places || 0} · 人物 ${counts.characters || 0}`;
            const gapText = Object.entries(gaps).map(([name, value]) => `${name} 少 ${value}`).join("；");
            const autoRepair = job.automatic_repair_state || {};
            const exhausted = autoRepair.status === "exhausted";
            continueButton.textContent = exhausted ? "继续补齐" : "继续构建";
            const repairText = exhausted
                ? `自动补齐已尝试 ${autoRepair.completed_rounds || 0}/${autoRepair.max_rounds || 0} 轮，仍未达到目标。`
                : "候选已完成，待补齐或登记。";
            genStatus.textContent = repairText;
            genStatus.className = "mgr-gen-status mgr-gen-cancelled";
            const reasonText = repairable.map((item) => `${item.metric}: ${item.reason}`).join("；");
            recoveryPlan.textContent = `${repairText}\n目标：${formatCounts(target)}\n实际：${formatCounts(actual)}\n差异：${gapText || "无"}${reasonText ? `\n原因：${reasonText}` : ""}`;
            genBtn.textContent = generationActionLabel();
            genBtn.disabled = false;
        } else {
            continueButton.textContent = "继续构建";
            cancelButton.disabled = true;
            registerActualButton.disabled = true;
            const errMsg = job.failure_detail || job.error || `退出码 ${job.returncode}`;
            const failedStage = job.progress?.current?.stage_label || "构建";
            genStatus.innerHTML = `<span data-ui-text>${job.status === "partial" ? "部分完成" : "失败"}</span> · <span data-ui-text>${escapeHtml(failedStage)}</span> · <span data-ui-output>${escapeHtml(errMsg)}</span>`;
            genStatus.className = "mgr-gen-status mgr-gen-failed";
            genBtn.textContent = generationActionLabel();
            genBtn.disabled = false;
            const canRecover = ["failed", "target_not_reached"].includes(job.status);
            const structuralReport = job.structural_repair_report || {};
            if (structuralReport.remaining_gaps) continueButton.textContent = "\u7ee7\u7eed\u8865\u9f50";
            continueButton.disabled = !canRecover;
            rebuildButton.disabled = !canRecover;
            recoveryPlan.setAttribute("data-ui-output", "");
            recoveryPlan.textContent = canRecover
                ? `${structuralRepairText(structuralReport)}${structuralReport.remaining_gaps ? "\n" : ""}${recoveryPlanText(job.resume_plan)}`
                : "";
        }
    }

    function formatGapText(gaps) {
        const labels = { districts: "区域", corridors: "通道", places: "POI", characters: "人物" };
        const entries = Object.entries(gaps || {}).filter(([, value]) => Number(value) > 0);
        return entries.length ? entries.map(([key, value]) => `${labels[key] || key} 少 ${value}`).join("；") : "无";
    }

    function recoveryPlanText(plan) {
        const translate = text => window.GeoUI?.t(text) || text;
        if (!plan?.label) return translate("继续构建将自动选择最靠后的有效检查点。");
        const reused = Array.isArray(plan.reused) && plan.reused.length
            ? translate("复用：") + (window.GeoUI?.language === "en" ? " " : "") + plan.reused.map(translate).join(window.GeoUI?.language === "en" ? ", " : "、")
            : translate("不复用已完成产物");
        return translate("继续构建：") + translate(plan.label) + (window.GeoUI?.language === "en" ? "; " : "，") + reused;
    }

    function structuralRepairText(report) {
        const gaps = report?.remaining_gaps || {};
        const entries = Object.entries(gaps).map(([kind, values]) => {
            const label = kind === "districts" ? "\u533a\u57df" : kind === "corridors" ? "\u901a\u9053" : kind;
            return Array.isArray(values) && values.length === 2 ? `${label} ${values[0]}/${values[1]}` : "";
        }).filter(Boolean);
        if (!entries.length) return "";
        const rounds = Array.isArray(report.rounds) ? report.rounds.length : 0;
        return `\u57ce\u5e02\u7ed3\u6784\u672a\u8fbe\u5230\u76ee\u6807\n${entries.join(" \u00b7 ")}\n\u5df2\u81ea\u52a8\u8865\u9f50 ${rounds}/${report.max_automatic_rounds || rounds} \u8f6e`;
    }

    async function cancelCurrentJob() {
        if (!Mgr.currentJobId) return;
        const message = "停止本次构建？已发生的 API 调用不会撤回；已完成的中间产物和日志会保留，但不会登记或启用本次候选版本。";
        if (!uiConfirm(window.GeoUI?.t(message) || message)) return;
        const cancelButton = $("mgr-btn-cancel-job");
        cancelButton.disabled = true;
        cancelButton.textContent = "正在停止...";
        try {
            await apiPost(`/api/jobs/${encodeURIComponent(Mgr.currentJobId)}/cancel`, {});
            pollJobStatus();
        } catch (err) {
            cancelButton.disabled = false;
            cancelButton.textContent = "停止本次构建";
            uiAlert("停止构建失败: " + (err.message || err));
        }
    }

    async function retryBuild(retryMode) {
        const reuseButton = $("mgr-btn-retry-reuse");
        const refreshButton = $("mgr-btn-retry-refresh");
        const genStatus = $("mgr-gen-status");
        if (!Mgr.currentJobId) return;
        if (retryMode === "rebuild" && !uiConfirm("重新构建会从城市发现开始，不沿用本次中间结果，并产生新的 LLM/Google API 成本。继续吗？")) return;
        reuseButton.disabled = true;
        refreshButton.disabled = true;
        genStatus.textContent = retryMode === "rebuild" ? "正在重新构建..." : "正在继续构建...";
        genStatus.className = "mgr-gen-status mgr-gen-running";
        try {
            const result = await apiPost(`/api/jobs/${encodeURIComponent(Mgr.currentJobId)}/retry`, {
                retry_mode: retryMode,
            });
            Mgr.currentJobId = result.job_id;
            $("mgr-gen-output").textContent += `\n\n✓ 已启动${retryMode === "rebuild" ? "重新构建" : "继续构建"}。`;
            pollJobStatus();
        } catch (err) {
            genStatus.textContent = "构建启动失败: " + (err.message || err);
            genStatus.className = "mgr-gen-status mgr-gen-failed";
            reuseButton.disabled = false;
            refreshButton.disabled = false;
        }
    }

    async function registerActualCandidate() {
        if (!Mgr.currentJobId) return;
        const confirmed = uiConfirm(
            "将按候选的真实数量登记并启用该版本。原始目标、实际数量、缺口、确认时间和本次构建账本会永久记录；后续任务与测试只读取真实登记的数据。继续吗？",
        );
        if (!confirmed) return;
        const button = $("mgr-btn-register-actual");
        button.disabled = true;
        try {
            const result = await apiPost(`/api/jobs/${encodeURIComponent(Mgr.currentJobId)}/register-actual`, { confirm: true });
            Mgr.currentJobId = result.job_id;
            $("mgr-gen-status").textContent = "正在按实际产物登记...";
            $("mgr-gen-status").className = "mgr-gen-status mgr-gen-running";
            pollJobStatus();
        } catch (err) {
            uiAlert("按实际产物登记失败: " + (err.message || err));
            button.disabled = false;
        }
    }

    async function onGenerationDone(job) {
        if (job.status === "completed") {
            await loadWorlds();
            const genOutput = $("mgr-gen-output");
            const registration = job.registration || {};
            genOutput.textContent += registration.status === "attached" || registration.status === "attached_with_shortfall"
                ? `\n\n✓ 城市构建完成，已登记到 ${registration.world_id}。右侧已刷新城市列表，可点击「加载到地图」查看。`
                : `\n\n✓ 城市构建完成，但世界登记未完成：${registration.error || "未请求"}`;
            // Keep "刚刚生成" highlight for a while, then clear
            setTimeout(() => {
                Mgr.lastGeneratedCityId = null;
                document.querySelectorAll(".mgr-world-new").forEach((el) => {
                    el.classList.remove("mgr-world-new");
                    el.querySelector(".mgr-badge-new")?.remove();
                });
            }, 20000);
        }
    }

    // ============================================================
    // Init
    // ============================================================
    let mapReady = false;

    async function init() {
        if (Mgr.initialized) {
            loadWorlds();
            // Tab re-activated: init map on first visit, else just resize
            if (!mapReady) {
                setTimeout(() => {
                    initPreviewMap();
                    mapReady = true;
                    updatePreviewMap();
                }, 120);
            } else {
                setTimeout(() => {
                    if (previewMap) previewMap.invalidateSize();
                    updatePreviewMap();
                }, 80);
            }
            return;
        }
        Mgr.initialized = true;
        await loadPlaceTypeCatalog();
        bindForm();
        updateSliderLabels();
        updateAutoBbox();
        updateTotalPlaces();
        await loadWorldRegistry();
        await loadWorlds();
        await restoreLatestJob();
        // Map init deferred to first tab activation
    }

    window.WorldManager = { init };

    document.addEventListener("DOMContentLoaded", () => {
        init().catch((err) => console.error("WorldManager init failed:", err));
    });
})();
