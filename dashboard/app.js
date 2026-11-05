"use strict";

const $ = (id) => document.getElementById(id);
const formatters = Object.fromEntries(Object.entries(locales).map(([key, locale]) => [key, new Intl.NumberFormat(locale)]));
const number = (value) => Number.isFinite(value) ? formatters[language].format(value) : "—";
let latestSessions = [];
let latestRegistry = {workers: []};
let latestEvents = [];
let streamConnected = false;
let observerToken = new URLSearchParams(location.hash.slice(1)).get("observer") || "";
if (observerToken) {
  history.replaceState(null, "", location.pathname);
  try { sessionStorage.setItem("kontur-observer", observerToken); } catch (_) {}
} else {
  try { observerToken = sessionStorage.getItem("kontur-observer") || ""; } catch (_) {}
}
if (observerToken) {
  for (const selector of ["#fee-log", "#pools", 'a[href="#pools"]', 'a[href="/api/audit-export"]']) {
    document.querySelectorAll(selector).forEach((element) => { element.hidden = true; });
  }
}
let busy = false;
let latestMetrics = null;
let latestStatus = null;
let lastUpdate = null;
let networkState = "connecting";

function duration(seconds) {
  if (!Number.isFinite(seconds)) return "—";
  if (seconds < 60) return `${seconds} ${t("sec")}`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} ${t("min")}`;
  return `${Math.floor(seconds / 3600)} ${t("hr")} ${Math.floor(seconds % 3600 / 60)} ${t("min")}`;
}

function endpoint(value) {
  if (!value) return t("Not configured");
  const host = String(value.host ?? "");
  return `${host.includes(":") ? `[${host}]` : host}:${value.port}`;
}

function cell(row, value, cssClass = "") {
  const td = document.createElement("td");
  td.textContent = value;
  if (cssClass) td.className = cssClass;
  row.appendChild(td);
}

function renderSessions() {
  const search = $("search").value.trim().toLocaleLowerCase(locales[language]);
  const filter = $("filter").value;
  const shown = latestSessions.filter((session) =>
    (filter === "all" || session.state === filter) &&
    String(session.worker || "").toLocaleLowerCase(locales[language]).includes(search));
  const tbody = $("sessions");
  tbody.replaceChildren();
  for (const session of shown) {
    const row = document.createElement("tr");
    const worker = session.worker || t("Waiting for authorization");
    cell(row, worker);
    row.firstChild.title = worker;
    cell(row, `● ${t(session.state === "connected" ? "Connected" : "Connecting")}`,
      session.state === "connected" ? "row-status" : "row-status waiting");
    cell(row, session.pool === "primary" ? t("Primary") : session.pool === "backup" ? t("Backup") : session.pool === "fee" ? "DevFee" : "—");
    cell(row, number(session.accepted));
    cell(row, number(session.rejected));
    cell(row, duration(session.duration_seconds));
    tbody.appendChild(row);
  }
  $("session-count").textContent = number(latestSessions.length);
  const empty = $("empty");
  empty.hidden = shown.length > 0;
  empty.querySelector("b").textContent = t(latestSessions.length ? "Nothing found" : "No connections yet");
  empty.querySelector("p").textContent = t(latestSessions.length ? "Try changing the search or filter." : "Miners connected to the proxy will appear here.");
}

function renderSnapshot() {
  $("connection-status").className = `live-status ${networkState === "connecting" ? "" : networkState}`;
  $("connection-status").replaceChildren(document.createElement("i"), document.createTextNode(` ${t(networkState === "online" ? "Online" : networkState === "offline" ? "Offline" : "Connecting")}`));
  $("last-update").textContent = lastUpdate ? `${t("Updated")} ${lastUpdate.toLocaleTimeString(locales[language])}` : t("Waiting for data");
  if (!latestMetrics || !latestStatus) { $("active-hint").textContent = t("Waiting for data"); renderSessions(); return; }
  const metrics = latestMetrics, status = latestStatus, sessions = latestSessions;
  $("active").textContent = number(metrics.active);
  $("active-hint").textContent = `${t("Limit")}: ${number(status.limit)}`;
  $("accepted").textContent = number(metrics.accepted);
  $("rejected").textContent = number(metrics.rejected);
  $("upstream-failures").textContent = number(metrics.upstream_failures);
  $("connections-total").textContent = number(metrics.connections);
  $("backup-connections").textContent = number(metrics.backup_connections);
  $("pending-unmatched").textContent = `${number(sessions.reduce((sum, item) => sum + (item.pending || 0), 0))} / ${number(metrics.unmatched)}`;
  $("limits-loops").textContent = `${number(metrics.refused_capacity)} / ${number(metrics.blocked_loops)}`;
  $("refused-network").textContent = number(metrics.refused_network);
  $("redirect-notices").textContent = number(metrics.redirect_notices);
  $("idle-timeouts").textContent = number(metrics.idle_timeouts);
  const audit = metrics.audit;
  $("audit-debt").textContent = audit?.outstanding_fee_difficulty ?? "—";
  $("audit-user").textContent = audit ? audit.user_accepted_difficulty + " / " + number(audit.user_accepted_shares) : "—";
  $("audit-fee").textContent = audit ? audit.fee_accepted_difficulty + " / " + number(audit.fee_accepted_shares) : "—";
  $("audit-percent").textContent = audit && Number.isFinite(audit.fee_percentage) ? number(Math.round(audit.fee_percentage * 10000) / 10000) + "%" : "—";
  const rate = audit?.hashrate_windows?.["5m"]?.hashrate_hs;
  $("audit-hashrate").textContent = Number.isFinite(rate) ? number(Math.round(rate)) + " H/s" : "—";
  $("audit-local").textContent = audit ? number(metrics.invalid_local) : "—";
  $("redirect-alert").hidden = !(metrics.redirect_notices > 0);
  $("share-rtt").textContent = metrics.share_rtt_samples > 0
    ? `${number(Math.round(metrics.share_rtt_total_ms / metrics.share_rtt_samples * 10) / 10)} ${t("ms")}` : "—";
  $("primary-pool").textContent = endpoint(status.primary);
  $("backup-pool").textContent = endpoint(status.backup);
  $("tls-label").textContent = t(status.tls ? "TLS · certificate verification enabled" : "TCP · no TLS");
  $("listener").textContent = status.listeners?.join(", ") || "—";
  renderSessions();
  renderWorkers();
  renderEvents();
}

async function refresh() {
  if (busy) return;
  busy = true;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 5000);
  try {
    const response = await fetch("/api/snapshot", {cache: "no-store", signal: controller.signal,
      headers: observerToken ? {Authorization: "Bearer " + observerToken} : {}});
    if (!response.ok) throw new Error("Snapshot unavailable");
    acceptSnapshot(await response.json());
    networkState = "online";
    lastUpdate = new Date();
    $("offline-notice").hidden = true;
    renderSnapshot();
  } catch (_) {
    networkState = "offline";
    $("offline-notice").hidden = false;
    renderSnapshot();
  } finally {
    clearTimeout(timeout);
    busy = false;
  }
}

$("refresh").addEventListener("click", refresh);
$("search").addEventListener("input", renderSessions);
$("filter").addEventListener("change", renderSessions);
document.addEventListener("kontur-language-change", renderSnapshot);
renderSnapshot();
refresh();
const stream = new EventSource("/api/stream" + (observerToken ? "?observer=" + encodeURIComponent(observerToken) : ""));
stream.addEventListener("snapshot", (event) => {
  try {
    acceptSnapshot(JSON.parse(event.data));
    streamConnected = true;
    networkState = "online";
    lastUpdate = new Date();
    $("offline-notice").hidden = true;
    renderSnapshot();
  } catch (_) { streamConnected = false; }
});
stream.addEventListener("stream-error", () => { streamConnected = false; refresh(); });
stream.onerror = () => { streamConnected = false; refresh(); };
window.addEventListener("pagehide", () => stream.close(), {once: true});
setInterval(() => { if (!streamConnected) refresh(); }, 3000);


function acceptSnapshot(snapshot) {
  if (!Array.isArray(snapshot.sessions) || !snapshot.metrics || !snapshot.status ||
      !Array.isArray(snapshot.registry?.workers) || !Array.isArray(snapshot.events)) throw new Error("Invalid snapshot");
  latestSessions = snapshot.sessions;
  latestMetrics = snapshot.metrics;
  latestStatus = snapshot.status;
  latestRegistry = snapshot.registry;
  latestEvents = snapshot.events;
}

function hashrate(value) {
  if (!Number.isFinite(value)) return "—";
  const scales = [[1e18, "EH/s"], [1e15, "PH/s"], [1e12, "TH/s"], [1e9, "GH/s"], [1e6, "MH/s"], [1e3, "kH/s"], [1, "H/s"]];
  const [scale, unit] = scales.find(([limit]) => value >= limit) || scales[scales.length - 1];
  return number(Math.round(value / scale * 100) / 100) + " " + unit;
}

function renderWorkers() {
  const search = $("worker-search").value.trim().toLocaleLowerCase(locales[language]);
  const filter = $("worker-filter").value;
  const shown = latestRegistry.workers.filter((w) => (filter === "all" || w.status === filter) &&
    (w.worker + " " + w.ip).toLocaleLowerCase(locales[language]).includes(search));
  shown.sort((a, b) => a.worker.localeCompare(b.worker, locales[language]) || a.ip.localeCompare(b.ip));
  const rows = $("worker-rows");
  rows.replaceChildren();
  for (const worker of shown) {
    const row = document.createElement("tr");
    cell(row, worker.worker);
    row.firstChild.title = worker.worker;
    cell(row, worker.ip);
    cell(row, "● " + t(worker.status === "online" ? "Online" : "Offline"), worker.status === "online" ? "row-status" : "row-status offline");
    for (const window of ["15m", "1h", "24h"]) {
      const estimate = worker.hashrate_windows?.[window];
      cell(row, hashrate(estimate?.hashrate_hs) + (estimate?.warmup ? " *" : ""));
      row.lastChild.title = estimate ? t("Measured interval") + ": " + duration(Math.floor(estimate.measured_seconds)) : t("Difficulty unavailable in transparent mode");
    }
    for (const key of ["accepted", "rejected", "stale", "invalid_local"]) cell(row, number(worker[key]));
    cell(row, Number.isFinite(worker.pool_response_ms) ? number(Math.round(worker.pool_response_ms * 10) / 10) + " " + t("ms") : "—");
    cell(row, "—");
    row.lastChild.title = t("ASIC ping is not measured");
    rows.appendChild(row);
  }
  $("worker-count").textContent = number(latestRegistry.workers.length);
  $("workers-empty").hidden = shown.length > 0;
  $("worker-limit").hidden = !(latestRegistry.dropped_observations > 0);
  $("stream-label").textContent = t(streamConnected ? "SSE · live" : "Polling · 3 sec");
}

function renderEvents() {
  const rows = $("fee-events");
  rows.replaceChildren();
  for (const event of latestEvents.slice(-50).reverse()) {
    const row = document.createElement("tr");
    const timestamp = new Date(event.timestamp);
    cell(row, Number.isNaN(timestamp.getTime()) ? "—" : timestamp.toLocaleTimeString(locales[language]));
    cell(row, event.worker || "—");
    cell(row, event.target === "fee" ? "DevFee" : t(event.target === "backup" ? "Backup" : "Primary"));
    cell(row, t(event.kind === "assignment" ? "Assignment" : "Switch"));
    rows.appendChild(row);
  }
  $("events-empty").hidden = latestEvents.length > 0;
}

$("worker-search").addEventListener("input", renderWorkers);
$("worker-filter").addEventListener("change", renderWorkers);
