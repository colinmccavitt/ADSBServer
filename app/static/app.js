// ADS-B Tracker - Map & WebSocket client

const aircraft = {};       // icao -> { data, marker, label, trail }
let selectedIcao = null;
let ws = null;

// --- Plane SVG icon ---
function createPlaneIcon(heading, altitude) {
    const rotation = heading != null ? heading : 0;
    // Color by altitude: ground=green, low=cyan, mid=yellow, high=orange, very high=red
    let color = "#60a5fa"; // default blue
    if (altitude != null) {
        if (altitude <= 0)       color = "#34d399";
        else if (altitude < 5000)  color = "#22d3ee";
        else if (altitude < 15000) color = "#fbbf24";
        else if (altitude < 30000) color = "#f97316";
        else                       color = "#ef4444";
    }

    const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="28" height="28" style="transform:rotate(${rotation}deg)">
        <path d="M12 2 L10 9 L3 12 L10 13 L9 21 L12 18 L15 21 L14 13 L21 12 L14 9 Z"
              fill="${color}" stroke="#0f172a" stroke-width="0.8"/>
    </svg>`;

    return L.divIcon({
        html: svg,
        iconSize: [28, 28],
        iconAnchor: [14, 14],
        className: ""
    });
}

// --- Map setup ---
const map = L.map("map", {
    center: [38.85596, -77.04952],
    zoom: 11,
    zoomControl: true,
    attributionControl: true,
});

// Dark map tiles
L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a> &copy; <a href="https://carto.com/">CARTO</a>',
    subdomains: "abcd",
    maxZoom: 19,
}).addTo(map);

// Map will be recentered when config loads from the server

// --- Aircraft management ---
function updateAircraft(data) {
    const icao = data.icao;
    let entry = aircraft[icao];

    if (!entry) {
        entry = { data: data, marker: null, label: null, trail: [] };
        aircraft[icao] = entry;
    } else {
        // Merge fields (keep old values where new ones are null)
        for (const key of Object.keys(data)) {
            if (data[key] != null) {
                entry.data[key] = data[key];
            }
        }
    }

    const d = entry.data;

    // Update or create map marker if we have a position
    if (d.latitude != null && d.longitude != null) {
        const latLng = [d.latitude, d.longitude];

        // Track trail
        entry.trail.push(latLng);
        if (entry.trail.length > 100) entry.trail.shift();

        if (entry.marker) {
            entry.marker.setLatLng(latLng);
            entry.marker.setIcon(createPlaneIcon(d.track, d.altitude));
        } else {
            entry.marker = L.marker(latLng, {
                icon: createPlaneIcon(d.track, d.altitude),
                zIndexOffset: d.altitude || 0,
            }).addTo(map);

            entry.marker.on("click", () => selectAircraft(icao));
        }

        // Update label
        const labelText = d.callsign || icao;
        if (entry.label) {
            entry.label.setLatLng(latLng);
            entry.label.getElement().textContent = labelText;
        } else {
            entry.label = L.marker(latLng, {
                icon: L.divIcon({
                    html: labelText,
                    className: "aircraft-label",
                    iconSize: null,
                    iconAnchor: [-16, 8],
                }),
                interactive: false,
            }).addTo(map);
        }

        // Draw trail if this aircraft is selected
        if (icao === selectedIcao && entry.trail.length > 1) {
            if (entry.trailLine) entry.trailLine.setLatLngs(entry.trail);
            else {
                entry.trailLine = L.polyline(entry.trail, {
                    color: "#3b82f6",
                    weight: 2,
                    opacity: 0.6,
                    dashArray: "6 4",
                }).addTo(map);
            }
        }
    }

    // Update sidebar list
    updateSidebarItem(icao, d);

    // Update detail panel if selected
    if (icao === selectedIcao) {
        updateDetailPanel(d);
    }
}

function removeAircraft(icao) {
    const entry = aircraft[icao];
    if (!entry) return;

    if (entry.marker) map.removeLayer(entry.marker);
    if (entry.label) map.removeLayer(entry.label);
    if (entry.trailLine) map.removeLayer(entry.trailLine);
    delete aircraft[icao];

    // Remove sidebar item
    const el = document.getElementById("ac-" + icao);
    if (el) el.remove();

    // Close detail panel if it was showing this aircraft
    if (icao === selectedIcao) {
        selectedIcao = null;
        document.getElementById("detail-panel").classList.add("hidden");
    }

    // Update local count immediately for removes (server stats will catch up)
    document.getElementById("stat-aircraft").textContent =
        Object.keys(aircraft).length + " aircraft";
}

// --- Sidebar ---
function updateSidebarItem(icao, d) {
    let el = document.getElementById("ac-" + icao);
    if (!el) {
        el = document.createElement("div");
        el.id = "ac-" + icao;
        el.className = "ac-item";
        el.onclick = () => selectAircraft(icao);
        document.getElementById("aircraft-list").appendChild(el);
    }

    if (icao === selectedIcao) el.classList.add("selected");
    else el.classList.remove("selected");

    const callsign = d.callsign || "------";
    const alt = d.altitude != null ? d.altitude.toLocaleString() + " ft" : "---";
    const speed = d.ground_speed != null ? Math.round(d.ground_speed) + " kt" : "";
    const typeStr = d.aircraft_type ? ` · ${d.aircraft_type}` : "";
    const regStr = d.registration || icao;

    el.innerHTML = `
        <div class="ac-item-left">
            <div class="ac-callsign">${callsign}${typeStr}</div>
            <div class="ac-icao">${regStr}${d.registration ? ' · ' + icao : ''}</div>
        </div>
        <div class="ac-item-right">
            <div class="ac-alt">${alt}</div>
            <div>${speed}</div>
        </div>
    `;
}


// --- Selection / Detail ---
function selectAircraft(icao) {
    // Deselect old
    if (selectedIcao) {
        const oldEl = document.getElementById("ac-" + selectedIcao);
        if (oldEl) oldEl.classList.remove("selected");
        const oldEntry = aircraft[selectedIcao];
        if (oldEntry && oldEntry.trailLine) {
            map.removeLayer(oldEntry.trailLine);
            oldEntry.trailLine = null;
        }
    }

    selectedIcao = icao;
    const entry = aircraft[icao];
    if (!entry) return;

    // Highlight in sidebar
    const el = document.getElementById("ac-" + icao);
    if (el) el.classList.add("selected");

    // Pan map to aircraft
    if (entry.data.latitude != null && entry.data.longitude != null) {
        map.panTo([entry.data.latitude, entry.data.longitude]);
    }

    // Draw trail
    if (entry.trail.length > 1) {
        entry.trailLine = L.polyline(entry.trail, {
            color: "#3b82f6",
            weight: 2,
            opacity: 0.6,
            dashArray: "6 4",
        }).addTo(map);
    }

    updateDetailPanel(entry.data);
    document.getElementById("detail-panel").classList.remove("hidden");
}

function updateDetailPanel(d) {
    document.getElementById("detail-callsign").textContent = d.callsign || "Unknown";
    document.getElementById("detail-icao").textContent = d.icao;

    // Aircraft info section — show only if we have enrichment data
    const hasInfo = d.registration || d.aircraft_type || d.aircraft_model ||
                    d.manufacturer || d.owner_operator || d.year_built;
    document.getElementById("detail-info-title").style.display = hasInfo ? "" : "none";
    document.getElementById("detail-info-grid").style.display = hasInfo ? "" : "none";

    document.getElementById("detail-reg").textContent = d.registration || "---";
    document.getElementById("detail-type").textContent = d.aircraft_type || "---";
    document.getElementById("detail-model").textContent = d.aircraft_model || "---";
    document.getElementById("detail-operator").textContent = d.owner_operator || "---";
    document.getElementById("detail-manufacturer").textContent = d.manufacturer || "---";
    document.getElementById("detail-year").textContent = d.year_built || "---";

    if (d.is_military) {
        document.getElementById("detail-icao").textContent = d.icao + " (Military)";
    }

    // Flight data
    let altText = d.altitude != null ? d.altitude.toLocaleString() + " ft" : "---";
    if (d.alt_geom != null && d.altitude != null) {
        altText += "  (WGS84 " + d.alt_geom.toLocaleString() + " ft)";
    }
    document.getElementById("detail-alt").textContent = altText;
    document.getElementById("detail-speed").textContent =
        d.ground_speed != null ? Math.round(d.ground_speed) + " kt" : "---";
    document.getElementById("detail-heading").textContent =
        d.track != null ? Math.round(d.track) + "\u00B0" : "---";
    document.getElementById("detail-vrate").textContent =
        d.vertical_rate != null ? (d.vertical_rate > 0 ? "+" : "") + d.vertical_rate + " ft/m" : "---";
    document.getElementById("detail-squawk").textContent = d.squawk || "---";
    document.getElementById("detail-ground").textContent =
        d.on_ground != null ? (d.on_ground ? "Yes" : "No") : "---";
    document.getElementById("detail-msgs").textContent =
        d.message_count != null ? d.message_count.toLocaleString() : "---";

    if (d.last_seen) {
        const dt = new Date(d.last_seen);
        document.getElementById("detail-lastseen").textContent = dt.toLocaleTimeString();
    } else {
        document.getElementById("detail-lastseen").textContent = "---";
    }

    // Show source collectors if available (server mode)
    const srcSection = document.getElementById("detail-collectors-section");
    if (d.source_collectors && d.source_collectors.length > 0) {
        srcSection.style.display = "";
        document.getElementById("detail-sources").textContent = d.source_collectors.join(", ");
    } else {
        srcSection.style.display = "none";
    }
}

// Close detail panel
document.getElementById("detail-close").addEventListener("click", () => {
    document.getElementById("detail-panel").classList.add("hidden");
    if (selectedIcao) {
        const el = document.getElementById("ac-" + selectedIcao);
        if (el) el.classList.remove("selected");
        const entry = aircraft[selectedIcao];
        if (entry && entry.trailLine) {
            map.removeLayer(entry.trailLine);
            entry.trailLine = null;
        }
        selectedIcao = null;
    }
});

// --- Stats (pushed via WebSocket) ---
function updateStats(stats) {
    document.getElementById("stat-msgs").textContent = stats.messages_per_second + " msg/s";
    document.getElementById("stat-total").textContent = stats.messages_total.toLocaleString() + " msgs";
    document.getElementById("stat-aircraft").textContent = stats.aircraft_count + " aircraft";
}

// --- WebSocket ---
function connectWebSocket() {
    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(protocol + "//" + location.host + "/ws");

    ws.onopen = () => {
        const statusEl = document.getElementById("stat-status");
        statusEl.textContent = "Connected";
        statusEl.className = "status-connected";
    };

    ws.onmessage = (event) => {
        const msg = JSON.parse(event.data);
        if (msg.type === "update") {
            if (msg.aircraft) updateAircraft(msg.aircraft);
            if (msg.stats) updateStats(msg.stats);
        } else if (msg.type === "remove" && msg.icao) {
            removeAircraft(msg.icao);
        } else if (msg.type === "autogain") {
            handleAutoGainProgress(msg);
        }
    };

    ws.onclose = () => {
        const statusEl = document.getElementById("stat-status");
        statusEl.textContent = "Disconnected";
        statusEl.className = "status-disconnected";
        // Reconnect after 3 seconds
        setTimeout(connectWebSocket, 3000);
    };

    ws.onerror = () => {
        ws.close();
    };
}

// --- Config panel ---
let receiverMarker = null;

document.getElementById("config-toggle").addEventListener("click", () => {
    const panel = document.getElementById("config-panel");
    const btn = document.getElementById("config-toggle");
    panel.classList.toggle("config-hidden");
    btn.classList.toggle("active");
});

async function loadConfig() {
    try {
        const res = await fetch("/api/config");
        const cfg = await res.json();

        document.getElementById("cfg-lat").value = cfg.latitude;
        document.getElementById("cfg-lon").value = cfg.longitude;

        // Populate gain dropdown
        const sel = document.getElementById("cfg-gain");
        sel.innerHTML = "";
        for (const g of cfg.supported_gains) {
            const opt = document.createElement("option");
            opt.value = g;
            opt.textContent = g + " dB";
            if (Math.abs(g - cfg.gain) < 0.01) opt.selected = true;
            sel.appendChild(opt);
        }

        // Center map on receiver and place marker
        map.setView([cfg.latitude, cfg.longitude], map.getZoom());
        updateReceiverMarker(cfg.latitude, cfg.longitude);
    } catch (e) {
        console.error("Failed to load config:", e);
    }
}

function updateReceiverMarker(lat, lon) {
    const icon = L.divIcon({
        html: `<svg width="18" height="18" viewBox="0 0 18 18">
            <circle cx="9" cy="9" r="7" fill="#3b82f6" fill-opacity="0.3" stroke="#3b82f6" stroke-width="2"/>
            <circle cx="9" cy="9" r="3" fill="#3b82f6"/>
        </svg>`,
        iconSize: [18, 18],
        iconAnchor: [9, 9],
        className: "",
    });

    if (receiverMarker) {
        receiverMarker.setLatLng([lat, lon]);
    } else {
        receiverMarker = L.marker([lat, lon], { icon, interactive: false, zIndexOffset: -1000 }).addTo(map);
    }
}

document.getElementById("cfg-save").addEventListener("click", async () => {
    const lat = parseFloat(document.getElementById("cfg-lat").value);
    const lon = parseFloat(document.getElementById("cfg-lon").value);
    const gain = parseFloat(document.getElementById("cfg-gain").value);
    const statusEl = document.getElementById("cfg-status");

    if (isNaN(lat) || isNaN(lon) || lat < -90 || lat > 90 || lon < -180 || lon > 180) {
        statusEl.textContent = "Invalid coordinates";
        statusEl.className = "error";
        return;
    }

    statusEl.textContent = "Saving...";
    statusEl.className = "";

    try {
        const res = await fetch("/api/config", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ latitude: lat, longitude: lon, gain: gain }),
        });

        if (!res.ok) throw new Error("Server returned " + res.status);
        const cfg = await res.json();

        statusEl.textContent = "Saved";
        statusEl.className = "success";

        // Recenter map and update marker
        map.setView([cfg.latitude, cfg.longitude], map.getZoom());
        updateReceiverMarker(cfg.latitude, cfg.longitude);

        setTimeout(() => { statusEl.textContent = ""; }, 3000);
    } catch (e) {
        statusEl.textContent = "Error: " + e.message;
        statusEl.className = "error";
    }
});

// --- Auto Gain ---
let autoGainRunning = false;

function handleAutoGainProgress(msg) {
    const container = document.getElementById("autogain-results");
    container.classList.remove("autogain-hidden");

    if (msg.phase === "testing") {
        const results = msg.results || [];
        const maxMsgs = Math.max(1, ...results.map(r => r.messages));

        let html = `<div class="autogain-progress">Testing gain ${msg.step}/${msg.total_steps}: <b>${msg.gain} dB</b>...</div>`;

        // Bars for completed results
        for (const r of results) {
            const pct = Math.round((r.messages / maxMsgs) * 100);
            html += `<div class="autogain-bar-row">
                <div class="autogain-bar-label">${r.gain} dB</div>
                <div class="autogain-bar-track"><div class="autogain-bar-fill" style="width:${pct}%"></div></div>
                <div class="autogain-bar-value">${r.messages} msgs</div>
            </div>`;
        }

        // Pulsing bar for current test
        html += `<div class="autogain-bar-row">
            <div class="autogain-bar-label">${msg.gain} dB</div>
            <div class="autogain-bar-track"><div class="autogain-bar-fill testing" style="width:100%"></div></div>
            <div class="autogain-bar-value">...</div>
        </div>`;

        container.innerHTML = html;

    } else if (msg.phase === "done") {
        const results = msg.results || [];
        const bestGain = msg.best_gain;
        const maxMsgs = Math.max(1, ...results.map(r => r.messages));

        let html = `<div class="autogain-progress">Best gain: <b>${bestGain} dB</b></div>`;

        for (const r of results) {
            const pct = Math.round((r.messages / maxMsgs) * 100);
            const isBest = Math.abs(r.gain - bestGain) < 0.01;
            html += `<div class="autogain-bar-row">
                <div class="autogain-bar-label">${r.gain} dB</div>
                <div class="autogain-bar-track"><div class="autogain-bar-fill${isBest ? " best" : ""}" style="width:${pct}%"></div></div>
                <div class="autogain-bar-value${isBest ? " best" : ""}">${r.messages} msgs</div>
            </div>`;
        }

        container.innerHTML = html;

        // Update the gain dropdown to the new best gain
        const sel = document.getElementById("cfg-gain");
        for (const opt of sel.options) {
            opt.selected = Math.abs(parseFloat(opt.value) - bestGain) < 0.01;
        }

        autoGainRunning = false;
        document.getElementById("cfg-autogain").disabled = false;
        document.getElementById("cfg-save").disabled = false;
        document.getElementById("cfg-autogain").textContent = "Auto Gain";
        document.getElementById("cfg-status").textContent = "Gain set to " + bestGain + " dB";
        document.getElementById("cfg-status").className = "success";
        setTimeout(() => {
            document.getElementById("cfg-status").textContent = "";
            document.getElementById("cfg-status").className = "";
        }, 5000);
    }
}

document.getElementById("cfg-autogain").addEventListener("click", async () => {
    if (autoGainRunning) return;
    autoGainRunning = true;

    const btn = document.getElementById("cfg-autogain");
    btn.disabled = true;
    btn.textContent = "Testing...";
    document.getElementById("cfg-save").disabled = true;
    document.getElementById("cfg-status").textContent = "";

    // Open config panel if not already open
    document.getElementById("config-panel").classList.remove("config-hidden");
    document.getElementById("config-toggle").classList.add("active");

    try {
        const res = await fetch("/api/autogain", { method: "POST" });
        if (!res.ok) throw new Error("Server returned " + res.status);
    } catch (e) {
        document.getElementById("cfg-status").textContent = "Error: " + e.message;
        document.getElementById("cfg-status").className = "error";
        autoGainRunning = false;
        btn.disabled = false;
        btn.textContent = "Auto Gain";
        document.getElementById("cfg-save").disabled = false;
    }
});

// --- Aircraft Database Status ---
async function loadDbStatus() {
    try {
        const res = await fetch("/api/db/status");
        const db = await res.json();
        const indicator = document.getElementById("db-status-indicator");
        const text = document.getElementById("db-status-text");

        if (!db.loaded) {
            indicator.className = "db-indicator db-stale";
            text.textContent = "Not loaded";
            return;
        }

        const age = db.age_days;
        const count = db.aircraft_count.toLocaleString();

        if (age == null) {
            indicator.className = "db-indicator db-warn";
            text.textContent = `${count} aircraft (age unknown)`;
        } else if (age <= 7) {
            indicator.className = "db-indicator db-fresh";
            text.textContent = `${count} aircraft (${age.toFixed(1)}d old)`;
        } else if (age <= 14) {
            indicator.className = "db-indicator db-warn";
            text.textContent = `${count} aircraft (${age.toFixed(1)}d old)`;
        } else {
            indicator.className = "db-indicator db-stale";
            text.textContent = `${count} aircraft (${age.toFixed(1)}d old — stale!)`;
        }
    } catch (e) {
        document.getElementById("db-status-text").textContent = "Error loading status";
    }
}

document.getElementById("db-refresh-btn").addEventListener("click", async () => {
    const btn = document.getElementById("db-refresh-btn");
    const status = document.getElementById("db-refresh-status");
    btn.disabled = true;
    btn.textContent = "Downloading...";
    status.textContent = "";
    status.className = "";

    try {
        const res = await fetch("/api/db/update", { method: "POST" });
        if (!res.ok) throw new Error("Server returned " + res.status);
        const result = await res.json();
        status.textContent = `Updated: ${result.aircraft_count.toLocaleString()} aircraft`;
        status.className = "success";
        await loadDbStatus();
    } catch (e) {
        status.textContent = "Error: " + e.message;
        status.className = "error";
    } finally {
        btn.disabled = false;
        btn.textContent = "Refresh Now";
        setTimeout(() => { status.textContent = ""; status.className = ""; }, 5000);
    }
});

// --- Collector Panel ---
let collectorMarkers = {};  // collector_id -> L.marker
let collectorsVisible = false;

document.getElementById("collectors-toggle").addEventListener("click", () => {
    const panel = document.getElementById("collectors-panel");
    const btn = document.getElementById("collectors-toggle");
    panel.classList.toggle("collectors-hidden");
    btn.classList.toggle("active");
});

async function loadCollectors() {
    try {
        const res = await fetch("/api/collectors");
        const collectors = await res.json();

        if (!Array.isArray(collectors) || collectors.length === 0) {
            // No collectors — keep button hidden
            return;
        }

        // Show the collectors toggle button
        document.getElementById("collectors-toggle").style.display = "";
        collectorsVisible = true;

        const list = document.getElementById("collectors-list");
        list.innerHTML = "";

        for (const c of collectors) {
            const el = document.createElement("div");
            el.className = "collector-item";

            const ago = c.last_heartbeat
                ? Math.round((Date.now() - new Date(c.last_heartbeat).getTime()) / 1000)
                : null;
            const heartbeat = ago != null ? (ago < 30 ? "healthy" : "stale") : "unknown";

            el.innerHTML = `
                <div class="collector-item-left">
                    <div class="collector-name">${c.name || c.collector_id}</div>
                    <div class="collector-id">${c.collector_id}</div>
                </div>
                <div class="collector-item-right">
                    <div>${c.aircraft_count} aircraft</div>
                    <div>${c.messages_per_second} msg/s</div>
                    <span class="collector-health collector-${heartbeat}"></span>
                </div>
            `;
            list.appendChild(el);

            // Place/update collector marker on the map
            if (c.latitude != null && c.longitude != null) {
                updateCollectorMarker(c.collector_id, c.name, c.latitude, c.longitude);
            }
        }
    } catch (e) {
        // Not in server mode or server unavailable — silently ignore
    }
}

function updateCollectorMarker(id, name, lat, lon) {
    const label = name || id;
    const icon = L.divIcon({
        html: `<svg width="20" height="20" viewBox="0 0 20 20">
            <polygon points="10,2 18,18 2,18" fill="#f59e0b" fill-opacity="0.4" stroke="#f59e0b" stroke-width="1.5"/>
            <circle cx="10" cy="13" r="2.5" fill="#f59e0b"/>
        </svg>`,
        iconSize: [20, 20],
        iconAnchor: [10, 18],
        className: "",
    });

    if (collectorMarkers[id]) {
        collectorMarkers[id].setLatLng([lat, lon]);
    } else {
        collectorMarkers[id] = L.marker([lat, lon], {
            icon,
            zIndexOffset: -500,
        }).addTo(map);
        collectorMarkers[id].bindTooltip(label, {
            permanent: false,
            direction: "top",
            className: "collector-tooltip",
        });
    }
}

// Poll collectors every 10 seconds (only relevant in server mode)
setInterval(loadCollectors, 10000);

// --- Init ---
connectWebSocket();
loadConfig();
loadDbStatus();
loadCollectors();
