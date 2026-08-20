"""Client reuse semantics of AsyncProxmoxAPI._get_client / aclose.

No network involved: only client identity and lifecycle are asserted.
"""

import asyncio

from proxmoxsandbox._impl.async_proxmox import AsyncProxmoxAPI


def make_api() -> AsyncProxmoxAPI:
    return AsyncProxmoxAPI(
        host="proxmox.invalid:8006",
        user="root@pam",
        password="unused",
        verify_tls=False,
    )


def test_client_reused_within_loop() -> None:
    api = make_api()

    async def main():
        first = api._get_client()
        second = api._get_client()
        assert first is second
        await api.aclose()

    asyncio.run(main())


def test_client_recreated_on_new_loop() -> None:
    api = make_api()

    async def get():
        return api._get_client()

    first = asyncio.run(get())
    second = asyncio.run(get())
    assert first is not second

    async def close():
        await api.aclose()

    asyncio.run(close())


def test_aclose_recreates_client() -> None:
    api = make_api()

    async def main():
        first = api._get_client()
        await api.aclose()
        assert first.is_closed
        second = api._get_client()
        assert second is not first
        assert not second.is_closed
        await api.aclose()

    asyncio.run(main())


def test_aclose_from_other_loop_drops_without_closing() -> None:
    api = make_api()

    async def get():
        return api._get_client()

    first = asyncio.run(get())

    async def close():
        await api.aclose()

    # Created on a dead loop: aclose must not await into it, just drop.
    asyncio.run(close())
    assert not first.is_closed
    assert api._client is None


def test_aclose_without_client_is_noop() -> None:
    api = make_api()

    async def close():
        await api.aclose()

    asyncio.run(close())
