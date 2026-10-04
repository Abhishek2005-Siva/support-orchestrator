---
id: tech-error-codes
category: technical
title: API error codes reference
---
# API error codes reference

- **400** Bad Request: malformed JSON or invalid parameters; read the `error.message` field.
- **401** Unauthorized: invalid or missing API key.
- **403** Forbidden: key is valid but the plan or role lacks permission for this endpoint.
- **404** Not Found: wrong resource ID or endpoint path.
- **409** Conflict: duplicate idempotency key or concurrent update.
- **429** Too Many Requests: rate limit exceeded; honour Retry-After.
- **500** Internal Server Error and **502/503** gateway errors: temporary; retry with exponential backoff and check the status page.
Always log the `x-request-id` response header and include it in support tickets.
