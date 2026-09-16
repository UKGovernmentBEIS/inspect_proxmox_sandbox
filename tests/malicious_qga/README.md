# Malicious-QGA test harness

Exercises how the provider treats **guest-controlled QEMU-guest-agent responses**.
The sandbox runs an adversarial agent as root inside the guest, so every QGA
response field is attacker-controlled. See `../../SINKS.md` for the sink inventory
and dispositions these tests back.

## Layout

- `_shim.py` — a stdlib-only fake guest agent. Deployed into a real guest, it
  replaces `qemu-ga` on the virtio-serial port and serves **canned** replies from a
  scenario JSON (one reply per command type, plus per-path file-read replies). It
  executes nothing. It logs every request to `/root/shim.log` and serves that log
  back through itself so a test can see exactly what it saw and replied.
- `harness.py` — runner-side helpers: `install_malicious_agent` (deploy + swap via
  `systemd-run`, outside the agent's cgroup, masking the real agent so it can't
  race the shim for the port), `scenario(...)` builders, and `RequestRecorder`.
- `conftest.py` — a self-contained `proxmox_sandbox_environment` fixture; each test
  gets a fresh VM so one scenario's destructive swap can't leak into the next.
- `test_qga_responses_unit.py`, `test_retry_bound_unit.py` — the trust boundary and
  the retry deadline, no Proxmox needed (also pass under `python -O`).
- `test_malicious_qga_e2e.py` — reachability against a real instance
  (`req_proxmox`): string pid, string exitcode, string signal, non-integer
  returncode file.
- `test_smoke_swap.py` — proves the swap works end-to-end with an honest scenario.

## Running

Unit only (no Proxmox):

    uv run pytest tests/malicious_qga/test_qga_responses_unit.py \
                  tests/malicious_qga/test_retry_bound_unit.py

Full (needs single-instance env vars: `PROXMOX_HOST`, `PROXMOX_PASSWORD`, ...):

    uv run pytest tests/malicious_qga/

The e2e tests each provision a fresh VM and take a few minutes.
