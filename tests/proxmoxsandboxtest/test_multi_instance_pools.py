"""Tests for multi-instance pool-based allocation."""

import asyncio
import json
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from proxmoxsandbox._proxmox_sandbox_environment import ProxmoxSandboxEnvironment
from proxmoxsandbox.schema import ProxmoxSandboxEnvironmentConfig


@pytest.fixture
def mock_proxmox_api():
    """Mock AsyncProxmoxAPI."""
    with patch('proxmoxsandbox._proxmox_sandbox_environment.AsyncProxmoxAPI') as mock:
        api_instance = AsyncMock()
        api_instance.get.return_value = {"version": "8.0"}
        mock.return_value = api_instance
        yield mock


@pytest.fixture
def mock_built_in_vm():
    """Mock BuiltInVM."""
    with patch('proxmoxsandbox._proxmox_sandbox_environment.BuiltInVM') as mock:
        vm_instance = AsyncMock()
        vm_instance.ensure_exists = AsyncMock()
        mock.return_value = vm_instance
        yield mock


@pytest.fixture
def mock_infra_commands():
    """Mock InfraCommands."""
    with patch('proxmoxsandbox._proxmox_sandbox_environment.InfraCommands') as mock:
        infra = AsyncMock()
        # Mock VM creation response
        vm_config_mock = MagicMock()
        vm_config_mock.is_sandbox = True
        vm_config_mock.name = None
        infra.create_sdn_and_vms = AsyncMock(return_value=(
            [(101, vm_config_mock)],  # vm_configs_with_ids
            "zone1"  # sdn_zone_id
        ))
        infra.delete_sdn_and_vms = AsyncMock()
        mock.return_value = infra
        yield mock


@pytest.fixture
def simple_config_file():
    """Create config file with one instance."""
    config_data = {
        "instances": [
            {
                "instance_id": "test-1",
                "pool_id": "default",
                "host": "10.0.1.10",
                "port": 8006,
                "user": "root",
                "user_realm": "pam",
                "password": "test",
                "node": "pve1",
                "verify_tls": False,
            }
        ]
    }

    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(config_data, f)
        temp_path = f.name

    yield temp_path
    os.unlink(temp_path)


@pytest.mark.asyncio
async def test_single_instance_single_sample(
    simple_config_file,
    mock_proxmox_api,
    mock_built_in_vm,
    mock_infra_commands
):
    """
    Mainline test: One instance, one sample, full lifecycle.

    Simulates the Inspect AI lifecycle:
    1. User sets PROXMOX_CONFIG_FILE environment variable
    2. task_init is called once (loads instances, creates pools)
    3. Sample has config with instance_pool_id="default"
    4. sample_init acquires instance from pool
    5. sample_cleanup releases instance back to pool
    6. task_cleanup called at end
    """

    # Setup: Set env var (what user does)
    os.environ["PROXMOX_CONFIG_FILE"] = simple_config_file

    try:
        # Step 1: task_init (Inspect calls once per task, config may be None)
        # This loads instances from PROXMOX_CONFIG_FILE and creates pools
        await ProxmoxSandboxEnvironment.task_init("test_task", None)

        # Step 2: Create sample-specific config (passed to sample_init)
        config = ProxmoxSandboxEnvironmentConfig(
            instance_pool_id="default",
            # VMs and SDN config would go here
        )

        # Verify: Config only contains eval-specific settings
        assert config.instance_pool_id == "default"

        # Verify: Pool created with 1 instance
        assert "default" in ProxmoxSandboxEnvironment._instance_pools
        pool = ProxmoxSandboxEnvironment._instance_pools["default"]
        assert pool.qsize() == 1

        # Step 3: sample_init (Inspect calls for each sample with sample's config)
        environments = await ProxmoxSandboxEnvironment.sample_init(
            "test_task", config, {}
        )

        # Verify: Environment created, instance acquired from pool
        assert "default" in environments
        assert pool.qsize() == 0  # Instance taken from pool

        # Step 4: sample_cleanup (Inspect calls after sample)
        await ProxmoxSandboxEnvironment.sample_cleanup(
            "test_task", config, environments, interrupted=False
        )

        # Verify: Instance returned to pool
        assert pool.qsize() == 1

        # Step 5: task_cleanup (Inspect calls at end, config may be None)
        await ProxmoxSandboxEnvironment.task_cleanup(
            "test_task", None, cleanup=True
        )

        # Test passed!

    finally:
        # Cleanup
        if "PROXMOX_CONFIG_FILE" in os.environ:
            del os.environ["PROXMOX_CONFIG_FILE"]
        # Clear pools for next test
        ProxmoxSandboxEnvironment._instance_pools.clear()
        ProxmoxSandboxEnvironment._pool_locks.clear()


@pytest.fixture
def multi_pool_config_file():
    """Create config file with two pools."""
    config_data = {
        "instances": [
            {
                "instance_id": "ubuntu-1",
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
                "instance_id": "ubuntu-2",
                "pool_id": "ubuntu-pool",
                "host": "10.0.1.11",
                "port": 8006,
                "user": "root",
                "user_realm": "pam",
                "password": "test",
                "node": "pve2",
                "verify_tls": False,
            },
            {
                "instance_id": "kali-1",
                "pool_id": "kali-pool",
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

    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(config_data, f)
        temp_path = f.name

    yield temp_path
    os.unlink(temp_path)


@pytest.mark.asyncio
async def test_two_pools_two_configs(
    multi_pool_config_file,
    mock_proxmox_api,
    mock_built_in_vm,
    mock_infra_commands
):
    """
    Test with two pools and two samples with different configs.

    Simulates the Inspect AI lifecycle with multiple samples:
    1. One task with multiple samples needing different pools
    2. task_init called once, creates ALL pools from PROXMOX_CONFIG_FILE
    3. First sample has config with instance_pool_id="ubuntu-pool"
    4. Second sample has config with instance_pool_id="kali-pool"
    5. Each sample_init gets instance from its specified pool
    6. Instances are returned to correct pools on cleanup
    7. task_cleanup called once at end
    """

    # Setup: Set env var
    os.environ["PROXMOX_CONFIG_FILE"] = multi_pool_config_file

    try:
        # Step 1: task_init (called ONCE per task, config may be None)
        # This creates ALL pools from PROXMOX_CONFIG_FILE
        await ProxmoxSandboxEnvironment.task_init("cyber_task", None)

        # Step 2: Create two different sample configs (like two different samples in one task)
        # In inspect_cyber, these would come from different samples in the dataset
        config_ubuntu = ProxmoxSandboxEnvironmentConfig(
            instance_pool_id="ubuntu-pool",
            # Could have ubuntu-specific VMs here
        )

        config_kali = ProxmoxSandboxEnvironmentConfig(
            instance_pool_id="kali-pool",
            # Could have kali-specific VMs here
        )

        assert config_ubuntu.instance_pool_id == "ubuntu-pool"
        assert config_kali.instance_pool_id == "kali-pool"

        # Verify: BOTH pools created from single task_init call
        assert "ubuntu-pool" in ProxmoxSandboxEnvironment._instance_pools
        assert "kali-pool" in ProxmoxSandboxEnvironment._instance_pools

        ubuntu_pool = ProxmoxSandboxEnvironment._instance_pools["ubuntu-pool"]
        kali_pool = ProxmoxSandboxEnvironment._instance_pools["kali-pool"]

        assert ubuntu_pool.qsize() == 2  # 2 ubuntu instances
        assert kali_pool.qsize() == 1    # 1 kali instance

        # Step 3: Run first sample (using ubuntu pool)
        ubuntu_envs = await ProxmoxSandboxEnvironment.sample_init(
            "cyber_task", config_ubuntu, {}
        )

        # Verify: Ubuntu instance acquired, kali pool untouched
        assert "default" in ubuntu_envs
        assert ubuntu_pool.qsize() == 1  # One ubuntu instance taken
        assert kali_pool.qsize() == 1    # Kali pool unchanged

        # Verify the acquired instance is from ubuntu pool
        ubuntu_env = ubuntu_envs["default"]
        assert ubuntu_env.instance.pool_id == "ubuntu-pool"
        assert ubuntu_env.instance.instance_id in ["ubuntu-1", "ubuntu-2"]

        # Step 4: Run second sample (using kali pool, while ubuntu is still running)
        kali_envs = await ProxmoxSandboxEnvironment.sample_init(
            "cyber_task", config_kali, {}
        )

        # Verify: Kali instance acquired
        assert "default" in kali_envs
        assert ubuntu_pool.qsize() == 1  # Ubuntu pool unchanged
        assert kali_pool.qsize() == 0    # Kali instance taken

        # Verify the acquired instance is from kali pool
        kali_env = kali_envs["default"]
        assert kali_env.instance.pool_id == "kali-pool"
        assert kali_env.instance.instance_id == "kali-1"

        # Step 5: Cleanup first sample (ubuntu)
        await ProxmoxSandboxEnvironment.sample_cleanup(
            "cyber_task", config_ubuntu, ubuntu_envs, interrupted=False
        )

        # Verify: Ubuntu instance returned
        assert ubuntu_pool.qsize() == 2  # Ubuntu instance returned
        assert kali_pool.qsize() == 0    # Kali still in use

        # Step 6: Cleanup second sample (kali)
        await ProxmoxSandboxEnvironment.sample_cleanup(
            "cyber_task", config_kali, kali_envs, interrupted=False
        )

        # Verify: Kali instance returned
        assert ubuntu_pool.qsize() == 2  # Ubuntu pool full
        assert kali_pool.qsize() == 1    # Kali instance returned

        # Step 7: task_cleanup (called once at end)
        await ProxmoxSandboxEnvironment.task_cleanup(
            "cyber_task", None, cleanup=True
        )

        # Test passed!

    finally:
        # Cleanup
        if "PROXMOX_CONFIG_FILE" in os.environ:
            del os.environ["PROXMOX_CONFIG_FILE"]
        ProxmoxSandboxEnvironment._instance_pools.clear()
        ProxmoxSandboxEnvironment._pool_locks.clear()


@pytest.mark.asyncio
async def test_wrong_pool_id_raises_error(
    simple_config_file,
    mock_proxmox_api,
    mock_built_in_vm,
    mock_infra_commands
):
    """Test that requesting non-existent pool raises clear error."""
    os.environ["PROXMOX_CONFIG_FILE"] = simple_config_file

    try:
        await ProxmoxSandboxEnvironment.task_init("test_task", None)

        # Create config requesting non-existent pool
        config = ProxmoxSandboxEnvironmentConfig(
            instance_pool_id="nonexistent-pool"
        )

        # Should raise error with clear message
        with pytest.raises(RuntimeError) as exc_info:
            await ProxmoxSandboxEnvironment.sample_init(
                "test_task", config, {}
            )

        assert "Pool 'nonexistent-pool' not found" in str(exc_info.value)
        assert "default" in str(exc_info.value)  # Shows available pools

    finally:
        if "PROXMOX_CONFIG_FILE" in os.environ:
            del os.environ["PROXMOX_CONFIG_FILE"]
        ProxmoxSandboxEnvironment._instance_pools.clear()
        ProxmoxSandboxEnvironment._pool_locks.clear()


@pytest.mark.asyncio
async def test_pool_exhaustion_blocks(
    simple_config_file,
    mock_proxmox_api,
    mock_built_in_vm,
    mock_infra_commands
):
    """Test that acquiring from exhausted pool blocks until instance available."""
    os.environ["PROXMOX_CONFIG_FILE"] = simple_config_file

    try:
        await ProxmoxSandboxEnvironment.task_init("test_task", None)

        config = ProxmoxSandboxEnvironmentConfig(instance_pool_id="default")

        # Acquire the only instance
        env1 = await ProxmoxSandboxEnvironment.sample_init(
            "test_task", config, {}
        )

        pool = ProxmoxSandboxEnvironment._instance_pools["default"]
        assert pool.qsize() == 0  # Pool exhausted

        # Try to acquire second instance - should timeout
        import asyncio
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                ProxmoxSandboxEnvironment.sample_init(
                    "test_task", config, {}
                ),
                timeout=0.1  # 100ms timeout
            )

        # Pool should still be empty
        assert pool.qsize() == 0

        # Cleanup first environment
        await ProxmoxSandboxEnvironment.sample_cleanup(
            "test_task", config, env1, interrupted=False
        )

        # Now pool has instance again
        assert pool.qsize() == 1

    finally:
        if "PROXMOX_CONFIG_FILE" in os.environ:
            del os.environ["PROXMOX_CONFIG_FILE"]
        ProxmoxSandboxEnvironment._instance_pools.clear()
        ProxmoxSandboxEnvironment._pool_locks.clear()


@pytest.mark.asyncio
async def test_sample_error_releases_instance(
    simple_config_file,
    mock_proxmox_api,
    mock_built_in_vm,
):
    """Test that instance is returned to pool even when sample_init fails."""
    # Make infra_commands raise an error
    with patch('proxmoxsandbox._proxmox_sandbox_environment.InfraCommands') as mock_infra:
        infra_instance = mock_infra.return_value
        infra_instance.find_proxmox_ids_start.side_effect = Exception("Simulated error")

        os.environ["PROXMOX_CONFIG_FILE"] = simple_config_file

        try:
            await ProxmoxSandboxEnvironment.task_init("test_task", None)

            config = ProxmoxSandboxEnvironmentConfig(instance_pool_id="default")
            pool = ProxmoxSandboxEnvironment._instance_pools["default"]

            assert pool.qsize() == 1  # One instance available

            # sample_init should fail and return instance to pool
            with pytest.raises(Exception, match="Simulated error"):
                await ProxmoxSandboxEnvironment.sample_init(
                    "test_task", config, {}
                )

            # Instance should be back in pool
            assert pool.qsize() == 1

        finally:
            if "PROXMOX_CONFIG_FILE" in os.environ:
                del os.environ["PROXMOX_CONFIG_FILE"]
            ProxmoxSandboxEnvironment._instance_pools.clear()
            ProxmoxSandboxEnvironment._pool_locks.clear()


@pytest.mark.asyncio
async def test_empty_instances_list():
    """Test behavior when instances list is empty."""
    config_data = {"instances": []}

    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(config_data, f)
        temp_path = f.name

    try:
        os.environ["PROXMOX_CONFIG_FILE"] = temp_path

        await ProxmoxSandboxEnvironment.task_init("test_task", None)

        # No pools should be created
        assert len(ProxmoxSandboxEnvironment._instance_pools) == 0

        # Trying to use any pool should fail
        config = ProxmoxSandboxEnvironmentConfig(instance_pool_id="default")

        with pytest.raises(RuntimeError, match="Pool 'default' not found"):
            await ProxmoxSandboxEnvironment.sample_init(
                "test_task", config, {}
            )

    finally:
        os.unlink(temp_path)
        if "PROXMOX_CONFIG_FILE" in os.environ:
            del os.environ["PROXMOX_CONFIG_FILE"]
        ProxmoxSandboxEnvironment._instance_pools.clear()
        ProxmoxSandboxEnvironment._pool_locks.clear()


@pytest.mark.asyncio
async def test_concurrent_task_init_calls(multi_pool_config_file):
    """Test that concurrent task_init calls are safe (idempotent)."""
    os.environ["PROXMOX_CONFIG_FILE"] = multi_pool_config_file

    try:
        # Call task_init multiple times concurrently
        await asyncio.gather(
            ProxmoxSandboxEnvironment.task_init("task1", None),
            ProxmoxSandboxEnvironment.task_init("task2", None),
            ProxmoxSandboxEnvironment.task_init("task3", None),
        )

        # Both pools should exist exactly once
        assert "ubuntu-pool" in ProxmoxSandboxEnvironment._instance_pools
        assert "kali-pool" in ProxmoxSandboxEnvironment._instance_pools

        # Correct number of instances in each pool
        assert ProxmoxSandboxEnvironment._instance_pools["ubuntu-pool"].qsize() == 2
        assert ProxmoxSandboxEnvironment._instance_pools["kali-pool"].qsize() == 1

    finally:
        if "PROXMOX_CONFIG_FILE" in os.environ:
            del os.environ["PROXMOX_CONFIG_FILE"]
        ProxmoxSandboxEnvironment._instance_pools.clear()
        ProxmoxSandboxEnvironment._pool_locks.clear()


@pytest.mark.asyncio
async def test_cleanup_with_interrupted_flag(
    simple_config_file,
    mock_proxmox_api,
    mock_built_in_vm,
    mock_infra_commands
):
    """Test that instance is returned even when interrupted=True."""
    os.environ["PROXMOX_CONFIG_FILE"] = simple_config_file

    try:
        await ProxmoxSandboxEnvironment.task_init("test_task", None)

        config = ProxmoxSandboxEnvironmentConfig(instance_pool_id="default")
        pool = ProxmoxSandboxEnvironment._instance_pools["default"]

        # Acquire instance
        envs = await ProxmoxSandboxEnvironment.sample_init(
            "test_task", config, {}
        )
        assert pool.qsize() == 0

        # Cleanup with interrupted=True (simulating ctrl-c or timeout)
        await ProxmoxSandboxEnvironment.sample_cleanup(
            "test_task", config, envs, interrupted=True
        )

        # Instance should still be returned to pool
        assert pool.qsize() == 1

    finally:
        if "PROXMOX_CONFIG_FILE" in os.environ:
            del os.environ["PROXMOX_CONFIG_FILE"]
        ProxmoxSandboxEnvironment._instance_pools.clear()
        ProxmoxSandboxEnvironment._pool_locks.clear()
