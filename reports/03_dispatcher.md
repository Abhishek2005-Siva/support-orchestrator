# 03 — Dispatcher evaluation
_Live LLM, no cache. Labels: handwritten (hand), Bitext & ticket datasets (Kaggle) mapped by evals/label_maps.py._

## Dispatcher routing — model `nvidia/nemotron-3-super-120b-a12b` (186 cases)

| slice | n | exact-set acc | primary-intent acc | p50 ms | p95 ms |
|---|---|---|---|---|---|
| ALL | 186 | 92.5% | 93.5% | 4477 | 12162 |
| handwritten | 62 | 98.4% | 100.0% | 4237 | 7687 |
| bitext | 76 | 92.1% | 93.4% | 4793 | 23156 |
| tickets | 48 | 85.4% | 85.4% | 4556 | 7993 |

| class | precision | recall | support |
|---|---|---|---|
| escalation | 93.3% | 93.3% | 15 |
| off_topic | 80.0% | 100.0% | 8 |
| billing | 100.0% | 107.6% | 66 |
| technical | 98.3% | 92.1% | 63 |
| general | 78.6% | 75.0% | 44 |

### Misclassified (for manual review)

- [hand] “Where can I find my invoices, and how do I add a new team member?” expected ['billing', 'general'] (alt []) got **['billing']** sentiment=neutral conf=0.9 forced=[]
- [bitext/check_payment_methods] “I don't know what to do to see the available payment methods” expected ['billing'] (alt []) got **['general']** sentiment=neutral conf=0.9 forced=[]
- [bitext/track_refund] “I want to see my compensation status, I need help” expected ['billing'] (alt []) got **['general']** sentiment=neutral conf=0.8 forced=[]
- [bitext/contact_human_agent] “talking to live agent” expected ['escalation'] (alt []) got **['general']** sentiment=neutral conf=0.9 forced=[]
- [bitext/contact_human_agent] “I try to talk to an assistant” expected ['escalation'] (alt []) got **['escalation', 'general']** sentiment=neutral conf=0.8 forced=['explicit_human_request']
- [bitext/edit_account] “I want to modify the user data, I need help” expected ['general'] (alt [['billing']]) got **['technical']** sentiment=neutral conf=0.7 forced=[]
- [bitext/create_account] “I want help creating a new damn standard account” expected ['general'] (alt [['technical'], ['billing']]) got **['escalation', 'general']** sentiment=angry conf=0.9 forced=['angry_customer']
- [tickets/Technical Support] “Inquiry on System Requirements for Top Performance. Hello Customer Support, I am writing to seek information on the system requirements need” expected ['technical'] (alt []) got **['general']** sentiment=neutral conf=0.9 forced=[]
- [tickets/Technical Support] “Overview of Project Management Integrations. Customer Support, seeking details on integration capabilities for project management in a SaaS ” expected ['technical'] (alt []) got **['general']** sentiment=neutral conf=0.9 forced=[]
- [tickets/IT Support] “Support Inquiry Form. Requesting assistance to enhance integration and functionality within the SaaS platform. Currently, the compatibility ” expected ['technical'] (alt []) got **['general']** sentiment=neutral conf=0.85 forced=[]
- [tickets/Service Outages and Maintenance] “Exploring Tool Integration with Project Management SaaS. I am keen on integrating several tools with our project management software as a se” expected ['technical'] (alt []) got **['general']** sentiment=neutral conf=0.9 forced=[]
- [tickets/Service Outages and Maintenance] “Combine CCleaner and McAfee. Boost security and performance enhancement” expected ['technical'] (alt []) got **['off_topic']** sentiment=neutral conf=0.95 forced=[]
- [tickets/Billing and Payments] “Inquiries About Payment Methods for McAfee Total Protection 2021. Hello support team, I am keen on acquiring a McAfee Total Protection 2021 ” expected ['billing'] (alt []) got **['off_topic']** sentiment=neutral conf=0.95 forced=[]
- [tickets/Billing and Payments] “Inquiry About Project Management Software Pricing. Interested in learning more about project management software. Please provide details on ” expected ['billing'] (alt []) got **['general']** sentiment=neutral conf=0.9 forced=[]