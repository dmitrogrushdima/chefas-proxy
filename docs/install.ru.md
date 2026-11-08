## Установка на ферме

**1. Скачайте архив.** Откройте [Releases → Assets](https://github.com/dmitrogrushdima/chefas-proxy/releases).
Для Windows x64 нужен `chefas-proxy-windows-x64.zip`, для Ubuntu 24.04 x64 —
`chefas-proxy-linux-x64.tar.gz`. Если нужный файл ещё не выложен, возьмите
готовый пилотный архив у сопровождающего проекта. Кнопка **Code → Download ZIP**
скачивает исходники. В готовой сборке держите исполняемый файл рядом с `_internal`.

**2. Запустите на компьютере в сети ASIC.** На Windows откройте `start.cmd`,
укажите адрес и порт своего пула в созданном `chefas-proxy.json` и запустите
`start.cmd` снова. На Ubuntu после распаковки выполните:

```bash
cd chefas-proxy-linux-x64
sudo bash install.sh
sudoedit /etc/chefas-proxy/chefas-proxy.json
sudo systemctl enable --now chefas-proxy
sudo journalctl -u chefas-proxy -n 30 --no-pager
```

Пример полей для основного пула ViaBTC:

```json
{
  "pool_host": "btc.viabtc.io",
  "pool_port": 3333
}
```

**3. Подключите один ASIC.** Скопируйте из консоли или журнала строку
`ASIC URL` для вашей сети, например `stratum+tcp://192.168.1.50:3333`.
Впишите её в `Pool 1`, оставьте своё имя воркера и пароль, а прямой адрес
пула сохраните в `Pool 2` как резерв. Когда пул примет первую шару, можно
подключать остальные ASIC.

Внешний IP, центральный сервер и установка Python оператору не нужны.
Панель на компьютере с прокси доступна по `http://127.0.0.1:8080`.

[English](../README.md#install-on-your-farm) · [简体中文](install.zh-CN.md)
