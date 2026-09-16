# Spec: guest-controlled data audit of `inspect_proxmox_sandbox`

Baseline `82c75c1`.

## Threat model

An adversarial agent runs as root in a sandbox guest. It can replace `qemu-ga` on
`/dev/virtio-ports/org.qemu.guest_agent.0` and return arbitrary JSON, delay, or go silent; it
owns all guest filesystem content and process behaviour. Confirmed live that Proxmox does not
enforce its declared return schemas — a shim answering `guest-exec` with
`{"return": {"pid": "31337?injected=1&file="}}` reached the client verbatim through `pvesh`.

The Proxmox host, its API, and the operator are trusted. The asset is the **eval runner**, which
holds `root@pam` credentials: no guest control over the requests it sends, no unbounded hang, no
crash that isn't attributable to guest tampering.

## Task

Treat every field of every response that originates in the guest as attacker-controlled.
Enumerate where each one flows, and for each decide what an adversarial guest can achieve. The
two findings below are worked examples showing the expected depth — **they are not the scope.**

Derive the sink list yourself rather than trusting the examples: grep for `agent/`,
`exec_status`, `out-data`, `err-data`, `exitcode`, `exited`, `signal`, `pid`, `content`,
`truncated`, and for host-side state that guest behaviour can influence and the provider later
reads back (DHCP/DNS, lease files, IPAM). For each field ask what the guest can actually put
there versus what the code assumes — wrong type, missing, absent key, huge value, negative,
non-UTF-8, forged error text — and follow it to its consumer.

## Worked example 1: a value reaching a URL

`pid` flows from `guest-exec` into an authenticated API URL: `_impl/iso_write.py:257` (no check)
→ `_impl/agent_commands.py:114` (`f"...exec-status?pid={pid}"`).
`_proxmox_sandbox_environment.py:792`'s `assert isinstance(..., int)` is removed by `-O` and
accepts `True` anyway. Ceiling: the path prefix is fixed, so a guest gets extra query params on
exec-status, not arbitrary API calls — fix it because it's cheap, not because it's an escape.

Nearby, the same absence of validation turns type confusion into unattributable crashes:
`exitcode` string/absent (`:814-826`), `signal` `"x"` into `128 + signal_num` (`:843-845`),
`exited` `"1"` against `!= 1` (`:726`); and on file-read (`_impl/async_proxmox.py:230-288`)
non-base64 `content`, a body exceeding the requested `count`, `truncated` lying either way
(silently clipped output, or a spurious `OutputLimitExceededError`).

Disposition: **validate**.

## Worked example 2: a wait with no bound

`_proxmox_sandbox_environment.py:711` uses `tenacity.stop_never` when `timeout is None` — an
agent answering `{"exited": 0}` forever hangs the sample with no ceiling. Separately, the 25 ×
20s retry envelope in `_impl/agent_commands.py` isn't subordinate to the caller's timeout
(`stop_after_delay` only stops between attempts), so `exec(timeout=5)` can run for minutes
against an agent that stalls each request to httpx's 60s read timeout — once per post-exit read.

Disposition: **bound**. Note this class can't be validated away, and it bites honest wedged
guests too.

## Worked example 3: a negative settled without an experiment

`destroy_vm` (`_impl/qemu_commands.py:129-168`) issues a QEMU-level `status/stop` and polls the
*host's* `status/current` under a 300s bound — no agent involvement, so a hostile agent has no
input to teardown. Every other wait in the tree is likewise bounded (`await_vm` 1200s and its
ping loop 300s, `iso_write` 120s, `_read_return_code` 2s). Reading the code was enough; don't
spend a scenario on a conclusion you can reach this way.

Disposition: **accepted**, no change.

## Method and deliverable

Each guest-originated value gets exactly one disposition:

- **validate** — response-shaped problems go through one boundary: `_impl/qga_responses.py`,
  pydantic models for the exec, exec-status and file-read returns (`pid` positive non-bool int;
  `exited` ∈ {0,1}; `exitcode`/`signal` bounded; `out-data`/`err-data`/`content` size-capped).
  Anything else raises `GuestAgentTamperError`, logged WARNING with `vm_id` and the truncated
  value. No `assert` on guest data; no guest value in a URL, shell fragment or path before
  validation.
- **bound** — wait-shaped problems get a ceiling and a deadline, preserving the Windows
  flakiness tolerance `_QGA_MAX_RETRIES` exists for.
- **accepted** — with the one-line reason. A negative result is a result; record it and move on.

Deliverable is a table of (file:line, what the guest can actually put there, consequence,
disposition) plus the patches. Keep it to a table — the pydantic models already document the
validated fields, so the table's real job is the bounded and accepted rows, which have nowhere
else to live. No severity ratings, no separate findings report; the PR body carries the prose.

Pre-seeded accepted rows, so they aren't re-derived: forged results (guest root owns
`script.stdout`/`stderr`/`returncode`, so it can report anything by writing them — the
requirement is attributable failure, not tamper detection); QEMU/pvedaemon framing fuzz
(upstream's threat model); `_impl/built_in_vm.py:547` (template bake, pre-adversarial);
`_decode_legacy_file_read` (PVE < 9.2, already warns); log/ANSI injection into operator logs;
intra-guest privilege boundaries (`exec(user=...)`, assume guest root). Reopen any of these if
the audit turns up a consequence worse than assumed.

## Validation

E2E through the provider's own surface (`exec`/`read_file`/`write_file`), `req_proxmox` marker.
Canned stateless in-guest shim: owns the virtio port, answers sync/ping honestly, serves one
hardcoded reply per command baked in at install time. It executes nothing — `exec()`'s post-exit
reads of `script.stdout`/`stderr`/`returncode` come back through `file-read` and are canned too.
Fresh VM per scenario; no sequencing, no auto-restore, no cleanup assertions.

Every finding rated validate or bound needs a scenario. From the worked examples that is at
least: string `pid` containing `?`/`&`; exec-status type confusion; file-read `content`/`count`/
`truncated` lies; `timeout=None` with an agent that never reports exit; `timeout=5` with an agent
that stalls every request. Expect to add scenarios for whatever else the audit finds.

Run each scenario **pre-patch and confirm it fails**, else a later pass proves nothing (the `pid`
scenario needs `-O` to clear the assert at `:792`; the `iso_write` path doesn't). Then the
existing `req_proxmox` suite must still pass against an honest guest.

## Done

Every guest-originated value the grep surfaces has a disposition; every validate/bound row has a
scenario that failed pre-patch and passes post-patch; honest-guest behaviour is unchanged.
