# 第三方来源与许可

原创应用代码采用 Apache-2.0，见 [LICENSE](LICENSE)。依赖保留各自许可；项目许可不替换第三方许可，也不授予任何厂商商标使用权。

| 材料 | 来源 / 版本 | 许可与处理 |
| --- | --- | --- |
| React、React DOM、scheduler | `ui/package-lock.json` 锁定；[React](https://github.com/facebook/react) | MIT，实际安装包的完整声明保留在 `ui/public/THIRD_PARTY_NOTICES.txt` |
| xterm.js / addon-fit | 6.0.0 / 0.11.0；[xterm.js](https://github.com/xtermjs/xterm.js) | MIT，同一 notices 文件保留完整声明 |
| Questone2 平台测试文件 | [固定上游目录](https://github.com/kamelnetworks/sonic-buildimage/tree/837e6f73567bd66c031b28223b8b0823865999f4/device/celestica/x86_64-cel_questone_2-r0/Questone_2) | 上游 Apache-2.0；原声明、来源及修改记录见 `tests/fixtures/questone2/` 和 `NOTICE` |
| HPE/Celestica 逻辑 cage/lane 表 | 从上述公开平台数据整理 | Apache-2.0 派生数据；无设备序列号、MAC 或校准后的物理布局 |
| 构建工具 | TypeScript、Vite、React Vite 插件、Prettier、类型定义及传递依赖 | 由 npm 安装，版本与完整性在 lockfile；不随交换机运行包分发开发依赖 |

Python 后台自身仅依赖标准库；实机模式调用 SONiC 的现有工具、PAM、systemd、OpenSSH/IPMI。项目不分发 SONiC 镜像、Broadcom SDK 二进制或 BMC 固件。

界面为独立实现：本地图标使用内联 SVG，字体为系统字体，无 NVIDIA 原厂图片、logo、CSS 或脚本。Onyx 官方文档仅作为导航和操作组织参考，不代表官方授权、完整功能等价或联合产品。

可选 BMC 适配根据 Linux USB、IPMI、OpenSSH 文档和 AMI/OCP 公开实现研究后独立编写，未包含 AMI/Linux 源代码。资料链接见附件说明；其行为必须在目标固件上另行验证。

源码依赖和生成静态包可能包含不同的第三方组件。修改依赖后应更新对应声明并重新审查 `npm audit`；不要把锁定版本理解为持续安全保证。
