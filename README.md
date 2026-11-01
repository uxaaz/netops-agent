# netops-agent · 网络运维知识库 Agent

一个**零第三方依赖**、克隆下来就能跑的网络运维问答服务：本地知识库（markdown）+ BM25 检索 + 工具调用 +
越界拒答，附带可复现的 20 题评测集与 24 个单元测试。

> 说明：本项目是「检索增强 + 规则路由」的离线 Agent，不依赖任何大模型 API 即可完成检索、工具调用、
> 引用原文与拒答。仓库同时提供了 `OpenAICompatAgent` 作为接入真实模型的示例实现（当前环境无外网，
> **未在本机实测**，README 与代码注释均已标注）。

## 亮点

| 项 | 结果 | 复现方式 |
| --- | --- | --- |
| 评测集 | 20 题（知识库 14 / 工具 4 / 拒答 2） | `python -m app.eval` |
| 检索命中率 | **100.0%** | 同上，结果写 `eval-report.json` |
| 回答正确率 | **100.0%** | 同上 |
| 幻觉率 | **0.0%**（越界问题全部拒答） | 同上 |
| 平均 token / 延迟 | 200 tokens / 0.1 ms | 同上 |
| 单元测试 | **24 个全部通过** | `python -m unittest discover -s tests -v` |
| 运行依赖 | 无（Python 3.10+ 标准库） | `python -m app.server` |

数字来自本机实跑（报告 `eval-report.json` 已提交，含每题回答与耗时）。

## 目录结构

```
netops-agent/
├── app/
│   ├── rag.py        # markdown 分节 → 行感知切块（表格不切碎）→ BM25 检索
│   ├── tools.py      # 5 个运维工具 + OpenAI function calling schema + 错误码
│   ├── llm.py        # RuleBasedAgent（默认）/ OpenAICompatAgent（可选）+ 拒答策略
│   ├── server.py     # 标准库 HTTP 服务 + 单文件对话页面
│   └── eval.py       # 20 题评测，产出 eval-report.json
├── data/kb/          # 6 篇运维知识库（光模块 / 链路 down / VLAN ACL / OSPF / 工单 SLA / 等保）
├── tests/test_smoke.py
├── eval-report.json  # 实跑评测报告
├── docker-compose.yml
└── README.md
```

## 快速开始

```bash
# 1. 启动服务（默认 http://127.0.0.1:8000）
python -m app.server

# 2. 打开浏览器访问首页，或直接调接口
curl -s -X POST http://127.0.0.1:8000/api/ask \
  -H "Content-Type: application/json" \
  -d '{"question":"10G 光模块接收光功率低于多少 dBm 需要告警？"}'

# 3. 跑评测与测试
python -m app.eval
python -m unittest discover -s tests -v
```

无需 `pip install`：项目只使用标准库。仓库里的 `requirements.txt` 仅用于说明「可选的真实模型客户端」。

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 单文件对话页面 |
| GET | `/api/health` | 健康检查（status/version/agent/uptime） |
| GET | `/api/kb` | 知识库规模与文档清单 |
| GET | `/api/eval` | 最近一次评测报告 |
| POST | `/api/ask` | `{"question": "..."}` → `{answer, sources, toolCalls, latencyMs, tokens, refused}` |

错误码：`EMPTY_QUESTION`(400)、`QUESTION_TOO_LONG`(400)、`INVALID_JSON`(400)、404。

## 检索设计（为什么不是「向量库调包」）

1. **分节切块**：按 `## ` 二级标题切节，节标题参与索引并额外加权（问题命中标题说明主题匹配）。
2. **行感知切块**：markdown 表格连续行视为不可切分单元。早期实现按固定字符窗口切，
   导致「| 10GBASE-SR/LR | 低于 -15 dBm |」被切成两半——检索命中却答不出阈值；改成行感知后修复。
3. **中文 bigram + 英文词边界**：不引入 jieba / 模型下载，运维术语（型号、命令、编号）本身区分度高。
4. **问句停用词**：`什么/应该/怎么` 等无信息量 token 在查询侧过滤。
5. **顺序意图识别**：问「按什么顺序排查」时，含 3 个以上编号步骤的块分数 ×1.3。
6. **拒答**：最高分低于阈值（1.2）或命中「预测/下月/食堂」类域外模式 → 明确拒答，不编造。

## 工具（function calling schema）

| 工具 | 作用 | 关键错误码 |
| --- | --- | --- |
| `inspect_device` | 查设备 CPU/内存/温度/光功率/端口 | `DEVICE_NOT_FOUND` |
| `cfg_draft` | 生成 VLAN/ACL/OSPF 配置草稿（华为/思科） | `INVALID_ARGS` |
| `log_analyze` | 设备日志特征匹配（断链/光衰/超时/弱口令/错包） | `INVALID_ARGS` |
| `ticket_create` | 建工单（P1/P2/P3 → 2/8/24h SLA） | `INVALID_ARGS` |
| `ticket_update` | 工单流转 OPEN→ACK→FIXED→CLOSED | `TICKET_NOT_FOUND`、`INVALID_TRANSITION` |

## 与真实模型对接（可选，未实测）

```bash
export OPENAI_API_KEY=sk-...
export OPENAI_BASE_URL=https://api.deepseek.com/v1
export OPENAI_MODEL=deepseek-chat
python -m app.server      # build_agent() 检测到 key 后自动切换 OpenAICompatAgent
```

`OpenAICompatAgent` 已实现 tools 透传、最多 3 轮工具调用、异常兜底；但当前开发环境无外网，
**这条链路没有跑通过**，接入前请自行验证。

## Docker（未在本机验证）

```bash
docker compose up --build
```

compose 里包含一个 PostgreSQL 服务，用于演示「真实部署时知识库/工单落库」的编排方式；
当前实现使用内存仓储与本地 markdown，**不需要数据库也能跑**。

## 边界与已知限制

- 知识库规模为 6 篇文档 / 26 个块，检索质量结论只在这个规模上验证过；扩到数百篇应换成向量检索。
- `RuleBasedAgent` 的意图路由是正则规则，不是语义理解；说法差异较大时需要补规则。
- 评测集由作者构造（20 题），存在「自己出题自己答」的偏差，已如实标注题目与判分关键词。
- 设备台账、日志样本均为**示例数据**，不对接真实网管系统。

## License

MIT
