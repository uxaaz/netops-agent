"""netops-agent / tests/test_smoke.py

冒烟测试：只用 Python 标准库（unittest），覆盖检索、工具、Agent 与 HTTP 接口。

运行：
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app import tools  # noqa: E402
from app.llm import RuleBasedAgent, _is_out_of_scope  # noqa: E402
from app.rag import KnowledgeBase, chunk_text, tokenize  # noqa: E402
from app.server import AppState, make_handler  # noqa: E402

KB_DIR = PROJECT_ROOT / "data" / "kb"


class TestTokenizer(unittest.TestCase):
    def test_english_and_number_kept(self):
        tokens = tokenize("10GBASE-SR 收光 -15 dBm")
        self.assertIn("10gbase-sr", tokens)
        self.assertIn("-15", tokens)
        self.assertIn("dbm", tokens)

    def test_chinese_bigram(self):
        tokens = tokenize("光模块")
        self.assertIn("光模", tokens)
        self.assertIn("模块", tokens)


class TestChunking(unittest.TestCase):
    def test_table_not_split(self):
        table = "\n".join(
            [
                "| 模块类型 | 告警阈值 |",
                "| --- | --- |",
                "| 10GBASE-SR/LR | 低于 -15 dBm |",
                "| 工业级宽温模块 | 低于 -12 dBm |",
            ]
        )
        body = "引言" * 200 + "\n\n" + table
        chunks = chunk_text(body, size=160, overlap=40)
        # 表格必须完整落在某一个块里，不能出现「模块类型」与「-15 dBm」分家
        self.assertTrue(any("-15 dBm" in c and "模块类型" in c for c in chunks))

    def test_short_text_single_chunk(self):
        self.assertEqual(chunk_text("短文本"), ["短文本"])
        self.assertEqual(chunk_text("   "), [])


class TestRetrieval(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase.from_directory(KB_DIR)

    def test_corpus_loaded(self):
        stats = self.kb.stats()
        self.assertEqual(stats["documents"], 6)
        self.assertGreaterEqual(stats["chunks"], 20)

    def test_optical_threshold_hit(self):
        hits = self.kb.search("10G 光模块接收光功率低于多少需要告警？", k=3)
        self.assertTrue(hits)
        self.assertTrue(any("-15 dBm" in h.chunk.text for h in hits))

    def test_empty_query(self):
        self.assertEqual(self.kb.search("   "), [])


class TestTools(unittest.TestCase):
    def test_inspect_known_device(self):
        result = tools.call_tool("inspect_device", {"device": "SW-CORE-01"})
        self.assertEqual(result["device"], "SW-CORE-01")
        self.assertIn("cpuPercent", result)
        self.assertEqual(result["health"], "WARN")  # 台账里 GE0/0/48 是 down

    def test_inspect_unknown_device(self):
        with self.assertRaises(tools.ToolError) as ctx:
            tools.call_tool("inspect_device", {"device": "SW-NOT-EXIST"})
        self.assertEqual(ctx.exception.code, "DEVICE_NOT_FOUND")

    def test_cfg_draft_invalid_vlan(self):
        with self.assertRaises(tools.ToolError) as ctx:
            tools.call_tool("cfg_draft", {"platform": "huawei", "task": "vlan", "spec": {"vlanId": 5000}})
        self.assertEqual(ctx.exception.code, "INVALID_ARGS")

    def test_cfg_draft_ok(self):
        result = tools.call_tool("cfg_draft", {"platform": "huawei", "task": "vlan", "spec": {"vlanId": 120}})
        self.assertIn("vlan 120", result["config"])

    def test_log_analyze_finds_link_down(self):
        result = tools.call_tool("log_analyze", {"log": "%May  6 10:00:01 SW-CORE-01 %%01IFNET/4/LINK_STATE(l): GE0/0/48 is down"})
        self.assertTrue(any(c["category"] == "链路断链" for c in result["categories"]))

    def test_ticket_lifecycle(self):
        created = tools.call_tool("ticket_create", {"title": "核心交换机端口 down", "severity": "P1", "source": "test"})
        ticket_id = created["ticket"]["id"]
        self.assertEqual(created["ticket"]["slaHours"], 2)
        acked = tools.call_tool("ticket_update", {"ticketId": ticket_id, "to": "ACK"})
        self.assertEqual(acked["ticket"]["status"], "ACK")
        with self.assertRaises(tools.ToolError) as ctx:
            tools.call_tool("ticket_update", {"ticketId": ticket_id, "to": "CLOSED"})
        self.assertEqual(ctx.exception.code, "INVALID_TRANSITION")

    def test_ticket_not_found(self):
        with self.assertRaises(tools.ToolError) as ctx:
            tools.call_tool("ticket_update", {"ticketId": "WO-19700101-999", "to": "ACK"})
        self.assertEqual(ctx.exception.code, "TICKET_NOT_FOUND")

    def test_unknown_tool(self):
        with self.assertRaises(tools.ToolError) as ctx:
            tools.call_tool("no_such_tool", {})
        self.assertEqual(ctx.exception.code, "TOOL_NOT_FOUND")


class TestAgent(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase.from_directory(KB_DIR)
        cls.agent = RuleBasedAgent(cls.kb)

    def test_tool_routing_inspect(self):
        answer = self.agent.answer("SW-CORE-01 现在什么状态？")
        self.assertEqual(answer.tool_calls[0]["name"], "inspect_device")
        self.assertIn("CPU", answer.text)

    def test_kb_answer_has_source(self):
        answer = self.agent.answer("P1 工单的恢复时限是多少？")
        self.assertFalse(answer.refused)
        self.assertIn("2 小时", answer.text)
        self.assertTrue(answer.sources)

    def test_out_of_scope_refused(self):
        self.assertTrue(_is_out_of_scope("预测一下下个月哪台交换机会先坏？"))
        answer = self.agent.answer("你们公司食堂几点开饭？")
        self.assertTrue(answer.refused)
        self.assertIn("没有足够信息", answer.text)

    def test_unknown_topic_refused(self):
        answer = self.agent.answer("量子计算机会不会取代交换机？")
        self.assertTrue(answer.refused)


class TestHttpServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        state = AppState(KB_DIR)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _get(self, path: str) -> tuple[int, dict]:
        with urllib.request.urlopen(f"{self.base}{path}", timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def _post(self, path: str, payload: dict) -> tuple[int, dict]:
        req = urllib.request.Request(
            f"{self.base}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def test_health(self):
        status, body = self._get("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["agent"], "RuleBasedAgent")

    def test_kb_stats(self):
        status, body = self._get("/api/kb")
        self.assertEqual(status, 200)
        self.assertEqual(body["documents"], 6)

    def test_ask(self):
        status, body = self._post("/api/ask", {"question": "给 VLAN 120 生成华为配置命令"})
        self.assertEqual(status, 200)
        self.assertIn("vlan 120", body["answer"])
        self.assertEqual(body["toolCalls"][0]["name"], "cfg_draft")

    def test_ask_empty_question(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/api/ask", {"question": "  "})
        self.assertEqual(ctx.exception.code, 400)

    def test_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/api/nope")
        self.assertEqual(ctx.exception.code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
