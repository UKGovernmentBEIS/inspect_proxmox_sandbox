"""Tests for what sample_init leaves behind when it fails.

A failed sample_init must not tear anything down itself: Inspect only says
whether the user wants cleanup via task_cleanup(cleanup=...), so partial
infrastructure stays up (and tracked) for task_cleanup, and the instance goes
back to the pool for the next sample's pre-clean.
"""

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from proxmoxsandbox._impl.infra_commands import InfraCommands, ProxmoxTarget
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


def _make_infra_mock(**overrides):
    """Create a mock InfraCommands with sensible defaults.

    Pass keyword arguments to override specific attributes, e.g.:
        _make_infra_mock(find_proxmox_ids_start=AsyncMock(side_effect=Exception("boom")))
    """
    infra = MagicMock()
    infra.sdn_commands = MagicMock()
    infra.sdn_commands.read_all_vnets = AsyncMock(return_value=[])
    infra.qemu_commands = MagicMock()
    infra.qemu_commands.list_vms = AsyncMock(return_value=[])
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


async def _fail_sample_init_after_partial_create(infra, config_file):
    """Run task_init + a sample_init that fails inside create_sdn_and_vms."""
    os.environ["PROXMOX_CONFIG_FILE"] = config_file
    await ProxmoxSandboxEnvironment.task_init("test_task", None)
    config = ProxmoxSandboxEnvironmentConfig(instance_pool_id="default")
    pool = ProxmoxSandboxEnvironment.proxmox_pool._instance_pools["default"]
    assert pool.qsize() == 1

    with pytest.raises(ValueError, match="QEMU agent never answered"):
        await ProxmoxSandboxEnvironment.sample_init("test_task", config, {})

    assert infra.create_sdn_and_vms.called
    return pool


def _target_for(infra):
    # Matches fixtures/single_instance_config.json
    return {ProxmoxTarget(host="10.0.1.10", port=8006, node="pve1"): infra}


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup", [True, False])
async def test_sample_init_failure_defers_teardown_to_task_cleanup(
    simple_config_file, mock_proxmox_api, cleanup
):
    """Partial infrastructure survives a failed sample_init.

    task_cleanup then sweeps it if (and only if) Inspect asks for cleanup --
    this is what makes --no-sandbox-cleanup hold a half-built range up.
    """
    infra = _make_infra_mock(
        create_sdn_and_vms=AsyncMock(
            side_effect=ValueError("VM 123 QEMU agent never answered")
        ),
    )
    p1, p2, p3 = _patch_infra(infra)

    with (
        p1,
        p2,
        p3,
        patch.dict(InfraCommands._instances, _target_for(infra), clear=True),
    ):
        try:
            pool = await _fail_sample_init_after_partial_create(
                infra, simple_config_file
            )

            assert not infra.cleanup_no_id.called, (
                "sample_init must not tear down on failure; only task_cleanup "
                "knows whether the user asked for cleanup"
            )
            assert not infra.task_cleanup.called
            assert pool.qsize() == 1, "Instance goes back to the pool for reuse"

            await ProxmoxSandboxEnvironment.task_cleanup(
                "test_task", None, cleanup=cleanup
            )
            assert infra.task_cleanup.called == cleanup
        finally:
            del os.environ["PROXMOX_CONFIG_FILE"]


@pytest.mark.asyncio
async def test_sample_init_no_cleanup_on_early_failure(
    simple_config_file,
    mock_proxmox_api,
):
    """A failure before anything is created releases the instance untouched."""
    os.environ["PROXMOX_CONFIG_FILE"] = simple_config_file

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
            assert not infra.cleanup_no_id.called
            assert pool.qsize() == 1

        finally:
            if "PROXMOX_CONFIG_FILE" in os.environ:
                del os.environ["PROXMOX_CONFIG_FILE"]


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "vms,expect_cleanup",
    [
        # A previous sample's VM on a pre-existing (non-ephemeral) VNET: no
        # zone to notice, so the VM itself has to trigger the pre-clean.
        ([{"vmid": 123, "name": "left-behind", "tags": "inspect;kali"}], True),
        # Templates and untagged user VMs are not leftovers.
        (
            [
                {"vmid": 100, "name": "tpl", "tags": "inspect;kali", "template": 1},
                {"vmid": 999, "name": "builder"},
            ],
            False,
        ),
    ],
)
async def test_sample_init_precheck_notices_leftover_vms(
    simple_config_file, mock_proxmox_api, vms, expect_cleanup
):
    os.environ["PROXMOX_CONFIG_FILE"] = simple_config_file

    qemu_mock = MagicMock()
    qemu_mock.list_vms = AsyncMock(return_value=vms)
    infra = _make_infra_mock(
        qemu_commands=qemu_mock,
        find_proxmox_ids_start=AsyncMock(
            side_effect=Exception("Stopping after pre-check")
        ),
    )
    p1, p2, p3 = _patch_infra(infra)

    with p1, p2, p3:
        try:
            await ProxmoxSandboxEnvironment.task_init("test_task", None)
            config = ProxmoxSandboxEnvironmentConfig(instance_pool_id="default")

            with pytest.raises(Exception, match="Stopping after pre-check"):
                await ProxmoxSandboxEnvironment.sample_init("test_task", config, {})

            assert infra.cleanup_no_id.called == expect_cleanup
        finally:
            del os.environ["PROXMOX_CONFIG_FILE"]


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

    sdn_mock = MagicMock()
    # Pre-existing user vnets in user-named zones; none matches ZONE_REGEX.
    # Includes the static built-in SDN (intentionally permanent) and a
    # near-miss user zone whose 7-char *prefix* would match an unanchored
    # pattern but which is not a provider zone.
    sdn_mock.read_all_vnets = AsyncMock(
        return_value=[
            {"vnet": "monitor", "zone": "a254c5f5"},
            {"vnet": "inspvmv0", "zone": "inspvmz"},
            {"vnet": "abc123v", "zone": "abc123za"},
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

            with pytest.raises(Exception, match="Stopping after pre-check"):
                await ProxmoxSandboxEnvironment.sample_init("test_task", config, {})

            assert not infra.cleanup_no_id.called, (
                "Pre-check must NOT call cleanup_no_id when only pre-existing "
                "user VNETs (or the static inspvm* SDN) are present."
            )

        finally:
            if "PROXMOX_CONFIG_FILE" in os.environ:
                del os.environ["PROXMOX_CONFIG_FILE"]


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

    sdn_mock = MagicMock()
    sdn_mock.read_all_vnets = AsyncMock(
        return_value=[{"vnet": "tlo123v0", "zone": "tlo123z"}]
    )
    infra = _make_infra_mock(
        sdn_commands=sdn_mock,
        cleanup_no_id=AsyncMock(side_effect=RuntimeError("Pre-check cleanup failed")),
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
            assert infra.cleanup_no_id.called, "Pre-check should have attempted cleanup"

        finally:
            if "PROXMOX_CONFIG_FILE" in os.environ:
                del os.environ["PROXMOX_CONFIG_FILE"]


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
