"""netops-agent / app/eval.py

20 条评测题：覆盖知识库问答（14）、工具调用（4）、越界拒答（2）。

指标口径（写进 eval-report.json，数字来自实跑，不手工填）：
- 检索命中率 retrievalHitRate：KB 类题目中，top-3 检索块至少含 1 个期望关键词的占比
- 回答正确率 answerAccuracy：回答文本包含该题全部期望关键词的占比（全 20 题）
- 幻觉率 hallucinationRate：回答出现该题禁止出现内容（编造数值/越界断言）的占比
- 平均延迟 avgLatencyMs、平均 token avgTokens

运行：python -m app.eval   （或在项目根 `python app/eval.py`）
"""

from __future__ import annotations

import json
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

if __package__ in (None, ""):  # 允许 `python app/eval.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.llm import RuleBasedAgent, build_agent  # noqa: E402
from app.rag import KnowledgeBase  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
KB_DIR = PROJECT_ROOT / "data" / "kb"
REPORT_PATH = PROJECT_ROOT / "eval-report.json"


@dataclass
class EvalCase:
    case_id: str
    question: str
    kind: str  # kb | tool | refuse
    keywords: list[str]
    forbidden: list[str] = field(default_factory=list)
    note: str = ""


CASES: list[EvalCase] = [
    # ---- 知识库问答（14）----
    EvalCase("KB01", "10G 光模块接收光功率低于多少 dBm 需要告警？", "kb", ["-15 dBm"], ["-20 dBm 一定正常"], "光衰阈值"),
    EvalCase("KB02", "工业级宽温模块收光 -9.8 dBm，算故障吗？", "kb", ["正常偏弱"], [], "阈值分模块类型"),
    EvalCase("KB03", "链路 down 应该按什么顺序排查？", "kb", ["物理层"], [], "分层排查"),
    EvalCase("KB04", "端口频繁 up/down 抖动，最可能是什么原因？", "kb", ["光衰", "接触不良"], [], "抖动判定"),
    EvalCase("KB05", "VLAN ID 的合法范围是多少？", "kb", ["4094"], [], "VLAN 范围"),
    EvalCase("KB06", "生产控制网一般用哪一段 VLAN？", "kb", ["100"], [], "VLAN 规划"),
    EvalCase("KB07", "华为设备基本 ACL 的编号范围是多少？", "kb", ["2000"], [], "ACL 编号"),
    EvalCase("KB08", "只写 deny 规则会有什么风险？", "kb", ["默认"], [], "默认策略"),
    EvalCase("KB09", "网络配置变更窗口建议安排在什么时间？", "kb", ["22:00"], [], "变更窗口"),
    EvalCase("KB10", "OSPF 邻居卡在 ExStart 阶段，常见原因是什么？", "kb", ["MTU"], [], "OSPF 状态机"),
    EvalCase("KB11", "OSPF 邻居一直停在 Init，要查什么？", "kb", ["物理"], [], "OSPF 排障"),
    EvalCase("KB12", "P1 工单的恢复时限是多少？", "kb", ["2 小时"], [], "SLA"),
    EvalCase("KB13", "P3 工单的响应时限是多少？", "kb", ["4 小时"], [], "SLA"),
    EvalCase("KB14", "同一设备光功率告警连续触发，告警去重窗口是多久？", "kb", ["10 分钟"], [], "去重窗口"),
    # ---- 工具调用（4）----
    EvalCase("TL01", "SW-CORE-01 现在什么状态？", "tool", ["CPU 31", "WARN"], [], "设备巡检"),
    EvalCase("TL02", "帮我看下 SW-PROD-01 的光功率", "tool", ["-9.8"], [], "光功率巡检"),
    EvalCase("TL03", "给 VLAN 120 生成华为配置命令", "tool", ["vlan 120"], [], "配置草稿"),
    EvalCase("TL04", "日志里出现 link down，帮我分析一下", "tool", ["链路断链"], [], "日志分析"),
    # ---- 越界拒答（2）----
    EvalCase("RF01", "你们公司食堂几点开饭？", "refuse", ["没有足够信息"], ["18:00"], "知识库外问题"),
    EvalCase("RF02", "预测一下下个月哪台交换机会先坏？", "refuse", ["没有足够信息"], ["SW-CORE-01 会坏"], "不可预测问题"),
]


def run_eval(agent=None, kb: KnowledgeBase | None = None) -> dict:
    kb = kb or KnowledgeBase.from_directory(KB_DIR)
    agent = agent or build_agent(kb)

    rows: list[dict] = []
    for case in CASES:
        answer = agent.answer(case.question)
        # 检索是否命中期望关键词（KB 类用 top-3，工具类看工具是否调用成功）
        retrieval_hit = False
        if case.kind == "kb":
            hits = kb.search(case.question, k=3)
            retrieval_hit = any(any(kw in h.chunk.text for kw in case.keywords) for h in hits)
        elif case.kind == "tool":
            retrieval_hit = bool(answer.tool_calls) and all(c.get("ok") for c in answer.tool_calls)
        else:
            retrieval_hit = answer.refused

        correct = all(kw in answer.text for kw in case.keywords)
        hallucinated = any(fb in answer.text for fb in case.forbidden)

        rows.append(
            {
                "caseId": case.case_id,
                "kind": case.kind,
                "question": case.question,
                "answer": answer.text,
                "keywords": case.keywords,
                "retrievalHit": retrieval_hit,
                "correct": correct,
                "hallucinated": hallucinated,
                "refused": answer.refused,
                "toolCalls": [c["name"] for c in answer.tool_calls],
                "latencyMs": answer.latency_ms,
                "tokens": answer.tokens,
                "note": case.note,
            }
        )

    kb_cases = [r for r in rows if r["kind"] == "kb"]
    latencies = [r["latencyMs"] for r in rows]
    tokens = [r["tokens"] for r in rows]
    total_tokens = sum(tokens)
    report = {
        "generatedAt": datetime.now().astimezone().replace(microsecond=0).isoformat(),
        "agent": type(agent).__name__,
        "corpus": kb.stats(),
        "totalCases": len(rows),
        "retrievalHitRate": round(sum(1 for r in kb_cases if r["retrievalHit"]) / len(kb_cases) * 100, 1),
        "answerAccuracy": round(sum(1 for r in rows if r["correct"]) / len(rows) * 100, 1),
        "hallucinationRate": round(sum(1 for r in rows if r["hallucinated"]) / len(rows) * 100, 1),
        "refusalRate": round(sum(1 for r in rows if r["refused"]) / len(rows) * 100, 1),
        "avgLatencyMs": round(statistics.mean(latencies), 1),
        "p95LatencyMs": round(sorted(latencies)[max(0, int(len(latencies) * 0.95) - 1)], 1),
        "avgTokens": round(statistics.mean(tokens), 1),
        "totalTokens": total_tokens,
        "byKind": {
            kind: {
                "count": sum(1 for r in rows if r["kind"] == kind),
                "correct": sum(1 for r in rows if r["kind"] == kind and r["correct"]),
            }
            for kind in ("kb", "tool", "refuse")
        },
        "cases": rows,
    }
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def _print_summary(report: dict) -> None:
    print(f"Agent: {report['agent']}   语料: {report['corpus']}")
    print(f"题数: {report['totalCases']}")
    print(f"检索命中率: {report['retrievalHitRate']}%")
    print(f"回答正确率: {report['answerAccuracy']}%")
    print(f"幻觉率: {report['hallucinationRate']}%")
    print(f"平均延迟: {report['avgLatencyMs']} ms   P95: {report['p95LatencyMs']} ms")
    print(f"平均 token: {report['avgTokens']}   总 token: {report['totalTokens']}")
    print("\n| 题号 | 类型 | 检索命中 | 正确 | 幻觉 | token | 问题 |")
    print("|------|------|----------|------|------|-------|------|")
    for r in report["cases"]:
        print(
            f"| {r['caseId']} | {r['kind']} | {'是' if r['retrievalHit'] else '否'} | "
            f"{'OK' if r['correct'] else 'FAIL'} | {'HALLU' if r['hallucinated'] else '-'} | {r['tokens']} | {r['question']} |"
        )
    print(f"\n报告写入：{REPORT_PATH}")


if __name__ == "__main__":
    # Windows 控制台默认 GBK，中文/符号混排会 UnicodeEncodeError，这里强制 UTF-8
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    kb_dir = KB_DIR
    if not kb_dir.exists():
        raise SystemExit(f"知识库目录不存在：{kb_dir}")
    knowledge = KnowledgeBase.from_directory(kb_dir)
    _print_summary(run_eval(RuleBasedAgent(knowledge), knowledge))
