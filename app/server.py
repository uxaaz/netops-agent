"""netops-agent / app/server.py

零第三方依赖的 HTTP 服务（Python 标准库 http.server）。

为什么不用 FastAPI：本项目开发环境处于无外网沙箱，`pip install fastapi uvicorn` 无法完成。
为了「克隆下来就能跑」，服务层用标准库实现，接口契约保持 REST + JSON，
需要换成 FastAPI 时只替换本文件即可，`app/llm.py`、`app/rag.py`、`app/tools.py` 不受影响。

启动：
    python -m app.server                 # 默认 127.0.0.1:8000
    python -m app.server --port 9000

接口：
    GET  /               对话页面（单文件 HTML）
    GET  /api/health     健康检查
    GET  /api/kb         知识库规模与文档清单
    GET  /api/eval       最近一次评测报告（eval-report.json）
    POST /api/ask        提问，body: {"question": "..."}
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

if __package__ in (None, ""):  # 允许 `python app/server.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import __version__  # noqa: E402
from app.llm import build_agent  # noqa: E402
from app.rag import KnowledgeBase  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
KB_DIR = PROJECT_ROOT / "data" / "kb"
EVAL_REPORT = PROJECT_ROOT / "eval-report.json"

INDEX_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>网络运维知识库 Agent</title>
<style>
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  body { margin: 0; font: 15px/1.6 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
         background: #f4f6f9; color: #1f2430; }
  .wrap { max-width: 820px; margin: 0 auto; padding: 28px 18px 64px; }
  h1 { font-size: 20px; margin: 0 0 4px; }
  .meta { color: #6b7280; font-size: 13px; margin-bottom: 20px; }
  .panel { background: #fff; border: 1px solid #e4e8ef; border-radius: 12px; padding: 16px; margin-bottom: 14px; }
  textarea { width: 100%; min-height: 74px; resize: vertical; padding: 10px 12px; font: inherit;
             border: 1px solid #d6dbe4; border-radius: 8px; outline: none; }
  textarea:focus { border-color: #5b8def; }
  .row { display: flex; gap: 10px; align-items: center; margin-top: 10px; flex-wrap: wrap; }
  button { font: inherit; padding: 8px 18px; border-radius: 8px; border: 1px solid #5b8def;
           background: #5b8def; color: #fff; cursor: pointer; }
  button:disabled { opacity: .55; cursor: default; }
  .samples button { background: #fff; color: #3b5bd9; border-color: #d6dbe4; padding: 5px 10px; font-size: 13px; }
  .ans { white-space: pre-wrap; }
  .tag { display: inline-block; font-size: 12px; color: #3b5bd9; background: #eef2fd;
         border-radius: 6px; padding: 1px 7px; margin-right: 6px; }
  .src { color: #6b7280; font-size: 12px; margin-top: 10px; border-top: 1px dashed #e4e8ef; padding-top: 8px; }
  .refuse { color: #b45309; }
  code { background: #f1f3f7; padding: 1px 5px; border-radius: 4px; font-size: 13px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>网络运维知识库 Agent</h1>
  <div class="meta" id="meta">检索增强问答 · 工具调用 · 越界拒答</div>

  <div class="panel">
    <textarea id="q" placeholder="例如：10G 光模块接收光功率低于多少需要告警？"></textarea>
    <div class="row">
      <button id="send">提问</button>
      <span class="samples" id="samples"></span>
    </div>
  </div>

  <div class="panel" id="result" style="display:none">
    <div id="badges" style="margin-bottom:8px"></div>
    <div class="ans" id="ans"></div>
    <div class="src" id="src"></div>
  </div>
</div>
<script>
const samples = [
  "10G 光模块接收光功率低于多少 dBm 需要告警？",
  "SW-CORE-01 现在什么状态？",
  "给 VLAN 120 生成华为配置命令",
  "P1 工单的恢复时限是多少？",
  "你们公司食堂几点开饭？"
];
const sampleBox = document.getElementById("samples");
samples.forEach(s => {
  const b = document.createElement("button");
  b.textContent = s.length > 14 ? s.slice(0, 14) + "…" : s;
  b.title = s;
  b.onclick = () => { document.getElementById("q").value = s; ask(); };
  sampleBox.appendChild(b);
});

const qEl = document.getElementById("q");
const sendEl = document.getElementById("send");
const resultEl = document.getElementById("result");
const ansEl = document.getElementById("ans");
const badgesEl = document.getElementById("badges");
const srcEl = document.getElementById("src");

async function ask() {
  const question = qEl.value.trim();
  if (!question) return;
  sendEl.disabled = true;
  resultEl.style.display = "block";
  ansEl.textContent = "思考中…";
  badgesEl.innerHTML = "";
  srcEl.textContent = "";
  try {
    const resp = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question })
    });
    const data = await resp.json();
    ansEl.textContent = data.answer || data.message || "（无回答）";
    ansEl.className = "ans" + (data.refused ? " refuse" : "");
    const badges = [];
    badges.push(`<span class="tag">${data.latencyMs} ms</span>`);
    badges.push(`<span class="tag">≈${data.tokens} tokens</span>`);
    if (data.toolCalls && data.toolCalls.length) {
      badges.push(`<span class="tag">工具：${data.toolCalls.map(t => t.name).join(", ")}</span>`);
    }
    if (data.refused) badges.push(`<span class="tag">已拒答</span>`);
    badgesEl.innerHTML = badges.join("");
    if (data.sources && data.sources.length) {
      srcEl.textContent = "检索来源：" + data.sources.map(s => `《${s.docTitle}·${s.section}》`).join("；");
    }
  } catch (err) {
    ansEl.textContent = "请求失败：" + err;
  } finally {
    sendEl.disabled = false;
  }
}
sendEl.onclick = ask;
qEl.addEventListener("keydown", e => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) ask(); });

// 支持 ?q=问题 直接出结果（方便演示、截图和分享链接）
const initial = new URLSearchParams(location.search).get("q");
if (initial) { qEl.value = initial; ask(); }

fetch("/api/kb").then(r => r.json()).then(d => {
  document.getElementById("meta").textContent =
    `检索增强问答 · 工具调用 · 越界拒答　|　知识库 ${d.documents} 篇 / ${d.chunks} 块 / 词表 ${d.vocabulary}`;
}).catch(() => {});
</script>
</body>
</html>
"""


class AppState:
    """进程内共享状态：知识库、Agent、启动时间。"""

    def __init__(self, kb_dir: Path = KB_DIR) -> None:
        self.started_at = time.time()
        self.kb = KnowledgeBase.from_directory(kb_dir)
        self.agent = build_agent(self.kb)

    def health(self) -> dict:
        return {
            "status": "ok",
            "service": "netops-agent",
            "version": __version__,
            "agent": type(self.agent).__name__,
            "uptimeSeconds": round(time.time() - self.started_at, 1),
            "time": datetime.now().astimezone().replace(microsecond=0).isoformat(),
        }


def make_handler(state: AppState):
    class Handler(BaseHTTPRequestHandler):
        server_version = "netops-agent/0.1.0"

        # ---- 工具方法 ----
        def _send_json(self, payload: dict, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, html: str) -> None:
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            if length > 64 * 1024:
                raise ValueError("请求体过大")
            return json.loads(self.rfile.read(length).decode("utf-8"))

        def log_message(self, fmt: str, *args) -> None:  # 精简访问日志
            print(f"[{self.log_date_time_string()}] {self.address_string()} {fmt % args}")

        # ---- 路由 ----
        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                self._send_html(INDEX_HTML)
            elif path == "/api/health":
                self._send_json(state.health())
            elif path == "/api/kb":
                payload = state.kb.stats()
                payload["docs"] = [
                    {"docId": c.doc_id, "title": c.doc_title, "section": c.section}
                    for c in state.kb.chunks[:1]
                ]
                docs = {}
                for chunk in state.kb.chunks:
                    docs.setdefault(chunk.doc_id, {"docId": chunk.doc_id, "title": chunk.doc_title, "sections": []})
                    if chunk.section and chunk.section not in docs[chunk.doc_id]["sections"]:
                        docs[chunk.doc_id]["sections"].append(chunk.section)
                payload["docs"] = list(docs.values())
                self._send_json(payload)
            elif path == "/api/eval":
                if EVAL_REPORT.exists():
                    self._send_json(json.loads(EVAL_REPORT.read_text(encoding="utf-8")))
                else:
                    self._send_json({"error": "尚未生成评测报告，先运行 python -m app.eval"}, status=404)
            else:
                self._send_json({"error": "not found", "path": path}, status=404)

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path != "/api/ask":
                self._send_json({"error": "not found", "path": path}, status=404)
                return
            try:
                data = self._read_json()
            except (ValueError, json.JSONDecodeError) as err:
                self._send_json({"error": "INVALID_JSON", "message": str(err)}, status=400)
                return
            question = str(data.get("question") or "").strip()
            if not question:
                self._send_json({"error": "EMPTY_QUESTION", "message": "question 不能为空"}, status=400)
                return
            if len(question) > 500:
                self._send_json({"error": "QUESTION_TOO_LONG", "message": "question 最多 500 字"}, status=400)
                return
            answer = state.agent.answer(question)
            self._send_json(answer.to_dict())

        def do_OPTIONS(self) -> None:  # noqa: N802
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

    return Handler


def serve(host: str = "127.0.0.1", port: int = 8000, kb_dir: Path = KB_DIR) -> None:
    state = AppState(kb_dir)
    httpd = ThreadingHTTPServer((host, port), make_handler(state))
    print(f"netops-agent {__version__} 已启动：http://{host}:{httpd.server_address[1]}/")
    print(f"知识库：{state.kb.stats()}    Agent：{type(state.agent).__name__}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n收到中断，正在关闭…")
    finally:
        httpd.server_close()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="netops-agent HTTP 服务（零第三方依赖）")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1")
    parser.add_argument("--port", type=int, default=8000, help="监听端口，默认 8000")
    parser.add_argument("--kb", default=str(KB_DIR), help="知识库目录")
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args()
    serve(args.host, args.port, Path(args.kb))
