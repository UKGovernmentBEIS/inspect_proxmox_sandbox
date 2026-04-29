# Investigate `test_opnsense_escape` indefinite hang

Investigation, not yet a confirmed fix. `test_opnsense_escape` hung
silently for at least 70 minutes against a freshly built nitrovirt
Proxmox host with no progress and no error. The hang was only caught
by a manual `py-spy` and host-side inspection; from pytest's vantage
point the test was simply "still running".

## Symptom

Sequence on the new nitrovirt Proxmox instance:

1. `test_opnsense_eval.py::test_opnsense_domain_filtering` PASSED
   (this builds the OPNsense base template, so the second test only
   has to clone from it).
2. `test_opnsense_escape.py::test_opnsense_escape` started, brought
   up `opnsense-lan` (VM 103) and `agent` (VM 104), and then never
   issued another Proxmox API request for the next ~60 minutes.
3. State at minute ~70:
   - `pgrep -af pytest` → still alive
   - `ss -tnp` for the pytest pid → 0 TCP connections
   - `pgrep -P <pytest>` → 0 children
   - `py-spy dump` → main thread idle in `select()` inside
     `asyncio._run_once`, called from `eval()` at
     `tests/proxmoxsandboxtest/test_opnsense_escape.py:104`. No
     `proxmoxsandbox` frames visible.
   - Proxmox `/cluster/tasks` → most recent task was `qmstart` for
     VM 104, finished at 11:33:56. Test was killed at ~12:35.
   - QGA on VM 103 (OPNsense): `"QEMU guest agent is not running"`
   - QGA on VM 104 (agent): responding fine

The test was killed manually. It would otherwise have run forever
(or until the wider session timed out).

## Why this matters

A test that hangs silently is worse than a test that fails. CI
inherits the outer test-runner timeout (`pytest` has none configured;
`pytest-timeout` is not installed) and an interactive run gives no
diagnostic until the user gets suspicious. Compare with the upload
hang already documented in `docs/fixes/7-pycurl-timeout.md` — same
failure mode, different code path.

The longest tenacity budget anywhere in the codebase is
`VM_TIMEOUT = 1200` (20 min). 70 min of no progress means either
several long retries fired in succession, or an `await` is genuinely
unbounded. Either way the user-facing behaviour is "hangs forever".

## Hypotheses, in order of likelihood

1. **OPNsense's QGA never came up on this run, and a tenacity loop
   somewhere is polling it without an effective stop.** The base
   template was built by the *first* test (when OPNsense came up
   fine) and has the QGA-installing rc.d script baked in. On this
   second test we cloned from the template and started the VM, but
   QGA never reported running. If the orchestration code waits for
   QGA via a polling loop with `stop_after_delay(VM_TIMEOUT)` per
   attempt and a separate retry around the whole thing, you can
   get 60 + min of waiting without any single tenacity decorator
   misbehaving.

2. **Nitrovirt-specific OPNsense boot bug.** The first test passed
   on the same host, so the template build itself is fine — but
   *cloning and booting* OPNsense on nitrovirt may differ from
   bare metal in a way that prevents the QGA channel from coming
   up (e.g. virtio-serial device ordering, virtual NIC type, or
   the rc.d script silently failing on first boot when no IP is
   configured yet). Worth checking the OPNsense console log on a
   freshly cloned VM 103.

3. **Stale state from the first test bleeding into the second.**
   `_opnsense_subnets_by_vnet` is keyed by VNet alias; if `task_init`
   from test #1 left a vnet/subnet/IPAM record that test #2 then
   races against, the second bring-up could deadlock waiting for a
   resource that's already half-owned. Less likely given the first
   test passed cleanly, but worth ruling out by running test #2 on
   its own.

## Files

- `tests/proxmoxsandboxtest/test_opnsense_escape.py` — the test
- `src/proxmoxsandbox/_impl/opnsense.py` — OPNsense bring-up
- `src/proxmoxsandbox/_impl/qemu_commands.py` — `wait_for_status`
  with QGA reachability poll (the 300s budget there only covers a
  *single* invocation; if the caller wraps it in another retry,
  total wall time is unbounded)
- `src/proxmoxsandbox/_impl/infra_commands.py` — calls into both
- `pyproject.toml` — needs `pytest-timeout` if we want a
  belt-and-braces outer bound

## Investigation steps

1. **Reproduce in isolation.** Run only the second test:
   ```
   uv run pytest tests/proxmoxsandboxtest/test_opnsense_escape.py \
       --log-cli-level=DEBUG -s
   ```
   With `-s` and DEBUG logging, the inspect_ai display + the
   proxmoxsandbox tracers should print exactly which step is
   stalling. If it reproduces, capture the last few log lines.
2. **Check OPNsense console.** While the test is hung, attach to
   VM 103's serial console via the Proxmox UI / `qm terminal 103`
   and confirm whether OPNsense booted fully, and whether the
   `qemu-guest-agent` package is installed and the service is
   running. The injected rc.d script is responsible for installing
   QGA on first boot (`src/proxmoxsandbox/scripts/experimental/
   opnsense_injector/`), so if QGA isn't there, the script silently
   failed.
3. **Search for unbounded retry composition.** `grep -rn
   "tenacity.retry" src/proxmoxsandbox` and look for any decorator
   that wraps a call which itself retries. Two nested retries
   multiply their budgets.
4. **Run on a bare-metal Proxmox host.** If the test passes there,
   the bug is nitrovirt-specific and probably hypothesis #2. If it
   hangs there too, hypothesis #1 or #3.

## Likely fix

Two layers, both worth doing:

### Outer test-level timeout

Add `pytest-timeout` and a per-test cap so a hang fails loudly:

```toml
# pyproject.toml
[tool.pytest.ini_options]
timeout = 1800            # 30 min hard cap per test
timeout_method = "thread"  # so asyncio hangs are interrupted
```

(Choose a value that comfortably covers a healthy first-time
template build but not a 70-min hang.)

### Inner orchestration timeout

Once root cause is identified (likely a QGA wait path), give that
specific wait an explicit `stop_after_delay` matched to expected
boot time + slack, and raise a clear error pointing at the most
likely cause ("OPNsense QGA did not come up within Xs — check
template build / rc.d injector").

## Tests

The outer `pytest-timeout` change is self-testing: it either fires
or it doesn't.

The inner timeout, once written, can be unit-tested by mocking the
`/agent/ping` endpoint to always 500 and asserting the orchestration
raises a clearly-named exception within a bounded wall-clock
window.
