# Repo guidance for coding agents

See [README.md](README.md) for project overview, configuration, and usage.
See [CONTRIBUTING.md](CONTRIBUTING.md) for dev setup, testing, linting, design notes,
and the reasoning behind the rules below.

## Before you open a PR

1. Does your change assert something about runtime behaviour — what the Proxmox API or
   the QEMU guest agent actually does? Run it against a real Proxmox host and paste the
   output. Reading upstream source is not evidence, and neither is a test that never
   reaches a host, however green.
2. Show it failing without your change and passing with it. If you can't, say so
   explicitly in the PR description.
3. If you can't run against a host, don't open the PR. Open an issue with your reasoning
   and stop.
4. Report what you actually ran, verbatim, including which tests were skipped, and how
   many times — the Windows guest agent channel is flaky enough that a single pass
   proves little. Never describe a run you didn't perform.
5. Note the tooling you used. If you ran review passes over your own change, say which
   model and what they found.

## Changelog (`CHANGELOG.md`)

New work goes under the `## Unreleased` heading. Each entry is read by a **user of this
library** — someone running `inspect eval` against the Proxmox sandbox, or provisioning
a Proxmox host for it. Write for them, not for a reviewer of the diff.

Rules:

- **Describe what the user experiences, not how it's implemented.** The mechanism (pool
  queues, SDN zone naming, QGA retry schedules, `httpx` client wiring) belongs in the
  PR, not the changelog. The user only cares about the observable change.
- **Cut the non-actionable.** If you're explaining *why* an obvious change is good, or
  *how* it works internally, cut it.
- **Keep what's actionable**: new config names and environment variables,
  breaking-change migration steps, version requirements (e.g. the minimum Proxmox
  version), the symptom a bug fix removes, public API names (exception types callers
  catch).
- **Don't churn existing entries.** When editing, make changes that are necessary
  (accuracy, completeness) and minimal. Don't swap synonyms or reorder for taste.

Detail is warranted when it's actionable. A change the user must act on to get the new
behaviour earns its length; a self-explanatory fix gets one line:

```
- Optional egress lockdown for sandbox guests (when using the bundled provisioning
  scripts): drops forwarded guest traffic and stops the SDN `dnsmasq` instances
  recursing upstream. Installed inert, so nothing changes until
  `/etc/inspect-proxmox-egress-lockdown` is created; see the README
- Fix: guest file / command-output reads work again on Proxmox < 9.2.
```

## Key Concepts

1. **Sandbox Environment**: Isolated execution environment for a single sample/task
2. **Instance Pool**: Group of Proxmox servers sharing the same VM images, managed as a queue
3. **Sample**: Single evaluation case that runs in isolation (one sample per instance at a time)
4. **Task**: Collection of samples (one eval run)
5. **SDN (Software Defined Networking)**: Virtual networks connecting VMs within a sample

## Design Patterns

### Infrastructure vs Eval Config Separation
- **Infrastructure**: Where to run (Proxmox instances, credentials)
  - Loaded from `PROXMOX_CONFIG_FILE` or single-instance env vars
  - Managed by `_proxmox_pool.py` (`QueueBasedProxmoxPool`)
- **Eval Config**: What to run (VMs, networks, which pool)
  - Defined in `sandbox.py` per challenge/eval
  - References infrastructure by `instance_pool_id`

### Pool-Based Instance Allocation
Pool logic lives in `_proxmox_pool.py`. The abstract base class `ProxmoxPoolABC`
allows alternative implementations (e.g. remote pool server).

```
task_init:       Initialize pools from PROXMOX_CONFIG_FILE / env vars
sample_init:     Acquire instance from pool (blocks if none available)
                 Create API client, ensure templates, create VMs
sample_cleanup:  Destroy VMs, release instance back to pool
task_cleanup:    Sweep orphaned resources across all instances
```

Queues provide automatic blocking when instances are exhausted. Only instances
that were successfully cleaned up are returned to the pool — dirty instances
are withheld to prevent cascading failures.

### Inspect AI Lifecycle Integration
Inspect calls these methods in order:
1. `config_deserialize(dict)` -> `ProxmoxSandboxEnvironmentConfig`
2. `task_init(task_name, config)` -> Setup pools
3. `sample_init(task_name, config, metadata)` -> Acquire instance, create VMs
4. [Sample runs...]
5. `sample_cleanup(task_name, config, environments, interrupted)` -> Destroy VMs, release instance
6. `task_cleanup(task_name, config, cleanup)` -> Final cleanup

## Gotchas

### QEMU Guest Agent Required
VMs with `is_sandbox=True` **must** have `qemu-guest-agent` installed and running. Without it, command execution fails silently or with opaque errors.

### VM ID Collisions
Proxmox assigns VM IDs (integers). The code finds available ranges via `find_proxmox_ids_start()`, but rapid creation/deletion can cause races.

### Pydantic Frozen Models
All config models use `frozen=True` — immutable after creation. You can't modify configs in place; create new instances instead.

### Instance Release on Exceptions
`sample_init` and `sample_cleanup` use try/finally to release instances back to pools. If you add new failure paths, ensure they don't leak instances.

### Windows QGA Reliability
The QEMU guest agent channel on Windows drops ~5-7% of calls. `agent_commands.py` retries any HTTP 500 from QGA endpoints (3 attempts, 3s delay).

## Comment Policy

**Write comments that explain the current state of the code, not the conversation that led to it.**

### What NOT to Comment

- References to implementation conversations or PRs
- Descriptions of changes you just made ("Added logging here")
- Vague TODOs ("Refactor this later")
- Obvious code narration ("Loop through all instances")

### What TO Comment

- **Why**, not what: explain the reason behind non-obvious choices
- **Footguns**: warn about things that will break if changed carelessly
- **Specific TODOs**: with enough context to act on without archaeology

## Performance Considerations

- **VM Template Caching**: Templates are cached per Proxmox instance after first download — first run is slow
- **Connection Pooling**: `AsyncProxmoxAPI` creates a new `httpx` client per request (room for optimization)
- **Concurrency**: Set `max_sandboxes` to number of available instances for optimal throughput; the pool's `default_concurrency()` does this automatically
