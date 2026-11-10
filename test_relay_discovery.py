from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from relay_discovery import RelayAdvertisement, SERVICE_TYPE
from vicon_sync_relay import EventLog, Relay


HAS_DISCOVERY = importlib.util.find_spec("zeroconf") is not None and importlib.util.find_spec("ifaddr") is not None


class DiscoveryFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_discovery_does_not_need_optional_packages(self) -> None:
        advertisement = RelayAdvertisement("0.0.0.0", 51002, enabled=False)
        with patch.dict("sys.modules", {"zeroconf": None}):
            self.assertFalse(await advertisement.start())
        await advertisement.close()

    async def test_missing_package_keeps_manual_connections_available(self) -> None:
        advertisement = RelayAdvertisement("0.0.0.0", 51002)
        with patch.dict("sys.modules", {"ifaddr": None}), self.assertLogs("vicon-sync-relay", "WARNING"):
            self.assertFalse(await advertisement.start())
        self.assertIsNone(advertisement.zeroconf)


@unittest.skipUnless(HAS_DISCOVERY, "optional Bonjour packages are not installed")
class AdvertisementTests(unittest.IsolatedAsyncioTestCase):
    async def test_advertises_actual_tcp_port_and_interfaces_without_pairing_secret(self) -> None:
        adapter = SimpleNamespace(ips=[SimpleNamespace(ip=ip) for ip in
            ("127.0.0.1", "192.168.137.1", "10.0.0.5", ("::1", 0, 0))])
        zeroconf = SimpleNamespace(async_register_service=AsyncMock(), async_close=AsyncMock())
        advertisement = RelayAdvertisement("0.0.0.0", 54321)
        with patch("ifaddr.get_adapters", return_value=[adapter]), patch("zeroconf.asyncio.AsyncZeroconf", return_value=zeroconf) as constructor:
            self.assertTrue(await advertisement.start())
            self.assertTrue(await advertisement.start())  # repeated starts do not leak a responder
        self.assertEqual(constructor.call_args.kwargs["interfaces"], ["10.0.0.5", "192.168.137.1"])
        self.assertEqual(advertisement.service.port, 54321)
        self.assertEqual(advertisement.service.type, SERVICE_TYPE)
        self.assertEqual(advertisement.service.properties, {
            b"relay": b"opencap-vicon", b"protocol": b"1", b"relay_version": b"2",
        })
        zeroconf.async_register_service.assert_awaited_once()
        await advertisement.close()
        await advertisement.close()
        zeroconf.async_close.assert_awaited_once()

    async def test_multicast_failure_does_not_prevent_relay_startup(self) -> None:
        zeroconf = SimpleNamespace(async_register_service=AsyncMock(side_effect=OSError("multicast blocked")), async_close=AsyncMock())
        advertisement = RelayAdvertisement("192.168.137.1", 51002)
        with patch("zeroconf.asyncio.AsyncZeroconf", return_value=zeroconf), self.assertLogs("vicon-sync-relay", "WARNING"):
            self.assertFalse(await advertisement.start())
        self.assertIsNone(advertisement.zeroconf)
        zeroconf.async_close.assert_awaited_once()


@unittest.skipUnless(HAS_DISCOVERY and os.environ.get("OPENCAP_TEST_DISCOVERY") == "1", "set OPENCAP_TEST_DISCOVERY=1 to test local multicast")
class LiveDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_bonjour_resolves_to_authenticated_tcp_relay(self) -> None:
        import ifaddr
        from zeroconf import IPVersion
        from zeroconf.asyncio import AsyncServiceInfo, AsyncZeroconf
        addresses = [item.ip for adapter in ifaddr.get_adapters() for item in adapter.ips
                     if isinstance(item.ip, str) and not item.ip.startswith("127.")]
        if not addresses:
            self.skipTest("no network IPv4 adapter")
        address = addresses[0]
        with tempfile.TemporaryDirectory() as directory:
            relay = Relay(token="live-test-token", event_log=EventLog(Path(directory) / "events.jsonl"), write_nexus_sidecar=False)
            server = await asyncio.start_server(relay.accept_client, address, 0)
            port = server.sockets[0].getsockname()[1]
            advertisement = RelayAdvertisement(address, port)
            browser = AsyncZeroconf(interfaces=[address], ip_version=IPVersion.V4Only)
            writer = None
            try:
                self.assertTrue(await advertisement.start())
                info = AsyncServiceInfo(SERVICE_TYPE, advertisement.service.name)
                self.assertTrue(await info.async_request(browser.zeroconf, 3000))
                self.assertEqual(info.port, port)
                self.assertIn(address, info.parsed_addresses())
                reader, writer = await asyncio.open_connection(info.parsed_addresses()[0], info.port)
                writer.write(json.dumps({"type": "hello", "protocol": 1, "token": "live-test-token", "client_id": "discovery-test"}).encode() + b"\n")
                await writer.drain()
                hello = json.loads(await asyncio.wait_for(reader.readline(), 2))
                self.assertEqual(hello["type"], "hello_ack")
                self.assertEqual(hello["relay_version"], 2)
            finally:
                if writer is not None:
                    writer.close()
                    await writer.wait_closed()
                await browser.async_close()
                await advertisement.close()
                server.close()
                await server.wait_closed()
