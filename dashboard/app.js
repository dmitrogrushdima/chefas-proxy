"use strict";

const $ = (id) => document.getElementById(id);
const number = (value) => Number.isFinite(value) ? new Intl.NumberFormat("ru-RU").format(value) : "—";
let latestSessions = [];
let busy = false;

function duration(seconds) {
  if (!Number.isFinite(seconds)) return "—";
  if (seconds < 60) return `${seconds} с`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} мин`;
  return `${Math.floor(seconds / 3600)} ч ${Math.floor(seconds % 3600 / 60)} мин`;
}

function endpoint(value) {
  if (!value) return "Не настроен";
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
  const search = $("search").value.trim().toLocaleLowerCase("ru");
  const filter = $("filter").value;
  const shown = latestSessions.filter((session) =>
    (filter === "all" || session.state === filter) &&
    String(session.worker || "").toLocaleLowerCase("ru").includes(search));
  const tbody = $("sessions");
  tbody.replaceChildren();
  for (const session of shown) {
    const row = document.createElement("tr");
    const worker = session.worker || "Ожидание авторизации";
    cell(row, worker);
    row.firstChild.title = worker;
    cell(row, session.state === "connected" ? "● Подключён" : "● Подключается",
      session.state === "connected" ? "row-status" : "row-status waiting");
    cell(row, session.pool === "primary" ? "Основной" : session.pool === "backup" ? "Резервный" : "—");
    cell(row, number(session.accepted));
    cell(row, number(session.rejected));
    cell(row, duration(session.duration_seconds));
    tbody.appendChild(row);
  }
  $("session-count").textContent = number(latestSessions.length);
  const empty = $("empty");
  empty.hidden = shown.length > 0;
  empty.querySelector("b").textContent = latestSessions.length ? "Ничего не найдено" : "Подключений пока нет";
  empty.querySelector("p").textContent = latestSessions.length ? "Попробуйте изменить поиск или фильтр." : "Здесь появятся майнеры, подключённые к прокси.";
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
    $("active").textContent = number(metrics.active);
    $("active-hint").textContent = `Лимит: ${number(status.limit)}`;
    $("accepted").textContent = number(metrics.accepted);
    $("rejected").textContent = number(metrics.rejected);
    $("upstream-failures").textContent = number(metrics.upstream_failures);
    $("connections-total").textContent = number(metrics.connections);
    $("backup-connections").textContent = number(metrics.backup_connections);
    $("pending-unmatched").textContent = `${number(sessions.reduce((sum, item) => sum + (item.pending || 0), 0))} / ${number(metrics.unmatched)}`;
    $("limits-loops").textContent = `${number(metrics.refused_capacity)} / ${number(metrics.blocked_loops)}`;
    $("primary-pool").textContent = endpoint(status.primary);
    $("backup-pool").textContent = endpoint(status.backup);
    $("tls-label").textContent = status.tls ? "TLS · проверка сертификата" : "TCP · без TLS";
    $("listener").textContent = status.listeners?.join(", ") || "—";
    $("connection-status").className = "live-status online";
    $("connection-status").replaceChildren(document.createElement("i"), document.createTextNode(" На связи"));
    $("last-update").textContent = `Обновлено ${new Date().toLocaleTimeString("ru-RU")}`;
    $("offline-notice").hidden = true;
    renderSessions();
  } catch (_) {
    $("connection-status").className = "live-status offline";
    $("connection-status").replaceChildren(document.createElement("i"), document.createTextNode(" Нет связи"));
    $("offline-notice").hidden = false;
  } finally {
    clearTimeout(timeout);
    busy = false;
  }
}

$("refresh").addEventListener("click", refresh);
$("search").addEventListener("input", renderSessions);
$("filter").addEventListener("change", renderSessions);
refresh();
setInterval(refresh, 3000);
