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

        # Mock the pre-check to return no VNETs (clean instance)
        sdn_commands_mock = AsyncMock()
        sdn_commands_mock.read_all_vnets = AsyncMock(return_value=[])
        infra_instance.sdn_commands = sdn_commands_mock

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

        # Mock the pre-check to return no VNETs (clean instance)
        sdn_commands_mock = AsyncMock()
        sdn_commands_mock.read_all_vnets = AsyncMock(return_value=[])
        infra_instance.sdn_commands = sdn_commands_mock

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
async def test_sample_init_precheck_cleans_dirty_instance(
    simple_config_file,
    mock_proxmox_api,
    mock_built_in_vm,
):
    """Test that pre-check detects and cleans leftover VNETs.

    When an instance has leftover VNETs from a failed previous cleanup,
    the pre-check should detect them and call cleanup_no_id before proceeding.
    """
    os.environ["PROXMOX_CONFIG_FILE"] = simple_config_file

    patch_path = "proxmoxsandbox._proxmox_sandbox_environment.InfraCommands"
    with patch(patch_path) as mock_infra:
        infra_instance = AsyncMock()

        # Mock the pre-check to find leftover VNETs
        sdn_commands_mock = AsyncMock()
        sdn_commands_mock.read_all_vnets = AsyncMock(
            return_value=[{"vnet": "leftover1"}, {"vnet": "leftover2"}]
        )
        infra_instance.sdn_commands = sdn_commands_mock

        infra_instance.cleanup_no_id = AsyncMock()

        # Make find_proxmox_ids_start fail so we can check if cleanup was called
        infra_instance.find_proxmox_ids_start = AsyncMock(
            side_effect=Exception("Stopping after pre-check")
        )

        mock_infra.return_value = infra_instance

        try:
            await ProxmoxSandboxEnvironment.task_init("test_task", None)

            config = ProxmoxSandboxEnvironmentConfig(instance_pool_id="default")

            # sample_init will fail, but pre-check should have run first
            with pytest.raises(Exception, match="Stopping after pre-check"):
                await ProxmoxSandboxEnvironment.sample_init("test_task", config, {})

            # Verify pre-check cleanup was called for leftover VNETs
            assert infra_instance.cleanup_no_id.called, (
                "Pre-check should have called cleanup_no_id for leftover VNETs"
            )

        finally:
            if "PROXMOX_CONFIG_FILE" in os.environ:
                del os.environ["PROXMOX_CONFIG_FILE"]
            ProxmoxSandboxEnvironment.proxmox_pool.clear_pools()


@pytest.mark.asyncio
async def test_sample_init_precheck_cleanup_fails_but_continues(
    simple_config_file,
    mock_proxmox_api,
    mock_built_in_vm,
):
    """Test that pre-check logs error if cleanup fails, but continues.

    If the pre-check detects VNETs but cleanup fails, it should log an error
    and continue (not raise).
    """
    os.environ["PROXMOX_CONFIG_FILE"] = simple_config_file

    patch_path = "proxmoxsandbox._proxmox_sandbox_environment.InfraCommands"
    with patch(patch_path) as mock_infra:
        infra_instance = AsyncMock()

        # Mock the pre-check to find VNETs but cleanup fails
        sdn_commands_mock = AsyncMock()
        sdn_commands_mock.read_all_vnets = AsyncMock(
            return_value=[{"vnet": "leftover1"}]
        )
        infra_instance.sdn_commands = sdn_commands_mock

        infra_instance.cleanup_no_id = AsyncMock(
            side_effect=RuntimeError("Pre-check cleanup failed")
        )

        # Make find_proxmox_ids_start fail to stop early
        infra_instance.find_proxmox_ids_start = AsyncMock(
            side_effect=Exception("Stopping after pre-check")
        )

        mock_infra.return_value = infra_instance

        try:
            await ProxmoxSandboxEnvironment.task_init("test_task", None)

            config = ProxmoxSandboxEnvironmentConfig(instance_pool_id="default")

            # Pre-check cleanup fails but sample_init continues
            # (will fail later for different reason)
            with pytest.raises(Exception, match="Stopping after pre-check"):
                await ProxmoxSandboxEnvironment.sample_init("test_task", config, {})

            # Verify cleanup was attempted despite failure
            assert infra_instance.cleanup_no_id.called, (
                "Pre-check should have attempted cleanup"
            )

        finally:
            if "PROXMOX_CONFIG_FILE" in os.environ:
                del os.environ["PROXMOX_CONFIG_FILE"]
            ProxmoxSandboxEnvironment.proxmox_pool.clear_pools()


@pytest.mark.asyncio
async def test_sample_init_precheck_read_vnets_fails_but_continues(
    simple_config_file,
    mock_proxmox_api,
    mock_built_in_vm,
):
    """Test that pre-check logs error if read_all_vnets fails, but continues.

    If the pre-check cannot read VNETs, it should log an error and continue
    (not raise).
    """
    os.environ["PROXMOX_CONFIG_FILE"] = simple_config_file

    patch_path = "proxmoxsandbox._proxmox_sandbox_environment.InfraCommands"
    with patch(patch_path) as mock_infra:
        infra_instance = AsyncMock()

        # Mock the pre-check to fail when reading VNETs
        sdn_commands_mock = AsyncMock()
        sdn_commands_mock.read_all_vnets = AsyncMock(
            side_effect=Exception("API error: cannot read VNETs")
        )
        infra_instance.sdn_commands = sdn_commands_mock

        infra_instance.cleanup_no_id = AsyncMock()

        # Make find_proxmox_ids_start fail to stop early
        infra_instance.find_proxmox_ids_start = AsyncMock(
            side_effect=Exception("Stopping after pre-check")
        )

        mock_infra.return_value = infra_instance

        try:
            await ProxmoxSandboxEnvironment.task_init("test_task", None)

            config = ProxmoxSandboxEnvironmentConfig(instance_pool_id="default")

            # Pre-check read fails but sample_init continues
            # (will fail later for different reason)
            with pytest.raises(Exception, match="Stopping after pre-check"):
                await ProxmoxSandboxEnvironment.sample_init("test_task", config, {})

            # Cleanup should not have been called (couldn't read VNETs)
            assert not infra_instance.cleanup_no_id.called, (
                "Cleanup should not be called if read_all_vnets fails"
            )

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

        # Mock the pre-check to return no VNETs (clean instance)
        sdn_commands_mock = AsyncMock()
        sdn_commands_mock.read_all_vnets = AsyncMock(return_value=[])
        infra_instance.sdn_commands = sdn_commands_mock

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
