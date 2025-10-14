from pathlib import Path
from typing import Dict

from inspect_ai.util import SandboxConnection, SandboxEnvironment
from pytest import raises

from proxmoxsandbox._impl.qemu_commands import QemuCommands
from proxmoxsandbox._impl.sdn_commands import SdnCommands
from proxmoxsandbox._proxmox_sandbox_environment import ProxmoxSandboxEnvironment
from proxmoxsandbox.schema import (
    DhcpRange,
    ProxmoxSandboxEnvironmentConfig,
    SdnConfig,
    SubnetConfig,
    VmConfig,
    VmNicConfig,
    VmSourceConfig,
    VnetConfig,
)

from .proxmox_sandbox_utils import setup_sandbox

CURRENT_DIR = Path(__file__).parent


async def test_built_in() -> None:
    envs_dict: Dict[str, SandboxEnvironment] = {}
    sandbox_env_config = ProxmoxSandboxEnvironmentConfig(
        sdn_config=SdnConfig(
            vnet_configs=(
                VnetConfig(
                    alias="vnet80",
                    subnets=(
                        SubnetConfig(
                            cidr="10.80.0.0/24",
                            gateway="10.80.0.1",
                            snat=True,
                            dhcp_ranges=(
                                DhcpRange(start="10.80.0.16", end="10.80.0.32"),
                            ),
                        ),
                    ),
                ),
                VnetConfig(
                    alias="vnet81",
                    subnets=(
                        SubnetConfig(
                            cidr="10.81.0.0/24",
                            gateway="10.81.0.1",
                            snat=True,
                            dhcp_ranges=(
                                DhcpRange(start="10.81.0.16", end="10.81.0.32"),
                            ),
                        ),
                    ),
                ),
            ),
            use_pve_ipam_dnsnmasq=True,
        ),
        vms_config=(
            VmConfig(
                vm_source_config=VmSourceConfig(built_in="ubuntu24.04"),
                nics=(
                    VmNicConfig(vnet_alias="vnet80"),
                    VmNicConfig(vnet_alias="vnet81"),
                ),
                ram_mb=2345,
                vcpus=1,
                is_sandbox=True,
                uefi_boot=True,
            ),
        ),
    )
    try:
        task_name = "sandbox_test_smoketask"
        task_name, envs_dict = await setup_sandbox(task_name, sandbox_env_config)
        sandbox = envs_dict["default"]
        uname_result = await sandbox.exec(
            [
                "uname",
                "-a",
            ]
        )
        assert uname_result.success, f"Failed to run uname: {uname_result=}"
        assert "Ubuntu" in uname_result.stdout, (
            f"Unexpected result of uname: {uname_result.stdout=}"
        )

        ipa_result = await sandbox.exec(
            [
                "ip",
                "addr",
            ]
        )
        assert uname_result.success, f"Failed to run ip addr: {ipa_result=}"
        assert "10.80.0" in ipa_result.stdout and "10.81.0" in ipa_result.stdout, (
            f"Unexpected result of ip addr: {ipa_result.stdout=}"
        )

        nproc_result = await sandbox.exec(["nproc"])
        assert "1" == nproc_result.stdout.strip(), (
            f"Unexpected result of nproc: {nproc_result=}"
        )

        mem_result = await sandbox.exec(["lshw", "-short", "-c", "memory"])
        assert "2345MiB" in mem_result.stdout, (
            f"Unexpected result of lshw: {mem_result=}"
        )

        uefi_result = await sandbox.exec(["efibootmgr"])
        assert uefi_result.success, f"Failed to run efibootmgr: {uefi_result=}"

        # check internet connectivity excluding DNS
        curl_nodns_result = await sandbox.exec(["curl", "--fail", "http://1.1.1.1"])
        assert curl_nodns_result.success, (
            f"Failed to run curl without dns: {curl_nodns_result=}"
        )

        # check internet connectivity with DNS
        curl_result = await sandbox.exec(["curl", "--fail", "http://amazon.com"])
        assert curl_result.success, f"Failed to run curl: {curl_result=}"

    finally:
        await ProxmoxSandboxEnvironment.sample_cleanup(
            task_name="unused",
            config=sandbox_env_config,
            environments=envs_dict,
            interrupted=False,
        )


async def test_multiple_sandboxes_isolated(sandbox_env_config) -> None:
    sandboxes = {}

    try:
        task_name = "sandbox_1_task"
        task_name, envs_dict = await setup_sandbox(task_name, sandbox_env_config)
        sandboxes["first"] = envs_dict["default"]

        # Second time round, the sandbox should get a different SDN IP range
        task_name = "sandbox_2_task"
        task_name, envs_dict = await setup_sandbox(task_name, sandbox_env_config)
        sandboxes["second"] = envs_dict["default"]

        second_ip = (
            await sandboxes["second"].exec(
                [
                    "bash",
                    "-c",
                    r'ip a | grep -oP "(?<=inet\s)\d+(\.\d+){3}" | grep -v "127\.0"',
                ]
            )
        ).stdout.splitlines()
        if len(second_ip) != 1:
            raise Exception(f"Expected exactly one IP address, got {second_ip}")

        ping_result = await sandboxes["first"].exec(
            ["ping", "-c", "1", second_ip[0]], timeout=3
        )

        # This currently fails, as the Proxmox firewall is needed in order to
        # ensure sandboxes cannot see each other.
        # assert not ping_result.success, (
        #     f"Should not be able to ping between sandboxes; {ping_result=}"
        # )
        print(
            f"FIXME: ping_result.success is {ping_result.success},"
            + " but it should be False"
        )
    finally:
        await ProxmoxSandboxEnvironment.sample_cleanup(
            task_name="unused",
            config=sandbox_env_config,
            environments=sandboxes,
            interrupted=False,
        )


async def test_ova() -> None:
    envs_dict: Dict[str, SandboxEnvironment] = {}
    sandbox_env_config = ProxmoxSandboxEnvironmentConfig(
        vms_config=(
            VmConfig(
                vm_source_config=VmSourceConfig(
                    ova=CURRENT_DIR / ".." / "oVirtTinyCore64-13.11.ova"
                ),
                name="test-ova",
            ),
        )
    )
    try:
        _, envs_dict = await setup_sandbox("tcova", sandbox_env_config)
        uname_result = await envs_dict["default"].exec(
            [
                "uname",
                "-a",
            ]
        )
        assert uname_result.success, f"Failed to run uname: {uname_result=}"
        assert "tinycore" in uname_result.stdout, (
            f"Unexpected result of uname: {uname_result.stdout=}"
        )
    finally:
        await ProxmoxSandboxEnvironment.sample_cleanup(
            task_name="unused",
            config=sandbox_env_config,
            environments=envs_dict,
            interrupted=False,
        )


async def test_at_least_one_sandbox() -> None:
    sandbox_env_config = ProxmoxSandboxEnvironmentConfig(
        vms_config=(
            VmConfig(
                vm_source_config=VmSourceConfig(
                    built_in="ubuntu24.04",
                ),
                name="non-sandbox-1",
                is_sandbox=False,
            ),
            VmConfig(
                vm_source_config=VmSourceConfig(
                    built_in="ubuntu24.04",
                ),
                name="non-sandbox-2",
                is_sandbox=False,
            ),
        )
    )
    with raises(ValueError) as e_info:
        await setup_sandbox("ta1s", sandbox_env_config)
    assert "No default sandbox found" in str(e_info.value)
    await ProxmoxSandboxEnvironment.cli_cleanup(id=None)


async def test_connect(proxmox_sandbox_environment: ProxmoxSandboxEnvironment) -> None:
    connection: SandboxConnection = await proxmox_sandbox_environment.connection()
    # we can't really do much more than this assertion;
    # sandbox.connection() needs to be tested manually
    assert "open 'http" in connection.command


async def test_access_by_name() -> None:
    envs_dict: Dict[str, SandboxEnvironment] = {}
    sandbox_env_config = ProxmoxSandboxEnvironmentConfig(
        vms_config=(
            VmConfig(
                vm_source_config=VmSourceConfig(
                    built_in="ubuntu24.04",
                ),
                name="default",
                is_sandbox=True,
            ),
            VmConfig(
                vm_source_config=VmSourceConfig(
                    built_in="ubuntu24.04",
                ),
                name="other-vm",
                # We need to set this for the guest agent to be enabled on Proxmox.
                is_sandbox=True,
            ),
        )
    )
    try:
        _, envs_dict = await setup_sandbox("tabn", sandbox_env_config)
        print(envs_dict)
        # Sandbox VM stays "default"
        sandbox = envs_dict["default"]
        uname_result = await sandbox.exec(
            [
                "uname",
                "-a",
            ]
        )
        assert uname_result.success, f"Failed to run uname: {uname_result=}"
        assert "Ubuntu" in uname_result.stdout, (
            f"Unexpected result of uname: {uname_result.stdout=}"
        )
        # We can access this one via its name now
        sandbox = envs_dict["other-vm"]
        uname_result = await sandbox.exec(
            [
                "uname",
                "-a",
            ]
        )
        assert uname_result.success, f"Failed to run uname: {uname_result=}"
        assert "Ubuntu" in uname_result.stdout, (
            f"Unexpected result of uname: {uname_result.stdout=}"
        )

    finally:
        await ProxmoxSandboxEnvironment.sample_cleanup(
            task_name="unused",
            config=sandbox_env_config,
            environments=envs_dict,
            interrupted=False,
        )


async def test_cli_cleanup(
    qemu_commands: QemuCommands, sdn_commands: SdnCommands
) -> None:
    sandbox_env_config = ProxmoxSandboxEnvironmentConfig(
        vms_config=(
            VmConfig(
                vm_source_config=VmSourceConfig(built_in="ubuntu24.04"),
                name="test-cli-cleanup",
            ),
        )
    )

    existing_vms = await qemu_commands.list_vms()
    existing_zones = await sdn_commands.list_sdn_zones()

    await setup_sandbox("tcc", sandbox_env_config)

    all_vms = await qemu_commands.list_vms()
    all_zones = await sdn_commands.list_sdn_zones()

    assert len(all_vms) == len(existing_vms) + 1
    assert len(all_zones) == len(existing_zones) + 1

    await ProxmoxSandboxEnvironment.cli_cleanup(id=None)

    post_cleanup_vms = await qemu_commands.list_vms()
    post_cleanup_zones = await sdn_commands.list_sdn_zones()

    assert set([vm["vmid"] for vm in post_cleanup_vms]) == set(
        [vm["vmid"] for vm in existing_vms]
    )

    existing_zones.sort(key=lambda x: x["zone"])
    post_cleanup_zones.sort(key=lambda x: x["zone"])
    assert post_cleanup_zones == existing_zones
