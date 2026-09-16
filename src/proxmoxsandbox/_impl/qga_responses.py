"""Trust boundary for QEMU-guest-agent (QGA) responses.

The sandbox runs attacker-controlled code as root inside the guest, and every
host<->guest interaction goes through the guest agent. So each field of every QGA
response is attacker-controlled: the guest can return a `pid` of `"1 OR 1=1"`, an
`exitcode` of `{}`, or a `returncode` file of `rm -rf`. This module is the single
place those values are checked before they reach a URL, a shell, a path, or
arithmetic in the provider.

Anything that fails validation raises `GuestAgentTamperError` (never `assert`,
which `python -O` strips, and never a bare `int()`/`KeyError`/`TypeError` that
surfaces as an unattributable crash). The error is logged at WARNING with the
vm_id and a truncated copy of the offending value, and it is attributable: it
names the guest as the source, so a lying guest looks like a lying guest, not
like a provider bug.

What is deliberately NOT validated here (see SINKS.md for the full inventory):
  * out-data / err-data and file-read content become opaque stdout/stderr/bytes
    handed back to the caller - a guest lying about its own output is an accepted
    forged result, not a provider compromise.
  * `truncated` from file-read only decides whether to raise OutputLimitExceeded;
    a lie there is another forged result.
"""

from logging import getLogger
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError

logger = getLogger(__name__)

# Cap on how much of an untrusted value we echo into logs/exceptions.
_MAX_LOGGED = 120


class GuestAgentTamperError(Exception):
    """A guest-agent response field was not the shape the provider requires.

    Carries the vm_id and a truncated repr of the offending value. The raw value
    is never interpolated into a URL/command/path - only into this diagnostic.
    """

    def __init__(self, vm_id: Any, field: str, raw: Any, detail: str = "") -> None:
        self.vm_id = vm_id
        self.field = field
        self.raw_repr = _truncate(raw)
        super().__init__(
            f"VM {vm_id}: untrusted guest-agent field {field!r} "
            f"{detail or 'failed validation'} (raw={self.raw_repr})"
        )


def _truncate(value: Any) -> str:
    r = repr(value)
    return r if len(r) <= _MAX_LOGGED else r[:_MAX_LOGGED] + "...<truncated>"


def _raise(vm_id: Any, field: str, raw: Any, detail: str) -> "GuestAgentTamperError":
    err = GuestAgentTamperError(vm_id, field, raw, detail)
    logger.warning(str(err))
    return err


def _first_error(exc: ValidationError) -> str:
    errs = exc.errors()
    if not errs:
        return "failed validation"
    e = errs[0]
    return f"{'.'.join(str(x) for x in e['loc']) or 'value'}: {e['msg']}"


class _Strict(BaseModel):
    # strict: no coercion (a bool or str pid is rejected, not cast to int).
    # extra=ignore: unknown fields (Proxmox framing) are dropped, not errors.
    model_config = ConfigDict(strict=True, extra="ignore", populate_by_name=True)


class ExecResponse(_Strict):
    """The `agent/exec` response. `pid` flows into the exec-status URL."""

    pid: StrictInt


class ExecStatusResponse(_Strict):
    """The `agent/exec-status` response.

    `exited` gates the wait loop; `exitcode` becomes a returncode and feeds
    `== 124`/`== 0` comparisons; `signal` feeds `128 + signal`. All three must be
    integers. out-data/err-data are opaque payload, not security sinks - kept as
    Any so a weird-but-honest agent isn't rejected for their type.
    """

    exited: StrictInt
    exitcode: Optional[StrictInt] = None
    signal: Optional[StrictInt] = None
    out_data: Optional[Any] = Field(default=None, alias="out-data")
    err_data: Optional[Any] = Field(default=None, alias="err-data")

    @property
    def has_err_data(self) -> bool:
        # Presence, not truthiness: an explicit empty err-data still selects the
        # wrapper-error branch, matching the original `"err-data" in status`.
        return "err-data" in self.__pydantic_fields_set__ or self.err_data is not None


def validate_exec_pid(response: Any, vm_id: Any) -> int:
    """Return the guest-agent exec pid as an int, or raise GuestAgentTamperError."""
    try:
        return ExecResponse.model_validate(response).pid
    except ValidationError as e:
        raw = response.get("pid") if isinstance(response, dict) else response
        raise _raise(vm_id, "pid", raw, _first_error(e)) from e


def validate_exec_status(status: Any, vm_id: Any) -> ExecStatusResponse:
    """Validate an exec-status response's numeric fields, or raise."""
    try:
        return ExecStatusResponse.model_validate(status)
    except ValidationError as e:
        raise _raise(vm_id, "exec-status", status, _first_error(e)) from e


def parse_return_code(raw: bytes, vm_id: Any) -> int:
    """Parse the guest's returncode file as an int, or raise GuestAgentTamperError.

    Replaces a bare `int(...)` on guest bytes, which would raise ValueError and
    surface as an unattributable crash.
    """
    text = raw.decode("utf-8", errors="replace").strip()
    try:
        return int(text)
    except ValueError as e:
        raise _raise(vm_id, "returncode", text, "not an integer") from e
