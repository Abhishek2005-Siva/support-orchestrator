"""Static (no-LLM) guardrail evaluation: SQLi + prompt-injection heuristics. Prints detail for misses."""
import sys; sys.path.insert(0, ".")
from evals import security_corpus as C
from app.guardrails import sqli
from app.guardrails.input import injection_score, GRAY_THRESHOLD, BLOCK_THRESHOLD, sanitize

def rate(xs, f): 
    hits = [x for x in xs if f(x)]; return len(hits) / max(1, len(xs)), [x for x in xs if not f(x)], hits

print("=== SQLi (message-level strong patterns) ===")
kag, _ = C.sqli_kaggle(600)
for name, xs in [("handwritten attacks", C.SQLI_ATTACKS), ("kaggle SQLiV3 attacks", kag)]:
    r, miss, _ = rate(xs, lambda x: bool(sqli.check_message(x)))
    print(f"{name}: detected {r:.1%} of {len(xs)}"); 
    for m in miss[:8]: print("   MISS:", m[:110])
print("=== SQLi (tool-arg freetext mode) ===")
for name, xs in [("handwritten attacks", C.SQLI_ATTACKS), ("kaggle", kag)]:
    r, miss, _ = rate(xs, lambda x: bool(sqli.check_freetext(x)))
    print(f"{name}: detected {r:.1%}")
for name, xs in [("handwritten benign", C.SQLI_BENIGN), ("bitext benign", C.bitext_messages(500))]:
    r, _, fp = rate(xs, lambda x: bool(sqli.check_message(x)))
    r2, _, fp2 = rate(xs, lambda x: bool(sqli.check_freetext(x)))
    print(f"{name}: false positives message-mode {r:.1%} ({len(fp)}), freetext-mode {r2:.1%} ({len(fp2)})")
    for m in (fp + fp2)[:6]: print("   FP:", m[:110])

print("\n=== Prompt injection (heuristic only; block>=%.2f, gray>=%.2f) ===" % (BLOCK_THRESHOLD, GRAY_THRESHOLD))
def blocked(x): return injection_score(sanitize(x, 2000)[0])[0] >= BLOCK_THRESHOLD
def grayplus(x): return injection_score(sanitize(x, 2000)[0])[0] >= GRAY_THRESHOLD
sets = {"handwritten": (C.INJECTION_ATTACKS, C.INJECTION_BENIGN),
        "kaggle-test(detect)": C.injection_kaggle("test", 150), "kaggle-train(detect)": C.injection_kaggle("train", 200),
        "malignant": C.injection_malignant(150)}
for name, (att, ben) in sets.items():
    a, miss, _ = rate(att, blocked); a2, _, _ = rate(att, grayplus); b, _, fp = rate(ben, blocked); b2, _, fp2 = rate(ben, grayplus)
    print(f"{name}: attacks n={len(att)} blocked {a:.1%} (block+gray {a2:.1%}) | benign n={len(ben)} FP blocked {b:.1%} (block+gray {b2:.1%})")
    if "-v" in sys.argv:
        for m in miss[:12]: print("   MISS:", m[:130].replace("\n"," "))
        for m in fp[:8]: print("   FP:", m[:130].replace("\n"," "))
bm = C.bitext_messages(600)
b, _, fp = rate(bm, blocked); b2, _, fp2 = rate(bm, grayplus)
print(f"bitext real support messages n={len(bm)}: FP blocked {b:.1%} ({len(fp)}), block+gray {b2:.1%} ({len(fp2)})")
for m in fp2[:8]: print("   GRAY/FP:", m[:130], injection_score(m))
