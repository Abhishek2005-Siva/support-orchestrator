"""Map Kaggle dataset labels onto this system's intents. Mappings are deliberately conservative and documented in the
reports: noisy labels are the main reason routing accuracy on Kaggle data is lower than on handwritten cases."""
import random, re
from pathlib import Path
import pandas as pd
RAW = Path(__file__).resolve().parents[1] / "data" / "raw"
FILL = {"Order Number": "12345", "Invoice Number": "INV-00001234", "Account Category": "billing", "Money Amount": "50", "Refund Amount": "50",
        "Person Name": "Alex", "Date": "May 5", "Account Type": "premium", "Payment Method": "credit card", "Settings": "settings",
        "Online Company Portal Info": "portal", "Salutation": "Hi", "Website URL": "orbit.example", "Customer Support Hours": "hours",
        "Customer Support Phone Number": "the hotline", "Customer Support Email": "support@orbit.example", "Live Chat Support": "live chat",
        "Profile": "profile", "Name": "Alex", "Email": "me@example.com", "Time": "5pm", "Company Name": "Orbit"}
def clean(t): return re.sub(r"\{\{(.+?)\}\}", lambda m: FILL.get(m.group(1), m.group(1).lower()), t)

# Bitext intent -> (expected, alternatives). DELIVERY/SHIPPING/ORDER categories are excluded: physical-goods concepts don't exist for Orbit.
BITEXT = {
 "check_invoice": (["billing"], []), "get_invoice": (["billing"], []), "payment_issue": (["billing"], [["billing", "technical"]]),
 "check_payment_methods": (["billing"], [["general"]]), "check_refund_policy": (["billing"], []), "get_refund": (["billing"], []),
 "track_refund": (["billing"], []), "check_cancellation_fee": (["billing"], []),
 "contact_customer_service": (["general"], []), "contact_human_agent": (["escalation"], []),
 "edit_account": (["general"], [["billing"]]), "switch_account": (["general"], [["billing"]]), "create_account": (["general"], [["technical"], ["billing"]]),
 "registration_problems": (["technical"], [["general"], ["general", "technical"]]), "delete_account": (["general"], [["billing", "general"], ["billing"]]),
 "newsletter_subscription": (["general"], []), "recover_password": (["technical"], [["general"]]),
 "review": (["general"], []), "complaint": (["general"], [["escalation"], ["escalation", "general"]]),
}
TICKET_QUEUE = {"Technical Support": ["technical"], "IT Support": ["technical"], "Service Outages and Maintenance": ["technical"],
                "Billing and Payments": ["billing"]}

def bitext_cases(n_per=4, seed=21):
    df = pd.read_csv(RAW / "bitext-gen-ai-chatbot-customer-support-dataset" / "Bitext_Sample_Customer_Support_Training_Dataset_27K_responses-v11.csv")
    rnd, out = random.Random(seed), []
    for intent, (exp, alt) in BITEXT.items():
        rows = df[df.intent == intent].instruction.tolist()
        for q in rnd.sample(rows, n_per):
            out.append({"message": clean(q), "expected": sorted(exp), "alt": [sorted(a) for a in alt], "tags": ["bitext", intent]})
    return out

# The ticket dataset covers many unrelated products (routers, brand strategy, medical systems). Orbit correctly treats those as
# off_topic, so only tickets whose text matches topics Orbit actually supports are used as routing ground truth.
RELEVANT = {"technical": r"\b(error|crash(es|ed|ing)?|bug|login|log in|password|slow|timeout|sync(hroni[sz]ation)?|outage|not working|api|integration|dashboard|connect(ion)?|install(ation)?|performance|freez\w+|broken)\b",
            "billing": r"\b(invoice|billing|charged?|payment|refund|subscription|price|pricing|credit card|receipt)\b"}
def ticket_cases(n_per_queue=12, seed=21, max_chars=420):
    df = pd.read_csv(RAW / "multilingual-customer-support-tickets" / "dataset-tickets-multi-lang-4-20k.csv")
    df = df[(df.language == "en") & df.queue.isin(TICKET_QUEUE)]
    df = df[~df.body.astype(str).str.contains(r"medical|patient|hospital|brand|marketing|router|wifi|investment|breach", case=False, regex=True)]
    df = df[[bool(re.search(RELEVANT[TICKET_QUEUE[q][0]], f"{s} {b}", re.I)) for q, s, b in zip(df.queue, df.subject.astype(str), df.body.astype(str))]]
    rnd, out = random.Random(seed), []
    for q, exp in TICKET_QUEUE.items():
        sub = df[df.queue == q]
        for _, r in sub.sample(n_per_queue, random_state=seed).iterrows():
            msg = f"{r.subject}. {str(r.body)}"[:max_chars]
            out.append({"message": msg, "expected": sorted(exp), "alt": [], "tags": ["tickets", q]})
    return out
