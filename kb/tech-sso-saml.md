---
id: tech-sso-saml
category: technical
title: SSO / SAML setup and troubleshooting
---
# SSO / SAML setup and troubleshooting

SSO (SAML 2.0) is available on Business and Enterprise. Configure under Dashboard > Security > SSO using your identity provider metadata URL. 
Common error **SSO_SAML_ASSERTION_EXPIRED**: the assertion's NotOnOrAfter time is already in the past, usually due to **clock skew** between your identity provider and Orbit. Fix: sync the IdP server clock with NTP; we allow up to 3 minutes of skew. Other causes: wrong ACS URL, wrong audience (entity ID) or an expired signing certificate.
