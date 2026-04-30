"""End-to-end test for OPNsense domain-whitelist gateway.

This test creates the full OPNsense + agent topology and verifies that:
- The agent VM can reach whitelisted domains (ifconfig.me)
- The agent VM cannot reach non-whitelisted domains (google.com)

First run is slow (~8 min) as it creates the utility VM and downloads the
OPNsense image. Subsequent runs reuse cached templates (~2 min).
"""

from ipaddress import ip_address, ip_network

from inspect_ai import Task, eval
from inspect_ai.dataset import Sample
from inspect_ai.model import ModelOutput, get_model
from inspect_ai.scorer import includes
from inspect_ai.solver import basic_agent
from inspect_ai.tool import bash
from inspect_ai.util import SandboxEnvironmentSpec

from proxmoxsandbox._proxmox_sandbox_environment import (
    ProxmoxSandboxEnvironmentConfig,
)
from proxmoxsandbox.schema import (
    DhcpRange,
    SdnConfig,
    SubnetConfig,
    VmConfig,
    VmNicConfig,
    VmSourceConfig,
    VnetConfig,
)


def test_opnsense_domain_filtering() -> None:
    eval_logs = eval(
        tasks=[
            Task(
                dataset=[
                    Sample(
                        input="Test connectivity",
                        target="DONE",
                    ),
                ],
                solver=[
                    basic_agent(
                        tools=[bash(timeout=180)],
                        message_limit=20,
                    ),
                ],
                scorer=includes(),
                sandbox=SandboxEnvironmentSpec(
                    type="proxmox",
                    config=ProxmoxSandboxEnvironmentConfig(
                    vms_config=(
                        # Agent sandbox on LAN behind OPNsense
                        VmConfig(
                            vm_source_config=VmSourceConfig(built_in="ubuntu24.04"),
                            name="agent",
                            ram_mb=512,
                            vcpus=1,
                            nics=(VmNicConfig(vnet_alias="lan"),),
                        ),
                    ),
                    sdn_config=SdnConfig(
                        vnet_configs=(
                            VnetConfig(
                                alias="wan",
                                subnets=(
                                    SubnetConfig(
                                        cidr=ip_network("10.0.1.0/24"),
                                        gateway=ip_address("10.0.1.1"),
                                        snat=True,
                                        dhcp_ranges=(
                                            DhcpRange(
                                                start=ip_address("10.0.1.50"),
                                                end=ip_address("10.0.1.100"),
                                            ),
                                        ),
                                    ),
                                ),
                            ),
                            # LAN: OPNsense manages this VNet
                            VnetConfig(
                                alias="lan",
                                subnets=(
                                    SubnetConfig(
                                        cidr=ip_network("10.0.2.0/24"),
                                        gateway=ip_address("10.0.2.1"),
                                        vnet_type="opnsense",
                                        domain_whitelist=(
                                            "ifconfig.me",
                                            "api.ipify.org",
                                        ),
                                        dhcp_ranges=(
                                            DhcpRange(
                                                start=ip_address("10.0.2.50"),
                                                end=ip_address("10.0.2.200"),
                                            ),
                                        ),
                                    ),
                                ),
                            ),
                        ),
                        use_pve_ipam_dnsnmasq=True,
                    ),
                    ),
                ),
            )
        ],
        model=get_model(
            "mockllm/model",
            custom_outputs=[
                # Step 0: Wait for OPNsense to boot and DHCP/DNS to be ready.
                # OPNsense takes ~60s to boot; retry curl until it works.
                ModelOutput.for_tool_call(
                    model="mockllm/model",
                    tool_name="bash",
                    tool_arguments={
                        "cmd": (
                            "for i in $(seq 1 30); do "
                            "  result=$(curl -s --connect-timeout 5 ifconfig.me"
                            " 2>/dev/null); "
                            '  if [ -n "$result" ]; '
                            'then echo "READY after $i attempts"; exit 0; fi; '
                            "  sleep 5; "
                            "done; echo TIMEOUT"
                        )
                    },
                ),
                # Test 1: curl a whitelisted domain
                ModelOutput.for_tool_call(
                    model="mockllm/model",
                    tool_name="bash",
                    tool_arguments={
                        "cmd": "curl -s --connect-timeout 15 ifconfig.me"
                    },
                ),
                # Test 2: curl a blocked domain (should timeout)
                ModelOutput.for_tool_call(
                    model="mockllm/model",
                    tool_name="bash",
                    tool_arguments={
                        "cmd": (
                            "curl -s --connect-timeout 10 https://google.com;"
                            " echo EXIT_CODE=$?"
                        )
                    },
                ),
                # Test 3: curl another whitelisted domain
                ModelOutput.for_tool_call(
                    model="mockllm/model",
                    tool_name="bash",
                    tool_arguments={
                        "cmd": "curl -s --connect-timeout 15 https://api.ipify.org"
                    },
                ),
                # Test 4: curl a bare IP (should be blocked — not in whitelist)
                ModelOutput.for_tool_call(
                    model="mockllm/model",
                    tool_name="bash",
                    tool_arguments={
                        "cmd": (
                            "curl -s --connect-timeout 10 http://1.1.1.1;"
                            " echo EXIT_CODE=$?"
                        )
                    },
                ),
                # Submit
                ModelOutput.for_tool_call(
                    model="mockllm/model",
                    tool_name="submit",
                    tool_arguments={"answer": "DONE"},
                ),
            ],
        ),
        log_level="trace",
        display="plain",
        sandbox_cleanup=True,
    )

    assert len(eval_logs) == 1
    log = eval_logs[0]
    assert log.error is None, f"Eval error: {log.error}"
    assert log.samples
    sample = log.samples[0]

    tool_calls = [x for x in sample.messages if x.role == "tool"]
    assert len(tool_calls) >= 5, (
        f"Expected at least 5 tool calls, got {len(tool_calls)}"
    )

    # Tool call 0: wait for OPNsense to be ready
    readiness_result = tool_calls[0].text
    print(f"Readiness check: {readiness_result!r}")
    assert "READY" in readiness_result, (
        f"OPNsense did not become ready: {readiness_result}"
    )

    # Tool call 1: curl ifconfig.me — should return an IP address
    ifconfig_result = tool_calls[1].text
    print(f"ifconfig.me result: {ifconfig_result!r}")
    assert any(c.isdigit() for c in ifconfig_result), (
        f"Expected ifconfig.me to return an IP, got: {ifconfig_result}"
    )

    # Tool call 2: curl google.com — should fail/timeout
    google_result = tool_calls[2].text
    print(f"google.com result: {google_result!r}")
    # curl exit code 6 = DNS resolution refused (Unbound whitelist),
    # 7 = connection refused, 28 = timeout
    assert (
        "EXIT_CODE=28" in google_result
        or "EXIT_CODE=7" in google_result
        or "EXIT_CODE=6" in google_result
        or "timed out" in google_result.lower()
    ), f"Expected google.com to be blocked, got: {google_result}"

    # Tool call 3: curl api.ipify.org — should return an IP
    ipify_result = tool_calls[3].text
    print(f"api.ipify.org result: {ipify_result!r}")
    assert any(c.isdigit() for c in ipify_result), (
        f"Expected api.ipify.org to return an IP, got: {ipify_result}"
    )

    # Tool call 4: curl bare IP 1.1.1.1 — should be blocked
    bare_ip_result = tool_calls[4].text
    print(f"1.1.1.1 result: {bare_ip_result!r}")
    assert (
        "EXIT_CODE=28" in bare_ip_result
        or "EXIT_CODE=7" in bare_ip_result
        or "timed out" in bare_ip_result.lower()
    ), f"Expected bare IP 1.1.1.1 to be blocked, got: {bare_ip_result}"
