"""Tests that each sample durably records the pool instance it actually ran on.

The frozen ProxmoxSandboxEnvironmentConfig defaults host/port/node from
process env vars, and that config is what Inspect serialises into each
sample's `sandbox` field — so with a pool, every sample used to be
attributed to the single PROXMOX_HOST env default regardless of which
instance it acquired. sample_init now stamps the acquired instance into the
Inspect sample store (`proxmox:*` keys), which lands in the .eval log.
"""

import asyncio
import json
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from inspect_ai.util._store import Store, init_subtask_store

from proxmoxsandbox._impl.infra_commands import InfraCommands
from proxmoxsandbox._proxmox_sandbox_environment import ProxmoxSandboxEnvironment
from proxmoxsandbox.schema import ProxmoxSandboxEnvironmentConfig

# Sentinel env default: TEST-NET-3, provably not a real pool instance.
ENV_SENTINEL_HOST = "203.0.113.255"


def _make_mock_infra():
    infra = MagicMock()
    vm_config_mock = MagicMock()
    vm_config_mock.is_sandbox = True
    vm_config_mock.name = None
    vm_config_mock.os_type = None
    infra.create_sdn_and_vms = AsyncMock(
        return_value=(
            [(101, vm_config_mock)],
            "zone1",
            (),
        )
    )
    infra.delete_sdn_and_vms = AsyncMock()
    infra.deregister_resources = MagicMock()
    infra.task_cleanup = AsyncMock()
    infra.find_proxmox_ids_start = AsyncMock(return_value="tst100")
    infra.cleanup_no_id = AsyncMock()
    infra.sdn_commands = MagicMock()
    infra.sdn_commands.read_all_vnets = AsyncMock(return_value=[])
    infra.qemu_commands = MagicMock()
    infra.task_wrapper = MagicMock()
    infra.built_in_vm = AsyncMock()
    infra.built_in_vm.ensure_exists = AsyncMock()
    infra.async_proxmox = AsyncMock()
    infra.node = "pve1"
    return infra


@pytest.fixture
def mock_proxmox_api():
    with patch("proxmoxsandbox._proxmox_sandbox_environment.AsyncProxmoxAPI") as mock:
        api_instance = AsyncMock()
        api_instance.get.return_value = {"version": "8.0"}
        mock.return_value = api_instance
        yield mock


@pytest.fixture
def mock_infra_commands():
    infra = _make_mock_infra()
    with (
        patch.object(
            InfraCommands, "get_instance", side_effect=LookupError("not found")
        ),
        patch.object(InfraCommands, "build", return_value=infra),
        patch.object(InfraCommands, "set_instance"),
    ):
        yield infra


@pytest.fixture(autouse=True)
def cleanup_state():
    """Isolate pool/infra class state and the sentinel env vars per test."""
    saved = {
        var: os.environ.get(var)
        for var in ("PROXMOX_HOST", "PROXMOX_NODE", "PROXMOX_CONFIG_FILE")
    }
    ProxmoxSandboxEnvironment.proxmox_pool.clear_pools()
    yield
    for var, value in saved.items():
        if value is None:
            os.environ.pop(var, None)
        else:
            os.environ[var] = value
    ProxmoxSandboxEnvironment.proxmox_pool.clear_pools()
    InfraCommands._instances.clear()


def _config_file(instances: list[dict]) -> str:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump({"instances": instances}, f)
        return f.name


def _instance(instance_id: str, host: str, node: str) -> dict:
    return {
        "instance_id": instance_id,
        "pool_id": "default",
        "host": host,
        "port": 8006,
        "user": "root",
        "user_realm": "pam",
        "password": "test",
        "node": node,
        "verify_tls": False,
    }


@pytest.mark.asyncio
async def test_sample_records_acquired_instance_not_env_default(
    mock_proxmox_api, mock_infra_commands
):
    """The store must record the acquired pool instance, not the env default."""
    config_file = _config_file([_instance("pool-inst-1", "10.0.1.10", "pve1")])
    os.environ["PROXMOX_CONFIG_FILE"] = config_file
    os.environ["PROXMOX_HOST"] = ENV_SENTINEL_HOST
    os.environ["PROXMOX_NODE"] = "env-default-sentinel"
    try:
        await ProxmoxSandboxEnvironment.task_init("test_task", None)
        config = ProxmoxSandboxEnvironmentConfig()

        sample_store = Store()
        init_subtask_store(sample_store)
        environments = await ProxmoxSandboxEnvironment.sample_init(
            "test_task", config, {}
        )

        # The bug: the frozen config field is the env default. Documented
        # here as the trap — do not use it for host attribution.
        assert config.host == ENV_SENTINEL_HOST
        assert config.node == "env-default-sentinel"

        # The fix: the store records the instance actually acquired.
        assert sample_store.get("proxmox:instance_id") == "pool-inst-1"
        assert sample_store.get("proxmox:host") == "10.0.1.10"
        assert sample_store.get("proxmox:port") == 8006
        assert sample_store.get("proxmox:node") == "pve1"
        assert sample_store.get("proxmox:pool_id") == "default"

        await ProxmoxSandboxEnvironment.sample_cleanup(
            "test_task", config, environments, False
        )
    finally:
        os.unlink(config_file)


@pytest.mark.asyncio
async def test_concurrent_samples_record_distinct_instances(
    mock_proxmox_api, mock_infra_commands
):
    """Two concurrent samples over a 2-instance pool record different hosts.

    Mirrors the incident: concurrent epochs each logged host=PROXMOX_HOST
    even though they provably ran on different Proxmox servers.
    """
    config_file = _config_file(
        [
            _instance("pool-inst-1", "10.0.1.10", "pve1"),
            _instance("pool-inst-2", "10.0.1.11", "pve2"),
        ]
    )
    os.environ["PROXMOX_CONFIG_FILE"] = config_file
    os.environ["PROXMOX_HOST"] = ENV_SENTINEL_HOST
    try:
        await ProxmoxSandboxEnvironment.task_init("test_task", None)
        config = ProxmoxSandboxEnvironmentConfig()

        hold_both = asyncio.Barrier(2)

        async def run_sample(sample_store: Store) -> None:
            # Each asyncio task has its own context, so this store is
            # per-sample — the same isolation Inspect's task runner provides.
            init_subtask_store(sample_store)
            environments = await ProxmoxSandboxEnvironment.sample_init(
                "test_task", config, {}
            )
            # Hold the instance until the other sample has acquired its own,
            # forcing the pool to hand out both instances.
            await hold_both.wait()
            await ProxmoxSandboxEnvironment.sample_cleanup(
                "test_task", config, environments, False
            )

        store_a, store_b = Store(), Store()
        await asyncio.gather(run_sample(store_a), run_sample(store_b))

        by_id = {
            "pool-inst-1": ("10.0.1.10", "pve1"),
            "pool-inst-2": ("10.0.1.11", "pve2"),
        }
        recorded_ids = set()
        for sample_store in (store_a, store_b):
            instance_id = sample_store.get("proxmox:instance_id")
            recorded_ids.add(instance_id)
            expected_host, expected_node = by_id[instance_id]
            assert sample_store.get("proxmox:host") == expected_host
            assert sample_store.get("proxmox:node") == expected_node
            assert sample_store.get("proxmox:host") != ENV_SENTINEL_HOST

        assert recorded_ids == {"pool-inst-1", "pool-inst-2"}, (
            f"both samples recorded the same instance: {recorded_ids}"
        )
    finally:
        os.unlink(config_file)


@pytest.mark.asyncio
async def test_failed_sample_init_still_records_instance(
    mock_proxmox_api, mock_infra_commands
):
    """Samples that fail during VM creation still record which instance they used.

    Attribution matters most for failure forensics (the motivating incident
    was diagnosed from QGA retry warnings), so the stamp happens immediately
    after acquisition, before anything can fail.
    """
    config_file = _config_file([_instance("pool-inst-1", "10.0.1.10", "pve1")])
    os.environ["PROXMOX_CONFIG_FILE"] = config_file
    try:
        mock_infra_commands.create_sdn_and_vms = AsyncMock(
            side_effect=ValueError("boom")
        )
        await ProxmoxSandboxEnvironment.task_init("test_task", None)
        config = ProxmoxSandboxEnvironmentConfig()

        sample_store = Store()
        init_subtask_store(sample_store)
        with pytest.raises(ValueError, match="boom"):
            await ProxmoxSandboxEnvironment.sample_init("test_task", config, {})

        assert sample_store.get("proxmox:instance_id") == "pool-inst-1"
        assert sample_store.get("proxmox:host") == "10.0.1.10"
    finally:
        os.unlink(config_file)
