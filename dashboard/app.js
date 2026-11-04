"use strict";

const $ = (id) => document.getElementById(id);
const number = (value) => Number.isFinite(value) ? new Intl.NumberFormat(locales[language]).format(value) : "—";
let latestSessions = [];
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
    cell(row, session.pool === "primary" ? t("Primary") : session.pool === "backup" ? t("Backup") : "—");
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
  $("redirect-alert").hidden = !(metrics.redirect_notices > 0);
  $("share-rtt").textContent = metrics.share_rtt_samples > 0
    ? `${number(Math.round(metrics.share_rtt_total_ms / metrics.share_rtt_samples * 10) / 10)} ${t("ms")}` : "—";
  $("primary-pool").textContent = endpoint(status.primary);
  $("backup-pool").textContent = endpoint(status.backup);
  $("tls-label").textContent = t(status.tls ? "TLS · certificate verification enabled" : "TCP · no TLS");
  $("listener").textContent = status.listeners?.join(", ") || "—";
  renderSessions();
}

async function refresh() {
  if (busy) return;
  busy = true;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 5000);
  try {
    const responses = await Promise.all(["/api/metrics", "/api/sessions", "/api/status"].map((path) =>
      fetch(path, {cache: "no-store", signal: controller.signal})));
    if (responses.some((response) => !response.ok)) throw new Error("Не удалось получить данные");
    const [metrics, sessions, status] = await Promise.all(responses.map((response) => response.json()));
    if (!Array.isArray(sessions)) throw new Error("Неверные данные соединений");
    latestSessions = sessions;
    latestMetrics = metrics;
    latestStatus = status;
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
setInterval(refresh, 3000);
