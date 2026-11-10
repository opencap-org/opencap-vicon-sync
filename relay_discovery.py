"""Optional Bonjour advertisement for the existing authenticated relay."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
import uuid
from typing import Any


SERVICE_TYPE = "_opencap-vicon._tcp.local."
LOGGER = logging.getLogger("vicon-sync-relay")


class RelayAdvertisement:
    def __init__(self, host: str, port: int, *, enabled: bool = True) -> None:
        self.host = host
        self.port = port
        self.enabled = enabled
        self.zeroconf: Any = None
        self.service: Any = None

    async def start(self) -> bool:
        if not self.enabled:
            return False
        if self.zeroconf is not None:
            return True
        try:
            import ifaddr
            from zeroconf import IPVersion, ServiceInfo
            from zeroconf.asyncio import AsyncZeroconf
        except ImportError:
            LOGGER.warning("Automatic discovery unavailable: run py -3 -m pip install -r requirements.txt. Manual IP connections still work.")
            return False
        try:
            if self.host == "0.0.0.0":
                addresses = sorted({
                    item.ip for adapter in ifaddr.get_adapters() for item in adapter.ips
                    if isinstance(item.ip, str) and not ipaddress.ip_address(item.ip).is_loopback
                })
            else:
                address = ipaddress.ip_address(self.host)
                addresses = [str(address)] if address.version == 4 and not address.is_loopback else []
            if not addresses:
                LOGGER.warning("No network IPv4 address available for relay discovery; manual connections still work.")
                return False
            computer = re.sub(r"[^A-Za-z0-9-]", "-", socket.gethostname())[:24] or "PC"
            self.service = ServiceInfo(
                SERVICE_TYPE,
                f"OpenCap Vicon {computer}-{self.port}.{SERVICE_TYPE}",
                addresses=[socket.inet_aton(address) for address in addresses],
                port=self.port,
                server=f"opencap-vicon-{uuid.getnode():012x}.local.",
                properties={"relay": "opencap-vicon", "protocol": "1", "relay_version": "2"},
            )
            self.zeroconf = AsyncZeroconf(interfaces=addresses, ip_version=IPVersion.V4Only)
            await asyncio.wait_for(
                self.zeroconf.async_register_service(self.service, allow_name_change=True), timeout=5,
            )
            LOGGER.info("Advertising OpenCap relay discovery on %s (TCP %d)", ", ".join(addresses), self.port)
            return True
        except Exception as error:
            # Advertising is optional; a multicast/firewall problem must never
            # prevent the normal TCP capture relay from starting.
            LOGGER.warning("Relay discovery unavailable: %s; manual connections still work", error)
            await self.close()
            return False

    async def close(self) -> None:
        zeroconf, self.zeroconf = self.zeroconf, None
        self.service = None
        if zeroconf is not None:
            await zeroconf.async_close()
