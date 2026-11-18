# Questone2 测试数据

来源：[kamelnetworks/sonic-buildimage](https://github.com/kamelnetworks/sonic-buildimage/tree/837e6f73567bd66c031b28223b8b0823865999f4/device/celestica/x86_64-cel_questone_2-r0/Questone_2)，固定 commit `837e6f73567bd66c031b28223b8b0823865999f4`，上游 Apache-2.0，原许可声明见本目录 `LICENSE`。

`hwsku.json`、`platform.json` 和 `port_config.ini` 保留该上游版本。`td3-as13-48f8h.config.bcm` 包含本地 IEEE/HiGig 位图修正；这项修改用于验证静态拆分时无关字段得到保留。

这些是公开平台配置与测试输入，不含设备账户、密码、主机密钥、管理 IP、MAC、序列号或真实业务 CONFIG_DB。它们帮助测试 lane/index 映射、文件事务、错误恢复和读回校验，本身不证明任何新设备已经完成硬件验收。不要把测试文件直接覆盖到交换机。
