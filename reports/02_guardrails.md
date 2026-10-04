# 02 — Guardrail measurements
_Static detectors are pure Python (<1 ms). Datasets: handwritten support-themed sets + Kaggle corpora (`syedsaqlainhussain/sql-injection-dataset`, `krishnayadav456wrsty/prompt-injection-and-jailbreak-detection-dataset`, `marycamilainfo/prompt-injection-malignant`, Bitext support phrasing)._

## SQL-injection detector (`app/guardrails/sqli.py`)

| set | n | detected (message mode) | detected (tool-arg free-text mode) |
|---|---|---|---|
| handwritten attacks | 52 | 100.0% | 100.0% |
| Kaggle SQLiV3 attacks (held-out sample) | 600 | 95.2% | 95.2% |

| benign set | n | false positives (message) | false positives (free-text) |
|---|---|---|---|
| handwritten benign support text containing SQL-ish words | 41 | 0.0% | 0.0% |
| Bitext real support messages | 500 | 0.0% | 0.0% |

Strict mode (identifier args such as `INV-00000001`): 100 % of the meta-character corpus rejected, 0 legitimate IDs rejected (see `tests/test_guardrails_input.py`). Layer 1 (parameterised queries) makes injection structurally impossible regardless of these detection rates.

## Prompt-injection detector (`app/guardrails/input.py`)

Noisy-OR score over weighted patterns. ≥0.8 blocks outright; 0.35–0.8 is the gray zone (second opinion from an LLM classifier); below = allow.

| attack set | n | blocked by regex | regex block + gray zone | after LLM gray-zone check |
|---|---|---|---|---|
| handwritten | 42 | 88.1% | 97.6% | 97.6% |
| Kaggle prompt-injection (test split, en/de/es/fr) | 106 | 47.2% | 67.9% | 66.0% |
| Kaggle prompt-injection (train split) | 200 | 45.0% | 65.0% | 64.5% |
| Kaggle 'malignant' jailbreak/act-as prompts | 150 | 24.0% | 78.0% | 69.3% |

| benign set | n | false positives (regex block) | in gray zone | false positives after LLM check |
|---|---|---|---|---|
| handwritten benign look-alikes | 40 | 0.0% | 5.0% | 0.0% |
| Bitext real support messages | 600 | 0.0% | 0.0% | 0.0% |
| Kaggle benign (test split, general instructions) | 107 | 0.0% | 0.0% | 0.0% |
| Kaggle conversation prompts | 150 | 0.0% | 0.0% | 0.0% |

**Reading these numbers honestly:** the Kaggle jailbreak corpora are dominated by long role-play prompts ("you are FreeGPT…") that have nothing to do with support; the regex layer catches the ones that look like instruction-override / prompt-extraction / data-exfiltration. Everything it misses is handled by the remaining layers: the dispatcher's `off_topic` intent (topic control), tool allow-lists + identity injection, intent-gated writes, and the output validator. End-to-end outcomes on adversarial inputs are in `04_eval_full.md` (adversarial categories).