---
id: billing-failed-payments
category: billing
title: Failed payments and retries
---
# Failed payments and retries

When a charge fails the invoice moves to status "failed". Common reasons: **card_declined** (bank refused), **insufficient_funds**, **expired_card**.

Retry schedule: we automatically retry on day 1, day 3, day 5 and day 7 after the first failure and email you each time (this happens whether or not you update your card; after updating it you can retry immediately with "Retry payment"). If the invoice is still unpaid after 14 days the account is suspended and the workspace becomes read-only; data is kept for 30 days.
To fix it: update the payment method in Dashboard > Billing, then click "Retry payment". A suspended account is reactivated automatically within 15 minutes of a successful payment.
