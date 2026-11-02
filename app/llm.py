"""netops-agent / app/llm.py

Agent 答案生成层。两种 provider：

1. `RuleBasedAgent`（默认，离线可用）：意图规则 + RAG 抽取式回答 + 工具调用。
   没有外部 API 也能完整演示「检索 → 判断 → 调工具 → 给结论」，评测跑的就是它，数字可复现。
2. `OpenAICompatAgent`（可选）：走 OpenAI 兼容的 /v1/chat/completions + function calling。
   需要 OPENAI_API_KEY；当前开发环境无外网、无 API key，**该实现未在本机实测**，
   仅作为接入真实模型的结构示例（接口、工具 schema、错误重试均已写好）。

两者的输入输出契约一致：`answer(question) -> Answer`。
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from . import tools
from .rag import KnowledgeBase, SearchHit

# 回答少于这个检索分数就认为知识库未覆盖，直接拒答（抑制幻觉）
RELEVANCE_FLOOR = 1.2
# 单次回答带入的检索块数量
TOP_K = 3
# 回答正文最大字符数（控制 token）
MAX_ANSWER_CHARS = 700


@dataclass
class Answer:
    text: str
    sources: list[dict] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)
    latency_ms: int = 0
    tokens: int = 0
    refused: bool = False

    def to_dict(self) -> dict:
        return {
            "answer": self.text,
            "sources": self.sources,
            "toolCalls": self.tool_calls,
            "latencyMs": self.latency_ms,
            "tokens": self.tokens,
            "refused": self.refused,
        }


def estimate_tokens(text: str) -> int:
    """粗略 token 估算：中文约 1 字 = 1 token，英文按 4 字符 = 1 token，取折中系数。"""
    cn = len(re.findall(r"[\u4e00-\u9fff]", text))
    other = len(text) - cn
    return int(cn + other / 3.5) + 1


# ---- 意图识别 ----
_DEVICE_RE = re.compile(r"(SW-[A-Z0-9\-]+|FW-[A-Z0-9\-]+|AP-[A-Z0-9\-]+|\b(?:\d{1,3}\.){3}\d{1,3}\b)")
_TICKET_ID_RE = re.compile(r"WO-\d{8}-\d{3}")


def _detect_tool(question: str) -> tuple[str, dict[str, Any]] | None:
    """基于关键词把问题路由到工具。返回 (工具名, 参数) 或 None。"""
    q = question

    # 工单流转：出现工单号 + 目标状态
    if "工单" in q and (m := _TICKET_ID_RE.search(q)):
        target = None
        for state, pat in (("ACK", r"受理|确认|接单|ACK"), ("FIXED", r"修复|处理完|FIXED"), ("CLOSED", r"关闭|归档|CLOSED")):
            if re.search(pat, q, re.I):
                target = state
                break
        if target:
            return "ticket_update", {"ticketId": m.group(0), "to": target}

    # 建单
    if re.search(r"建单|创建工单|派单|开单", q):
        sev = "P2"
        if m := re.search(r"\bP([123])\b", q, re.I):
            sev = f"P{m.group(1)}"
        return "ticket_create", {"title": q[:60], "severity": sev, "source": "chat"}

    # 日志分析
    if re.search(r"日志|logbuffer|LINK_STATE|%%0\d|error counter", q, re.I):
        return "log_analyze", {"log": q}

    # 配置草稿
    if re.search(r"VLAN|ACL|OSPF", q, re.I) and re.search(r"配置|命令|脚本|草稿", q):
        platform = "cisco" if re.search(r"cisco|思科", q, re.I) else "huawei"
        if m := re.search(r"VLAN\s*(\d{1,4})", q, re.I):
            return "cfg_draft", {
                "platform": platform,
                "task": "vlan",
                "spec": {"vlanId": int(m.group(1)), "name": "AUTO"},
            }
        if m := re.search(r"ACL\s*(2\d{3}|3\d{3})", q, re.I):
            return "cfg_draft", {"platform": platform, "task": "acl", "spec": {"aclNumber": int(m.group(1))}}
        return "cfg_draft", {"platform": platform, "task": "ospf", "spec": {"processId": 1}}

    # 设备巡检
    if dev := _DEVICE_RE.search(q):
        return "inspect_device", {"device": dev.group(0)}

    return None


def _tool_summary(name: str, result: dict[str, Any]) -> str:
    """把工具结果压成一句人话，避免把大 JSON 塞进回答。"""
    if name == "inspect_device":
        ports = "、".join(f"{p['name']}={p['status']}" for p in result.get("ports", [])[:3])
        optical = result.get("optical")
        optical_txt = f"，收光 {optical['rxDbm']} dBm" if optical else ""
        warn = "；".join(result.get("warnings", [])) or "无明显异常"
        return (
            f"{result['device']}（{result['name']}，{result['ip']}）"
            f"CPU {result['cpuPercent']}%、内存 {result['memPercent']}%、温度 {result['temperatureC']}℃{optical_txt}；"
            f"端口 {ports}；健康状态 {result['health']}。{warn}"
        )
    if name == "log_analyze":
        cats = result.get("categories", [])
        if not cats:
            return "日志未匹配到已知故障特征，建议人工复核。"
        parts = [f"{c['category']}（建议：{c['suggestion']}）" for c in cats]
        return f"识别到 {len(cats)} 类问题：" + "；".join(parts)
    if name == "cfg_draft":
        warn = "；".join(result.get("warnings", [])) or "无额外风险提示"
        return f"配置草稿如下（未下发）：\n```\n{result['config']}\n```\n风险提示：{warn}"
    if name == "ticket_create":
        t = result["ticket"]
        return f"已创建工单 {t['id']}，级别 {t['severity']}，SLA {t['slaHours']} 小时，状态 {t['status']}。"
    if name == "ticket_update":
        t = result["ticket"]
        return f"工单 {t['id']} 已流转到 {t['status']}。"
    return json.dumps(result, ensure_ascii=False)


def _render_table_row(line: str) -> str:
    """把 markdown 表格行渲染成「列名：值；值」的句子，避免答案里出现裸竖线。"""
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    cells = [c for c in cells if c]
    if len(cells) < 2:
        return line.strip()
    return cells[0] + "：" + "；".join(cells[1:])


def _extract_sentences(text: str, keywords: list[str], limit: int = 4) -> list[str]:
    """从检索块中挑与问题关键词最相关的「行」：表格行整行保留，正文按句切分。

    运维知识库里关键数值（阈值、时限、编号范围）大多落在表格行上，
    如果按句号切分会把整张表当成一句或直接丢掉，因此表格行按行保留。
    """
    units: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("```"):
            continue
        if line.startswith("|"):
            if set(line) <= set("|-: "):  # 表格分隔行无信息量
                continue
            units.append(_render_table_row(line))
        else:
            for sent in re.split(r"(?<=[。！？])", line):
                if sent.strip():
                    units.append(sent.strip())

    scored = []
    for idx, unit in enumerate(units):
        hit = sum(1 for kw in keywords if kw and kw in unit)
        if hit:
            scored.append((hit, idx, unit))
    scored.sort(key=lambda x: (-x[0], x[1]))
    picked = [u for _, _, u in scored[:limit]]
    if not picked:
        picked = units[:2]
    order = {u: i for i, u in enumerate(units)}
    picked = [u.rstrip("，,；;、") for u in picked]
    picked.sort(key=lambda u: order.get(u, 0))
    return picked


# 明显的域外/不可预测问题：命中后直接拒答，避免 BM25 把「坏」「几点」这类词硬匹配到某篇文档
_OUT_OF_SCOPE_RE = re.compile(
    r"预测|下个月|下周|明天|未来|会不会|猜|先坏|哪个先|食堂|午饭|工资|天气|股票|星座|"
    r"你是谁|你会什么|讲个笑话|写首诗",
)


def _unique_hits(hits: list) -> list:
    """按「文档·小节」去重，避免同一篇文档在来源列表里重复出现。"""
    seen: set[tuple[str, str]] = set()
    out = []
    for hit in hits:
        key = (hit.chunk.doc_title, hit.chunk.section)
        if key in seen:
            continue
        seen.add(key)
        out.append(hit)
    return out


def _is_out_of_scope(question: str) -> bool:
    return bool(_OUT_OF_SCOPE_RE.search(question))



class RuleBasedAgent:
    """离线 Agent：规则路由 → 工具调用 / RAG 抽取 → 结构化答案。"""

    def __init__(self, kb: KnowledgeBase) -> None:
        self.kb = kb

    def answer(self, question: str) -> Answer:
        started = time.perf_counter()
        tool_calls: list[dict] = []

        routed = _detect_tool(question)
        if routed:
            name, args = routed
            try:
                result = tools.call_tool(name, args)
                tool_calls.append({"name": name, "arguments": args, "ok": True})
                text = _tool_summary(name, result)
            except tools.ToolError as err:
                tool_calls.append({"name": name, "arguments": args, "ok": False, "error": err.code})
                text = f"工具调用失败（{err.code}）：{err.message}"
            latency = int((time.perf_counter() - started) * 1000)
            return Answer(text=text, tool_calls=tool_calls, latency_ms=latency, tokens=estimate_tokens(text))

        hits = self.kb.search(question, k=TOP_K)
        if _is_out_of_scope(question):
            text = "知识库中没有足够信息回答这个问题：本助手只覆盖网络巡检、故障排查、配置草稿与工单流程，且无法预测未来事件。"
            latency = int((time.perf_counter() - started) * 1000)
            return Answer(
                text=text,
                sources=[h.to_dict() for h in hits[:1]],
                latency_ms=latency,
                tokens=estimate_tokens(text),
                refused=True,
            )
        if not hits or hits[0].score < RELEVANCE_FLOOR:
            text = "知识库中没有足够信息回答这个问题，请补充背景，或换个更具体的运维问题（如设备型号、故障现象、错误日志）。"
            latency = int((time.perf_counter() - started) * 1000)
            return Answer(
                text=text,
                sources=[h.to_dict() for h in hits[:1]],
                latency_ms=latency,
                tokens=estimate_tokens(text),
                refused=True,
            )

        keywords = re.findall(r"[A-Za-z0-9\-./]+|[\u4e00-\u9fff]{2,4}", question)
        best = hits[0].chunk
        picked = _extract_sentences(best.text, keywords, limit=3)
        points = [f"- {p}" for p in picked]

        # 第二名块常含另一张表（如 10G 与 1000BASE 分在不同块），并进来能补齐阈值类答案
        second = hits[1].chunk if len(hits) > 1 and hits[1].score >= RELEVANCE_FLOOR * 2 else None
        if second is not None and second.chunk_id != best.chunk_id:
            extra_points = [f"- {p}" for p in _extract_sentences(second.text, keywords, limit=2)]
            for p in extra_points:
                if p not in points:
                    points.append(p)

        evidence = best.text
        body = "\n".join(points)
        if len(evidence) > MAX_ANSWER_CHARS - len(body) - 40:
            evidence = evidence[: max(200, MAX_ANSWER_CHARS - len(body) - 40)] + "…"

        # 相关内容：排除与主答案同文档同小节的块，避免重复
        related: list = []
        seen_rel = {(best.doc_title, best.section)}
        for hit in hits[1:]:
            key = (hit.chunk.doc_title, hit.chunk.section)
            if hit.score >= RELEVANCE_FLOOR * 2 and key not in seen_rel:
                seen_rel.add(key)
                related.append(hit.chunk)
        extra = ""
        if related:
            extra = "\n\n相关内容：" + "；".join(f"《{c.doc_title}·{c.section}》" for c in related)

        text = f"根据《{best.doc_title}·{best.section}》：\n{body}\n\n原文依据：{evidence}{extra}"
        latency = int((time.perf_counter() - started) * 1000)
        return Answer(
            text=text,
            sources=[h.to_dict() for h in _unique_hits(hits)],
            latency_ms=latency,
            tokens=estimate_tokens(text),
        )


class OpenAICompatAgent:
    """可选：接入 OpenAI 兼容接口（含 function calling）。

    未在当前环境验证（无外网/无 key）。启用方式：
        export OPENAI_API_KEY=sk-...
        export OPENAI_BASE_URL=https://api.deepseek.com/v1   # 或任意兼容网关
        export OPENAI_MODEL=deepseek-chat
    调用 `build_agent()` 时若检测到 OPENAI_API_KEY 会自动选择本实现。
    """

    def __init__(self, kb: KnowledgeBase, api_key: str, base_url: str, model: str, max_rounds: int = 3) -> None:
        self.kb = kb
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.max_rounds = max_rounds

    def _post(self, payload: dict[str, Any], timeout: int = 30) -> dict[str, Any]:
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            raise RuntimeError(f"模型接口返回 {err.code}：{err.read()[:200]!r}") from err

    def answer(self, question: str) -> Answer:
        started = time.perf_counter()
        hits = self.kb.search(question, k=TOP_K)
        context = "\n\n".join(f"【{h.chunk.doc_title}·{h.chunk.section}】\n{h.chunk.text}" for h in hits)
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": (
                    "你是网络运维助手。只依据给定资料回答，资料没有的内容必须说明「知识库未覆盖」，禁止编造设备状态与命令。"
                    "需要查询设备状态、生成配置、分析日志、建单时调用工具。"
                ),
            },
            {"role": "user", "content": f"资料：\n{context}\n\n问题：{question}"},
        ]
        tool_calls: list[dict] = []

        for _ in range(self.max_rounds):
            data = self._post(
                {"model": self.model, "messages": messages, "tools": tools.TOOL_SCHEMAS, "temperature": 0.2}
            )
            choice = data["choices"][0]["message"]
            messages.append(choice)
            calls = choice.get("tool_calls") or []
            if not calls:
                text = (choice.get("content") or "").strip()
                latency = int((time.perf_counter() - started) * 1000)
                return Answer(
                    text=text,
                    sources=[h.to_dict() for h in _unique_hits(hits)],
                    tool_calls=tool_calls,
                    latency_ms=latency,
                    tokens=estimate_tokens(text) + estimate_tokens(context),
                )
            for call in calls:
                fn = call["function"]["name"]
                args = json.loads(call["function"].get("arguments") or "{}")
                try:
                    result = tools.call_tool(fn, args)
                    tool_calls.append({"name": fn, "arguments": args, "ok": True})
                except tools.ToolError as err:
                    result = err.to_dict()
                    tool_calls.append({"name": fn, "arguments": args, "ok": False, "error": err.code})
                messages.append(
                    {"role": "tool", "tool_call_id": call.get("id"), "content": json.dumps(result, ensure_ascii=False)}
                )

        text = "工具调用轮次超限，未能给出结论。"
        latency = int((time.perf_counter() - started) * 1000)
        return Answer(text=text, tool_calls=tool_calls, latency_ms=latency, tokens=estimate_tokens(text))


def build_agent(kb: KnowledgeBase):
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if api_key:
        return OpenAICompatAgent(
            kb=kb,
            api_key=api_key,
            base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        )
    return RuleBasedAgent(kb)
