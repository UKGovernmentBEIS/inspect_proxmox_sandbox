"""Tests that each sample logs the pool instance it actually ran on.

sample_init logs the acquired instance at INFO level so a sample can be
attributed to the Proxmox server it ran on.
"""

import asyncio
import json
import logging
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from proxmoxsandbox._impl.infra_commands import InfraCommands
from proxmoxsandbox._proxmox_sandbox_environment import ProxmoxSandboxEnvironment
from proxmoxsandbox.schema import ProxmoxSandboxEnvironmentConfig

LOGGER_NAME = "proxmoxsandbox._proxmox_sandbox_environment"


def test_provider_logger_opted_into_info():
    """The provider lowers its own logger to INFO so its lines reach the .eval.

    Inspect leaves third-party package loggers at the root WARNING default, so
    without this the attribution line would never be created under a default
    `inspect eval`. Users can still suppress via --log-level-transcript warning.
    """
    assert logging.getLogger("proxmoxsandbox").level == logging.INFO
    assert logging.getLogger(LOGGER_NAME).isEnabledFor(logging.INFO)


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
        var: os.environ.get(var) for var in ("PROXMOX_HOST", "PROXMOX_CONFIG_FILE")
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
async def test_concurrent_samples_log_distinct_instances(
    mock_proxmox_api, mock_infra_commands, caplog
):
    """Two concurrent samples over a 2-instance pool log different hosts.

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

        async def run_sample() -> None:
            environments = await ProxmoxSandboxEnvironment.sample_init(
                "test_task", config, {}
            )
            # Hold the instance until the other sample has acquired its own,
            # forcing the pool to hand out both instances.
            await hold_both.wait()
            await ProxmoxSandboxEnvironment.sample_cleanup(
                "test_task", config, environments, False
            )

        with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
            await asyncio.gather(run_sample(), run_sample())

        acquired = [
            r.message for r in caplog.records if "Acquired instance" in r.message
        ]
        assert len(acquired) == 2

        by_id = {
            "pool-inst-1": ("host=10.0.1.10", "node=pve1"),
            "pool-inst-2": ("host=10.0.1.11", "node=pve2"),
        }
        recorded_ids = set()
        for message in acquired:
            instance_id = next(i for i in by_id if i in message)
            recorded_ids.add(instance_id)
            expected_host, expected_node = by_id[instance_id]
            assert expected_host in message
            assert expected_node in message
            assert ENV_SENTINEL_HOST not in message

        assert recorded_ids == {"pool-inst-1", "pool-inst-2"}, (
            f"both samples logged the same instance: {recorded_ids}"
        )
    finally:
        os.unlink(config_file)


@pytest.mark.asyncio
async def test_failed_sample_init_still_logs_instance(
    mock_proxmox_api, mock_infra_commands, caplog
):
    """Samples that fail during VM creation still log which instance they used.

    Attribution matters most for failure forensics (the motivating incident
    was diagnosed from QGA retry warnings), so the log happens immediately
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

        with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
            with pytest.raises(ValueError, match="boom"):
                await ProxmoxSandboxEnvironment.sample_init("test_task", config, {})

        acquired = [
            r.message for r in caplog.records if "Acquired instance" in r.message
        ]
        assert len(acquired) == 1
        assert "pool-inst-1" in acquired[0]
        assert "host=10.0.1.10" in acquired[0]
    finally:
        os.unlink(config_file)
