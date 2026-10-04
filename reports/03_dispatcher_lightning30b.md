# 03 — Dispatcher evaluation
_Live LLM, no cache. Labels: handwritten (hand), Bitext & ticket datasets (Kaggle) mapped by evals/label_maps.py._

## Dispatcher routing — model `nvidia/nemotron-3.5-lightning-30b-a3b` (186 cases)

| slice | n | exact-set acc | primary-intent acc | p50 ms | p95 ms |
|---|---|---|---|---|---|
| ALL | 186 | 85.5% | 88.2% | 9494 | 53154 |
| handwritten | 62 | 98.4% | 100.0% | 13777 | 53154 |
| bitext | 76 | 88.2% | 93.4% | 12358 | 55336 |
| tickets | 48 | 64.6% | 64.6% | 6420 | 47556 |

| class | precision | recall | support |
|---|---|---|---|
| escalation | 75.0% | 100.0% | 15 |
| off_topic | 57.1% | 100.0% | 8 |
| billing | 100.0% | 92.4% | 66 |
| technical | 96.3% | 82.5% | 63 |
| general | 72.7% | 72.7% | 44 |

### Misclassified (for manual review)

- [hand] “Where can I find my invoices, and how do I add a new team member?” expected ['billing', 'general'] (alt []) got **['billing']** sentiment=neutral conf=0.9 forced=[]
- [bitext/payment_issue] “i dont know how to notify of troublse with payments” expected ['billing'] (alt [['billing', 'technical']]) got **['billing', 'escalation']** sentiment=neutral conf=0.3 forced=['low_dispatch_confidence']
- [bitext/check_payment_methods] “I don't know what to do to see the available payment methods” expected ['billing'] (alt []) got **['general']** sentiment=neutral conf=0.9 forced=[]
- [bitext/get_refund] “refund currency symbol50” expected ['billing'] (alt []) got **['billing', 'escalation']** sentiment=neutral conf=0.3 forced=['low_dispatch_confidence']
- [bitext/track_refund] “I want to see my compensation status, I need help” expected ['billing'] (alt []) got **['general']** sentiment=neutral conf=0.8 forced=[]
- [bitext/contact_customer_service] “I don't know how I can speak wih customer service” expected ['general'] (alt []) got **['escalation']** sentiment=angry conf=0.9 forced=['angry_customer']
- [bitext/contact_human_agent] “I try to talk to an assistant” expected ['escalation'] (alt []) got **['escalation', 'general']** sentiment=neutral conf=0.3 forced=['explicit_human_request']
- [bitext/contact_human_agent] “can you help me speaking to an assistant?” expected ['escalation'] (alt []) got **['escalation', 'general']** sentiment=neutral conf=0.3 forced=['explicit_human_request']
- [bitext/edit_account] “I want to modify the user data, I need help” expected ['general'] (alt [['billing']]) got **['technical']** sentiment=neutral conf=0.7 forced=[]
- [bitext/review] “I don't know how I can write a comment for  a product” expected ['general'] (alt []) got **['technical']** sentiment=neutral conf=0.8 forced=[]
- [tickets/Technical Support] “Inquiry on System Requirements for Top Performance. Hello Customer Support, I am writing to seek information on the system requirements need” expected ['technical'] (alt []) got **['off_topic']** sentiment=neutral conf=0.95 forced=[]
- [tickets/Technical Support] “Overview of Project Management Integrations. Customer Support, seeking details on integration capabilities for project management in a SaaS ” expected ['technical'] (alt []) got **['general']** sentiment=positive conf=0.9 forced=[]
- [tickets/Technical Support] “Application Crash. Facing an application crash while using project management software on a 4K touchscreen monitor. The issue might be due t” expected ['technical'] (alt []) got **['escalation', 'technical']** sentiment=neutral conf=0.3 forced=['low_dispatch_confidence']
- [tickets/Technical Support] “Support Request for SaaS Platform Crash. Our SaaS platform unexpectedly crashed, leading to data loss. Potential reasons might be insufficie” expected ['technical'] (alt []) got **['off_topic']** sentiment=neutral conf=0.95 forced=[]
- [tickets/IT Support] “Support Inquiry Form. Requesting assistance to enhance integration and functionality within the SaaS platform. Currently, the compatibility ” expected ['technical'] (alt []) got **['general']** sentiment=neutral conf=0.85 forced=[]
- [tickets/IT Support] “Inquiry Regarding Dokumentenscanner Integration with GitLab. I am contacting you to seek guidance on integrating Dokumentenscanner with GitL” expected ['technical'] (alt []) got **['off_topic']** sentiment=neutral conf=0.95 forced=[]
- [tickets/IT Support] “Request for Detailed Specifications of Project Management SaaS Platform. Hello, I am writing to inquire about the detailed specifications fo” expected ['technical'] (alt []) got **['general']** sentiment=neutral conf=0.9 forced=[]
- [tickets/IT Support] “Request for Integration Update Information. I am reaching out to request details on the integration update for my compatible software and ha” expected ['technical'] (alt []) got **['general']** sentiment=neutral conf=0.9 forced=[]
- [tickets/IT Support] “Website crashes frequently after Magento update due to incompatible plugin versions. The website is crashing often after the Magento update ” expected ['technical'] (alt []) got **['off_topic']** sentiment=neutral conf=0.95 forced=[]
- [tickets/Service Outages and Maintenance] “Problem with Server During Peak Hours. To Whom It May Concern, I hope this message finds you well. I am reaching out due to a server outage ” expected ['technical'] (alt []) got **['escalation', 'technical']** sentiment=neutral conf=0.3 forced=['low_dispatch_confidence']
- [tickets/Service Outages and Maintenance] “Urgent Request to Optimize Server Performance. Dear Customer Support, I urgently need adjustments to the server settings to optimize perform” expected ['technical'] (alt []) got **['escalation', 'technical']** sentiment=neutral conf=0.3 forced=['low_dispatch_confidence']
- [tickets/Service Outages and Maintenance] “Exploring Tool Integration with Project Management SaaS. I am keen on integrating several tools with our project management software as a se” expected ['technical'] (alt []) got **['general']** sentiment=neutral conf=0.9 forced=[]
- [tickets/Service Outages and Maintenance] “Combine CCleaner and McAfee. Boost security and performance enhancement” expected ['technical'] (alt []) got **['off_topic']** sentiment=neutral conf=0.95 forced=[]
- [tickets/Service Outages and Maintenance] “nan. I am keen on integrating several tools with a project management software as a service (SaaS) to boost workflow and productivity. This ” expected ['technical'] (alt []) got **['general']** sentiment=neutral conf=0.7 forced=[]
- [tickets/Billing and Payments] “Inquiries About Payment Methods for McAfee Total Protection 2021. Hello support team, I am keen on acquiring a McAfee Total Protection 2021 ” expected ['billing'] (alt []) got **['off_topic']** sentiment=neutral conf=0.95 forced=[]
- [tickets/Billing and Payments] “Inquiry About Project Management Software Pricing. Interested in learning more about project management software. Please provide details on ” expected ['billing'] (alt []) got **['general']** sentiment=neutral conf=0.95 forced=[]
- [tickets/Billing and Payments] “Query for Billing Integration Options on SaaS Platform. I am writing to inquire about the billing integration options available on your SaaS” expected ['billing'] (alt []) got **['general']** sentiment=neutral conf=0.9 forced=[]