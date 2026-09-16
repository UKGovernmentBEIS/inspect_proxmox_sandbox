# Spec: Guest-controlled data trust audit of `inspect_proxmox_sandbox`

## 1. Background and motivation

`inspect_proxmox_sandbox` (audit baseline: commit `82c75c19e0a260aef718113894a557ba4c53c647`) runs adversarial AI agents as **root inside sandbox guest VMs**. All host-side interaction with a guest goes through the QEMU guest agent (QGA) via the Proxmox API. A guest root can replace `qemu-ga` with an arbitrary program on `/dev/virtio-ports/org.qemu.guest_agent.0` and return **arbitrary JSON** to any agent command.

We have already **confirmed live** that Proxmox does not enforce its declared return schemas: a shim answering `guest-exec` with `{"return": {"pid": "31337?injected=1&file="}}` was passed verbatim through `pvesh` / the HTTP API to the client. In the provider, that value reaches an f-string that builds an authenticated API URL:

- `src/proxmoxsandbox/_impl/iso_write.py:257` — `pid = exec_resp["pid"]` (no validation)
- `src/proxmoxsandbox/_impl/agent_commands.py:114` — `f"/nodes/{self.node}/qemu/{vm_id}/agent/exec-status?pid={pid}"`
- `src/proxmoxsandbox/_proxmox_sandbox_environment.py:792` — the only guard on the main exec path is `assert isinstance(exec_response_pid, int)`, which is compiled out under `python -O` / `PYTHONOPTIMIZE`.

**Your task is the generalisation:** treat *every* field of *every* response that originates in the guest as attacker-controlled, enumerate where each one flows in the provider, and determine — by code analysis plus live experiment against your Proxmox instance — what an adversarial guest can achieve through each. The PID issue above is the seed example and your harness's first regression test, not the scope.

## 2. Threat model

- **Adversary:** root inside a sandbox guest VM. Controls: the QGA channel (can run a fake agent returning arbitrary bytes/JSON, delay, or go silent), all guest filesystem content (anything `file-read` returns, including symlinks/FIFOs/devices/huge or binary files), all guest process behaviour (exit codes, signals, output files), guest DHCP/DNS client behaviour, and timing.
- **Not the adversary:** the Proxmox host OS, the Proxmox API, the eval author, the operator, the network between eval runner and Proxmox.
- **Assets to protect, in priority order:**
  1. The **eval runner process** (runs with `root@pam` Proxmox credentials): no guest-influenced control over the requests it sends (URL path/query/body/method), no crash, no hang, no resource exhaustion, no credential exposure.
  2. The **Proxmox instance state**: no guest-triggered failure of cleanup/teardown, no cross-sample or cross-instance contamination, no dirty-instance pool exhaustion.
  3. **Eval integrity**: guest-forged results should at worst look like a failed/lying command, and ideally be *detected and logged as tampering*, never crash the sample with an unattributable stack trace, and never corrupt another VM's results.
- Intra-guest privilege boundaries (e.g. `exec(user=...)`) are **out of scope** — assume guest root.

## 3. Deliverables

1. **`SINKS.md` — a complete inventory** of guest-originated data sinks. One row per (source field → consuming code location), with: file:line, the guest-controlled type/value space actually possible (vs. the type the code assumes), what the code does with it, worst plausible consequence, and a verdict: `SAFE` / `HARDEN` / `VULNERABLE`, each with a one-line justification. Completeness matters more than depth: a reviewer should be able to grep the codebase and find no QGA-response field access you haven't listed.
2. **A malicious-QGA test harness** (see §5) checked into `tests/`, plus a set of reproducible attack scenarios, each either demonstrating an issue or documenting a negative result.
3. **A findings report** with severity, PoC steps, and observed behaviour on your live Proxmox instance for everything rated `HARDEN`/`VULNERABLE`.
4. **A patch series** implementing a single validation boundary (see §6), with regression tests that run against both a real guest and the malicious harness.

## 4. Inventory: where to look (starting map, verify and extend)

Audit at minimum every consumer of these API surfaces — the list below is from our review and is believed complete for the baseline commit, but **you must re-derive it** (grep for `agent/`, `exec_status`, `out-data`, `err-data`, `exitcode`, `exited`, `signal`, `pid`, `content`, `truncated`):

**A. `guest-exec` / `guest-exec-status` responses**
- `_proxmox_sandbox_environment.py`: `exec()` — `exec_response_pid` (790–792); `exec_status["exited"]` (726); `"err-data"`/`"out-data"`/`"exitcode"` (814–826); `exec_status.get("signal")` → `128 + signal_num` arithmetic (843–845). Note guest-controlled types here: what happens if `exitcode` is a string, a float, absent, or 2⁶⁴? If `signal` is `"x"` (TypeError on `128 + "x"`)? If `exited` is `"1"` vs `1` (the `!= 1` comparison)?
- `_impl/iso_write.py`: `pid` (257), `status.get("exited")` (268), `exitcode`/`err-data`/`out-data` (274–277).
- `_impl/built_in_vm.py`: `res["pid"]` (547), `exec_status["exited"]`, `exec_status["out-data"].strip().endswith(...)` (549–557) — lower risk (runs during template bake before the agent is adversarial) but inventory it anyway.
- `_impl/agent_commands.py`: `get_agent_exec_status` error-message matching `_is_pid_gone` (41–50) and `_is_transient_qga_error` (66–85) — these match on **error text that the guest agent composes**; determine whether a guest can forge messages ("does not exist", "no such file", "failed to open file") to steer host retry/fallback logic, e.g. making a running process report as finished or converting a real error into an infinite retry loop (25 retries × 20 s per call site).
- `pid` gone → `{"exited": 1}` synthesis (120–127): can a guest use the single-shot exec-status semantics plus forged errors to make the provider read stale on-disk results from a *previous* command?

**B. `guest-file-read` responses**
- `_impl/async_proxmox.py::read_file_capped` (230–288): `data.get("content")` — guest controls size (is `count` actually enforced server-side, or only requested? test with a shim returning more than `count`), base64 validity (`_B64_SEGMENT` regex over arbitrary input — check CPU/memory on pathological inputs), and `data.get("truncated")` (guest can lie both ways: suppress truncation → silently clipped file contents delivered to scorer; assert truncation → spurious `OutputLimitExceededError`).
- Legacy path `_decode_legacy_file_read` (290–311): `content.encode("iso-8859-1")` on guest text.
- `_read_return_code` (`_proxmox_sandbox_environment.py` 895–907): `int(...)` on guest bytes — ValueError propagation, negative/huge ints, and interaction with the `returncode == 124 → TimeoutError` and `126 → PermissionError` mapping (875–886): a guest can forge these to raise specific exception types in the eval runner; check whether any Inspect-side handler treats those exceptions in an exploitable way.
- Filesystem-level tricks the shim can't express but the guest can: `read_file`/exec-output paths pointing at FIFOs (does the read block forever → sample hang past all timeouts?), `/dev/zero` (16 MiB cap enforced where?), symlinked targets, files that change between size-check and read.

**C. Everything else that transits the guest**
- `guest-ping` (used as liveness in `await_vm` and before every file-read): a selectively-responding agent — can it hold a VM in "starting" forever, or wedge `destroy_vm`'s stop-wait (`qemu_commands.py` 129–168) and thereby wedge cleanup and the pool?
- Timeout-less exec: `exec(timeout=None)` → `tenacity.stop_never` (`_proxmox_sandbox_environment.py` 709–711) with a guest that answers exec-status `{"exited": 0}` forever. Quantify: can a guest indefinitely hang a sample/task?
- QGA transport itself: shim returning non-JSON, oversized single lines, deeply nested JSON, `\xff` mid-stream, response floods — observe effects on the **host's** QEMU process and pvedaemon (memory, CPU, error propagation back to the provider). This is a bonus objective; do not fuzz QEMU exhaustively, just characterise the failure mode of malformed framing.
- Guest-influenced values landing in **Proxmox-side state later read back by the provider**: DHCP hostname → lease files/IPAM → `read_all_ipam_mappings` → `PveIpamStatus(**item)` / `MacAddress(...)` parsing (`sdn_commands.py` 71–87, 462–464) — can guest-chosen strings make cleanup-time parsing raise and abort teardown?
- Log injection: guest strings (`out-data`, error texts) flow into `logger.warning`/`trace_action` f-strings throughout — check for newline/ANSI injection into operator logs and Inspect transcripts.

## 5. Test harness requirements

Build `tests/malicious_qga/`:

- **In-guest shim**: a configurable fake agent (extend our PoC: reads the virtio port, answers `guest-sync`/`guest-sync-delimited`/`guest-ping`, and serves per-command canned or scripted responses loaded from a scenario file). Must support: wrong types, missing keys, extra keys, huge values, malformed JSON, delayed/partial/absent responses, and per-request sequencing (answer correctly N times, then lie — needed to attack the write-script-then-exec and poll loops mid-flight).
- **Deployment**: installed into a sandbox VM via a normal provider `write_file`/`exec` before swapping (`systemd-run` outside the agent cgroup, auto-restore on timeout — reuse our pattern), so scenarios are scriptable end-to-end from pytest against your fresh Proxmox instance (`req_proxmox` marker per `pyproject.toml`).
- **Runner-side assertions**: each scenario drives the real provider (`ProxmoxSandboxEnvironment.exec/read_file/write_file`, plus `sample_cleanup`) and asserts on (a) exceptions raised and their types, (b) every HTTP request the provider actually sent — wrap/mock-record `AsyncProxmoxAPI.request` and the httpx client to capture method/URL/body, so URL-injection is detected mechanically, not by eyeballing pveproxy logs, (c) wall-clock bounds (hang detection), (d) post-scenario instance cleanliness (no leftover VMs/zones/IPAM entries/ISOs — reuse `cleanup_no_id` discovery logic as the oracle).
- Run the full suite twice: normal interpreter and `PYTHONOPTIMIZE=1`, to catch every assert-based guard, not just line 792.

## 6. Required fix shape (for the patch series)

- **One validation boundary**: all QGA response parsing goes through a single module (suggest `_impl/qga_responses.py`) with typed validators (pydantic models are already a dependency) for exec, exec-status, and file-read returns: `pid` strictly a positive non-bool int; `exited` ∈ {0,1}; `exitcode`/`signal` bounded ints; `out-data`/`err-data`/`content` strings with explicit size ceilings; unknown/missing fields → a dedicated `GuestAgentTamperError` (or similar) that is **logged at WARNING with vm_id and the offending raw value (truncated), and fails the sample attributably** rather than crashing with a bare KeyError/TypeError/AssertionError.
- No `assert` for any guest-derived value; no guest-derived value ever reaches an f-string that builds a URL, shell fragment, or file path without passing the validator first.
- Error-text matching (`_is_pid_gone`, "no such file", etc.) either removed in favour of structured discrimination where possible, or explicitly documented as guest-influencable with the resulting behaviour bounded (finite retries, safe fallback).
- Behaviour-preserving for honest guests: the existing live-Proxmox test suite must still pass.

## 7. Constraints, safety, acceptance

- Use only your own fresh Proxmox instance; snapshot/restore between destructive scenarios. Every scenario must leave the instance clean or its teardown documented.
- Time-box QEMU/transport fuzzing (§4C) to characterisation, not vulnerability research; file anything memory-safety-shaped upstream rather than exploiting it.
- **Done means:** (1) `SINKS.md` covers every guest-data access in the codebase with a verdict; (2) every `VULNERABLE`/`HARDEN` row has a harness scenario reproducing it and a passing regression test after the patch; (3) the seed finding (string pid → URL query injection, both `iso_write` and `-O` exec paths) is demonstrated by the harness pre-patch and blocked post-patch; (4) the suite passes under both normal and `PYTHONOPTIMIZE=1` interpreters; (5) a summary table maps each finding to threat-model asset 1/2/3 from §2.
