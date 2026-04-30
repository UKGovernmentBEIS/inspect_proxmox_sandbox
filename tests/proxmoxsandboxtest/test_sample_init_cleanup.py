"""Tests for sample_init cleanup on failure.

This test module verifies that when sample_init fails during infrastructure
creation, any partially-created resources are cleaned up before the instance
is returned to the pool.
"""

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from proxmoxsandbox._impl.infra_commands import InfraCommands
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


@pytest.fixture(autouse=True)
def cleanup_infra_instances():
    """Clear InfraCommands._instances after each test."""
    yield
    InfraCommands._instances.clear()


def _make_infra_mock(**overrides):
    """Create a mock InfraCommands with sensible defaults.

    Pass keyword arguments to override specific attributes, e.g.:
        _make_infra_mock(find_proxmox_ids_start=AsyncMock(side_effect=Exception("boom")))
    """
    infra = MagicMock()
    infra.sdn_commands = MagicMock()
    infra.sdn_commands.read_all_vnets = AsyncMock(return_value=[])
    infra.qemu_commands = MagicMock()
    infra.task_wrapper = MagicMock()
    infra.built_in_vm = AsyncMock()
    infra.built_in_vm.ensure_exists = AsyncMock()
    infra.async_proxmox = AsyncMock()
    infra.node = "pve1"
    infra.find_proxmox_ids_start = AsyncMock(return_value="test123")
    infra.create_sdn_and_vms = AsyncMock()
    infra.cleanup_no_id = AsyncMock()
    infra.deregister_resources = MagicMock()
    infra.task_cleanup = AsyncMock()

    for key, value in overrides.items():
        setattr(infra, key, value)
    return infra


def _patch_infra(infra_mock):
    """Return a context manager that patches InfraCommands classmethods."""
    return (
        patch.object(
            InfraCommands, "get_instance", side_effect=LookupError("not found")
        ),
        patch.object(InfraCommands, "build", return_value=infra_mock),
        patch.object(InfraCommands, "set_instance"),
    )


@pytest.mark.asyncio
async def test_sample_init_cleanup_on_create_sdn_failure(
    simple_config_file,
    mock_proxmox_api,
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
    ProxmoxSandboxEnvironment.proxmox_pool.clear_pools()

    error_msg = "Duplicate IP ranges found: [('10.129.0.0/24', '10.129.0.0/24')]"
    infra = _make_infra_mock(
        create_sdn_and_vms=AsyncMock(side_effect=ValueError(error_msg)),
    )
    p1, p2, p3 = _patch_infra(infra)

    with p1, p2, p3:
        try:
            await ProxmoxSandboxEnvironment.task_init("test_task", None)

            config = ProxmoxSandboxEnvironmentConfig(instance_pool_id="default")
            pool = ProxmoxSandboxEnvironment.proxmox_pool._instance_pools["default"]

            assert pool.qsize() == 1

            with pytest.raises(ValueError, match="Duplicate IP ranges found"):
                await ProxmoxSandboxEnvironment.sample_init("test_task", config, {})

            assert infra.create_sdn_and_vms.called, (
                "create_sdn_and_vms should have been called"
            )

            assert infra.cleanup_no_id.called, (
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
):
    """Test that cleanup is NOT called when failure happens before any infra creation.

    If the failure happens early (e.g., in find_proxmox_ids_start), no cleanup
    should be attempted since nothing was created.
    """
    os.environ["PROXMOX_CONFIG_FILE"] = simple_config_file
    ProxmoxSandboxEnvironment.proxmox_pool.clear_pools()

    infra = _make_infra_mock(
        find_proxmox_ids_start=AsyncMock(
            side_effect=Exception("API connection failed")
        ),
    )
    p1, p2, p3 = _patch_infra(infra)

    with p1, p2, p3:
        try:
            await ProxmoxSandboxEnvironment.task_init("test_task", None)

            config = ProxmoxSandboxEnvironmentConfig(instance_pool_id="default")
            pool = ProxmoxSandboxEnvironment.proxmox_pool._instance_pools["default"]

            with pytest.raises(Exception, match="API connection failed"):
                await ProxmoxSandboxEnvironment.sample_init("test_task", config, {})

            assert not infra.create_sdn_and_vms.called

            # Cleanup should not be called since nothing was created
            assert not infra.cleanup_no_id.called

            assert pool.qsize() == 1

        finally:
            if "PROXMOX_CONFIG_FILE" in os.environ:
                del os.environ["PROXMOX_CONFIG_FILE"]
            ProxmoxSandboxEnvironment.proxmox_pool.clear_pools()


@pytest.mark.asyncio
async def test_sample_init_precheck_cleans_dirty_instance(
    simple_config_file,
    mock_proxmox_api,
):
    """Test that pre-check detects and cleans leftover provider VNETs.

    When an instance has leftover VNETs from a failed previous cleanup,
    the pre-check should detect them and call cleanup_no_id before proceeding.

    Leftover VNETs must live in a zone matching the provider's ephemeral
    zone naming convention (ZONE_REGEX); pre-existing user VNETs are
    intentionally ignored — see test_sample_init_precheck_ignores_pre_existing_vnets.
    """
    os.environ["PROXMOX_CONFIG_FILE"] = simple_config_file
    ProxmoxSandboxEnvironment.proxmox_pool.clear_pools()

    sdn_mock = MagicMock()
    # Both vnets live in a zone whose name matches ZONE_REGEX
    # (3 chars + 3 digits + "z"), simulating an orphaned ephemeral zone.
    sdn_mock.read_all_vnets = AsyncMock(
        return_value=[
            {"vnet": "tlo123v0", "zone": "tlo123z"},
            {"vnet": "tlo123v1", "zone": "tlo123z"},
        ]
    )
    infra = _make_infra_mock(
        sdn_commands=sdn_mock,
        find_proxmox_ids_start=AsyncMock(
            side_effect=Exception("Stopping after pre-check")
        ),
    )
    p1, p2, p3 = _patch_infra(infra)

    with p1, p2, p3:
        try:
            await ProxmoxSandboxEnvironment.task_init("test_task", None)

            config = ProxmoxSandboxEnvironmentConfig(instance_pool_id="default")

            # sample_init will fail, but pre-check should have run first
            with pytest.raises(Exception, match="Stopping after pre-check"):
                await ProxmoxSandboxEnvironment.sample_init("test_task", config, {})

            # Verify pre-check cleanup was called for leftover VNETs
            assert infra.cleanup_no_id.called, (
                "Pre-check should have called cleanup_no_id for leftover VNETs"
            )

        finally:
            if "PROXMOX_CONFIG_FILE" in os.environ:
                del os.environ["PROXMOX_CONFIG_FILE"]
            ProxmoxSandboxEnvironment.proxmox_pool.clear_pools()


@pytest.mark.asyncio
async def test_sample_init_precheck_ignores_pre_existing_vnets(
    simple_config_file,
    mock_proxmox_api,
):
    """Test that pre-check does NOT trigger cleanup for pre-existing user VNETs.

    When sdn_config=None, samples plug into pre-existing VNETs that the user
    manages. Those zones do not match the provider's ephemeral zone naming
    convention (ZONE_REGEX), and the pre-check must leave them alone so it
    doesn't risk wiping user state via cleanup_no_id.
    """
    os.environ["PROXMOX_CONFIG_FILE"] = simple_config_file
    ProxmoxSandboxEnvironment.proxmox_pool.clear_pools()

    sdn_mock = MagicMock()
    # Pre-existing user vnets in user-named zones; neither matches ZONE_REGEX.
    # Also include the static built-in SDN, which is intentionally permanent.
    sdn_mock.read_all_vnets = AsyncMock(
        return_value=[
            {"vnet": "monitor", "zone": "a254c5f5"},
            {"vnet": "inspvmv0", "zone": "inspvmz"},
        ]
    )
    infra = _make_infra_mock(
        sdn_commands=sdn_mock,
        find_proxmox_ids_start=AsyncMock(side_effect=Exception("Stopping after pre-check")),
    )
    p1, p2, p3 = _patch_infra(infra)

    with p1, p2, p3:
        try:
            await ProxmoxSandboxEnvironment.task_init("test_task", None)

            config = ProxmoxSandboxEnvironmentConfig(instance_pool_id="default")

            with pytest.raises(Exception, match="Stopping after pre-check"):
                await ProxmoxSandboxEnvironment.sample_init("test_task", config, {})

            assert not infra.cleanup_no_id.called, (
                "Pre-check must NOT call cleanup_no_id when only pre-existing "
                "user VNETs (or the static inspvm* SDN) are present."
            )

        finally:
            if "PROXMOX_CONFIG_FILE" in os.environ:
                del os.environ["PROXMOX_CONFIG_FILE"]
            ProxmoxSandboxEnvironment.proxmox_pool.clear_pools()


@pytest.mark.asyncio
async def test_sample_init_precheck_cleanup_fails_but_continues(
    simple_config_file,
    mock_proxmox_api,
):
    """Test that pre-check logs error if cleanup fails, but continues.

    If the pre-check detects VNETs but cleanup fails, it should log an error
    and continue (not raise).
    """
    os.environ["PROXMOX_CONFIG_FILE"] = simple_config_file
    ProxmoxSandboxEnvironment.proxmox_pool.clear_pools()

    sdn_mock = MagicMock()
    sdn_mock.read_all_vnets = AsyncMock(
        return_value=[{"vnet": "tlo123v0", "zone": "tlo123z"}]
    )
    infra = _make_infra_mock(
        sdn_commands=sdn_mock,
        cleanup_no_id=AsyncMock(
            side_effect=RuntimeError("Pre-check cleanup failed")
        ),
        find_proxmox_ids_start=AsyncMock(
            side_effect=Exception("Stopping after pre-check")
        ),
    )
    p1, p2, p3 = _patch_infra(infra)

    with p1, p2, p3:
        try:
            await ProxmoxSandboxEnvironment.task_init("test_task", None)

            config = ProxmoxSandboxEnvironmentConfig(instance_pool_id="default")

            # Pre-check cleanup fails but sample_init continues
            # (will fail later for different reason)
            with pytest.raises(Exception, match="Stopping after pre-check"):
                await ProxmoxSandboxEnvironment.sample_init("test_task", config, {})

            # Verify cleanup was attempted despite failure
            assert infra.cleanup_no_id.called, (
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
):
    """Test that pre-check logs error if read_all_vnets fails, but continues.

    If the pre-check cannot read VNETs, it should log an error and continue
    (not raise).
    """
    os.environ["PROXMOX_CONFIG_FILE"] = simple_config_file
    ProxmoxSandboxEnvironment.proxmox_pool.clear_pools()

    sdn_mock = MagicMock()
    sdn_mock.read_all_vnets = AsyncMock(
        side_effect=Exception("API error: cannot read VNETs")
    )
    infra = _make_infra_mock(
        sdn_commands=sdn_mock,
        find_proxmox_ids_start=AsyncMock(
            side_effect=Exception("Stopping after pre-check")
        ),
    )
    p1, p2, p3 = _patch_infra(infra)

    with p1, p2, p3:
        try:
            await ProxmoxSandboxEnvironment.task_init("test_task", None)

            config = ProxmoxSandboxEnvironmentConfig(instance_pool_id="default")

            # Pre-check read fails but sample_init continues
            # (will fail later for different reason)
            with pytest.raises(Exception, match="Stopping after pre-check"):
                await ProxmoxSandboxEnvironment.sample_init("test_task", config, {})

            # Cleanup should not have been called (couldn't read VNETs)
            assert not infra.cleanup_no_id.called, (
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
):
    """Test that instance is NOT returned to pool when cleanup fails.

    This test verifies the critical behavior from sample_cleanup:
    - If cleanup fails, the instance is "dirty" (has leftover resources)
    - Dirty instances should NOT be returned to pool
    - This prevents cascading failures across samples
    """
    os.environ["PROXMOX_CONFIG_FILE"] = simple_config_file
    ProxmoxSandboxEnvironment.proxmox_pool.clear_pools()

    infra = _make_infra_mock(
        create_sdn_and_vms=AsyncMock(
            side_effect=ValueError("Duplicate IP ranges found")
        ),
        cleanup_no_id=AsyncMock(
            side_effect=RuntimeError("Cleanup failed: SDN zone locked")
        ),
    )
    p1, p2, p3 = _patch_infra(infra)

    with p1, p2, p3:
        try:
            await ProxmoxSandboxEnvironment.task_init("test_task", None)

            config = ProxmoxSandboxEnvironmentConfig(instance_pool_id="default")
            pool = ProxmoxSandboxEnvironment.proxmox_pool._instance_pools["default"]

            assert pool.qsize() == 1

            with pytest.raises(ValueError, match="Duplicate IP ranges found"):
                await ProxmoxSandboxEnvironment.sample_init("test_task", config, {})

            assert infra.create_sdn_and_vms.called, (
                "create_sdn_and_vms should have been called"
            )

            assert infra.cleanup_no_id.called, (
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
