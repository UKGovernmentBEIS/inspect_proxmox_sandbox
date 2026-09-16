"""Validation boundary for values that originate inside the guest.

Root in a sandbox VM can replace qemu-ga and answer any agent command with
anything; Proxmox passes the reply through without enforcing its declared
return schema. Every field read from a guest-agent response goes through the
models here before it is used, so a lie fails as GuestAgentTamperError
(logged with the VM and the offending value) instead of as a stray KeyError,
TypeError or a value smuggled into a URL.

Bounds are "what an honest agent can produce", not "what we would like":
anything beyond them is tampering, not load.
"""

import base64
import binascii
import re
from logging import getLogger
from typing import Any, Optional, Tuple, Type, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
)

logger = getLogger(__name__)

# qemu-ga caps captured exec output at 16 MiB per stream (GUEST_EXEC_MAX_OUTPUT).
MAX_EXEC_DATA_CHARS = 16 * 1024**2

_LOGGED_VALUE_CHARS = 200

# decode=0 file-read content is the concatenation of each ~1 MiB chunk's own
# base64 (each keeps its padding), so it's a sequence of padded groups, not
# one base64 string.
_B64_SEGMENT = re.compile(rb"[A-Za-z0-9+/]+={0,2}")
_B64_CONCATENATION = re.compile(rb"(?:[A-Za-z0-9+/]+={0,2})*")


class GuestAgentTamperError(Exception):
    """A guest-agent response was not what an honest qemu-ga can produce."""

    def __init__(self, vm_id: int, what: str, detail: str):
        self.vm_id = vm_id
        self.what = what
        super().__init__(
            f"VM {vm_id}: guest agent {what} response is not trustworthy: {detail}"
        )


def _short(value: Any) -> str:
    text = repr(value)
    if len(text) > _LOGGED_VALUE_CHARS:
        text = f"{text[:_LOGGED_VALUE_CHARS]}... ({len(text)} chars)"
    return text


def tamper(
    vm_id: int, what: str, detail: str, value: Any = None
) -> GuestAgentTamperError:
    if value is not None:
        detail = f"{detail}: {_short(value)}"
    logger.warning("VM %s: guest agent %s response rejected: %s", vm_id, what, detail)
    return GuestAgentTamperError(vm_id, what, detail)


_M = TypeVar("_M", bound=BaseModel)


def parse_guest(model: Type[_M], vm_id: int, what: str, data: Any) -> _M:
    if not isinstance(data, dict):
        raise tamper(vm_id, what, "not an object", data)
    try:
        return model.model_validate(data)
    except ValidationError as ex:
        detail = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
            for err in ex.errors()
        )
        raise tamper(vm_id, what, detail, data) from None


def _int_flag(value: Any) -> int:
    # PVE converts JSON booleans to 1/0 for exec-status but not elsewhere,
    # so accept both, and only those.
    if isinstance(value, (bool, int)) and value in (0, 1):
        return int(value)
    raise ValueError("must be 0 or 1")


class ExecReturn(BaseModel):
    """`guest-exec` return."""

    model_config = ConfigDict(extra="ignore")

    # Goes straight into the exec-status URL, hence strict: no bool, no str.
    pid: StrictInt = Field(gt=0, le=2**31 - 1)


class ExecStatus(BaseModel):
    """`guest-exec-status` return (after PVE's base64-decode of the data fields)."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    exited: int
    # Any 32-bit process exit code (Windows uses the full DWORD range).
    exitcode: Optional[StrictInt] = Field(None, ge=-(2**31), le=2**32 - 1)
    # Linux signals are 1..64; 128 + signal must stay a valid exit code.
    signal: Optional[StrictInt] = Field(None, ge=0, le=127)
    out_data: Optional[StrictStr] = Field(
        None, alias="out-data", max_length=MAX_EXEC_DATA_CHARS
    )
    err_data: Optional[StrictStr] = Field(
        None, alias="err-data", max_length=MAX_EXEC_DATA_CHARS
    )

    @field_validator("exited", mode="before")
    @classmethod
    def _exited_flag(cls, value: Any) -> int:
        return _int_flag(value)


class FileRead(BaseModel):
    """`agent/file-read` return with decode=0."""

    model_config = ConfigDict(extra="ignore")

    content: StrictStr = ""
    truncated: int = 0

    @field_validator("truncated", mode="before")
    @classmethod
    def _truncated_flag(cls, value: Any) -> int:
        if value is None:
            return 0
        return _int_flag(value)


def decode_file_read(vm_id: int, data: Any, count: int) -> Tuple[bytes, bool]:
    """Validate and decode a decode=0 file-read for a request of `count` bytes.

    PVE reads until `count` bytes or EOF and flags `truncated` iff EOF was
    never seen, so an honest reply has `len(content) <= count` and is flagged
    exactly when it filled `count`. Verified live on PVE 9.2 with files of
    count-1, count and count+1 bytes.
    """
    what = "file-read"
    parsed = parse_guest(FileRead, vm_id, what, data)
    try:
        encoded = parsed.content.encode("ascii")
    except UnicodeEncodeError:
        raise tamper(vm_id, what, "content is not ASCII", parsed.content) from None
    if _B64_CONCATENATION.fullmatch(encoded) is None:
        raise tamper(vm_id, what, "content is not base64", parsed.content)
    try:
        raw = b"".join(
            base64.b64decode(seg, validate=True)
            for seg in _B64_SEGMENT.findall(encoded)
        )
    except binascii.Error as ex:
        raise tamper(vm_id, what, f"content is not base64 ({ex})", parsed.content)
    if len(raw) > count:
        raise tamper(vm_id, what, f"{len(raw)} bytes returned for a {count}-byte read")
    truncated = bool(parsed.truncated)
    if truncated != (len(raw) == count):
        raise tamper(
            vm_id, what, f"truncated={truncated} with {len(raw)} of {count} bytes"
        )
    return raw, truncated
