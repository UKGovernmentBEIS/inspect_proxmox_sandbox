# Validate `domain_whitelist` content and `VnetConfig` subnet shape

Bundles two Pydantic-validator additions in `src/proxmoxsandbox/schema.py`.
They share a file, a review pass, and the same "tighten validators added
for the OPNsense feature" theme.

---

## Part A: content checks for `SubnetConfig.domain_whitelist`

### Problem

`SubnetConfig._validate_vnet_type_constraints` only checks "is set / isn't
set". The strings inside are never validated. All of these are accepted:

| Input                 | What ends up in Unbound                                | Effect                                    |
| --------------------- | ------------------------------------------------------ | ----------------------------------------- |
| `""`                  | `local-zone "." transparent` (root zone)              | **Disables Unbound filtering entirely**   |
| `"."`                 | `local-zone "." transparent` (root zone)              | Same — disables filtering                 |
| `"*.example.com"`     | `local-zone "*.example.com." transparent`             | Matches nothing (wildcards unsupported)   |
| `"foo\nbar.com"`      | Newline-injected directive in `dns_whitelist.conf`    | Config injection if input is untrusted    |
| `"https://example.com"` | Treated as FQDN, never resolves                      | Silent no-op                              |
| `"example.com:443"`   | Treated as FQDN, never resolves                       | Silent no-op                              |
| `" example.com"`      | Whitespace in zone name                               | Unbound rejects at parse time             |
| `"1.2.3.4"`           | Treated as FQDN                                        | pf alias might accept it; Unbound won't   |

The first two are the worst — empty string or `"."` opens the root zone
in Unbound's `local-zone` table, defeating the default-deny.

### Fix

In `SubnetConfig`, validate each whitelist entry:

```python
import re

# RFC 1035 hostname; accepts trailing dot; rejects wildcards, IPs,
# whitespace, newlines, ports, schemes
_FQDN_RE = re.compile(
    r"^(?=.{1,253}\.?$)"
    r"([a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+"
    r"[a-zA-Z]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.?$"
)

@model_validator(mode="after")
def _validate_vnet_type_constraints(self) -> "SubnetConfig":
    # ... existing checks ...
    if self.vnet_type == "opnsense":
        # ... existing required-field checks ...
        for entry in self.domain_whitelist or ():
            if not _FQDN_RE.match(entry):
                raise ValueError(
                    f"domain_whitelist entry {entry!r} is not a valid "
                    f"FQDN. Wildcards (*.example.com), IPs, URLs, ports, "
                    f"and whitespace are not supported."
                )
```

The regex requires at least one dot (so bare TLDs like `"com"` are
rejected) and a non-numeric TLD label (rejecting `"1.2.3.4"`).

### Tests

`tests/proxmoxsandboxtest/test_schema.py`:

```python
@pytest.mark.parametrize("bad", [
    "", ".", "*", "*.example.com", "1.2.3.4",
    "example.com:443", "https://example.com", " example.com",
    "ex ample.com", "foo\nbar.com", "foo\"bar.com",
])
def test_subnet_rejects_bad_whitelist_entry(bad):
    with pytest.raises(ValidationError, match="domain_whitelist"):
        SubnetConfig(
            cidr=ip_network("10.0.2.0/24"),
            gateway=ip_address("10.0.2.1"),
            dhcp_ranges=(DhcpRange(...),),
            vnet_type="opnsense",
            domain_whitelist=(bad,),
        )

@pytest.mark.parametrize("good", [
    "example.com", "sub.example.com", "example.com.",
    "foo-bar.example.com", "a.b.c.d.example.com",
])
def test_subnet_accepts_valid_whitelist_entry(good): ...
```

---

## Part B: cross-subnet validation on `VnetConfig`

### Problem

`VnetConfig` accepts arbitrary tuples of subnets with no consistency
check. Two failure modes (verified by `_opnsense_subnets_by_vnet`
output):

1. **Mixing `proxmox` + `opnsense` subnets on the same VNet**: both are
   processed. Proxmox creates its dnsmasq + IPAM on the bridge; OPNsense
   auto-generates a gateway VM that runs its own DHCP. Two DHCP servers
   compete on one L2 broadcast domain.

2. **Two `opnsense` subnets on the same VNet**:
   `_opnsense_subnets_by_vnet` is a dict keyed by VNet alias —
   second subnet silently overwrites first, no warning.

### Fix

Add a `model_validator(mode="after")` to `VnetConfig`:

```python
@model_validator(mode="after")
def _validate_subnet_types(self) -> "VnetConfig":
    types = {s.vnet_type for s in self.subnets}
    if len(types) > 1:
        raise ValueError(
            f"All subnets within a VnetConfig must share the same "
            f"vnet_type; got mixed: {sorted(types)}. OPNsense and "
            f"Proxmox-managed DHCP cannot coexist on the same bridge."
        )
    opnsense_count = sum(
        1 for s in self.subnets if s.vnet_type == "opnsense"
    )
    if opnsense_count > 1:
        raise ValueError(
            f"At most one OPNsense-managed subnet per VnetConfig "
            f"is supported; got {opnsense_count}."
        )
    return self
```

### Tests

`tests/proxmoxsandboxtest/test_schema.py`:

```python
def test_vnet_rejects_mixed_subnet_types():
    with pytest.raises(ValidationError, match="vnet_type"):
        VnetConfig(alias="lan", subnets=(
            SubnetConfig(..., vnet_type="proxmox", snat=False),
            SubnetConfig(..., vnet_type="opnsense", domain_whitelist=("a.com",)),
        ))

def test_vnet_rejects_multiple_opnsense_subnets():
    with pytest.raises(ValidationError, match="At most one"):
        VnetConfig(alias="lan", subnets=(
            SubnetConfig(..., vnet_type="opnsense", domain_whitelist=("a.com",)),
            SubnetConfig(..., vnet_type="opnsense", domain_whitelist=("b.com",)),
        ))
```
