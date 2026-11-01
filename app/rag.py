"""netops-agent / app/rag.py

零依赖 RAG 检索：markdown 分节切块 + BM25 打分。

设计取舍：
- 不引入 jieba / sklearn / sentence-transformers，避免模型下载与第三方依赖；
- 中文用「字符 bigram」做索引词，英文/数字用词边界切分，检索质量在运维术语场景足够；
- 检索是纯函数式索引，进程内构建一次即可（本项目知识库规模为 6 篇文档 / 数十个块）。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

# 中文字符区间 + 英文数字
_CN = r"\u4e00-\u9fff"
_WORD_RE = re.compile(rf"-?[a-zA-Z0-9][a-zA-Z0-9\-_/.]*[a-zA-Z0-9]|-?[a-zA-Z0-9]|[{_CN}]+")


def tokenize(text: str) -> list[str]:
    """把文本切成检索词：英文/数字保留原词（小写），中文串切成 bigram（单字串保留单字）。"""
    tokens: list[str] = []
    for m in _WORD_RE.finditer(text):
        piece = m.group(0)
        if re.fullmatch(rf"[{_CN}]+", piece):
            if len(piece) == 1:
                tokens.append(piece)
            else:
                tokens.extend(piece[i : i + 2] for i in range(len(piece) - 1))
        else:
            tokens.append(piece.lower())
    return tokens


@dataclass
class Chunk:
    """知识库切片。"""

    chunk_id: str
    doc_id: str
    doc_title: str
    section: str
    text: str
    tokens: Counter = field(default_factory=Counter)
    # 标题 + 节标题的检索词集合：命中它们说明「文档主题匹配」，比正文偶然出现同一个词更有说服力
    heading_tokens: set[str] = field(default_factory=set)

    def to_dict(self) -> dict:
        return {
            "chunkId": self.chunk_id,
            "docId": self.doc_id,
            "docTitle": self.doc_title,
            "section": self.section,
            "text": self.text,
        }


@dataclass
class SearchHit:
    chunk: Chunk
    score: float

    def to_dict(self) -> dict:
        return {**self.chunk.to_dict(), "score": round(self.score, 4)}


def split_markdown(text: str) -> list[tuple[str, str]]:
    """按 `## ` 二级标题切节；没有二级标题时按空行分块。返回 [(section_title, body)]。"""
    lines = text.splitlines()
    sections: list[tuple[str, list[str]]] = []
    current_title = ""
    current_body: list[str] = []

    def flush() -> None:
        body = "\n".join(current_body).strip()
        if body:
            sections.append((current_title, body.splitlines()))

    for line in lines:
        if line.startswith("## "):
            flush()
            current_title = line[3:].strip()
            current_body = []
        elif line.startswith("# "):
            # 一级标题是文档标题，不进正文
            continue
        else:
            current_body.append(line)
    flush()

    if not sections:
        body = "\n".join(lines).strip()
        return [("", body)] if body else []
    return [(title, "\n".join(body)) for title, body in sections]


def _line_groups(text: str) -> list[str]:
    """把 markdown 正文分成不可再拆的最小单元：表格连续行整体算一个单元，其余按行。"""
    groups: list[str] = []
    table_buf: list[str] = []
    for line in text.splitlines():
        if line.lstrip().startswith("|"):
            table_buf.append(line)
            continue
        if table_buf:
            groups.append("\n".join(table_buf))
            table_buf = []
        groups.append(line)
    if table_buf:
        groups.append("\n".join(table_buf))
    return groups


def chunk_text(text: str, size: int = 320, overlap: int = 60) -> list[str]:
    """按行装填切块，表格/代码块等连续结构保持完整，不再从字符中间切断。

    这样做的原因：光功率阈值、ACL 编号范围、SLA 时限等关键信息几乎都在 markdown 表格里，
    一旦按固定字符窗口切开，同一行的「模块类型 + 阈值」会落到两个 chunk，检索命中却答不出来。
    """
    text = text.strip()
    if len(text) <= size:
        return [text] if text else []

    groups = [g for g in _line_groups(text) if g.strip()]
    chunks: list[str] = []
    buf: list[str] = []
    buf_len = 0

    for group in groups:
        glen = len(group) + 1
        # 单个结构（如超长表格）本身超过窗口：独占一个 chunk，不做二次切断
        if glen > size:
            if buf:
                chunks.append("\n".join(buf).strip())
                buf, buf_len = [], 0
            chunks.append(group.strip())
            continue
        if buf_len + glen > size and buf:
            chunks.append("\n".join(buf).strip())
            # 重叠：把上一块尾部若干行带进下一块开头
            tail: list[str] = []
            tail_len = 0
            for prev in reversed(buf):
                tail.insert(0, prev)
                tail_len += len(prev) + 1
                if tail_len >= overlap:
                    break
            buf = tail
            buf_len = tail_len
        buf.append(group)
        buf_len += glen
    if buf:
        chunks.append("\n".join(buf).strip())
    return [c for c in chunks if c]



# 中文功能词：出现在问句里但没有检索价值，查询侧过滤以降低噪声
_STOP_TOKENS = frozenset(
    "什么 应该 怎么 如何 哪些 哪个 请问 一下 我们 你们 帮忙 麻烦 是否 可以 需要 一般 通常 这个 那个 是什 么是".split()
)
# 「按什么顺序/步骤」类问题：带编号列表的块更可能是答案，给它提权
_ORDER_INTENT_RE = re.compile(r"顺序|流程|步骤|先查|怎么排查|如何排查|排查思路")
_ORDERED_STEP_RE = re.compile(r"^\s*\d+[.、)]\s", re.MULTILINE)


def _has_ordered_steps(text: str) -> bool:
    return len(_ORDERED_STEP_RE.findall(text)) >= 3


class KnowledgeBase:
    """知识库索引：构建 chunk → 词频表 → BM25 检索。"""

    def __init__(self, docs: list[tuple[str, str, str]] | None = None) -> None:
        """docs: [(doc_id, doc_title, content)]"""
        self.chunks: list[Chunk] = []
        self.doc_titles: dict[str, str] = {}
        self._df: Counter = Counter()
        self._avg_len: float = 0.0
        if docs:
            self.load(docs)

    # ---- 加载 ----
    def load(self, docs: list[tuple[str, str, str]]) -> None:
        self.chunks.clear()
        self.doc_titles.clear()
        for doc_id, doc_title, content in docs:
            self.doc_titles[doc_id] = doc_title
            for s_idx, (section, body) in enumerate(split_markdown(content)):
                for c_idx, piece in enumerate(chunk_text(body)):
                    chunk = Chunk(
                        chunk_id=f"{doc_id}#{s_idx}-{c_idx}",
                        doc_id=doc_id,
                        doc_title=doc_title,
                        section=section,
                        text=piece,
                    )
                    # 文档标题与节标题对检索很有区分度（「分层定位」「判定阈值」等），重复计入提升权重
                    heading = f"{doc_title} {section} {section}"
                    chunk.heading_tokens = set(tokenize(f"{doc_title} {section}"))
                    chunk.tokens = Counter(tokenize(f"{heading} {piece}"))
                    self.chunks.append(chunk)
        self._build_stats()

    @classmethod
    def from_directory(cls, directory: str | Path) -> "KnowledgeBase":
        """加载目录下的 .md 文件，标题取首行 `# `。"""
        docs: list[tuple[str, str, str]] = []
        for path in sorted(Path(directory).glob("*.md")):
            content = path.read_text(encoding="utf-8")
            title = path.stem
            for line in content.splitlines():
                if line.startswith("# "):
                    title = line[2:].strip()
                    break
            docs.append((path.stem, title, content))
        return cls(docs)

    def _build_stats(self) -> None:
        self._df = Counter()
        for chunk in self.chunks:
            self._df.update(chunk.tokens.keys())
        self._avg_len = (
            sum(sum(c.tokens.values()) for c in self.chunks) / len(self.chunks) if self.chunks else 0.0
        )

    # ---- 检索 ----
    def search(self, query: str, k: int = 3, k1: float = 1.5, b: float = 0.75) -> list[SearchHit]:
        if not self.chunks or not query.strip():
            return []
        q_tokens = tokenize(query)
        if not q_tokens:
            return []
        q_tokens = [t for t in q_tokens if t not in _STOP_TOKENS] or q_tokens
        wants_order = bool(_ORDER_INTENT_RE.search(query))
        n = len(self.chunks)
        hits: list[SearchHit] = []
        for chunk in self.chunks:
            length = sum(chunk.tokens.values()) or 1
            score = 0.0
            for token in set(q_tokens):
                tf = chunk.tokens.get(token, 0)
                if tf == 0:
                    continue
                df = self._df.get(token, 0)
                idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
                denom = tf + k1 * (1 - b + b * length / (self._avg_len or 1))
                score += idf * (tf * (k1 + 1)) / denom
                if token in chunk.heading_tokens:
                    score += idf * 0.6  # 标题命中加分：问题主题与文档主题一致
            if wants_order and score > 0 and _has_ordered_steps(chunk.text):
                score *= 1.3
            if score > 0:
                hits.append(SearchHit(chunk=chunk, score=score))
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:k]

    def stats(self) -> dict:
        return {
            "documents": len(self.doc_titles),
            "chunks": len(self.chunks),
            "vocabulary": len(self._df),
            "avgChunkTokens": round(self._avg_len, 1),
        }
