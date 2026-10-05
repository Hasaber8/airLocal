(() => {
    const find = id => document.getElementById(id);
    let health = null;
    let chosenDate = null;
    const ns = "http://www.w3.org/2000/svg";
    const number = value => value === null || value === undefined ? "Not available" : Math.round(value).toLocaleString();
    const clock = time => new Date(time).toLocaleTimeString([], {hour: "numeric", minute: "2-digit"});
    const labelDate = value => new Date(`${value}T12:00:00`).toLocaleDateString([], {month: "short", day: "numeric"});

    function svg(tag, attributes = {}) {
        const node = document.createElementNS(ns, tag);
        for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
        return node;
    }
    function text(chart, x, y, value) {
        const node = svg("text", {x, y, fill: "#91a397", "font-size": 9});
        node.textContent = value;
        chart.append(node);
    }
    function hint(id, value) {
        const node = document.createElement("p");
        node.className = "empty-state";
        node.textContent = value;
        find(id).replaceChildren(node);
    }
    function metric(id, value) {
        find(id).textContent = value;
        find(id).classList.toggle("metric-missing", value === "Not available");
    }

    function render() {
        if (!health) return;
        const samples = health.samples.filter(sample => sample.date === chosenDate);
        const latest = samples.at(-1);
        metric("health-steps", number(latest?.steps));
        metric("health-calories", number(latest?.calories));
        metric("health-distance", latest?.distanceKm === null || latest?.distanceKm === undefined ? "Not available" : latest.distanceKm.toFixed(2));
        metric("health-zone", number(latest?.activeZoneMinutes));
        const source = latest ? `BLE reading at ${clock(latest.timestamp)}` : "No BLE reading recorded on this date";
        for (const id of ["steps-context", "calories-context", "distance-context"]) find(id).textContent = source;
        find("zone-context").textContent = latest?.weeklyZoneMinutes !== null && latest?.weeklyZoneMinutes !== undefined ? `${number(latest.weeklyZoneMinutes)} this week · BLE` : source;
        const heart = samples.filter(sample => sample.heartRate !== null && sample.heartRate !== undefined);
        const lastHeart = heart.at(-1);
        metric("health-heart", number(lastHeart?.heartRate));
        find("heart-time").textContent = lastHeart ? clock(lastHeart.timestamp) : "";
        find("heart-source").textContent = lastHeart ? "BLE MEASUREMENT" : "NO READING";
        find("heart-source").className = "badge source-live";
        find("heart-context").textContent = lastHeart ? `Measured directly by the tracker. Signal confidence: ${lastHeart.heartRateConfidenceLabel}.` : "Record BLE readings to build your heart-rate chart.";
        find("heart-coverage").textContent = `${heart.length} BLE measurements recorded on this Mac`;
        find("resting-heart").textContent = "Resting heart rate is not provided by LiveActivity";
        drawHeart(heart);
        drawSteps(health.history.days);
        find("history-updated").textContent = "Only our own BLE captures are included. Daily counters use the last captured total, not a sum of repeated reads.";
    }

    function drawHeart(points) {
        if (!points.length) {hint("heart-chart", "No BLE heart-rate measurements recorded for this date."); return;}
        const chart = svg("svg", {viewBox: "0 0 600 170", role: "img", "aria-label": "Heart-rate measurements from BLE"});
        const minimum = Math.max(0, Math.floor(Math.min(...points.map(point => point.heartRate)) / 20) * 20 - 20);
        const maximum = Math.ceil(Math.max(...points.map(point => point.heartRate)) / 20) * 20 + 20;
        const start = points[0].timestamp;
        const end = Math.max(points.at(-1).timestamp, start + 60_000);
        const x = point => points.length === 1 ? 315 : 42 + (point.timestamp - start) / (end - start) * 542;
        const y = point => 142 - (point.heartRate - minimum) / (maximum - minimum) * 120;
        for (let i = 0; i < 4; i++) {
            const py = 142 - i * 40;
            chart.append(svg("line", {x1: 40, x2: 587, y1: py, y2: py, stroke: "#e9efeb", "stroke-dasharray": "3 4"}));
            text(chart, 0, py + 3, Math.round(minimum + (maximum - minimum) * i / 3));
        }
        let segment = [];
        const flush = () => {
            if (segment.length > 1) chart.append(svg("polyline", {points: segment.map(point => `${x(point)},${y(point)}`).join(" "), stroke: "#8aaf9d", fill: "none", "stroke-width": 1.7}));
            segment = [];
        };
        for (const point of points) {
            if (segment.length && point.timestamp - segment.at(-1).timestamp > 300_000) flush();
            segment.push(point);
            const circle = svg("circle", {cx: x(point), cy: y(point), r: points.length > 100 ? 1.5 : 3, fill: "#126b54"});
            const title = svg("title"); title.textContent = `${clock(point.timestamp)} · ${point.heartRate} bpm · direct BLE`;
            circle.append(title); chart.append(circle);
        }
        flush();
        text(chart, 42, 165, clock(start)); text(chart, 535, 165, clock(points.at(-1).timestamp));
        find("heart-chart").replaceChildren(chart);
    }

    function drawSteps(days) {
        const entries = days.filter(day => day.steps !== null && day.steps !== undefined).slice(-7);
        if (!entries.length) {hint("steps-chart", "Your BLE readings will build a daily history here."); return;}
        const chart = svg("svg", {viewBox: "0 0 380 215", role: "img", "aria-label": "Daily totals recorded over BLE"});
        const maximum = Math.max(...entries.map(day => day.steps), 1);
        const width = 310 / entries.length;
        entries.forEach((day, index) => {
            const height = day.steps / maximum * 145;
            const bar = svg("rect", {x: 43 + index * width, y: 176 - height, width: width * .57, height, rx: 4, fill: day.date === chosenDate ? "#1f7a59" : "#c6dfd0"});
            const title = svg("title"); title.textContent = `${day.date}: ${number(day.steps)} steps · last BLE capture`; bar.append(title); chart.append(bar);
            text(chart, 43 + index * width, 194, labelDate(day.date)); text(chart, 43 + index * width, 169 - height, number(day.steps));
        });
        find("steps-chart").replaceChildren(chart);
    }

    window.renderHealth = (data, reader, job) => {
        health = data || {history: {days: []}, samples: [], live: null, today: new Date().toLocaleDateString("en-CA")};
        const dates = [...new Set([health.today, ...health.samples.map(sample => sample.date)])].sort().reverse();
        if (!chosenDate) chosenDate = health.live?.date || health.today;
        const picker = find("health-date"); picker.replaceChildren();
        dates.forEach(day => {const option = document.createElement("option"); option.value = day; option.textContent = `${day === health.today ? "Today · " : ""}${labelDate(day)}`; option.selected = day === chosenDate; picker.append(option);});
        const running = job?.status === "running";
        find("live-button").disabled = running || !reader.available;
        find("record-button").disabled = running || !reader.available;
        find("live-button").textContent = running && job.kind === "live" ? "Reading measurements…" : "↻ Refresh tracker";
        find("record-button").textContent = running && job.kind === "record" ? `Recording ${job.samplesRecorded || 0}/12…` : "Record one minute";
        find("health-updated").textContent = health.live ? `Last BLE measurement: ${new Date(health.live.timestamp).toLocaleString()}` : "Refresh the tracker to collect your first BLE measurement.";
        if (!reader.available) find("health-updated").textContent = "Tracker setup required. Build and provision the reader, then run standalone/run.py pair --device UUID from a fresh scan.";
        find("health-source").textContent = "BLE-only. Every health value comes from this reader's direct tracker captures. No Fitbit app, phone database, or imported history is used.";
        render();
    };
    find("health-date").addEventListener("change", event => {chosenDate = event.target.value; render();});
    async function action(kind) {
        find("live-button").disabled = true; find("record-button").disabled = true;
        error(null);
        try {
            const response = await fetch("/api/read", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({kind})});
            const result = await response.json();
            if (!response.ok) throw new Error(result.error || "Request failed");
            renderJob(result); await refresh();
        } catch (failure) {error(failure.message); await refresh();}
    }
    find("live-button").addEventListener("click", () => action("live"));
    find("record-button").addEventListener("click", () => action("record"));
})();
