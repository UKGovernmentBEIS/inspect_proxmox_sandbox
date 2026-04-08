# Domain Whitelist Gateway: Investigation Spec

> **Decision (2026-04-01):** OPNsense was chosen as the gateway appliance.
> See `docs/opnsense-gateway.md` for technical reference and integration plan.
> The candidate evaluation below is historical context only.

## Goal

Add a feature to `inspect_proxmox_sandbox` that allows per-VNet domain whitelisting
for egress traffic. When configured, only outbound connections to IP addresses matching
a set of whitelisted domain names should be permitted. All other egress is dropped.

This is achieved by deploying a gateway VM in the VNet that performs DNS-based egress
filtering. Sandbox VMs route through this gateway instead of using Proxmox's built-in
SNAT.

## Background

### This repo

`inspect_proxmox_sandbox` is an Inspect AI sandbox provider that manages VMs on
Proxmox. It creates SDN zones/vnets/subnets, boots VMs, and communicates with them
via the QEMU guest agent. Key files:

- `src/proxmoxsandbox/schema.py` -- Pydantic config models (SdnConfig, VnetConfig,
  SubnetConfig, VmConfig, etc.)
- `src/proxmoxsandbox/_impl/sdn_commands.py` -- SDN CRUD (zones, vnets, subnets, IPAM)
- `src/proxmoxsandbox/_impl/infra_commands.py` -- Orchestration (create/delete SDN + VMs)
- `src/proxmoxsandbox/_impl/qemu_commands.py` -- VM lifecycle
- `src/proxmoxsandbox/_impl/built_in_vm.py` -- Built-in VM image management

Currently, when `snat=True` on a subnet, Proxmox itself acts as the gateway and does
MASQUERADE. There is no traffic filtering. The README roadmap mentions "pfSense VM as
default route" and "Firewall off the SDN from the Proxmox server and from other SDNs."

### Why domain whitelisting matters

These sandboxes run AI agents that may be adversarial. We need to control what they
can reach on the internet. A domain whitelist is a practical middle ground between
full internet access and no internet access.

### Mechanism

All three candidates work the same way under the hood: they resolve whitelisted
FQDNs to IP addresses and populate packet filter rules/sets with those IPs. Traffic
to non-whitelisted IPs is dropped. This is L3/L4 filtering -- none of these approaches
can inspect encrypted traffic (post-ECH/ESNI). The security model accepts this
limitation: we're filtering by IP, refreshed from DNS.

## Candidates to evaluate

### 1. VyOS (rolling release)

DELETED

### 2. OPNsense

**Why it's interesting:** Mature firewall appliance. Broad feature set. Active
community. BSD-licensed.

**Key links:**

- OPNsense download: https://opnsense.org/download/
- config.xml importer: https://docs.opnsense.org/manual/install.html
- opnsense-confgen: https://github.com/malwarology/opnsense-confgen
- Aliases (FQDN resolution): https://docs.opnsense.org/manual/aliases.html
- Firewall API: https://docs.opnsense.org/development/api/core/firewall.html
- VM installation: https://docs.opnsense.org/manual/virtuals.html

**What to investigate:**

1. Download the ISO. Boot it on the Proxmox instance. Document: RAM at idle, boot
   time, disk footprint.
2. Attempt a fully unattended setup using config.xml import:
   - Generate a config.xml (manually or via opnsense-confgen) with:
     - WAN and LAN interfaces configured
     - NAT outbound on WAN
     - An alias of type "Host(s)" containing FQDNs (e.g., `example.com`, `ifconfig.me`)
     - A firewall rule: allow LAN->WAN only to the FQDN alias, block all else
     - DHCP on LAN advertising itself as gateway
     - API key bootstrapped for future programmatic changes
   - Pack config.xml into an ISO
   - Attach ISO to the OPNsense VM and boot
   - Does it come up fully configured with zero interaction?
3. Test the same scenarios as VyOS (curl whitelisted domain, curl blocked domain,
   ping raw IP).
4. How does OPNsense refresh the FQDN-to-IP resolution? What's the interval?
   Is it configurable?
5. Can we template config.xml generation in Python? How complex is the XML schema
   for the parts we need (interfaces, NAT, aliases, rules)?
6. Document the automation gap: what can't be done via config.xml import and would
   require the REST API or manual steps?

### 3. Plain Linux (Ubuntu 24.04) + nft-dns

DELETED

## Evaluation criteria

For each candidate, produce a summary covering:

| Criterion                | What to measure                                                                           |
| ------------------------ | ----------------------------------------------------------------------------------------- |
| **Works at all**         | Can you get domain-based egress filtering working end-to-end?                             |
| **Automation**           | Can you go from image + config to working gateway with zero interaction?                  |
| **Boot time**            | Time from VM start to gateway accepting and filtering traffic                             |
| **Resource usage**       | RAM and disk at idle with the gateway config active                                       |
| **FQDN refresh**         | How/when are domain-to-IP mappings refreshed? Configurable?                               |
| **Failure mode**         | What happens if DNS is unreachable? If the filtering daemon crashes? Fail open or closed? |
| **Wildcard support**     | Can you whitelist `*.example.com`?                                                        |
| **Dev integration cost** | How much code in this repo to create/configure/tear down the gateway VM?                  |
| **Config complexity**    | Lines of config/template needed. How brittle is it?                                       |
| **Logging/debugging**    | How do you see what was allowed/blocked?                                                  |

## Proxmox access

The investigator has access to a Proxmox instance via environment variables:

```
PROXMOX_HOST, PROXMOX_PORT, PROXMOX_USER, PROXMOX_REALM,
PROXMOX_PASSWORD, PROXMOX_NODE, PROXMOX_VERIFY_TLS, PROXMOX_IMAGE_STORAGE
```

You can use the sandbox provider itself (via `uv run` in this repo) to create VNets
and VMs, or use the Proxmox API directly. The provider's existing test suite in
`tests/` shows how to set up SDN configs programmatically.

## Networking setup for testing

For each candidate, you'll need two VNets:

1. **WAN VNet** -- has `snat=True` so the gateway VM can reach the internet.
2. **LAN VNet** -- has `snat=False`. Sandbox VMs live here. The gateway VM is their
   only route out.

The gateway VM has one NIC on each VNet. It runs NAT (masquerade) from LAN to WAN
and applies the domain whitelist filter to forwarded traffic.

The Proxmox host's built-in SNAT on the WAN VNet provides the actual internet path.
The Proxmox firewall (a separate investigation) should eventually prevent LAN VMs from
bypassing the gateway, but for this investigation, just test that the gateway filtering
works when traffic is routed through it correctly.

## Output

Write your findings to `docs/domain-whitelist-gateway-findings.md` in this repo.
Include:

1. A summary table comparing the three candidates on the evaluation criteria above.
2. Detailed notes for each candidate (what you did, what worked, what didn't, exact
   commands and configs used).
3. A recommendation with rationale.
4. If the recommended option works, a sketch of the schema changes and code changes
   needed in this repo to integrate it (which files, what new config fields, rough
   lifecycle: when is the gateway VM created/configured/torn down).
