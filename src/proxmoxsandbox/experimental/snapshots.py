from inspect_ai import Task, eval, task
from inspect_ai.approval import ApprovalPolicy, auto_approver, human_approver
from inspect_ai.dataset import Sample
from inspect_ai.model import ModelOutput, get_model
from inspect_ai.scorer import includes
from inspect_ai.solver import basic_agent
from inspect_ai.tool import Tool, bash, tool
from inspect_ai.util import SandboxEnvironmentSpec, sandbox, store

from proxmoxsandbox._proxmox_sandbox_environment import (
    ProxmoxSandboxEnvironment,
    ProxmoxSandboxEnvironmentConfig,
)

example_dataset = [
    Sample(
        input="""
sample text
        """,
        target="42",
    ),
]


@tool
def create_snapshot() -> Tool:
    async def do_create_snapshot() -> int:
        """
        Use this function to create a snapshot of your sandbox.

        Returns:
          The number of snapshot, which can be used to rollback
        """
        current_snapshot_id = store().get("current_snapshot_id", 0)
        new_snapshot_id = current_snapshot_id + 1
        try:
            await (
                sandbox()
                .as_type(ProxmoxSandboxEnvironment)
                .create_snapshot(f"inspect{new_snapshot_id}")
            )
        except TypeError as e:
            raise ValueError("This tool only works with VM sandboxes") from e
        return new_snapshot_id

    return do_create_snapshot


@tool
def rollback_to_snapshot() -> Tool:
    async def do_rollback_to_snapshot(snapshot_id: int) -> bool:
        """
        Use this function to roll back to a previous snapshot of your sandbox.

        Args:
            snapshot_id (int): id of the previous snapshot
        Returns:
            bool: Always True
        """
        try:
            await (
                sandbox()
                .as_type(ProxmoxSandboxEnvironment)
                .restore_snapshot(f"inspect{snapshot_id}")
            )
        except TypeError as e:
            raise ValueError("This tool only works with VM sandboxes") from e

        return True

    return do_rollback_to_snapshot


@task
def try_snapshots() -> Task:
    return Task(
        dataset=example_dataset,
        solver=[
            basic_agent(
                tools=[bash(), create_snapshot(), rollback_to_snapshot()],
                message_limit=20,
            ),
        ],
        scorer=includes(),
        sandbox=SandboxEnvironmentSpec(
            type="proxmox",
            config=ProxmoxSandboxEnvironmentConfig(),
        ),
    )


if __name__ == "__main__":
    eval(
        tasks=[try_snapshots()],
        model=get_model(
            "mockllm/model",
            custom_outputs=[
                ModelOutput.for_tool_call(
                    model="mockllm/model",
                    tool_name="bash",
                    tool_arguments={
                        "cmd": "ls -d /f*"
                    },  # the file doesn't exist, so this
                    # will return 'No such file or directory'
                ),
                ModelOutput.for_tool_call(
                    model="mockllm/model",
                    tool_name="create_snapshot",
                    tool_arguments={},
                ),
                ModelOutput.for_tool_call(
                    model="mockllm/model",
                    tool_name="bash",
                    tool_arguments={"cmd": "touch /football_result"},
                ),
                ModelOutput.for_tool_call(
                    model="mockllm/model",
                    tool_name="bash",
                    tool_arguments={"cmd": "ls -d /f*"},
                ),
                ModelOutput.for_tool_call(
                    model="mockllm/model",
                    tool_name="rollback_to_snapshot",
                    tool_arguments={"snapshot_id": 1},
                ),
                ModelOutput.for_tool_call(
                    model="mockllm/model",
                    tool_name="bash",
                    tool_arguments={
                        "cmd": "ls -d /f*"
                    },  # this will again show 'No such file or directory' since
                    # we're at the previous snapshot
                ),
                ModelOutput.for_tool_call(
                    model="mockllm/model",
                    tool_name="submit",
                    tool_arguments={"answer": "42"},
                ),
            ],
        ),
        log_level="trace",
        approval=[
            ApprovalPolicy(
                human_approver(), ["create_snapshot", "rollback_to_snapshot", "submit"]
            ),
            ApprovalPolicy(auto_approver(), "*"),
        ],
        # sandbox_cleanup=False
    )
