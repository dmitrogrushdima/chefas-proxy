"use strict";

const translations = {
  ru: {
    "Kontur — overview":"Контур — обзор", "Sections":"Разделы", "WORKSPACE":"РАБОЧЕЕ ПРОСТРАНСТВО", "OVERVIEW":"ОБЗОР", "Overview":"Обзор", "Connections":"Подключения", "Pools":"Пулы", "Diagnostics":"Диагностика", "Local dashboard":"Локальная панель", "Data stays here":"Данные остаются здесь", "Language":"Язык", "TEST VERSION":"ТЕСТОВАЯ ВЕРСИЯ", "Connecting":"Подключение", "CONTROL CENTER":"ЦЕНТР УПРАВЛЕНИЯ", "Proxy overview":"Обзор прокси", "Connections and pool activity in one place.":"Состояние подключений и работы с пулами в одном месте.", "Waiting for data":"Ожидание данных", "Refresh data":"Обновить данные", "Refresh":"Обновить", "Proxy connection lost. The last data may be out of date.":"Нет связи с прокси. Последние данные могут быть неактуальны.", "Overview metrics":"Общие показатели", "Active connections":"Активные подключения", "Accepted shares":"Принято шар", "Since proxy start":"За время работы прокси", "Rejected shares":"Отклонено шар", "From pool responses":"По ответам пула", "Connection errors":"Ошибки подключения", "While connecting to a pool":"При подключении к пулу", "MONITORING":"МОНИТОРИНГ", "Active miner sessions":"Активные сеансы майнеров", "Search worker":"Поиск воркера", "Status filter":"Фильтр состояния", "All sessions":"Все сеансы", "Connected":"Подключены", "WORKER":"ВОРКЕР", "STATUS":"СОСТОЯНИЕ", "ROUTE":"МАРШРУТ", "ACCEPTED":"ПРИНЯТО", "REJECTED":"ОТКЛОНЕНО", "DURATION":"ВРЕМЯ", "Current session data":"Данные текущих сеансов", "Auto-refresh · 3 sec":"Автообновление · 3 сек", "Counters since process start":"Счётчики за время работы процесса", "Total connections":"Всего подключений", "Backup connections":"Переходов на резерв", "Pending / unmatched":"Ожидают ответа / не сопоставлены", "Capacity limit / obvious loops":"Лимит / очевидные петли", "Counters are based on observed pool responses and may differ from pool statistics. They do not calculate revenue.":"Счётчики основаны на наблюдаемых ответах пула и могут отличаться от его статистики. Это не расчёт дохода.", "Current configuration":"Текущая конфигурация", "PRIMARY POOL":"ОСНОВНОЙ ПУЛ", "BACKUP POOL":"РЕЗЕРВНЫЙ ПУЛ", "On connection failure":"При ошибке подключения", "Proxy port":"Порт прокси", "NOTE":"НА ЗАМЕТКУ", "Clarity that matters":"Прозрачно по делу", "We show only what we can observe. Hashrate, revenue and DevFee will appear once implemented and verified.":"Показываем только то, что можем наблюдать. Хешрейт, доход и DevFee появятся после их реализации и проверки.", "About the counters":"О счётчиках", "PROTOTYPE 01":"ПРОТОТИП 01", "Built for those who keep things under control.":"Создано для тех, кто держит процесс под контролем.", "Local monitoring · BTC / Stratum V1":"Локальный мониторинг · BTC / Stratum V1",
    "No connections yet":"Подключений пока нет", "Miners connected to the proxy will appear here.":"Здесь появятся майнеры, подключённые к прокси.", "Nothing found":"Ничего не найдено", "Try changing the search or filter.":"Попробуйте изменить поиск или фильтр.", "Waiting for authorization":"Ожидание авторизации", "Primary":"Основной", "Backup":"Резервный", "Not configured":"Не настроен", "TLS · certificate verified":"TLS · проверка сертификата", "TCP · no TLS":"TCP · без TLS", "Online":"На связи", "Offline":"Нет связи", "Limit":"Лимит", "Updated":"Обновлено", "sec":"с", "min":"мин", "hr":"ч"
  },
  "zh-CN": {
    "Kontur — overview":"Kontur — 概览", "Sections":"栏目", "WORKSPACE":"工作区", "OVERVIEW":"概览", "Overview":"概览", "Connections":"连接", "Pools":"矿池", "Diagnostics":"诊断", "Local dashboard":"本地面板", "Data stays here":"数据保留在本地", "Language":"语言", "TEST VERSION":"测试版本", "Connecting":"正在连接", "CONTROL CENTER":"控制中心", "Proxy overview":"代理概览", "Connections and pool activity in one place.":"在一处查看连接和矿池状态。", "Waiting for data":"正在等待数据", "Refresh data":"刷新数据", "Refresh":"刷新", "Proxy connection lost. The last data may be out of date.":"代理连接已断开，显示的数据可能已过期。", "Overview metrics":"概览指标", "Active connections":"活动连接", "Accepted shares":"已接受份额", "Since proxy start":"自代理启动以来", "Rejected shares":"已拒绝份额", "From pool responses":"根据矿池响应", "Connection errors":"连接错误", "While connecting to a pool":"连接矿池时", "MONITORING":"监控", "Active miner sessions":"活动矿机会话", "Search worker":"搜索矿工", "Status filter":"状态筛选", "All sessions":"全部会话", "Connected":"已连接", "WORKER":"矿工", "STATUS":"状态", "ROUTE":"路由", "ACCEPTED":"已接受", "REJECTED":"已拒绝", "DURATION":"时长", "Current session data":"当前会话数据", "Auto-refresh · 3 sec":"自动刷新 · 3 秒", "Counters since process start":"进程启动以来的计数", "Total connections":"总连接数", "Backup connections":"备用矿池连接数", "Pending / unmatched":"等待响应 / 无法匹配", "Capacity limit / obvious loops":"连接数上限 / 明显环路", "Counters are based on observed pool responses and may differ from pool statistics. They do not calculate revenue.":"计数基于观察到的矿池响应，可能与矿池统计不同，不用于计算收益。", "Current configuration":"当前配置", "PRIMARY POOL":"主矿池", "BACKUP POOL":"备用矿池", "On connection failure":"连接失败时使用", "Proxy port":"代理端口", "NOTE":"提示", "Clarity that matters":"清晰透明", "We show only what we can observe. Hashrate, revenue and DevFee will appear once implemented and verified.":"只显示可以观察到的数据。算力、收益和开发费将在实现并验证后显示。", "About the counters":"关于计数器", "PROTOTYPE 01":"原型 01", "Built for those who keep things under control.":"为掌控运行状态的人而设计。", "Local monitoring · BTC / Stratum V1":"本地监控 · BTC / Stratum V1",
    "No connections yet":"暂无连接", "Miners connected to the proxy will appear here.":"连接到代理的矿机会显示在这里。", "Nothing found":"未找到结果", "Try changing the search or filter.":"请尝试更改搜索或筛选条件。", "Waiting for authorization":"等待授权", "Primary":"主矿池", "Backup":"备用矿池", "Not configured":"未配置", "TLS · certificate verified":"TLS · 验证证书", "TCP · no TLS":"TCP · 未加密", "Online":"在线", "Offline":"离线", "Limit":"上限", "Updated":"更新于", "sec":"秒", "min":"分", "hr":"小时"
  }
};

translations.ru["Blocked by IP filter"] = "Отклонено IP-фильтром";
translations["zh-CN"]["Blocked by IP filter"] = "IP 筛选拒绝";
const locales = {en: "en-US", ru: "ru-RU", "zh-CN": "zh-CN"};
let language = "en";
try { const saved = localStorage.getItem("kontur-language"); if (Object.prototype.hasOwnProperty.call(locales, saved)) language = saved; } catch (_) { /* storage can be disabled */ }
const staticNodes = [];
const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
while (walker.nextNode()) {
  const node = walker.currentNode;
  if (["SCRIPT", "STYLE"].includes(node.parentElement?.tagName)) continue;
  const text = node.nodeValue.trim();
  if (text && Object.prototype.hasOwnProperty.call(translations.ru, text)) {
    staticNodes.push({node, text, before: node.nodeValue.match(/^\s*/)[0], after: node.nodeValue.match(/\s*$/)[0]});
  }
}
const staticAttributes = [];
for (const element of document.querySelectorAll("[aria-label], [placeholder]")) {
  for (const name of ["aria-label", "placeholder"]) {
    const text = element.getAttribute(name);
    if (text && Object.prototype.hasOwnProperty.call(translations.ru, text)) staticAttributes.push({element, name, text});
  }
}
function t(text) { return translations[language]?.[text] || text; }
function applyLanguage() {
  document.documentElement.lang = language;
  document.title = language === "ru" ? "Контур — Mining Proxy" : "Kontur — Mining Proxy";
  for (const entry of staticNodes) entry.node.nodeValue = entry.before + t(entry.text) + entry.after;
  for (const entry of staticAttributes) entry.element.setAttribute(entry.name, t(entry.text));
  document.getElementById("language").value = language;
  document.getElementById("language").setAttribute("aria-label", t("Language"));
  document.dispatchEvent(new Event("kontur-language-change"));
}
document.getElementById("language").addEventListener("change", (event) => {
  if (!Object.prototype.hasOwnProperty.call(locales, event.target.value)) return;
  language = event.target.value;
  try { localStorage.setItem("kontur-language", language); } catch (_) { /* use the current session */ }
  applyLanguage();
});
applyLanguage();
