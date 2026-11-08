# 在矿场安装 CHEFAS Proxy

[English](../README.md#install-on-your-farm) · [Русский](install.ru.md)

## 1. 下载适合系统的安装包

在 [GitHub Releases](https://github.com/dmitrogrushdima/chefas-proxy/releases) 的 **Assets** 中选择：

| 矿场电脑系统 | 下载文件 |
|---|---|
| Windows x64 | `chefas-proxy-windows-x64.zip` |
| Ubuntu 24.04 x64 | `chefas-proxy-linux-x64.tar.gz` |

如果试用版尚未上传，请向项目维护者索取对应的安装包。GitHub 的
**Code → Download ZIP** 下载的是源代码。解压后，保留可执行文件及其旁边的
`_internal` 目录。独立安装包已包含运行环境和管理面板。

## 2. 在矿机所在的局域网运行

**Windows**：解压后双击 `start.cmd`，在生成的 `chefas-proxy.json` 中填写
BTC 矿池的地址和端口，保存后再运行一次 `start.cmd`。

**Ubuntu**：解压后在终端执行：

```bash
cd chefas-proxy-linux-x64
sudo bash install.sh
sudoedit /etc/chefas-proxy/chefas-proxy.json
sudo systemctl enable --now chefas-proxy
sudo journalctl -u chefas-proxy -n 30 --no-pager
```

例如，使用 ViaBTC 时，在现有配置中设置 `"pool_host": "btc.viabtc.io"`
和 `"pool_port": 3333`。管理面板可在运行代理的电脑上通过
`http://127.0.0.1:8080` 打开。

## 3. 先连接一台 ASIC

从控制台或服务日志中复制矿机所在网络的 **ASIC URL**，
例如 `stratum+tcp://192.168.1.50:3333`。将它填入矿机的 **Pool 1**，
保留原有矿工名和密码。把原来的直连矿池地址留在 **Pool 2** 作为备用。
确认矿池已接受通过代理提交的份额后，再连接更多矿机。

代理在矿场本地运行，矿机通过局域网连接。安装时无需提供矿场的公网 IP。
