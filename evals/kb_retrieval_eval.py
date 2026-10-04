"""KB retrieval evaluation -> reports/01_kb_retrieval.md
Compares bm25 / dense / hybrid on (a) handwritten queries and (b) Bitext (Kaggle) customer queries whose
intent is mapped to the KB page that answers it. Also calibrates the 'no relevant article' threshold with
out-of-scope queries."""
import asyncio, random, re, sys, time
from pathlib import Path
import pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.core.config import get_settings, ROOT
from app.tools.kb import KnowledgeBase

HAND = [  # (query, acceptable doc ids)
 ("my API returns 401 unauthorized", {"tech-api-authentication", "tech-error-codes"}),
 ("getting 429 too many requests from the api", {"tech-api-rate-limits"}),
 ("what is the rate limit on the Pro plan", {"tech-api-rate-limits", "billing-plans-and-pricing"}),
 ("RATE_LIMIT_429 in my logs", {"tech-api-rate-limits"}),
 ("webhooks are not arriving", {"tech-webhooks", "tech-service-status"}),
 ("webhook timeout after 10 seconds", {"tech-webhooks"}),
 ("how do I verify the webhook signature", {"tech-webhooks"}),
 ("SAML login fails assertion expired", {"tech-sso-saml"}),
 ("SSO clock skew error", {"tech-sso-saml"}),
 ("how to export my data to csv", {"tech-data-export"}),
 ("export is bigger than 2GB", {"tech-data-export"}),
 ("dashboard is blank and won't load", {"tech-dashboard-loading", "tech-service-status"}),
 ("pip install orbit-sdk fails", {"tech-sdk-installation"}),
 ("which python version does the sdk need", {"tech-sdk-installation"}),
 ("connect slack integration", {"tech-integrations"}),
 ("I lost my phone with the authenticator app", {"tech-password-2fa"}),
 ("reset my password link expired", {"tech-password-2fa"}),
 ("is orbit down right now", {"tech-service-status"}),
 ("how long do you keep my event data", {"tech-data-retention"}),
 ("queries time out after 30 seconds", {"tech-performance"}),
 ("mobile app keeps crashing on android", {"tech-mobile-app"}),
 ("account locked after wrong password attempts", {"tech-account-locked"}),
 ("which ip addresses do webhooks come from", {"tech-ip-allowlist"}),
 ("I was charged twice this month", {"billing-duplicate-charges"}),
 ("duplicate charge on my card", {"billing-duplicate-charges"}),
 ("can I get a refund", {"billing-refund-policy"}),
 ("refund window for annual plans", {"billing-refund-policy"}),
 ("how long does a refund take to reach my bank", {"billing-refund-timeline"}),
 ("my payment failed", {"billing-failed-payments", "billing-failed-payment-card-expiry"}),
 ("card expired how to update", {"billing-failed-payment-card-expiry", "billing-payment-methods"}),
 ("how many times do you retry failed payments", {"billing-failed-payments"}),
 ("how much is the business plan", {"billing-plans-and-pricing"}),
 ("downgrade my plan", {"billing-plan-changes"}),
 ("do you charge a fee to cancel", {"billing-cancellation"}),
 ("add my VAT number to invoices", {"billing-taxes-vat"}),
 ("how do promo codes work", {"billing-promo-credits"}),
 ("I want to dispute a charge with my bank", {"billing-disputes-chargebacks"}),
 ("download invoice pdf", {"billing-invoices-and-receipts"}),
 ("what payment methods do you accept", {"billing-payment-methods"}),
 ("why is my account suspended", {"general-suspension", "billing-failed-payments"}),
 ("how fast does support reply to vip customers", {"general-contact-support"}),
 ("when do you hand over to a human", {"general-escalation-policy"}),
 ("do you ask for passwords in chat", {"general-security-privacy"}),
 ("uptime guarantee and sla credits", {"policy-sla-credits"}),
 ("how do I invite a teammate", {"general-team-seats-roles", "general-account-management"}),
 ("delete my workspace permanently", {"general-delete-account"}),
 ("unsubscribe from marketing emails", {"general-communication-preferences"}),
]
OOS = ["what is the weather in paris tomorrow", "give me a recipe for lasagna", "who won the world cup in 2018",
       "write a poem about the ocean", "how do I fix my car engine", "what is the capital of australia",
       "tell me a joke about cats", "best programming language for game development", "translate hello to japanese",
       "how many calories in a banana"]
INTENT_DOCS = {  # Bitext intent -> KB docs that answer it
 "check_invoice": {"billing-invoices-and-receipts"}, "get_invoice": {"billing-invoices-and-receipts"},
 "check_payment_methods": {"billing-payment-methods"}, "payment_issue": {"billing-failed-payments", "billing-payment-methods", "billing-failed-payment-card-expiry"},
 "check_refund_policy": {"billing-refund-policy"}, "get_refund": {"billing-refund-policy", "billing-refund-timeline", "billing-duplicate-charges"},
 "track_refund": {"billing-refund-timeline"}, "check_cancellation_fee": {"billing-cancellation"},
 "delete_account": {"general-delete-account"}, "recover_password": {"tech-password-2fa", "tech-account-locked"},
 "newsletter_subscription": {"general-communication-preferences"}, "contact_human_agent": {"general-contact-support", "general-escalation-policy"},
 "contact_customer_service": {"general-contact-support"}, "edit_account": {"general-account-management"},
 "switch_account": {"general-account-management", "billing-plan-changes"}, "complaint": {"general-feedback", "general-escalation-policy", "policy-support-conduct"},
 "review": {"general-feedback"}, "registration_problems": {"general-account-management", "tech-account-locked"},
 "create_account": {"general-account-management", "general-team-seats-roles"},
}
FILL = {"Order Number": "12345", "Invoice Number": "INV-00001234", "Account Category": "billing", "Money Amount": "50", "Refund Amount": "50",
        "Person Name": "Alex", "Online Order Interaction": "order page", "Date": "May 5", "Account Type": "premium", "Payment Method": "credit card",
        "Settings": "settings", "Online Company Portal Info": "portal", "Salutation": "Hi", "Website URL": "orbit.example", "Customer Support Hours": "hours",
        "Customer Support Phone Number": "the hotline", "Customer Support Email": "support@orbit.example", "Live Chat Support": "live chat",
        "Profile": "profile", "Name": "Alex", "Email": "me@example.com", "Time": "5pm", "Company Name": "Orbit"}
def clean(t): return re.sub(r"\{\{(.+?)\}\}", lambda m: FILL.get(m.group(1), m.group(1).lower()), t)

def bitext_queries(n_per=4, seed=7):
    df = pd.read_csv(ROOT / "data/raw/bitext-gen-ai-chatbot-customer-support-dataset/Bitext_Sample_Customer_Support_Training_Dataset_27K_responses-v11.csv")
    rnd = random.Random(seed); out = []
    for intent, docs in INTENT_DOCS.items():
        rows = df[df.intent == intent].instruction.tolist()
        for q in rnd.sample(rows, n_per): out.append((clean(q), docs))
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
    sets = {"handwritten": HAND, "bitext(kaggle)": bitext_queries()}
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
    lines += ["", "## BM25 weight sweep for dense-led fusion (`hybrid` = dense + w * bm25_norm)", "", "| w | handwritten r@1 | handwritten MRR | bitext r@1 | bitext MRR |", "|---|---|---|---|---|"]
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
    for q, _ in HAND + bitext_queries(): 
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
