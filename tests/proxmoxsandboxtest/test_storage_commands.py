import tempfile
from io import BytesIO
from pathlib import Path
from typing import BinaryIO, cast

import pycdlib

from proxmoxsandbox._impl.async_proxmox import AsyncProxmoxAPI
from proxmoxsandbox._impl.storage_commands import StorageCommands


async def test_upload_size_check_different(
    storage_commands: StorageCommands, async_proxmox_api: AsyncProxmoxAPI
) -> None:
    test_iso_name = "test_upload_size_check_different.iso"

    await delete_existing_iso(storage_commands, async_proxmox_api, test_iso_name)

    try:
        temp_iso = await create_temp_iso("abc")

        await storage_commands.upload_file_to_storage(
            file=temp_iso,
            content_type="iso",
            filename=test_iso_name,
            size_check=temp_iso.stat().st_size,
        )

        test_iso_uploaded = await find_uploaded_iso(storage_commands, test_iso_name)

        test_iso_size = temp_iso.stat().st_size

        assert test_iso_uploaded["size"] == test_iso_size

        await storage_commands.upload_file_to_storage(
            file=temp_iso,
            content_type="iso",
            filename=test_iso_name,
            size_check=test_iso_size - 1,  # lie about the size to induce re-upload
        )

        test_iso_reuploaded = await find_uploaded_iso(storage_commands, test_iso_name)

        assert test_iso_uploaded["ctime"] != test_iso_reuploaded["ctime"]
    finally:
        temp_iso.unlink()


async def test_upload_size_check_same(
    storage_commands: StorageCommands, async_proxmox_api: AsyncProxmoxAPI
) -> None:
    test_iso_name = "test_upload_size_check_same.iso"

    await delete_existing_iso(storage_commands, async_proxmox_api, test_iso_name)

    try:
        temp_iso = await create_temp_iso("abc")

        await storage_commands.upload_file_to_storage(
            file=temp_iso,
            content_type="iso",
            filename=test_iso_name,
            size_check=temp_iso.stat().st_size,
        )

        test_iso_uploaded = await find_uploaded_iso(storage_commands, test_iso_name)

        test_iso_size = temp_iso.stat().st_size

        assert test_iso_uploaded["size"] == test_iso_size

        await storage_commands.upload_file_to_storage(
            file=temp_iso,
            content_type="iso",
            filename=test_iso_name,
            size_check=test_iso_size,
        )

        test_iso_reuploaded = await find_uploaded_iso(storage_commands, test_iso_name)

        # reupload should have had no effect
        assert test_iso_uploaded["ctime"] == test_iso_reuploaded["ctime"]
    finally:
        temp_iso.unlink()


async def test_upload_no_size_check(
    storage_commands: StorageCommands, async_proxmox_api: AsyncProxmoxAPI
) -> None:
    test_iso_name = "test_upload_no_size_check.iso"

    await delete_existing_iso(storage_commands, async_proxmox_api, test_iso_name)

    try:
        temp_iso = await create_temp_iso("def")

        await storage_commands.upload_file_to_storage(
            file=temp_iso,
            content_type="iso",
            filename=test_iso_name,
        )

        test_iso_uploaded = await find_uploaded_iso(storage_commands, test_iso_name)

        await storage_commands.upload_file_to_storage(
            file=temp_iso,
            content_type="iso",
            filename=test_iso_name,
        )

        test_iso_reuploaded = await find_uploaded_iso(storage_commands, test_iso_name)

        assert test_iso_uploaded["ctime"] != test_iso_reuploaded["ctime"]
    finally:
        temp_iso.unlink()


async def delete_existing_iso(storage_commands, async_proxmox_api, test_iso_name):
    await async_proxmox_api.request(
        method="DELETE",
        path=f"/nodes/{storage_commands.node}/storage/{storage_commands.storage}/content/local:iso/{test_iso_name}",
        raise_errors=True,  # this does *not* error if the file does not exist; rather this is a sanity check that the connection, auth etc. is working  # noqa: E501
    )


async def create_temp_iso(content: str) -> Path:
    iso = pycdlib.PyCdlib()
    iso.new(interchange_level=3, joliet=3, rock_ridge="1.12", vol_ident="TESTVOL")

    content_bytes = content.encode("utf-8")
    buffer = BytesIO(content_bytes)

    iso.add_fp(
        buffer,
        len(content_bytes),
        # we don't care about any of these names
        "/META_DATA",
        joliet_path="/meta-data",
        rr_name="meta-data",
    )

    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".iso")
    iso.write_fp(cast(BinaryIO, temp_file))
    return Path(temp_file.name)


async def find_uploaded_iso(
    storage_commands: StorageCommands, test_iso_name: str
) -> dict:
    content_including_upload = await storage_commands.list_storage()

    return [
        content
        for content in content_including_upload
        if content["volid"] == f"local:iso/{test_iso_name}"
    ][0]
