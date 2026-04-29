# Set timeouts on pycurl uploads

Pre-existing, not OPNsense-specific — but exposed during this PR's
exploratory testing when a cleanup-then-re-create eval flow hung
indefinitely on the ISO upload step.

## Problem

`upload_file_with_curl` in `src/proxmoxsandbox/_impl/async_proxmox.py`
configures a `pycurl.Curl()` handle without setting any timeouts.
During testing, a `pycurl.perform()` call stalled for ~9 minutes with
no recovery. All Python threads were in `epoll_wait`/`futex`; the
process had socket FDs but no active TCP connections to the Proxmox
host. The eval had to be killed.

The hang reproduced once (after a leftover-cleanup pass on the second
eval invocation) and did not reproduce on a third invocation, so root
cause is likely a transient Proxmox-side or network glitch.

## Why it matters

A flaky network or a Proxmox-side hiccup turns into an unbounded hang.
CI relies on the outer test-runner timeout to recover, which masks the
real failure mode and gives no diagnostic.

## Files

- `src/proxmoxsandbox/_impl/async_proxmox.py` — `upload_file_with_curl`
  inner `do_upload` function

## Fix

Set `CONNECTTIMEOUT` (TCP+TLS handshake) and a stall-based
`LOW_SPEED_LIMIT` / `LOW_SPEED_TIME` (better than a flat `TIMEOUT` for
multi-GB uploads — only fires if transfer truly stalls):

```python
curl.setopt(pycurl.CONNECTTIMEOUT, 30)        # seconds to establish connection
curl.setopt(pycurl.LOW_SPEED_LIMIT, 1024)     # bytes/sec
curl.setopt(pycurl.LOW_SPEED_TIME, 60)        # for this many seconds → abort
# Optional safety cap on total request time:
# curl.setopt(pycurl.TIMEOUT, 1200)
```

Wrap `pycurl.perform()` so a `pycurl.error` raised by the timeout
becomes a clearer Python error indicating an upload stall. Optionally
log the URL and file size to make diagnosis easier.

## Tests

Hard to unit-test without an actual stalled HTTP server. Skip — the
change is small and mechanically obvious. If desired, a test could
point pycurl at a local socket that accepts connections but never
reads, and assert the call raises within ~70s.
