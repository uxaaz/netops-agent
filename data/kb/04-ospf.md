# OSPF 邻居建立与路由排障

## 基础配置（华为 VRP）

```text
ospf 1 router-id 10.255.255.1
 area 0.0.0.0
  network 10.10.1.0 0.0.0.255
  network 10.10.2.0 0.0.0.255
 quit
```

`router-id` 建议使用 Loopback 地址，全网唯一；`area 0.0.0.0` 为骨干区域，非骨干区域必须与骨干区域直连或通过虚连接打通。

## 邻居无法建立的排查顺序

OSPF 邻居状态从 `Down → Init → 2-Way → ExStart → Exchange → Loading → Full`。卡在不同阶段原因不同：

| 卡在 | 常见原因 | 排查命令 |
| --- | --- | --- |
| Down / Init | 物理不通、组播被 ACL 拦截、接口未宣告 | `display ospf interface` |
| 2-Way | 广播网络里 DR/BDR 选举正常（DROther 之间保持 2-Way 属正常） | `display ospf peer` |
| ExStart | MTU 不一致、Router-ID 冲突 | `display ospf peer verbose` |
| Exchange/Loading | LSA 过大或认证不匹配 | `display ospf error` |
| Full 但无路由 | 区域类型（Stub/NSSA）或路由策略过滤 | `display ospf lsdb` |

必要条件：**Area ID 一致、Hello/Dead 定时器一致、认证方式与密钥一致、MTU 一致、网络类型一致**。

## 常见误配置

- 只配了 `area` 没配 `network`：邻居不起来，且没有任何路由。
- 把接口放进 OSPF 后又配 `silent-interface`：接口不再收发 Hello，邻居断开。
- 两台设备 Router-ID 相同：ExStart 阶段反复重传，长期无法到 Full。
- 骨干区域被切成两段：需要配置 Virtual-Link，否则非骨干区域路由无法互通。
