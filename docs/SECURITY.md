# Network security model

BibTeX metadata and publisher redirects are untrusted. An attacker may supply a URL or
hostname intended to reach loopback, a private network, link-local services, or cloud
metadata endpoints from the machine running BibVault.

## Request boundary

BibVault accepts only HTTP and HTTPS acquisition URLs. It rejects local hostnames and
non-public IP literals, resolves hostnames before requests, and rejects the entire DNS
answer if any address is non-public. Redirects pass through the same checks. Response
size and PDF/HTML validation are separate controls and do not replace this network
boundary.

The ordinary HTTPX path performs two checks:

1. a request hook validates the URL and its current DNS answer; and
2. the connection backend resolves again immediately before opening TCP, validates all
   returned addresses, and connects using the selected IP address rather than resolving
   the hostname a third time.

The original hostname remains the HTTP `Host` value and TLS SNI name, so certificate
validation is not weakened. DNS failures fail at the connection boundary instead of
falling back to an unguarded connection.

Crawl4AI's HTTP adapter uses an aiohttp resolver that validates the addresses returned
to its connector. Its adapter tests remain opt-in.

## DNS rebinding

A preflight-only check has a time-of-check/time-of-use window: an attacker-controlled
hostname can first resolve publicly, pass validation, and then resolve to a private
address when the HTTP client opens its socket. The connection-time resolver and IP
pinning close that window for the ordinary HTTPX and Crawl4AI HTTP paths.

The interactive Chromium path is different. BibVault checks each routed URL before
allowing it, but Chromium performs its own later DNS lookup and does not expose a
portable pre-connect peer-address policy here. Therefore interactive browser mode is
not treated as an SSRF security boundary. It is explicit opt-in and should be used only
with trusted references; run it in a network sandbox when the host can reach sensitive
internal services.

## Regression coverage

Offline tests cover private and link-local literals, alternate loopback spellings,
hostnames with private or mixed DNS answers, redirects into private networks, a DNS
answer that changes between preflight and connection, and verification that the TCP
backend receives the already validated IP rather than the attacker-controlled hostname.
No real network or installed browser is needed for these checks.
