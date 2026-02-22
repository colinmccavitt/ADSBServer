// ADSB Server Admin Dashboard — live updates via Server-Sent Events

let evtSource = null;
let connected = false;

// --- Formatting helpers ---

function formatDuration(seconds) {
    if (seconds == null || isNaN(seconds)) return "---";
    const s = Math.floor(seconds);
    if (s < 60)   return s + "s";
    if (s < 3600)  return Math.floor(s / 60) + "m " + (s % 60) + "s";
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    if (h < 24) return h + "h " + m + "m";
    const d = Math.floor(h / 24);
    return d + "d " + (h % 24) + "h";
}

function formatUptime(seconds) {
    return formatDuration(seconds);
}

function timeAgo(isoString) {
    if (!isoString) return "---";
    const diff = (Date.now() - new Date(isoString).getTime()) / 1000;
    if (diff < 0) return "just now";
    if (diff < 5) return "just now";
    if (diff < 60) return Math.floor(diff) + "s ago";
    if (diff < 3600) return Math.floor(diff / 60) + "m ago";
    return Math.floor(diff / 3600) + "h ago";
}

function timeSince(isoString) {
    if (!isoString) return "---";
    const diff = (Date.now() - new Date(isoString).getTime()) / 1000;
    return formatDuration(diff);
}

function formatTime(isoString) {
    if (!isoString) return "---";
    return new Date(isoString).toLocaleTimeString();
}

function formatLatLon(lat, lon) {
    if (lat == null || lon == null) return "---";
    return lat.toFixed(4) + ", " + lon.toFixed(4);
}

function healthClass(isoString) {
    if (!isoString) return "health-unknown";
    const ago = (Date.now() - new Date(isoString).getTime()) / 1000;
    return ago < 30 ? "health-healthy" : "health-stale";
}

// --- Update the page ---

function updateStats(stats) {
    document.getElementById("stat-aircraft").textContent = stats.aircraft_count || 0;
    document.getElementById("stat-mps").textContent = stats.messages_per_second || 0;
    document.getElementById("stat-collectors").textContent = stats.collector_count || 0;
    document.getElementById("stat-clients").textContent = stats.client_count || 0;
    document.getElementById("stat-uptime").textContent = formatUptime(stats.uptime_seconds);
}

function updateCollectors(collectors) {
    const body = document.getElementById("collectors-body");
    const empty = document.getElementById("collectors-empty");
    const count = document.getElementById("collectors-count");

    count.textContent = collectors.length + " connected";

    if (collectors.length === 0) {
        body.innerHTML = "";
        empty.style.display = "";
        return;
    }
    empty.style.display = "none";

    // Build rows
    let html = "";
    for (const c of collectors) {
        const hClass = healthClass(c.last_heartbeat);
        const hLabel = hClass === "health-healthy" ? "Healthy"
                     : hClass === "health-stale"   ? "Stale"
                     : "Unknown";

        html += `<tr>
            <td><span class="health-dot ${hClass}"></span>${hLabel}</td>
            <td>
                <div class="collector-name-cell">
                    <span class="collector-name-main">${escHtml(c.name || c.collector_id)}</span>
                    ${c.name ? '<span class="collector-name-id">' + escHtml(c.collector_id) + '</span>' : ''}
                </div>
            </td>
            <td class="mono">${formatLatLon(c.latitude, c.longitude)}</td>
            <td>${c.aircraft_count}</td>
            <td>${c.messages_per_second}</td>
            <td title="${formatTime(c.connected_since)}">${timeSince(c.connected_since)}</td>
            <td>${timeAgo(c.last_heartbeat)}</td>
        </tr>`;
    }
    body.innerHTML = html;
}

function updateClients(clients) {
    const body = document.getElementById("clients-body");
    const empty = document.getElementById("clients-empty");
    const count = document.getElementById("clients-count");

    count.textContent = clients.length + " connected";

    if (clients.length === 0) {
        body.innerHTML = "";
        empty.style.display = "";
        return;
    }
    empty.style.display = "none";

    let html = "";
    for (const c of clients) {
        const typeBadge = c.client_type === "api"
            ? '<span class="type-badge api">API</span>'
            : '<span class="type-badge browser">Browser</span>';

        html += `<tr>
            <td>${typeBadge}</td>
            <td class="mono">${escHtml(c.client_id)}</td>
            <td class="mono">${escHtml(c.remote_addr || "---")}</td>
            <td>${formatTime(c.connected_since)}</td>
            <td>${timeSince(c.connected_since)}</td>
        </tr>`;
    }
    body.innerHTML = html;
}

function escHtml(str) {
    if (!str) return "";
    return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

// --- SSE connection ---

function connectSSE() {
    evtSource = new EventSource("/admin/stream");

    evtSource.onopen = () => {
        connected = true;
        document.getElementById("sse-dot").classList.add("connected");
        document.getElementById("sse-status").textContent = "Live";
    };

    evtSource.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data);
            if (data.stats) updateStats(data.stats);
            if (data.collectors) updateCollectors(data.collectors);
            if (data.clients) updateClients(data.clients);
        } catch (e) {
            console.error("Failed to parse SSE data:", e);
        }
    };

    evtSource.onerror = () => {
        connected = false;
        document.getElementById("sse-dot").classList.remove("connected");
        document.getElementById("sse-status").textContent = "Reconnecting...";
        evtSource.close();
        setTimeout(connectSSE, 3000);
    };
}

// --- Init ---
connectSSE();
