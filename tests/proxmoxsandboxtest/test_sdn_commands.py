from pytest import raises

from proxmoxsandbox._impl.sdn_commands import SdnCommands
from proxmoxsandbox.schema import DhcpRange, SdnConfig, SubnetConfig, VnetConfig


async def test_create_sdn_no_vnets(ids_start: str, sdn_commands: SdnCommands) -> None:
    with raises(ValueError) as e_info:
        await sdn_commands.create_sdn(
            proxmox_ids_start=ids_start,
            sdn_config=SdnConfig(vnet_configs=(), use_pve_ipam_dnsnmasq=False),
        )
    assert "No vnets provided" in str(e_info.value)


async def test_create_sdn_with_vnets(ids_start: str, sdn_commands: SdnCommands) -> None:
    sdn_zone_id, vnet_aliases = await sdn_commands.create_sdn(
        proxmox_ids_start=ids_start,
        sdn_config=SdnConfig(
            vnet_configs=(
                VnetConfig(),
                VnetConfig(alias="test_create_sdn_with_vnets"),
            ),
            use_pve_ipam_dnsnmasq=False,
        ),
    )

    assert sdn_zone_id is not None
    assert len(vnet_aliases) == 2
    assert vnet_aliases[0][1] is None
    assert vnet_aliases[1][1] == "test_create_sdn_with_vnets"

    await sdn_commands.tear_down_sdn_zone_and_vnet(sdn_zone_id)


async def test_create_sdn_with_vnets_and_subnet(
    ids_start: str, sdn_commands: SdnCommands
) -> None:
    sdn_zone_id, vnet_aliases = await sdn_commands.create_sdn(
        proxmox_ids_start=ids_start,
        sdn_config=SdnConfig(
            vnet_configs=(
                VnetConfig(
                    subnets=(
                        SubnetConfig(
                            cidr="10.32.32.0/24",
                            gateway="10.32.32.1",
                            snat=True,
                            dhcp_ranges=(),
                        ),
                    )
                ),
            ),
            use_pve_ipam_dnsnmasq=False,
        ),
    )

    assert sdn_zone_id is not None
    assert len(vnet_aliases) == 1

    await sdn_commands.tear_down_sdn_zone_and_vnet(sdn_zone_id)


async def test_inconsistent_ipam_setting_true_but_no_dhcp(
    ids_start: str,
    sdn_commands: SdnCommands,
) -> None:
    with raises(ValueError) as e_info:
        await sdn_commands.create_sdn(
            proxmox_ids_start=ids_start,
            sdn_config=SdnConfig(
                vnet_configs=(VnetConfig(),),
                use_pve_ipam_dnsnmasq=True,
            ),
        )
    assert "use_pve_ipam_dnsnmasq" in str(e_info.value)


async def test_inconsistent_ipam_setting_false_but_dhcp(
    ids_start: str,
    sdn_commands: SdnCommands,
) -> None:
    with raises(ValueError) as e_info:
        await sdn_commands.create_sdn(
            proxmox_ids_start=ids_start,
            sdn_config=SdnConfig(
                vnet_configs=(
                    VnetConfig(
                        subnets=(
                            SubnetConfig(
                                cidr="10.32.32.0/24",
                                gateway="10.32.32.1",
                                snat=False,
                                dhcp_ranges=(
                                    DhcpRange(start="10.32.32.16", end="10.32.32.32"),
                                ),
                            ),
                        )
                    ),
                ),
                use_pve_ipam_dnsnmasq=False,
            ),
        )
    assert "use_pve_ipam_dnsnmasq" in str(e_info.value)


async def test_create_sdn_overlapping(
    ids_start: str,
    sdn_commands: SdnCommands,
) -> None:
    with raises(ValueError) as e_info:
        await sdn_commands.create_sdn(
            proxmox_ids_start=ids_start,
            sdn_config=SdnConfig(
                vnet_configs=(
                    VnetConfig(
                        subnets=(
                            SubnetConfig(
                                cidr="10.0.0.0/8",
                                gateway="10.0.0.1",
                                snat=False,
                                dhcp_ranges=(
                                    DhcpRange(start="10.0.0.16", end="10.0.0.32"),
                                ),
                            ),
                        )
                    ),
                    VnetConfig(
                        subnets=(
                            SubnetConfig(
                                cidr="10.128.0.0/9",
                                gateway="10.128.0.1",
                                snat=False,
                                dhcp_ranges=(
                                    DhcpRange(start="10.128.0.16", end="10.128.0.32"),
                                ),
                            ),
                        )
                    ),
                ),
                use_pve_ipam_dnsnmasq=True,
            ),
        )
    assert "Duplicate IP ranges" in str(e_info.value)


async def test_create_sdn_auto(ids_start: str, sdn_commands: SdnCommands) -> None:
    sdn_zone_id, vnet_aliases = await sdn_commands.create_sdn(
        proxmox_ids_start=ids_start, sdn_config="auto"
    )

    assert sdn_zone_id is not None
    assert len(vnet_aliases) == 1

    await sdn_commands.tear_down_sdn_zone_and_vnet(sdn_zone_id)


async def test_create_sdn_none(ids_start: str, sdn_commands: SdnCommands) -> None:
    sdn_zone_id, vnet_aliases = await sdn_commands.create_sdn(
        proxmox_ids_start=ids_start, sdn_config=None
    )

    assert sdn_zone_id is None
    assert vnet_aliases == []


async def test_create_dhcp_mapping(ids_start: str, sdn_commands: SdnCommands) -> None:
    """Test creating a DHCP static mapping."""
    from ipaddress import ip_address, ip_network

    sdn_zone_id, vnet_aliases = await sdn_commands.create_sdn(
        proxmox_ids_start=ids_start,
        sdn_config=SdnConfig(
            vnet_configs=(
                VnetConfig(
                    alias="test_dhcp_mapping",
                    subnets=(
                        SubnetConfig(
                            cidr=ip_network("10.33.33.0/24"),
                            gateway=ip_address("10.33.33.1"),
                            snat=True,
                            dhcp_ranges=(
                                DhcpRange(
                                    start=ip_address("10.33.33.50"),
                                    end=ip_address("10.33.33.100"),
                                ),
                            ),
                        ),
                    ),
                ),
            ),
            use_pve_ipam_dnsnmasq=True,
        ),
    )

    vnet_id = vnet_aliases[0][0]

    await sdn_commands.create_dhcp_mapping(
        vnet_id=vnet_id,
        zone_id=sdn_zone_id,
        mac_address="52:54:00:AA:BB:CC",
        ip_addr="10.33.33.10",
    )

    await sdn_commands.tear_down_sdn_zone_and_vnet(sdn_zone_id)
