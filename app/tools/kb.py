"""Knowledge base: markdown pages -> chunks -> hybrid retrieval (BM25 + dense embeddings, RRF fusion).

Accuracy controls:
  A-KB-01  dense-led hybrid retrieval (dense = paraphrase robustness; small BM25 weight rescues exact tokens such as RATE_LIMIT_429)
  A-KB-02  category is a SOFT preference (small score bonus), not a filter: a mislabelled intent cannot hide the right article
  A-KB-03  minimum-relevance threshold -> returns "no relevant article" instead of loosely related text
           (the agent is instructed to say it doesn't know / escalate rather than improvise)
  A-KB-04  every hit carries a stable source id (doc#chunk) that the validator uses to check grounding
Latency: query embeddings are LRU-cached; BM25 is in-process; dense is optional (falls back to BM25-only).
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from rank_bm25 import BM25Okapi

from app.core.config import ROOT, get_settings
from app.llm.gateway import LLMError, get_gateway
from app.observability.tracing import tracer

KB_DIR = ROOT / "kb"
INDEX_DIR = ROOT / "data" / "kb_index"
_STOP = set("a an and are as at be but by can do does for from has have how i if in is it me my of on or our so that the "
            "their there this to was we what when where which who why will with you your please hi hello thanks thank "
            "need want would could should get got tell about know information info help question questions give show let see look find".split())
_TOKEN = re.compile(r"[a-z0-9_]+")
CATEGORY_GROUPS = {"payments": {"payments", "policy"}, "cards": {"cards", "policy"}, "general": {"general", "policy", "payments", "cards"}}


def _stem(w: str) -> str:
    for suf in ("ing", "ed", "es", "s"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def tokenize(text: str) -> list[str]:
    toks = []
    for t in _TOKEN.findall(text.lower()):
        if t in _STOP:
            continue
        toks.append(_stem(t))
        if "_" in t:  # RATE_LIMIT_429 -> also index the parts
            toks.extend(_stem(p) for p in t.split("_") if p and p not in _STOP)
    return toks


@dataclass
class Chunk:
    id: str
    doc_id: str
    title: str
    category: str
    text: str


@dataclass
class KBHit:
    chunk_id: str
    doc_id: str
    title: str
    category: str
    text: str
    score: float  # relevance in [0,1]: cosine if dense available else normalised BM25
    bm25: float = 0.0
    dense: float | None = None

    def as_dict(self) -> dict:
        return {"source": self.chunk_id, "title": self.title, "category": self.category,
                "score": round(self.score, 3), "text": self.text}


def load_chunks(kb_dir: Path = KB_DIR, max_chars: int = 900) -> list[Chunk]:
    chunks: list[Chunk] = []
    for f in sorted(kb_dir.glob("*.md")):
        raw = f.read_text()
        meta = {}
        body = raw
        if raw.startswith("---"):
            _, fm, body = raw.split("---", 2)
            for line in fm.strip().splitlines():
                k, _, v = line.partition(":")
                meta[k.strip()] = v.strip()
        doc_id = meta.get("id", f.stem)
        title = meta.get("title", doc_id)
        body = re.sub(r"^# .*\n", "", body.strip(), count=1).strip()
        parts, cur = [], ""
        for para in re.split(r"\n\s*\n|\n(?=- )", body):
            if cur and len(cur) + len(para) > max_chars:
                parts.append(cur.strip())
                cur = ""
            cur += para + "\n"
        if cur.strip():
            parts.append(cur.strip())
        for i, p in enumerate(parts):
            chunks.append(Chunk(f"{doc_id}#{i}", doc_id, title, meta.get("category", "general"), f"{title}\n{p}"))
    return chunks


class KnowledgeBase:
    def __init__(self, kb_dir: Path = KB_DIR, index_dir: Path = INDEX_DIR, use_dense: bool | None = None):
        self.kb_dir, self.index_dir = kb_dir, index_dir
        self.chunks: list[Chunk] = []
        self.bm25: BM25Okapi | None = None
        self.emb: np.ndarray | None = None
        self.use_dense = (get_settings().llm_mode == "live") if use_dense is None else use_dense
        self._qcache: dict[str, np.ndarray] = {}
        self.loaded = False

    def _kb_hash(self) -> str:
        h = hashlib.sha256(get_settings().embed_model.encode())
        for f in sorted(self.kb_dir.glob("*.md")):
            h.update(f.name.encode())
            h.update(f.read_bytes())
        return h.hexdigest()[:16]

    async def load(self, rebuild: bool = False):
        self.chunks = load_chunks(self.kb_dir)
        self.bm25 = BM25Okapi([tokenize(c.text) for c in self.chunks])
        self.emb = None
        if self.use_dense:
            meta_f, emb_f = self.index_dir / "meta.json", self.index_dir / "embeddings.npy"
            h = self._kb_hash()
            if not rebuild and meta_f.exists() and emb_f.exists() and json.loads(meta_f.read_text()).get("hash") == h:
                self.emb = np.load(emb_f)
            else:
                try:
                    vecs = await get_gateway().embed([c.text for c in self.chunks], "passage")
                    self.emb = np.array(vecs, dtype=np.float32)
                    self.emb /= np.linalg.norm(self.emb, axis=1, keepdims=True) + 1e-9
                    self.index_dir.mkdir(parents=True, exist_ok=True)
                    np.save(emb_f, self.emb)
                    meta_f.write_text(json.dumps({"hash": h, "n": len(self.chunks), "model": get_settings().embed_model}))
                except LLMError:
                    self.emb = None  # degrade gracefully to BM25-only
        self.loaded = True

    async def _qvec(self, q: str) -> np.ndarray | None:
        if self.emb is None:
            return None
        if q in self._qcache:
            return self._qcache[q]
        try:
            v = np.array((await get_gateway().embed([q], "query"))[0], dtype=np.float32)
        except LLMError:
            return None
        v /= np.linalg.norm(v) + 1e-9
        if len(self._qcache) > 2000:
            self._qcache.clear()
        self._qcache[q] = v
        return v

    async def search(self, query: str, *, category: str | None = None, k: int = 3, mode: str = "hybrid") -> list[KBHit]:
        if not self.loaded:
            await self.load()
        async with tracer.span("kb.search", kind="retriever", input={"query": query, "category": category, "mode": mode}) as sp:
            allowed = CATEGORY_GROUPS.get(category) if category else None
            min_score = get_settings().kb_min_score if self.emb is not None else get_settings().kb_min_score_bm25
            hits = await self._search(query, allowed, k, mode)   # A-KB-02: category is a SOFT preference (bonus), never a filter
            hits = [h for h in hits if h.score >= min_score]  # A-KB-03
            sp.update(output=[{"id": h.chunk_id, "score": round(h.score, 3)} for h in hits])
            return hits

    async def _search(self, query: str, allowed: set[str] | None, k: int, mode: str) -> list[KBHit]:
        n = len(self.chunks)
        idx = list(range(n))
        if not idx:
            return []
        bm = np.array(self.bm25.get_scores(tokenize(query)))  # type: ignore[union-attr]
        qv = await self._qvec(query) if mode in ("hybrid", "dense", "rrf") else None
        dense = (self.emb @ qv) if (qv is not None and self.emb is not None) else None
        if mode in ("dense", "rrf") and dense is None:
            mode = "bm25"
        bm_max = max(float(bm.max()), 1e-9)
        if mode == "bm25" or dense is None:
            fused = bm.copy()
        elif mode == "dense":
            fused = dense.copy()
        elif mode == "rrf":  # reciprocal-rank fusion (kept for the comparison in reports/01)
            fused = np.zeros(n)
            for r, i in enumerate(sorted(idx, key=lambda i: -bm[i])):
                fused[i] += 1.0 / (60 + r)
            for r, i in enumerate(sorted(idx, key=lambda i: -dense[i])):
                fused[i] += 1.0 / (60 + r)
        else:  # "hybrid": dense-led weighted fusion; BM25 is a tie-breaker that rescues exact tokens (error codes)
            fused = dense + get_settings().kb_bm25_weight * (bm / bm_max if bm_max > 1.0 else 0.0)
        if allowed:  # preferred categories get a small bonus so ties go to the right topic, but a clearly better article elsewhere still wins
            bonus = np.array([get_settings().kb_category_bonus if c.category in allowed else 0.0 for c in self.chunks])
            fused = fused + bonus
        order = sorted(idx, key=lambda i: -fused[i])[:k]
        out = []
        for i in order:
            c = self.chunks[i]
            bm_norm = float(bm[i]) / bm_max if bm_max > 0.5 else 0.0
            rel = float(dense[i]) if dense is not None else min(1.0, float(bm[i]) / 8.0)
            out.append(KBHit(c.id, c.doc_id, c.title, c.category, c.text, rel, bm25=float(bm[i]),
                             dense=float(dense[i]) if dense is not None else None))
        return out


_kb: KnowledgeBase | None = None


async def get_kb() -> KnowledgeBase:
    global _kb
    if _kb is None:
        _kb = KnowledgeBase()
        await _kb.load()
    return _kb


def reset_kb():
    global _kb
    _kb = None
