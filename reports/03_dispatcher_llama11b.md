# 03 — Dispatcher evaluation
_Live LLM, no cache. Labels: handwritten (hand), Bitext & ticket datasets (Kaggle) mapped by evals/label_maps.py._

## Dispatcher routing — model `meta/llama-3.2-11b-vision-instruct` (186 cases)

| slice | n | exact-set acc | primary-intent acc | p50 ms | p95 ms |
|---|---|---|---|---|---|
| ALL | 186 | 89.8% | 90.9% | 4708 | 11150 |
| handwritten | 62 | 98.4% | 100.0% | 4578 | 10241 |
| bitext | 76 | 94.7% | 96.1% | 4238 | 10901 |
| tickets | 48 | 70.8% | 70.8% | 5536 | 12209 |

| class | precision | recall | support |
|---|---|---|---|
| escalation | 63.3% | 100.0% | 15 |
| off_topic | 100.0% | 100.0% | 8 |
| billing | 100.0% | 92.4% | 66 |
| technical | 98.2% | 88.9% | 63 |
| general | 84.1% | 77.3% | 44 |

### Misclassified (for manual review)

- [hand] “Where can I find my invoices, and how do I add a new team member?” expected ['billing', 'general'] (alt []) got **['billing']** sentiment=neutral conf=0.95 forced=[]
- [bitext/check_payment_methods] “i do not know what i need to do to list ur payment methods” expected ['billing'] (alt []) got **['general']** sentiment=neutral conf=0.85 forced=[]
- [bitext/check_payment_methods] “I don't know what to do to see the available payment methods” expected ['billing'] (alt []) got **['technical']** sentiment=neutral conf=0.95 forced=[]
- [bitext/get_refund] “need assistance demanding my money back” expected ['billing'] (alt []) got **['billing', 'escalation']** sentiment=negative conf=0.9 forced=[]
- [bitext/track_refund] “I want to see my compensation status, I need help” expected ['billing'] (alt []) got **['general']** sentiment=neutral conf=0.8 forced=[]
- [tickets/Technical Support] “Inquiry on System Requirements for Top Performance. Hello Customer Support, I am writing to seek information on the system requirements need” expected ['technical'] (alt []) got **['escalation', 'technical']** sentiment=neutral conf=0.3 forced=['low_dispatch_confidence']
- [tickets/Technical Support] “Overview of Project Management Integrations. Customer Support, seeking details on integration capabilities for project management in a SaaS ” expected ['technical'] (alt []) got **['general']** sentiment=neutral conf=0.95 forced=[]
- [tickets/IT Support] “Problem with System Down. Hello Customer Support, I am facing system crash issues that are preventing access to critical tools across multip” expected ['technical'] (alt []) got **['escalation', 'technical']** sentiment=negative conf=0.95 forced=[]
- [tickets/IT Support] “Inquiry Regarding Dokumentenscanner Integration with GitLab. I am contacting you to seek guidance on integrating Dokumentenscanner with GitL” expected ['technical'] (alt []) got **['escalation', 'technical']** sentiment=neutral conf=0.3 forced=['low_dispatch_confidence']
- [tickets/IT Support] “nan. Esteemed Customer Support, <br />We are encountering an unforeseen downtime that is impacting user access to our system. This issue mig” expected ['technical'] (alt []) got **['escalation', 'technical']** sentiment=neutral conf=0.95 forced=[]
- [tickets/IT Support] “Request for Detailed Specifications of Project Management SaaS Platform. Hello, I am writing to inquire about the detailed specifications fo” expected ['technical'] (alt []) got **['general']** sentiment=neutral conf=0.95 forced=[]
- [tickets/Service Outages and Maintenance] “Problem with Server During Peak Hours. To Whom It May Concern, I hope this message finds you well. I am reaching out due to a server outage ” expected ['technical'] (alt []) got **['escalation', 'technical']** sentiment=neutral conf=0.95 forced=[]
- [tickets/Service Outages and Maintenance] “Problem with Service. A significant service outage has impacted multiple devices running software that utilizes data analytics. The issue mi” expected ['technical'] (alt []) got **['escalation', 'technical']** sentiment=negative conf=0.95 forced=[]
- [tickets/Service Outages and Maintenance] “Urgent Attention Required: Severe Outage Impacting Data Analytics Systems. A critical outage has impacted our data analytics systems, likely” expected ['technical'] (alt []) got **['escalation', 'technical']** sentiment=negative conf=0.95 forced=[]
- [tickets/Service Outages and Maintenance] “Unexpected System Outage Reported. Dear Customer Support,\n\nI am contacting you to report an unforeseen system outage that has had signific” expected ['technical'] (alt []) got **['escalation', 'technical']** sentiment=neutral conf=0.95 forced=[]
- [tickets/Service Outages and Maintenance] “Exploring Tool Integration with Project Management SaaS. I am keen on integrating several tools with our project management software as a se” expected ['technical'] (alt []) got **['escalation', 'technical']** sentiment=neutral conf=0.3 forced=['low_dispatch_confidence']
- [tickets/Service Outages and Maintenance] “Combine CCleaner and McAfee. Boost security and performance enhancement” expected ['technical'] (alt []) got **['escalation', 'general']** sentiment=neutral conf=0.3 forced=['low_dispatch_confidence']
- [tickets/Service Outages and Maintenance] “nan. I am keen on integrating several tools with a project management software as a service (SaaS) to boost workflow and productivity. This ” expected ['technical'] (alt []) got **['general']** sentiment=neutral conf=0.85 forced=[]
- [tickets/Billing and Payments] “Inquiry About Project Management Software Pricing. Interested in learning more about project management software. Please provide details on ” expected ['billing'] (alt []) got **['general']** sentiment=neutral conf=0.95 forced=[]