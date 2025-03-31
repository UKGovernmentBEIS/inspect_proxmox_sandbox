from pathlib import Path

from inspect_ai import Task, eval, task
from inspect_ai.dataset import Sample
from inspect_ai.model import ModelOutput, get_model
from inspect_ai.scorer import includes
from inspect_ai.solver import basic_agent
from inspect_ai.tool import bash

CURRENT_DIR = Path(__file__).parent


@task
def task_for_test() -> Task:
    return Task(
        dataset=[
            Sample(
                input="""
sample text
        """,
                target="42",
            ),
        ],
        solver=[
            basic_agent(
                tools=[bash()],
                message_limit=20,
            ),
        ],
        scorer=includes(),
        sandbox="proxmox",
    )


def test_inspect_eval() -> None:
    eval_logs = eval(
        tasks=[task_for_test()],
        model=get_model(
            "mockllm/model",
            custom_outputs=[
                ModelOutput.for_tool_call(
                    model="mockllm/model",
                    tool_name="bash",
                    tool_arguments={"cmd": "uname -a"},
                ),
                ModelOutput.for_tool_call(
                    model="mockllm/model",
                    tool_name="submit",
                    tool_arguments={"answer": "42"},
                ),
            ],
        ),
        log_level="trace",
        # sandbox_cleanup=False
    )

    assert len(eval_logs) == 1
    assert eval_logs[0]
    assert eval_logs[0].error is None
    assert eval_logs[0].samples
    sample = eval_logs[0].samples[0]
    tool_calls = [x for x in sample.messages if x.role == "tool"]
    assert "ubuntu" in tool_calls[0].text
