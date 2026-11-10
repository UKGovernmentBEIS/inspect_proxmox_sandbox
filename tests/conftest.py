# Options live here, not in proxmoxsandboxtest/conftest.py: pytest only reads
# command-line options from conftests it loads before parsing arguments, and
# for `pytest tests` that stops at this directory.
import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--runslow",
        action="store_true",
        default=False,
        help="also run tests marked `slow` (e.g. the Kali built-in)",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    if config.getoption("--runslow"):
        return
    skip_slow = pytest.mark.skip(reason="marked slow; pass --runslow to run")
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip_slow)
