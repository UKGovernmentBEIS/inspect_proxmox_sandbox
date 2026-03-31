# ADR-001: dnsmasq nftset over pfSense for domain-based egress filtering

**Status:** Accepted
**Date:** 2026-03-31
**Context:** allow_domains egress filtering for sandbox VMs

## Decision

Use dnsmasq's `nftset=` directive to dynamically push resolved IPs into the
nftables `allowed_ips` set, rather than deploying pfSense/OPNsense as a
firewall appliance.

## Why this approach

dnsmasq is both the DNS resolver and the nftables set populator. When a
sandbox VM queries `cdn.example.com`, dnsmasq resolves it, returns the answer
to the client, and simultaneously pushes the resolved IPs into the nftables
set. The firewall and DNS resolver share state directly — zero lag, zero
desync.

This is a **cooperative design**: the DNS resolver and packet filter are on
the same box and communicate via a shared data structure (the nftables set).

## The alternative not taken

**pfSense/OPNsense FQDN aliases** resolve domains *independently* from the
client on a 300-second timer via a daemon called `filterdns`. The firewall's
DNS resolver (unbound) and packet filter (pf) are architecturally separate
with no shared-state mechanism.

This creates a **resolver desync problem**: if `cdn.example.com` is behind a
CDN with geo-distributed anycast, pfSense gets IPs {A,B,C} while the sandbox
VM gets {D,E,F}. Traffic to {D,E,F} is dropped because pfSense only knows
about {A,B,C}. The pfSense docs explicitly warn about this.

This is an **adversarial design**: two systems independently discover the same
information and hope they agree.

Additional costs of pfSense:
- 2-4GB RAM per clone vs ~256MB (10-16x overhead)
- Full FreeBSD with PHP web UI (large attack surface)
- 30-60s boot time vs seconds
- Per-eval isolation requires per-eval clones (expensive) or shared instance
  (breaks isolation)

## The general principle

**When you control both sides of a data flow, make them share state directly
instead of having each side independently discover the same information.**
Any time you're resolving/discovering/computing the same thing in two places
and hoping they agree, you have an adversarial design that will eventually
desync.
