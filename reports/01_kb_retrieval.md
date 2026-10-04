# 01 — Knowledge-base retrieval evaluation
_43 chunks from 43 pages; embeddings: `nvidia/nemotron-3-embed-1b`_

| query set | n | mode | recall@1 | recall@3 | MRR | avg ms |
|---|---|---|---|---|---|---|
| handwritten | 47 | bm25 | 0.87 | 0.98 | 0.92 | 0 |
| handwritten | 47 | dense | 1.00 | 1.00 | 1.00 | 991 |
| handwritten | 47 | rrf | 0.91 | 0.98 | 0.95 | 0 |
| handwritten | 47 | hybrid | 1.00 | 1.00 | 1.00 | 0 |
| bitext(kaggle) | 76 | bm25 | 0.43 | 0.61 | 0.54 | 0 |
| bitext(kaggle) | 76 | dense | 0.87 | 0.97 | 0.92 | 1253 |
| bitext(kaggle) | 76 | rrf | 0.67 | 0.84 | 0.76 | 0 |
| bitext(kaggle) | 76 | hybrid | 0.88 | 0.96 | 0.92 | 0 |

## BM25 weight sweep for dense-led fusion (`hybrid` = dense + w * bm25_norm)

| w | handwritten r@1 | handwritten MRR | bitext r@1 | bitext MRR |
|---|---|---|---|---|
| 0.0 | 1.00 | 1.00 | 0.87 | 0.92 |
| 0.05 | 1.00 | 1.00 | 0.88 | 0.92 |
| 0.1 | 1.00 | 1.00 | 0.83 | 0.90 |
| 0.2 | 1.00 | 1.00 | 0.71 | 0.82 |
| 0.3 | 1.00 | 1.00 | 0.66 | 0.78 |

## 'No relevant article' threshold calibration (top-1 relevance score)

- in-scope queries (n=123): min 0.16, p5 0.19, median 0.42
- out-of-scope queries (n=10): median 0.07, p95 0.14, max 0.15

- threshold 0.20: answers 94% of in-scope, abstains on 100% of out-of-scope
- threshold 0.25: answers 86% of in-scope, abstains on 100% of out-of-scope
- threshold 0.30: answers 76% of in-scope, abstains on 100% of out-of-scope
- threshold 0.35: answers 63% of in-scope, abstains on 100% of out-of-scope
- threshold 0.40: answers 54% of in-scope, abstains on 100% of out-of-scope
- threshold 0.45: answers 40% of in-scope, abstains on 100% of out-of-scope
- threshold 0.50: answers 29% of in-scope, abstains on 100% of out-of-scope

## Hybrid misses (not in top-3) for manual review

- [bitext(kaggle)] “I want to oldge a claim against your business” expected ['general-escalation-policy', 'general-feedback', 'policy-support-conduct'], got ['billing-disputes-chargebacks', 'tech-sso-saml', 'billing-payment-methods']
- [bitext(kaggle)] “i need assistance to create a platinum acfount for my fiance” expected ['general-account-management', 'general-team-seats-roles'], got ['billing-promo-credits', 'tech-account-locked', 'tech-api-authentication']
- [bitext(kaggle)] “opening premium account” expected ['general-account-management', 'general-team-seats-roles'], got ['general-escalation-policy', 'billing-promo-credits', 'general-contact-support']