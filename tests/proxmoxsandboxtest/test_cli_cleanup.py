"""Tests for the cli_cleanup function."""

import json
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from proxmoxsandbox._impl.infra_commands import InfraCommands
from proxmoxsandbox._proxmox_sandbox_environment import ProxmoxSandboxEnvironment


@pytest.fixture(autouse=True)
def cleanup_infra_instances():
    """Clear InfraCommands._instances after each test."""
    yield
    InfraCommands._instances.clear()


@pytest.fixture
def multi_instance_config_file():
    """Create config file with multiple instances across different pools."""
    config_data = {
        "instances": [
            {
                "instance_id": "server-1",
                "pool_id": "pool-a",
                "host": "10.0.1.10",
                "port": 8006,
                "user": "root",
                "user_realm": "pam",
                "password": "test",
                "node": "pve1",
                "verify_tls": False,
            },
            {
                "instance_id": "server-2",
                "pool_id": "pool-a",
                "host": "10.0.1.11",
                "port": 8006,
                "user": "root",
                "user_realm": "pam",
                "password": "test",
                "node": "pve2",
                "verify_tls": False,
            },
            {
                "instance_id": "server-3",
                "pool_id": "pool-b",
                "host": "10.0.1.20",
                "port": 8006,
                "user": "root",
                "user_realm": "pam",
                "password": "test",
                "node": "pve3",
                "verify_tls": False,
            },
        ]
    }

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(config_data, f)
        temp_path = f.name

    yield temp_path
    os.unlink(temp_path)


@pytest.mark.asyncio
async def test_cli_cleanup_all_instances(multi_instance_config_file):
    """Test that cli_cleanup calls cleanup_no_id on all instances when id=None."""
    os.environ["PROXMOX_CONFIG_FILE"] = multi_instance_config_file

    mock_infra_instances = []

    def create_mock_infra(async_proxmox, node, image_storage):
        """Track each InfraCommands instance created via build()."""
        mock_infra = MagicMock()
        mock_infra.cleanup_no_id = AsyncMock()
        mock_infra.node = node
        mock_infra_instances.append(mock_infra)
        return mock_infra

    try:
        with (
            patch("proxmoxsandbox._proxmox_sandbox_environment.AsyncProxmoxAPI"),
            patch.object(InfraCommands, "build", side_effect=create_mock_infra),
        ):
            await ProxmoxSandboxEnvironment.cli_cleanup(id=None)

            # Verify InfraCommands.build was called for each instance
            assert len(mock_infra_instances) == 3

            # Check nodes
            nodes_created = {infra.node for infra in mock_infra_instances}
            assert nodes_created == {"pve1", "pve2", "pve3"}

            # Verify cleanup_no_id was called on each InfraCommands instance
            for mock_infra in mock_infra_instances:
                mock_infra.cleanup_no_id.assert_called_once()

    finally:
        if "PROXMOX_CONFIG_FILE" in os.environ:
            del os.environ["PROXMOX_CONFIG_FILE"]
        ProxmoxSandboxEnvironment.proxmox_pool.clear_pools()


@pytest.mark.asyncio
async def test_cli_cleanup_with_id_not_implemented():
    """Test that cli_cleanup with id parameter prints not implemented message."""
    # Mock print to capture output
    with patch("builtins.print") as mock_print:
        await ProxmoxSandboxEnvironment.cli_cleanup(id="some-id")

        # Verify the not implemented message was printed
        mock_print.assert_called_once()
        args = mock_print.call_args[0][0]
        assert "Cleanup by ID not implemented" in args
        assert "[red]" in args  # Rich formatting


@pytest.mark.asyncio
async def test_cli_cleanup_empty_config():
    """Test cli_cleanup handles empty instance list gracefully."""
    config_data = {"instances": []}

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(config_data, f)
        temp_path = f.name

    try:
        os.environ["PROXMOX_CONFIG_FILE"] = temp_path

        with (
            patch("proxmoxsandbox._proxmox_sandbox_environment.AsyncProxmoxAPI"),
            patch.object(InfraCommands, "build"),
        ):
            # Should not crash with empty instances
            await ProxmoxSandboxEnvironment.cli_cleanup(id=None)

    finally:
        os.unlink(temp_path)
        if "PROXMOX_CONFIG_FILE" in os.environ:
            del os.environ["PROXMOX_CONFIG_FILE"]
        ProxmoxSandboxEnvironment.proxmox_pool.clear_pools()


@pytest.mark.asyncio
async def test_cli_cleanup_handles_cleanup_error():
    """Test that cli_cleanup propagates exception when cleanup fails."""
    config_data = {
        "instances": [
            {
                "instance_id": "server-1",
                "pool_id": "default",
                "host": "10.0.1.10",
                "port": 8006,
                "user": "root",
                "user_realm": "pam",
                "password": "test",
                "node": "pve1",
                "verify_tls": False,
            },
            {
                "instance_id": "server-2",
                "pool_id": "default",
                "host": "10.0.1.11",
                "port": 8006,
                "user": "root",
                "user_realm": "pam",
                "password": "test",
                "node": "pve2",
                "verify_tls": False,
            },
        ]
    }

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(config_data, f)
        temp_path = f.name

    try:
        os.environ["PROXMOX_CONFIG_FILE"] = temp_path

        cleanup_calls = []

        def create_failing_infra(async_proxmox, node, image_storage):
            instance_num = len(cleanup_calls) + 1
            cleanup_calls.append(instance_num)

            async def fail_cleanup():
                if instance_num == 1:
                    raise Exception("Cleanup failed on first instance")

            mock_infra = MagicMock()
            mock_infra.cleanup_no_id = fail_cleanup
            return mock_infra

        with (
            patch("proxmoxsandbox._proxmox_sandbox_environment.AsyncProxmoxAPI"),
            patch.object(InfraCommands, "build", side_effect=create_failing_infra),
        ):
            # Should raise the exception from the first cleanup
            with pytest.raises(Exception, match="Cleanup failed on first instance"):
                await ProxmoxSandboxEnvironment.cli_cleanup(id=None)

            # Only the first cleanup should have been attempted
            # (Current implementation doesn't continue on error)
            assert cleanup_calls == [1]

    finally:
        os.unlink(temp_path)
        if "PROXMOX_CONFIG_FILE" in os.environ:
            del os.environ["PROXMOX_CONFIG_FILE"]
        ProxmoxSandboxEnvironment.proxmox_pool.clear_pools()


@pytest.mark.asyncio
async def test_cli_cleanup_pools_created_correctly():
    """Test that cli_cleanup creates pools correctly before cleanup."""
    config_data = {
        "instances": [
            {
                "instance_id": "server-1",
                "pool_id": "ubuntu-pool",
                "host": "10.0.1.10",
                "port": 8006,
                "user": "root",
                "user_realm": "pam",
                "password": "test",
                "node": "pve1",
                "verify_tls": False,
            },
            {
                "instance_id": "server-2",
                "pool_id": "debian-pool",
                "host": "10.0.1.11",
                "port": 8006,
                "user": "root",
                "user_realm": "pam",
                "password": "test",
                "node": "pve2",
                "verify_tls": False,
            },
        ]
    }

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(config_data, f)
        temp_path = f.name

    try:
        os.environ["PROXMOX_CONFIG_FILE"] = temp_path

        def create_mock_infra(async_proxmox, node, image_storage):
            mock_infra = MagicMock()
            mock_infra.cleanup_no_id = AsyncMock()
            return mock_infra

        with (
            patch("proxmoxsandbox._proxmox_sandbox_environment.AsyncProxmoxAPI"),
            patch.object(InfraCommands, "build", side_effect=create_mock_infra),
        ):
            # Pools should not exist before cleanup
            assert len(ProxmoxSandboxEnvironment.proxmox_pool._instance_pools) == 0

            await ProxmoxSandboxEnvironment.cli_cleanup(id=None)

            # Pools should have been created
            pools = ProxmoxSandboxEnvironment.proxmox_pool._instance_pools
            assert "ubuntu-pool" in pools
            assert "debian-pool" in pools

    finally:
        os.unlink(temp_path)
        if "PROXMOX_CONFIG_FILE" in os.environ:
            del os.environ["PROXMOX_CONFIG_FILE"]
        ProxmoxSandboxEnvironment.proxmox_pool.clear_pools()
