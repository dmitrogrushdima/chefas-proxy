# 安装 CHEFAS Proxy

[English](quickstart.md) · [Русский](install.ru.md)

## 面板视频

[观看面板演示：18 秒，MP4，俄语界面](assets/dashboard-overview-ru.mp4)。

视频展示一台 ASIC 的真实统计和只读用户界面，矿工名与 IP 已匿名化。
这是界面演示，不是安装过程录像。

## 下载

打开 [Releases → Assets](https://github.com/dmitrogrushdima/chefas-proxy/releases)。
Windows x64 下载 `chefas-proxy-windows-x64.zip`；Ubuntu 24.04 x64 下载
`chefas-proxy-linux-x64.tar.gz`。同时下载 `.sha256` 文件以核对归档。
**Code → Download ZIP** 是开发源代码，不是可直接安装的程序。
解压整个归档，保留可执行文件旁的 `_internal`。安装包已包含 Python。

## Windows

1. 解压到自己的目录并运行 `start.cmd`。
2. 编辑自动打开的 `chefas-proxy.json`，把 `POOL_HOST` 改为自己的 BTC 矿池，
   例如 `"pool_host": "stratum+tcp://btc.viabtc.io:3333"`，保存。
3. 再次运行 `start.cmd`，把控制台中的 **ASIC URL** 填入矿机 Pool 1。
   保留原有矿工名和密码，并保持代理窗口运行。

首次运行会自动生成 `.env` 中的随机凭据，重复运行不会覆盖现有文件。

## Ubuntu

在解压后的目录运行：

```bash
sudo bash install.sh
sudoedit /etc/chefas-proxy/chefas-proxy.json
```

设置自己的 BTC 矿池后启动服务：

```bash
sudo systemctl enable --now chefas-proxy
sudo journalctl -u chefas-proxy -n 30 --no-pager
```

把日志中的 **ASIC URL** 填入 Pool 1。安装程序自动生成私有凭据，
服务以非 root 用户运行。

## 本地面板

在代理电脑打开 **http://127.0.0.1:8080**，使用
`CHEFAS_PROXY_ADMIN_TOKEN` 登录。Windows 从 `.env` 复制该值；Ubuntu：

```bash
sudo sed -n 's/^CHEFAS_PROXY_ADMIN_TOKEN=//p' /etc/chefas-proxy/chefas-proxy.env
```

不要共享环境文件或公开面板端口。远程访问使用
[SSH 隧道](quickstart.md#open-the-dashboard)。用户发行版的 DevFee 目标固定为
矿池确认难度的 1%，不提供维护者私有面板或手动费率控制。

先测试一台 ASIC 并确认矿池接受份额；Pool 2 保留原有直连矿池作为备用。

面板显示矿池接受的工作量、分时算力、拒绝份额、矿工状态和 DevFee 统计。
没有通用的 TH/s 目标；应在没有维修和停机的完整时间窗口内，
与自己的 ASIC 型号通常表现进行比较。过期份额已包含在拒绝份额中。
流量计数是应用层字节，不包括 TCP/TLS 开销；估算节省比例不代表
整台电脑的总网络用量，也不是流量节省保证。
[故障排查](quickstart.md) · [兼容性](compatibility.md)
