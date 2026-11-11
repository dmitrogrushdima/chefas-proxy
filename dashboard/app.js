"use strict";

const $ = (id) => document.getElementById(id);
const formatters = Object.fromEntries(Object.entries(locales).map(([key, locale]) => [key, new Intl.NumberFormat(locale)]));
const number = (value) => Number.isFinite(value) ? formatters[language].format(value) : "—";
let latestSessions = [];
let latestRegistry = {workers: []};
let latestEvents = [];
let connectionEvents = [];
let renderedConnectionHistory = "";
let renderedAttention = "";
let feeTestBusy = false;
let snapshotTime = NaN;
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
// Farm operators also sign in as admin. Only the private test listener
// advertises manual-fee controls and the combined VPS view.
const serviceAdmin = () => accessRole === "admin" && !!routeConfig?.fee_test && !routeConfig?.fee_locked;
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

const connectionStages = {
  tcp_connected: "Waiting for authorization",
  authorized: "Authorized, waiting for job",
  authorization_rejected: "Pool rejected authorization",
  job_sent: "Waiting for first accepted share",
  share_accepted: "Pool accepted shares",
  upstream_lost: "Pool connection lost",
  upstream_unavailable: "Pool unavailable",
  disconnected: "ASIC disconnected",
};

const connectionFailures = new Set(["authorization_rejected", "upstream_lost", "upstream_unavailable"]);
const attentionWindowMs = 5 * 60 * 1000;

function connectionIssues(sessions, events, now) {
  const active = new Set();
  const issues = [];
  for (const session of sessions) {
    active.add(session.id);
    const diagnostic = session.diagnostic;
    if (!diagnostic) continue;
    const {stage, changed_at_utc, last_accepted_utc, pool} = diagnostic;
    const stalled = ["job_sent", "share_accepted"].includes(stage) &&
      now - Date.parse(last_accepted_utc || changed_at_utc) >= attentionWindowMs;
    if (connectionFailures.has(stage) || stalled) {
      issues.push({id: session.id, worker: session.worker || diagnostic.worker, pool,
        reason: stalled ? "No accepted shares for 5 minutes" : connectionStages[stage],
        timestamp: stalled ? last_accepted_utc || changed_at_utc : changed_at_utc});
    }
  }

  const seen = new Set();
  for (let index = events.length - 1; index >= 0; index--) {
    const event = events[index];
    if (active.has(event.session_id) || seen.has(event.session_id) || event.stage === "disconnected") continue;
    seen.add(event.session_id);
    const age = now - Date.parse(event.timestamp);
    if (age >= 0 && age < attentionWindowMs && connectionFailures.has(event.stage)) {
      issues.push({id: event.session_id, worker: event.worker, pool: event.pool,
        reason: connectionStages[event.stage], timestamp: event.timestamp, recent: true});
    }
  }
  return issues;
}

function renderConnectionAttention() {
  const panel = $("connection-attention");
  const issues = accessRole === "observer" || networkState !== "online" ? [] :
    connectionIssues(latestSessions, connectionEvents, snapshotTime);
  panel.hidden = !issues.length;
  const version = language + JSON.stringify(issues);
  if (version === renderedAttention) return;
  renderedAttention = version;
  if (!issues.length) panel.open = false;
  $("attention-count").textContent = number(issues.length);
  const rows = $("attention-items");
  rows.replaceChildren();
  for (const issue of issues) {
    const item = document.createElement("li");
    const name = document.createElement("strong");
    name.textContent = issue.worker || t("Session") + " #" + number(issue.id);
    const reason = document.createElement("span");
    reason.textContent = t(issue.reason) + (issue.recent ? " · " + t("Recent session") : "");
    const context = document.createElement("small");
    context.textContent = [issue.pool, eventTime(issue.timestamp)].filter(Boolean).join(" · ");
    item.append(name, reason, context);
    rows.appendChild(item);
  }
}

function eventTime(timestamp) {
  const date = new Date(timestamp || "");
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString(locales[language]);
}

function renderConnectionEvents() {
  const history = $("connection-log");
  history.hidden = accessRole === "observer" || !connectionEvents.length;
  const rows = $("connection-events");
  if (history.hidden) {
    rows.replaceChildren();
    renderedConnectionHistory = "";
    return;
  }
  if (!history.open) return;
  const recent = connectionEvents.slice(-30);
  const version = language + JSON.stringify(recent);
  if (version === renderedConnectionHistory) return;
  renderedConnectionHistory = version;
  rows.replaceChildren();
  for (const event of recent.reverse()) {
    const row = document.createElement("tr");
    cell(row, eventTime(event.timestamp));
    cell(row, number(event.session_id));
    cell(row, event.worker || "—");
    cell(row, event.pool || "—");
    cell(row, t(connectionStages[event.stage] || event.stage));
    rows.appendChild(row);
  }
}

let pilotSnapshot = null;
let pilotRefreshing = false;
let pilotSeen = false;

async function refreshPilotSnapshot() {
  if (!serviceAdmin()) { pilotSnapshot = null; return; }
  if (pilotRefreshing || document.hidden) return;
  pilotRefreshing = true;
  try {
    const reply = await fetch("/api/pilot-3333", {
      headers: authHeaders(), cache: "no-store", signal: AbortSignal.timeout(4000)
    });
    const snapshot = reply.ok ? await reply.json() : null;
    const workers = snapshot?.registry?.workers ?? snapshot?.workers;
    pilotSnapshot = Array.isArray(snapshot?.sessions) && Array.isArray(workers)
      ? {sessions: snapshot.sessions, workers} : null;
    pilotSeen ||= !!pilotSnapshot;
  } catch (_) {
    pilotSnapshot = null;
  } finally {
    pilotRefreshing = false;
    renderSnapshot();
  }
}
setInterval(refreshPilotSnapshot, 3000);

function renderSessions() {
  const pilot = serviceAdmin() ? pilotSnapshot : null;
  const sessions = pilot ? [...latestSessions.map(session => ({...session, port: 3334})),
    ...pilot.sessions.map(session => ({...session, port: 3333}))] : latestSessions;
  const search = $("search").value.trim().toLocaleLowerCase(locales[language]);
  const filter = $("filter").value;
  const shown = sessions.filter((session) =>
    (filter === "all" || session.state === filter) &&
    (String(session.worker || session.diagnostic?.worker || "") + " " + (session.port ?? ""))
      .toLocaleLowerCase(locales[language]).includes(search));
  const tbody = $("sessions");
  tbody.replaceChildren();
  for (const session of shown) {
    const row = document.createElement("tr");
    const worker = session.worker || session.diagnostic?.worker || t("Waiting for authorization");
    cell(row, session.port ? worker + " · " + session.port : worker);
    row.firstChild.title = worker;
    const diagnostic = session.diagnostic;
    const stage = diagnostic?.stage;
    const active = stage === "share_accepted" || (session.state === "connected" && (!stage || (stage === "job_sent" && session.accepted > 0)));
    const label = active && stage === "job_sent" ? "Connected" : connectionStages[stage] || (session.state === "connected" ? "Connected" : "Connecting");
    cell(row, "● " + t(label), active ? "row-status" : "row-status waiting");
    const route = session.pool === "primary" ? t("Primary") : session.pool === "backup" ? t("Backup") : session.pool === "fee" ? "DevFee" : "—";
    cell(row, diagnostic?.pool ? route + " · " + diagnostic.pool : route);
    cell(row, eventTime(diagnostic?.last_session_accepted_utc || diagnostic?.last_accepted_utc));
    cell(row, number(session.accepted));
    cell(row, number(session.rejected));
    cell(row, duration(session.duration_seconds));
    tbody.appendChild(row);
  }
  $("session-count").textContent = number(sessions.length);
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
  const pilot = accessRole === "admin" ? pilotSnapshot : null;
  const combined = accessRole === "admin" && pilotSeen;
  const overviewSessions = pilot ? [...sessions, ...pilot.sessions] : sessions;
  const scope = $("overview-scope");
  scope.hidden = !(accessRole === "admin" && pilotSeen);
  scope.textContent = t(pilot ? "Overview includes ports 3333 and 3334." : "Port 3333 data unavailable. Overview shows port 3334 only.");
  renderRouteDiagnostics(status);
  $("active").textContent = number(combined ? overviewSessions.length : metrics.active);
  $("active-hint").textContent = combined ? t("Current session data") : `${t("Limit")}: ${number(status.limit)}`;
  for (const field of ["accepted", "rejected"]) {
    $(field).textContent = number(combined ? overviewSessions.reduce((sum, session) => sum + (session[field] || 0), 0) : metrics[field]);
  }
  $("accepted-scope").textContent = t(combined ? "Current session data" : "Since proxy start");
  $("rejected-scope").textContent = t(combined ? "Current session data" : "From pool responses");
  $("upstream-failure-hint").textContent = t("While connecting to a pool") +
    (accessRole === "admin" && pilotSeen ? " · 3334" : "");
  $("upstream-failures").textContent = number(metrics.upstream_failures);
  $("connections-total").textContent = number(metrics.connections);
  $("backup-connections").textContent = number(metrics.backup_connections);
  $("pending-unmatched").textContent = `${number(sessions.reduce((sum, item) => sum + (item.pending || 0), 0))} / ${number(metrics.unmatched)}`;
  $("limits-loops").textContent = `${number(metrics.refused_capacity)} / ${number(metrics.blocked_loops)}`;
  $("refused-network").textContent = number(metrics.refused_network);
  $("redirect-notices").textContent = number(metrics.redirect_notices);
  $("idle-timeouts").textContent = number(metrics.idle_timeouts);
  const audit = metrics.audit;
  const onlineWorkers = [...latestRegistry.workers, ...(pilot?.workers || [])].filter(worker => worker.status === "online");
  const estimates = onlineWorkers.map(worker => worker.hashrate_windows?.["15m"])
    .filter(estimate => Number.isFinite(estimate?.hashrate_hs));
  const totalRate = estimates.length ? estimates.reduce((sum, estimate) => sum + estimate.hashrate_hs, 0) : null;
  $("kpi-hashrate").textContent = hashrate(totalRate) + (estimates.length && (estimates.length !== onlineWorkers.length || estimates.some(estimate => estimate.warmup)) ? " *" : "");
  $("kpi-workers").textContent = number(onlineWorkers.length);
  const debt = String(audit?.outstanding_fee_difficulty ?? "—");
  const feeShare = routeFeeShare((pilot?.workers || []).filter(worker => worker.status === "online"));
  $("kpi-fee-label").textContent = t(combined ? "DevFee share 1h" : "DevFee debt");
  $("kpi-fee-hint").textContent = combined ? "3333 · " + t("Accepted difficulty · target 1%") : t("Accepted difficulty");
  $("kpi-fee-debt").textContent = combined ? formatFeeShare(feeShare) : debt.length > 24 ? debt.slice(0, 21) + "…" : debt;
  $("kpi-fee-debt").title = combined ? "" : debt;
  $("audit-debt").textContent = audit?.outstanding_fee_difficulty ?? "—";
  $("audit-user").textContent = audit ? audit.user_accepted_difficulty + " / " + number(audit.user_accepted_shares) : "—";
  $("audit-fee").textContent = audit ? audit.fee_accepted_difficulty + " / " + number(audit.fee_accepted_shares) : "—";
  $("audit-percent").textContent = audit && Number.isFinite(audit.fee_percentage) ? number(Math.round(audit.fee_percentage * 10000) / 10000) + "%" : "—";
  const localRates = latestRegistry.workers.filter(worker => worker.status === "online")
    .map(worker => worker.hashrate_windows?.["15m"]).filter(window => Number.isFinite(window?.hashrate_hs));
  $("audit-hashrate").textContent = hashrate(localRates.length ? localRates.reduce((sum, window) => sum + window.hashrate_hs, 0) : null) +
    (localRates.some(window => window.warmup) ? " *" : "");
  $("audit-local").textContent = audit ? number(metrics.invalid_local) : "—";
  $("redirect-alert").hidden = !(metrics.redirect_notices > 0);
  $("share-rtt").textContent = metrics.share_rtt_samples > 0
    ? `${number(Math.round(metrics.share_rtt_total_ms / metrics.share_rtt_samples * 10) / 10)} ${t("ms")}` : "—";
  $("primary-pool").textContent = endpoint(status.primary);
  $("backup-pool").textContent = endpoint(status.backup);
  $("tls-label").textContent = t((status.primary?.tls ?? status.tls) ? "TLS · certificate verification enabled" : "TCP · no TLS");
  $("listener").textContent = status.listeners?.join(", ") || "—";
  renderSessions();
  renderConnectionAttention();
  renderFeeTest();
  renderWorkers();
  renderEvents();
  renderConnectionEvents();
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
    networkState = streamConnected ? "online" : "offline";
    $("offline-notice").hidden = streamConnected;
    renderSnapshot();
  } finally {
    clearTimeout(timeout);
    busy = false;
  }
}

$("refresh").addEventListener("click", refresh);
$("search").addEventListener("input", renderSessions);
$("filter").addEventListener("change", renderSessions);
$("connection-log").addEventListener("toggle", renderConnectionEvents);
document.addEventListener("chefas-proxy-language-change", renderSnapshot);
renderSnapshot();
bootAccess();
window.addEventListener("pagehide", () => streamController?.abort());
window.addEventListener("pageshow", (event) => { if (event.persisted) restartStream(); });
document.addEventListener("visibilitychange", () => {
  if (document.hidden) streamController?.abort();
  else {
    restartStream();
    refresh();
  }
});
setInterval(() => { if (!document.hidden && !streamConnected) refresh(); }, 3000);



function acceptSnapshot(snapshot) {
  if (!Array.isArray(snapshot.sessions) || !snapshot.metrics || !snapshot.status ||
      !Array.isArray(snapshot.registry?.workers) || !Array.isArray(snapshot.events)) throw new Error("Invalid snapshot");
  snapshotTime = Date.parse(snapshot.generated_at_utc);
  latestSessions = snapshot.sessions;
  latestMetrics = snapshot.metrics;
  latestStatus = snapshot.status;
  latestRegistry = snapshot.registry;
  latestEvents = snapshot.events;
  connectionEvents = Array.isArray(snapshot.diagnostic_events) ? snapshot.diagnostic_events : [];
}

function hashrate(value) {
  if (!Number.isFinite(value)) return "—";
  const scales = [[1e18, "EH/s"], [1e15, "PH/s"], [1e12, "TH/s"], [1e9, "GH/s"], [1e6, "MH/s"], [1e3, "kH/s"], [1, "H/s"]];
  const [scale, unit] = scales.find(([limit]) => value >= limit) || scales[scales.length - 1];
  return number(Math.round(value / scale * 100) / 100) + " " + unit;
}

function routeFeeShare(workers) {
  let fee = 0, total = 0, warmup = false;
  for (const worker of workers) for (const [route, data] of Object.entries(worker.route_diagnostics || {})) {
    const window = data.hashrate_windows?.["1h"];
    const work = (window?.hashrate_hs || 0) * (window?.measured_seconds || 0);
    total += work;
    if (route === "fee") fee += work;
    warmup ||= !!window?.warmup;
  }
  return total > 0 ? {percent: 100 * fee / total, warmup} : null;
}

function formatFeeShare(share) {
  return share ? number(Math.round(share.percent * 100) / 100) + "%" + (share.warmup ? " *" : "") : "—";
}

function renderRouteDiagnostics(status) {
  const section = $("route-health");
  if (!serviceAdmin()) {
    section.hidden = true;
    $("route-health-rows").replaceChildren();
    return;
  }
  const pilot = serviceAdmin() ? pilotSnapshot : null;
  const sources = [[pilot ? 3334 : status.listeners?.join(", "), latestRegistry.workers], [3333, pilot?.workers || []]];
  const rows = $("route-health-rows");
  rows.replaceChildren();
  for (const [port, workers] of sources) for (const worker of workers) {
    if (worker.status !== "online") continue;
    const routes = Object.entries(worker.route_diagnostics || {});
    const feeShare = routeFeeShare([worker]);
    for (const [route, data] of routes) {
      const row = document.createElement("tr");
      const window = data.hashrate_windows?.["1h"];
      const metric = name => {
        const sample = data.timing?.[name];
        return sample ? number(Math.round(sample.mean_ms * 10) / 10) + " " + t("ms") + " (" + number(sample.samples) + ")" : "—";
      };
      cell(row, [worker.worker, port, route === "fee" ? "DevFee" : t(route === "backup" ? "Backup" : "Primary")].filter(Boolean).join(" · "));
      cell(row, hashrate(window?.hashrate_hs) + (window?.warmup ? " *" : ""));
      cell(row, route === "fee" ? formatFeeShare(feeShare) : "—");
      cell(row, ["accepted", "rejected", "stale"].map(key => number(data.counts[key] || 0)).join(" / "));
      for (const key of ["validation_ms", "socket_ms", "reply_ms", "after_fee_ms", "primary_gap_ms"]) cell(row, metric(key));
      rows.appendChild(row);
    }
  }
  section.hidden = !rows.children.length;
}

function renderWorkers() {
  const pilot = serviceAdmin() ? pilotSnapshot : null;
  const workers = pilot ? [...latestRegistry.workers.map(worker => ({...worker, port: 3334})),
    ...pilot.workers.map(worker => ({...worker, port: 3333}))] : latestRegistry.workers;
  const search = $("worker-search").value.trim().toLocaleLowerCase(locales[language]);
  const filter = $("worker-filter").value;
  const shown = workers.filter((w) => (filter === "all" || w.status === filter) &&
    (w.worker + " " + w.ip + " " + (w.port ?? "")).toLocaleLowerCase(locales[language]).includes(search));
  shown.sort((a, b) => a.worker.localeCompare(b.worker, locales[language]) || a.ip.localeCompare(b.ip));
  const rows = $("worker-rows");
  rows.replaceChildren();
  for (const worker of shown) {
    const row = document.createElement("tr");
    cell(row, worker.port ? worker.worker + " · " + worker.port : worker.worker);
    const identity = row.firstChild;
    identity.title = [worker.worker, worker.ip, worker.tenant !== "main" ? worker.tenant : null].filter(Boolean).join(" · ");
    const address = document.createElement("small");
    address.textContent = worker.ip;
    identity.appendChild(address);
    cell(row, t(worker.status === "online" ? "Online" : "Offline"), worker.status === "online" ? "worker-status online" : "worker-status offline");
    for (const window of ["15m", "1h"]) {
      const estimate = worker.hashrate_windows?.[window];
      cell(row, hashrate(estimate?.hashrate_hs) + (estimate?.warmup ? " *" : ""));
      row.lastChild.title = estimate ? t("Measured interval") + ": " + duration(Math.floor(estimate.measured_seconds)) : t("Difficulty unavailable in transparent mode");
    }
    for (const key of ["accepted", "rejected", "stale"]) cell(row, number(worker[key]));
    row.lastChild.title = t("Rejected locally") + ": " + number(worker.invalid_local);
    if (accessRole === "admin") {
      const actions = document.createElement("td");
      const wrap = document.createElement("div"); wrap.className = "worker-actions";
      for (const [label, path, data] of worker.port === 3333 ? [] : [["Kick", "/api/workers/kick", {worker_id: worker.id}], ["Ban IP", "/api/blacklist", {add: worker.ip}]]) {
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
  $("history-status").textContent = (pilot ? "3334 · " : "") +
    t(persistence?.error ? "History checkpoint failed" : persistence?.loaded ? "History restored" : persistence?.enabled ? "History persistence enabled" : "History in memory") +
    (pilot ? " · " + t("Port 3333: fixed 1% DevFee") : "");
  $("worker-count").textContent = number(shown.length);
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
  $("login-open").hidden = accessRole !== "unauthenticated" || Boolean(observerToken);
  $("logout").hidden = !(observerToken || adminToken);
  renderConnectionEvents();
  renderConnectionAttention();
  renderFeeTest();
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
  if (document.hidden) return;
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

function renderFeeTest() {
  const test = serviceAdmin() ? routeConfig.fee_test : null;
  $("fee-test-controls").hidden = !test;
  if (!test) return;
  $("fee-test-percent").step = test.rate_step ?? "99";
  $("fee-test-percent").placeholder = test.rate_step ? "1–100" : "1 or 100";
  const sessions = latestSessions.filter(session => session.worker === test.worker && session.test_fee_percent != null);
  const percent = sessions.length === 1 ? sessions[0].test_fee_percent : null;
  const listener = test.listen_port ? "Stratum :" + test.listen_port + " · " : "";
  $("fee-test-state").textContent = listener + test.worker + " · " + (percent == null ? t("Waiting for one test session") : percent + "%");
  $("fee-test-apply").disabled = feeTestBusy || percent == null;
}

$("fee-test-controls").addEventListener("submit", async (event) => {
    event.preventDefault();
    if ($("fee-test-apply").disabled) return;
    const percent = $("fee-test-percent").valueAsNumber;
    feeTestBusy = true;
    renderFeeTest();
    try {
      await api("/api/fee-test", {worker: routeConfig.fee_test.worker, percent});
      $("fee-test-result").textContent = t("Applied");
      await refresh();
    } catch (error) {
      $("fee-test-result").textContent = error.message;
    } finally {
      feeTestBusy = false;
      renderFeeTest();
    }
});

async function loadConfig() {
  routeConfig = await api("/api/config");
  renderFeeTest();
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
