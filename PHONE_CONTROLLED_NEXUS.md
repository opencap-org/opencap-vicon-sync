# Design: control Nexus capture from iPhone buttons

## Status

This is the implementation design for the reverse workflow:

> The operator taps Start/Stop in OpenCap, and the same actions start/stop a
> Vicon Nexus trial.

This direction is implemented and bench-tested with Nexus 2.20. Each new lab
installation should still run the acceptance tests before study use because
Nexus arm state cannot be verified electronically by the relay.

## Supported Nexus mechanism

Nexus's supported live-control mechanism is **Start/Stop over network >
Receive**. It accepts the same null-terminated UDP XML protocol that Nexus uses
for capture notifications.

The Nexus 2.20 Python package installed with this system is intended for trial
data access and offline operations; its public `ViconNexus` wrapper does not
expose a live StartCapture/StopCapture method. The reverse workflow should use
the documented UDP receiver, not UI automation or simulated mouse clicks.

Nexus cannot be in network Send and Receive mode simultaneously. Switching to
Receive means the current relay will no longer receive Nexus's Start, Stop, and
Complete broadcasts. The reverse relay therefore needs its own command state
machine and completion detection.

## Proposed architecture

```text
                         scheduled event over existing TCP
                    +----------------------------------------+
                    |                                        v
iPhone Start/Stop --+--> Windows relay --> UDP XML --> Nexus Receive
     button          authenticated       PC adapter IP:49150
                    command                   |
                                               v
                                      current Nexus session
                                               |
                                               v
                                   file watcher confirms save
                                               |
                                               v
                                      Complete sent to phone
```

The phone must continue to communicate through the Windows relay. Sending UDP
directly from iOS would lose authentication, clock translation, idempotency,
file completion, and centralized logging.

## Nexus configuration for the reverse mode

1. Put Nexus in Live mode and select the intended database/session.
2. In **Capture Tools > Auto Capture Setup > Show Advanced**, enable
   **Start/Stop over network**.
3. Select **Receive**.
4. Select **All** and set UDP port `49150`.
5. Keep **Capture before start** at `0` unless the conversion metadata includes
   the same pre-trigger duration.
6. Click **Arm**.
7. Enable the **Lock** control so Nexus automatically re-arms after each trial.
8. Disable file overwrite and use a unique trial name for every capture.

In this mode Nexus owns UDP `49150`. The reverse relay must not bind a listener
to the same address/port; it only sends datagrams to Nexus. When Nexus and the
relay run on the same PC, target the PC address on the phone-facing adapter.
The tested Nexus 2.20 listener accepts the Windows phone-facing adapter address
(for example, `192.168.137.1:49150`) but does not accept the loopback
destination `127.0.0.1:49150`.

Nexus ignores `DatabasePath` when receiving a CaptureStart packet. The operator
must select the correct session in Nexus before arming, and the reverse relay
must be configured with that same session directory for completion monitoring.

## Relay operating modes

The relay has an explicit, mutually exclusive option:

```text
--direction nexus-controls-phone   # current default; listens on UDP 49150
--direction phone-controls-nexus   # sends to Nexus UDP 49150
--direction auto                   # follows explicit app Save while idle
```

In `auto` mode the relay restores its last saved direction, then changes sockets
only when an authenticated phone explicitly taps **Save and reconnect**. Normal
network reconnects do not mutate the shared mode. It refuses to change
direction during Starting, Capturing, Stopping, AwaitingFiles, or Orphaned.
This automates the Windows side only; the operator must still select the
corresponding Nexus **Send** or **Receive** mode.

The reverse mode needs these configuration values:

```text
--nexus-host 192.168.137.1
--nexus-port 49150
--nexus-root "D:\path\to\ViconData"
--trigger-delay-ms 250
```

`--nexus-root` is recursive and can be repeated. Nexus still writes to its
currently active session (`DatabasePath` is empty); the root lets the relay find
and confirm the resulting files globally. `--nexus-session` is an optional
preferred-folder hint and is updated in memory when a different active session
is discovered.

OpenCap may reuse an operator-facing trial label. The relay therefore allocates
a collision-free Nexus filename (`trial_1`, `trial_1_1`, `trial_1_2`, ...)
instead of rejecting a later Start command.

It must retain the existing authenticated TCP server, two-way clock probes,
event log, and iPhone reconnect behavior.

## Phone-to-relay command protocol

The iPhone sends one command with a stable UUID. Retries reuse that UUID so the
relay can return the previous result instead of starting a second trial.

Start example:

```json
{
  "type": "capture_command",
  "protocol": 1,
  "command_id": "UUID",
  "action": "start",
  "trial_name": "P001_Biofeedback_20260924_180819",
  "workflow": "biofeedback",
  "client_uptime_ns": 692373366978750
}
```

Stop example:

```json
{
  "type": "capture_command",
  "protocol": 1,
  "command_id": "UUID",
  "action": "stop",
  "capture_id": "UUID",
  "client_uptime_ns": 692382071217291
}
```

The relay must respond with accepted/rejected status, a capture ID, the packet
ID, and the scheduled Windows performance-clock time. A Start rejection must
leave the phone stopped. A Stop timeout must stop the phone locally and display
that Nexus completion is unconfirmed.

## UDP packets sent to Nexus

Every packet must:

- fit in one UDP datagram;
- be UTF-8 XML;
- end with a null byte;
- contain a PacketID not previously used; and
- use a filesystem-safe, unique Name.

Start template, shown formatted only for readability:

```xml
<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<CaptureStart>
  <Name VALUE="P001_Biofeedback_20260924_180819"/>
  <Notes VALUE="Started from OpenCap iPhone"/>
  <Description VALUE="workflow=biofeedback;command_id=UUID"/>
  <DatabasePath VALUE=""/>
  <Delay VALUE="250"/>
  <PacketID VALUE="100001"/>
</CaptureStart>
```

Stop template:

```xml
<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<CaptureStop RESULT="SUCCESS">
  <Name VALUE="P001_Biofeedback_20260924_180819"/>
  <DatabasePath VALUE=""/>
  <Delay VALUE="250"/>
  <PacketID VALUE="100002"/>
</CaptureStop>
```

The production relay should serialize XML rather than concatenate unescaped
user strings. Persist the next PacketID across relay restarts.

## Start sequence

1. The phone screen validates that its camera/model/workflow is ready.
2. The operator taps the phone's Start button.
3. The phone sends one authenticated `capture_command` and enters **Starting**.
4. The relay rejects the command unless its state is Idle, the trial name is
   unique, and the configured Nexus session is accessible.
5. The relay generates a capture ID and PacketID and chooses a shared target,
   normally Windows monotonic time plus 250 ms.
6. The relay sends CaptureStart to Nexus and sends the equivalent scheduled
   `capture_event` to the phone.
7. The phone maps the target to iPhone uptime and applies its measured ARKit
   delivery compensation.
8. Nexus and the phone begin against the same future target.
9. The phone changes to Recording only after the relay accepts the command.

The 250 ms target provides preparation time; it is not the expected alignment
error. Bench testing must verify the exact Delay behavior of Nexus 2.20 in
Receive mode before study use.

## Stop and completion sequence

1. The operator taps Stop on the phone.
2. The relay validates that the capture ID is active.
3. The relay chooses a shared future stop target and sends CaptureStop to Nexus
   plus a scheduled stop event to the phone.
4. The phone stops its recorded stream at the compensated camera boundary and
   saves its sidecar.
5. Because Nexus cannot also Send while in Receive mode, the relay watches the
   configured Nexus session for the expected trial's raw files.
6. The relay considers the Nexus trial complete only after the expected raw
   files exist and their sizes/timestamps remain stable across multiple polls.
7. The relay writes `<trial>.optigait-sync.json` and sends a Complete message to
   the phone. Post-capture pipelines and C3D generation may finish later and
   should have their own status if required.

File watching is confirmation of disk output, not proof that Nexus was armed at
the instant Start was sent. Until Nexus state can be independently observed,
the UI must say **Nexus arm not electronically verified** and the operator must
confirm Arm/Lock in Nexus.

## Relay state machine and safeguards

Use an explicit state machine:

```text
Idle -> Starting -> Capturing -> Stopping -> AwaitingFiles -> Idle
                         |
                         +-> Orphaned -> AwaitingFiles -> Idle
```

Required safeguards:

- allow only one authenticated controller to own a capture;
- deduplicate `command_id` and PacketID values;
- reject Start outside Idle and Stop outside Capturing;
- sanitize trial names and reject collisions;
- keep Permit overwrite disabled in Nexus;
- provide a stop watchdog and an always-visible local Nexus Stop fallback;
- log every command, datagram, target time, acknowledgement, timeout, and file
  observation with Windows wall and monotonic timestamps;
- never claim Nexus completion solely because the phone stopped; and
- recover after relay/phone reconnect without replaying Start as a new trial.

## Interrupted-capture recovery

An unexpected phone disconnect does not immediately stop Nexus, because a
short Wi-Fi interruption should not end a valid trial. Instead, the relay marks
the trial **Orphaned** and watches the configured Nexus session for finalized
files.

On reconnect, the updated app reports whether it still owns a local capture and
its capture ID:

- If the capture IDs match, the relay returns to **Capturing**. No new Start is
  sent and the trial continues.
- If the app was terminated and therefore has no local capture, the relay sends
  one guarded Nexus Stop, emits the matching Stop event, and enters
  **AwaitingFiles**.
- If Nexus was stopped manually while the phone was unavailable, stable raw
  trial files can complete the orphaned capture without another Start.
- A different phone, a mismatched capture ID, or an older app that omits the
  local-capture fields cannot trigger automatic Stop.

The Vicon Sync settings screen also exposes **Recover interrupted capture**
when the relay reports an owned Capturing, Orphaned, or AwaitingFiles trial.
The button is a fallback: it confirms before asking the relay to stop Nexus and
restart file confirmation. It cannot restore the phone video after iOS has
terminated the app, so that interrupted phone trial must not be used as a
complete synchronized recording.

## App behavior

The phone UI provides:

- an explicit control-direction setting;
- Start and Stop buttons enabled only on supported prepared screens;
- Starting, Recording, Stopping, Awaiting Nexus File, Complete, and Failed
  states;
- a trial-name preview and collision-safe identifier;
- relay rejection and timeout messages;
- an early warning when Nexus has not created any trial files, with an explicit
  instruction to check **Lock**, **Arm/Disarm**, and the active session folder;
- automatic local-capture reporting on reconnect plus a guarded recovery
  button;
- command IDs and capture IDs saved in the phone sidecar; and
- a clear warning that Nexus Arm/Lock is manually verified.

The existing clock estimator, camera-delivery estimator, recorded-frame ledger,
sidecar writer, and frame-map tool can be reused.

## Implementation and validation phases

1. Add XML serialization and a reverse-mode UDP sender to the relay.
2. Add relay unit tests for null termination, XML escaping, persistent unique
   PacketIDs, command deduplication, invalid state transitions, and timeouts.
3. Bench-test Start/Stop against Nexus 2.20 with no participant and verify Name,
   Delay, Arm/Lock, and file output behavior.
4. Add the Nexus session file watcher and completion tests.
5. Add the phone command protocol and UI states.
6. Run at least ten short captures and require one-frame start/stop alignment,
   no duplicate trials, matching frame counts, and correct sidecars.
7. Test Wi-Fi loss during Starting, Capturing, Stop, and AwaitingFiles.
8. Only then expose phone-controlled capture in study builds.

## Alternative for stronger external confirmation

For a later hardware-grade system, the phone can command a networked trigger
controller connected to a Vicon Lock remote-trigger input, while the Windows
relay independently observes Nexus/file state. This adds hardware and does not
remove the need for phone/Windows clock alignment, so the documented UDP
Receive path is the appropriate first implementation.

## Official references

- [Vicon Nexus User Guide (PDF)](https://help.vicon.com/download/attachments/673091964/Vicon%20Nexus%20User%20Guide.pdf)
- [Vicon: Automatically start and stop capture](https://vicon-help.atlassian.net/wiki/spaces/Nexus215/pages/11377361/Automatically%2Bstart%2Band%2Bstop%2Bcapture)
- [Vicon: UDP capture broadcast/trigger](https://vicon-help.atlassian.net/wiki/spaces/Nexus215/pages/11374978)
