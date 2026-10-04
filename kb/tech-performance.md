---
id: tech-performance
category: technical
title: Slow queries and timeouts
---
# Slow queries and timeouts

Query requests time out after **30 seconds**. To speed up queries: narrow the date range, add filters, use pagination (`limit` up to 1,000), and prefer pre-aggregated endpoints. Avoid polling faster than once per 5 seconds; use webhooks instead. Responses with HTTP 504 mean the query exceeded the timeout.
