---
id: tech-api-authentication
category: technical
title: API authentication and keys
---
# API authentication and keys

All API requests need an API key sent as `Authorization: Bearer <key>`. Create and revoke keys in Dashboard > Developers > API keys. Keys are shown only once at creation.
A **401 Unauthorized** (AUTH_401_INVALID_TOKEN) means the key is missing, revoked, expired or has a typo/whitespace. Check that you use the key for the right workspace and environment. To rotate a key: create a new key, deploy it, then revoke the old key. Never put keys in client-side code or public repositories; if a key leaks revoke it immediately.
