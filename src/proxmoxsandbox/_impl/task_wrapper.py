import abc
import asyncio
from logging import getLogger
from typing import Awaitable, Callable

import tenacity

from proxmoxsandbox._impl.async_proxmox import AsyncProxmoxAPI


class TaskWrapper(abc.ABC):
    logger = getLogger(__name__)

    async_proxmox: AsyncProxmoxAPI

    def __init__(self, async_proxmox: AsyncProxmoxAPI):
        self.async_proxmox = async_proxmox

    async def do_action_and_wait_for_tasks(
        self, the_action: Callable[[], Awaitable[None]], async_wait_seconds: int = 2
    ) -> None:
        incomplete_tasks_pre_action = await self.new_incomplete_tasks(
            pre_existing_incomplete_tasks=[]
        )

        await the_action()

        # Sometimes the resulting server-side tasks don't turn up immediately
        await asyncio.sleep(async_wait_seconds)

        @tenacity.retry(
            wait=tenacity.wait_exponential(min=0.1, exp_base=1.3),
            stop=tenacity.stop_after_delay(1200),
            retry=tenacity.retry_if_result(lambda x: x is False),
        )
        async def new_tasks_are_complete() -> bool:
            post_action_current_tasks = await self.new_incomplete_tasks(
                pre_existing_incomplete_tasks=incomplete_tasks_pre_action
            )
            return not post_action_current_tasks

        await new_tasks_are_complete()

    async def new_incomplete_tasks(self, pre_existing_incomplete_tasks):
        current_tasks = await self.async_proxmox.request("GET", "/cluster/tasks")

        current_incomplete_tasks = [
            current_task
            for current_task in current_tasks
            if (
                ("status" in current_task and current_task["status"] != "OK")
                or "status" not in current_task
            )
        ]

        new_tasks = [
            current_incomplete_task
            for current_incomplete_task in current_incomplete_tasks
            if not any(
                pre_existing_task["upid"] == current_incomplete_task["upid"]
                for pre_existing_task in pre_existing_incomplete_tasks
            )
        ]
        return new_tasks
