const $ = id => document.getElementById(id);
const state = {captures: [], selected: null, kind: "primary", detail: null, jobId: null, loading: false};
const names = {primary: "Primary", sensor: "Sensor", live: "Live activity", unknown: "Unclassified"};
const colors = {primary: "#126b54", sensor: "#8270bd", live: "#417b94", unknown: "#93a49d"};

function bytes(value) {
    if (value < 1024) return `${value} B`;
    return `${(value / 1024).toFixed(1)} KB`;
}

function date(value, options = {}) {
    return new Date(value).toLocaleString(undefined, {month: "short", day: "numeric", ...options});
}

function error(message) {
    $("error-banner").textContent = message || "";
    $("error-banner").hidden = !message;
}

async function api(path, options) {
    const response = await fetch(path, options);
    const body = await response.json();
    if (!response.ok) throw new Error(body.error || "Request failed");
    return body;
}

function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
}

function svgElement(tag, attributes) {
    const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
    return node;
}

function drawCaptureChart(captures) {
    const container = $("capture-chart");
    container.replaceChildren();
    if (!captures.length) {
        container.append(element("p", "empty-state", "Your first capture will appear here."));
        return;
    }
    const points = captures.slice(0, 12).reverse();
    const maximum = Math.max(...points.map(item => item.size), 1024);
    const svg = svgElement("svg", {viewBox: "0 0 600 180", role: "img", "aria-label": "Capture file sizes in bytes"});
    for (let step = 0; step <= 3; step++) {
        const y = 155 - step * 45;
        svg.append(svgElement("line", {x1: 43, x2: 588, y1: y, y2: y, stroke: "#e9efeb", "stroke-dasharray": "3 4"}));
        const label = svgElement("text", {x: 0, y: y + 3, fill: "#92a298", "font-size": 9});
        label.textContent = bytes(maximum * step / 3);
        svg.append(label);
    }
    const coordinates = points.map((item, index) => ({item,
        x: points.length === 1 ? 315 : 55 + index * 520 / (points.length - 1),
        y: 155 - item.size / maximum * 135}));
    const positions = coordinates.map(point => `${point.x},${point.y}`).join(" ");
    svg.append(svgElement("polyline", {points: positions, fill: "none", stroke: "#cee1d6", "stroke-width": 2}));
    for (const {item, x, y} of coordinates) {
        const circle = svgElement("circle", {cx: x, cy: y, r: 4, fill: colors[item.kind], stroke: "white", "stroke-width": 2});
        const title = svgElement("title", {});
        title.textContent = `${date(item.timestamp, {hour: "numeric", minute: "2-digit"})}: ${item.size.toLocaleString()} bytes (${names[item.kind]})`;
        circle.append(title);
        svg.append(circle);
    }
    container.append(svg);
}

function renderLibrary() {
    const query = $("search-input").value.trim().toLowerCase();
    const filter = $("type-filter").value;
    const captures = state.captures.filter(item => (filter === "all" || item.kind === filter) &&
        [item.id, item.sha256, ...item.copies].some(value => value.toLowerCase().includes(query)));
    const body = $("capture-rows");
    body.replaceChildren();
    $("file-count").textContent = `${captures.length} capture${captures.length === 1 ? "" : "s"}`;
    $("library-empty").hidden = Boolean(captures.length);
    for (const capture of captures) {
        const row = element("tr", capture.id === state.selected ? "selected" : "");
        row.tabIndex = 0;
        row.setAttribute("aria-selected", String(capture.id === state.selected));
        const nameCell = element("td");
        nameCell.append(element("span", "capture-name", capture.id.replace(/\.bin$/, "")));
        nameCell.append(element("span", "capture-date", `${date(capture.timestamp, {hour: "numeric", minute: "2-digit"})}${capture.copies.length > 1 ? ` · ${capture.copies.length} copies` : ""}`));
        const typeCell = element("td");
        typeCell.append(element("span", `type-pill ${capture.kind}`, names[capture.kind]));
        row.append(nameCell, typeCell, element("td", "", bytes(capture.size)), element("td", "", capture.source));
        row.addEventListener("click", () => selectCapture(capture.id));
        row.addEventListener("keydown", event => {
            if (event.key === "Enter" || event.key === " ") {event.preventDefault(); selectCapture(capture.id);}
        });
        body.append(row);
    }
}

function fact(label, value, className = "") {
    const row = element("div", `fact-row ${className}`);
    row.append(element("dt", "", label), element("dd", "", value));
    return row;
}

async function selectCapture(id, offset = 0) {
    state.selected = id;
    renderLibrary();
    try {
        const detail = await api(`/api/captures/${encodeURIComponent(id)}?offset=${offset}&limit=256`);
        if (state.selected !== id) return;
        state.detail = detail;
        $("inspect-title").textContent = `${names[detail.kind]} capture`;
        $("inspect-subtitle").textContent = detail.id;
        $("download-link").hidden = false;
        $("download-link").href = `/api/captures/${encodeURIComponent(id)}/download`;
        $("inspect-content").hidden = false;
        $("inspect-facts").replaceChildren(
            fact("Stored bytes", `${detail.size.toLocaleString()} bytes`),
            fact("Captured / file time", date(detail.timestamp, {hour: "numeric", minute: "2-digit", second: "2-digit"})),
            fact("Time source", detail.timestampSource), fact("Capture source", detail.source),
            fact("Type evidence", detail.classification === "sidecar" ? "Recorded request metadata" : detail.classification === "filename" ? "Recorded filename" : "Not classified"),
            fact("Envelope", detail.observedEnvelope ? "Observed 21-byte prefix" : "Not recognized"),
            fact("Bytes after prefix", detail.payloadSize === null ? "Not determined" : `${detail.payloadSize.toLocaleString()}${detail.blockAligned ? " · 16-byte aligned" : ""}`),
            fact("Body entropy", `${detail.entropy.toFixed(3)} bits / byte`), fact("SHA-256", detail.sha256, "hash")
        );
        const histogram = svgElement("svg", {viewBox: "0 0 256 50", role: "img", "aria-label": "Byte-value frequency distribution"});
        const maximum = Math.max(...detail.histogram, 1);
        detail.histogram.forEach((count, index) => {
            const height = count / maximum * 46;
            histogram.append(svgElement("rect", {x: index, y: 48 - height, width: .7, height, fill: "#80ad96"}));
        });
        $("histogram").replaceChildren(histogram);
        const hex = $("hex-view");
        hex.replaceChildren();
        for (const row of detail.rows) {
            const line = element("div", "hex-row");
            line.append(element("span", "hex-offset", row.offset.toString(16).padStart(6, "0")));
            row.bytes.forEach((byte, index) => line.append(element("span",
                `hex-byte${detail.observedEnvelope && row.offset + index < 21 ? " header-byte" : ""}`,
                byte.toString(16).padStart(2, "0"))));
            line.append(element("span", "hex-ascii", row.ascii));
            hex.append(line);
        }
        $("byte-range").textContent = `${detail.offset.toLocaleString()}–${detail.nextOffset.toLocaleString()} of ${detail.size.toLocaleString()}`;
        $("previous-page").disabled = detail.offset === 0;
        $("next-page").disabled = detail.nextOffset >= detail.size;
        $("metadata-view").textContent = JSON.stringify(detail.metadata, null, 2);
    } catch (failure) {error(failure.message);}
}

function renderJob(job) {
    if (!job) return;
    $("job-status").textContent = job.status.toUpperCase();
    $("job-status").className = `badge ${job.status}`;
    $("job-title").textContent = job.kind === "record" ? {running: "Recording a BLE session.", complete: "BLE measurements recorded locally.", failed: "The recording needs attention."}[job.status] : {running: "A new reading is on its way.", complete: "Reading saved on this Mac.", failed: "The reader needs a little attention."}[job.status];
    $("job-log").textContent = job.log;
    $("job-log").scrollTop = $("job-log").scrollHeight;
    if (job.status === "failed" && state.failureId !== job.id) {
        state.failureId = job.id;
        error("BLE read failed. Keep the tracker nearby and disconnected from other Bluetooth hosts. See diagnostics for details.");
    }
    if (job.status === "complete" && job.id !== state.jobId) {
        state.jobId = job.id;
        if (job.captureId) selectCapture(job.captureId);
    }
}

async function refresh() {
    if (state.loading) return;
    state.loading = true;
    try {
        const data = await api("/api/state");
        state.captures = data.captures;
        $("technical-count").textContent = `${data.summary.count} unique captures`;
        if (window.renderHealth) window.renderHealth(data.health, data.reader, data.job);
        $("stat-count").textContent = data.summary.count;
        const copies = data.summary.copies - data.summary.count;
        $("stat-copies").textContent = copies ? `${copies} duplicate ${copies === 1 ? "copy" : "copies"} grouped` : "Each unique file, counted once";
        $("stat-standalone").textContent = data.summary.standaloneCount;
        $("stat-bytes").textContent = bytes(data.summary.bytes);
        const latest = data.captures[0];
        $("stat-latest").textContent = latest ? date(latest.timestamp) : "No data";
        $("stat-latest-time").textContent = latest ? date(latest.timestamp, {hour: "numeric", minute: "2-digit"}) : "Run your first local read";
        const running = data.job?.status === "running";
        $("device-state").textContent = running ? "Reader running" : data.reader.available ? "Laptop paired" : "Setup needed";
        $("read-button").disabled = running || !data.reader.available;
        $("read-button").classList.toggle("spinning", running);
        $("read-button").textContent = running ? "Reading tracker…" : "⌁  Read tracker";
        $("read-hint").textContent = running ? "Discovery, authentication, and transfer. Up to 60 seconds." : data.reader.available ? `Paired tracker ${data.reader.peerId}. Saves locally over BLE.` : "Build and provision the reader, then pair with standalone/run.py pair --device UUID.";
        $("key-id").textContent = data.reader.keyId || "Not configured";
        renderLibrary();
        drawCaptureChart(data.captures);
        renderJob(data.job);
        if (!state.selected && latest) selectCapture(latest.id);
    } catch (failure) {error(`Cannot load the local dashboard: ${failure.message}`);}
    finally {state.loading = false;}
}

$("refresh-button").addEventListener("click", () => {error(null); refresh();});
$("search-input").addEventListener("input", renderLibrary);
$("type-filter").addEventListener("change", renderLibrary);
document.querySelectorAll("[data-kind]").forEach(button => button.addEventListener("click", () => {
    state.kind = button.dataset.kind;
    document.querySelectorAll("[data-kind]").forEach(item => {
        const selected = item.dataset.kind === state.kind;
        item.classList.toggle("selected", selected);
        item.setAttribute("aria-pressed", String(selected));
    });
}));
$("read-button").addEventListener("click", async () => {
    $("read-button").disabled = true;
    error(null);
    try {
        const job = await api("/api/read", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({kind: state.kind})});
        renderJob(job);
        await refresh();
    } catch (failure) {error(failure.message); await refresh();}
});
$("previous-page").addEventListener("click", () => selectCapture(state.selected, Math.max(0, state.detail.offset - 256)));
$("next-page").addEventListener("click", () => selectCapture(state.selected, state.detail.nextOffset));
document.querySelectorAll(".nav-item").forEach(item => item.addEventListener("click", () => {
    document.querySelectorAll(".nav-item").forEach(link => link.classList.toggle("active", link === item));
}));
refresh();
setInterval(refresh, 2500);
