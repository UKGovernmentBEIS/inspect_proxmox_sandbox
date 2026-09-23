"""A guest reply must not break the runner or reject honest process failures."""

import base64
import random

import pytest
from pydantic import JsonValue

from .guest_agent_fixture import (
    Scenario,
    exercise,
    isolated,
    isolated_many,
)


@pytest.mark.parametrize("payload", [b"", b"hello\n", bytes(range(256)) * 16])
async def test_guest_files_are_returned_byte_for_byte(payload: bytes):
    reply = {"content": base64.b64encode(payload).decode()}
    observed = await exercise(Scenario(file_reply=reply))
    assert observed.kind == "file"
    assert observed.content == base64.b64encode(payload).decode()


async def test_separately_encoded_file_chunks_are_all_returned():
    chunks = [b"first", b"\x00\xff", b"last chunk"]
    reply = {"content": "".join(base64.b64encode(chunk).decode() for chunk in chunks)}
    observed = await exercise(Scenario(file_reply=reply))
    assert observed.kind == "file"
    assert observed.content == base64.b64encode(b"".join(chunks)).decode()


@pytest.mark.parametrize("content", ["AAAA=", "AAAA==", "AAAA", "YQ==Yg==Yw=="])
async def test_file_cannot_exceed_the_requested_size(content: str):
    observed = await exercise(Scenario(file_reply={"content": content}, read_limit=2))
    assert observed.kind == "rejected"


@pytest.mark.parametrize("optimized", [False, True])
def test_invalid_tail_is_rejected_without_stalling_the_runner(optimized: bool):
    observed = isolated(
        Scenario(file_reply={"content": "A" * 64 + "!"}), optimized=optimized
    )
    assert observed.kind == "rejected"
    assert observed.vm_id == 100


@pytest.mark.parametrize("value", ["42&file=other", True, None, {}, [], -1])
async def test_invalid_process_number_is_rejected_before_another_request(value):
    observed = await exercise(Scenario(operation="exec", exec_reply={"pid": value}))
    assert observed.kind == "rejected"
    assert observed.vm_id == 100


@pytest.mark.parametrize("with_stderr", [False, True])
@pytest.mark.parametrize("exception", [-1073741819, 0xC0000005])
async def test_windows_crash_is_reported_as_a_process_failure(
    with_stderr: bool, exception: int
):
    status: dict[str, JsonValue] = {"exited": 1, "signal": exception}
    if with_stderr:
        status["err-data"] = "runner failed"
    observed = await exercise(
        Scenario(operation="exec", os_type="win11", status_reply=status, return_code="")
    )
    assert observed.kind == "exec"
    assert observed.returncode == -1073741819
    assert observed.success is False


async def test_killed_linux_runner_can_report_stderr_without_an_exit_code():
    observed = await exercise(
        Scenario(
            operation="exec",
            status_reply={"exited": 1, "signal": 9, "err-data": "runner failed"},
            return_code="",
        )
    )
    assert observed.kind == "exec"
    assert observed.returncode == 137
    assert observed.stderr == "runner failed"
    assert observed.success is False


def test_corrupt_file_encodings_are_rejected_at_different_positions():
    randomizer = random.Random(42)
    cases = []
    for _ in range(40):
        payload = randomizer.randbytes(randomizer.randrange(1, 100_000))
        encoded = base64.b64encode(payload).decode()
        position = randomizer.randrange(len(encoded) + 1)
        corrupted = encoded[:position] + "!" + encoded[position:]
        cases.append(Scenario(file_reply={"content": corrupted}))
    assert all(result.kind == "rejected" for result in isolated_many(cases))


async def test_valid_guest_command_returns_its_output_and_exit_code():
    observed = await exercise(
        Scenario(operation="exec", stdout="result", stderr="warning", return_code="7")
    )
    assert observed.kind == "exec"
    assert observed.stdout == "result"
    assert observed.stderr == "warning"
    assert observed.returncode == 7
    assert observed.success is False


@pytest.mark.parametrize(
    "status",
    [
        {},
        {"exited": "1"},
        {"exited": 2},
        {"exited": None},
        {"exited": 1, "exitcode": "0"},
        {"exited": 1, "exitcode": True},
        {"exited": 1, "exitcode": 2**33},
        {"exited": 1, "signal": "killed"},
        {"exited": 1, "signal": 9999},
        {"exited": 1, "err-data": ["error"]},
    ],
)
async def test_malformed_status_is_reported_as_an_invalid_guest_reply(status):
    observed = await exercise(Scenario(operation="exec", status_reply=status))
    assert observed.kind == "rejected"
    assert observed.vm_id == 100


@pytest.mark.parametrize(
    "reply",
    [
        {"content": "!!!!"},
        {"content": "A"},
        {"content": "AAAA===="},
        {"content": "h\u00e9llo"},
        {"content": 12345},
        {"content": ["QUFB"]},
        {"content": "YQ==", "truncated": "1"},
        {"content": "YQ==", "truncated": 1},
        [],
        None,
    ],
)
async def test_malformed_file_reply_is_reported_with_its_vm(reply):
    observed = await exercise(Scenario(file_reply=reply))
    assert observed.kind == "rejected"
    assert observed.vm_id == 100


async def test_a_full_file_read_reports_that_more_data_may_remain():
    observed = await exercise(
        Scenario(file_reply={"content": "YWJj", "truncated": 1}, read_limit=3)
    )
    assert observed.kind == "file"
    assert observed.content == "YWJj"
    assert observed.truncated is True


async def test_guest_error_text_is_bounded_before_becoming_an_exception():
    observed = await exercise(Scenario(http_error="X" * 100_000))
    assert observed.kind == "error"
    assert observed.error_type == "HTTPStatusError"
    assert observed.error_length is not None and observed.error_length < 10_000


@pytest.mark.parametrize(
    "reply", [{"content": 12}, {"content": "\u0100"}, {"truncated": "yes"}]
)
async def test_older_hosts_also_reject_malformed_file_replies(reply):
    observed = await exercise(Scenario(file_reply=reply, release="8.4"))
    assert observed.kind == "rejected"
    assert observed.vm_id == 100


async def test_older_hosts_preserve_binary_bytes_and_honor_the_read_limit():
    observed = await exercise(
        Scenario(
            file_reply={"content": "\u0000\u00ffrest"}, release="8.4", read_limit=2
        )
    )
    assert observed.kind == "file"
    assert observed.content == base64.b64encode(b"\x00\xff").decode()
    assert observed.truncated is True


@pytest.mark.parametrize("truncated", [False, True])
async def test_older_hosts_preserve_the_server_truncation_flag(truncated):
    observed = await exercise(
        Scenario(
            file_reply={"content": "caf\u00e9", "truncated": truncated},
            release="9.1",
            read_limit=1024,
        )
    )
    assert observed.kind == "file"
    assert observed.content == base64.b64encode(b"caf\xe9").decode()
    assert observed.truncated is truncated


def test_invalid_reply_during_iso_upload_stops_the_command():
    observed = isolated(
        Scenario(
            operation="exec",
            command=["echo", "x" * 200_000],
            iso_uploads=True,
            status_replies=[{"exited": "invalid"}],
        )
    )
    assert observed.kind == "rejected"


async def test_guest_file_text_cannot_disguise_a_rejected_reply_as_a_missing_file():
    observed = await exercise(
        Scenario(file_reply={"content": "Agent error: No such file or directory"})
    )
    assert observed.kind == "rejected"
    assert observed.vm_id == 100


async def test_invalid_status_is_not_hidden_at_any_stage_of_large_input_execution():
    scenario = Scenario(operation="exec", input="x" * 100_000)
    control = await exercise(scenario)
    assert control.kind == "exec"
    for position in range(control.status_reads):
        observed = await exercise(
            scenario.model_copy(
                update={
                    "status_replies": [{"exited": 1, "exitcode": 0}] * position
                    + [{"exited": "invalid"}]
                }
            )
        )
        assert observed.kind == "rejected", position
        assert observed.vm_id == 100


@pytest.mark.parametrize("return_code", ["1_0", "not-a-number", "9" * 100])
async def test_malformed_returncode_file_cannot_become_a_command_result(return_code):
    observed = await exercise(Scenario(operation="exec", return_code=return_code))
    assert observed.kind in ("error", "rejected")
    if observed.kind == "error":
        assert observed.error_type == "ValueError"


@pytest.mark.parametrize("field", ["out-data", "err-data"])
async def test_oversized_process_output_is_rejected(field):
    observed = await exercise(
        Scenario(
            operation="exec",
            status_reply={"exited": 1, "exitcode": 0, field: "x" * (32 * 1024 * 1024)},
        )
    )
    assert observed.kind == "rejected"
    assert observed.vm_id == 100
