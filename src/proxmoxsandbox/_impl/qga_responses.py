"""Validate process and file replies from an untrusted guest agent."""

import base64
import binascii
import re
import reprlib
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

# Proxmox concatenates independently encoded chunks, preserving each padding.
# Decoding the whole string at once would stop after the first padded chunk.
_B64_SEGMENT = re.compile(rb"[A-Za-z0-9+/]+={0,2}")


class GuestAgentTamperError(Exception):
    """A guest-agent response was not what an honest qemu-ga can produce."""

    def __init__(self, vm_id: int, what: str, detail: str):
        self.vm_id = vm_id
        self.what = what
        super().__init__(
            f"VM {vm_id}: guest agent {what} response is not trustworthy: {detail}"
        )


def _short(value: Any) -> str:
    formatter = reprlib.Repr()
    formatter.maxstring = _LOGGED_VALUE_CHARS
    return formatter.repr(value)[:_LOGGED_VALUE_CHARS]


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
    # QEMU also uses this field for Windows exception codes.
    signal: Optional[StrictInt] = Field(None, ge=-(2**30), le=2**32 - 1)
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

    @field_validator("signal")
    @classmethod
    def _termination_code(cls, value: int | None) -> int | None:
        if value is not None and not (value <= 127 or value >= 0xC0000000):
            raise ValueError("must be a signal number or Windows exception code")
        return value


class FileRead(BaseModel):
    """Shape of a file-read reply before decoding its content."""

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
    """Decode independently padded chunks within the requested byte allowance."""
    what = "file-read"
    parsed = parse_guest(FileRead, vm_id, what, data)
    if len(parsed.content) > 4 * count:
        raise tamper(vm_id, what, "encoded content exceeds the requested size")
    try:
        encoded = parsed.content.encode("ascii")
    except UnicodeEncodeError:
        raise tamper(vm_id, what, "content is not ASCII", parsed.content) from None
    raw = bytearray()
    offset = 0
    try:
        for match in _B64_SEGMENT.finditer(encoded):
            if match.start() != offset:
                raise tamper(vm_id, what, "content is not base64", parsed.content)
            segment = match.group()
            if len(segment) % 4:
                raise tamper(vm_id, what, "content is not base64", parsed.content)
            decoded_size = len(segment) // 4 * 3 - (
                len(segment) - len(segment.rstrip(b"="))
            )
            if len(raw) + decoded_size > count:
                raise tamper(vm_id, what, "content exceeds the requested size")
            raw.extend(base64.b64decode(segment, validate=True))
            offset = match.end()
    except binascii.Error as ex:
        raise tamper(vm_id, what, f"content is not base64 ({ex})", parsed.content)
    if offset != len(encoded):
        raise tamper(vm_id, what, "content is not base64", parsed.content)
    truncated = bool(parsed.truncated)
    if truncated != (len(raw) == count):
        raise tamper(
            vm_id, what, f"truncated={truncated} with {len(raw)} of {count} bytes"
        )
    return bytes(raw), truncated
