# 可选 Questone2 / AMI USB 带内接入参考

此目录不是通用 BMC 安装器。Web 安装程序不会启动这些脚本。示例不包含真实设备地址、MAC、serial、凭据或主机公钥。

`questone2-usb-profile.example.json` 故意保留无法通过校验的 USB 拓扑占位符；其中链路本地地址也只是示例。先以只读方式确认目标 USB VID/PID、serial、物理路径、驱动、BMC 已有地址和路由冲突，另行准备 root-only 实际配置。不要从型号名称推导这些字段。

主机脚本仅管理精确匹配的 USB 接口，使用 `/32` 地址和仅本机可用的专用策略路由，不修改默认路由、DNS、全局转发或 SONiC CONFIG_DB。它拒绝接管未知地址/规则，记录自己创建的资源并支持停止恢复。SONiC 可能将无过滤的 `local` 规则编号为 1001；只允许这种精确例外。

```sh
# 先在安全位置填写实际 profile；这里的 --check 只读，不创建网络状态。
sudo python3 host_usb_link.py --check --config /path/to/reviewed-profile.json
```

`--run` 才会配置和监督接口；systemd 单元只是安装模板，路径和目标配置必须经部署者确认。服务重启需要保存原接口基线，不能把临时实验配置当作可接管的配置。回退只移除日志中属于本脚本的资源，遇到外部修改会拒绝覆盖。

旧 AMI 固件的 USB 收发卡死恢复是额外、默认关闭的功能。只有对 VID `046b` / PID `ffb0`、CDC 驱动和配置 2 完成独立验证后，才可在实际 profile 中设置 `transport_recovery: "questone2-bmc-0.32-cdc"`。运行时仍会与该 root profile 的完整 USB 身份精确比对。发布版不内置任何某台设备的 serial、拓扑或 IP。

启用后，每 10 秒对固定 BMC 地址 TCP 22 检查一次，总超时 1 秒。只有连续 3 次连接超时才可能请求 USBDEVFS_RESET；拒绝连接或 SSH 服务响应异常不会触发。限频为 5 分钟最多 1 次、滚动一小时最多 3 次，跨服务重启保留。设备 fd 和配置在打开前后复核，复位子进程结果不明时禁止继续自动复位。不要删除记录绕过限频。USB 复位是硬件操作，必须在目标固件验证；它不是 BMC 整机重启。

`sonic-web-usb.sh` 是特定旧 AMI/BusyBox 路径布局的 BMC 侧参考，默认拒绝执行。它含合成示例地址，可能启动现有 SSH 服务；使用前必须审核全部路径、地址、监听限制和启动机制，并明确设置 `BMC_USB_ADAPTER_REVIEWED=1`。这个确认变量不代替实机核验。脚本不会更改账户或密码，也不提供默认凭据。

本地测试：

```sh
sudo python3 -m unittest -v test_host_usb_link.py test_bmc_usb_guard.py
# 可选；只在开发机全新 namespace 中验证 Linux 路由语义：
sudo unshare --net python3 test_linux_policy.py
```

这些测试使用合成身份和数据，不连接真实 BMC，不调用真实 USB reset。单元测试通过不能替代服务权限、目标 USB 重枚举、BMC 启动持久性及带固定公钥 SSH 的实机验收。

资料：[ip-rule](https://man7.org/linux/man-pages/man8/ip-rule.8.html)、[Linux USB reset 实现](https://github.com/torvalds/linux/blob/v6.12/drivers/usb/core/devio.c#L1499)、[OCP MegaRAC USB gadget](https://github.com/opencomputeproject/OCP-HM-MegaRAC_Community_Edition/blob/a7584dca1fb709e557b9393ecfcaa2df00e00788/meta-ami/meta-common/recipes-ami/host-interface/host-interface/configure_usb_gadget.sh)、[AMI USB reset 状态清理参考](https://github.com/ami-megarac/OSSW/blob/f85e00204c85c79b9555db581b42443bab472e10/SPX-12/usbe-src/coreusb.c#L598)。公开源码资料与实际旧固件不是同一构建，不能据此保证兼容。
