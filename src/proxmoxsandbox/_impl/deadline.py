"""Elapsed-time limits for guest communication."""

import asyncio
from contextvars import ContextVar
from typing import Any, Coroutine, TypeVar

_T = TypeVar("_T")
_deadline: ContextVar[float | None] = ContextVar("guest_agent_deadline", default=None)


def remaining_budget() -> float | None:
    """Return the enclosing operation's remaining allowance, if any."""
    deadline = _deadline.get()
    return (
        None
        if deadline is None
        else max(0, deadline - asyncio.get_running_loop().time())
    )


async def within_budget(
    operation: Coroutine[Any, Any, _T], seconds: float, *, independent: bool = False
) -> _T:
    """Cancel overdue work and reject results delivered after the deadline."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    parent = None if independent else _deadline.get()
    if parent is not None:
        deadline = min(deadline, parent)
    remaining = deadline - loop.time()
    if remaining <= 0:
        operation.close()
        raise TimeoutError("Operation exceeded its waiting allowance")
    token = _deadline.set(deadline)
    try:
        try:
            result = await asyncio.wait_for(operation, remaining)
        except asyncio.TimeoutError as exc:
            raise TimeoutError("Operation exceeded its waiting allowance") from exc
        if loop.time() >= deadline:
            raise TimeoutError("Operation exceeded its waiting allowance")
        return result
    finally:
        _deadline.reset(token)
