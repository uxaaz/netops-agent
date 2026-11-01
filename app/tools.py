"""netops-agent / app/tools.py

Agent 可调用的运维工具（Function Calling 契约 + 本地实现）。

工具契约与 netops-mcp 项目保持一致（同名、同参数 schema、同错误码），
区别是本文件直接以 Python 函数形式供 Agent 调用，不经过 MCP 协议。

所有工具只做模拟/生成：不连接真实设备、不下发配置、不调用外部系统。
"""

from __future__ import annotations

import datetime as _dt
import re
from typing import Any

TOOL_ERRORS = ("DEVICE_NOT_FOUND", "INVALID_ARGS", "INVALID_TRANSITION", "TICKET_NOT_FOUND", "TOOL_NOT_FOUND")


class ToolError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> dict:
        return {"ok": False, "code": self.code, "message": self.message}


DEVICES: dict[str, dict[str, Any]] = {
    "SW-CORE-01": {
        "name": "核心交换机",
        "ip": "10.10.1.1",
        "vendor": "Huawei",
        "model": "S5731-H48T4XC",
        "site": "总部机房",
        "cpuPercent": 31,
        "memPercent": 46,
        "temperatureC": 42,
        "latencyMs": 2,
        "optical": {"rxDbm": -3.2, "txDbm": -2.8},
        "ports": [
            {"name": "GE0/0/1", "status": "up", "peer": "SW-ACC-01"},
            {"name": "GE0/0/24", "status": "up", "peer": "FW-EDGE-01"},
            {"name": "GE0/0/48", "status": "down", "peer": "-"},
        ],
    },
    "SW-ACC-01": {
        "name": "办公楼接入交换机",
        "ip": "10.10.2.11",
        "vendor": "Huawei",
        "model": "S5735-L24T4X",
        "site": "办公楼 3F 弱电间",
        "cpuPercent": 18,
        "memPercent": 33,
        "temperatureC": 38,
        "latencyMs": 4,
        "optical": None,
        "ports": [{"name": "GE0/0/1", "status": "up", "peer": "SW-CORE-01"}],
    },
    "SW-PROD-01": {
        "name": "生产控制网工业交换机",
        "ip": "10.20.3.2",
        "vendor": "Huawei",
        "model": "S5735I-S24T4X",
        "site": "生产区控制柜",
        "cpuPercent": 12,
        "memPercent": 27,
        "temperatureC": 51,
        "latencyMs": 6,
        "optical": {"rxDbm": -9.8, "txDbm": -2.1},
        "ports": [{"name": "XGE0/0/1", "status": "up", "peer": "FW-PROD-01"}],
    },
    "FW-EDGE-01": {
        "name": "出口防火墙",
        "ip": "10.10.1.254",
        "vendor": "Huawei",
        "model": "USG6000E",
        "site": "总部机房",
        "cpuPercent": 44,
        "memPercent": 52,
        "temperatureC": 47,
        "latencyMs": 1,
        "optical": None,
        "ports": [{"name": "GE1/0/1", "status": "up", "peer": "SW-CORE-01"}],
    },
    "AP-FLOOR3-01": {
        "name": "办公楼 3F 无线 AP",
        "ip": "10.10.2.51",
        "vendor": "H3C",
        "model": "WA6320",
        "site": "办公楼 3F",
        "cpuPercent": 22,
        "memPercent": 39,
        "temperatureC": 36,
        "latencyMs": 9,
        "optical": None,
        "ports": [{"name": "GE0/0/1", "status": "up", "peer": "SW-ACC-01"}],
    },
}

TICKETS: dict[str, dict[str, Any]] = {}
_ticket_seq = 0
SEVERITY_SLA = {"P1": 2, "P2": 8, "P3": 24}
TICKET_FLOW = ["OPEN", "ACK", "FIXED", "CLOSED"]

# ---- Function Calling 用的工具 schema（OpenAI tools 格式）----
TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "inspect_device",
            "description": "采集单台网络设备的运行状态（CPU/内存/温度/端口/光功率/时延）。设备不存在返回 DEVICE_NOT_FOUND。",
            "parameters": {
                "type": "object",
                "properties": {"device": {"type": "string", "description": "设备编号（如 SW-CORE-01）或 IP"}},
                "required": ["device"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cfg_draft",
            "description": "生成华为/思科配置草稿（VLAN/ACL/OSPF），只产出文本不下发。",
            "parameters": {
                "type": "object",
                "properties": {
                    "platform": {"type": "string", "enum": ["huawei", "cisco"]},
                    "task": {"type": "string", "enum": ["vlan", "acl", "ospf"]},
                    "spec": {"type": "object", "description": "vlan={vlanId,name,ports[]} acl={aclNumber,permit[],deny[]} ospf={processId,area,networks[]}"},
                },
                "required": ["platform", "task", "spec"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "log_analyze",
            "description": "分析设备日志，识别断链、光衰、通讯超时、弱口令、物理层错包并给出处理建议。",
            "parameters": {
                "type": "object",
                "properties": {"log": {"type": "string", "description": "日志原文"}},
                "required": ["log"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ticket_create",
            "description": "创建运维工单，P1/P2/P3 对应 SLA 2/8/24 小时。",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "severity": {"type": "string", "enum": ["P1", "P2", "P3"]},
                    "source": {"type": "string"},
                },
                "required": ["title", "severity"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ticket_update",
            "description": "按状态机推进工单：OPEN→ACK→FIXED→CLOSED。",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticketId": {"type": "string"},
                    "to": {"type": "string", "enum": ["ACK", "FIXED", "CLOSED"]},
                },
                "required": ["ticketId", "to"],
            },
        },
    },
]


# ---- 实现 ----
def _find_device(key: Any) -> tuple[str, dict[str, Any]] | None:
    if not isinstance(key, str) or not key:
        return None
    if key in DEVICES:
        return key, DEVICES[key]
    for dev_id, dev in DEVICES.items():
        if dev["ip"] == key:
            return dev_id, dev
    return None


def inspect_device(args: dict[str, Any]) -> dict[str, Any]:
    found = _find_device(args.get("device"))
    if not found:
        raise ToolError("DEVICE_NOT_FOUND", f"设备台账中不存在：{args.get('device')}")
    dev_id, dev = found
    optical = dev.get("optical")
    warnings: list[str] = []
    if optical and optical["rxDbm"] < -15:
        warnings.append("接收光功率低于 -15 dBm，建议清洁尾纤并复测")
    down_ports = [p["name"] for p in dev["ports"] if p["status"] == "down"]
    if down_ports:
        warnings.append(f"端口 down：{', '.join(down_ports)}")
    return {
        "ok": True,
        "device": dev_id,
        "name": dev["name"],
        "ip": dev["ip"],
        "vendor": dev["vendor"],
        "model": dev["model"],
        "site": dev["site"],
        "cpuPercent": dev["cpuPercent"],
        "memPercent": dev["memPercent"],
        "temperatureC": dev["temperatureC"],
        "latencyMs": dev["latencyMs"],
        "optical": optical,
        "ports": dev["ports"],
        "health": "WARN" if warnings else "OK",
        "warnings": warnings,
    }


def _draft_vlan(spec: dict[str, Any], platform: str) -> dict[str, Any]:
    try:
        vlan_id = int(spec.get("vlanId"))
    except (TypeError, ValueError):
        vlan_id = 0
    if not 1 <= vlan_id <= 4094:
        raise ToolError("INVALID_ARGS", "vlanId 需为 1-4094 的整数")
    name = str(spec.get("name") or f"VLAN{vlan_id}")
    ports = spec.get("ports") if isinstance(spec.get("ports"), list) else []
    if platform == "huawei":
        lines = [f"vlan {vlan_id}", f" description {name}", "quit"]
        for p in ports:
            lines += [f"interface {p}", " port link-type access", f" port default vlan {vlan_id}", "quit"]
    else:
        lines = [f"vlan {vlan_id}", f" name {name}", "exit"]
        for p in ports:
            lines += [f"interface {p}", " switchport mode access", f" switchport access vlan {vlan_id}", "exit"]
    warnings = [] if ports else ["未指定端口，配置仅创建 VLAN"]
    return {"ok": True, "task": "vlan", "config": "\n".join(lines), "warnings": warnings}


def _draft_acl(spec: dict[str, Any], platform: str) -> dict[str, Any]:
    try:
        acl_number = int(spec.get("aclNumber"))
    except (TypeError, ValueError):
        acl_number = 0
    if not 2000 <= acl_number <= 3999:
        raise ToolError("INVALID_ARGS", "ACL 编号需为 2000-3999 的整数")
    permit = spec.get("permit") if isinstance(spec.get("permit"), list) else []
    deny = spec.get("deny") if isinstance(spec.get("deny"), list) else []
    lines = [f"acl number {acl_number}"]
    seq = 5
    for rule in deny:
        lines.append(f" rule {seq} deny ip source {rule} any")
        seq += 5
    for rule in permit:
        lines.append(f" rule {seq} permit ip source {rule} any")
        seq += 5
    lines.append("quit")
    warnings = [] if (permit or deny) else ["ACL 为空，默认放通全部流量"]
    return {"ok": True, "task": "acl", "config": "\n".join(lines), "warnings": warnings}


def _draft_ospf(spec: dict[str, Any], platform: str) -> dict[str, Any]:
    try:
        process_id = int(spec.get("processId") or 1)
    except (TypeError, ValueError):
        process_id = 0
    if process_id < 1:
        raise ToolError("INVALID_ARGS", "OSPF 进程号需为正整数")
    networks = spec.get("networks") if isinstance(spec.get("networks"), list) else []
    lines = [f"ospf {process_id} router-id {spec.get('routerId') or '10.255.255.1'}", f" area {spec.get('area') or '0.0.0.0'}"]
    lines += [f"  network {n}" for n in networks]
    lines.append("quit")
    warnings = [] if networks else ["未声明任何 network，OSPF 不会建立邻居"]
    return {"ok": True, "task": "ospf", "config": "\n".join(lines), "warnings": warnings}


def cfg_draft(args: dict[str, Any]) -> dict[str, Any]:
    platform = args.get("platform")
    task = args.get("task")
    spec = args.get("spec")
    if platform not in ("huawei", "cisco"):
        raise ToolError("INVALID_ARGS", "platform 仅支持 huawei / cisco")
    if not isinstance(spec, dict):
        raise ToolError("INVALID_ARGS", "spec 需为对象")
    if task == "vlan":
        return _draft_vlan(spec, platform)
    if task == "acl":
        return _draft_acl(spec, platform)
    if task == "ospf":
        return _draft_ospf(spec, platform)
    raise ToolError("INVALID_ARGS", f"不支持的 task：{task}")


LOG_RULES = [
    ("链路断链", re.compile(r"(LINK-3-UPDOWN|link down|is down|interface down|LINK_STATE[^\n]*down)[^\n]*", re.I), "按物理层→链路层逐段排查：跳线、光模块、尾纤、对端端口"),
    ("光模块/光衰", re.compile(r"(光功率|光衰|optical|rx power|rxpower)[^\n]*", re.I), "检查收光功率，清洁或更换尾纤后复测"),
    ("通讯超时", re.compile(r"(timeout|timed out|通讯超时|请求超时)[^\n]*", re.I), "先 ping 网关与对端，再查上位机服务与端口"),
    ("弱口令/高危端口", re.compile(r"(弱口令|default password|admin/admin|23/tcp|telnet)[^\n]*", re.I), "按等保要求整改口令与端口，关闭 Telnet 改用 SSH"),
    ("物理层错包", re.compile(r"(CRC|error counter|错包|FCS)[^\n]*", re.I), "排查网线与接口，必要时更换端口或线缆"),
]


def log_analyze(args: dict[str, Any]) -> dict[str, Any]:
    log = str(args.get("log") or "")
    if not log.strip():
        raise ToolError("INVALID_ARGS", "log 不能为空")
    hits = []
    for name, pattern, suggestion in LOG_RULES:
        m = pattern.search(log)
        if m:
            hits.append({"category": name, "evidence": m.group(0).strip()[:160], "suggestion": suggestion})
    return {
        "ok": True,
        "categories": hits,
        "summary": "未匹配到已知故障特征，建议人工复核" if not hits else f"识别到 {len(hits)} 类问题",
    }


def ticket_create(args: dict[str, Any]) -> dict[str, Any]:
    global _ticket_seq
    title = str(args.get("title") or "").strip()
    severity = str(args.get("severity") or "").upper()
    if not title:
        raise ToolError("INVALID_ARGS", "title 不能为空")
    if severity not in SEVERITY_SLA:
        raise ToolError("INVALID_ARGS", "severity 仅支持 P1/P2/P3")
    _ticket_seq += 1
    now = _dt.datetime.now().astimezone().replace(microsecond=0)
    ticket = {
        "id": f"WO-{now:%Y%m%d}-{_ticket_seq:03d}",
        "title": title,
        "severity": severity,
        "source": str(args.get("source") or "manual"),
        "status": "OPEN",
        "slaHours": SEVERITY_SLA[severity],
        "createdAt": now.isoformat(),
        "history": [{"status": "OPEN", "at": now.isoformat()}],
    }
    TICKETS[ticket["id"]] = ticket
    return {"ok": True, "ticket": ticket}


def ticket_update(args: dict[str, Any]) -> dict[str, Any]:
    ticket = TICKETS.get(str(args.get("ticketId") or ""))
    if ticket is None:
        raise ToolError("TICKET_NOT_FOUND", f"工单不存在：{args.get('ticketId')}")
    target = str(args.get("to") or "").upper()
    if target not in TICKET_FLOW:
        raise ToolError("INVALID_TRANSITION", f"未知目标状态：{target}")
    idx = TICKET_FLOW.index(ticket["status"])
    if TICKET_FLOW.index(target) != idx + 1:
        nxt = TICKET_FLOW[idx + 1] if idx + 1 < len(TICKET_FLOW) else "（终态）"
        raise ToolError("INVALID_TRANSITION", f"不允许 {ticket['status']} → {target}；下一步只能是 {nxt}")
    at = _dt.datetime.now().astimezone().replace(microsecond=0).isoformat()
    ticket["status"] = target
    ticket["history"].append({"status": target, "at": at})
    return {"ok": True, "ticket": ticket}


_HANDLERS = {
    "inspect_device": inspect_device,
    "cfg_draft": cfg_draft,
    "log_analyze": log_analyze,
    "ticket_create": ticket_create,
    "ticket_update": ticket_update,
}


def call_tool(name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    handler = _HANDLERS.get(name)
    if handler is None:
        raise ToolError("TOOL_NOT_FOUND", f"未知工具：{name}")
    return handler(args or {})
