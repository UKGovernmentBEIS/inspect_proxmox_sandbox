# Static-map IP sanity checks

## Problem

`_collect_static_maps` in `src/proxmoxsandbox/_impl/infra_commands.py`
blindly emits `<staticmap>` entries for any `VmNicConfig(mac, ipv4)` on
an OPNsense LAN. Live-confirmed via `generate_config_xml`, three
silent footguns:

| Configuration                                | Generated `<staticmap>`         | Outcome                                              |
| -------------------------------------------- | ------------------------------- | ---------------------------------------------------- |
| `ipv4=10.0.99.42` on `10.0.2.0/24` LAN       | `<ipaddr>10.0.99.42</ipaddr>`  | OPNsense ignores; VM gets dynamic IP, user confused  |
| `ipv4=10.0.2.55` w/ DHCP range `.50–.200`    | `<ipaddr>10.0.2.55</ipaddr>`   | Static-vs-dynamic conflict (mostly works, but risky) |
| `ipv4=10.0.2.1` (= OPNsense LAN IP)           | `<ipaddr>10.0.2.1</ipaddr>`    | Assigns gateway IP to a sandbox VM                   |

## Why it matters

Tests pass because OPNsense silently ignores invalid entries. The user
believes their VM has a static IP; the agent gets a dynamic one (or
none, or a duplicate). Nothing fails loudly.

## Files

- `src/proxmoxsandbox/_impl/infra_commands.py` — `_collect_static_maps`
  (or its caller, where the matching `SubnetConfig` is in scope)

## Fix

`_collect_static_maps` already knows the LAN subnet the static map will
go to (its caller has `opnsense_subnets[lan_alias]`). Validate at
collection time:

```python
def _validate_static_ip(
    subnet: SubnetConfig, vm_name: str | None, ipv4: IPvAnyAddress
) -> None:
    name = vm_name or "<unnamed>"
    if ipv4 not in subnet.cidr:
        raise ValueError(
            f"VM {name!r}: static IP {ipv4} is outside LAN subnet "
            f"{subnet.cidr}"
        )
    if ipv4 == subnet.gateway:
        raise ValueError(
            f"VM {name!r}: static IP {ipv4} collides with the OPNsense "
            f"gateway IP"
        )
    for r in subnet.dhcp_ranges:
        if r.start <= ipv4 <= r.end:
            raise ValueError(
                f"VM {name!r}: static IP {ipv4} is inside the dynamic "
                f"DHCP range {r.start}–{r.end}; choose an address "
                f"outside the range"
            )
```

Call from `_collect_static_maps` (signature must take the
`opnsense_subnets` mapping or the caller passes the `SubnetConfig`).

## Tests

`tests/proxmoxsandboxtest/test_infra_commands.py` (new file or extend
existing):

```python
def _opn_subnet():
    return SubnetConfig(
        cidr=ip_network("10.0.2.0/24"),
        gateway=ip_address("10.0.2.1"),
        dhcp_ranges=(DhcpRange(
            start=ip_address("10.0.2.50"),
            end=ip_address("10.0.2.200"),
        ),),
        vnet_type="opnsense",
        domain_whitelist=("example.com",),
    )

@pytest.mark.parametrize("bad_ip", [
    "10.0.99.42",  # outside subnet
    "10.0.2.1",    # gateway collision
    "10.0.2.55",   # inside DHCP pool
    "10.0.2.150",  # inside DHCP pool
])
def test_static_ip_rejected(bad_ip): ...

def test_static_ip_accepted_in_subnet_outside_pool():
    # 10.0.2.10 — in subnet, not gateway, not in pool
    ...
```
