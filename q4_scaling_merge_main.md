# Merging main (with no_context_vars) into q4_scaling

## Background

The `q4_scaling` branch adds pool-based multi-instance allocation. The `no_context_vars` PR (#55, "Rationalize task cleanup") was merged to `main` and replaces Python `ContextVar` usage with instance-level resource tracking on `QemuCommands`/`SdnCommands`. These two changes needed to be combined.

The previous attempt at merging (`q4_scaling_merge_main`) had a messy history (merge, revert, reapply) and was scrapped. A fresh branch was created from `q4_scaling` and `main` was merged in cleanly.

## What each side contributes

### From q4_scaling
- `QueueBasedProxmoxPool`: asyncio.Queue per `pool_id`, one-sample-per-instance constraint
- `instance_pool_id` on `ProxmoxSandboxEnvironmentConfig`
- Instance acquire/release lifecycle in `sample_init`/`sample_cleanup`
- `_ensure_instance_clean` pre-check (tag-based dirty instance detection)
- Two-phase VM creation: all IPAM mappings first, then all VMs (prevents DHCP race conditions)

### From main (no_context_vars)
- Instance-level tracking sets: `_tracked_vm_ids` on `QemuCommands`, `_tracked_sdn_zone_ids`/`_tracked_ipam_mappings` on `SdnCommands`
- `register_vm()`, `register_sdn_zone()`, `register_ipam_mapping()` during creation
- `deregister_resources()` during successful cleanup
- `task_cleanup()` sweeps orphaned resources by diffing tracked vs actually-deleted
- `InfraCommands` singleton pattern: `_instances` ClassVar dict keyed by `ProxmoxTarget(host, port, node)`, with `get_instance()`/`set_instance()` classmethods
- `InfraCommands.build()` factory method creates full object graph
- `IpamMapping` Pydantic model replaces raw dict-based DHCP mapping tracking
- `create_sdn_and_vms` returns 3 values: `(vm_configs_with_ids, sdn_zone_id, ipam_mappings)`

## Why the ContextVar approach was buggy

`task_cleanup` runs in its own async context, so `ContextVar.get()` returns default empty sets, making `task_cleanup` effectively a no-op. The no_context_vars refactoring fixes this by using instance-level sets on the commands objects rather than context-scoped variables.

## Key integration decision: lazy InfraCommands singleton

The main problem: q4_scaling doesn't know which Proxmox instance a sample will use until `sample_init` acquires one from the pool. But `InfraCommands` needs a specific instance's API credentials.

Solution: `InfraCommands` singleton is lazily created in `sample_init` when an instance is first acquired:

```python
target = ProxmoxTarget(host=instance.host, port=instance.port, node=instance.node)
try:
    infra_commands = InfraCommands.get_instance(target)
except LookupError:
    infra_commands = InfraCommands.build(async_proxmox_api, instance.node, config.image_storage)
    InfraCommands.set_instance(target, infra_commands)
```

This means `task_init` only creates pools (no InfraCommands), and `task_cleanup` iterates `InfraCommands._instances` to sweep all targets that were used.

## Concurrency model

The no_context_vars approach does NOT assume one-sample-per-instance. The shared tracking set + per-sample deregistration pattern works correctly with concurrent samples: each sample registers its resources on creation and deregisters them on cleanup. `task_cleanup` only sweeps what's left (orphans from interrupted samples). q4_scaling adds the one-sample-per-instance constraint on top via the pool mechanism.

## Interrupted sample handling

When `interrupted=True` in `sample_cleanup`:
- VM/SDN deletion is **skipped** (too slow/unreliable during Ctrl-C)
- The instance is still released back to the pool
- `task_cleanup` will sweep orphaned resources via InfraCommands tracking sets
- This is a change from q4_scaling's original behavior which attempted cleanup even when interrupted

## Files with merge conflicts (12 total)

### Source files
- **`_impl/qemu_commands.py`** (2 conflicts): logging style in `await_vm`, NIC config combining `str(nic.mac).upper()` with firewall support
- **`_impl/sdn_commands.py`** (7 conflicts): constructor takes `task_wrapper`, tear_down uses `IpamMapping` model, instance-level tracking sets, removed old `create_dhcp_mapping`/`cleanup` methods
- **`_impl/infra_commands.py`** (2 conflicts): imports, `create_sdn_and_vms` body combining two-phase ordering with `create_ipam_mappings` that returns and registers mappings
- **`_proxmox_sandbox_environment.py`** (7 conflicts): most complex — `task_init`, `sample_init`, `sample_cleanup`, `task_cleanup`, `cli_cleanup`, `ensure_vms` all needed reconciliation
- **`schema.py`** (4 conflicts): `OsType` literal, `image_storage` field alongside `instance_pool_id`, IPAM doc note

### Config/docs files
- **`CHANGELOG.md`**, **`pyproject.toml`** (version 0.9.5)
- **`CONTRIBUTING.md`** (Windows and debug logging sections)
- **`README.md`** (IPAM patch note)
- **`build_proxmox_auto.sh`** (`VM_MEM_MB` variable)

### Test files
- **`conftest.py`**: added `import logging` and `import os`
- **`test_proxmox_sandbox_environment_e2e.py`**: kept q4_scaling's assert format, included main's `test_task_cleanup_after_interrupted_sample`
- **`test_sdn_commands.py`**: updated to use `IpamMapping` model and `create_ipam_mapping`

## Test mock updates required

Three test files needed significant rewriting because the merged code uses `InfraCommands` classmethods instead of direct construction, and `BuiltInVM` is no longer imported in `_proxmox_sandbox_environment.py`:

### Common changes across all three files
1. **Removed `mock_built_in_vm` fixture** — `BuiltInVM` is no longer imported in `_proxmox_sandbox_environment.py` (accessed via `infra_commands.built_in_vm` instead)
2. **Replaced `InfraCommands` constructor mocking** with:
   - `patch.object(InfraCommands, 'get_instance', side_effect=LookupError)`
   - `patch.object(InfraCommands, 'build', return_value=mock_infra)`
   - `patch.object(InfraCommands, 'set_instance')`
3. **Added attributes to infra mocks**: `qemu_commands`, `task_wrapper`, `node`, `async_proxmox` (accessed in `ProxmoxSandboxEnvironment.__init__`)
4. **Made `create_sdn_and_vms` return 3 values** (added empty `ipam_mappings` tuple)
5. **Added `os_type`** to vm_config_mock
6. **Added `autouse` fixture** to clear `InfraCommands._instances` after each test (prevents cross-test contamination)
7. **Used `MagicMock()` not `AsyncMock(spec=InfraCommands)`** — the spec is too strict because `InfraCommands` is an ABC with type annotations that don't create real attributes

### test_multi_instance_pools.py
- Extracted `_make_mock_infra()` helper for consistent mock creation
- `test_cleanup_with_interrupted_flag`: now asserts `delete_sdn_and_vms` is NOT called (cleanup skipped) but instance IS released

### test_sample_init_cleanup.py
- Extracted `_make_infra_mock(**overrides)` helper with keyword overrides
- Extracted `_patch_infra(infra_mock)` returning a tuple of three context managers
- All six tests updated to use the new pattern

### test_cli_cleanup.py
- `cli_cleanup` calls `InfraCommands.build()` not the constructor, so `patch.object(InfraCommands, 'build', side_effect=...)` replaces `patch(...InfraCommands) as mock; mock.return_value = ...`
- `create_mock_infra` callback signature changed to `(async_proxmox, node, image_storage)` to match `build()` positional args

## Other notable changes in the merge

- **`ensure_vms`** signature changed from `(async_proxmox_api, config)` to `(infra_commands, config)` — avoids creating a redundant `InfraCommands` inside the method
- **`uv.lock`** took main's version
- **`sdn_commands.tear_down_sdn_zones_and_vnets`** has try/except for `httpx.HTTPStatusError` (treats "does not exist" 500 errors as already gone)
- **`create_sdn_and_vms`** in `infra_commands.py` removed old `create_dhcp_mappings` method entirely; uses `create_ipam_mappings` which returns `Tuple[IpamMapping, ...]`
