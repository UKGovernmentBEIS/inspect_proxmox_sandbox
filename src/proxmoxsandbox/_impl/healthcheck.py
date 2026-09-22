"""Compose-style guest healthcheck, polled during sample startup only.

Success latches: once the check passes the VM is ready for the purposes of
`depends_on`; there is no ongoing health monitoring.
"""

import asyncio
import itertools
import time
from dataclasses import dataclass
from logging import getLogger
from typing import Awaitable, Callable, Literal

import httpx
from inspect_ai.util import ExecResult, OutputLimitExceededError

from proxmoxsandbox._impl.agent_commands import is_transient_qga_error
from proxmoxsandbox.schema import HealthCheck

logger = getLogger(__name__)

# Consecutive guest-agent transport failures tolerated before giving up. These
# do not count towards `HealthCheck.retries`: a flaky QGA channel (Windows
# drops a few percent of calls) must not masquerade as a failing service.
# The only transport retry budget: the executor's AgentCommands does not retry.
_TRANSPORT_ERROR_LIMIT = 25

HealthCheckExecutor = Callable[[HealthCheck], Awaitable[ExecResult[str]]]


class HealthCheckFailed(RuntimeError):
    """A VM's healthcheck did not pass within its retry budget."""


@dataclass(frozen=True)
class Probe:
    """Result of one healthcheck attempt, with exceptions already classified.

    healthy:     the command exited 0.
    unhealthy:   it exited non-zero, timed out in the guest, or the guest agent
                 returned a non-transient error (counts as a failed attempt, as
                 in compose).
    unreachable: the guest agent could not be reached, per the same transient
                 classification the executor retries on; the service's state is
                 unknown, so this does not count as a failed attempt.
    """

    outcome: Literal["healthy", "unhealthy", "unreachable"]
    detail: str = ""


class HealthCheckRunner:
    """Run one VM's healthcheck to completion.

    `execute` runs `spec.test` inside the guest and returns the exec result;
    it may raise `TimeoutError` or an httpx error. `clock` and `sleep` are
    injectable so tests can run the schedule instantly.
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
        failures = 0
        transport_errors = 0
        for attempt in itertools.count(1):
            probe_started = self.clock()
            probe = await self._probe()

            if probe.outcome == "healthy":
                logger.info(f"{self.label}: healthcheck passed on attempt {attempt}")
                return

            if probe.outcome == "unreachable":
                transport_errors += 1
                logger.warning(
                    f"{self.label}: healthcheck attempt {attempt} could not reach "
                    f"the guest agent ({probe.detail}), "
                    f"{transport_errors}/{_TRANSPORT_ERROR_LIMIT}"
                )
                if transport_errors >= _TRANSPORT_ERROR_LIMIT:
                    raise HealthCheckFailed(
                        f"{self.label}: healthcheck could not reach the guest agent "
                        f"in {transport_errors} consecutive attempts "
                        f"(last error: {probe.detail})"
                    )
                await self.sleep(self.spec.interval)
                continue

            # unhealthy: the guest answered, so the transport streak is over.
            transport_errors = 0
            if probe_started - start < self.spec.start_period:
                logger.debug(
                    f"{self.label}: healthcheck attempt {attempt} failed "
                    f"({probe.detail}) during start_period; not counted"
                )
            else:
                failures += 1
                logger.info(
                    f"{self.label}: healthcheck attempt {attempt} failed "
                    f"({probe.detail}), {failures}/{self.spec.retries}"
                )
                if failures >= self.spec.retries:
                    raise HealthCheckFailed(
                        f"{self.label}: healthcheck failed {failures} consecutive "
                        f"time(s); last attempt {probe.detail}"
                    )
            await self.sleep(self.spec.interval)

    async def _probe(self) -> Probe:
        """Run the check once and classify what happened."""
        try:
            result = await self.execute(self.spec)
        except httpx.TransportError as e:
            return Probe("unreachable", type(e).__name__)
        except httpx.HTTPStatusError as e:
            if is_transient_qga_error(e):
                return Probe("unreachable", f"HTTP {e.response.status_code}")
            # A non-transient status means the agent answered: a missing or
            # unreadable `test` or result file, or an API error that will not
            # come good. Either way it is this VM's problem, so spend a retry
            # rather than the transport budget. The body can quote guest
            # output, so keep it to debug and report the status code.
            logger.debug(f"{self.label}: non-transient guest agent error: {e}")
            return Probe("unhealthy", f"HTTP {e.response.status_code}")
        except TimeoutError:
            return Probe("unhealthy", f"timed out after {self.spec.timeout}s")
        except PermissionError:
            return Probe("unhealthy", "permission denied")
        except OutputLimitExceededError:
            return Probe("unhealthy", "output limit exceeded")

        if result.success:
            return Probe("healthy")
        # Guest output may contain credentials; keep it out of exceptions
        # and INFO-level logs.
        logger.debug(
            f"{self.label}: healthcheck stdout={result.stdout!r} "
            f"stderr={result.stderr!r}"
        )
        return Probe("unhealthy", f"exit code {result.returncode}")
