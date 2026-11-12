"use strict";

const translations = {
  ru: {
    "Stratum port": "Порт Stratum",
    "CHEFAS Proxy — overview":"CHEFAS Proxy — обзор", "Sections":"Разделы", "WORKSPACE":"РАБОЧЕЕ ПРОСТРАНСТВО", "OVERVIEW":"ОБЗОР", "Overview":"Обзор", "Connections":"Подключения", "Pools":"Пулы", "Diagnostics":"Диагностика", "Local dashboard":"Локальная панель", "Data stays here":"Данные остаются здесь", "Language":"Язык", "Connecting":"Подключение", "CONTROL CENTER":"ЦЕНТР УПРАВЛЕНИЯ", "Proxy overview":"Обзор прокси", "Connections and pool activity in one place.":"Состояние подключений и работы с пулами в одном месте.", "Waiting for data":"Ожидание данных", "Refresh data":"Обновить данные", "Refresh":"Обновить", "Proxy connection lost. The last data may be out of date.":"Нет связи с прокси. Последние данные могут быть неактуальны.", "Overview metrics":"Общие показатели", "Active connections":"Активные подключения", "Accepted shares":"Принято шар", "Since proxy start":"За время работы прокси", "Rejected shares":"Отклонено шар", "From pool responses":"По ответам пула", "Connection errors":"Ошибки подключения", "While connecting to a pool":"При подключении к пулу", "MONITORING":"МОНИТОРИНГ", "Active miner sessions":"Активные сеансы майнеров", "Search worker":"Поиск воркера", "Status filter":"Фильтр состояния", "All sessions":"Все сеансы", "Connected":"Подключены", "WORKER":"ВОРКЕР", "STATUS":"СОСТОЯНИЕ", "ROUTE":"МАРШРУТ", "ACCEPTED":"ПРИНЯТО", "REJECTED":"ОТКЛОНЕНО", "DURATION":"ВРЕМЯ", "Current session data":"Данные текущих сеансов", "Auto-refresh · 3 sec":"Автообновление · 3 сек", "Counters since process start":"Счётчики за время работы процесса", "Total connections":"Всего подключений", "Backup connections":"Переходов на резерв", "Pending / unmatched":"Ожидают ответа / не сопоставлены", "Capacity limit / obvious loops":"Лимит / очевидные петли", "Counters are based on observed pool responses and may differ from pool statistics. They do not calculate revenue.":"Счётчики основаны на наблюдаемых ответах пула и могут отличаться от его статистики. Это не расчёт дохода.", "Current configuration":"Текущая конфигурация", "PRIMARY POOL":"ОСНОВНОЙ ПУЛ", "BACKUP POOL":"РЕЗЕРВНЫЙ ПУЛ", "On connection failure":"При ошибке подключения", "Proxy port":"Порт прокси", "NOTE":"НА ЗАМЕТКУ", "Clarity that matters":"Прозрачно по делу", "We show only what we can observe. Hashrate, revenue and DevFee will appear once implemented and verified.":"Показываем только то, что можем наблюдать. Хешрейт, доход и DevFee появятся после их реализации и проверки.", "About the counters":"О счётчиках", "Built for those who keep things under control.":"Создано для тех, кто держит процесс под контролем.", "Local monitoring · BTC / Stratum V1":"Локальный мониторинг · BTC / Stratum V1",
    "No connections yet":"Подключений пока нет", "Miners connected to the proxy will appear here.":"Здесь появятся майнеры, подключённые к прокси.", "Nothing found":"Ничего не найдено", "Try changing the search or filter.":"Попробуйте изменить поиск или фильтр.", "Waiting for authorization":"Ожидание авторизации", "Primary":"Основной", "Backup":"Резервный", "Not configured":"Не настроен", "TLS · certificate verified":"TLS · проверка сертификата", "TCP · no TLS":"TCP · без TLS", "Online":"На связи", "Offline":"Нет связи", "Limit":"Лимит", "Updated":"Обновлено", "sec":"с", "min":"мин", "hr":"ч"
  },
  "zh-CN": {
    "CHEFAS Proxy — overview":"CHEFAS Proxy — 概览", "Sections":"栏目", "WORKSPACE":"工作区", "OVERVIEW":"概览", "Overview":"概览", "Connections":"连接", "Pools":"矿池", "Diagnostics":"诊断", "Local dashboard":"本地面板", "Data stays here":"数据保留在本地", "Language":"语言", "Connecting":"正在连接", "CONTROL CENTER":"控制中心", "Proxy overview":"代理概览", "Connections and pool activity in one place.":"在一处查看连接和矿池状态。", "Waiting for data":"正在等待数据", "Refresh data":"刷新数据", "Refresh":"刷新", "Proxy connection lost. The last data may be out of date.":"代理连接已断开，显示的数据可能已过期。", "Overview metrics":"概览指标", "Active connections":"活动连接", "Accepted shares":"已接受份额", "Since proxy start":"自代理启动以来", "Rejected shares":"已拒绝份额", "From pool responses":"根据矿池响应", "Connection errors":"连接错误", "While connecting to a pool":"连接矿池时", "MONITORING":"监控", "Active miner sessions":"活动矿机会话", "Search worker":"搜索矿工", "Status filter":"状态筛选", "All sessions":"全部会话", "Connected":"已连接", "WORKER":"矿工", "STATUS":"状态", "ROUTE":"路由", "ACCEPTED":"已接受", "REJECTED":"已拒绝", "DURATION":"时长", "Current session data":"当前会话数据", "Auto-refresh · 3 sec":"自动刷新 · 3 秒", "Counters since process start":"进程启动以来的计数", "Total connections":"总连接数", "Backup connections":"备用矿池连接数", "Pending / unmatched":"等待响应 / 无法匹配", "Capacity limit / obvious loops":"连接数上限 / 明显环路", "Counters are based on observed pool responses and may differ from pool statistics. They do not calculate revenue.":"计数基于观察到的矿池响应，可能与矿池统计不同，不用于计算收益。", "Current configuration":"当前配置", "PRIMARY POOL":"主矿池", "BACKUP POOL":"备用矿池", "On connection failure":"连接失败时使用", "Proxy port":"代理端口", "NOTE":"提示", "Clarity that matters":"清晰透明", "We show only what we can observe. Hashrate, revenue and DevFee will appear once implemented and verified.":"只显示可以观察到的数据。算力、收益和开发费将在实现并验证后显示。", "About the counters":"关于计数器", "Built for those who keep things under control.":"为掌控运行状态的人而设计。", "Local monitoring · BTC / Stratum V1":"本地监控 · BTC / Stratum V1",
    "No connections yet":"暂无连接", "Miners connected to the proxy will appear here.":"连接到代理的矿机会显示在这里。", "Nothing found":"未找到结果", "Try changing the search or filter.":"请尝试更改搜索或筛选条件。", "Waiting for authorization":"等待授权", "Primary":"主矿池", "Backup":"备用矿池", "Not configured":"未配置", "TLS · certificate verified":"TLS · 验证证书", "TCP · no TLS":"TCP · 未加密", "Online":"在线", "Offline":"离线", "Limit":"上限", "Updated":"更新于", "sec":"秒", "min":"分", "hr":"小时"
  }
};

Object.assign(translations.ru, {"Estimated hashrate":"Оценочный хешрейт", "Active workers":"Активные воркеры", "DevFee debt":"Задолженность DevFee", "Accepted difficulty · 5 minute window":"Принятая сложность · окно 5 минут", "Authorized worker identities online":"Авторизованные воркеры на связи", "Accepted difficulty · target 1%":"Принятая сложность · цель 1%", "Farm performance":"Показатели фермы"});
Object.assign(translations["zh-CN"], {"Estimated hashrate":"估算算力", "Active workers":"在线矿工", "DevFee debt":"开发费待计难度", "Accepted difficulty · 5 minute window":"已接受难度 · 5分钟窗口", "Authorized worker identities online":"在线已授权矿工", "Accepted difficulty · target 1%":"已接受难度 · 目标1%", "Farm performance":"矿场概况"});

translations.ru["Blocked by IP filter"] = "Отклонено IP-фильтром";
translations["zh-CN"]["Blocked by IP filter"] = "IP 筛选拒绝";
translations.ru["Pool redirect requests (not blocked)"] = "Запросы перенаправления пула (не блокируются)";
translations["zh-CN"]["Pool redirect requests (not blocked)"] = "矿池重定向请求（未阻止）";
translations.ru["Avg. share response time"] = "Среднее время ответа на шару";
translations["zh-CN"]["Avg. share response time"] = "份额平均响应时间";
translations.ru.ms = "мс";
translations["zh-CN"].ms = "毫秒";
translations.ru["TLS · certificate verification enabled"] = "TLS · проверка сертификата включена";
translations["zh-CN"]["TLS · certificate verification enabled"] = "TLS · 已启用证书验证";
translations.ru["Pool requested miner reconnection. Requests are not blocked and a miner may bypass the proxy."] = "Пул запросил переподключение майнера. Эти запросы не блокируются; майнер может обойти прокси.";
translations["zh-CN"]["Pool requested miner reconnection. Requests are not blocked and a miner may bypass the proxy."] = "矿池请求矿机重新连接。此类请求不会被阻止，矿机可能绕过代理。";
translations.ru["View diagnostics"] = "К диагностике";
translations["zh-CN"]["View diagnostics"] = "查看诊断";
translations.ru["Idle sessions closed"] = "Закрыто неактивных сеансов";
translations["zh-CN"]["Idle sessions closed"] = "已关闭的空闲会话";
translations.ru["Export JSON"] = "Выгрузить JSON";
translations["zh-CN"]["Export JSON"] = "导出 JSON";
translations.ru["AUDIT"] = "АУДИТ";
translations["zh-CN"]["AUDIT"] = "审计";
translations.ru["Accepted work"] = "Принятая работа";
translations["zh-CN"]["Accepted work"] = "已接受工作量";
translations.ru["User difficulty / shares"] = "Сложность клиента / шары";
translations["zh-CN"]["User difficulty / shares"] = "用户难度 / 份额";
translations.ru["DevFee difficulty / shares"] = "Сложность DevFee / шары";
translations["zh-CN"]["DevFee difficulty / shares"] = "开发费难度 / 份额";
translations.ru["DevFee share of difficulty"] = "Доля DevFee по сложности";
translations["zh-CN"]["DevFee share of difficulty"] = "开发费难度占比";
translations.ru["Estimated hashrate · 5 min"] = "Оценка хешрейта · 5 мин";
translations["zh-CN"]["Estimated hashrate · 5 min"] = "预计算力 · 5 分钟";
translations.ru["Rejected locally"] = "Отклонено локально";
translations["zh-CN"]["Rejected locally"] = "本地拒绝";
translations.ru["Pool-confirmed work since proxy start. Estimates are not payouts."] = "Работа, подтверждённая пулами с запуска прокси. Оценка не равна выплатам.";
translations["zh-CN"]["Pool-confirmed work since proxy start. Estimates are not payouts."] = "代理启动后矿池确认的工作量。估算值不代表实际支付。";
translations.ru["Workers"] = "Воркеры";
translations["zh-CN"]["Workers"] = "矿工";
translations.ru["LIVE OBSERVER"] = "МОНИТОРИНГ В РЕАЛЬНОМ ВРЕМЕНИ";
translations["zh-CN"]["LIVE OBSERVER"] = "实时监控";
translations.ru["Grouped by worker name and source IP. History resets on restart."] = "Группировка по имени и IP. История сбрасывается при перезапуске.";
translations["zh-CN"]["Grouped by worker name and source IP. History resets on restart."] = "按矿工名和来源 IP 分组。重启后历史重置。";
translations.ru["Search worker or IP"] = "Поиск имени или IP";
translations["zh-CN"]["Search worker or IP"] = "搜索矿工或 IP";
translations.ru["Worker status"] = "Состояние воркера";
translations["zh-CN"]["Worker status"] = "矿工状态";
translations.ru["All workers"] = "Все воркеры";
translations["zh-CN"]["All workers"] = "全部矿工";
translations.ru["Worker history limit reached. Some observations are unavailable."] = "Достигнут лимит истории воркеров. Часть наблюдений недоступна.";
translations["zh-CN"]["Worker history limit reached. Some observations are unavailable."] = "矿工历史已达上限，部分观测数据不可用。";
translations.ru["HASHRATE 15m"] = "ХЕШРЕЙТ 15м";
translations["zh-CN"]["HASHRATE 15m"] = "算力 15分";
translations.ru["HASHRATE 1h"] = "ХЕШРЕЙТ 1ч";
translations["zh-CN"]["HASHRATE 1h"] = "算力 1时";
translations.ru["HASHRATE 24h"] = "ХЕШРЕЙТ 24ч";
translations["zh-CN"]["HASHRATE 24h"] = "算力 24时";
translations.ru["STALE"] = "УСТАРЕВШИЕ";
translations["zh-CN"]["STALE"] = "过期";
translations.ru["LOCAL"] = "ЛОКАЛЬНЫЕ ОТКАЗЫ";
translations["zh-CN"]["LOCAL"] = "本地拒绝";
translations.ru["POOL RESPONSE"] = "ОТВЕТ ПУЛА";
translations["zh-CN"]["POOL RESPONSE"] = "矿池响应";
translations.ru["ASIC PING"] = "PING ASIC";
translations["zh-CN"]["ASIC PING"] = "ASIC 延迟";
translations.ru["No worker data yet"] = "Данных по воркерам пока нет";
translations["zh-CN"]["No worker data yet"] = "暂无矿工数据";
translations.ru["Statistics appear after pool authorization."] = "Статистика появится после авторизации пулом.";
translations["zh-CN"]["Statistics appear after pool authorization."] = "矿池授权后显示统计数据。";
translations.ru["Hashrate is estimated from accepted difficulty; 1-minute buckets."] = "Хешрейт по принятой сложности; интервалы по 1 минуте.";
translations["zh-CN"]["Hashrate is estimated from accepted difficulty; 1-minute buckets."] = "算力按已接受难度估算；采用一分钟分桶。";
translations.ru["Live updates · 1 sec"] = "Обновление в реальном времени · 1 сек";
translations["zh-CN"]["Live updates · 1 sec"] = "实时更新 · 1 秒";
translations.ru["Session transitions"] = "Переключения сессий";
translations["zh-CN"]["Session transitions"] = "会话切换";
translations.ru["Latest 500 events in memory. This is not a persistent audit log."] = "Последние 500 событий в памяти. Журнал не сохраняется на диск.";
translations["zh-CN"]["Latest 500 events in memory. This is not a persistent audit log."] = "内存中最近 500 条事件，重启后不保留。";
translations.ru["TIME"] = "ВРЕМЯ";
translations["zh-CN"]["TIME"] = "时间";
translations.ru["TARGET"] = "НАЗНАЧЕНИЕ";
translations["zh-CN"]["TARGET"] = "目标";
translations.ru["EVENT"] = "СОБЫТИЕ";
translations["zh-CN"]["EVENT"] = "事件";
translations.ru["No transitions yet"] = "Переключений пока нет";
translations["zh-CN"]["No transitions yet"] = "暂无切换";
translations.ru["Outstanding DevFee difficulty"] = "Расчётный долг DevFee по сложности";
translations["zh-CN"]["Outstanding DevFee difficulty"] = "待补开发费难度";
translations.ru["Measured interval"] = "Измеренный интервал";
translations["zh-CN"]["Measured interval"] = "测量时长";
translations.ru["Difficulty unavailable in transparent mode"] = "В прозрачном режиме сложность не учитывается";
translations["zh-CN"]["Difficulty unavailable in transparent mode"] = "透明模式下不统计难度";
translations.ru["ASIC ping is not measured"] = "Ping ASIC не измеряется";
translations["zh-CN"]["ASIC ping is not measured"] = "未测量 ASIC 延迟";
translations.ru["SSE · live"] = "SSE · в реальном времени";
translations["zh-CN"]["SSE · live"] = "SSE · 实时";
translations.ru["Polling · 3 sec"] = "Опрос · 3 сек";
translations["zh-CN"]["Polling · 3 sec"] = "轮询 · 3 秒";
translations.ru["Assignment"] = "Назначение";
translations["zh-CN"]["Assignment"] = "分配";
translations.ru["Switch"] = "Переключение";
translations["zh-CN"]["Switch"] = "切换";
translations.ru["* Incomplete window. Stale shares are included in rejected."] = "* Неполное окно. Устаревшие шары входят в отклонённые.";
translations["zh-CN"]["* Incomplete window. Stale shares are included in rejected."] = "* 时间窗未填满。过期份额计入拒绝份额。";
translations.ru["Sign in"] = "Войти";
translations["zh-CN"]["Sign in"] = "登录";
translations.ru["Sign out"] = "Выйти";
translations["zh-CN"]["Sign out"] = "退出";
translations.ru["Administrator sign in"] = "Вход администратора";
translations["zh-CN"]["Administrator sign in"] = "管理员登录";
translations.ru["Enter the CHEFAS_PROXY_ADMIN_TOKEN configured on the server."] = "Введите CHEFAS_PROXY_ADMIN_TOKEN, заданный на сервере.";
translations["zh-CN"]["Enter the CHEFAS_PROXY_ADMIN_TOKEN configured on the server."] = "输入服务器配置的 CHEFAS_PROXY_ADMIN_TOKEN。";
translations.ru["Admin token"] = "Токен администратора";
translations["zh-CN"]["Admin token"] = "管理员令牌";
translations.ru["Cancel"] = "Отмена";
translations["zh-CN"]["Cancel"] = "取消";
translations.ru["Invalid admin token"] = "Неверный токен администратора";
translations["zh-CN"]["Invalid admin token"] = "管理员令牌无效";
translations.ru["Routes"] = "Маршруты";
translations["zh-CN"]["Routes"] = "路由";
translations.ru["Settings apply to new connections. Active miners can reconnect automatically."] = "Настройки применяются к новым подключениям. Активные майнеры могут переподключиться автоматически.";
translations["zh-CN"]["Settings apply to new connections. Active miners can reconnect automatically."] = "设置适用于新连接，活动矿机可自动重连。";
translations.ru["User Pool"] = "Пользовательский пул";
translations["zh-CN"]["User Pool"] = "用户矿池";
translations.ru["DevFee Pool"] = "Пул комиссии";
translations["zh-CN"]["DevFee Pool"] = "开发费矿池";
translations.ru["Host"] = "Адрес";
translations["zh-CN"]["Host"] = "主机";
translations.ru["Port"] = "Порт";
translations["zh-CN"]["Port"] = "端口";
translations.ru["User / Wallet"] = "Пользователь / Кошелёк";
translations["zh-CN"]["User / Wallet"] = "用户 / 钱包";
translations.ru["Password"] = "Пароль";
translations["zh-CN"]["Password"] = "密码";
translations.ru["Forward ASIC credentials"] = "Передавать реквизиты ASIC";
translations["zh-CN"]["Forward ASIC credentials"] = "转发 ASIC 凭据";
translations.ru["Leave blank to keep"] = "Пусто — сохранить прежний";
translations["zh-CN"]["Leave blank to keep"] = "留空保留原值";
translations.ru["Enable DevFee Pool"] = "Использовать пул комиссии";
translations["zh-CN"]["Enable DevFee Pool"] = "启用开发费矿池";
translations.ru["Reconnect active miners"] = "Переподключить активные майнеры";
translations["zh-CN"]["Reconnect active miners"] = "重新连接活动矿机";
translations.ru["Save routes"] = "Сохранить маршруты";
translations["zh-CN"]["Save routes"] = "保存路由";
translations.ru["Create Observer Link"] = "Создать ссылку наблюдателя";
translations["zh-CN"]["Create Observer Link"] = "创建观察者链接";
translations.ru["Observer Link"] = "Ссылка наблюдателя";
translations["zh-CN"]["Observer Link"] = "观察者链接";
translations.ru["IP Blacklist"] = "Блокировка IP";
translations["zh-CN"]["IP Blacklist"] = "IP 黑名单";
translations.ru["Blocks new connections. Use Kick to disconnect an active worker."] = "Блокирует новые подключения. Кнопка отключения завершит активную сессию.";
translations["zh-CN"]["Blocks new connections. Use Kick to disconnect an active worker."] = "阻止新连接，使用断开按钮结束活动会话。";
translations.ru["IP / CIDR"] = "IP / CIDR";
translations["zh-CN"]["IP / CIDR"] = "IP / CIDR";
translations.ru["Ban IP"] = "Заблокировать IP";
translations["zh-CN"]["Ban IP"] = "封禁 IP";
translations.ru["Kick"] = "Отключить";
translations["zh-CN"]["Kick"] = "断开";
translations.ru["Remove"] = "Удалить";
translations["zh-CN"]["Remove"] = "删除";
translations.ru["ACTIONS"] = "ДЕЙСТВИЯ";
translations["zh-CN"]["ACTIONS"] = "操作";
translations.ru["Applied"] = "Применено";
translations["zh-CN"]["Applied"] = "已应用";
translations.ru["Routes saved"] = "Маршруты сохранены";
translations["zh-CN"]["Routes saved"] = "路由已保存";
translations.ru["Request failed"] = "Ошибка запроса";
translations["zh-CN"]["Request failed"] = "请求失败";
translations.ru["FIRST SHARE EMA"] = "ПЕРВАЯ ШАРА EMA";
translations["zh-CN"]["FIRST SHARE EMA"] = "首份额 EMA";
translations.ru["Notify to first accepted share; includes mining time, not network RTT"] = "От отправки задания до получения первой принятой шары; включает время майнинга, не сетевой RTT";
translations["zh-CN"]["Notify to first accepted share; includes mining time, not network RTT"] = "从任务发送到首次接受份额的接收，包含挖矿时间，并非网络 RTT";
translations.ru["Grouped by worker name and source IP. Worker history can survive restarts."] = "Группировка по имени и IP. История воркеров может сохраняться после перезапуска.";
translations["zh-CN"]["Grouped by worker name and source IP. Worker history can survive restarts."] = "按矿工名和来源 IP 分组，矿工历史可在重启后保留。";
translations.ru["invalid request or stale configuration"] = "Некорректный запрос или устаревшая версия настроек";
translations["zh-CN"]["invalid request or stale configuration"] = "请求无效或配置版本过期";
translations.ru["state or upstream unavailable"] = "Не удалось сохранить состояние или подключиться к пулу";
translations["zh-CN"]["state or upstream unavailable"] = "无法保存状态或连接矿池";
translations.ru["administrator required"] = "Нужны права администратора";
translations["zh-CN"]["administrator required"] = "需要管理员权限";
translations.ru["rate limit"] = "Превышен лимит запросов";
translations["zh-CN"]["rate limit"] = "请求频率超限";
translations.ru["Reload routes"] = "Обновить форму";
translations["zh-CN"]["Reload routes"] = "重新加载路由";
translations.ru["Routes reloaded"] = "Форма маршрутов обновлена";
translations["zh-CN"]["Routes reloaded"] = "路由已重新加载";
translations.ru["History checkpoint failed"] = "Ошибка сохранения истории";
translations["zh-CN"]["History checkpoint failed"] = "历史保存失败";
translations.ru["History restored"] = "История восстановлена";
translations["zh-CN"]["History restored"] = "历史已恢复";
translations.ru["History persistence enabled"] = "Сохранение истории включено";
translations["zh-CN"]["History persistence enabled"] = "已启用历史保存";
translations["ru"]["LAST ACCEPTED SHARE"] = "ПОСЛЕДНЯЯ ПРИНЯТАЯ ШАРА";
translations["zh-CN"]["LAST ACCEPTED SHARE"] = "最近接受的份额";
translations["ru"]["SESSION"] = "СЕССИЯ";
translations["zh-CN"]["SESSION"] = "会话";
translations["ru"]["ASIC connection history"] = "История подключений ASIC";
translations["zh-CN"]["ASIC connection history"] = "ASIC 连接历史";
translations["ru"]["Latest connection changes since this proxy started."] = "Последние изменения с момента запуска прокси.";
translations["zh-CN"]["Latest connection changes since this proxy started."] = "代理启动以来的最近连接变化。";
translations["ru"]["Authorized, waiting for job"] = "Авторизован, ждём задание";
translations["zh-CN"]["Authorized, waiting for job"] = "已授权，等待任务";
translations["ru"]["Pool rejected authorization"] = "Пул отклонил авторизацию";
translations["zh-CN"]["Pool rejected authorization"] = "矿池拒绝授权";
translations["ru"]["Waiting for first accepted share"] = "Ждём первую принятую шару";
translations["zh-CN"]["Waiting for first accepted share"] = "等待首个被接受的份额";
translations["ru"]["Pool accepted shares"] = "Пул принимает шары";
translations["zh-CN"]["Pool accepted shares"] = "矿池已接受份额";
translations["ru"]["Pool connection lost"] = "Связь с пулом потеряна";
translations["zh-CN"]["Pool connection lost"] = "矿池连接已断开";
translations["ru"]["Pool unavailable"] = "Пул недоступен";
translations["zh-CN"]["Pool unavailable"] = "矿池不可用";
translations["ru"]["ASIC disconnected"] = "ASIC отключился";
translations["zh-CN"]["ASIC disconnected"] = "ASIC 已断开";
translations.ru["Needs attention"] = "Требуется внимание";
translations["zh-CN"]["Needs attention"] = "需要关注";
translations.ru["No accepted shares for 5 minutes"] = "Нет принятых шар 5 минут";
translations["zh-CN"]["No accepted shares for 5 minutes"] = "5 分钟内无已接受份额";
translations.ru["Lab fee override"] = "Комиссия на стенде";
translations["zh-CN"]["Lab fee override"] = "测试费率";
translations.ru["Set test worker to 100%"] = "Переключить воркер на 100%";
translations["zh-CN"]["Set test worker to 100%"] = "将测试矿工切换到 100%";
translations.ru["Return test worker to 1%"] = "Вернуть воркер на 1%";
translations["zh-CN"]["Return test worker to 1%"] = "将测试矿工恢复到 1%";
translations.ru["Waiting for one test session"] = "Ожидание одного тестового подключения";
translations["zh-CN"]["Waiting for one test session"] = "等待一个测试连接";
translations.ru["Recent session"] = "Недавняя сессия";
translations["zh-CN"]["Recent session"] = "最近的会话";
translations.ru["Session"] = "Сессия";
translations["zh-CN"]["Session"] = "会话";
translations.ru["History in memory"] = "История только в памяти";
translations["zh-CN"]["History in memory"] = "历史仅保留在内存中";
const locales = {en: "en-US", ru: "ru-RU", "zh-CN": "zh-CN"};
const browserLanguage = (navigator.language || "en").toLowerCase();
let language = browserLanguage.startsWith("ru") ? "ru" : browserLanguage.startsWith("zh") ? "zh-CN" : "en";
try { const saved = localStorage.getItem("chefas-proxy-language"); if (Object.prototype.hasOwnProperty.call(locales, saved)) language = saved; } catch (_) { /* storage can be disabled */ }
Object.assign(translations.ru, {
  "Route performance": "Работа по маршрутам",
  "Overview includes ports 3333 and 3334.": "Обзор включает порты 3333 и 3334.",
  "Port 3333 data unavailable. Overview shows port 3334 only.": "Нет данных с порта 3333. Обзор показывает только порт 3334.",
  "Accepted difficulty": "Принятая сложность",
  "Port 3333: fixed 1% DevFee": "Порт 3333: фиксированная комиссия 1%",
  "Worker share totals include previous sessions. Stale shares are included in rejected.": "Счётчики воркеров включают прошлые сеансы. Устаревшие шары входят в отклонённые.",
  "Overview counters cover port 3334. Worker and route tables include both ports.": "Счётчики обзора относятся к порту 3334. Таблицы воркеров и маршрутов охватывают оба порта.",
  "DevFee share 1h": "Доля DevFee за час",
  "Pool-confirmed hashrate over 1h. Counters and mean times (sample count) since start.": "Хешрейт за час по данным пула. Счётчики и среднее время (число замеров) с запуска.",
  "Accepted / rejected / stale": "Принято / отказано / устарело",
  "Validation": "Проверка",
  "Socket write": "Запись в сокет",
  "Pool wait": "Ожидание пула",
  "First share after DevFee": "Первая шара после DevFee",
  "Usual share interval": "Обычный интервал шар"
});
Object.assign(translations["zh-CN"], {
  "Route performance": "路由性能",
  "Overview includes ports 3333 and 3334.": "概览涵盖端口 3333 和 3334。",
  "Port 3333 data unavailable. Overview shows port 3334 only.": "端口 3333 数据不可用。概览仅显示端口 3334。",
  "Accepted difficulty": "已接受难度",
  "Port 3333: fixed 1% DevFee": "端口 3333：固定 1% 开发费",
  "Worker share totals include previous sessions. Stale shares are included in rejected.": "矿工份额总数包含历史会话。过期份额计入拒绝份额。",
  "Overview counters cover port 3334. Worker and route tables include both ports.": "概览计数仅涵盖端口 3334。矿工和路由表涵盖两个端口。",
  "DevFee share 1h": "1 小时开发费份额",
  "Pool-confirmed hashrate over 1h. Counters and mean times (sample count) since start.": "矿池确认的 1 小时算力。计数和平均时间（样本数）自启动以来统计。",
  "Accepted / rejected / stale": "接受 / 拒绝 / 过期",
  "Validation": "验证",
  "Socket write": "套接字写入",
  "Pool wait": "等待矿池",
  "First share after DevFee": "开发费后的首个份额",
  "Usual share interval": "正常份额间隔"
});

translations.ru["Pool-accepted shares · 15 minute window"] = "Шары, принятые пулом · окно 15 минут";
translations["zh-CN"]["Pool-accepted shares · 15 minute window"] = "矿池已接受的份额 · 15分钟窗口";

translations.ru["Live estimated hashrate"] = "Текущий оценочный хешрейт";
translations["zh-CN"]["Live estimated hashrate"] = "实时估算算力";

Object.assign(translations.ru, {
  "Sign in to your dashboard": "Вход в вашу панель",
  "Use the token created during installation to manage your farm.": "Используйте токен, созданный при установке, для управления своей фермой.",
  "Panel token": "Токен панели",
  "Where is my token?": "Где найти токен?",
  "Windows: open .env beside chefas-proxy.json. Copy only the value after CHEFAS_PROXY_ADMIN_TOKEN=.": "Windows: откройте .env рядом с chefas-proxy.json. Скопируйте только значение после CHEFAS_PROXY_ADMIN_TOKEN=.",
  "Ubuntu: the token is in /etc/chefas-proxy/chefas-proxy.env. See the installation guide for the command.": "Ubuntu: токен находится в /etc/chefas-proxy/chefas-proxy.env. Команда приведена в инструкции по установке.",
  "Connection details": "Подробности подключений",
  "IP access controls": "Управление доступом по IP",
  "Connect your first ASIC": "Подключите первый ASIC",
  "The proxy is running. Worker statistics appear after pool authorization.": "Прокси запущен. Статистика появится после авторизации воркера на пуле.",
  "In ASIC Pool 1, use the proxy computer’s LAN address and Stratum port.": "В Pool 1 ASIC укажите локальный адрес компьютера с прокси и порт Stratum.",
  "Keep your existing pool worker name and password.": "Оставьте прежнее имя воркера и пароль пула.",
  "Keep the direct pool in Pool 2 as a fallback.": "Сохраните прямой пул в Pool 2 как резерв.",
  "Use an address reachable from the ASIC network, not 127.0.0.1. If several ASIC URLs appear in the console, choose the address on the ASIC network.": "Используйте адрес, доступный из сети ASIC, а не 127.0.0.1. Если в консоли несколько ASIC URL, выберите адрес в сети ASIC.",
  "Stratum port": "Порт Stratum",
  "DevFee accounting": "Учёт DevFee",
  "Accounting details": "Подробности учёта",
  "Remaining fee work": "Остаток работы для комиссии",
  "Pool-confirmed work. Short windows may differ from the target.": "Работа, принятая пулом. На коротком интервале доля может отличаться от цели.",
  "DevFee uses separate pool jobs; estimates are not payouts.": "DevFee использует отдельные задания пула; оценки не являются выплатами.",
  "Fixed DevFee target": "Фиксированная цель DevFee",
  "Measured by pool-accepted difficulty": "По сложности, принятой пулом",
  "Fixed target: 1%. The rate cannot be changed in this release.": "Фиксированная цель: 1%. В этой сборке ставка не изменяется."
});
Object.assign(translations["zh-CN"], {
  "Sign in to your dashboard": "登录您的面板",
  "Use the token created during installation to manage your farm.": "使用安装时生成的令牌管理自己的矿场。",
  "Panel token": "面板令牌",
  "Where is my token?": "在哪里找到令牌？",
  "Windows: open .env beside chefas-proxy.json. Copy only the value after CHEFAS_PROXY_ADMIN_TOKEN=.": "Windows：打开 chefas-proxy.json 旁的 .env，只复制 CHEFAS_PROXY_ADMIN_TOKEN= 后的值。",
  "Ubuntu: the token is in /etc/chefas-proxy/chefas-proxy.env. See the installation guide for the command.": "Ubuntu：令牌位于 /etc/chefas-proxy/chefas-proxy.env，读取命令见安装指南。",
  "Connection details": "连接详情",
  "IP access controls": "IP 访问管理",
  "Connect your first ASIC": "连接第一台 ASIC",
  "The proxy is running. Worker statistics appear after pool authorization.": "代理已运行，矿池授权后将显示矿工统计。",
  "In ASIC Pool 1, use the proxy computer’s LAN address and Stratum port.": "在 ASIC 的 Pool 1 中填写代理电脑的局域网地址和 Stratum 端口。",
  "Keep your existing pool worker name and password.": "保留原有矿工名和矿池密码。",
  "Keep the direct pool in Pool 2 as a fallback.": "在 Pool 2 保留直连矿池作为备用。",
  "Use an address reachable from the ASIC network, not 127.0.0.1. If several ASIC URLs appear in the console, choose the address on the ASIC network.": "使用 ASIC 网络可以访问的地址，不要使用 127.0.0.1；若控制台显示多个 ASIC URL，请选择 ASIC 所在网络的地址。",
  "Stratum port": "Stratum 端口",
  "DevFee accounting": "DevFee 统计",
  "Accounting details": "统计详情",
  "Remaining fee work": "剩余佣金工作量",
  "Pool-confirmed work. Short windows may differ from the target.": "基于矿池确认的工作量，短期比例可能偏离目标。",
  "DevFee uses separate pool jobs; estimates are not payouts.": "DevFee 使用独立的矿池任务，估算值不等于收益。",
  "Fixed DevFee target": "固定 DevFee 目标",
  "Measured by pool-accepted difficulty": "按矿池接受的难度计算",
  "Fixed target: 1%. The rate cannot be changed in this release.": "固定目标为 1%，此发行版不能更改费率。"
});

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
  document.title = language === "ru" ? "CHEFAS Proxy — Mining Proxy" : "CHEFAS Proxy — Mining Proxy";
  for (const entry of staticNodes) entry.node.nodeValue = entry.before + t(entry.text) + entry.after;
  for (const entry of staticAttributes) entry.element.setAttribute(entry.name, t(entry.text));
  document.getElementById("language").value = language;
  document.getElementById("language").setAttribute("aria-label", t("Language"));
  document.dispatchEvent(new Event("chefas-proxy-language-change"));
}
document.getElementById("language").addEventListener("change", (event) => {
  if (!Object.prototype.hasOwnProperty.call(locales, event.target.value)) return;
  language = event.target.value;
  try { localStorage.setItem("chefas-proxy-language", language); } catch (_) { /* use the current session */ }
  applyLanguage();
});
applyLanguage();
