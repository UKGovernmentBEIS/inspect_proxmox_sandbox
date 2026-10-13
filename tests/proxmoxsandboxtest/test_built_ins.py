import pytest

from proxmoxsandbox.built_ins import ensure_built_in_templates
from proxmoxsandbox.schema import ProxmoxInstanceConfig

pytestmark = pytest.mark.req_proxmox


async def test_ensure_built_in_templates_is_idempotent(
    instance_config: ProxmoxInstanceConfig,
) -> None:
    first = await ensure_built_in_templates(instance_config, names=["debian13"])
    assert "debian13" in first

    second = await ensure_built_in_templates(instance_config, names=["debian13"])
    assert second["debian13"] == first["debian13"]
