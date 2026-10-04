import json, shutil
from pathlib import Path
import pytest
from app.core.config import ROOT, get_settings

MANIFEST = json.loads((ROOT / "data" / "seed_manifest.json").read_text()) if (ROOT / "data" / "seed_manifest.json").exists() else {"customers": {}}


def customers_with(tag: str) -> list[str]:
    return [cid for cid, v in MANIFEST["customers"].items() if tag in v["tags"]]


@pytest.fixture
async def db(tmp_path, monkeypatch):
    """Isolated copy of the seeded SQLite DB (so write tools never touch data/support.db)."""
    from app.db import session as sess
    from app.tools import runtime
    dst = tmp_path / "t.db"
    import sqlite3
    src_c, dst_c = sqlite3.connect(ROOT / "data" / "support.db"), sqlite3.connect(dst)
    src_c.backup(dst_c)  # WAL-safe copy (a plain file copy can miss un-checkpointed pages)
    src_c.close(); dst_c.close()
    monkeypatch.setattr(get_settings(), "log_dir", tmp_path / "logs")
    await sess.reset_engine(f"sqlite+aiosqlite:///{dst}")
    runtime.reset_business_today()
    yield dst
    await sess.reset_engine(f"sqlite+aiosqlite:///{ROOT / 'data' / 'support.db'}")
    runtime.reset_business_today()


@pytest.fixture
def kb_bm25(monkeypatch):
    """KB without network: BM25 only."""
    from app.tools import kb as kbmod
    k = kbmod.KnowledgeBase(use_dense=False)
    kbmod._kb = k
    yield k
    kbmod._kb = None


@pytest.fixture(autouse=True)
async def _isolate_singletons(tmp_path, monkeypatch):
    """Each test runs on its own event loop: drop the process-wide gateway / KB so no HTTP pool outlives its loop,
    and keep test traces out of the real logs/ directory."""
    from app.llm import gateway as gw
    from app.tools import kb as kbmod
    monkeypatch.setattr(get_settings(), "log_dir", tmp_path / "logs")
    gw.set_gateway(None)
    kbmod.reset_kb()
    yield
    g = gw._gateway
    if g is not None and hasattr(g, "aclose"):
        try:
            await g.aclose()
        except Exception:
            pass
    gw.set_gateway(None)
    kbmod.reset_kb()
