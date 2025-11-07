"""Unit tests for configuration loading."""

import json
import os
import tempfile

import pytest
from pydantic import ValidationError

from proxmoxsandbox.schema import (
    ProxmoxInstanceConfig,
    ProxmoxSandboxEnvironmentConfig,
    _load_instances_from_env_or_file,
)


def test_load_instances_from_file():
    """Test loading instances from PROXMOX_CONFIG_FILE."""
    config_data = {
        "instances": [
            {
                "instance_id": "test-1",
                "pool_id": "ubuntu-pool",
                "host": "10.0.1.10",
                "port": 8006,
                "user": "root",
                "user_realm": "pam",
                "password": "secret",
                "node": "pve1",
                "verify_tls": False,
            },
            {
                "instance_id": "test-2",
                "pool_id": "kali-pool",
                "host": "10.0.1.20",
                "port": 8006,
                "user": "root",
                "user_realm": "pam",
                "password": "secret",
                "node": "pve2",
                "verify_tls": True,
            },
        ]
    }

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False
    ) as f:
        json.dump(config_data, f)
        temp_path = f.name

    try:
        # Clear any existing env vars
        old_env = os.environ.copy()
        for key in list(os.environ.keys()):
            if key.startswith("PROXMOX_"):
                del os.environ[key]

        os.environ["PROXMOX_CONFIG_FILE"] = temp_path

        instances = _load_instances_from_env_or_file()

        assert len(instances) == 2
        assert isinstance(instances[0], ProxmoxInstanceConfig)
        assert instances[0].instance_id == "test-1"
        assert instances[0].pool_id == "ubuntu-pool"
        assert instances[0].host == "10.0.1.10"
        assert instances[0].verify_tls is False

        assert instances[1].instance_id == "test-2"
        assert instances[1].pool_id == "kali-pool"
        assert instances[1].verify_tls is True

    finally:
        os.unlink(temp_path)
        # Restore original environment
        os.environ.clear()
        os.environ.update(old_env)


def test_load_instances_from_env_vars():
    """Test legacy loading from individual environment variables."""
    old_env = os.environ.copy()

    try:
        # Clear any existing Proxmox env vars
        for key in list(os.environ.keys()):
            if key.startswith("PROXMOX_"):
                del os.environ[key]

        # Set legacy env vars
        os.environ["PROXMOX_HOST"] = "10.0.1.10"
        os.environ["PROXMOX_PORT"] = "8006"
        os.environ["PROXMOX_USER"] = "admin"
        os.environ["PROXMOX_REALM"] = "pve"
        os.environ["PROXMOX_PASSWORD"] = "test123"
        os.environ["PROXMOX_NODE"] = "node1"
        os.environ["PROXMOX_VERIFY_TLS"] = "0"

        instances = _load_instances_from_env_or_file()

        assert len(instances) == 1
        assert instances[0].instance_id == "default"
        assert instances[0].pool_id == "default"
        assert instances[0].host == "10.0.1.10"
        assert instances[0].port == 8006
        assert instances[0].user == "admin"
        assert instances[0].user_realm == "pve"
        assert instances[0].password == "test123"
        assert instances[0].node == "node1"
        assert instances[0].verify_tls is False

    finally:
        # Restore original environment
        os.environ.clear()
        os.environ.update(old_env)


def test_load_instances_empty_when_no_config():
    """Test that empty tuple is returned when no config is available."""
    old_env = os.environ.copy()

    try:
        # Clear all Proxmox env vars
        for key in list(os.environ.keys()):
            if key.startswith("PROXMOX_"):
                del os.environ[key]

        instances = _load_instances_from_env_or_file()

        assert instances == ()
        assert len(instances) == 0

    finally:
        # Restore original environment
        os.environ.clear()
        os.environ.update(old_env)


def test_config_file_takes_priority_over_env_vars():
    """Test that PROXMOX_CONFIG_FILE takes priority over env vars."""
    config_data = {
        "instances": [
            {
                "instance_id": "from-file",
                "pool_id": "file-pool",
                "host": "10.0.2.10",
                "port": 8006,
                "user": "root",
                "user_realm": "pam",
                "password": "secret",
                "node": "pve-file",
                "verify_tls": False,
            }
        ]
    }

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False
    ) as f:
        json.dump(config_data, f)
        temp_path = f.name

    old_env = os.environ.copy()

    try:
        # Set legacy env vars (should be ignored)
        os.environ["PROXMOX_HOST"] = "10.0.1.10"
        os.environ["PROXMOX_NODE"] = "pve-env"

        # Set config file (should take priority)
        os.environ["PROXMOX_CONFIG_FILE"] = temp_path

        instances = _load_instances_from_env_or_file()

        assert len(instances) == 1
        assert instances[0].instance_id == "from-file"
        assert instances[0].host == "10.0.2.10"
        assert instances[0].node == "pve-file"

    finally:
        os.unlink(temp_path)
        os.environ.clear()
        os.environ.update(old_env)


def test_invalid_json_in_config_file():
    """Test that invalid JSON in config file raises appropriate error."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False
    ) as f:
        f.write("{invalid json")
        temp_path = f.name

    old_env = os.environ.copy()

    try:
        os.environ["PROXMOX_CONFIG_FILE"] = temp_path

        with pytest.raises(json.JSONDecodeError):
            _load_instances_from_env_or_file()

    finally:
        os.unlink(temp_path)
        os.environ.clear()
        os.environ.update(old_env)


def test_missing_required_fields_in_instance():
    """Test that missing required fields in instance config raises error."""
    config_data = {
        "instances": [
            {
                "instance_id": "test-1",
                # Missing pool_id!
                "host": "10.0.1.10",
                "port": 8006,
                "user": "root",
                "user_realm": "pam",
                "password": "secret",
                "node": "pve1",
                "verify_tls": False,
            }
        ]
    }

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False
    ) as f:
        json.dump(config_data, f)
        temp_path = f.name

    old_env = os.environ.copy()

    try:
        os.environ["PROXMOX_CONFIG_FILE"] = temp_path

        with pytest.raises(ValidationError):
            _load_instances_from_env_or_file()

    finally:
        os.unlink(temp_path)
        os.environ.clear()
        os.environ.update(old_env)


def test_sandbox_config_defaults():
    """Test ProxmoxSandboxEnvironmentConfig default values."""
    config = ProxmoxSandboxEnvironmentConfig()

    # Check defaults
    assert config.instance_pool_id == "default"
    assert config.sdn_config == "auto"
    assert len(config.vms_config) == 1
    assert config.vms_config[0].vm_source_config.built_in == "ubuntu24.04"


def test_sandbox_config_explicit_pool_id():
    """Test ProxmoxSandboxEnvironmentConfig with explicit pool_id."""
    config = ProxmoxSandboxEnvironmentConfig(
        instance_pool_id="custom-pool"
    )

    assert config.instance_pool_id == "custom-pool"
