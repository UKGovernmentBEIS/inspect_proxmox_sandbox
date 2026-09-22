"""Failure paths of download_url_to_storage, which need no live Proxmox."""

from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from proxmoxsandbox._impl.storage_commands import (
    MIN_DOWNLOAD_TIMEOUT_SECONDS,
    LocalStorageCommands,
    download_timeout_for_size,
)

NODE = "proxmox"
UPID = "UPID:proxmox:00001234:download:x.ova"
OVA = "x.ova"
OVA_SIZE = 15796254720

# Indented as wget writes it; the tail is reported stripped.
PROGRESS_LINE = "  4900000K .......... 31%  9.80M 18m2s"
PROGRESS_REPORT = PROGRESS_LINE.strip()


def _make_storage_commands(
    *,
    task_status: dict[str, Any],
    task_log: Optional[list[dict[str, Any]]] = None,
    stored_size: Optional[int] = None,
) -> LocalStorageCommands:
    """Storage commands against a host that reports the given task state.

    `stored_size` is the size the storage listing reports for the download, or
    None for "the file isn't there" (which is all a listing tells us about a
    download still in progress).
    """

    async def request(method: str, path: str, **kwargs: Any) -> Any:
        if method == "POST" and "download-url" in path:
            return UPID
        if "/tasks/" in path and path.endswith("/status"):
            return task_status
        if "/tasks/" in path and "/log?" in path:
            return task_log or []
        if "/content?content=" in path:
            if stored_size is None:
                return []
            return [{"volid": f"local:import/{OVA}", "size": stored_size}]
        raise AssertionError(f"unexpected request: {method} {path}")

    async_proxmox = MagicMock()
    async_proxmox.request = AsyncMock(side_effect=request)

    # Run the action directly: the real wrapper polls /cluster/tasks, which is not
    # what these tests are about.
    async def do_action_and_wait_for_tasks(action: Any, **kwargs: Any) -> None:
        await action()

    task_wrapper = MagicMock()
    task_wrapper.do_action_and_wait_for_tasks = AsyncMock(
        side_effect=do_action_and_wait_for_tasks
    )

    return LocalStorageCommands(async_proxmox, NODE, task_wrapper)


def test_download_timeout_scales_with_size() -> None:
    assert download_timeout_for_size(None) == MIN_DOWNLOAD_TIMEOUT_SECONDS
    assert download_timeout_for_size(1024) == MIN_DOWNLOAD_TIMEOUT_SECONDS
    # A 15GB OVA gets appreciably longer than the floor.
    assert download_timeout_for_size(OVA_SIZE) > 2 * MIN_DOWNLOAD_TIMEOUT_SECONDS


async def test_failed_download_task_raises_immediately() -> None:
    """A dead download must not cost us the whole timeout."""
    storage_commands = _make_storage_commands(
        task_status={"status": "stopped", "exitstatus": "connection timed out"},
        task_log=[{"n": 1, "t": PROGRESS_LINE}],
    )

    with pytest.raises(ValueError) as raised:
        await storage_commands.download_url_to_storage(
            url="https://example.invalid/x.ova",
            content_type="import",
            filename=OVA,
            size_check=OVA_SIZE,
            # Long enough that a retrying implementation would hang the test.
            timeout_seconds=3600,
        )

    assert "connection timed out" in str(raised.value)
    assert PROGRESS_REPORT in str(raised.value)


async def test_timeout_reports_how_far_the_download_got() -> None:
    storage_commands = _make_storage_commands(
        task_status={"status": "running"},
        task_log=[
            {"n": 1, "t": "downloading https://example.invalid/x.ova to /var/lib/vz"},
            {"n": 2, "t": PROGRESS_LINE},
        ],
    )

    with pytest.raises(TimeoutError) as raised:
        await storage_commands.download_url_to_storage(
            url="https://example.invalid/x.ova",
            content_type="import",
            filename=OVA,
            size_check=OVA_SIZE,
            timeout_seconds=0,
        )

    message = str(raised.value)
    assert PROGRESS_REPORT in message
    assert "running" in message
    assert str(OVA_SIZE) in message


async def test_completed_download_of_expected_size_succeeds() -> None:
    storage_commands = _make_storage_commands(
        task_status={"status": "stopped", "exitstatus": "OK"},
        stored_size=OVA_SIZE,
    )

    await storage_commands.download_url_to_storage(
        url="https://example.invalid/x.ova",
        content_type="import",
        filename=OVA,
        size_check=OVA_SIZE,
    )


async def test_wrong_size_download_is_reported() -> None:
    storage_commands = _make_storage_commands(
        task_status={"status": "stopped", "exitstatus": "OK"},
        stored_size=OVA_SIZE - 1,
    )

    with pytest.raises(ValueError, match="size mismatch"):
        await storage_commands.download_url_to_storage(
            url="https://example.invalid/x.ova",
            content_type="import",
            filename=OVA,
            size_check=OVA_SIZE,
            timeout_seconds=0,
        )
