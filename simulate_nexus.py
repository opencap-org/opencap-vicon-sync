#!/usr/bin/env python3
"""Send representative Nexus capture packets to the local relay."""

from __future__ import annotations

import argparse
import socket
import time
from xml.sax.saxutils import quoteattr


def packet(root: str, *, name: str, database_path: str, packet_id: int, delay_ms: int | None = None) -> bytes:
    attributes = ' RESULT="SUCCESS"' if root == "CaptureStop" else ""
    children = [
        f"<Name VALUE={quoteattr(name)}/>",
        f"<DatabasePath VALUE={quoteattr(database_path)}/>",
    ]
    if delay_ms is not None:
        children.append(f'<Delay VALUE="{delay_ms}"/>')
    children.append(f'<PacketID VALUE="{packet_id}"/>')
    xml = f'<?xml version="1.0" encoding="UTF-8" standalone="no"?><{root}{attributes}>{"".join(children)}</{root}>'
    return xml.encode("utf-8") + b"\x00"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=49150)
    parser.add_argument("--name", default="OpenCap-Simulator-01")
    parser.add_argument("--database-path", default=r"C:\Vicon\SimulatedSession")
    parser.add_argument("--duration", type=float, default=3.0)
    parser.add_argument("--delay-ms", type=int, default=33)
    parser.add_argument("--duplicate-start", action="store_true")
    args = parser.parse_args()

    base_id = int(time.time() * 1000) % 2_000_000_000
    start = packet(
        "CaptureStart",
        name=args.name,
        database_path=args.database_path,
        packet_id=base_id,
        delay_ms=args.delay_ms,
    )
    stop = packet(
        "CaptureStop",
        name=args.name,
        database_path=args.database_path,
        packet_id=base_id + 1,
        delay_ms=args.delay_ms,
    )
    complete = packet(
        "CaptureComplete",
        name=args.name,
        database_path=args.database_path,
        packet_id=base_id + 2,
    )

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.sendto(start, (args.host, args.port))
        print(f"sent CaptureStart packet {base_id}")
        if args.duplicate_start:
            sock.sendto(start, (args.host, args.port))
            print("sent duplicate CaptureStart")
        time.sleep(max(0.05, args.duration))
        sock.sendto(stop, (args.host, args.port))
        print(f"sent CaptureStop packet {base_id + 1}")
        time.sleep(0.1)
        sock.sendto(complete, (args.host, args.port))
        print(f"sent CaptureComplete packet {base_id + 2}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
