---
id: tech-webhooks
category: technical
title: Webhooks: delivery, retries and signatures
---
# Webhooks: delivery, retries and signatures

Orbit POSTs events to your HTTPS endpoint. Your endpoint must respond with a 2xx status within **10 seconds**; slower responses are recorded as WEBHOOK_TIMEOUT and retried. Retries use exponential backoff for up to **24 hours** (1 min, 5 min, 30 min, 2 h, then every 6 h). After 24 hours of failures the endpoint is disabled and you are emailed.
Verify authenticity with the `Orbit-Signature` header (HMAC-SHA256 of the raw body using your webhook secret). Respond fast and process asynchronously (queue the event, return 200 immediately). Check Dashboard > Developers > Webhooks > Delivery log for failures.
