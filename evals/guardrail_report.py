"""Guardrail measurements on attack / benign corpora -> reports/02_guardrails.md
python evals/guardrail_report.py            # static detectors only (no API calls)
python evals/guardrail_report.py --llm      # also measure the gray-zone LLM classifier (uses the live API)"""
import argparse, asyncio, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from evals import security_corpus as C
from app.core.config import get_settings
from app.guardrails import sqli
from app.guardrails.input import BLOCK_THRESHOLD, GRAY_THRESHOLD, injection_score, sanitize, llm_injection_check

def pct(n, d): return f"{100 * n / d:.1f}%" if d else "n/a"

async def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--llm", action="store_true"); a = ap.parse_args()
    s = get_settings(); s.llm_cache = False
    L = ["# 02 — Guardrail measurements", "_Static detectors are pure Python (<1 ms). Datasets: handwritten support-themed sets + Kaggle corpora "
         "(`syedsaqlainhussain/sql-injection-dataset`, `krishnayadav456wrsty/prompt-injection-and-jailbreak-detection-dataset`, `marycamilainfo/prompt-injection-malignant`, Bitext support phrasing)._", ""]
    # ---- SQLi
    kag, _ = C.sqli_kaggle(600, seed=99)  # seed differs from the one used while tuning patterns
    bit = C.bitext_messages(500, seed=5)
    L += ["## SQL-injection detector (`app/guardrails/sqli.py`)", "", "| set | n | detected (message mode) | detected (tool-arg free-text mode) |", "|---|---|---|---|"]
    for name, xs in [("handwritten attacks", C.SQLI_ATTACKS), ("Kaggle SQLiV3 attacks (held-out sample)", kag)]:
        L.append(f"| {name} | {len(xs)} | {pct(sum(bool(sqli.check_message(x)) for x in xs), len(xs))} | {pct(sum(bool(sqli.check_freetext(x)) for x in xs), len(xs))} |")
    L += ["", "| benign set | n | false positives (message) | false positives (free-text) |", "|---|---|---|---|"]
    for name, xs in [("handwritten benign support text containing SQL-ish words", C.SQLI_BENIGN), ("Bitext real support messages", bit)]:
        L.append(f"| {name} | {len(xs)} | {pct(sum(bool(sqli.check_message(x)) for x in xs), len(xs))} | {pct(sum(bool(sqli.check_freetext(x)) for x in xs), len(xs))} |")
    L += ["", "Strict mode (identifier args such as `INV-00000001`): 100 % of the meta-character corpus rejected, 0 legitimate IDs rejected (see `tests/test_guardrails_input.py`). "
          "Layer 1 (parameterised queries) makes injection structurally impossible regardless of these detection rates.", ""]
    # ---- injection
    sets = {"handwritten": C.INJECTION_ATTACKS, "Kaggle prompt-injection (test split, en/de/es/fr)": C.injection_kaggle("test", 150)[0],
            "Kaggle prompt-injection (train split)": C.injection_kaggle("train", 200)[0], "Kaggle 'malignant' jailbreak/act-as prompts": C.injection_malignant(150)[0]}
    ben = {"handwritten benign look-alikes": C.INJECTION_BENIGN, "Bitext real support messages": C.bitext_messages(600),
           "Kaggle benign (test split, general instructions)": C.injection_kaggle("test", 150)[1], "Kaggle conversation prompts": C.injection_malignant(150)[1]}
    sc = lambda x: injection_score(sanitize(x, 2000)[0])[0]
    L += ["## Prompt-injection detector (`app/guardrails/input.py`)", "", f"Noisy-OR score over weighted patterns. ≥{BLOCK_THRESHOLD} blocks outright; {GRAY_THRESHOLD}–{BLOCK_THRESHOLD} is the gray zone (second opinion from an LLM classifier); below = allow.", "",
          "| attack set | n | blocked by regex | regex block + gray zone |" + (" after LLM gray-zone check |" if a.llm else ""), "|---|---|---|---|" + ("---|" if a.llm else "")]
    gray_cache = {}
    async def final_blocked(x):
        sc_ = sc(x)
        if sc_ >= BLOCK_THRESHOLD: return True
        if sc_ >= GRAY_THRESHOLD and a.llm:
            if x not in gray_cache:
                inj, conf = await llm_injection_check(x); gray_cache[x] = inj and conf >= 0.6
            return gray_cache[x]
        return False
    for name, xs in sets.items():
        row = f"| {name} | {len(xs)} | {pct(sum(sc(x) >= BLOCK_THRESHOLD for x in xs), len(xs))} | {pct(sum(sc(x) >= GRAY_THRESHOLD for x in xs), len(xs))} |"
        if a.llm:
            res = await asyncio.gather(*[final_blocked(x) for x in xs]); row += f" {pct(sum(res), len(xs))} |"
        L.append(row)
    L += ["", "| benign set | n | false positives (regex block) | in gray zone | false positives after LLM check |" if a.llm else "| benign set | n | false positives (regex block) | in gray zone |", "|---|---|---|---|" + ("---|" if a.llm else "")]
    for name, xs in ben.items():
        row = f"| {name} | {len(xs)} | {pct(sum(sc(x) >= BLOCK_THRESHOLD for x in xs), len(xs))} | {pct(sum(GRAY_THRESHOLD <= sc(x) < BLOCK_THRESHOLD for x in xs), len(xs))} |"
        if a.llm:
            res = await asyncio.gather(*[final_blocked(x) for x in xs]); row += f" {pct(sum(res), len(xs))} |"
        L.append(row)
    L += ["", "**Reading these numbers honestly:** the Kaggle jailbreak corpora are dominated by long role-play prompts (\"you are FreeGPT…\") that have nothing to do with "
          "support; the regex layer catches the ones that look like instruction-override / prompt-extraction / data-exfiltration. Everything it misses is handled by the "
          "remaining layers: the dispatcher's `off_topic` intent (topic control), tool allow-lists + identity injection, intent-gated writes, and the output validator. "
          "End-to-end outcomes on adversarial inputs are in `04_eval_full.md` (adversarial categories)."]
    Path(s.report_dir, "02_guardrails.md").write_text("\n".join(L)); print("\n".join(L))
asyncio.run(main())
