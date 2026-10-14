"""HTTP errors retain useful diagnostics without copying unbounded text."""

from functools import partial

import httpx
import pytest

from proxmoxsandbox._impl.agent_commands import GuestExecIncompleteError
from proxmoxsandbox._impl.async_proxmox import AsyncProxmoxAPI

from .guest_agent_fixture import GuestReplies, Scenario, make_sandbox


@pytest.fixture
def error_api(monkeypatch):
    def create(reason: str, body: str) -> AsyncProxmoxAPI:
        replies = GuestReplies(Scenario())

        async def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith(("/file-read", "/exec")):
                return httpx.Response(
                    400, text=body, extensions={"reason_phrase": reason.encode()}
                )
            return await replies.handle(request)

        client = partial(httpx.AsyncClient, transport=httpx.MockTransport(handle))
        monkeypatch.setattr(httpx, "AsyncClient", client)
        return AsyncProxmoxAPI("proxmox.test:8006", "test-user", "test-password")

    return create


@pytest.mark.parametrize("operation", ["read_file", "exec"])
@pytest.mark.parametrize(
    "reason, body",
    [
        ("Agent error " + "X" * 100_000, ""),
        ("Bad Request", "File operation failed " + "Y" * 100_000),
        ("Agent error " + "X" * 100_000, "File operation failed " + "Y" * 100_000),
    ],
    ids=["long-reason", "long-body", "both-long"],
)
async def test_large_http_errors_keep_diagnostics_but_not_all_text(
    error_api, operation, reason, body
):
    sandbox = make_sandbox(error_api(reason, body))
    expected_error = (
        httpx.HTTPStatusError if operation == "read_file" else GuestExecIncompleteError
    )
    with pytest.raises(expected_error) as raised:
        if operation == "read_file":
            await sandbox.read_file("/sample")
        else:
            await sandbox.exec(["echo", "hello"])

    failure = raised.value
    if isinstance(failure, GuestExecIncompleteError):
        assert failure.vm_id == 100
        assert len(str(failure)) < 20_000
        failure = failure.__cause__
    assert isinstance(failure, httpx.HTTPStatusError)
    message = str(failure)
    assert len(message) < 20_000
    assert reason[:20] in message
    if body:
        assert body[:20] in message
    assert failure.response.status_code == 400


async def test_long_reason_does_not_hide_a_missing_file_error_in_the_body(error_api):
    sandbox = make_sandbox(
        error_api(
            "X" * 100_000,
            "Agent error: failed to open file '/sample': No such file or directory",
        )
    )
    with pytest.raises(FileNotFoundError):
        await sandbox.read_file("/sample")
