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
  try { sessionStorage.setItem("chefas-proxy-observer", observerToken); sessionStorage.removeItem("chefas-proxy-admin"); } catch (_) {}
} else {
  try { observerToken = sessionStorage.getItem("chefas-proxy-observer") || ""; } catch (_) {}
}
let adminToken = "";
try { adminToken = sessionStorage.getItem("chefas-proxy-admin") || ""; } catch (_) {}
let accessRole = observerToken ? "observer" : "public";
let routeConfig = null;
let streamController = null;
const authHeaders = () => observerToken || adminToken ? {Authorization: "Bearer " + (observerToken || adminToken)} : {};
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
  if (host.includes("://")) {
    try {
      const url = new URL(host);
      return `${url.protocol}//${url.hostname}:${value.port}`;
    } catch (_) { return host; }
  }
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
  $("kpi-hashrate").textContent = hashrate(audit?.hashrate_windows?.["5m"]?.hashrate_hs);
  $("kpi-workers").textContent = number(latestRegistry.workers.filter(w => w.status === "online").length);
  const debt = audit?.outstanding_fee_difficulty ?? "—";
  $("kpi-fee-debt").textContent = debt.length > 24 ? debt.slice(0, 21) + "…" : debt;
  $("kpi-fee-debt").title = debt;
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
  $("tls-label").textContent = t((status.primary?.tls ?? status.tls) ? "TLS · certificate verification enabled" : "TCP · no TLS");
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
      headers: authHeaders()});
    if (response.status === 403 && !observerToken && !$("login-dialog").open) $("login-dialog").showModal();
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
document.addEventListener("chefas-proxy-language-change", renderSnapshot);
renderSnapshot();
bootAccess();
window.addEventListener("pagehide", () => streamController?.abort());
window.addEventListener("pageshow", (event) => { if (event.persisted) restartStream(); });
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
    cell(row, t(worker.status === "online" ? "Online" : "Offline"), worker.status === "online" ? "worker-status online" : "worker-status offline");
    for (const window of ["15m", "1h", "24h"]) {
      const estimate = worker.hashrate_windows?.[window];
      cell(row, hashrate(estimate?.hashrate_hs) + (estimate?.warmup ? " *" : ""));
      row.lastChild.title = estimate ? t("Measured interval") + ": " + duration(Math.floor(estimate.measured_seconds)) : t("Difficulty unavailable in transparent mode");
    }
    for (const key of ["accepted", "rejected", "stale", "invalid_local"]) cell(row, number(worker[key]));
    cell(row, Number.isFinite(worker.pool_response_ms) ? number(Math.round(worker.pool_response_ms * 10) / 10) + " " + t("ms") : "—");
    cell(row, Number.isFinite(worker.notify_to_first_share_ema_ms) ? number(Math.round(worker.notify_to_first_share_ema_ms)) + " " + t("ms") : "—");
    row.lastChild.title = t("Notify to first accepted share; includes mining time, not network RTT");
    if (accessRole === "admin") {
      const actions = document.createElement("td");
      const wrap = document.createElement("div"); wrap.className = "worker-actions";
      for (const [label, path, data] of [["Kick", "/api/workers/kick", {worker_id: worker.id}], ["Ban IP", "/api/blacklist", {add: worker.ip}]]) {
        const button = document.createElement("button"); button.type = "button"; button.textContent = t(label);
        button.addEventListener("click", async () => {
          button.disabled = true;
          try { await api(path, data); await loadBlacklist(); await refresh(); managementMessage(t("Applied")); }
          catch (error) { managementMessage(error.message); }
          finally { button.disabled = false; }
        });
        wrap.appendChild(button);
      }
      actions.appendChild(wrap); row.appendChild(actions);
    }
    rows.appendChild(row);
  }
  const persistence = latestRegistry.persistence;
  $("history-status").textContent = t(persistence?.error ? "History checkpoint failed" : persistence?.loaded ? "History restored" : persistence?.enabled ? "History persistence enabled" : "History in memory");
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


function renderAccess() {
  document.body.classList.toggle("observer-view", accessRole === "observer");
  document.querySelectorAll(".admin-only").forEach((node) => { node.hidden = accessRole !== "admin"; });
  $("logout").hidden = !(observerToken || adminToken);
}

async function api(path, data, headers = authHeaders()) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 20000);
  try {
    const response = await fetch(path, {method: data === undefined ? "GET" : "POST", cache: "no-store",
      headers: {...headers, ...(data === undefined ? {} : {"Content-Type": "application/json"})},
      body: data === undefined ? undefined : JSON.stringify(data), signal: controller.signal});
    const result = await response.json();
    if (!response.ok) throw new Error(t(result.error || "Request failed"));
    return result;
  } finally { clearTimeout(timeout); }
}

function managementMessage(message) { $("management-result").textContent = message; }

async function bootAccess() {
  try {
    const access = await api("/api/access");
    accessRole = access.role;
    renderAccess();
    if (accessRole === "unauthenticated" && !observerToken && !$("login-dialog").open) $("login-dialog").showModal();
    if (accessRole === "admin") await Promise.all([loadConfig(), loadBlacklist()]);
  } catch (_) { accessRole = "public"; renderAccess(); }
  await refresh();
  restartStream();
}

function restartStream() {
  streamController?.abort();
  const controller = new AbortController();
  streamController = controller;
  streamConnected = false;
  runStream(controller);
}

async function runStream(controller) {
  while (!controller.signal.aborted) {
    let heartbeat;
    try {
      let lastFrame = Date.now();
      heartbeat = setInterval(() => { if (Date.now() - lastFrame > 10000) controller.abort(); }, 1000);
      const response = await fetch("/api/stream", {cache: "no-store", headers: authHeaders(), signal: controller.signal});
      if (!response.ok || !response.body) throw new Error("Stream unavailable");
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      try {
        while (!controller.signal.aborted) {
          const part = await reader.read();
          if (part.done) break;
          buffer += decoder.decode(part.value, {stream: true});
          if (buffer.length > 2200000) throw new Error("Stream frame too large");
          let end;
          while ((end = buffer.indexOf("\n\n")) >= 0) {
            const frame = buffer.slice(0, end); buffer = buffer.slice(end + 2);
            if (!frame.includes("event: snapshot")) continue;
            const data = frame.split("\n").find((line) => line.startsWith("data: "));
            if (!data) continue;
            acceptSnapshot(JSON.parse(data.slice(6)));
            lastFrame = Date.now(); streamConnected = true; networkState = "online";
            lastUpdate = new Date(); $("offline-notice").hidden = true; renderSnapshot();
          }
        }
      } finally { await reader.cancel().catch(() => {}); }
    } catch (_) { /* polling keeps the last valid data available */ }
    finally { clearInterval(heartbeat); if (streamController === controller) streamConnected = false; }
    if (!controller.signal.aborted) await new Promise((resolve) => setTimeout(resolve, 3000));
  }
  if (streamController === controller && controller.signal.aborted && !document.hidden) {
    setTimeout(() => { if (streamController === controller) restartStream(); }, 3000);
  }
}

async function loadConfig() {
  routeConfig = await api("/api/config");
  $("tp-mode").checked = !!routeConfig.tp_mode;
  $("tp-form").style.display = routeConfig.fee_locked ? "none" : "";
  $("fee-enabled").closest("label").style.display = routeConfig.fee_locked ? "none" : "";
  $("fee-fields").style.display = routeConfig.fee_locked ? "none" : "";
  for (const [prefix, route] of [["user", routeConfig.user_pool], ["fee", routeConfig.devfee_pool]]) {
    $(prefix + "-host").value = route?.host || "";
    $(prefix + "-port").value = route?.port || "";
    $(prefix + "-worker").value = route?.worker || "";
    $(prefix + "-password").value = "";
  }
  $("fee-enabled").checked = !!routeConfig.devfee_pool;
  $("fee-enabled").disabled = routeConfig.require_devfee;
  $("fee-fields").disabled = !$("fee-enabled").checked;
}

async function loadBlacklist() {
  if (accessRole !== "admin") return;
  const result = await api("/api/blacklist");
  const rows = $("blacklist-rows"); rows.replaceChildren();
  for (const address of result.blacklist) {
    const row = document.createElement("tr"); cell(row, address);
    const action = document.createElement("td"); const button = document.createElement("button");
    button.type = "button"; button.textContent = t("Remove");
    button.onclick = async () => {
      try { await api("/api/blacklist", {remove: address}); await loadBlacklist(); }
      catch (error) { managementMessage(error.message); }
    };
    action.appendChild(button); row.appendChild(action); rows.appendChild(row);
  }
}

$("login-open").onclick = () => { if (!$("login-dialog").open) $("login-dialog").showModal(); };
$("login-cancel").onclick = () => $("login-dialog").close();
$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const candidate = $("admin-token").value;
  try {
    const access = await api("/api/access", undefined, {Authorization: "Bearer " + candidate});
    if (access.role !== "admin") throw new Error(t("Invalid admin token"));
    adminToken = candidate; observerToken = "";
    try { sessionStorage.setItem("chefas-proxy-admin", candidate); sessionStorage.removeItem("chefas-proxy-observer"); } catch (_) {}
    $("admin-token").value = ""; $("login-error").textContent = ""; $("login-dialog").close();
    await bootAccess();
  } catch (error) { $("login-error").textContent = error.message; }
});
$("logout").onclick = () => {
  streamController?.abort();
  try { sessionStorage.removeItem("chefas-proxy-admin"); sessionStorage.removeItem("chefas-proxy-observer"); } catch (_) {}
  location.reload();
};
$("fee-enabled").onchange = () => { $("fee-fields").disabled = !$("fee-enabled").checked; };
$("route-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!routeConfig) return;
  const route = (prefix) => {
    const value = {host: $(prefix + "-host").value.trim(), port: Number($(prefix + "-port").value), worker: $(prefix + "-worker").value};
    if ($(prefix + "-password").value) value.password = $(prefix + "-password").value;
    return value;
  };
  try {
    await api("/api/config", {generation: routeConfig.generation, user_pool: route("user"),
      ...(!routeConfig?.fee_locked && {devfee_pool: $("fee-enabled").checked ? route("fee") : null}), reconnect: $("route-reconnect").checked});
    await loadConfig(); managementMessage(t("Routes saved"));
  } catch (error) { managementMessage(error.message); }
});
$("ban-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try { await api("/api/blacklist", {add: $("ban-address").value.trim()}); $("ban-address").value = ""; await loadBlacklist(); }
  catch (error) { managementMessage(error.message); }
});
$("observer-create").onclick = async () => {
  try {
    const result = await api("/api/observer-links", {});
    $("observer-link").value = location.origin + location.pathname + result.fragment;
    $("observer-link").hidden = false;
  } catch (error) { managementMessage(error.message); }
};

for (const link of document.querySelectorAll('a[href="/api/audit-export"]')) {
  link.addEventListener("click", async (event) => {
    event.preventDefault();
    try {
      const report = await api("/api/audit-export");
      const url = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)], {type: "application/json"}));
      const download = document.createElement("a"); download.href = url; download.download = "chefas-proxy-diagnostics.json";
      download.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) { managementMessage(error.message); }
  });
}

$("route-reload").onclick = async () => {
  try { await loadConfig(); managementMessage(t("Routes reloaded")); }
  catch (error) { managementMessage(error.message); }
};

$("tp-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await api("/api/mode", {tp_mode: $("tp-mode").checked});
    $("tp-result").textContent = "Applied to new connections. Existing sessions retain their current mode.";
  } catch (error) {
    $("tp-result").textContent = error.message;
  }
});
