"""Compose-style guest healthcheck, polled during sample startup only.

Success latches: once the check passes the VM is ready for the purposes of
`depends_on`; there is no ongoing health monitoring.
"""

import asyncio
import time
from logging import getLogger
from typing import Awaitable, Callable

import httpx
from inspect_ai.util import ExecResult

from proxmoxsandbox.schema import HealthCheck

logger = getLogger(__name__)

# Consecutive guest-agent transport failures tolerated before giving up. These
# do not count towards `HealthCheck.retries`: a flaky QGA channel (Windows
# drops a few percent of calls) must not masquerade as a failing service.
# Mirrors _QGA_MAX_RETRIES in agent_commands.py.
_TRANSPORT_ERROR_LIMIT = 25

HealthCheckExecutor = Callable[[HealthCheck], Awaitable[ExecResult[str]]]


class HealthCheckFailed(RuntimeError):
    """A VM's healthcheck did not pass within its retry budget."""


class HealthCheckRunner:
    """Run one VM's healthcheck to completion.

    `execute` runs `spec.test` inside the guest and returns the exec result;
    it may raise `TimeoutError` (counts as a failed attempt, as in compose) or an
    httpx error (transport problem; retried without consuming `retries`).
    `clock` and `sleep` are injectable so tests can run the schedule instantly.
    """

    def __init__(
        self,
        spec: HealthCheck,
        execute: HealthCheckExecutor,
        *,
        label: str,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.spec = spec
        self.execute = execute
        self.label = label
        self.clock = clock
        self.sleep = sleep

    async def run(self) -> None:
        start = self.clock()
        consecutive_failures = 0
        consecutive_transport_errors = 0
        attempt = 0
        while True:
            attempt += 1
            try:
                result = await self.execute(self.spec)
            except (httpx.TransportError, httpx.HTTPStatusError) as e:
                consecutive_transport_errors += 1
                logger.warning(
                    f"{self.label}: healthcheck attempt {attempt} could not reach "
                    f"the guest agent ({type(e).__name__}), "
                    f"{consecutive_transport_errors}/{_TRANSPORT_ERROR_LIMIT}"
                )
                if consecutive_transport_errors >= _TRANSPORT_ERROR_LIMIT:
                    raise HealthCheckFailed(
                        f"{self.label}: healthcheck could not reach the guest agent "
                        f"in {consecutive_transport_errors} consecutive attempts "
                        f"(last error: {type(e).__name__})"
                    ) from e
                await self.sleep(self.spec.interval)
                continue
            except TimeoutError:
                consecutive_transport_errors = 0
                detail = f"timed out after {self.spec.timeout}s"
            else:
                consecutive_transport_errors = 0
                if result.success:
                    logger.info(
                        f"{self.label}: healthcheck passed on attempt {attempt}"
                    )
                    return
                detail = f"exit code {result.returncode}"
                # Guest output may contain credentials; keep it out of
                # exceptions and INFO-level logs.
                logger.debug(
                    f"{self.label}: healthcheck stdout={result.stdout!r} "
                    f"stderr={result.stderr!r}"
                )

            in_start_period = self.clock() - start < self.spec.start_period
            if in_start_period:
                logger.debug(
                    f"{self.label}: healthcheck attempt {attempt} failed ({detail}) "
                    f"during start_period; not counted"
                )
            else:
                consecutive_failures += 1
                logger.info(
                    f"{self.label}: healthcheck attempt {attempt} failed ({detail}), "
                    f"{consecutive_failures}/{self.spec.retries}"
                )
                if consecutive_failures >= self.spec.retries:
                    raise HealthCheckFailed(
                        f"{self.label}: healthcheck failed {consecutive_failures} "
                        f"consecutive time(s); last attempt {detail}"
                    )
            await self.sleep(self.spec.interval)
