"""Unit tests for the QGA response trust boundary (no Proxmox needed).

These lock down the validator behaviour that the e2e scenarios demonstrate is
reachable through real Proxmox. They must pass under `python -O` too - the whole
point of the boundary is that it does not rely on `assert`.
"""

import logging

import pytest

from proxmoxsandbox._impl.qga_responses import (
    GuestAgentTamperError,
    parse_return_code,
    validate_exec_pid,
    validate_exec_status,
)

VM = 101


class TestExecPid:
    def test_int_pid_passes(self):
        assert validate_exec_pid({"pid": 4321}, VM) == 4321

    @pytest.mark.parametrize(
        "pid",
        [
            "1 OR 1=1",  # the seed finding: query injection into exec-status URL
            "12345",  # numeric string still rejected (strict, no coercion)
            True,  # bool is not an int here
            1.0,  # float is not an int
            None,
            {"nested": 1},
        ],
    )
    def test_non_int_pid_rejected(self, pid):
        with pytest.raises(GuestAgentTamperError):
            validate_exec_pid({"pid": pid}, VM)

    def test_missing_pid_rejected(self):
        with pytest.raises(GuestAgentTamperError):
            validate_exec_pid({}, VM)

    def test_non_dict_response_rejected(self):
        with pytest.raises(GuestAgentTamperError):
            validate_exec_pid("nope", VM)

    def test_error_names_vm_and_truncates_value(self, caplog):
        big = "A" * 5000
        with caplog.at_level(logging.WARNING):
            with pytest.raises(GuestAgentTamperError) as ei:
                validate_exec_pid({"pid": big}, VM)
        # The raw value is truncated in both the message and the log record.
        assert "101" in str(ei.value)
        assert len(str(ei.value)) < 400
        assert any(r.levelno == logging.WARNING for r in caplog.records)


class TestExecStatus:
    def test_honest_status_passes(self):
        s = validate_exec_status({"exited": 1, "exitcode": 0}, VM)
        assert s.exited == 1 and s.exitcode == 0 and s.signal is None

    def test_err_data_presence_detected(self):
        s = validate_exec_status(
            {"exited": 1, "exitcode": 2, "err-data": "", "out-data": "x"}, VM
        )
        assert s.has_err_data is True
        assert s.err_data == "" and s.out_data == "x"

    def test_no_err_data(self):
        s = validate_exec_status({"exited": 1, "exitcode": 0}, VM)
        assert s.has_err_data is False

    @pytest.mark.parametrize(
        "status",
        [
            {"exited": 1, "exitcode": "0"},  # string exitcode -> returncode confusion
            {"exited": 1, "signal": "9"},  # string signal -> 128+signal TypeError
            {"exited": 1, "exitcode": {}},  # dict exitcode
            {"exited": "1"},  # string exited
            {"exited": 1.0},  # float exited
            {},  # exited missing
            "notadict",
        ],
    )
    def test_bad_status_rejected(self, status):
        with pytest.raises(GuestAgentTamperError):
            validate_exec_status(status, VM)


class TestReturnCode:
    @pytest.mark.parametrize(
        "raw,expected", [(b"0", 0), (b"137", 137), (b" 42 \n", 42), (b"-1", -1)]
    )
    def test_integer_returncode(self, raw, expected):
        assert parse_return_code(raw, VM) == expected

    @pytest.mark.parametrize("raw", [b"rm -rf /", b"0; curl evil", b"NaN", b"0x10"])
    def test_non_integer_returncode_rejected(self, raw):
        with pytest.raises(GuestAgentTamperError):
            parse_return_code(raw, VM)

    def test_empty_returncode_rejected(self):
        # Empty is handled as ReturnCodeNotWritten by the caller *before* this is
        # reached; if it does reach here it is still tamper, not a silent 0.
        with pytest.raises(GuestAgentTamperError):
            parse_return_code(b"   ", VM)
