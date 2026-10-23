"""Inspect must not turn unknown execution into a retryable tool timeout."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from inspect_ai.model import ChatMessageAssistant, ChatMessageTool, execute_tools
from inspect_ai.tool import ToolCall, ToolDef
from inspect_ai.util import SandboxEnvironment
from inspect_ai.util._anyio import inner_exception
from inspect_ai.util._sandbox.events import SandboxEnvironmentProxy

from proxmoxsandbox._impl.agent_commands import GuestExecIncompleteError
from proxmoxsandbox._impl.deadline import within_budget


@pytest.mark.parametrize("pid", [None, 123])
@pytest.mark.parametrize("incomplete", [False, True])
async def test_inspect_preserves_unresolved_execution(incomplete, pid):
    failure = (
        GuestExecIncompleteError(130, pid, "retained-script.bat")
        if incomplete
        else TimeoutError("Command timed out")
    )
    provider = MagicMock(spec=SandboxEnvironment)
    provider.exec = AsyncMock(side_effect=failure)
    proxy = SandboxEnvironmentProxy(provider)

    async def probe() -> str:
        """Exercise a mocked sandbox without a model or guest."""
        await proxy.exec(["echo", "test"])
        raise AssertionError("the failing exec must not return")

    messages = [
        ChatMessageAssistant(
            content="",
            tool_calls=[ToolCall(id="probe", function="probe", arguments={})],
        )
    ]
    if incomplete:
        with pytest.raises(Exception) as raised:
            await execute_tools(messages, [ToolDef(probe)], max_output=1024)
        assert inner_exception(raised.value) is failure
    else:
        output = await execute_tools(messages, [ToolDef(probe)], max_output=1024)
        assert len(output.messages) == 1
        message = output.messages[0]
        assert isinstance(message, ChatMessageTool)
        assert message.error is not None
        assert message.error.type == "timeout"
        assert message.error.message == "Command timed out before completing."
    provider.exec.assert_awaited_once()


async def test_enclosing_budget_preserves_unresolved_execution():
    failure = GuestExecIncompleteError(130, 123, "retained-script.sh")

    async def fail():
        raise failure

    with pytest.raises(GuestExecIncompleteError) as raised:
        await within_budget(fail(), 1)
    assert raised.value is failure
