# Исследовательские ориентиры

Дата первичного обзора: 2026-11-05. Указанные возможности — заявления и документация проектов, а не результаты наших испытаний. Эти материалы изучаются; код, графика и тексты в наш продукт не переносились. Лицензии и версии проверяются заново перед любым использованием зависимостей.

| Источник | Чем полезен | Граница применения |
| --- | --- | --- |
| [RustMinerSystem](https://github.com/EvilGenius-dot/RustMinerSystem) и [документация панели](https://minerproxy.org/document/rustminersystem/dashboard) | Пользовательские сценарии: порты, наблюдение за воркерами, пул, ошибки, удобство установки | Репозиторий связан с сайтом; наличие файла MIT не доказывает, что весь исполняемый продукт можно собрать из опубликованного исходного кода. Мы не используем его бинарные файлы как основу. |
| [SimpleMiningProxy](https://github.com/famfamfam/simple-mining-proxy) | Пример BTC/SHA-256 Stratum V1: отдельное соединение майнера, TLS, переключение, учёт | Репозиторий указан как GPL-3.0; не копировать реализацию. Утверждения проекта о возможностях необходимо тестировать самостоятельно. |
| [Stratehm stratum-proxy](https://github.com/Stratehm/stratum-proxy) | Исторический пример стратегий переключения и веб-управления | GPL-3.0; возраст и совместимость нужно проверять. Не считать эталоном современных ASIC. |
| [Braiins Farm Proxy](https://braiins.com/braiins-proxy) и [FAQ](https://academy.braiins.com/farm-proxy/faqs) | Коммерческий ориентир: список заявленных испытанных пулов и майнеров, агрегация, ограничения видимости воркеров | Закрытый код; бесплатная цена и заявленные показатели не являются результатами наших тестов. |
| [Stratum V2 приложения](https://github.com/stratum-mining/sv2-apps), [спецификация](https://github.com/stratum-mining/sv2-spec) | Работа переводчика V1/V2, каналы, архитектура и протокольные тесты | Это отдельный протокол и будущий этап. Приложения обозначены разработчиками как alpha; лицензии проверять для конкретного компонента. |
| [BIP 310](https://github.com/bitcoin/bips/blob/master/bip-0310.mediawiki) | Согласование расширений Stratum V1 и version rolling | Draft; совместимость зависит от конкретных прошивок и пулов. |
| [XMRig Proxy](https://github.com/xmrig/xmrig-proxy) | Наблюдение за архитектурой и нагрузочными сценариями крупного прокси | CryptoNote/Monero, не BTC Stratum V1; нельзя делать вывод о совместимости с Bitcoin. GPL-3.0. |

## Принцип работы с источниками

Изучаем требования, публичную документацию и способы проверки. Собственные решения фиксируем своими словами и реализуем самостоятельно. Внешние библиотеки добавляются только после проверки происхождения, версии, лицензии, поддержки и безопасности; обязанности лицензии сохраняются. Не устанавливаем посторонние бинарные файлы на рабочую ферму ради анализа.


## Product review, 2026-11-06

The linked repositories were reviewed as published project interfaces/documentation, not as a successful reverse engineering or independent audit of their binaries. Stars, a Rust/Go label and broad README claims do not establish memory usage, safe fees or firmware compatibility.

| Primary reference | Useful lesson | Action for our product |
| --- | --- | --- |
| [RustMinerSystem / EvilGenius-dot](https://github.com/EvilGenius-dot/RustMinerSystem), [zhahitech listing](https://github.com/zhahitech/RustMinerSystem) | Pool/worker management and packaging are operator-facing features | Short operator README, local UI and a clear qualification matrix; do not execute vendor installers or claim source completeness |
| [KTMinerProxy](https://github.com/kt007007/KTMinerProxy) | Published per-device statistics and forwarding workflows | Prioritize useful status/reject accounting; published scale claims remain unverified |
| [TCMinerProxy](https://github.com/MinerProxyPro/TCMinerProxy) | Installation and lifecycle instructions are part of the product | Separate native deployment from source development; retain local, reviewable launch scripts |
| [Braiins configuration](https://academy.braiins.com/farm-proxy/configuration) | Explicit extranonce constraints and device-dependent caveats | Conserve nonce bytes, expose bounds, qualify hardware instead of guessing vendor behavior |
| [BIP310](https://github.com/bitcoin/bips/blob/master/bip-0310.mediawiki) | Negotiated masks affect reconstruction of the submitted header | Require an identical negotiated mask on alternate pools, test PoW after translation |

No claim that a $5 host can run 50,000 ASICs in 50 MB has been adopted. Our 400-session echo regression is not a capacity guarantee. Similarly, NiceHash product/service fees must be compared on the same fee base and current terms; our 1% accepted-work target alone does not prove a better total return.
