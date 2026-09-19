# Guest-controlled data trust audit — sink inventory

Baseline: `82c75c1`. The sandbox runs an adversarial agent as **root inside the
guest**, and every host↔guest interaction goes through the QEMU guest agent (QGA)
over the Proxmox HTTP API. So **every field of every QGA response is
attacker-controlled**. This table inventories where guest-originated response data
flows in the provider, what an adversarial guest can put there (after Proxmox's own
transforms), the consequence if it is used unvalidated, and the disposition.

Assets, in priority order:
1. **The eval runner process** (holds `root@pam`): must not be crashed, hung, or
   made to issue guest-controlled requests.
2. **The Proxmox instance**: no guest-triggered cleanup failure, worker/pool
   exhaustion, or unbounded hang.
3. **Eval integrity**: a lying guest should look like a lying/failed command, never
   an unattributable provider crash.

Dispositions:
- **validate** — the value is constrained at a single trust boundary
  (`_impl/qga_responses.py`); a bad value raises `GuestAgentTamperError` (logged
  WARNING with vm_id + truncated raw), never `assert` (stripped by `python -O`) and
  never a bare `int()`/`TypeError`/`KeyError`.
- **bound** — the value can only cause finite, attributable resource use.
- **accepted** — the value is opaque payload or an unavoidable forged result; a
  root guest lying about its own output/exit is in-scope for the eval, not a
  provider compromise.

## Reachability through Proxmox

Proxmox (PVE 9.2, `PVE::API2::Qemu::Agent`) transforms QGA responses before the
provider sees them:
- **exec-status**: base64-decodes `out-data`/`err-data` to strings and maps JSON
  booleans to `1`/`0`, but **`pid`, `exitcode`, `signal` pass through with the
  guest's raw JSON type** — a string/float/object survives.
- **exec**: returns `{pid}` verbatim.
- **file-read** (`decode=0`): the host loops guest-file-read, concatenates the
  guest's base64 `buf-b64` into `content`, and sets `truncated` from the guest's
  `eof`. So `content` is always valid base64→bytes (no type confusion), but its
  bytes, its length, and `truncated` are guest-chosen.

This is why type-confusion on `pid`/`exitcode`/`signal` is reachable but
type-confusion on file-read `content` is not — confirmed live by the scenarios in
`tests/malicious_qga/`.

## Sinks

| # | Sink (file:line) | Guest-controlled value (post-PVE) | What the code does | Consequence if unvalidated | Disposition |
|---|---|---|---|---|---|
| 1 | `_proxmox_sandbox_environment.py:805`, `_impl/iso_write.py:259` | exec `pid`: any JSON type | interpolated into `exec-status?pid={pid}` URL (`agent_commands.py:133`) | **Seed finding.** String `pid` injects into the API query string; the old `assert isinstance(pid,int)` is stripped by `python -O`, reopening it | **validate** — `validate_exec_pid` → int |
| 2 | `_proxmox_sandbox_environment.py:737` | exec-status `exited`: any type | gates the wait loop (`!= 1`) | non-int breaks the comparison; `exited` never 1 → wait forever | **validate** (type) + **bound** (deadline) |
| 3 | `_proxmox_sandbox_environment.py:833` | exec-status `exitcode`: any type | `returncode = exec_status["exitcode"]` in the wrapper-error branch → `ExecResult.returncode` | non-int returncode flows to `== 124`/`== 0`; silently wrong result, or a non-comparable object handed to the caller | **validate** |
| 4 | `_proxmox_sandbox_environment.py:856-858` | exec-status `signal`: any type | `returncode = 128 + signal_num` in the killed-wrapper path | string/object signal → `TypeError` = unattributable crash | **validate** |
| 5 | `_proxmox_sandbox_environment.py:924` | returncode file bytes | `parse_return_code(raw)` (was `int(raw)`) | non-integer file → `ValueError` = unattributable crash | **validate** |
| 6 | `_proxmox_sandbox_environment.py:831-832` | exec-status `out-data`/`err-data` (strings) | become `stdout`/`stderr`; `err-data` *presence* selects the wrapper-error branch | forged/lying output; branch selection is benign (only changes where stdout/stderr are sourced) | **accepted** |
| 7 | `_impl/async_proxmox.py:281-288` | file-read `content` bytes | base64→bytes, returned to caller / decoded to stdout | opaque payload; a root guest lying about a file's content is a forged result | **accepted** |
| 8 | `_impl/async_proxmox.py:288,310` | file-read `truncated` | raises `OutputLimitExceededError` when set | forged "output too big"; still an attributable, typed exception | **accepted** |
| 9 | PVE-side file-read loop (guest `count=0`/`eof=0`) | guest never signals EOF | PVE loops host-side; the provider's GET hits its 60s httpx read timeout → treated as transient → retried | finite worker/time use, no infinite hang, bounded by the retry envelope | **bound** |
| 10 | `_impl/agent_commands.py` retry envelope | any transient-looking 5xx/timeout | `_retry_on_qga_error` retries up to 25× | a guest returning transient-looking errors on every exec-status call could stretch a short exec into minutes of inner retries, ignoring the caller timeout | **bound** — retry loop now takes a `deadline` from the caller timeout |
| 11 | exec/exec-status QMP framing (QEMU/pvedaemon) | malformed/oversized JSON frames | parsed by PVE Perl before the provider | out of the provider's trust boundary; a memory-safety issue there is an upstream QEMU/PVE bug | **accepted** (characterise + file upstream, don't exploit) |
| 12 | `_impl/built_in_vm.py:546-557` | cloud-init template values baked at build | provider-authored, not guest-sourced at runtime | not guest-controlled | **accepted** |
| 13 | `_impl/async_proxmox.py:290-311` `_decode_legacy_file_read` | PVE < 9.2 decode=1 bytes | Latin-1 round-trip | same opaque-payload class as row 7 | **accepted** |
| 14 | log/exception sinks for untrusted values | any raw value | `GuestAgentTamperError` logs a `repr()`-escaped, length-capped copy | control-byte/ANSI log injection via the value we log | **accepted** (mitigated: `repr()` escapes control bytes; capped at 120 chars) |
| 15 | intra-guest privilege boundaries | anything the guest root does to itself | N/A — inside the sandbox | in-scope for the agent, out-of-scope for host isolation | **accepted** |

## Notes on the bounds

**Row 10 (retry envelope).** The exec wait loop (`wait_for_exec`) already had a
tenacity `stop_after_delay(timeout + grace)`, but each `get_agent_exec_status` call
inside it retried transient errors up to ~460s on its own, so a guest that returned
a transient-looking 5xx on every status poll could ignore the caller's timeout
entirely. `_retry_on_qga_error` now accepts an absolute `deadline`; `exec()` and
`iso_write` pass one derived from the caller timeout, so the inner loop is
subordinate to it. With no caller timeout (`timeout=None`) the behaviour is
unchanged and unbounded by design — indistinguishable from an honest command that
never terminates.

**Row 9 (file-read EOF starvation).** Not exercised live: forcing PVE's host-side
loop to spin risks a multi-minute hang on shared Proxmox hardware, so it is
characterised from the PVE source only. The ceiling is the per-request 60s httpx
read timeout times the retry cap; it is finite, not infinite.

## Regression coverage

- `tests/malicious_qga/test_qga_responses_unit.py` — the boundary itself
  (validate/reject matrix), runs without Proxmox and under `python -O`.
- `tests/malicious_qga/test_malicious_qga_e2e.py` — reachability: a real guest
  swaps in a canned malicious agent (`_shim.py`) and the provider is driven through
  its normal entry points. Rows 1, 3, 4, 5 fail pre-patch (URL injection /
  AssertionError / TypeError / ValueError) and are caught as `GuestAgentTamperError`
  post-patch.
