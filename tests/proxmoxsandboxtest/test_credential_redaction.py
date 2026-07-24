"""Tests that Proxmox credentials cannot leak through configuration logs."""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import SecretStr, ValidationError

from proxmoxsandbox._impl.async_proxmox import AsyncProxmoxAPI
from proxmoxsandbox._proxmox_sandbox_environment import (
    ProxmoxSandboxEnvironment,
)
from proxmoxsandbox.schema import (
    ProxmoxInstanceConfig,
)

PASSWORD_SENTINEL = "audit-password-sentinel-do-not-log"


def _instance_config() -> ProxmoxInstanceConfig:
    return ProxmoxInstanceConfig(
        instance_id="audit-instance",
        pool_id="audit-pool",
        host="127.0.0.1",
        port=8006,
        user="root",
        user_realm="pam",
        password=PASSWORD_SENTINEL,
        node="proxmox",
        verify_tls=False,
    )


def test_passwords_are_redacted_in_config_representations():
    """Config repr, str, and JSON serialization must not contain passwords."""
    config = _instance_config()

    assert isinstance(config.password, SecretStr)
    assert config.password.get_secret_value() == PASSWORD_SENTINEL
    assert PASSWORD_SENTINEL not in repr(config)
    assert PASSWORD_SENTINEL not in str(config)
    assert PASSWORD_SENTINEL not in config.model_dump_json()


def test_validation_error_messages_hide_raw_instance_config():
    """Rendered validation errors must not include raw credentials."""
    with pytest.raises(ValidationError) as exc_info:
        ProxmoxInstanceConfig.model_validate({"password": PASSWORD_SENTINEL})

    message = str(exc_info.value)
    assert PASSWORD_SENTINEL not in message
    assert PASSWORD_SENTINEL not in repr(exc_info.value)
    assert "input_value=" not in message


def test_password_is_unwrapped_only_for_api_authentication():
    """The API client still receives the configured plaintext credential."""
    api = AsyncProxmoxAPI.from_instance_config(_instance_config())

    assert api.password == PASSWORD_SENTINEL


@pytest.mark.asyncio
async def test_cleanup_failure_log_excludes_instance_password(caplog):
    """A cleanup warning contains safe instance context but no credential."""
    instance = _instance_config()
    infra_commands = MagicMock()
    infra_commands.delete_sdn_and_vms = AsyncMock(
        side_effect=RuntimeError("forced cleanup failure")
    )
    environment = ProxmoxSandboxEnvironment(
        infra_commands=infra_commands,
        agent_commands=MagicMock(),
        ipam_mappings=(),
        vm_id=100,
        all_vm_ids=(100,),
        sdn_zone_id="abc123z",
        instance=instance,
        pool_id=instance.pool_id,
    )

    with (
        patch.object(ProxmoxSandboxEnvironment, "proxmox_pool"),
        caplog.at_level(logging.DEBUG, logger=ProxmoxSandboxEnvironment.logger.name),
        pytest.raises(RuntimeError, match="forced cleanup failure"),
    ):
        await ProxmoxSandboxEnvironment.sample_cleanup(
            task_name="audit",
            config=None,
            environments={"default": environment},
            interrupted=False,
        )

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert PASSWORD_SENTINEL not in messages
    assert "instance=ProxmoxInstanceConfig" not in messages
    assert "password=" not in messages
    assert "instance_id=audit-instance" in messages
    assert "pool_id=audit-pool" in messages
    assert "host=127.0.0.1" in messages
    assert "port=8006" in messages
    assert "node=proxmox" in messages
