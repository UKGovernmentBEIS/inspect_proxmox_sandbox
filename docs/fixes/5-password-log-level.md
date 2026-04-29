# Don't log OPNsense plaintext password at INFO

## Problem

`src/proxmoxsandbox/_impl/opnsense.py:214`:

```python
logger.info(f"OPNsense root password: {plaintext_password}")
```

Live confirmed: each eval logs `OPNsense root password: <random>` (e.g.
`RtEoJzCWHS5te5HZZe9E5g`, `_x2XVd28y_D0sJpZdCSUqQ`). The line ends up in:

- The local Inspect `.eval` JSON log
- The `aisitools` telemetry stream → S3 bucket
  `aisi-data-eu-west-2-prod`

So a fresh, valid OPNsense root password is in the shared telemetry
bucket every time an eval runs.

## Why it matters

Practical exposure is low: from LAN, SSH/web admin are blocked by the
floating pf rules; from WAN, an attacker would need network access to
the Proxmox host's WAN VNet, which already implies a stronger
compromise. So the password isn't a key to anything an attacker
without prior access could exploit.

But the principle of not putting credentials into shared log streams
still applies, and reviewers of telemetry data shouldn't be casually
exposed to gateway credentials when investigating unrelated runs.

## Files

- `src/proxmoxsandbox/_impl/opnsense.py` (lines 213–214)
- Possibly `docs/opnsense-gateway.md` (the "Interacting with OPNsense"
  section explains how to extract the password from the eval log)

## Fix

Drop to DEBUG so it stays out of the default telemetry stream:

```python
logger.debug(f"OPNsense root password: {plaintext_password}")
```

Operators who actually need the password for debugging set
`INSPECT_LOG_LEVEL=debug` for that run.

Alternatively, remove the line entirely. The bcrypt hash in
`config.xml` is sufficient for the running OPNsense; the plaintext is
discarded after that. An operator who needs SSH access can patch the
template build step locally to use a known password.

Recommend: drop to DEBUG (preserves the operator workflow) and update
`docs/opnsense-gateway.md` to mention the log-level requirement.

## Tests

`tests/proxmoxsandboxtest/test_opnsense_password.py` (new):

```python
import logging
from proxmoxsandbox._impl.opnsense import generate_config_xml

def test_password_not_logged_at_info(caplog):
    caplog.set_level(logging.INFO, logger="proxmoxsandbox._impl.opnsense")
    generate_config_xml(_some_opnsense_subnet())
    # The randomly-generated password should not have been emitted.
    assert "password" not in caplog.text.lower()

def test_password_available_at_debug(caplog):
    caplog.set_level(logging.DEBUG, logger="proxmoxsandbox._impl.opnsense")
    generate_config_xml(_some_opnsense_subnet())
    assert "OPNsense root password" in caplog.text
```
