"""Tests for sample_init cleanup on failure.

This test module verifies that when sample_init fails during infrastructure
creation, any partially-created resources are cleaned up before the instance
is returned to the pool.
"""

import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from proxmoxsandbox._proxmox_sandbox_environment import ProxmoxSandboxEnvironment
from proxmoxsandbox.schema import ProxmoxSandboxEnvironmentConfig


@pytest.fixture
def simple_config_file():
    """Path to single instance config fixture."""
    return str(Path(__file__).parent / "fixtures" / "single_instance_config.json")


@pytest.fixture
def mock_proxmox_api():
    """Mock AsyncProxmoxAPI."""
    with patch("proxmoxsandbox._proxmox_sandbox_environment.AsyncProxmoxAPI") as mock:
        api_instance = AsyncMock()
        api_instance.get.return_value = {"version": "8.0"}
        mock.return_value = api_instance
        yield mock


@pytest.fixture
def mock_built_in_vm():
    """Mock BuiltInVM."""
    with patch("proxmoxsandbox._proxmox_sandbox_environment.BuiltInVM") as mock:
        vm_instance = AsyncMock()
        vm_instance.ensure_exists = AsyncMock()
        mock.return_value = vm_instance
        yield mock


@pytest.mark.asyncio
async def test_sample_init_cleanup_on_create_sdn_failure(
    simple_config_file,
    mock_proxmox_api,
    mock_built_in_vm,
):
    """Test that partial infrastructure is cleaned up when create_sdn_and_vms fails.

    This test simulates the scenario where:
    1. sample_init acquires an instance from the pool
    2. create_sdn_and_vms is called, which partially succeeds (creates SDN)
    3. Then fails with an exception (e.g., duplicate CIDR error)
    4. The exception handler SHOULD clean up the partial SDN
    5. Then release the instance back to pool
    """
    os.environ["PROXMOX_CONFIG_FILE"] = simple_config_file

    patch_path = "proxmoxsandbox._proxmox_sandbox_environment.InfraCommands"
    with patch(patch_path) as mock_infra:
        infra_instance = AsyncMock()

        infra_instance.find_proxmox_ids_start = AsyncMock(return_value="test123")

        error_msg = "Duplicate IP ranges found: [('10.129.0.0/24', '10.129.0.0/24')]"
        infra_instance.create_sdn_and_vms = AsyncMock(side_effect=ValueError(error_msg))

        infra_instance.cleanup_no_id = AsyncMock()

        mock_infra.return_value = infra_instance

        try:
            await ProxmoxSandboxEnvironment.task_init("test_task", None)

            config = ProxmoxSandboxEnvironmentConfig(instance_pool_id="default")
            pool = ProxmoxSandboxEnvironment.proxmox_pool._instance_pools["default"]

            assert pool.qsize() == 1

            with pytest.raises(ValueError, match="Duplicate IP ranges found"):
                await ProxmoxSandboxEnvironment.sample_init("test_task", config, {})

            assert infra_instance.create_sdn_and_vms.called, (
                "create_sdn_and_vms should have been called"
            )

            assert infra_instance.cleanup_no_id.called, (
                "cleanup_no_id should be called when create_sdn_and_vms fails, "
                "to clean up any partial infrastructure. "
                "Without this, leftover resources cause conflicts for next sample."
            )

            assert pool.qsize() == 1, (
                "Instance should be returned to pool even on failure"
            )

        finally:
            if "PROXMOX_CONFIG_FILE" in os.environ:
                del os.environ["PROXMOX_CONFIG_FILE"]
            ProxmoxSandboxEnvironment.proxmox_pool.clear_pools()


@pytest.mark.asyncio
async def test_sample_init_no_cleanup_on_early_failure(
    simple_config_file,
    mock_proxmox_api,
    mock_built_in_vm,
):
    """Test that cleanup is NOT called when failure happens before any infra creation.

    If the failure happens early (e.g., in find_proxmox_ids_start), no cleanup
    should be attempted since nothing was created.
    """
    os.environ["PROXMOX_CONFIG_FILE"] = simple_config_file

    patch_path = "proxmoxsandbox._proxmox_sandbox_environment.InfraCommands"
    with patch(patch_path) as mock_infra:
        infra_instance = AsyncMock()

        # Fail before any infrastructure is created
        infra_instance.find_proxmox_ids_start = AsyncMock(
            side_effect=Exception("API connection failed")
        )

        infra_instance.create_sdn_and_vms = AsyncMock()
        infra_instance.cleanup_no_id = AsyncMock()

        mock_infra.return_value = infra_instance

        try:
            await ProxmoxSandboxEnvironment.task_init("test_task", None)

            config = ProxmoxSandboxEnvironmentConfig(instance_pool_id="default")
            pool = ProxmoxSandboxEnvironment.proxmox_pool._instance_pools["default"]

            with pytest.raises(Exception, match="API connection failed"):
                await ProxmoxSandboxEnvironment.sample_init("test_task", config, {})

            assert not infra_instance.create_sdn_and_vms.called

            # Cleanup should not be called since nothing was created
            assert not infra_instance.cleanup_no_id.called

            assert pool.qsize() == 1

        finally:
            if "PROXMOX_CONFIG_FILE" in os.environ:
                del os.environ["PROXMOX_CONFIG_FILE"]
            ProxmoxSandboxEnvironment.proxmox_pool.clear_pools()


@pytest.mark.asyncio
async def test_sample_init_dirty_instance_not_returned_when_cleanup_fails(
    simple_config_file,
    mock_proxmox_api,
    mock_built_in_vm,
):
    """Test that instance is NOT returned to pool when cleanup fails.

    This test verifies the critical behavior from sample_cleanup:
    - If cleanup fails, the instance is "dirty" (has leftover resources)
    - Dirty instances should NOT be returned to pool
    - This prevents cascading failures across samples
    """
    os.environ["PROXMOX_CONFIG_FILE"] = simple_config_file

    patch_path = "proxmoxsandbox._proxmox_sandbox_environment.InfraCommands"
    with patch(patch_path) as mock_infra:
        infra_instance = AsyncMock()

        infra_instance.find_proxmox_ids_start = AsyncMock(return_value="test789")

        infra_instance.create_sdn_and_vms = AsyncMock(
            side_effect=ValueError("Duplicate IP ranges found")
        )

        infra_instance.cleanup_no_id = AsyncMock(
            side_effect=RuntimeError("Cleanup failed: SDN zone locked")
        )

        mock_infra.return_value = infra_instance

        try:
            await ProxmoxSandboxEnvironment.task_init("test_task", None)

            config = ProxmoxSandboxEnvironmentConfig(instance_pool_id="default")
            pool = ProxmoxSandboxEnvironment.proxmox_pool._instance_pools["default"]

            assert pool.qsize() == 1

            with pytest.raises(ValueError, match="Duplicate IP ranges found"):
                await ProxmoxSandboxEnvironment.sample_init("test_task", config, {})

            assert infra_instance.create_sdn_and_vms.called, (
                "create_sdn_and_vms should have been called"
            )

            assert infra_instance.cleanup_no_id.called, (
                "cleanup_no_id should have been attempted"
            )

            assert pool.qsize() == 0, (
                "Instance should NOT be returned to pool when cleanup fails. "
                "Dirty instances cause cascading failures. "
            )

        finally:
            if "PROXMOX_CONFIG_FILE" in os.environ:
                del os.environ["PROXMOX_CONFIG_FILE"]
            ProxmoxSandboxEnvironment.proxmox_pool.clear_pools()
