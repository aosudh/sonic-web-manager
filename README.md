# SONiC Web Manager

面向 SONiC 的独立 Web 管理应用：React/TypeScript 前端，Python 标准库后台，按 Onyx 风格组织紧凑的交换机管理菜单。它不是 NVIDIA Onyx，也不承诺不同 SONiC 发行版具有相同功能。

当前提供配置查看、逻辑端口图、端口与链路状态、传感器、BMC 只读监控、配置计划与执行、管理员网页终端。菜单中的功能只有后端确认可用时才开放；未支持的页面会说明原因。前端、SONiC 适配层与机型布局分别维护，未知机型使用逻辑端口列表。

## 能力边界

- 普通配置通过参数校验、变更预览、用户确认、写前备份和写后读回执行。失败可能已经产生部分变更，界面会保留结果；不宣称自动回滚。
- 已实现端口、VLAN、LAG、接口地址、静态路由、主机名、NTP 等动作，具体开放范围以运行时能力检查为准。多 ASIC 写入当前禁用。
- Questone2 静态拆分适配只针对代码中固定的 SONiC 构建、指定 cage 和平台文件结构，并要求目标设备的独立验证标记。拆分会重启整台交换机；这不是通用在线动态 breakout。
- 机型图目前为逻辑示意。`index`、ASIC lane、接口名和前面板物理位置不能互相替代。
- 风扇/温度可读。尚未验证的手动 PWM、自动控制恢复接口不会开放；DEMO 调速是模拟功能。
- CPU 每 2 秒独立采样，温度使用摄氏度并显示样本时间。缺失或过期读数保持未知；`ASIC External` 不等同 ASIC 内部结温。
- BMC 页读取固定只读 IPMI 命令与最近 SEL 记录；SOL 配置启用不代表可以连接。BMC 系统终端是单独配置的 SSH，连接 BMC 自身操作系统。

## 本地开发与演示

建议 Linux、Python 3.10+、Node.js 24.13.0 / npm 11。开发依赖不部署到交换机。`package-lock.json` 锁定依赖，使用 `npm ci`。

```sh
cd ui
npm ci
npm test
npm run build
cd ..
python3 -m unittest discover -s tests -v
python3 -m server.app --bind 127.0.0.1 --port 8765 \
  --data-dir ./state --static-dir ui/dist --demo
```

打开 `http://127.0.0.1:8765`，显式 DEMO 账户为 `demo` / `demo`。此账户只存在于回环地址上的演示模式；所有配置为内存模拟，真实串口和终端禁用。实机读取失败不会自动退回演示数据。

前端热更新可另开终端运行 `cd ui && npm run dev`。Vite 只绑定 `127.0.0.1:5173`，代理到同机 8765；生产使用静态文件，不运行 Vite/Node 服务。

附加 USB 适配单元测试只模拟接口和命令；在隔离开发机运行：

```sh
cd deploy/bmc-inband
sudo python3 -m unittest -v test_host_usb_link.py test_bmc_usb_guard.py
```

这些测试不需要交换机。`test_linux_policy.py` 仅供全新隔离 network namespace 验证，拒绝已有网络接口；不要在交换机主网络 namespace 运行它。

## 构建与安装

先构建前端，再生成运行包和源码包，输出目录必须在源码目录之外：

```sh
SOURCE_DATE_EPOCH=0 python3 deploy/build_release.py --output-dir ../sonic-web-release
```

构建脚本固定归档顺序、权限、所有者和时间。相同源码、静态产物及 `SOURCE_DATE_EPOCH` 会生成相同归档；重建前端还应保持 Node/npm 和 lockfile 一致。归档内有 SHA256 文件清单，用于校验完整性，并不替代可信发布渠道或签名。

将运行包复制到目标 SONiC，解压后执行：

```sh
sudo python3 deploy/install.py --port 8443
```

实机需要现有 SONiC CLI/数据库接口、Python 3.10+、libpam、systemd 和 OpenSSL；BMC/终端功能还需要相应工具与权限。安装程序部署本应用，不安装 SONiC 镜像或修改 BMC 固件，也不会自动运行 `deploy/bmc-inband/` 附件。

默认绑定启动时 `eth0` 的 IPv4 管理地址，HTTPS 8443，不监听所有网卡。应用生成独立自签证书，不覆盖 SONiC REST 的证书。使用受信证书或按本地管理流程核验该证书；管理地址变化后，需要重启应用并更新证书 SAN。

```sh
sudo systemctl status sonic-web-manager
sudo journalctl -u sonic-web-manager -n 100
sudo systemctl restart sonic-web-manager
```

运行包位于 `/host/sonic-web/releases`，状态、应用证书和备份位于 `/host/sonic-web/state`。主服务限制为半核 CPU / 384 MiB；终端有独立资源与生命周期限制。升级保留前一版本，可回退应用：

```sh
sudo python3 /host/sonic-web/current/deploy/install.py --rollback
```

这不会回退 SONiC 或业务配置。切换 SONiC 镜像后应重新安装服务并验证接口兼容性。

## 认证、终端与配置

实机使用 PAM 校验现有本地账户。管理员由操作系统 UID/组判定，其他有效账户为只读；密码不写入应用配置。写 API 检查会话、权限、同源和 CSRF。普通配置操作在确认计划后异步执行，配置持久化由保存流程完成。

网页 SONiC 终端以当前认证账户运行，保留该账户已有 sudo 权限，不直接启动 root 登录 shell。管理员在终端输入的命令直接执行，不经过配置计划。每个会话最多 2 个终端、服务最多 4 个；5 分钟无输入或累计 30 分钟关闭。应用不记录终端 I/O；设备自身审计与目标程序仍可记录操作。

BMC SSH 只接受管理员预先配置的固定地址、账户和核验过的主机公钥。`/host/sonic-web/state/bmc-console.json` 必须由 root 持有、权限 0600；不要放密码。以下仅为文档示例，必须替换且完成实际验证后才设置 `verified`：

```json
{"verified": false, "host": "192.0.2.10", "port": 22, "username": "bmc-operator", "host_key": "ssh-ed25519 REPLACE_WITH_VERIFIED_PUBLIC_KEY"}
```

浏览器本地串口是独立的行式控制台，连接管理员电脑选择的串口设备，需要 Web Serial 和安全上下文；它不是远程 SOL，也不是完整 VT100 终端。

配置下载经过脱敏，不是完整恢复备份。不要把私有配置、TLS 私钥、BMC 账户、主机公钥、测试日志或本地状态提交到仓库。

## 可选平台适配

`deploy/bmc-inband/` 保留可审阅的 Questone2/AMI USB 适配代码与测试，所有地址、MAC 和 serial 样本均为示例。默认 profile 不可直接运行，旧固件 USB 恢复默认不启用，BMC 侧脚本还要求显式确认标记。先读附件 README；不要复制示例到未知设备执行。

其他平台需要确认 CLI、数据库 schema、传感器单位、端口映射和权限。`server/sonic.py` 中的能力检查及独立平台 JSON 是适配入口；不能通过删除检查来宣称支持。

## 许可与来源

项目采用 [Apache-2.0](LICENSE)。平台派生数据及第三方依赖保留原有声明，见 [NOTICE](NOTICE)、[THIRD_PARTY.md](THIRD_PARTY.md)。原创贡献归属 aosudh 与贡献者。项目与相关厂商无官方隶属关系。

参考：[SONiC Management Framework](https://github.com/sonic-net/SONiC/blob/master/doc/mgmt/Management%20Framework.md)、[动态拆分设计](https://github.com/sonic-net/SONiC/blob/master/doc/dynamic-port-breakout/sonic-dynamic-port-breakout-HLD.md)、[Onyx Web 界面文档](https://networking-docs.nvidia.com/onyxum/3102002/web-interface-overview)、[Web Serial](https://developer.chrome.com/docs/capabilities/serial)、[xterm.js](https://xtermjs.org/docs/)。
