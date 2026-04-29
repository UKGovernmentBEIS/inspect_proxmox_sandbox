# Restrict ICMP egress on OPNsense LAN

## Problem

Agent VM can ping any public IP from behind the gateway. Live test:

```
$ ping -c 2 -W 3 1.1.1.1
64 bytes from 1.1.1.1: icmp_seq=2 ttl=52 time=3.50 ms
PING_BARE_EXIT=0
```

`src/proxmoxsandbox/scripts/experimental/opnsense_config.xml` rule
`a1000001-…` is `pass icmp from <network>lan</network> to <any/>` with
the comment "Allow ICMP from LAN (for diagnostics)" and no `<quick>`.
I expected the later `a1000005` block-all rule to override, but in this
pf configuration the pass rule wins.

## Why it matters

ICMP is an egress channel that bypasses the domain whitelist:

- Connectivity probing of arbitrary public hosts (port 0 / no-state info leak)
- Sub-MTU echo payloads as a covert channel
- Source-IP discovery via reflected echo replies from a known reflector

## Files

- `src/proxmoxsandbox/scripts/experimental/opnsense_config.xml` — rule `a1000001-…`

## Fix

Two reasonable options:

**(a) Delete the rule entirely.** The block-all (`a1000005`) then catches
ICMP. This loses the ability to ping the gateway from LAN, which is
rarely needed inside an eval.

**(b) Restrict destination to OPNsense itself.** Replace `<any/>` with
`<network>lanip</network>`:

```xml
<rule uuid="a1000001-...">
  <type>pass</type>
  <interface>lan</interface>
  <ipprotocol>inet</ipprotocol>
  <protocol>icmp</protocol>
  <source><network>lan</network></source>
  <destination><network>lanip</network></destination>
  <descr>Allow ICMP from LAN to gateway only (diagnostics)</descr>
</rule>
```

Recommend (a) — simpler, smaller attack surface, no real cost.

## Tests

Add a probe + assertion to `tests/proxmoxsandboxtest/test_opnsense_escape.py`:

```python
# Probe (between existing probes)
ModelOutput.for_tool_call(
    model="mockllm/model",
    tool_name="bash",
    tool_arguments={"cmd": "ping -c 2 -W 3 1.1.1.1; echo PING_EXIT=$?"},
),
```

Assert the result contains `PING_EXIT=1` or `PING_EXIT=2` (failure) — not
`PING_EXIT=0`.
