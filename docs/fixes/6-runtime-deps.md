# Document and pre-flight OPNsense runtime dependencies

## Problem

First run on a fresh dev VM (Docker pre-installed, but not qemu-utils)
failed with a 50-line traceback ending in:

```
FileNotFoundError: [Errno 2] No such file or directory: 'qemu-img'
```

`_ensure_stock_image_local` in `src/proxmoxsandbox/_impl/opnsense.py`
shells out to `wget`, `bunzip2`, and `qemu-img`. None of these are
mentioned as a prerequisite in `README.md`, `CONTRIBUTING.md`, or
`docs/opnsense-gateway.md` (they appear in the OPNsense-gateway doc
only as example commands a user would run by hand).

`docker` is also required but is more commonly pre-installed.

## Why it matters

Cryptic first-run failure for anyone trying the new feature. The
traceback doesn't make it obvious which package is missing on which
machine — controller vs. Proxmox host.

## Files

- `src/proxmoxsandbox/_impl/opnsense.py` — add a pre-flight check
- `README.md` (or `docs/opnsense-gateway.md`) — add a prerequisites
  section for the OPNsense feature

## Fix

### Code change

Add to `OpnsenseTemplateManager.ensure_template` (called during
`task_init`, fails fast before any work happens):

```python
import shutil

_REQUIRED_BINS = ("qemu-img", "bunzip2", "docker", "wget")

def _check_runtime_deps() -> None:
    missing = [b for b in _REQUIRED_BINS if shutil.which(b) is None]
    if missing:
        raise RuntimeError(
            f"OPNsense gateway support requires these binaries on PATH "
            f"but they are missing: {', '.join(missing)}. "
            f"On Debian/Ubuntu: sudo apt install qemu-utils bzip2 "
            f"docker.io wget"
        )
```

Call it at the top of `ensure_template` before any other work — only
when an OPNsense subnet is actually requested.

### Doc change

Add a section to `README.md` (under installation/requirements) or
to `docs/opnsense-gateway.md`:

> **OPNsense gateway prerequisites (controller machine).** If your
> eval uses `vnet_type="opnsense"`, the machine running Inspect needs
> these binaries on PATH for the one-time base-template build:
> `qemu-img`, `bunzip2`, `wget`, `docker`. On Debian/Ubuntu:
> `sudo apt install qemu-utils bzip2 docker.io wget`. After the base
> template is built (cached to `~/.cache/opnsense-injector/`), only
> `docker` is reused — and even that only if the cache is invalidated.

## Tests

Hard to unit-test `shutil.which` cleanly without mocking `PATH`.
Acceptable to skip; the change is mechanically obvious. Optionally:

```python
def test_runtime_deps_check(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda b: None)
    with pytest.raises(RuntimeError, match="qemu-img"):
        _check_runtime_deps()
```
