# Claude Code Context for inspect-proxmox-sandbox

This file provides essential context for AI assistants working on the `inspect-proxmox-sandbox` project.

## Project Overview

**inspect-proxmox-sandbox** is a sandbox provider for [Inspect AI](https://inspect.ai-safety-institute.org.uk/) that enables running AI evaluations in isolated Proxmox virtual machines. This is particularly useful for cybersecurity evaluations, CTF challenges, and other tasks requiring full VM isolation.

### Key Technologies
- **Proxmox VE**: Open-source virtualization platform for running VMs
- **Inspect AI**: Framework for running AI model evaluations with tool use
- **Python**: Primary implementation language (3.12+)
- **Poetry**: Dependency management and packaging
- **Pydantic**: Configuration validation and serialization

## Architecture Overview

### High-Level Flow
```
Inspect AI Evaluation
    ↓
ProxmoxSandboxEnvironment (this package)
    ↓
Proxmox Instance(s) via REST API
    ↓
Virtual Machines (VMs)
    ↓
Agent executes bash/python commands via QEMU guest agent
```

### Key Concepts

1. **Sandbox Environment**: Isolated execution environment for a single sample/task
2. **Instance Pool**: Group of Proxmox servers sharing the same VM images
3. **Sample**: Single evaluation case that runs in isolation
4. **Task**: Collection of samples (one eval run)
5. **SDN (Software Defined Networking)**: Virtual networks connecting VMs

## Project Structure

```
src/proxmoxsandbox/
├── schema.py                              # Pydantic models for configuration
├── _proxmox_sandbox_environment.py        # Main sandbox implementation
└── _impl/
    ├── async_proxmox.py                   # Proxmox API client
    ├── infra_commands.py                  # VM/network creation/deletion
    ├── agent_commands.py                  # Command execution via guest agent
    ├── qemu_commands.py                   # Low-level QEMU operations
    ├── built_in_vm.py                     # Built-in VM template management
    └── task_wrapper.py                    # Proxmox task status tracking

tests/proxmoxsandboxtest/
├── test_multi_instance_pools.py           # Multi-instance pool tests
└── test_eval.py                           # End-to-end evaluation tests
```

## Key Files and Their Purposes

### `schema.py`
Defines all configuration models:
- `ProxmoxInstanceConfig`: Connection info for a single Proxmox server
- `ProxmoxSandboxEnvironmentConfig`: Eval-specific config (VMs, networking, pool selection)
- `VmConfig`, `VmSourceConfig`: VM specifications
- `SdnConfig`, `VnetConfig`, `SubnetConfig`: Network configurations

**Design principle**: Clear separation between infrastructure (instances) and eval config (VMs/networking).

### `_proxmox_sandbox_environment.py`
Main sandbox implementation with Inspect AI lifecycle hooks:
- `task_init()`: Load instances, create pools, ensure VM templates exist
- `sample_init()`: Acquire instance, create VMs, return sandbox environments
- `sample_cleanup()`: Destroy VMs, release instance back to pool
- `task_cleanup()`: Final cleanup (optional pool destruction)

**Design principle**: Instance pools are class variables shared across all tasks. One sample per instance at a time.

### `_impl/async_proxmox.py`
HTTP client for Proxmox REST API:
- Handles authentication (ticket + CSRF token)
- Automatic re-authentication on token expiry
- Wrapper methods: `get()`, `post()`, `put()`, `delete()`
- File upload support via `pycurl` for VM templates

### `_impl/infra_commands.py`
High-level VM and network operations:
- `create_sdn_and_vms()`: Creates complete network topology and VMs
- `delete_sdn_and_vms()`: Tears down everything created
- Manages Proxmox SDN zones, VNets, subnets, and DHCP

### `_impl/agent_commands.py`
Command execution inside VMs:
- Uses QEMU guest agent (requires `qemu-guest-agent` in VM)
- `exec_command()`: Run commands and capture output
- Handles stdin/stdout/stderr, exit codes, timeouts
- Base64 encoding for binary-safe input

## Important Design Patterns

### 1. Infrastructure vs Eval Config Separation
- **Infrastructure**: Where to run (Proxmox instances, credentials)
  - Loaded from `PROXMOX_CONFIG_FILE` environment variable
  - Shared across all evaluations
- **Eval Config**: What to run (VMs, networks, which pool)
  - Defined in `sandbox.py` per challenge/eval
  - References infrastructure by `instance_pool_id`

### 2. Pool-Based Instance Allocation
```python
# Class variables (shared across all tasks)
_instance_pools: Dict[str, asyncio.Queue[ProxmoxInstanceConfig]]
_pool_locks: Dict[str, asyncio.Lock]

# Lifecycle
task_init:     Create pools, populate with instances
sample_init:   Acquire instance from pool (blocks if none available)
sample_cleanup: Release instance back to pool
```

**Key insight**: Queues provide automatic blocking when instances are exhausted, enabling safe concurrent execution.

### 3. Inspect AI Lifecycle Integration
Inspect calls these methods in order:
1. `config_deserialize(dict)` → `ProxmoxSandboxEnvironmentConfig`
2. `task_init(task_name, config)` → Setup pools
3. `sample_init(task_name, config, metadata)` → Create VMs
4. [Sample runs...]
5. `sample_cleanup(task_name, config, environments, interrupted)` → Destroy VMs
6. `task_cleanup(task_name, config, cleanup)` → Final cleanup

### 4. Multi-Instance Concurrency
- Inspect AI's `max_sandboxes` controls overall concurrency
- Instance pools control per-instance exclusivity
- One sample per Proxmox instance at a time (hard requirement)
- Multiple samples can run across different instances simultaneously

## Configuration Examples

### Legacy Single Instance (Backwards Compatible)
```bash
export PROXMOX_HOST=10.0.1.10
export PROXMOX_NODE=pve1
export PROXMOX_USER=root
export PROXMOX_PASSWORD=secret
```

### Multi-Instance with Config File (Recommended)
```bash
export PROXMOX_CONFIG_FILE=/path/to/instances.json
```

**instances.json**:
```json
{
  "instances": [
    {
      "instance_id": "proxmox-1",
      "pool_id": "ubuntu-ami-123",
      "host": "10.0.1.10",
      "port": 8006,
      "user": "root",
      "user_realm": "pam",
      "password": "secret",
      "node": "pve1",
      "verify_tls": false
    }
  ]
}
```

### Eval Config (sandbox.py)
```python
from proxmoxsandbox.schema import ProxmoxSandboxEnvironmentConfig, VmConfig, VmSourceConfig

def create_sandbox_config() -> ProxmoxSandboxEnvironmentConfig:
    return ProxmoxSandboxEnvironmentConfig(
        instance_pool_id="ubuntu-ami-123",  # References pool in PROXMOX_CONFIG_FILE
        vms_config=(
            VmConfig(
                vm_source_config=VmSourceConfig(built_in="ubuntu24.04"),
                name="agent-vm",
                ram_mb=2048,
                vcpus=2,
                is_sandbox=True  # This VM is accessible to the AI agent
            ),
        ),
        sdn_config="auto"  # Auto-create simple network
    )
```

## Common Development Tasks

### Running Tests
```bash
# All tests
poetry run pytest

# Specific test file
poetry run pytest tests/proxmoxsandboxtest/test_multi_instance_pools.py

# With coverage
poetry run pytest --cov=proxmoxsandbox
```

### Installing for Development
```bash
# Editable install (changes immediately reflected)
pip install -e .

# Or with poetry
poetry install
```

### Code Style
- Use `ruff` for linting and formatting
- Type hints are encouraged (mypy compatible)
- Docstrings for public APIs

### Comment Policy
**DO NOT add comments that describe changes you just made.**
- ❌ Bad: `# First instance - verify all fields` (after adding assertions)
- ❌ Bad: `# TODO: Refactor this later` or `# Leave this for now`
- ✅ Good: Only add comments that explain **why** code exists, not **what** it does
- ✅ Good: `# TODO: Extract this to a separate module for better testability` (specific actionable TODO)

**Comments should be permanent documentation, not change artifacts:**
- If a comment only makes sense "right now" during a refactoring, don't add it
- Readers expect code to be complete; don't document what's "already there"
- Future TODOs must be specific and actionable, not vague placeholders

## Important Gotchas

### 1. QEMU Guest Agent Required
VMs marked with `is_sandbox=True` **must** have `qemu-guest-agent` installed and running. Without it, command execution fails.

### 2. VM ID Collisions
Proxmox assigns VM IDs (integers). The code finds available ranges to avoid collisions, but rapid creation/deletion can cause race conditions. The `proxmox_ids_start` mechanism handles this.

### 3. Pydantic Frozen Models
All config models use `frozen=True`, making them immutable after creation. This is intentional for safety but means you can't modify configs in place.

### 4. Async Context
Most operations are async. Remember to `await` calls to Proxmox API and command execution methods.

### 5. Instance Release on Exceptions
Always use try/finally to release instances back to pools, even on errors. Otherwise instances can leak and become unavailable.

### 6. SDN Configuration
- `sdn_config="auto"`: Simple auto-created network (good for most cases)
- `sdn_config=None`: Use existing Proxmox VNETs (no creation/deletion)
- `sdn_config=SdnConfig(...)`: Full control over network topology

### 7. Built-in VMs
Built-in VMs (Ubuntu 24.04, Debian 13, Kali 2025.3) are downloaded and cached on first use. This can take several minutes per instance on first run.

## Testing Strategy

### Unit Tests
Mock Proxmox API calls using `unittest.mock.AsyncMock`. Focus on logic flow and error handling.

### Integration Tests
Require real Proxmox instances. Use environment variables to configure test instances. Mark with `@pytest.mark.integration` (skipped by default).

### Test Fixtures
Common mocks provided:
- `mock_proxmox_api`: Mocked AsyncProxmoxAPI
- `mock_infra_commands`: Mocked InfraCommands
- `mock_built_in_vm`: Mocked BuiltInVM

## Debugging Tips

### Enable Trace Logging
```bash
inspect eval task.py --log-level trace
```

### Check VM Creation Status
VMs may fail to start due to resource constraints. Check Proxmox web UI for VM status and logs.

### Network Connectivity Issues
If VMs can't communicate:
1. Check SDN configuration in Proxmox
2. Verify firewall rules
3. Check DHCP configuration
4. Ensure VNet zones are applied (`pvesh create /cluster/sdn`)

### Instance Pool State
Inspect class variables during debugging:
```python
print(ProxmoxSandboxEnvironment._instance_pools)  # See available instances
print(ProxmoxSandboxEnvironment._pool_locks)      # Check lock state
```

## Related Documentation

- **Inspect AI Docs**: https://inspect.ai-safety-institute.org.uk/
- **Proxmox API Docs**: https://pve.proxmox.com/pve-docs/api-viewer/
- **QEMU Guest Agent**: https://pve.proxmox.com/wiki/Qemu-guest-agent
- **Design Doc**: See `scaling_overview.md` for multi-instance architecture

## Making Changes

### Adding New VM Sources
1. Add to `VmSourceConfig.built_in` literal type in `schema.py`
2. Implement download logic in `built_in_vm.py`
3. Update tests and documentation

### Modifying Pool Behavior
Pool logic lives in `_proxmox_sandbox_environment.py`:
- `task_init`: Pool creation
- `sample_init`: Instance acquisition
- `sample_cleanup`: Instance release

### Changing Network Configuration
Network models in `schema.py`, implementation in `infra_commands.py`. Be careful: Proxmox SDN requires specific configurations and zone types.

### Adding New Config Fields
1. Add field to appropriate Pydantic model in `schema.py`
2. Update deserialization if needed
3. Use field in implementation
4. Update tests
5. Consider backwards compatibility

## Performance Considerations

- **Parallel Operations**: Use `asyncio.gather()` for operations across multiple instances
- **Connection Pooling**: AsyncProxmoxAPI creates new httpx client per request (room for optimization)
- **VM Template Caching**: Templates are cached on each Proxmox instance after first download
- **Concurrency**: Set `max_sandboxes` to number of available instances for optimal throughput

## Security Notes

- Proxmox credentials stored in environment variables or config files
- Use `verify_tls=True` in production (default: False for dev convenience)
- VMs are deleted after sample completion (unless `sandbox_cleanup=False`)
- No secrets should be committed to git (use `.env` or external config)

---

**Last Updated**: 2025-01-06
**Maintainer**: AISI (UK AI Safety Institute)
**License**: See LICENSE file
