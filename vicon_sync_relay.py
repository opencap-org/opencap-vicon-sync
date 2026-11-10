#!/usr/bin/env python3
"""Synchronize Vicon Nexus capture and OpenCap over authenticated TCP.

The relay supports both mutually-exclusive Nexus network modes: Nexus Send
(`nexus-controls-phone`) and Nexus Receive (`phone-controls-nexus`).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import re
import signal
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import Any

from relay_discovery import RelayAdvertisement


PROTOCOL_VERSION = 1
RELAY_VERSION = 2
# Nexus 2.20's Auto Capture Setup limits this field to 49150 on the lab build.
# Keep the UDP relay port configurable, but use the highest accepted value by default.
DEFAULT_NEXUS_PORT = 49150
DEFAULT_PHONE_PORT = 51002
NEXUS_CONTROLS_PHONE = "nexus-controls-phone"
PHONE_CONTROLS_NEXUS = "phone-controls-nexus"
AUTO_DIRECTION = "auto"
MAX_LINE_BYTES = 256 * 1024
LOGGER = logging.getLogger("vicon-sync-relay")

# Nexus 2.20 may save a remotely triggered capture under the trial name already
# displayed in the Nexus UI instead of the Name value in CaptureStart.  Limit
# fallback discovery to actual per-trial artifacts so changes to a subject VSK,
# session ENF, or our own sidecar cannot falsely confirm a capture.
NEXUS_TRIAL_SUFFIXES = (
    ".Trial.enf",
    ".history",
    ".system",
    ".xcp",
    ".x1d",
    ".x2d",
    ".c3d",
)
NEXUS_RAW_SUFFIXES = (".x1d", ".x2d")


def _value(element: ET.Element, name: str) -> str | None:
    child = element.find(name)
    if child is None:
        return None
    return child.attrib.get("VALUE")


def _integer(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def parse_nexus_packet(data: bytes) -> dict[str, Any]:
    """Parse one Nexus CaptureStart/CaptureStop/CaptureComplete packet."""
    payload = data.rstrip(b"\x00 \t\r\n")
    if not payload:
        raise ValueError("empty Nexus packet")
    try:
        root = ET.fromstring(payload.decode("utf-8"))
    except (UnicodeDecodeError, ET.ParseError) as error:
        raise ValueError(f"invalid Nexus XML: {error}") from error

    kind_by_tag = {
        "CaptureStart": "start",
        "CaptureStop": "stop",
        "CaptureComplete": "complete",
    }
    event_kind = kind_by_tag.get(root.tag)
    if event_kind is None:
        raise ValueError(f"unsupported Nexus packet root: {root.tag}")

    packet: dict[str, Any] = {
        "event": event_kind,
        "packet_id": _integer(_value(root, "PacketID")),
        "name": _value(root, "Name") or "",
        "database_path": _value(root, "DatabasePath") or "",
        "raw_xml": payload.decode("utf-8"),
    }
    delay_ms = _integer(_value(root, "Delay"))
    if delay_ms is not None:
        packet["delay_ms"] = delay_ms
    if "RESULT" in root.attrib:
        packet["result"] = root.attrib["RESULT"]
    timecode = _value(root, "TimeCode")
    if timecode:
        packet["timecode"] = timecode
    duration = root.find("Duration")
    if duration is not None:
        packet["duration"] = dict(duration.attrib)
    return packet


def _safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return cleaned[:120] or "vicon-trial"


def pairing_token(value: str) -> str:
    """Use a portable explicit token in Python and Windows task arguments."""
    if re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
        raise argparse.ArgumentTypeError("token must contain letters, numbers, '_' or '-'")
    return value


def validate_trial_name(value: Any) -> str:
    """Return a Nexus/filesystem-safe name or reject it without rewriting."""
    if not isinstance(value, str):
        raise ValueError("trial_name must be a string")
    name = value.strip()
    if not name or len(name) > 120 or _safe_filename(name) != name:
        raise ValueError("trial_name must use 1-120 letters, numbers, '.', '_' or '-'")
    return name


def serialize_nexus_command(
    *,
    action: str,
    name: str,
    packet_id: int,
    delay_ms: int,
    command_id: str,
    workflow: str = "",
) -> bytes:
    """Serialize one null-terminated Nexus Receive packet safely."""
    if action == "start":
        root = ET.Element("CaptureStart")
        values = (
            ("Name", name),
            ("Notes", "Started from OpenCap iPhone"),
            ("Description", f"workflow={workflow};command_id={command_id}"),
            ("DatabasePath", ""),
            ("Delay", str(delay_ms)),
            ("PacketID", str(packet_id)),
        )
    elif action == "stop":
        root = ET.Element("CaptureStop", {"RESULT": "SUCCESS"})
        values = (
            ("Name", name),
            ("DatabasePath", ""),
            ("Delay", str(delay_ms)),
            ("PacketID", str(packet_id)),
        )
    else:
        raise ValueError(f"unsupported capture action: {action}")
    for tag, value in values:
        ET.SubElement(root, tag, {"VALUE": value})
    return b'<?xml version="1.0" encoding="UTF-8" standalone="no"?>' + ET.tostring(
        root, encoding="utf-8", short_empty_elements=True
    ) + b"\x00"


class PersistentPacketIDs:
    """Monotonic packet IDs persisted before they are returned to the caller."""

    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            self.next_value = max(1, int(path.read_text(encoding="ascii").strip()))
        except (OSError, ValueError):
            self.next_value = 100_001

    def take(self) -> int:
        value = self.next_value
        self.next_value += 1
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(str(self.next_value), encoding="ascii")
        os.replace(temporary, self.path)
        return value


@dataclass(eq=False)
class PhoneClient:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    peer: str
    authenticated: bool = False
    client_id: str = ""
    app_version: str = ""
    connected_unix_ns: int = field(default_factory=time.time_ns)

    async def send(self, message: dict[str, Any]) -> None:
        encoded = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        self.writer.write(encoded + b"\n")
        await self.writer.drain()

    def close(self) -> None:
        self.writer.close()


class EventLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, separators=(",", ":"), ensure_ascii=False)
        with self.path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(line + "\n")
            stream.flush()


class Relay:
    def __init__(
        self,
        *,
        token: str,
        event_log: EventLog,
        write_nexus_sidecar: bool,
        direction: str = NEXUS_CONTROLS_PHONE,
        nexus_host: str = "127.0.0.1",
        nexus_port: int = DEFAULT_NEXUS_PORT,
        nexus_session: Path | None = None,
        nexus_roots: tuple[Path, ...] = (),
        trigger_delay_ms: int = 250,
        packet_ids: PersistentPacketIDs | None = None,
        file_timeout_seconds: float = 60.0,
        file_poll_seconds: float = 0.5,
        file_stable_polls: int = 3,
        allow_direction_switch: bool = False,
    ) -> None:
        self.token = token
        self.event_log = event_log
        self.write_nexus_sidecar = write_nexus_sidecar
        self.direction = direction
        self.nexus_host = nexus_host
        self.nexus_port = nexus_port
        self.nexus_session = nexus_session
        self.nexus_roots = nexus_roots
        self.trigger_delay_ms = max(0, trigger_delay_ms)
        self.packet_ids = packet_ids or PersistentPacketIDs(event_log.path.with_name("vicon_packet_id.txt"))
        self.file_timeout_seconds = max(0.05, file_timeout_seconds)
        self.file_poll_seconds = max(0.05, file_poll_seconds)
        self.file_stable_polls = max(2, file_stable_polls)
        self.allow_direction_switch = allow_direction_switch
        self.direction_switcher: Callable[[str], Awaitable[tuple[bool, str | None]]] | None = None
        self.udp_transport: asyncio.DatagramTransport | None = None
        self.clients: set[PhoneClient] = set()
        self.seen_packet_keys: set[str] = set()
        self.seen_packet_order: list[str] = []
        self.current_state = "idle"
        self.current_trial: dict[str, Any] = {}
        self.trial_events: list[dict[str, Any]] = []
        self.client_messages: list[dict[str, Any]] = []
        self.command_results: dict[str, dict[str, Any]] = {}
        self.command_order: list[str] = []
        self.controller_client_id: str | None = None
        self.file_watch_task: asyncio.Task[None] | None = None
        self.capture_file_baseline: dict[str, tuple[int, int]] = {}
        self.capture_search_since_unix_ns = 0

    def set_udp_transport(self, transport: asyncio.DatagramTransport | None) -> None:
        self.udp_transport = transport

    def set_direction_switcher(
        self,
        switcher: Callable[[str], Awaitable[tuple[bool, str | None]]],
    ) -> None:
        self.direction_switcher = switcher

    async def accept_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        peer_info = writer.get_extra_info("peername")
        peer = str(peer_info)
        client = PhoneClient(reader=reader, writer=writer, peer=peer)
        self.clients.add(client)
        LOGGER.info("Phone connection opened from %s", peer)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                if len(line) > MAX_LINE_BYTES:
                    raise ValueError("phone message exceeds maximum line size")
                try:
                    message = json.loads(line)
                except json.JSONDecodeError as error:
                    await client.send({"type": "error", "message": f"invalid JSON: {error.msg}"})
                    continue
                if not isinstance(message, dict):
                    await client.send({"type": "error", "message": "JSON message must be an object"})
                    continue
                await self._handle_phone_message(client, message)
        except (ConnectionError, asyncio.IncompleteReadError, ValueError) as error:
            LOGGER.warning("Phone connection %s closed with error: %s", peer, error)
        finally:
            self.clients.discard(client)
            same_controller_is_connected = any(
                item.authenticated and item.client_id == client.client_id
                for item in self.clients
            )
            if (
                self.direction == PHONE_CONTROLS_NEXUS
                and self.current_state == "capturing"
                and client.client_id
                and client.client_id == self.controller_client_id
                and not same_controller_is_connected
            ):
                self.current_state = "orphaned"
                self.event_log.append(
                    {
                        "record_type": "capture_orphaned",
                        "capture_id": self.current_trial.get("capture_id"),
                        "trial_name": self.current_trial.get("name"),
                        "reason": "controller_disconnected",
                        "server_unix_ns": time.time_ns(),
                        "server_perf_ns": time.perf_counter_ns(),
                    }
                )
                self._start_file_watch(
                    str(self.current_trial.get("name") or ""),
                    str(self.current_trial.get("capture_id") or ""),
                    fail_on_timeout=False,
                )
            client.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            LOGGER.info("Phone connection closed from %s", peer)

    async def _handle_phone_message(self, client: PhoneClient, message: dict[str, Any]) -> None:
        message_type = message.get("type")
        if not client.authenticated:
            if message_type != "hello":
                await client.send({"type": "error", "message": "hello required"})
                client.close()
                return
            if message.get("protocol") != PROTOCOL_VERSION:
                await client.send({"type": "error", "message": "unsupported protocol"})
                client.close()
                return
            if not self.token or message.get("token") == self.token:
                client.authenticated = True
                client.client_id = str(message.get("client_id") or "")
                client.app_version = str(message.get("app_version") or "")
                direction_switch_error = await self._switch_requested_direction(message)
                automatically_stop_interrupted = self._reconcile_phone_hello(message)
                now_perf = time.perf_counter_ns()
                now_unix = time.time_ns()
                capabilities = [
                    "direction-handshake",
                    "phone-capture-commands",
                    "nexus-file-confirmation",
                    "interrupted-capture-recovery",
                ]
                if self.allow_direction_switch:
                    capabilities.append("automatic-direction-switch")
                await client.send(
                    {
                        "type": "hello_ack",
                        "protocol": PROTOCOL_VERSION,
                        "relay_version": RELAY_VERSION,
                        "capabilities": capabilities,
                        "server_id": str(uuid.getnode()),
                        "server_perf_ns": now_perf,
                        "server_unix_ns": now_unix,
                        "direction": self.direction,
                        "nexus_state": self.current_state,
                        "current_trial": self.current_trial,
                        "direction_switch_error": direction_switch_error,
                    }
                )
                # A phone that briefly lost Wi-Fi must not silently miss either edge
                # of a trial. During capture replay Start; after it, replay the latest
                # Stop/Complete. Original timestamps and event IDs are preserved.
                if self.trial_events and self.current_state != "orphaned":
                    replay_source = (
                        self.trial_events[0]
                        if self.current_state == "capturing"
                        else self.trial_events[-1]
                    )
                    replay = dict(replay_source)
                    replay["replayed_after_reconnect"] = True
                    await client.send(replay)
                if automatically_stop_interrupted:
                    await self._stop_interrupted_capture(reason="phone_reconnected_without_local_capture")
                LOGGER.info("Authenticated phone %s client_id=%s", client.peer, client.client_id)
            else:
                await client.send({"type": "error", "message": "pairing token rejected"})
                client.close()
            return

        if message_type == "clock_probe":
            receive_perf = time.perf_counter_ns()
            receive_unix = time.time_ns()
            reply = {
                "type": "clock_probe_reply",
                "id": message.get("id"),
                "client_send_uptime_ns": message.get("client_send_uptime_ns"),
                "server_receive_perf_ns": receive_perf,
                "server_receive_unix_ns": receive_unix,
                "server_send_perf_ns": time.perf_counter_ns(),
                "server_send_unix_ns": time.time_ns(),
            }
            await client.send(reply)
            return

        if message_type == "capture_command":
            await self._handle_capture_command(client, message)
            return

        if message_type in {"event_ack", "capture_timing", "client_status"}:
            record = {
                "record_type": "phone_message",
                "server_receive_unix_ns": time.time_ns(),
                "server_receive_perf_ns": time.perf_counter_ns(),
                "client_id": client.client_id,
                "peer": client.peer,
                "message": message,
            }
            self.client_messages.append(record)
            self.event_log.append(record)
            if (
                message_type == "capture_timing"
                and self.write_nexus_sidecar
                and self.current_state in {"stopped", "complete"}
                and self.trial_events
            ):
                self._write_sidecar(self.trial_events[-1])
            return

        if message_type == "ping":
            await client.send({"type": "pong", "server_unix_ns": time.time_ns()})
            return

        await client.send({"type": "error", "message": f"unsupported message type: {message_type}"})

    async def _switch_requested_direction(self, message: dict[str, Any]) -> str | None:
        requested = str(message.get("requested_direction") or "")
        if not self.allow_direction_switch or requested not in {
            NEXUS_CONTROLS_PHONE,
            PHONE_CONTROLS_NEXUS,
        }:
            return None
        # Normal network reconnects report the phone's preference for status
        # checking but must not let several idle lab phones fight over the
        # global Nexus mode. Only the explicit Save and reconnect action can
        # request a direction mutation.
        if message.get("request_direction_change") is not True:
            return None
        if requested == self.direction:
            return None
        if self.current_state in {
            "starting", "capturing", "stopping", "awaiting_files", "orphaned",
        }:
            return f"capture_active_in_{self.direction}"
        if self.direction_switcher is None:
            return "direction_switcher_unavailable"
        previous = self.direction
        switched, error = await self.direction_switcher(requested)
        if not switched:
            return error or "direction_switch_failed"
        self.direction = requested
        self.current_state = "idle"
        self.current_trial = {}
        self.trial_events = []
        self.controller_client_id = None
        self.event_log.append(
            {
                "record_type": "direction_changed",
                "previous_direction": previous,
                "direction": requested,
                "client_id": str(message.get("client_id") or ""),
                "server_unix_ns": time.time_ns(),
                "server_perf_ns": time.perf_counter_ns(),
            }
        )
        LOGGER.info("Changed control direction from %s to %s", previous, requested)
        return None

    def _reconcile_phone_hello(self, message: dict[str, Any]) -> bool:
        """Reconcile relay state with the phone's explicit local capture claim.

        Older app builds omit local_capture_active. They remain recoverable via
        the explicit recovery command but never trigger an automatic Nexus Stop.
        """
        if self.direction != PHONE_CONTROLS_NEXUS:
            return False
        if self.current_state not in {"capturing", "orphaned"}:
            return False
        if "local_capture_active" not in message:
            return False
        connecting_client_id = str(message.get("client_id") or "")
        if not connecting_client_id or connecting_client_id != self.controller_client_id:
            return False
        local_active = message.get("local_capture_active") is True
        local_capture_id = str(message.get("local_capture_id") or "")
        relay_capture_id = str(self.current_trial.get("capture_id") or "")
        if local_active and local_capture_id == relay_capture_id:
            self.current_state = "capturing"
            self.event_log.append(
                {
                    "record_type": "capture_reconciled",
                    "capture_id": relay_capture_id,
                    "result": "phone_capture_still_active",
                    "server_unix_ns": time.time_ns(),
                    "server_perf_ns": time.perf_counter_ns(),
                }
            )
            return False
        self.current_state = "orphaned"
        self.event_log.append(
            {
                "record_type": "capture_reconciled",
                "capture_id": relay_capture_id,
                "result": "phone_capture_missing",
                "reported_local_capture_id": local_capture_id or None,
                "server_unix_ns": time.time_ns(),
                "server_perf_ns": time.perf_counter_ns(),
            }
        )
        return True

    async def _handle_capture_command(self, client: PhoneClient, message: dict[str, Any]) -> None:
        receive_perf = time.perf_counter_ns()
        receive_unix = time.time_ns()
        command_id = str(message.get("command_id") or "")
        self.event_log.append(
            {
                "record_type": "capture_command",
                "server_receive_perf_ns": receive_perf,
                "server_receive_unix_ns": receive_unix,
                "client_id": client.client_id,
                "peer": client.peer,
                "message": message,
            }
        )
        if command_id in self.command_results:
            replay = dict(self.command_results[command_id])
            replay["deduplicated"] = True
            await client.send(replay)
            return

        def reject(reason: str) -> dict[str, Any]:
            return {
                "type": "capture_command_result",
                "protocol": PROTOCOL_VERSION,
                "command_id": command_id,
                "action": str(message.get("action") or ""),
                "status": "rejected",
                "reason": reason,
                "server_receive_perf_ns": receive_perf,
                "server_receive_unix_ns": receive_unix,
            }

        result: dict[str, Any]
        try:
            uuid.UUID(command_id)
        except (ValueError, AttributeError):
            result = reject("invalid_command_id")
            await self._remember_and_send_command_result(client, command_id, result)
            return
        if message.get("protocol") != PROTOCOL_VERSION:
            result = reject("unsupported_protocol")
            await self._remember_and_send_command_result(client, command_id, result)
            return
        if self.direction != PHONE_CONTROLS_NEXUS:
            result = reject("relay_not_in_phone_controls_nexus_mode")
            await self._remember_and_send_command_result(client, command_id, result)
            return

        action = message.get("action")
        if action == "start":
            result = await self._start_from_phone(client, message, receive_perf, receive_unix)
        elif action == "stop":
            result = await self._stop_from_phone(client, message, receive_perf, receive_unix)
        elif action == "recover":
            result = await self._recover_from_phone(client, message, receive_perf, receive_unix)
        else:
            result = reject("unsupported_action")
        if command_id not in self.command_results:
            await self._remember_and_send_command_result(client, command_id, result)
        else:
            self.event_log.append(
                {
                    "record_type": "capture_command_result",
                    "client_id": client.client_id,
                    "peer": client.peer,
                    **result,
                }
            )

    async def _remember_and_send_command_result(
        self,
        client: PhoneClient,
        command_id: str,
        result: dict[str, Any],
    ) -> None:
        if command_id:
            self.command_results[command_id] = result
            self.command_order.append(command_id)
            if len(self.command_order) > 4096:
                expired = self.command_order.pop(0)
                self.command_results.pop(expired, None)
        self.event_log.append(
            {
                "record_type": "capture_command_result",
                "client_id": client.client_id,
                "peer": client.peer,
                **result,
            }
        )
        await client.send(result)

    def _base_command_result(
        self,
        *,
        message: dict[str, Any],
        receive_perf: int,
        receive_unix: int,
        capture_id: str,
        packet_id: int,
        scheduled_perf: int,
        scheduled_unix: int,
    ) -> dict[str, Any]:
        return {
            "type": "capture_command_result",
            "protocol": PROTOCOL_VERSION,
            "command_id": message["command_id"],
            "action": message["action"],
            "status": "accepted",
            "capture_id": capture_id,
            "packet_id": packet_id,
            "server_receive_perf_ns": receive_perf,
            "server_receive_unix_ns": receive_unix,
            "scheduled_server_perf_ns": scheduled_perf,
            "scheduled_server_unix_ns": scheduled_unix,
        }

    async def _start_from_phone(
        self,
        client: PhoneClient,
        message: dict[str, Any],
        receive_perf: int,
        receive_unix: int,
    ) -> dict[str, Any]:
        def rejected(reason: str) -> dict[str, Any]:
            return {
                "type": "capture_command_result", "protocol": PROTOCOL_VERSION,
                "command_id": message["command_id"], "action": "start",
                "status": "rejected", "reason": reason,
                "server_receive_perf_ns": receive_perf, "server_receive_unix_ns": receive_unix,
            }

        if self.current_state != "idle":
            return rejected(f"invalid_state_{self.current_state}")
        controller_id = client.client_id or client.peer
        if self.controller_client_id not in (None, controller_id):
            return rejected("another_controller_owns_capture")
        if not self._has_capture_search_location():
            return rejected("nexus_session_unavailable")
        try:
            requested_name = validate_trial_name(message.get("trial_name"))
        except ValueError:
            return rejected("invalid_trial_name")
        name = self._available_trial_name(requested_name)

        workflow = str(message.get("workflow") or "")[:80]
        capture_id = str(uuid.uuid4())
        try:
            packet_id = self.packet_ids.take()
        except OSError:
            LOGGER.exception("Could not persist the next Nexus PacketID")
            return rejected("packet_id_persistence_failed")
        scheduled_perf = receive_perf + self.trigger_delay_ms * 1_000_000
        scheduled_unix = receive_unix + self.trigger_delay_ms * 1_000_000
        self.current_state = "starting"
        self.capture_file_baseline = self._session_trial_snapshot()
        self.capture_search_since_unix_ns = receive_unix
        payload = serialize_nexus_command(
            action="start", name=name, packet_id=packet_id,
            delay_ms=self.trigger_delay_ms, command_id=message["command_id"], workflow=workflow,
        )
        if not self._send_nexus_datagram(payload):
            self.current_state = "idle"
            return rejected("nexus_udp_unavailable")

        self.current_state = "capturing"
        self.controller_client_id = controller_id
        self.current_trial = {
            "name": name,
            "requested_name": requested_name,
            "database_path": str(self.nexus_session or ""),
            "capture_id": capture_id,
            "command_id": message["command_id"],
            "workflow": workflow,
            "start_packet_id": packet_id,
            "controller_client_id": controller_id,
        }
        event = self._command_event(
            event="start", name=name, capture_id=capture_id,
            command_id=message["command_id"], packet_id=packet_id,
            receive_perf=receive_perf, receive_unix=receive_unix,
            scheduled_perf=scheduled_perf, scheduled_unix=scheduled_unix,
            raw_xml=payload[:-1].decode("utf-8"),
        )
        self.trial_events = [event]
        self.client_messages = []
        self.event_log.append({"record_type": "nexus_command", **event})
        result = self._base_command_result(
            message=message, receive_perf=receive_perf, receive_unix=receive_unix,
            capture_id=capture_id, packet_id=packet_id,
            scheduled_perf=scheduled_perf, scheduled_unix=scheduled_unix,
        )
        # Preserve TCP order: the command result arrives before its capture edge.
        self._remember_result_without_sending(message["command_id"], result)
        try:
            await client.send(result)
        except (ConnectionError, OSError) as error:
            # The UDP command has already been sent. Retaining the result before
            # this write lets a reconnect safely retry the same command UUID.
            LOGGER.warning("Start accepted but phone reply failed for %s: %s", client.peer, error)
        await self._broadcast(event)
        return result

    async def _recover_from_phone(
        self,
        client: PhoneClient,
        message: dict[str, Any],
        receive_perf: int,
        receive_unix: int,
    ) -> dict[str, Any]:
        def rejected(reason: str) -> dict[str, Any]:
            return {
                "type": "capture_command_result", "protocol": PROTOCOL_VERSION,
                "command_id": message["command_id"], "action": "recover",
                "status": "rejected", "reason": reason,
                "server_receive_perf_ns": receive_perf, "server_receive_unix_ns": receive_unix,
            }

        if self.current_state not in {"capturing", "orphaned", "awaiting_files"}:
            return rejected(f"invalid_state_{self.current_state}")
        if self.controller_client_id != (client.client_id or client.peer):
            return rejected("controller_does_not_own_capture")
        capture_id = str(message.get("capture_id") or "")
        if not capture_id or capture_id != str(self.current_trial.get("capture_id") or ""):
            return rejected("capture_id_mismatch")
        result = {
            "type": "capture_command_result", "protocol": PROTOCOL_VERSION,
            "command_id": message["command_id"], "action": "recover",
            "status": "accepted", "capture_id": capture_id,
            "server_receive_perf_ns": receive_perf, "server_receive_unix_ns": receive_unix,
        }
        # Preserve TCP order: acknowledge the guarded recovery request before
        # emitting the synthetic Stop edge which changes the phone workflow.
        self._remember_result_without_sending(message["command_id"], result)
        try:
            await client.send(result)
        except (ConnectionError, OSError) as error:
            LOGGER.warning("Recovery accepted but phone reply failed for %s: %s", client.peer, error)
        # If Stop was already issued, simply restart file confirmation. If the
        # relay still considers Nexus capturing/orphaned, Stop it explicitly.
        if self.current_state == "awaiting_files":
            self._start_file_watch(
                str(self.current_trial.get("name") or ""), capture_id,
                fail_on_timeout=True,
            )
        else:
            await self._stop_interrupted_capture(reason="phone_requested_recovery")
        return result

    async def _stop_from_phone(
        self,
        client: PhoneClient,
        message: dict[str, Any],
        receive_perf: int,
        receive_unix: int,
    ) -> dict[str, Any]:
        def rejected(reason: str) -> dict[str, Any]:
            return {
                "type": "capture_command_result", "protocol": PROTOCOL_VERSION,
                "command_id": message["command_id"], "action": "stop",
                "status": "rejected", "reason": reason,
                "server_receive_perf_ns": receive_perf, "server_receive_unix_ns": receive_unix,
            }

        if self.current_state != "capturing":
            return rejected(f"invalid_state_{self.current_state}")
        if self.controller_client_id != (client.client_id or client.peer):
            return rejected("controller_does_not_own_capture")
        capture_id = str(message.get("capture_id") or "")
        if capture_id != self.current_trial.get("capture_id"):
            return rejected("capture_id_mismatch")
        try:
            packet_id = self.packet_ids.take()
        except OSError:
            LOGGER.exception("Could not persist the next Nexus PacketID")
            return rejected("packet_id_persistence_failed")
        name = self.current_trial["name"]
        scheduled_perf = receive_perf + self.trigger_delay_ms * 1_000_000
        scheduled_unix = receive_unix + self.trigger_delay_ms * 1_000_000
        # Globally discovered raw files must be finalized around this Stop,
        # not merely belong to another recent trial elsewhere in the data root.
        self.capture_search_since_unix_ns = receive_unix
        self.current_state = "stopping"
        payload = serialize_nexus_command(
            action="stop", name=name, packet_id=packet_id,
            delay_ms=self.trigger_delay_ms, command_id=message["command_id"],
        )
        if not self._send_nexus_datagram(payload):
            self.current_state = "capturing"
            return rejected("nexus_udp_unavailable")
        self.current_state = "awaiting_files"
        self.current_trial["stop_packet_id"] = packet_id
        self.current_trial["stop_command_id"] = message["command_id"]
        event = self._command_event(
            event="stop", name=name, capture_id=capture_id,
            command_id=message["command_id"], packet_id=packet_id,
            receive_perf=receive_perf, receive_unix=receive_unix,
            scheduled_perf=scheduled_perf, scheduled_unix=scheduled_unix,
            raw_xml=payload[:-1].decode("utf-8"), result="SUCCESS",
        )
        self.trial_events.append(event)
        self.event_log.append({"record_type": "nexus_command", **event})
        result = self._base_command_result(
            message=message, receive_perf=receive_perf, receive_unix=receive_unix,
            capture_id=capture_id, packet_id=packet_id,
            scheduled_perf=scheduled_perf, scheduled_unix=scheduled_unix,
        )
        self._remember_result_without_sending(message["command_id"], result)
        try:
            await client.send(result)
        except (ConnectionError, OSError) as error:
            LOGGER.warning("Stop accepted but phone reply failed for %s: %s", client.peer, error)
        await self._broadcast(event)
        self._start_file_watch(name, capture_id, fail_on_timeout=True)
        return result

    async def _stop_interrupted_capture(self, *, reason: str) -> None:
        if self.current_state not in {"capturing", "orphaned"}:
            return
        try:
            packet_id = self.packet_ids.take()
        except OSError:
            LOGGER.exception("Could not persist PacketID for interrupted capture recovery")
            return
        name = str(self.current_trial.get("name") or "")
        capture_id = str(self.current_trial.get("capture_id") or "")
        now_perf = time.perf_counter_ns()
        now_unix = time.time_ns()
        self.capture_search_since_unix_ns = now_unix
        scheduled_perf = now_perf + self.trigger_delay_ms * 1_000_000
        scheduled_unix = now_unix + self.trigger_delay_ms * 1_000_000
        command_id = f"automatic-recovery-{uuid.uuid4()}"
        payload = serialize_nexus_command(
            action="stop", name=name, packet_id=packet_id,
            delay_ms=self.trigger_delay_ms, command_id=command_id,
        )
        if not self._send_nexus_datagram(payload):
            self.event_log.append(
                {
                    "record_type": "capture_recovery_failure",
                    "capture_id": capture_id,
                    "reason": "nexus_udp_unavailable",
                    "server_unix_ns": now_unix,
                    "server_perf_ns": now_perf,
                }
            )
            return
        self.current_state = "awaiting_files"
        self.current_trial["interruption_reason"] = reason
        self.current_trial["stop_command_id"] = command_id
        self.current_trial["stop_packet_id"] = packet_id
        event = self._command_event(
            event="stop", name=name, capture_id=capture_id,
            command_id=command_id, packet_id=packet_id,
            receive_perf=now_perf, receive_unix=now_unix,
            scheduled_perf=scheduled_perf, scheduled_unix=scheduled_unix,
            raw_xml=payload[:-1].decode("utf-8"), result="SUCCESS",
        )
        event["recovery_reason"] = reason
        self.trial_events.append(event)
        self.event_log.append({"record_type": "nexus_recovery_command", **event})
        await self._broadcast(event)
        self._start_file_watch(name, capture_id, fail_on_timeout=True)

    def _remember_result_without_sending(self, command_id: str, result: dict[str, Any]) -> None:
        self.command_results[command_id] = result
        self.command_order.append(command_id)
        if len(self.command_order) > 4096:
            expired = self.command_order.pop(0)
            self.command_results.pop(expired, None)

    def _command_event(
        self, *, event: str, name: str, capture_id: str, command_id: str,
        packet_id: int, receive_perf: int, receive_unix: int,
        scheduled_perf: int, scheduled_unix: int, raw_xml: str,
        result: str | None = None,
    ) -> dict[str, Any]:
        return {
            "type": "capture_event", "protocol": PROTOCOL_VERSION,
            "event_id": str(uuid.uuid4()), "event": event,
            "packet_id": packet_id, "name": name,
            "database_path": str(self.nexus_session or ""),
            "delay_ms": self.trigger_delay_ms, "result": result,
            "raw_xml": raw_xml, "capture_id": capture_id,
            "command_id": command_id,
            "server_receive_perf_ns": receive_perf,
            "server_receive_unix_ns": receive_unix,
            "scheduled_server_perf_ns": scheduled_perf,
            "scheduled_server_unix_ns": scheduled_unix,
        }

    def _send_nexus_datagram(self, payload: bytes) -> bool:
        if self.udp_transport is None:
            return False
        try:
            self.udp_transport.sendto(payload, (self.nexus_host, self.nexus_port))
            return True
        except OSError:
            LOGGER.exception("Failed to send Nexus UDP command")
            return False

    @staticmethod
    def _trial_stem(path: Path) -> str | None:
        lower_name = path.name.lower()
        for suffix in NEXUS_TRIAL_SUFFIXES:
            if lower_name.endswith(suffix.lower()):
                return path.name[: -len(suffix)]
        return None

    def _has_capture_search_location(self) -> bool:
        return bool(
            (self.nexus_session is not None and self.nexus_session.is_dir())
            or any(root.is_dir() for root in self.nexus_roots)
        )

    def _directory_trial_files(self, directory: Path | None) -> list[Path]:
        if directory is None or not directory.is_dir():
            return []
        try:
            return [
                path for path in directory.iterdir()
                if path.is_file() and self._trial_stem(path) is not None
            ]
        except OSError as error:
            LOGGER.warning("Could not inspect Nexus session %s: %s", directory, error)
            return []

    def _session_trial_files(self) -> list[Path]:
        return self._directory_trial_files(self.nexus_session)

    def _session_trial_snapshot(self) -> dict[str, tuple[int, int]]:
        snapshot: dict[str, tuple[int, int]] = {}
        for path in self._session_trial_files():
            try:
                stat = path.stat()
            except OSError:
                continue
            snapshot[str(path)] = (stat.st_size, stat.st_mtime_ns)
        return snapshot

    def _trial_files(self, name: str) -> list[Path]:
        return [path for path in self._session_trial_files() if self._trial_stem(path) == name]

    def _available_trial_name(self, requested_name: str) -> str:
        """Resolve repeated OpenCap display names without rejecting Start."""
        if not self._trial_files(requested_name):
            return requested_name
        for number in range(1, 100_000):
            suffix = f"_{number}"
            candidate = f"{requested_name[:120 - len(suffix)]}{suffix}"
            if not self._trial_files(candidate):
                return candidate
        raise RuntimeError("could not allocate a unique Nexus trial name")

    def _changed_trial_groups(self) -> dict[str, list[Path]]:
        groups: dict[str, list[Path]] = {}
        for path in self._session_trial_files():
            try:
                stat = path.stat()
            except OSError:
                continue
            metadata = (stat.st_size, stat.st_mtime_ns)
            if self.capture_file_baseline.get(str(path)) == metadata:
                continue
            stem = self._trial_stem(path)
            if stem is not None:
                groups.setdefault(stem, []).append(path)
        return groups

    def _recent_trial_groups(self, directory: Path) -> dict[str, list[Path]]:
        """Return trial artifacts touched since the current phone capture began."""
        groups: dict[str, list[Path]] = {}
        # The filesystem and relay use the same Windows clock. Keep a narrow
        # margin for timestamp rounding without accepting the prior trial.
        cutoff_ns = max(0, self.capture_search_since_unix_ns - 1_000_000_000)
        for path in self._directory_trial_files(directory):
            try:
                stat = path.stat()
            except OSError:
                continue
            metadata = (stat.st_size, stat.st_mtime_ns)
            if stat.st_mtime_ns < cutoff_ns:
                continue
            if self.capture_file_baseline.get(str(path)) == metadata:
                continue
            stem = self._trial_stem(path)
            if stem is not None:
                groups.setdefault(stem, []).append(path)
        return groups

    def _discover_recent_trial_groups(self) -> list[tuple[Path, str, list[Path]]]:
        """Find newly written Nexus trials below configured data roots."""
        discovered: list[tuple[Path, str, list[Path]]] = []
        seen: set[tuple[str, str]] = set()
        for root in self.nexus_roots:
            if not root.is_dir():
                continue
            for directory_name, directory_names, file_names in os.walk(root, followlinks=False):
                # Avoid Windows metadata trees if a whole drive is configured.
                directory_names[:] = [
                    name for name in directory_names
                    if name.casefold() not in {"$recycle.bin", "system volume information"}
                ]
                directory = Path(directory_name)
                stems = {
                    stem
                    for file_name in file_names
                    if (stem := self._trial_stem(Path(file_name))) is not None
                }
                if not stems:
                    continue
                groups = self._recent_trial_groups(directory)
                for stem, files in groups.items():
                    key = (str(directory).casefold(), stem.casefold())
                    if key not in seen:
                        seen.add(key)
                        discovered.append((directory, stem, files))
        return discovered

    @staticmethod
    def _contains_raw_nexus_data(files: list[Path]) -> bool:
        return any(path.name.lower().endswith(NEXUS_RAW_SUFFIXES) for path in files)

    def _start_file_watch(self, name: str, capture_id: str, *, fail_on_timeout: bool) -> None:
        if self.file_watch_task is not None:
            self.file_watch_task.cancel()
        self.file_watch_task = asyncio.create_task(
            self._watch_for_trial_files(name, capture_id, fail_on_timeout=fail_on_timeout)
        )

    async def _watch_for_trial_files(
        self,
        name: str,
        capture_id: str,
        *,
        fail_on_timeout: bool = True,
    ) -> None:
        watch_started = time.monotonic()
        deadline = watch_started + self.file_timeout_seconds
        warning_after_seconds = min(
            5.0,
            max(self.file_poll_seconds, self.file_timeout_seconds / 4),
        )
        missing_trial_warning_sent = False
        previous: tuple[tuple[str, int, int], ...] = ()
        previous_stem: str | None = None
        observed_directory: Path | None = None
        stable_count = 0
        watch_states = {"awaiting_files"} if fail_on_timeout else {"orphaned"}
        while time.monotonic() < deadline and self.current_state in watch_states:
            if observed_directory is None:
                groups = self._changed_trial_groups()
            else:
                groups = self._recent_trial_groups(observed_directory)
            # Prefer the requested name when Nexus honors it. Otherwise select
            # the changed raw trial with the newest artifact. This also handles
            # Nexus auto-incrementing the name shown in its UI.
            raw_groups = {
                stem: paths for stem, paths in groups.items()
                if self._contains_raw_nexus_data(paths)
            }
            actual_name = name if name in raw_groups else None
            if actual_name is None:
                if raw_groups:
                    actual_name = max(
                        raw_groups,
                        key=lambda stem: max(path.stat().st_mtime_ns for path in raw_groups[stem]),
                    )
            if actual_name is None and observed_directory is None and self.nexus_roots:
                discovered = [
                    item for item in self._discover_recent_trial_groups()
                    if self._contains_raw_nexus_data(item[2])
                ]
                requested = [item for item in discovered if item[1] == name]
                candidates = requested or discovered
                if candidates:
                    observed_directory, actual_name, discovered_files = max(
                        candidates,
                        key=lambda item: max(path.stat().st_mtime_ns for path in item[2]),
                    )
                    groups = {actual_name: discovered_files}
            files = groups.get(actual_name, []) if actual_name is not None else []
            snapshot = tuple(sorted((str(path), path.stat().st_size, path.stat().st_mtime_ns) for path in files))
            stable_count = (
                stable_count + 1
                if snapshot and snapshot == previous and actual_name == previous_stem
                else 0
            )
            self.event_log.append({
                "record_type": "file_observation", "capture_id": capture_id,
                "trial_name": name, "observed_trial_name": actual_name,
                "server_unix_ns": time.time_ns(),
                "server_perf_ns": time.perf_counter_ns(), "files": list(snapshot),
                "stable_polls": stable_count,
            })
            if (
                not files
                and not missing_trial_warning_sent
                and time.monotonic() - watch_started >= warning_after_seconds
            ):
                missing_trial_warning_sent = True
                warning = {
                    "type": "capture_status", "protocol": PROTOCOL_VERSION,
                    "state": "awaiting_files", "capture_id": capture_id,
                    "reason": "nexus_trial_not_created_check_arm_lock",
                    "server_unix_ns": time.time_ns(),
                    "server_perf_ns": time.perf_counter_ns(),
                }
                self.event_log.append({"record_type": "capture_warning", **warning})
                await self._broadcast(warning)
            if (
                stable_count >= self.file_stable_polls - 1
                and self._contains_raw_nexus_data(files)
            ):
                await self._complete_phone_capture(actual_name or name, capture_id, files)
                return
            previous = snapshot
            previous_stem = actual_name
            await asyncio.sleep(self.file_poll_seconds)
        if fail_on_timeout and self.current_state == "awaiting_files":
            if not missing_trial_warning_sent:
                warning = {
                    "type": "capture_status", "protocol": PROTOCOL_VERSION,
                    "state": "awaiting_files", "capture_id": capture_id,
                    "reason": "nexus_trial_not_created_check_arm_lock",
                    "server_unix_ns": time.time_ns(),
                    "server_perf_ns": time.perf_counter_ns(),
                }
                self.event_log.append({"record_type": "capture_warning", **warning})
                await self._broadcast(warning)
            self.current_state = "failed"
            failure = {
                "type": "capture_status", "protocol": PROTOCOL_VERSION,
                "state": "failed", "capture_id": capture_id,
                "reason": "nexus_file_confirmation_timeout",
                "server_unix_ns": time.time_ns(), "server_perf_ns": time.perf_counter_ns(),
            }
            self.event_log.append({"record_type": "capture_failure", **failure})
            await self._broadcast(failure)
            self.current_state = "idle"
            self.controller_client_id = None
        elif not fail_on_timeout and self.current_state == "orphaned":
            self.event_log.append(
                {
                    "record_type": "orphan_file_watch_timeout",
                    "capture_id": capture_id,
                    "trial_name": name,
                    "server_unix_ns": time.time_ns(),
                    "server_perf_ns": time.perf_counter_ns(),
                }
            )

    async def _complete_phone_capture(self, name: str, capture_id: str, files: list[Path]) -> None:
        now_perf = time.perf_counter_ns()
        now_unix = time.time_ns()
        if files:
            actual_directory = files[0].parent
            self.nexus_session = actual_directory
            self.current_trial["database_path"] = str(actual_directory)
        event = self._command_event(
            event="complete", name=name, capture_id=capture_id,
            command_id=str(self.current_trial.get("stop_command_id") or ""),
            packet_id=self.current_trial.get("stop_packet_id") or self.trial_events[-1].get("packet_id") or 0,
            receive_perf=now_perf, receive_unix=now_unix,
            scheduled_perf=now_perf, scheduled_unix=now_unix,
            raw_xml="", result="SUCCESS",
        )
        event["observed_files"] = [str(path) for path in files]
        requested_name = str(
            self.current_trial.get("requested_name")
            or self.current_trial.get("name")
            or ""
        )
        self.current_trial["requested_name"] = requested_name
        self.current_trial["actual_name"] = name
        self.trial_events.append(event)
        self.current_state = "complete"
        self.event_log.append({"record_type": "nexus_file_complete", **event})
        if self.write_nexus_sidecar:
            self._write_sidecar(event)
        await self._broadcast(event)
        self.current_state = "idle"
        self.controller_client_id = None

    async def receive_nexus_packet(self, data: bytes, source: tuple[str, int] | None) -> None:
        receive_perf = time.perf_counter_ns()
        receive_unix = time.time_ns()
        try:
            packet = parse_nexus_packet(data)
        except ValueError as error:
            LOGGER.warning("Ignored UDP packet from %s: %s", source, error)
            return

        packet_key = f"{packet['event']}:{packet.get('packet_id')}:{packet.get('name')}"
        if packet_key in self.seen_packet_keys:
            LOGGER.info("Ignored duplicate Nexus packet %s", packet_key)
            return
        self.seen_packet_keys.add(packet_key)
        self.seen_packet_order.append(packet_key)
        if len(self.seen_packet_order) > 4096:
            expired = self.seen_packet_order.pop(0)
            self.seen_packet_keys.discard(expired)

        delay_ms = max(0, int(packet.get("delay_ms") or 0))
        event = {
            "type": "capture_event",
            "protocol": PROTOCOL_VERSION,
            "event_id": str(uuid.uuid4()),
            **packet,
            "server_receive_perf_ns": receive_perf,
            "server_receive_unix_ns": receive_unix,
            "scheduled_server_perf_ns": receive_perf + delay_ms * 1_000_000,
            "scheduled_server_unix_ns": receive_unix + delay_ms * 1_000_000,
            "udp_source": list(source) if source else None,
        }

        event_kind = packet["event"]
        if event_kind == "start":
            self.current_state = "capturing"
            self.current_trial = {
                "name": packet.get("name", ""),
                "database_path": packet.get("database_path", ""),
                "start_event_id": event["event_id"],
                "start_packet_id": packet.get("packet_id"),
            }
            self.trial_events = [event]
            self.client_messages = []
        else:
            self.trial_events.append(event)
            if event_kind == "stop":
                self.current_state = "stopped"
            elif event_kind == "complete":
                self.current_state = "complete"

        self.event_log.append({"record_type": "nexus_event", **event})
        LOGGER.info(
            "Nexus %-8s trial=%r packet=%s clients=%d",
            event_kind,
            packet.get("name", ""),
            packet.get("packet_id"),
            sum(1 for item in self.clients if item.authenticated),
        )
        await self._broadcast(event)

        if event_kind in {"stop", "complete"} and self.write_nexus_sidecar:
            self._write_sidecar(event)

    async def _broadcast(self, message: dict[str, Any]) -> None:
        authenticated = [client for client in self.clients if client.authenticated]
        results = await asyncio.gather(
            *(client.send(message) for client in authenticated),
            return_exceptions=True,
        )
        for client, result in zip(authenticated, results):
            if isinstance(result, Exception):
                LOGGER.warning("Failed to send event to %s: %s", client.peer, result)
                self.clients.discard(client)
                client.close()

    def _write_sidecar(self, complete_event: dict[str, Any]) -> None:
        database_path = str(complete_event.get("database_path") or self.current_trial.get("database_path") or "")
        trial_name = str(complete_event.get("name") or self.current_trial.get("name") or "")
        if not database_path:
            LOGGER.warning("CaptureComplete has no DatabasePath; Nexus sidecar not written")
            return
        directory = Path(database_path)
        if not directory.is_dir():
            LOGGER.warning("Nexus sidecar directory does not exist: %s", directory)
            return
        sidecar = directory / f"{_safe_filename(trial_name)}.opencap-sync.json"
        document = {
            "schema_version": "opencap-vicon-sync-v1",
            "written_unix_ns": time.time_ns(),
            "trial": self.current_trial,
            "nexus_events": self.trial_events,
            "phone_messages": self.client_messages,
        }
        try:
            temporary = sidecar.with_suffix(sidecar.suffix + ".tmp")
            temporary.write_text(json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, sidecar)
            LOGGER.info("Wrote Nexus sync sidecar %s", sidecar)
        except OSError as error:
            LOGGER.error("Could not write Nexus sync sidecar %s: %s", sidecar, error)


class NexusDatagramProtocol(asyncio.DatagramProtocol):
    def __init__(self, relay: Relay) -> None:
        self.relay = relay

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        asyncio.create_task(self.relay.receive_nexus_packet(data, addr))

    def error_received(self, exc: Exception) -> None:
        LOGGER.error("Nexus UDP listener error: %s", exc)


class NexusTransportManager:
    """Own the mutually-exclusive Nexus Send listener or Receive sender."""

    def __init__(self, relay: Relay, args: argparse.Namespace) -> None:
        self.relay = relay
        self.args = args
        self.transport: asyncio.DatagramTransport | None = None
        self.direction: str | None = None

    async def start(self, direction: str) -> None:
        self.transport = await self._open(direction)
        self.direction = direction
        self._persist_direction(direction)

    async def switch(self, direction: str) -> tuple[bool, str | None]:
        if direction == self.direction:
            return True, None
        previous = self.direction
        if self.transport is not None:
            self.transport.close()
            self.transport = None
            self.relay.set_udp_transport(None)
            # Give Winsock one event-loop turn to release UDP 49150 before a
            # Nexus Send listener is created on the same adapter.
            await asyncio.sleep(0)
        try:
            self.transport = await self._open(direction)
            self.direction = direction
            self._persist_direction(direction)
            return True, None
        except OSError as error:
            LOGGER.warning("Could not change Nexus direction to %s: %s", direction, error)
            if previous is not None:
                try:
                    self.transport = await self._open(previous)
                    self.direction = previous
                except OSError:
                    LOGGER.exception("Could not restore Nexus direction %s", previous)
                    self.direction = None
            return False, f"udp_socket_unavailable_{direction}"

    async def _open(self, direction: str) -> asyncio.DatagramTransport:
        loop = asyncio.get_running_loop()
        if direction == NEXUS_CONTROLS_PHONE:
            transport, _ = await loop.create_datagram_endpoint(
                lambda: NexusDatagramProtocol(self.relay),
                local_addr=(self.args.udp_host, self.args.nexus_port),
                allow_broadcast=True,
            )
            self.relay.set_udp_transport(None)
            LOGGER.info("Listening for Nexus UDP on %s:%d", self.args.udp_host, self.args.nexus_port)
            return transport
        transport, _ = await loop.create_datagram_endpoint(
            asyncio.DatagramProtocol,
            local_addr=("0.0.0.0", 0),
        )
        self.relay.set_udp_transport(transport)
        watch_locations = []
        if self.args.nexus_session:
            watch_locations.append(str(Path(self.args.nexus_session).resolve()))
        watch_locations.extend(str(Path(root).resolve()) for root in self.args.nexus_root)
        LOGGER.info(
            "Sending Nexus commands to %s:%d; watching %s",
            self.args.nexus_host,
            self.args.nexus_port,
            ", ".join(watch_locations),
        )
        return transport

    def close(self) -> None:
        if self.transport is not None:
            self.transport.close()
            self.transport = None
        self.relay.set_udp_transport(None)

    def _persist_direction(self, direction: str) -> None:
        if self.args.direction != AUTO_DIRECTION:
            return
        path = Path(self.args.direction_state_file)
        temporary = path.with_suffix(path.suffix + ".tmp")
        try:
            temporary.write_text(direction + "\n", encoding="utf-8")
            os.replace(temporary, path)
        except OSError as error:
            LOGGER.warning("Could not persist automatic direction to %s: %s", path, error)


async def run(args: argparse.Namespace) -> None:
    event_log = EventLog(Path(args.log))
    initial_direction = args.direction
    if args.direction == AUTO_DIRECTION:
        initial_direction = NEXUS_CONTROLS_PHONE
        try:
            saved_direction = Path(args.direction_state_file).read_text(encoding="utf-8").strip()
            if saved_direction in {NEXUS_CONTROLS_PHONE, PHONE_CONTROLS_NEXUS}:
                initial_direction = saved_direction
        except FileNotFoundError:
            pass
        except OSError as error:
            LOGGER.warning("Could not read automatic direction state: %s", error)
    relay = Relay(
        token=args.token,
        event_log=event_log,
        write_nexus_sidecar=args.write_nexus_sidecar,
        direction=initial_direction,
        nexus_host=args.nexus_host or "127.0.0.1",
        nexus_port=args.nexus_port,
        nexus_session=Path(args.nexus_session) if args.nexus_session else None,
        nexus_roots=tuple(Path(root) for root in args.nexus_root),
        trigger_delay_ms=args.trigger_delay_ms,
        packet_ids=PersistentPacketIDs(Path(args.packet_id_file)),
        file_timeout_seconds=args.file_timeout_seconds,
        allow_direction_switch=args.direction == AUTO_DIRECTION,
    )
    transport_manager = NexusTransportManager(relay, args)
    await transport_manager.start(initial_direction)
    relay.set_direction_switcher(transport_manager.switch)
    server = await asyncio.start_server(relay.accept_client, args.tcp_host, args.phone_port)
    advertisement = RelayAdvertisement(
        args.tcp_host, server.sockets[0].getsockname()[1], enabled=args.discovery,
    )
    addresses = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
    LOGGER.info("Listening for OpenCap TCP on %s", addresses)
    LOGGER.info("Control direction: %s (runtime %s)", args.direction, initial_direction)
    LOGGER.info("Event log: %s", Path(args.log).resolve())

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        # pythonw.exe has no console handle. Some Windows event-loop/Python
        # combinations raise OSError or RuntimeError instead of the documented
        # NotImplementedError when registering process signal handlers.
        with contextlib.suppress(NotImplementedError, OSError, RuntimeError):
            loop.add_signal_handler(signal_name, stop_event.set)
    try:
        async with server:
            await advertisement.start()
            await stop_event.wait()
    finally:
        await advertisement.close()
        transport_manager.close()


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--direction",
        choices=(NEXUS_CONTROLS_PHONE, PHONE_CONTROLS_NEXUS, AUTO_DIRECTION),
        default=NEXUS_CONTROLS_PHONE,
        help="Use auto to follow the authenticated phone while no capture is active",
    )
    parser.add_argument("--udp-host", default="0.0.0.0", help="Local address for Nexus UDP")
    parser.add_argument("--nexus-host", help="PC adapter address; required for phone-controls-nexus or auto")
    parser.add_argument("--nexus-port", type=int, default=DEFAULT_NEXUS_PORT)
    parser.add_argument("--nexus-session", help="Optional preferred Nexus session directory")
    parser.add_argument(
        "--nexus-root",
        action="append",
        default=[],
        help="Data root searched recursively for the active Nexus session; repeat for multiple roots",
    )
    parser.add_argument("--trigger-delay-ms", type=int, default=250)
    parser.add_argument("--file-timeout-seconds", type=float, default=60.0)
    parser.add_argument("--tcp-host", default="0.0.0.0", help="Local address for phone TCP")
    parser.add_argument("--phone-port", type=int, default=DEFAULT_PHONE_PORT)
    parser.add_argument(
        "--discovery", action=argparse.BooleanOptionalAction, default=True,
        help="Advertise the relay using Bonjour; --no-discovery keeps manual IP connections only",
    )
    parser.add_argument("--token", required=True, type=pairing_token, help="Lab-specific phone pairing token")
    parser.add_argument(
        "--log",
        default=str(Path(__file__).with_name("vicon_sync_events.jsonl")),
        help="Append-only JSONL audit log",
    )
    parser.add_argument(
        "--packet-id-file",
        default=str(Path(__file__).with_name("vicon_packet_id.txt")),
        help="Persistent next Nexus PacketID",
    )
    parser.add_argument(
        "--runtime-log",
        default=str(Path(__file__).with_name("vicon_sync_relay.log")),
        help="Human-readable runtime log (used by the windowless scheduled task)",
    )
    parser.add_argument(
        "--direction-state-file",
        default=str(Path(__file__).with_name("vicon_direction.txt")),
        help="Last selected runtime direction for --direction auto",
    )
    parser.add_argument(
        "--write-nexus-sidecar",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Write <trial>.opencap-sync.json after CaptureComplete",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser


def main() -> int:
    parser = build_argument_parser()
    args = parser.parse_args()
    if args.direction in {PHONE_CONTROLS_NEXUS, AUTO_DIRECTION} and not args.nexus_host:
        parser.error("--nexus-host must specify the PC's Nexus adapter address for phone-controls-nexus or auto")
    handlers: list[logging.Handler] = [
        logging.FileHandler(args.runtime_log, encoding="utf-8"),
    ]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
    )
    if (
        args.direction in {PHONE_CONTROLS_NEXUS, AUTO_DIRECTION}
        and not args.nexus_session
        and not args.nexus_root
    ):
        LOGGER.error("--nexus-session or --nexus-root is required for phone-controls-nexus or auto")
        return 2
    if args.nexus_session and not Path(args.nexus_session).is_dir():
        LOGGER.error("Nexus session directory is not accessible: %s", args.nexus_session)
        return 2
    inaccessible_roots = [root for root in args.nexus_root if not Path(root).is_dir()]
    if inaccessible_roots:
        LOGGER.error("Nexus data root is not accessible: %s", ", ".join(inaccessible_roots))
        return 2
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        return 0
    except OSError as error:
        LOGGER.error("Relay could not start: %s", error)
        return 2
    except Exception:
        LOGGER.exception("Relay stopped unexpectedly")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
