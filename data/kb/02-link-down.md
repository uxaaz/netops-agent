# 链路中断与端口 down 的分层排查

## 分层定位

链路中断按 OSI 自下而上分层排查，避免一上来就怀疑配置：

1. **物理层**：网线/光纤是否插好、端口指示灯是否亮、光模块是否插紧、跳线是否被压折。
2. **链路层**：`display interface brief` 看端口 `UP/DOWN`；`display port vlan` 看是否被 shutdown 或 VLAN 未放通。
3. **网络层**：`ping` 对端网关与对端设备管理地址；`display arp` 看 ARP 是否学习到。
4. **传输层及以上**：业务端口（如 DCS 的 102/44818 端口）是否被 ACL 拦截。

## 关键命令（华为 VRP）

```text
display interface GigabitEthernet 0/0/5
display interface brief | include DOWN
display logbuffer | include LINK_STATE
display transceiver diagnosis interface GigabitEthernet 0/0/5
display mac-address | include GE0/0/5
```

## 工业现场注意点

- 生产控制网（DCS/PLC）交换机重启会影响产线，**排查优先用只读命令**，禁止直接 `shutdown` 端口做试验。
- 工业交换机 STP 收敛慢（默认 30 秒级），接入设备重启后要等端口完成 STP 状态迁移再判断。
- 电磁干扰环境（变频器、大功率电机旁）网线必须用屏蔽双绞线，屏蔽层单端接地。

## 判定表

| 现象 | 最可能原因 | 下一步 |
| --- | --- | --- |
| 端口 down，对端也 down | 线缆/光模块故障 | 更换跳线或模块 |
| 本端 up，对端 down | 单通，端口协商或对端 shutdown | 检查对端配置 |
| 端口 up 但无流量 | VLAN 未放通或 MAC 未学习 | 查 VLAN 与 MAC 表 |
| 频繁 up/down 抖动 | 光衰临界或接触不良 | 查光功率与 CRC 计数 |
