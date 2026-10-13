import pytest

from proxmoxsandbox.built_ins import ensure_built_in_templates
from proxmoxsandbox.schema import ProxmoxInstanceConfig

pytestmark = pytest.mark.req_proxmox


async def test_ensure_built_in_templates_is_idempotent(
    instance_config: ProxmoxInstanceConfig,
) -> None:
    first = await ensure_built_in_templates(instance_config)
    assert set(first) == {"ubuntu24.04", "debian13", "kali2025.4"}

    second = await ensure_built_in_templates(instance_config)
    assert second == first
