# Hide Unbound identity / version on OPNsense gateway

## Problem

Live test from agent VM:

```
$ dig @10.0.2.1 +short version.bind chaos txt
"unbound 1.22.0"
$ dig @10.0.2.1 +short hostname.bind chaos txt
"opnsense-gw.localdomain"
$ dig @10.0.2.1 +short id.server chaos txt
"opnsense-gw.localdomain"
```

The CHAOS-class queries answer with the resolver software, version,
and hostname. This confirms the gateway is OPNsense + Unbound 1.22.0
to anyone on LAN.

## Why it matters

Information disclosure that narrows the attacker's hypothesis space —
they no longer have to guess between OPNsense / pfSense / dnsmasq /
Linux-with-nft and can target known-CVEs for that specific Unbound
version. Not a direct compromise vector but cheap to suppress.

## Files

- `src/proxmoxsandbox/scripts/experimental/opnsense_config.xml`
  — the `<unbound>` section

## Fix

Add to the Unbound `<server:>` configuration:

```
hide-identity: yes
hide-version: yes
```

In OPNsense's config.xml the equivalent is in the `<unbound>` block;
locate the existing element list and add the two flags. After this,
chaos queries return `REFUSED` or empty.

## Tests

Extend `tests/proxmoxsandboxtest/test_opnsense_escape.py` with a chaos
probe and assert no leak:

```python
ModelOutput.for_tool_call(
    model="mockllm/model",
    tool_name="bash",
    tool_arguments={"cmd": (
        "dig @10.0.2.1 +short version.bind chaos txt; "
        "dig @10.0.2.1 +short hostname.bind chaos txt; "
        "dig @10.0.2.1 +short id.server chaos txt"
    )},
),
```

Assert the result does NOT contain `unbound`, `opnsense`, or any version
string.
