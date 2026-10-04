---
id: tech-ip-allowlist
category: technical
title: IP allow-listing and firewalls
---
# IP allow-listing and firewalls

Webhook requests come from the fixed IP ranges published at status.orbit.example/ips. Add them to your firewall allow-list if your endpoint is behind one. API calls from your servers to api.orbit.example use HTTPS port 443 only. Enterprise customers can restrict API keys to specific IPs in Dashboard > Developers.
