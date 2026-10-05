"""KB retrieval evaluation -> reports/01_kb_retrieval.md
Compares bm25 / dense / hybrid on (a) handwritten queries and (b) PolyAI Banking77 (GitHub) customer queries whose
intent is mapped to the KB page that answers it. Also calibrates the 'no relevant article' threshold with
out-of-scope queries."""
import asyncio, random, re, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.core.config import get_settings, ROOT
from app.tools.kb import KnowledgeBase

HAND = [  # (query, acceptable doc ids)
 ("I was charged twice at the same shop", {"duplicate-charge", "pending-payments"}),
 ("what is a pending authorisation hold", {"pending-payments"}),
 ("how do I dispute a card payment", {"dispute-a-transaction"}),
 ("when do I get my money back while you investigate", {"provisional-credit", "dispute-a-transaction"}),
 ("I don't recognise a payment on my card", {"report-fraud", "mcc-and-merchant-names"}),
 ("my card was stolen am I liable", {"lost-or-stolen-card"}),
 ("how long to get a replacement card", {"replacement-card", "lost-or-stolen-card"}),
 ("can you waive my overdraft fee", {"fees-and-waivers", "overdraft"}),
 ("how long does an ACH transfer take", {"transfer-times"}),
 ("what is the wire cut off time", {"wire-transfers", "transfer-times"}),
 ("can I cancel a transfer I just sent", {"cancel-a-transfer", "wire-transfers"}),
 ("what does return code R03 mean", {"returned-transfers"}),
 ("daily limit for my debit card", {"limits"}),
 ("why was my card payment declined", {"declined-payments"}),
 ("how do I activate my new card", {"card-activation"}),
 ("reset my forgotten PIN", {"change-pin"}),
 ("contactless payment not working", {"contactless"}),
 ("do I pay a fee when I spend abroad", {"international-payments"}),
 ("out of network ATM charges", {"atm-withdrawals", "fees-and-waivers"}),
 ("my card got eaten by the cash machine", {"card-swallowed", "atm-withdrawals"}),
 ("I can't find a refund from a shop", {"refund-from-merchant"}),
 ("where do I see my statements", {"statements"}),
 ("what is this recurring payment on my account", {"direct-debits", "mcc-and-merchant-names"}),
 ("how do I open an account", {"open-account"}),
 ("close my account please", {"close-account"}),
 ("how do I change my address", {"edit-personal-details"}),
 ("why do you need to verify my identity", {"identity-checks", "open-account"}),
 ("make a formal complaint", {"complaints", "contact-support"}),
 ("someone died how do I tell the bank", {"bereavement"}),
 ("how do I keep my account safe from scams", {"security-tips", "report-fraud"}),
 ("savings account interest", {"savings-accounts", "interest-and-rates"}),
 ("is the app down", {"service-status", "mobile-app"}),
 ("joint account rules", {"joint-accounts"}),
 ("credit card basics", {"credit-card", "interest-and-rates"}),
]
OOS = ["what is the weather in paris tomorrow", "give me a recipe for lasagna", "who won the world cup in 2018",
       "write a poem about the ocean", "how do I fix my car engine", "what is the capital of australia",
       "tell me a joke about cats", "best programming language for game development", "translate hello to japanese",
       "how many calories in a banana"]
INTENT_DOCS = {  # PolyAI Banking77 intent -> KB docs that answer it (only intents Orbit Bank has an article for)
 "transaction_charged_twice": {"duplicate-charge", "pending-payments"}, "pending_card_payment": {"pending-payments"}, "card_payment_not_recognised": {"report-fraud", "mcc-and-merchant-names"},
 "lost_or_stolen_card": {"lost-or-stolen-card"}, "compromised_card": {"lost-or-stolen-card", "report-fraud"}, "getting_spare_card": {"replacement-card"}, "card_swallowed": {"card-swallowed", "atm-withdrawals"},
 "activate_my_card": {"card-activation"}, "card_arrival": {"card-activation", "replacement-card"}, "card_delivery_estimate": {"card-activation", "replacement-card"},
 "change_pin": {"change-pin"}, "pin_blocked": {"change-pin"}, "passcode_forgotten": {"change-pin", "mobile-app"}, "contactless_not_working": {"contactless"}, "declined_card_payment": {"declined-payments"},
 "declined_cash_withdrawal": {"declined-payments", "atm-withdrawals"}, "atm_support": {"atm-withdrawals"}, "cancel_transfer": {"cancel-a-transfer"}, "transfer_timing": {"transfer-times"},
 "pending_transfer": {"transfer-times", "pending-payments"}, "transfer_not_received_by_recipient": {"transfer-times", "returned-transfers"}, "failed_transfer": {"returned-transfers"},
 "declined_transfer": {"returned-transfers", "limits"}, "Refund_not_showing_up": {"refund-from-merchant"}, "extra_charge_on_statement": {"fees-and-waivers", "overdraft", "mcc-and-merchant-names"},
 "card_payment_fee_charged": {"fees-and-waivers", "international-payments"}, "transfer_fee_charged": {"fees-and-waivers", "wire-transfers"}, "cash_withdrawal_charge": {"atm-withdrawals", "fees-and-waivers"},
 "direct_debit_payment_not_recognised": {"direct-debits", "mcc-and-merchant-names"}, "terminate_account": {"close-account"}, "edit_personal_details": {"edit-personal-details"},
 "verify_my_identity": {"open-account", "identity-checks"}, "why_verify_identity": {"identity-checks"}, "unable_to_verify_identity": {"identity-checks", "open-account"}, "receiving_money": {"receiving-money"},
 "card_payment_wrong_exchange_rate": {"international-payments"}, "exchange_charge": {"international-payments"}, "balance_not_updated_after_bank_transfer": {"transfer-times", "pending-payments", "receiving-money"},
 "visa_or_mastercard": {"credit-card"}, "card_about_to_expire": {"replacement-card"},
}


def banking77_queries(n_per=4, seed=7):
    import csv
    by = {}
    for r in csv.DictReader(open(ROOT / "data/raw/banking77/test.csv")):
        by.setdefault(r["category"], []).append(r["text"])
    rnd = random.Random(seed); out = []
    for intent, docs in INTENT_DOCS.items():
        for q in rnd.sample(by[intent], min(n_per, len(by[intent]))): out.append((q, docs))
    return out

def metrics(res):  # res: list of (rank_of_first_correct or None)
    n = len(res)
    r1 = sum(1 for r in res if r == 1) / n; r3 = sum(1 for r in res if r and r <= 3) / n
    mrr = sum(1 / r for r in res if r) / n
    return r1, r3, mrr

async def main():
    s = get_settings(); s.kb_min_score = 0.0  # evaluate ranking independent of threshold
    kb = KnowledgeBase(use_dense=True); await kb.load()
    print("dense index:", kb.emb is not None, len(kb.chunks), "chunks")
    sets = {"handwritten": HAND, "banking77(github)": banking77_queries()}
    lines = ["# 01 — Knowledge-base retrieval evaluation", f"_{len(kb.chunks)} chunks from {len(set(c.doc_id for c in kb.chunks))} pages; embeddings: `{s.embed_model}`_", "",
             "| query set | n | mode | recall@1 | recall@3 | MRR | avg ms |", "|---|---|---|---|---|---|---|"]
    best = {}
    fails = []
    for name, qs in sets.items():
        for mode in ("bm25", "dense", "rrf", "hybrid"):
            res, t0 = [], time.perf_counter()
            for q, docs in qs:
                hits = await kb.search(q, k=5, mode=mode)
                rank = next((i + 1 for i, h in enumerate(hits) if h.doc_id in docs), None)
                res.append(rank)
                if mode == "hybrid" and (rank is None or rank > 3): fails.append((name, q, sorted(docs), [h.doc_id for h in hits[:3]]))
            r1, r3, mrr = metrics(res); ms = (time.perf_counter() - t0) / len(qs) * 1000
            lines.append(f"| {name} | {len(qs)} | {mode} | {r1:.2f} | {r3:.2f} | {mrr:.2f} | {ms:.0f} |")
            print(name, mode, f"r1={r1:.2f} r3={r3:.2f} mrr={mrr:.2f} {ms:.0f}ms")
    lines += ["", "## BM25 weight sweep for dense-led fusion (`hybrid` = dense + w * bm25_norm)", "", "| w | handwritten r@1 | handwritten MRR | banking77 r@1 | banking77 MRR |", "|---|---|---|---|---|"]
    default_w = s.kb_bm25_weight
    for w in (0.0, 0.05, 0.1, 0.2, 0.3):
        s.kb_bm25_weight = w; row = []
        for name, qs in sets.items():
            res = []
            for q, docs in qs:
                hits = await kb.search(q, k=5, mode="hybrid"); res.append(next((i + 1 for i, h in enumerate(hits) if h.doc_id in docs), None))
            r1, _, mrr = metrics(res); row += [f"{r1:.2f}", f"{mrr:.2f}"]
        lines.append(f"| {w} | " + " | ".join(row) + " |")
    s.kb_bm25_weight = default_w
    # threshold calibration: dense top-1 score on in-scope vs out-of-scope
    ins, oos = [], []
    for q, _ in HAND + banking77_queries(): 
        h = await kb.search(q, k=1, mode="hybrid"); ins.append(h[0].score if h else 0)
    for q in OOS:
        h = await kb.search(q, k=1, mode="hybrid"); oos.append(h[0].score if h else 0)
    import numpy as np
    lines += ["", "## 'No relevant article' threshold calibration (top-1 relevance score)", "",
              f"- in-scope queries (n={len(ins)}): min {min(ins):.2f}, p5 {np.percentile(ins,5):.2f}, median {np.median(ins):.2f}",
              f"- out-of-scope queries (n={len(oos)}): median {np.median(oos):.2f}, p95 {np.percentile(oos,95):.2f}, max {max(oos):.2f}", ""]
    for th in (0.2, 0.25, 0.3, 0.35, 0.4, 0.45, 0.5):
        tp = sum(1 for x in ins if x >= th) / len(ins); tn = sum(1 for x in oos if x < th) / len(oos)
        lines.append(f"- threshold {th:.2f}: answers {tp:.0%} of in-scope, abstains on {tn:.0%} of out-of-scope")
    lines += ["", "## Hybrid misses (not in top-3) for manual review", ""]
    for name, q, exp, got in fails: lines.append(f"- [{name}] “{q}” expected {exp}, got {got}")
    Path(s.report_dir).mkdir(exist_ok=True); Path(s.report_dir, "01_kb_retrieval.md").write_text("\n".join(lines))
if __name__ == "__main__":
    asyncio.run(main())
