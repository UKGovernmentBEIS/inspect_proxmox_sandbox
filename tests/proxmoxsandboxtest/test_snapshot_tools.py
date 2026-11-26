from unittest.mock import AsyncMock, MagicMock

import pytest
from inspect_ai.tool import ToolError
from inspect_ai.util._store import Store, init_subtask_store

from proxmoxsandbox._proxmox_sandbox_environment import ProxmoxSandboxEnvironment
from proxmoxsandbox.experimental import snapshots
from proxmoxsandbox.experimental.snapshots import (
    create_snapshot,
    rollback_to_snapshot,
)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    init_subtask_store(Store())
    env = MagicMock(spec=ProxmoxSandboxEnvironment)
    env.as_type.return_value = env
    env.create_snapshot = AsyncMock()
    env.restore_snapshot = AsyncMock()
    monkeypatch.setattr(snapshots, "sandbox", lambda: env)
    return env


async def test_successive_snapshots_get_successive_ids(env: MagicMock) -> None:
    create = create_snapshot()

    assert [await create(), await create(), await create()] == [1, 2, 3]
    assert [c.args for c in env.create_snapshot.await_args_list] == [
        ("inspect1",),
        ("inspect2",),
        ("inspect3",),
    ]


async def test_failed_create_does_not_use_up_the_id(env: MagicMock) -> None:
    create = create_snapshot()
    env.create_snapshot.side_effect = [RuntimeError("task failed"), None]

    with pytest.raises(RuntimeError):
        await create()
    assert await create() == 1
    env.create_snapshot.assert_awaited_with("inspect1")


async def test_rollback_to_created_snapshot(env: MagicMock) -> None:
    create = create_snapshot()
    await create()
    await create()

    assert await rollback_to_snapshot()(snapshot_id=1) is True
    env.restore_snapshot.assert_awaited_once_with("inspect1")


@pytest.mark.parametrize("snapshot_id", [0, 2, 99])
async def test_rollback_to_unknown_id_is_a_tool_error(
    env: MagicMock, snapshot_id: int
) -> None:
    await create_snapshot()()

    with pytest.raises(ToolError, match=f"No snapshot {snapshot_id}"):
        await rollback_to_snapshot()(snapshot_id=snapshot_id)
    env.restore_snapshot.assert_not_awaited()
