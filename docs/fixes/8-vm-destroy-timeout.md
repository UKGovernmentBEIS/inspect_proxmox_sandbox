# Investigate `vm_deleted()` 30s budget on slower Proxmox hosts

Investigation, not yet a confirmed fix. The 30s tenacity budget on the
post-DELETE existence check in `qemu_commands.destroy_vm` is at least
2× too short on AWS Nitro nested-virtualization ("nitrovirt") Proxmox
hosts, and may be an outlier even on bare metal.

## Symptom

Running the unit-level test suite against a fresh nitrovirt Proxmox
instance, four tests failed in their teardown phase:

- `test_built_in_vm.py::test_ubuntu`
- `test_built_in_vm.py::test_debian`
- `test_built_in_vm.py::test_kali`
- `test_qemu_commands.py::test_empty_nic_from_built_in`

All hit the same trace:

```
src/proxmoxsandbox/_impl/built_in_vm.py:282
  in clear_builtins → inner_clear_builtins
src/proxmoxsandbox/_impl/qemu_commands.py:167 → vm_deleted()
tenacity.RetryError: <attempt #10; slept for 32.01;
                      last result: failed (ValueError vm 100 still exists)>
```

Same Proxmox host happily ran the rest of the unit tests
(`test_storage_commands`, `test_sdn_commands`, the non-built-in
`test_qemu_commands`, `test_sample_init_cleanup`) and the first
OPNsense end-to-end test (`test_opnsense_domain_filtering`) without
issue, so the host is not broken — only this one budget is too tight.

## Why the 30s is likely the wrong number

`destroy_vm` in `src/proxmoxsandbox/_impl/qemu_commands.py` has three
tenacity retry budgets:

| Step | Budget |
|------|--------|
| `is_in_status` (e.g. await running) | 1200 s |
| `is_not_running` (post-stop) | 300 s |
| **`vm_deleted` (post-DELETE)** | **30 s** |

The DELETE itself returns a UPID and the actual disk teardown happens
asynchronously in a Proxmox node task. For the failing tests the VMs
are template VMs imported from OVAs (Ubuntu 24.04, Debian 13, Kali
2025.4), with full-size root disks on `local-lvm`. Removing those
volumes is wall-clock-bound by the storage backend, and on nitrovirt
the emulated storage is slower than bare metal.

The 30s budget is also asymmetric with the other two budgets in the
same function — `is_not_running` already gets 300s, and the work
performed under `vm_deleted` is at least as I/O-heavy as that.

## Files

- `src/proxmoxsandbox/_impl/qemu_commands.py` — `destroy_vm`,
  `vm_deleted` inner function (lines 153-167 at the time of writing)

## Investigation steps

1. Reproduce on the nitrovirt instance with debug logging enabled
   (`uv run pytest --log-cli-level=DEBUG`) to confirm where in
   `vm_deleted` the time is spent. Look at the trailing
   `/status/current` responses just before tenacity gives up — is the
   VM still `stopped` (storage cleanup running) or genuinely gone but
   the API still listing it?
2. Cross-check on a bare-metal Proxmox host. If destroy completes in
   < 5 s there, the gap is purely nitrovirt. If it takes > 30 s on
   bare metal too, the budget was always wrong.
3. Sanity-check that `do_action_and_wait_for_tasks` upstream is
   actually waiting for the qmdestroy UPID — if it is, the
   `/status/current` poll afterwards should already be a no-op and
   the budget is irrelevant. If it isn't, the design has two
   independent timeouts racing.

## Likely fix

Lift `vm_deleted` to a budget consistent with `is_not_running`:

```python
@tenacity.retry(
    wait=tenacity.wait_exponential(min=0.1, exp_base=1.3),
    stop=tenacity.stop_after_delay(300),
)
async def vm_deleted() -> None:
    ...
```

If investigation step 3 shows the upstream task-waiter already covers
the wait, this poll can probably be deleted entirely.

## Tests

Difficult to unit-test a wall-clock budget. Acceptable to skip: the
change is mechanical. If desired, a regression assertion that
`destroy_vm` succeeds against a real Proxmox host can be added by
reusing the existing `test_qemu_commands.py` fixtures — but they
already exercise this path, so the fix is implicitly covered.
