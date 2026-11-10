from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from xml.etree import ElementTree as ET

from vicon_sync_relay import (
    PHONE_CONTROLS_NEXUS,
    EventLog,
    PersistentPacketIDs,
    Relay,
    build_argument_parser,
    parse_nexus_packet,
    serialize_nexus_command,
)


class NexusPacketTests(unittest.TestCase):
    def test_start_packet(self) -> None:
        result = parse_nexus_packet(
            b'<?xml version="1.0"?><CaptureStart><Name VALUE="Walk 01"/>'
            b'<DatabasePath VALUE="D:/Trials"/><Delay VALUE="33"/>'
            b'<PacketID VALUE="912"/></CaptureStart>\x00'
        )
        self.assertEqual(result["event"], "start")
        self.assertEqual(result["name"], "Walk 01")
        self.assertEqual(result["database_path"], "D:/Trials")
        self.assertEqual(result["delay_ms"], 33)
        self.assertEqual(result["packet_id"], 912)

    def test_stop_result(self) -> None:
        result = parse_nexus_packet(
            b'<CaptureStop RESULT="SUCCESS"><Name VALUE="Walk 01"/>'
            b'<Delay VALUE="33"/><PacketID VALUE="913"/></CaptureStop>\x00\r\n'
        )
        self.assertEqual(result["event"], "stop")
        self.assertEqual(result["result"], "SUCCESS")
        self.assertEqual(result["delay_ms"], 33)

    def test_rejects_unrelated_xml(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported"):
            parse_nexus_packet(b"<SomethingElse/>\x00")

    def test_rejects_empty_packet(self) -> None:
        with self.assertRaisesRegex(ValueError, "empty"):
            parse_nexus_packet(b"\x00\r\n")

    def test_outbound_packet_is_null_terminated_and_xml_escaped(self) -> None:
        payload = serialize_nexus_command(
            action="start",
            name="P001_01",
            packet_id=100_001,
            delay_ms=250,
            command_id="id<&\"",
            workflow="biofeedback<&",
        )
        self.assertTrue(payload.endswith(b"\x00"))
        root = ET.fromstring(payload[:-1])
        self.assertEqual(root.tag, "CaptureStart")
        self.assertEqual(root.find("Name").attrib["VALUE"], "P001_01")
        self.assertEqual(root.find("Notes").attrib["VALUE"], "Started from OpenCap iPhone")
        self.assertEqual(
            root.find("Description").attrib["VALUE"],
            'workflow=biofeedback<&;command_id=id<&"',
        )

    def test_packet_ids_persist_across_instances(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "packet-id.txt"
            first = PersistentPacketIDs(path)
            self.assertEqual(first.take(), 100_001)
            second = PersistentPacketIDs(path)
            self.assertEqual(second.take(), 100_002)


class FakeDatagramTransport:
    def __init__(self) -> None:
        self.sent: list[tuple[bytes, tuple[str, int]]] = []

    def sendto(self, payload: bytes, destination: tuple[str, int]) -> None:
        self.sent.append((payload, destination))


class RelayCommandLineTests(unittest.TestCase):
    def test_pairing_token_defaults_to_opencap_and_can_be_overridden(self) -> None:
        parser = build_argument_parser()
        self.assertEqual(parser.parse_args([]).token, "opencap")
        self.assertEqual(parser.parse_args(["--token", "custom-lab_token"]).token, "custom-lab_token")

    def test_startup_rejects_invalid_explicit_pairing_tokens(self) -> None:
        script = Path(__file__).with_name("vicon_sync_relay.py")
        for arguments in (["--token", ""], ["--token", 'bad"token'], ["--token", "two words"]):
            with self.subTest(arguments=arguments), tempfile.TemporaryDirectory() as directory:
                result = subprocess.run(
                    [sys.executable, str(script), *arguments], cwd=directory,
                    capture_output=True, text=True, timeout=5,
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn("token", result.stderr)

    def test_phone_control_requires_an_explicit_nexus_adapter_address(self) -> None:
        script = Path(__file__).with_name("vicon_sync_relay.py")
        for direction in ("auto", PHONE_CONTROLS_NEXUS):
            with self.subTest(direction=direction), tempfile.TemporaryDirectory() as directory:
                result = subprocess.run(
                    [sys.executable, str(script), "--token", "lab-test-token",
                     "--direction", direction, "--nexus-root", directory],
                    cwd=directory, capture_output=True, text=True, timeout=5,
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn("--nexus-host must specify", result.stderr)


class FakePhone:
    def __init__(self, client_id: str = "phone-1") -> None:
        self.authenticated = True
        self.client_id = client_id
        self.app_version = "test"
        self.peer = "test"
        self.messages: list[dict] = []

    async def send(self, message: dict) -> None:
        self.messages.append(message)


class FailingPhone(FakePhone):
    async def send(self, message: dict) -> None:
        raise ConnectionError("test disconnect")


class RelayIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_metadata_only_cannot_confirm_saved_capture(self) -> None:
        for use_root in (False, True):
            with self.subTest(use_root=use_root), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                relay = Relay(
                    token="test-token", event_log=EventLog(root / "events.jsonl"),
                    write_nexus_sidecar=True, direction=PHONE_CONTROLS_NEXUS,
                    nexus_session=None if use_root else root,
                    nexus_roots=(root,) if use_root else (),
                    file_timeout_seconds=0.2, file_poll_seconds=0.05,
                    file_stable_polls=2,
                )
                relay.set_udp_transport(FakeDatagramTransport())  # type: ignore[arg-type]
                phone = FakePhone()
                relay.clients.add(phone)  # type: ignore[arg-type]
                await relay._handle_capture_command(phone, {  # type: ignore[arg-type]
                    "protocol": 1, "command_id": str(uuid.uuid4()),
                    "action": "start", "trial_name": "MetadataOnly",
                })
                capture_id = relay.current_trial["capture_id"]
                (root / "MetadataOnly.Trial.enf").write_bytes(b"metadata")
                (root / "MetadataOnly.system").write_bytes(b"metadata")
                await relay._handle_capture_command(phone, {  # type: ignore[arg-type]
                    "protocol": 1, "command_id": str(uuid.uuid4()),
                    "action": "stop", "capture_id": capture_id,
                })
                await asyncio.wait_for(relay.file_watch_task, timeout=1)
                self.assertFalse((root / "MetadataOnly.opencap-sync.json").exists())
                self.assertFalse(any(message.get("event") == "complete" for message in phone.messages))
                self.assertTrue(any(message.get("state") == "failed" for message in phone.messages))

    async def test_metadata_does_not_hide_raw_trial_in_another_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            preferred = root / "PreferredSession"
            actual = root / "ActualSession"
            preferred.mkdir()
            actual.mkdir()
            relay = Relay(
                token="test-token", event_log=EventLog(root / "events.jsonl"),
                write_nexus_sidecar=True, direction=PHONE_CONTROLS_NEXUS,
                nexus_session=preferred, nexus_roots=(root,),
                file_timeout_seconds=1, file_poll_seconds=0.05, file_stable_polls=2,
            )
            relay.set_udp_transport(FakeDatagramTransport())  # type: ignore[arg-type]
            phone = FakePhone()
            await relay._handle_capture_command(phone, {  # type: ignore[arg-type]
                "protocol": 1, "command_id": str(uuid.uuid4()),
                "action": "start", "trial_name": "RequestedName",
            })
            capture_id = relay.current_trial["capture_id"]
            (preferred / "RequestedName.system").write_bytes(b"metadata only")
            (actual / "RequestedName.Trial.enf").write_bytes(b"metadata only")
            (actual / "ActualName.x2d").write_bytes(b"raw capture")
            await relay._handle_capture_command(phone, {  # type: ignore[arg-type]
                "protocol": 1, "command_id": str(uuid.uuid4()),
                "action": "stop", "capture_id": capture_id,
            })
            await asyncio.wait_for(relay.file_watch_task, timeout=1)
            self.assertEqual(relay.current_state, "idle")
            self.assertEqual(relay.nexus_session, actual)
            self.assertTrue((actual / "ActualName.opencap-sync.json").is_file())
            self.assertFalse((preferred / "RequestedName.opencap-sync.json").exists())

    async def test_repeated_opencap_trial_name_gets_unique_nexus_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "trial_1.x2d").write_bytes(b"prior capture")
            relay = Relay(
                token="test-token", event_log=EventLog(root / "events.jsonl"),
                write_nexus_sidecar=False, direction=PHONE_CONTROLS_NEXUS,
                nexus_session=root, packet_ids=PersistentPacketIDs(root / "packet-id.txt"),
            )
            udp = FakeDatagramTransport()
            relay.set_udp_transport(udp)  # type: ignore[arg-type]
            phone = FakePhone()
            await relay._handle_capture_command(  # type: ignore[arg-type]
                phone,
                {
                    "type": "capture_command", "protocol": 1,
                    "command_id": str(uuid.uuid4()), "action": "start",
                    "trial_name": "trial_1", "workflow": "opencap",
                },
            )

            self.assertEqual(phone.messages[-1]["status"], "accepted")
            self.assertEqual(relay.current_trial["requested_name"], "trial_1")
            self.assertEqual(relay.current_trial["name"], "trial_1_1")
            self.assertEqual(parse_nexus_packet(udp.sent[-1][0])["name"], "trial_1_1")

    async def test_accepted_start_survives_phone_disconnect_before_reply(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            relay = Relay(
                token="test-token", event_log=EventLog(root / "events.jsonl"),
                write_nexus_sidecar=False, direction=PHONE_CONTROLS_NEXUS,
                nexus_session=root, packet_ids=PersistentPacketIDs(root / "packet-id.txt"),
            )
            relay.set_udp_transport(FakeDatagramTransport())  # type: ignore[arg-type]
            command_id = str(uuid.uuid4())
            await relay._handle_capture_command(  # type: ignore[arg-type]
                FailingPhone(),
                {
                    "type": "capture_command", "protocol": 1,
                    "command_id": command_id, "action": "start",
                    "trial_name": "ReconnectTrial", "workflow": "record_trial",
                },
            )
            self.assertEqual(relay.current_state, "capturing")
            self.assertEqual(relay.command_results[command_id]["status"], "accepted")

    async def test_phone_commands_start_stop_deduplicate_and_confirm_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            relay = Relay(
                token="test-token",
                event_log=EventLog(root / "events.jsonl"),
                write_nexus_sidecar=True,
                direction=PHONE_CONTROLS_NEXUS,
                nexus_session=root,
                trigger_delay_ms=250,
                packet_ids=PersistentPacketIDs(root / "packet-id.txt"),
                file_timeout_seconds=1,
                file_poll_seconds=0.01,
                file_stable_polls=2,
            )
            udp = FakeDatagramTransport()
            relay.set_udp_transport(udp)  # type: ignore[arg-type]
            phone = FakePhone()
            start_id = str(uuid.uuid4())
            start = {
                "type": "capture_command", "protocol": 1,
                "command_id": start_id, "action": "start",
                "trial_name": "P001_Biofeedback_01", "workflow": "biofeedback",
            }
            await relay._handle_capture_command(phone, start)  # type: ignore[arg-type]
            self.assertEqual(phone.messages[-1]["status"], "accepted")
            capture_id = phone.messages[-1]["capture_id"]
            self.assertEqual(relay.current_state, "capturing")
            self.assertTrue(udp.sent[0][0].endswith(b"\x00"))

            await relay._handle_capture_command(phone, start)  # type: ignore[arg-type]
            self.assertTrue(phone.messages[-1]["deduplicated"])
            self.assertEqual(len(udp.sent), 1)

            bad_start = dict(start, command_id=str(uuid.uuid4()), trial_name="P001_02")
            await relay._handle_capture_command(phone, bad_start)  # type: ignore[arg-type]
            self.assertEqual(phone.messages[-1]["status"], "rejected")
            self.assertIn("invalid_state", phone.messages[-1]["reason"])

            stop = {
                "type": "capture_command", "protocol": 1,
                "command_id": str(uuid.uuid4()), "action": "stop",
                "capture_id": capture_id,
            }
            await relay._handle_capture_command(phone, stop)  # type: ignore[arg-type]
            self.assertEqual(phone.messages[-1]["status"], "accepted")
            self.assertEqual(relay.current_state, "awaiting_files")
            (root / "P001_Biofeedback_01.x2d").write_bytes(b"raw-vicon-data")
            await asyncio.wait_for(relay.file_watch_task, timeout=1)
            self.assertEqual(relay.current_state, "idle")
            self.assertTrue((root / "P001_Biofeedback_01.opencap-sync.json").is_file())

    async def test_missing_nexus_files_times_out_without_claiming_complete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            relay = Relay(
                token="test-token",
                event_log=EventLog(root / "events.jsonl"),
                write_nexus_sidecar=True,
                direction=PHONE_CONTROLS_NEXUS,
                nexus_session=root,
                packet_ids=PersistentPacketIDs(root / "packet-id.txt"),
                file_timeout_seconds=0.06,
                file_poll_seconds=0.01,
            )
            relay.set_udp_transport(FakeDatagramTransport())  # type: ignore[arg-type]
            phone = FakePhone()
            relay.clients.add(phone)  # type: ignore[arg-type]
            await relay._handle_capture_command(  # type: ignore[arg-type]
                phone,
                {
                    "type": "capture_command", "protocol": 1,
                    "command_id": str(uuid.uuid4()), "action": "start",
                    "trial_name": "NoFileTrial", "workflow": "record_trial",
                },
            )
            capture_id = phone.messages[-1]["capture_id"]
            await relay._handle_capture_command(  # type: ignore[arg-type]
                phone,
                {
                    "type": "capture_command", "protocol": 1,
                    "command_id": str(uuid.uuid4()), "action": "stop",
                    "capture_id": capture_id,
                },
            )
            await asyncio.wait_for(relay.file_watch_task, timeout=1)
            self.assertEqual(relay.current_state, "idle")
            self.assertFalse((root / "NoFileTrial.opencap-sync.json").exists())
            records = [json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()]
            self.assertTrue(any(item["record_type"] == "capture_warning" for item in records))
            self.assertTrue(any(item["record_type"] == "capture_failure" for item in records))
            self.assertTrue(any(
                item.get("type") == "capture_status"
                and item.get("state") == "awaiting_files"
                and item.get("reason") == "nexus_trial_not_created_check_arm_lock"
                for item in phone.messages
            ))

    async def test_confirms_nexus_ui_name_when_receive_ignores_requested_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # An unchanged older trial must not satisfy the new capture.
            (root / "OlderTrial.x2d").write_bytes(b"old")
            relay = Relay(
                token="test-token",
                event_log=EventLog(root / "events.jsonl"),
                write_nexus_sidecar=True,
                direction=PHONE_CONTROLS_NEXUS,
                nexus_session=root,
                packet_ids=PersistentPacketIDs(root / "packet-id.txt"),
                file_timeout_seconds=1,
                file_poll_seconds=0.01,
                file_stable_polls=2,
            )
            relay.set_udp_transport(FakeDatagramTransport())  # type: ignore[arg-type]
            phone = FakePhone()
            await relay._handle_capture_command(  # type: ignore[arg-type]
                phone,
                {
                    "type": "capture_command", "protocol": 1,
                    "command_id": str(uuid.uuid4()), "action": "start",
                    "trial_name": "PhoneRequestedName", "workflow": "biofeedback",
                },
            )
            capture_id = phone.messages[-1]["capture_id"]
            await relay._handle_capture_command(  # type: ignore[arg-type]
                phone,
                {
                    "type": "capture_command", "protocol": 1,
                    "command_id": str(uuid.uuid4()), "action": "stop",
                    "capture_id": capture_id,
                },
            )
            (root / "NexusUIName.x2d").write_bytes(b"new raw data")
            (root / "NexusUIName.system").write_bytes(b"system")
            await asyncio.wait_for(relay.file_watch_task, timeout=1)
            self.assertEqual(relay.current_state, "idle")
            sidecar = root / "NexusUIName.opencap-sync.json"
            self.assertTrue(sidecar.is_file())
            document = json.loads(sidecar.read_text(encoding="utf-8"))
            self.assertEqual(document["trial"]["requested_name"], "PhoneRequestedName")
            self.assertEqual(document["trial"]["actual_name"], "NexusUIName")
            self.assertEqual(document["nexus_events"][-1]["name"], "NexusUIName")

    async def test_finds_active_session_below_global_data_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_root = Path(directory) / "ViconData"
            old_session = data_root / "Participant01" / "OldSession"
            active_session = data_root / "Participant02" / "NewSession"
            old_session.mkdir(parents=True)
            active_session.mkdir(parents=True)
            relay = Relay(
                token="test-token",
                event_log=EventLog(Path(directory) / "events.jsonl"),
                write_nexus_sidecar=True,
                direction=PHONE_CONTROLS_NEXUS,
                nexus_session=old_session,
                nexus_roots=(data_root,),
                packet_ids=PersistentPacketIDs(Path(directory) / "packet-id.txt"),
                file_timeout_seconds=1,
                file_poll_seconds=0.01,
                file_stable_polls=2,
            )
            relay.set_udp_transport(FakeDatagramTransport())  # type: ignore[arg-type]
            phone = FakePhone()
            await relay._handle_capture_command(  # type: ignore[arg-type]
                phone,
                {
                    "type": "capture_command", "protocol": 1,
                    "command_id": str(uuid.uuid4()), "action": "start",
                    "trial_name": "OpenCapTrial", "workflow": "record_trial",
                },
            )
            capture_id = phone.messages[-1]["capture_id"]
            await relay._handle_capture_command(  # type: ignore[arg-type]
                phone,
                {
                    "type": "capture_command", "protocol": 1,
                    "command_id": str(uuid.uuid4()), "action": "stop",
                    "capture_id": capture_id,
                },
            )
            (active_session / "OpenCapTrial.x2d").write_bytes(b"new raw data")
            (active_session / "OpenCapTrial.system").write_bytes(b"system")
            await asyncio.wait_for(relay.file_watch_task, timeout=1)

            self.assertEqual(relay.current_state, "idle")
            self.assertEqual(relay.nexus_session, active_session)
            sidecar = active_session / "OpenCapTrial.opencap-sync.json"
            self.assertTrue(sidecar.is_file())
            document = json.loads(sidecar.read_text(encoding="utf-8"))
            self.assertEqual(document["trial"]["database_path"], str(active_session))
            self.assertEqual(document["nexus_events"][-1]["database_path"], str(active_session))

    async def test_reconnect_reconciles_or_recovers_only_the_owning_phone(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            relay = Relay(
                token="test-token", event_log=EventLog(root / "events.jsonl"),
                write_nexus_sidecar=False, direction=PHONE_CONTROLS_NEXUS,
                nexus_session=root, packet_ids=PersistentPacketIDs(root / "packet-id.txt"),
                file_timeout_seconds=1, file_poll_seconds=0.01,
            )
            udp = FakeDatagramTransport()
            relay.set_udp_transport(udp)  # type: ignore[arg-type]
            phone = FakePhone("owning-phone")
            await relay._handle_capture_command(  # type: ignore[arg-type]
                phone,
                {
                    "type": "capture_command", "protocol": 1,
                    "command_id": str(uuid.uuid4()), "action": "start",
                    "trial_name": "InterruptedTrial", "workflow": "biofeedback",
                },
            )
            capture_id = relay.current_trial["capture_id"]
            self.assertFalse(
                relay._reconcile_phone_hello(
                    {
                        "client_id": "owning-phone", "local_capture_active": True,
                        "local_capture_id": capture_id,
                    }
                )
            )
            self.assertEqual(relay.current_state, "capturing")

            relay.current_state = "orphaned"
            self.assertFalse(
                relay._reconcile_phone_hello(
                    {"client_id": "different-phone", "local_capture_active": False}
                )
            )
            self.assertEqual(relay.current_state, "orphaned")

            self.assertTrue(
                relay._reconcile_phone_hello(
                    {"client_id": "owning-phone", "local_capture_active": False}
                )
            )
            await relay._stop_interrupted_capture(reason="phone_reconnected_without_local_capture")
            self.assertEqual(relay.current_state, "awaiting_files")
            self.assertEqual(len(udp.sent), 2)
            self.assertEqual(parse_nexus_packet(udp.sent[-1][0])["event"], "stop")
            self.assertEqual(relay.current_trial["interruption_reason"], "phone_reconnected_without_local_capture")
            if relay.file_watch_task is not None:
                relay.file_watch_task.cancel()

    async def test_recovery_command_requires_owner_and_acknowledges_before_stop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            relay = Relay(
                token="test-token", event_log=EventLog(root / "events.jsonl"),
                write_nexus_sidecar=False, direction=PHONE_CONTROLS_NEXUS,
                nexus_session=root, packet_ids=PersistentPacketIDs(root / "packet-id.txt"),
                file_timeout_seconds=1, file_poll_seconds=0.01,
            )
            relay.set_udp_transport(FakeDatagramTransport())  # type: ignore[arg-type]
            owner = FakePhone("owning-phone")
            relay.clients.add(owner)  # type: ignore[arg-type]
            await relay._handle_capture_command(  # type: ignore[arg-type]
                owner,
                {
                    "type": "capture_command", "protocol": 1,
                    "command_id": str(uuid.uuid4()), "action": "start",
                    "trial_name": "RecoveryTrial", "workflow": "record_trial",
                },
            )
            capture_id = relay.current_trial["capture_id"]
            relay.current_state = "orphaned"
            owner.messages.clear()

            intruder = FakePhone("different-phone")
            await relay._handle_capture_command(  # type: ignore[arg-type]
                intruder,
                {
                    "type": "capture_command", "protocol": 1,
                    "command_id": str(uuid.uuid4()), "action": "recover",
                    "capture_id": capture_id,
                },
            )
            self.assertEqual(intruder.messages[-1]["reason"], "controller_does_not_own_capture")

            await relay._handle_capture_command(  # type: ignore[arg-type]
                owner,
                {
                    "type": "capture_command", "protocol": 1,
                    "command_id": str(uuid.uuid4()), "action": "recover",
                    "capture_id": capture_id,
                },
            )
            self.assertEqual(owner.messages[0]["type"], "capture_command_result")
            self.assertEqual(owner.messages[0]["status"], "accepted")
            self.assertEqual(owner.messages[1]["type"], "capture_event")
            self.assertEqual(owner.messages[1]["event"], "stop")
            if relay.file_watch_task is not None:
                relay.file_watch_task.cancel()

    async def test_restarted_phone_gets_stop_without_replayed_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            relay = Relay(
                token="test-token", event_log=EventLog(root / "events.jsonl"),
                write_nexus_sidecar=False, direction=PHONE_CONTROLS_NEXUS,
                nexus_session=root, packet_ids=PersistentPacketIDs(root / "packet-id.txt"),
                file_timeout_seconds=1, file_poll_seconds=0.01,
            )
            relay.set_udp_transport(FakeDatagramTransport())  # type: ignore[arg-type]
            phone = FakePhone("owning-phone")
            relay.clients.add(phone)  # type: ignore[arg-type]
            await relay._handle_capture_command(  # type: ignore[arg-type]
                phone,
                {
                    "type": "capture_command", "protocol": 1,
                    "command_id": str(uuid.uuid4()), "action": "start",
                    "trial_name": "RestartedPhoneTrial", "workflow": "biofeedback",
                },
            )
            phone.messages.clear()
            phone.authenticated = False
            relay.current_state = "orphaned"
            await relay._handle_phone_message(
                phone,  # type: ignore[arg-type]
                {
                    "type": "hello", "protocol": 1, "client_id": "owning-phone",
                    "app_version": "test", "token": "test-token",
                    "local_capture_active": False,
                },
            )
            self.assertEqual(phone.messages[0]["type"], "hello_ack")
            capture_events = [item for item in phone.messages if item["type"] == "capture_event"]
            self.assertEqual([item["event"] for item in capture_events], ["stop"])
            self.assertEqual(relay.current_state, "awaiting_files")
            if relay.file_watch_task is not None:
                relay.file_watch_task.cancel()

    async def test_auto_direction_follows_phone_only_when_capture_is_inactive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            relay = Relay(
                token="test-token", event_log=EventLog(root / "events.jsonl"),
                write_nexus_sidecar=False, direction=PHONE_CONTROLS_NEXUS,
                nexus_session=root, allow_direction_switch=True,
            )
            requested: list[str] = []

            async def switcher(direction: str) -> tuple[bool, str | None]:
                requested.append(direction)
                return True, None

            relay.set_direction_switcher(switcher)
            relay.current_state = "complete"
            relay.current_trial = {"name": "finished"}
            error = await relay._switch_requested_direction(
                {"client_id": "phone-1", "requested_direction": "nexus-controls-phone"}
            )
            self.assertIsNone(error)
            self.assertEqual(requested, [])
            self.assertEqual(relay.direction, "phone-controls-nexus")

            error = await relay._switch_requested_direction(
                {
                    "client_id": "phone-1",
                    "requested_direction": "nexus-controls-phone",
                    "request_direction_change": True,
                }
            )
            self.assertIsNone(error)
            self.assertEqual(requested, ["nexus-controls-phone"])
            self.assertEqual(relay.direction, "nexus-controls-phone")
            self.assertEqual(relay.current_state, "idle")
            self.assertEqual(relay.current_trial, {})

            relay.current_state = "capturing"
            error = await relay._switch_requested_direction(
                {
                    "client_id": "phone-1",
                    "requested_direction": "phone-controls-nexus",
                    "request_direction_change": True,
                }
            )
            self.assertEqual(error, "capture_active_in_nexus-controls-phone")
            self.assertEqual(requested, ["nexus-controls-phone"])
            self.assertEqual(relay.direction, "nexus-controls-phone")

    async def test_authenticated_phone_receives_capture_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            relay = Relay(
                token="test-token",
                event_log=EventLog(Path(directory) / "events.jsonl"),
                write_nexus_sidecar=False,
            )
            server = await asyncio.start_server(relay.accept_client, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.write(
                    json.dumps(
                        {
                            "type": "hello",
                            "protocol": 1,
                            "client_id": "unit-test-phone",
                            "app_version": "test",
                            "token": "test-token",
                        }
                    ).encode("utf-8")
                    + b"\n"
                )
                await writer.drain()
                hello = json.loads(await asyncio.wait_for(reader.readline(), 1))
                self.assertEqual(hello["type"], "hello_ack")
                self.assertEqual(hello["relay_version"], 2)
                self.assertEqual(hello["direction"], "nexus-controls-phone")
                self.assertIn("phone-capture-commands", hello["capabilities"])

                packets = [
                    b'<CaptureStart><Name VALUE="Walk 01"/><Delay VALUE="33"/><PacketID VALUE="100"/></CaptureStart>\x00',
                    b'<CaptureStop RESULT="SUCCESS"><Name VALUE="Walk 01"/><PacketID VALUE="101"/></CaptureStop>\x00',
                    b'<CaptureComplete><Name VALUE="Walk 01"/><PacketID VALUE="102"/></CaptureComplete>\x00',
                ]
                received_events = []
                for payload in packets:
                    await relay.receive_nexus_packet(payload, ("127.0.0.1", 51001))
                    message = json.loads(await asyncio.wait_for(reader.readline(), 1))
                    received_events.append(message["event"])
                self.assertEqual(received_events, ["start", "stop", "complete"])

                # A reconnect after a lost Stop must receive the latest state edge.
                replay_reader, replay_writer = await asyncio.open_connection("127.0.0.1", port)
                replay_writer.write(
                    json.dumps(
                        {
                            "type": "hello",
                            "protocol": 1,
                            "client_id": "reconnected-phone",
                            "app_version": "test",
                            "token": "test-token",
                        }
                    ).encode("utf-8")
                    + b"\n"
                )
                await replay_writer.drain()
                replay_hello = json.loads(await asyncio.wait_for(replay_reader.readline(), 1))
                replay_event = json.loads(await asyncio.wait_for(replay_reader.readline(), 1))
                self.assertEqual(replay_hello["nexus_state"], "complete")
                self.assertEqual(replay_event["event"], "complete")
                self.assertTrue(replay_event["replayed_after_reconnect"])
                replay_writer.close()
                await replay_writer.wait_closed()

                # The same PacketID must not produce a second start event.
                await relay.receive_nexus_packet(packets[0], ("127.0.0.1", 51001))
                with self.assertRaises(asyncio.TimeoutError):
                    await asyncio.wait_for(reader.readline(), 0.05)
                writer.close()
                await writer.wait_closed()
            finally:
                server.close()
                await server.wait_closed()

    async def test_complete_writes_nexus_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            relay = Relay(
                token="test-token",
                event_log=EventLog(root / "events.jsonl"),
                write_nexus_sidecar=True,
            )
            escaped_path = str(root).replace("&", "&amp;")
            await relay.receive_nexus_packet(
                (
                    f'<CaptureStart><Name VALUE="Walk 02"/><DatabasePath VALUE="{escaped_path}"/>'
                    '<Delay VALUE="33"/><PacketID VALUE="200"/></CaptureStart>\x00'
                ).encode(),
                ("127.0.0.1", 51001),
            )
            await relay.receive_nexus_packet(
                b'<CaptureComplete><Name VALUE="Walk 02"/><PacketID VALUE="201"/></CaptureComplete>\x00',
                ("127.0.0.1", 51001),
            )
            sidecar = root / "Walk_02.opencap-sync.json"
            self.assertTrue(sidecar.is_file())
            document = json.loads(sidecar.read_text(encoding="utf-8"))
            self.assertEqual([item["event"] for item in document["nexus_events"]], ["start", "complete"])
            self.assertEqual(document["schema_version"], "opencap-vicon-sync-v1")


if __name__ == "__main__":
    unittest.main()
