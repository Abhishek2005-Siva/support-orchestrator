"""Map PolyAI Banking77 labels onto this system's intents. Banking77 comes from a neobank (top-ups, currency exchange, virtual cards), so some of its 77
intents do not exist at Orbit Bank; the mapping is deliberately conservative, ambiguous intents carry alternative acceptable answers, and the label noise is
the main reason routing agreement on this public data is lower than on the handwritten cases. Intents with no sensible Orbit Bank equivalent are excluded."""
import csv
import random
from pathlib import Path

RAW = Path(__file__).resolve().parents[1] / "data" / "raw" / "banking77"
P, C, G, E = "payments", "cards", "general", "escalation"
# intent -> (expected, alternatives)
BANKING77 = {
    # cards
    "activate_my_card": ([C], []), "card_about_to_expire": ([C], [[G]]), "card_acceptance": ([C], [[G]]), "card_arrival": ([C], []), "card_delivery_estimate": ([C], []),
    "card_not_working": ([C], []), "card_payment_not_recognised": ([C], [[P], [C, P]]), "card_swallowed": ([C], []), "change_pin": ([C], []), "compromised_card": ([C], [[E]]),
    "contactless_not_working": ([C], []), "declined_card_payment": ([C], [[P]]), "declined_cash_withdrawal": ([C], [[P]]), "getting_spare_card": ([C], []), "lost_or_stolen_card": ([C], []),
    "order_physical_card": ([C], []), "pin_blocked": ([C], []), "cash_withdrawal_not_recognised": ([C], [[P], [C, P]]), "atm_support": ([C], [[G]]), "wrong_amount_of_cash_received": ([C], [[P], [C, P]]),
    "pending_cash_withdrawal": ([P], [[C], [C, P]]), "visa_or_mastercard": ([C], [[G]]), "lost_or_stolen_phone": ([C], [[G], [E]]),
    # payments
    "transaction_charged_twice": ([P], [[C, P]]), "pending_card_payment": ([P], [[C]]), "pending_transfer": ([P], []), "cancel_transfer": ([P], []), "failed_transfer": ([P], []),
    "declined_transfer": ([P], []), "transfer_not_received_by_recipient": ([P], []), "transfer_timing": ([P], [[G]]), "transfer_fee_charged": ([P], []), "card_payment_fee_charged": ([P], [[C]]),
    "extra_charge_on_statement": ([P], []), "Refund_not_showing_up": ([P], []), "request_refund": ([P], []), "direct_debit_payment_not_recognised": ([P], [[C]]),
    "balance_not_updated_after_bank_transfer": ([P], []), "balance_not_updated_after_cheque_or_cash_deposit": ([P], []), "receiving_money": ([P], [[G]]), "transfer_into_account": ([P], [[G]]),
    "beneficiary_not_allowed": ([P], [[G]]), "reverted_card_payment?": ([P], [[C]]),
    # general
    "age_limit": ([G], []), "country_support": ([G], []), "edit_personal_details": ([G], []), "terminate_account": ([G], [[E]]), "unable_to_verify_identity": ([G], []),
    "verify_my_identity": ([G], []), "why_verify_identity": ([G], []), "verify_source_of_funds": ([G], [[E]]), "passcode_forgotten": ([G], [[C]]), "apple_pay_or_google_pay": ([G], [[C]]),
    "supported_cards_and_currencies": ([G], [[C]]),
}


def banking77_cases(n_per=4, seed=21, split="test"):
    by: dict[str, list[str]] = {}
    for r in csv.DictReader(open(RAW / f"{split}.csv")):
        by.setdefault(r["category"], []).append(r["text"])
    rnd, out = random.Random(seed), []
    for intent, (exp, alt) in BANKING77.items():
        for q in rnd.sample(by[intent], min(n_per, len(by[intent]))):
            out.append({"message": q, "expected": sorted(exp), "alt": [sorted(a) for a in alt], "tags": ["banking77", intent]})
    return out
