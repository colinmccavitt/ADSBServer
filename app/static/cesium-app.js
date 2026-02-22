// ADSB Server - CesiumJS 3D View

Cesium.Ion.defaultAccessToken = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJqdGkiOiIwN2NlMWNjZC03NDZkLTQyMDYtOWIwOS1iNzUzMThkMzUzZmEiLCJpZCI6MjI3NDU1LCJpYXQiOjE3NzEyOTAwMzZ9.SRMWk_kOLpiG8dxYYtRrM2YGUbHfQ9oGELkmqdrlyXk';

// ─── State ───────────────────────────────────────────────────────────────────
const aircraftMap = {};   // icao -> { data, entity, trailPositions, trailEntity }
let selectedIcao = null;
let followMode = false;

// ─── Watchlist ───────────────────────────────────────────────────────────────
let watchlist = new Set();

async function loadWatchlist() {
    try {
        const res = await fetch("/api/watchlist");
        const data = await res.json();
        watchlist = new Set((data.watchlist || []).map(r => r.toUpperCase()));
    } catch (e) {
        console.warn("Failed to load watchlist:", e);
    }
}

function isWatchlisted(d) {
    return d.registration && watchlist.has(d.registration.toUpperCase());
}
let showLabels = true;
let showTrails = true;
let showBuildings = true;
let enableLighting = true;
let ws = null;
let receiverLat = 38.85596;
let receiverLon = -77.04952;
let receiverEntity = null;
let buildingTileset = null;
let viewCentered = false;

// ─── Plane SVG for billboard ─────────────────────────────────────────────────
const planeCanvasCache = {};

function createPlaneCanvas(heading, altitude, isSelected, isWL) {
    const rotation = heading != null ? heading : 0;
    let color;
    if (isWL) {
        color = "#dc2626"; // watchlist always crimson
    } else if (altitude != null) {
        if (altitude <= 0)        color = "#34d399";
        else if (altitude < 5000) color = "#22d3ee";
        else if (altitude < 15000) color = "#fbbf24";
        else if (altitude < 30000) color = "#f97316";
        else                       color = "#ef4444";
    } else {
        color = "#60a5fa";
    }

    // Watchlist aircraft use a larger canvas with warning rings
    const size = isWL ? 56 : 40;
    const offset = isWL ? 8 : 0; // center the plane shape in the larger canvas
    const canvas = document.createElement("canvas");
    canvas.width = size;
    canvas.height = size;
    const ctx = canvas.getContext("2d");

    // Draw watchlist warning rings (static on canvas; pulsing handled by CSS in 2D)
    if (isWL) {
        const cx = size / 2;
        const cy = size / 2;
        // Outer ring
        ctx.beginPath();
        ctx.arc(cx, cy, cx - 2, 0, Math.PI * 2);
        ctx.strokeStyle = isSelected ? "rgba(255,255,255,0.9)" : "rgba(220,38,38,0.85)";
        ctx.lineWidth = 2.5;
        ctx.stroke();
        // Inner ring
        ctx.beginPath();
        ctx.arc(cx, cy, cx - 7, 0, Math.PI * 2);
        ctx.strokeStyle = isSelected ? "rgba(255,255,255,0.5)" : "rgba(220,38,38,0.45)";
        ctx.lineWidth = 1.5;
        ctx.stroke();
    }

    ctx.translate(size / 2, size / 2);
    ctx.rotate((rotation * Math.PI) / 180);
    ctx.translate(-size / 2, -size / 2);

    if (isSelected) {
        ctx.shadowColor = isWL ? "#ff6060" : "#2eaadc";
        ctx.shadowBlur = 14;
    } else if (isWL) {
        ctx.shadowColor = "#dc2626";
        ctx.shadowBlur = 7;
    }

    // Draw plane shape (offset for watchlist larger canvas)
    ctx.beginPath();
    ctx.moveTo(20 + offset, 4 + offset);
    ctx.lineTo(16 + offset, 15 + offset);
    ctx.lineTo(5 + offset, 20 + offset);
    ctx.lineTo(16 + offset, 22 + offset);
    ctx.lineTo(14 + offset, 33 + offset);
    ctx.lineTo(20 + offset, 30 + offset);
    ctx.lineTo(26 + offset, 33 + offset);
    ctx.lineTo(24 + offset, 22 + offset);
    ctx.lineTo(35 + offset, 20 + offset);
    ctx.lineTo(24 + offset, 15 + offset);
    ctx.closePath();

    ctx.fillStyle = color;
    ctx.fill();
    ctx.strokeStyle = (isSelected || isWL) ? "#ffffff" : "#37352f";
    ctx.lineWidth = isSelected ? 1.5 : (isWL ? 1.5 : 0.8);
    ctx.stroke();

    return canvas;
}

// ─── Altitude to color ───────────────────────────────────────────────────────
function altitudeToColor(altitude) {
    if (altitude == null) return Cesium.Color.fromCssColorString("#60a5fa");
    if (altitude <= 0)       return Cesium.Color.fromCssColorString("#34d399");
    if (altitude < 5000)     return Cesium.Color.fromCssColorString("#22d3ee");
    if (altitude < 15000)    return Cesium.Color.fromCssColorString("#fbbf24");
    if (altitude < 30000)    return Cesium.Color.fromCssColorString("#f97316");
    return Cesium.Color.fromCssColorString("#ef4444");
}

// Feet to meters
function feetToMeters(ft) {
    return ft * 0.3048;
}

// Best available altitude in meters for Cesium WGS84 ellipsoid positioning.
// Prefers geometric (GNSS) altitude; falls back to barometric.
function altMetersWGS84(d) {
    if (d.alt_geom != null) return feetToMeters(d.alt_geom);
    if (d.altitude != null) return feetToMeters(d.altitude);
    return 0;
}

// ─── Initialize Cesium Viewer ────────────────────────────────────────────────
const viewer = new Cesium.Viewer("cesiumContainer", {
    terrain: Cesium.Terrain.fromWorldTerrain(),
    skyBox: new Cesium.SkyBox({
        sources: {
            positiveX: Cesium.buildModuleUrl("Assets/Textures/SkyBox/tycho2t3_80_px.jpg"),
            negativeX: Cesium.buildModuleUrl("Assets/Textures/SkyBox/tycho2t3_80_mx.jpg"),
            positiveY: Cesium.buildModuleUrl("Assets/Textures/SkyBox/tycho2t3_80_py.jpg"),
            negativeY: Cesium.buildModuleUrl("Assets/Textures/SkyBox/tycho2t3_80_my.jpg"),
            positiveZ: Cesium.buildModuleUrl("Assets/Textures/SkyBox/tycho2t3_80_pz.jpg"),
            negativeZ: Cesium.buildModuleUrl("Assets/Textures/SkyBox/tycho2t3_80_mz.jpg"),
        },
    }),
    skyAtmosphere: new Cesium.SkyAtmosphere(),
    sceneModePicker: true,
    baseLayerPicker: true,
    navigationHelpButton: false,
    homeButton: false,
    geocoder: false,
    fullscreenButton: false,
    animation: false,
    timeline: false,
    selectionIndicator: true,
    infoBox: false,
    shadows: false,
});

// Enable lighting for day/night cycle
viewer.scene.globe.enableLighting = enableLighting;
viewer.scene.globe.dynamicAtmosphereLighting = true;
viewer.scene.globe.dynamicAtmosphereLightingFromSun = true;
viewer.scene.fog.enabled = true;
viewer.scene.fog.density = 0.0002;

// Depth test against terrain so entities behind terrain are hidden
viewer.scene.globe.depthTestAgainstTerrain = true;

// Smoother rendering
viewer.scene.postProcessStages.fxaa.enabled = true;

// ─── OSM Buildings ───────────────────────────────────────────────────────────
async function loadBuildings() {
    try {
        buildingTileset = await Cesium.createOsmBuildingsAsync();
        buildingTileset.style = new Cesium.Cesium3DTileStyle({
            color: {
                conditions: [
                    ["${feature['cesium#estimatedHeight']} >= 100", "color('rgba(180, 175, 165, 0.85)')"],
                    ["${feature['cesium#estimatedHeight']} >= 50", "color('rgba(195, 190, 180, 0.85)')"],
                    ["${feature['cesium#estimatedHeight']} >= 20", "color('rgba(210, 205, 195, 0.8)')"],
                    ["true", "color('rgba(220, 215, 205, 0.75)')"],
                ],
            },
        });
        viewer.scene.primitives.add(buildingTileset);
    } catch (e) {
        console.warn("Could not load OSM Buildings:", e);
    }
}
loadBuildings();

// ─── Receiver marker ─────────────────────────────────────────────────────────
function updateReceiverEntity(lat, lon) {
    receiverLat = lat;
    receiverLon = lon;

    if (receiverEntity) {
        viewer.entities.remove(receiverEntity);
    }

    receiverEntity = viewer.entities.add({
        position: Cesium.Cartesian3.fromDegrees(lon, lat, 0),
        point: {
            pixelSize: 14,
            color: Cesium.Color.fromCssColorString("#2eaadc").withAlpha(0.4),
            outlineColor: Cesium.Color.fromCssColorString("#2eaadc"),
            outlineWidth: 3,
            heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
            disableDepthTestDistance: Number.POSITIVE_INFINITY,
        },
        ellipse: {
            semiMinorAxis: 500,
            semiMajorAxis: 500,
            material: Cesium.Color.fromCssColorString("#2eaadc").withAlpha(0.08),
            outline: true,
            outlineColor: Cesium.Color.fromCssColorString("#2eaadc").withAlpha(0.25),
            outlineWidth: 1,
            heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
        },
        label: {
            text: "Receiver",
            font: "12px sans-serif",
            fillColor: Cesium.Color.fromCssColorString("#2eaadc"),
            outlineColor: Cesium.Color.BLACK,
            outlineWidth: 2,
            style: Cesium.LabelStyle.FILL_AND_OUTLINE,
            verticalOrigin: Cesium.VerticalOrigin.TOP,
            pixelOffset: new Cesium.Cartesian2(0, 12),
            disableDepthTestDistance: Number.POSITIVE_INFINITY,
            heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
        },
    });
}

// ─── Aircraft management ─────────────────────────────────────────────────────
function updateAircraft(data) {
    const icao = data.icao;
    let entry = aircraftMap[icao];

    if (!entry) {
        entry = { data: data, entity: null, trailPositions: [], trailEntity: null };
        aircraftMap[icao] = entry;
    } else {
        for (const key of Object.keys(data)) {
            if (data[key] != null) {
                entry.data[key] = data[key];
            }
        }
    }

    const d = entry.data;

    if (d.latitude != null && d.longitude != null) {
        const position = Cesium.Cartesian3.fromDegrees(d.longitude, d.latitude, altMetersWGS84(d));

        // Mirror 2D behavior: center the map view once when first aircraft appears.
        if (!viewCentered) {
            viewCentered = true;
            viewer.camera.flyTo({
                destination: Cesium.Cartesian3.fromDegrees(d.longitude, d.latitude, 50000),
                orientation: {
                    heading: 0,
                    pitch: Cesium.Math.toRadians(-45),
                    roll: 0,
                },
                duration: 1.2,
            });
        }

        const isSelected = icao === selectedIcao;
        const wl = isWatchlisted(d);

        // Record trail position
        entry.trailPositions.push(position);
        if (entry.trailPositions.length > 200) entry.trailPositions.shift();

        // Create billboard from canvas
        const canvas = createPlaneCanvas(d.track, d.altitude, isSelected, wl);
        const bbSize = wl ? 52 : 36;

        if (entry.entity) {
            entry.entity.position = position;
            entry.entity.billboard.image = canvas;
            entry.entity.billboard.width = bbSize;
            entry.entity.billboard.height = bbSize;
            if (entry.entity.label) {
                entry.entity.label.text = d.callsign || icao;
                entry.entity.label.show = showLabels;
                entry.entity.label.fillColor = wl
                    ? Cesium.Color.fromCssColorString("#ff6060")
                    : Cesium.Color.WHITE;
                entry.entity.label.outlineColor = wl
                    ? Cesium.Color.fromCssColorString("#7f0000")
                    : Cesium.Color.BLACK;
                entry.entity.label.scale = wl ? 1.1 : 0.9;
            }
        } else {
            entry.entity = viewer.entities.add({
                id: "aircraft-" + icao,
                position: position,
                billboard: {
                    image: canvas,
                    width: bbSize,
                    height: bbSize,
                    verticalOrigin: Cesium.VerticalOrigin.CENTER,
                    horizontalOrigin: Cesium.HorizontalOrigin.CENTER,
                    disableDepthTestDistance: Number.POSITIVE_INFINITY,
                    eyeOffset: new Cesium.Cartesian3(0, 0, wl ? -200 : -50),
                    alignedAxis: Cesium.Cartesian3.UNIT_Z,
                },
                label: {
                    text: d.callsign || icao,
                    font: wl ? "bold 13px sans-serif" : "bold 12px sans-serif",
                    fillColor: wl
                        ? Cesium.Color.fromCssColorString("#ff6060")
                        : Cesium.Color.WHITE,
                    outlineColor: wl
                        ? Cesium.Color.fromCssColorString("#7f0000")
                        : Cesium.Color.BLACK,
                    outlineWidth: 3,
                    style: Cesium.LabelStyle.FILL_AND_OUTLINE,
                    verticalOrigin: Cesium.VerticalOrigin.TOP,
                    pixelOffset: new Cesium.Cartesian2(0, wl ? 30 : 22),
                    disableDepthTestDistance: Number.POSITIVE_INFINITY,
                    show: showLabels,
                    scale: wl ? 1.1 : 0.9,
                },
                properties: {
                    icao: icao,
                    isAircraft: true,
                },
            });
        }

        // Trail polyline
        if (showTrails && entry.trailPositions.length > 1) {
            const color = altitudeToColor(d.altitude).withAlpha(0.5);
            if (entry.trailEntity) {
                entry.trailEntity.polyline.positions = [...entry.trailPositions];
                entry.trailEntity.polyline.material = color;
            } else {
                entry.trailEntity = viewer.entities.add({
                    polyline: {
                        positions: [...entry.trailPositions],
                        width: isSelected ? 3 : 1.5,
                        material: color,
                        clampToGround: false,
                    },
                });
            }
        }

        // Follow mode - track camera to selected aircraft
        if (followMode && icao === selectedIcao) {
            viewer.camera.lookAt(
                position,
                new Cesium.HeadingPitchRange(
                    Cesium.Math.toRadians(d.track || 0),
                    Cesium.Math.toRadians(-25),
                    3000
                )
            );
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
    const entry = aircraftMap[icao];
    if (!entry) return;

    if (entry.entity) viewer.entities.remove(entry.entity);
    if (entry.trailEntity) viewer.entities.remove(entry.trailEntity);
    delete aircraftMap[icao];

    const el = document.getElementById("ac-" + icao);
    if (el) el.remove();

    if (icao === selectedIcao) {
        selectedIcao = null;
        followMode = false;
        updateFollowButton();
        document.getElementById("detail-panel").classList.add("hidden");
        viewer.camera.lookAtTransform(Cesium.Matrix4.IDENTITY);
    }

    document.getElementById("stat-aircraft").textContent =
        Object.keys(aircraftMap).length + " aircraft";
}

// ─── Sidebar ─────────────────────────────────────────────────────────────────
function updateSidebarItem(icao, d) {
    let el = document.getElementById("ac-" + icao);
    if (!el) {
        el = document.createElement("div");
        el.id = "ac-" + icao;
        el.className = "ac-item";
        el.onclick = () => selectAircraft(icao);
        document.getElementById("aircraft-list").appendChild(el);
    }

    el.classList.toggle("selected", icao === selectedIcao);

    const wl = isWatchlisted(d);
    if (wl) el.classList.add("watchlist");
    else el.classList.remove("watchlist");

    const callsign = d.callsign || "------";
    const alt = d.altitude != null ? d.altitude.toLocaleString() + " ft" : "---";
    const speed = d.ground_speed != null ? Math.round(d.ground_speed) + " kt" : "";
    const typeStr = d.aircraft_type ? ` · ${d.aircraft_type}` : "";
    const regStr = d.registration || icao;
    const badge = wl ? '<span class="watchlist-badge">&#9872; Watch</span>' : "";

    el.innerHTML = `
        <div class="ac-item-left">
            <div class="ac-callsign">${callsign}${typeStr}${badge}</div>
            <div class="ac-icao">${regStr}${d.registration ? ' · ' + icao : ''}</div>
        </div>
        <div class="ac-item-right">
            <div class="ac-alt">${alt}</div>
            <div>${speed}</div>
        </div>
    `;
}

// ─── Selection / Detail ──────────────────────────────────────────────────────
function selectAircraft(icao) {
    // Deselect old
    if (selectedIcao) {
        const oldEl = document.getElementById("ac-" + selectedIcao);
        if (oldEl) oldEl.classList.remove("selected");

        const oldEntry = aircraftMap[selectedIcao];
        if (oldEntry && oldEntry.entity && oldEntry.data.latitude != null) {
            const oldWl = isWatchlisted(oldEntry.data);
            const canvas = createPlaneCanvas(oldEntry.data.track, oldEntry.data.altitude, false, oldWl);
            oldEntry.entity.billboard.image = canvas;
        }
        if (oldEntry && oldEntry.trailEntity) {
            oldEntry.trailEntity.polyline.width = 1.5;
        }
    }

    selectedIcao = icao;
    followMode = false;
    updateFollowButton();
    viewer.camera.lookAtTransform(Cesium.Matrix4.IDENTITY);

    const entry = aircraftMap[icao];
    if (!entry) return;

    // Highlight in sidebar
    const el = document.getElementById("ac-" + icao);
    if (el) el.classList.add("selected");

    // Update billboard to selected state
    if (entry.entity && entry.data.latitude != null) {
        const canvas = createPlaneCanvas(entry.data.track, entry.data.altitude, true, isWatchlisted(entry.data));
        entry.entity.billboard.image = canvas;
    }

    // Thicken trail
    if (entry.trailEntity) {
        entry.trailEntity.polyline.width = 3;
    }

    // Fly to aircraft
    if (entry.data.latitude != null && entry.data.longitude != null) {
        const altM = altMetersWGS84(entry.data) || 1000;
        viewer.camera.flyTo({
            destination: Cesium.Cartesian3.fromDegrees(
                entry.data.longitude,
                entry.data.latitude,
                altM + 5000
            ),
            orientation: {
                heading: Cesium.Math.toRadians(entry.data.track || 0),
                pitch: Cesium.Math.toRadians(-35),
                roll: 0,
            },
            duration: 1.5,
        });
    }

    updateDetailPanel(entry.data);
    document.getElementById("detail-panel").classList.remove("hidden");
}

function updateDetailPanel(d) {
    const banner = document.getElementById("detail-watchlist-banner");
    if (banner) {
        if (isWatchlisted(d)) banner.classList.add("visible");
        else banner.classList.remove("visible");
    }

    document.getElementById("detail-callsign").textContent = d.callsign || "Unknown";
    document.getElementById("detail-icao").textContent = d.icao;

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
}

// Close detail panel
document.getElementById("detail-close").addEventListener("click", () => {
    document.getElementById("detail-panel").classList.add("hidden");
    if (selectedIcao) {
        const el = document.getElementById("ac-" + selectedIcao);
        if (el) el.classList.remove("selected");
        const entry = aircraftMap[selectedIcao];
        if (entry && entry.entity && entry.data.latitude != null) {
            const canvas = createPlaneCanvas(entry.data.track, entry.data.altitude, false, isWatchlisted(entry.data));
            entry.entity.billboard.image = canvas;
        }
        if (entry && entry.trailEntity) {
            entry.trailEntity.polyline.width = 1.5;
        }
        selectedIcao = null;
        followMode = false;
        updateFollowButton();
        viewer.camera.lookAtTransform(Cesium.Matrix4.IDENTITY);
    }
});

// Fly-to button in detail panel
document.getElementById("detail-flyto").addEventListener("click", () => {
    if (!selectedIcao) return;
    const entry = aircraftMap[selectedIcao];
    if (!entry || entry.data.latitude == null) return;

    followMode = false;
    updateFollowButton();
    viewer.camera.lookAtTransform(Cesium.Matrix4.IDENTITY);

    const altM = altMetersWGS84(entry.data) || 1000;
    viewer.camera.flyTo({
        destination: Cesium.Cartesian3.fromDegrees(
            entry.data.longitude,
            entry.data.latitude,
            altM + 3000
        ),
        orientation: {
            heading: Cesium.Math.toRadians(entry.data.track || 0),
            pitch: Cesium.Math.toRadians(-30),
            roll: 0,
        },
        duration: 1.5,
    });
});

// Track camera button in detail panel
document.getElementById("detail-track").addEventListener("click", () => {
    if (!selectedIcao) return;
    followMode = !followMode;
    updateFollowButton();

    if (!followMode) {
        viewer.camera.lookAtTransform(Cesium.Matrix4.IDENTITY);
    }
});

// ─── Click handling on globe ─────────────────────────────────────────────────
const handler = new Cesium.ScreenSpaceEventHandler(viewer.scene.canvas);
handler.setInputAction((click) => {
    const picked = viewer.scene.pick(click.position);
    if (Cesium.defined(picked) && picked.id && picked.id.properties) {
        const props = picked.id.properties;
        if (props.isAircraft && props.isAircraft.getValue()) {
            selectAircraft(props.icao.getValue());
        }
    }
}, Cesium.ScreenSpaceEventType.LEFT_CLICK);

// ─── Stats ───────────────────────────────────────────────────────────────────
function updateStats(stats) {
    const posCount = stats.aircraft_with_position ?? 0;
    document.getElementById("stat-aircraft").textContent =
        stats.aircraft_count + " aircraft" + (posCount > 0 ? " (" + posCount + " pos)" : "");
    const pps = stats.positions_per_second ?? 0;
    const posEl = document.getElementById("stat-pos");
    if (posEl) posEl.textContent = pps + " pos/s";
    document.getElementById("stat-msgs").textContent = stats.messages_per_second + " msg/s";
    document.getElementById("stat-total").textContent = stats.messages_total.toLocaleString() + " msgs";
}

// ─── WebSocket ───────────────────────────────────────────────────────────────
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
        }
    };

    ws.onclose = () => {
        const statusEl = document.getElementById("stat-status");
        statusEl.textContent = "Disconnected";
        statusEl.className = "status-disconnected";
        setTimeout(connectWebSocket, 3000);
    };

    ws.onerror = () => {
        ws.close();
    };
}

// ─── Toolbar buttons ─────────────────────────────────────────────────────────
function updateFollowButton() {
    const btn = document.getElementById("btn-follow");
    btn.classList.toggle("active", followMode);
    btn.classList.toggle("inactive", !followMode);
}

document.getElementById("btn-home").addEventListener("click", () => {
    followMode = false;
    updateFollowButton();
    viewer.camera.lookAtTransform(Cesium.Matrix4.IDENTITY);

    viewer.camera.flyTo({
        destination: Cesium.Cartesian3.fromDegrees(receiverLon, receiverLat, 50000),
        orientation: {
            heading: 0,
            pitch: Cesium.Math.toRadians(-45),
            roll: 0,
        },
        duration: 1.5,
    });
});

document.getElementById("btn-follow").addEventListener("click", () => {
    if (!selectedIcao) return;
    followMode = !followMode;
    updateFollowButton();
    if (!followMode) {
        viewer.camera.lookAtTransform(Cesium.Matrix4.IDENTITY);
    }
});

document.getElementById("btn-buildings").addEventListener("click", () => {
    showBuildings = !showBuildings;
    const btn = document.getElementById("btn-buildings");
    btn.classList.toggle("active", showBuildings);
    btn.classList.toggle("inactive", !showBuildings);
    if (buildingTileset) {
        buildingTileset.show = showBuildings;
    }
});

document.getElementById("btn-labels").addEventListener("click", () => {
    showLabels = !showLabels;
    const btn = document.getElementById("btn-labels");
    btn.classList.toggle("active", showLabels);
    btn.classList.toggle("inactive", !showLabels);

    for (const icao of Object.keys(aircraftMap)) {
        const entry = aircraftMap[icao];
        if (entry.entity && entry.entity.label) {
            entry.entity.label.show = showLabels;
        }
    }
});

document.getElementById("btn-trails").addEventListener("click", () => {
    showTrails = !showTrails;
    const btn = document.getElementById("btn-trails");
    btn.classList.toggle("active", showTrails);
    btn.classList.toggle("inactive", !showTrails);

    for (const icao of Object.keys(aircraftMap)) {
        const entry = aircraftMap[icao];
        if (entry.trailEntity) {
            entry.trailEntity.show = showTrails;
        }
    }
});

document.getElementById("btn-night").addEventListener("click", () => {
    enableLighting = !enableLighting;
    const btn = document.getElementById("btn-night");
    btn.classList.toggle("active", enableLighting);
    btn.classList.toggle("inactive", !enableLighting);
    viewer.scene.globe.enableLighting = enableLighting;
});

// Set initial toolbar button states
document.getElementById("btn-buildings").classList.add("active");
document.getElementById("btn-labels").classList.add("active");
document.getElementById("btn-trails").classList.add("active");
document.getElementById("btn-night").classList.add("active");

// ─── Config panel ────────────────────────────────────────────────────────────
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

        const sel = document.getElementById("cfg-gain");
        sel.innerHTML = "";
        for (const g of cfg.supported_gains) {
            const opt = document.createElement("option");
            opt.value = g;
            opt.textContent = g + " dB";
            if (Math.abs(g - cfg.gain) < 0.01) opt.selected = true;
            sel.appendChild(opt);
        }

        receiverLat = cfg.latitude;
        receiverLon = cfg.longitude;
        updateReceiverEntity(cfg.latitude, cfg.longitude);

        // Fly camera to receiver location
        viewer.camera.flyTo({
            destination: Cesium.Cartesian3.fromDegrees(cfg.longitude, cfg.latitude, 50000),
            orientation: {
                heading: 0,
                pitch: Cesium.Math.toRadians(-45),
                roll: 0,
            },
            duration: 2,
        });
    } catch (e) {
        console.error("Failed to load config:", e);
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

        receiverLat = cfg.latitude;
        receiverLon = cfg.longitude;
        updateReceiverEntity(cfg.latitude, cfg.longitude);

        viewer.camera.flyTo({
            destination: Cesium.Cartesian3.fromDegrees(cfg.longitude, cfg.latitude, 50000),
            orientation: {
                heading: 0,
                pitch: Cesium.Math.toRadians(-45),
                roll: 0,
            },
            duration: 1.5,
        });

        setTimeout(() => { statusEl.textContent = ""; }, 3000);
    } catch (e) {
        statusEl.textContent = "Error: " + e.message;
        statusEl.className = "error";
    }
});

document.getElementById("cfg-autogain").addEventListener("click", async () => {
    const btn = document.getElementById("cfg-autogain");
    const statusEl = document.getElementById("cfg-status");
    btn.disabled = true;
    btn.textContent = "Testing...";
    document.getElementById("cfg-save").disabled = true;
    statusEl.textContent = "";

    document.getElementById("config-panel").classList.remove("config-hidden");
    document.getElementById("config-toggle").classList.add("active");

    try {
        const res = await fetch("/api/autogain", { method: "POST" });
        if (!res.ok) throw new Error("Server returned " + res.status);
        const result = await res.json();
        statusEl.textContent = "Best gain: " + result.best_gain + " dB";
        statusEl.className = "success";
    } catch (e) {
        statusEl.textContent = "Error: " + e.message;
        statusEl.className = "error";
    } finally {
        btn.disabled = false;
        btn.textContent = "Auto Gain";
        document.getElementById("cfg-save").disabled = false;
        setTimeout(() => { statusEl.textContent = ""; statusEl.className = ""; }, 5000);
    }
});

// ─── Init ────────────────────────────────────────────────────────────────────
connectWebSocket();
loadConfig();
loadWatchlist();
