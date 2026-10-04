"""Central configuration (pydantic-settings). Everything tunable lives here so
accuracy / latency / guardrail trade-offs are one env-var away."""
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class RoleModel(BaseModel):
    """Model + decoding settings for one LLM role."""
    model: str
    fallbacks: list[str] = []
    temperature: float = 0.0
    max_tokens: int = 700
    hedge_after_s: float | None = None  # per-role override of llm_hedge_after_s
    thinking: bool = False  # nemotron "reasoning" mode: slower (~3x) but can be more accurate


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    # --- LLM provider (NVIDIA NIM, OpenAI-compatible) ---
    nvidia_api_key: str = ""
    nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"
    llm_mode: str = "live"  # live | mock  (mock = deterministic fake LLM for load tests / unit tests)
    llm_max_concurrency: int = 4  # global semaphore around LLM calls (free tier is rate limited)
    llm_max_rpm: int = 70  # client-side token bucket; measured: free key 429s above ~100 rpm / conc>4
    llm_timeout_s: float = 20.0
    llm_hedge_after_s: float = 5.0  # 0 disables hedging
    llm_max_retries: int = 5   # ultra returns ~25 % transient 503 'overloaded'; those are cheap (0.3 s) to retry
    llm_cache: bool = True  # exact-match cache for temperature-0 calls
    answer_cache_enabled: bool = True  # validated KB-only answers (see graph/cache.py)
    answer_cache_ttl_s: int = 3600
    mock_latency_ms: int = 150  # simulated per-call latency in mock mode
    mock_fail_rate: float = 0.0  # chaos testing: fraction of mock LLM calls that raise

    # --- per-role models (override with env, e.g. DISPATCHER_MODEL=...) ---
    dispatcher_model: str = "nvidia/nemotron-3-ultra-550b-a55b"
    specialist_model: str = "nvidia/nemotron-3-ultra-550b-a55b"
    validator_model: str = "nvidia/nemotron-3-ultra-550b-a55b"
    merge_model: str = "nvidia/nemotron-3-ultra-550b-a55b"
    judge_model: str = "nvidia/nemotron-3-ultra-550b-a55b"
    fallback_models: str = "meta/llama-3.2-11b-vision-instruct,openai/gpt-oss-20b"   # proven-served models only (re-verified 2026-10-03 after nemotron-3-super reached end-of-life)
    validator_thinking: bool = False
    safety_model: str = "nvidia/nemotron-3.5-content-safety"
    embed_model: str = "nvidia/nemotron-3-embed-1b"
    enable_safety_model: bool = True
    safety_deadline_s: float = 2.5  # the safety model is best-effort: if it has not answered by now, continue without it
    merge_mode: str = "template"  # template (no LLM, fastest) | llm

    # --- guardrail / quality thresholds ---
    max_message_chars: int = 2000
    validator_confidence_threshold: float = 0.7
    dispatcher_confidence_threshold: float = 0.55
    max_revisions: int = 1  # each revision costs a full specialist+validator round (~6-10 s); bounded for latency, revisit with eval data
    max_react_iterations: int = 4
    refund_auto_limit_usd: float = 100.0  # refunds above this require human approval
    kb_min_score: float = 0.15  # below this, KB retrieval counts as "no answer found" (calibrated in reports/01)
    kb_bm25_weight: float = 0.05
    kb_category_bonus: float = 0.06  # soft preference for the agent's own topic; the dispatcher can mislabel, so it is never a hard filter
    kb_strong_score: float = 0.3  # prefetched KB hit at/above this => specialists get no re-search tool
    kb_min_score_bm25: float = 0.4  # stricter threshold when only BM25 is available (mock mode / embeddings down)

    # --- infra ---
    database_url: str = f"sqlite+aiosqlite:///{ROOT / 'data' / 'support.db'}"
    checkpoint_db: str = str(ROOT / "data" / "checkpoints.db")
    jwt_secret: str = "dev-secret"
    jwt_algorithm: str = "HS256"
    jwt_ttl_minutes: int = 30
    rate_limit_per_min: int = 100
    login_rate_limit_per_min_ip: int = 20   # brute-force protection on /auth/token (raised only for load tests)
    request_timeout_s: float = 60.0
    max_inflight_queries: int = 200
    demo_mode: bool = False        # exposes /demo/accounts (synthetic demo logins) for the public demo UI. NEVER enable with real data.
    cors_origins: str = ""         # comma-separated origins allowed to call the API from another host (e.g. a Vercel frontend)
    log_dir: Path = ROOT / "logs"
    report_dir: Path = ROOT / "reports"

    # --- observability ---
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"

    # --- slack ---
    slack_bot_token: str = ""
    slack_signing_secret: str = ""
    slack_review_channel: str = "#support-escalations"

    def role(self, name: str) -> RoleModel:
        fb = [m.strip() for m in self.fallback_models.split(",") if m.strip()]
        table = {
            "dispatcher": RoleModel(model=self.dispatcher_model, fallbacks=fb, max_tokens=300, hedge_after_s=2.5),
            "specialist": RoleModel(model=self.specialist_model, fallbacks=fb, max_tokens=900, temperature=0.1),
            # the quality gates NEVER fall back to a weaker model: a weak judge rejected 68 % of good drafts vs 8 % on the primary
            # (case study #16). If the strong model stays unreachable the validator fails closed to human review instead.
            "validator": RoleModel(model=self.validator_model, fallbacks=[], max_tokens=500, thinking=self.validator_thinking, hedge_after_s=3.0),
            "merge": RoleModel(model=self.merge_model, fallbacks=fb, max_tokens=700, temperature=0.1),
            "judge": RoleModel(model=self.judge_model, fallbacks=[], max_tokens=500),
            "safety": RoleModel(model=self.safety_model, max_tokens=60, hedge_after_s=1.5),
        }
        return table[name]


@lru_cache
def get_settings() -> Settings:
    return Settings()
