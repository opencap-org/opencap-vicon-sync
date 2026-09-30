# OpenCap–Vicon Nexus synchronization

Windows relay and setup tools for synchronizing Vicon Nexus capture with the
[OpenCap iPhone app](https://github.com/opencap-org/opencap-iphone). The relay
supports either Nexus controlling OpenCap or OpenCap controlling Nexus, records
clock-alignment metadata, and recovers safely from common reconnect and
interrupted-capture cases.

This directory supports both **Vicon Nexus controls the iPhone** and **the
iPhone controls Vicon Nexus**. The modes are mutually exclusive because Nexus
cannot Send and Receive network capture messages at the same time. In either
direction the iPhone uses the authenticated Windows relay, which also estimates
the Windows/iPhone clock offset.

## Architecture

```text
Nexus Start/Stop button
        |
        | null-terminated XML over UDP 49150
        v
Windows relay on the Nexus PC
        |
        | authenticated newline-delimited JSON over TCP 51002
        v
iPhone on the prepared Record Trial, Baseline, or Biofeedback screen
```

The iPhone initiates the TCP connection. It does not listen for UDP broadcasts,
which avoids the restricted iOS broadcast entitlement and provides reconnect,
authentication, acknowledgements, and clock probes.

## Known-good configuration

| Item | Value |
|---|---|
| Nexus version used in the lab | 2.20 |
| Nexus notification protocol | UDP XML |
| Nexus/relay UDP port | `49150` |
| Relay/iPhone TCP port | `51002` |
| Default Windows hotspot address | `192.168.137.1` |
| Recommended start/stop tolerance | One iPhone frame: 16.7 ms at 60 fps or 33.3 ms at 30 fps |

The Nexus UDP and phone TCP ports are intentionally different. Do not enter
`51002` in Nexus.

## Network setup

For routine lab use, prefer a dedicated 5 GHz access point with:

- the Nexus Windows PC connected by Ethernet;
- the iPhone connected to the private Wi-Fi SSID;
- client isolation disabled;
- a DHCP reservation for the Windows PC; and
- the phone-facing network kept separate from the Vicon camera/MX network.

Windows Mobile Hotspot is acceptable for initial testing. Its PC address is
commonly `192.168.137.1`, but confirm the address with `ipconfig`. On the iPhone,
join that hotspot/SSID and enter the PC's address in OpenCap.

## Install from a fresh Windows Nexus PC

Install Python 3 and Git, then open PowerShell:

```powershell
git clone https://github.com/opencap-org/opencap-vicon-sync.git
cd .\opencap-vicon-sync
```

Open PowerShell **as Administrator** in the same folder once to install the
Private-network firewall rules:

```powershell
.\configure_firewall.ps1
```

Then install the relay as the normal Windows user. Replace the Nexus data root
and token; use the same token in OpenCap:

```powershell
.\install_relay_task.ps1 `
  -Direction auto `
  -NexusHost "192.168.137.1" `
  -NexusRoot "D:\ViconData" `
  -PairingToken "replace-with-a-lab-specific-secret"
```

The task starts immediately and at every sign-in. Re-run the same command after
pulling updates or changing settings.

Always supply a lab-specific token. The scripts retain the older default token
only so existing OpenCap development builds and relay installations can be
migrated without losing connectivity.

## One-time Windows setup

Open an elevated PowerShell prompt in this directory and configure the private
network firewall rules:

```powershell
.\configure_firewall.ps1
```

This permits UDP `49150` from Nexus and TCP `51002` from the iPhone on Private
Windows networks. Keep the phone-facing network profile set to **Private**.

For an interactive first test, start the relay with:

```powershell
.\start_relay.ps1 -PairingToken "replace-with-a-lab-specific-secret"
```

For phone-controlled capture, put Nexus in Receive mode and pass the parent
data root that contains the lab's Nexus sessions:

```powershell
.\start_relay.ps1 `
  -Direction phone-controls-nexus `
  -NexusSession "D:\ViconData\P001\Session01" `
  -PairingToken "replace-with-a-lab-specific-secret"
```

After the end-to-end test passes, install the per-user startup task. A fixed
direction remains available:

```powershell
.\install_relay_task.ps1 -PairingToken "replace-with-a-lab-specific-secret"
```

For routine use of both directions, install the relay in `auto` mode with a
global Nexus data root. The authenticated iPhone direction then changes the
relay's UDP socket automatically whenever no capture is active:

```powershell
.\install_relay_task.ps1 `
  -Direction auto `
  -NexusHost "192.168.137.1" `
  -NexusRoot "D:\ViconData" `
  -PairingToken "replace-with-a-lab-specific-secret"
```

Use the same token in the iPhone app. Re-running the installation command
updates and restarts the task. The scheduled task uses `pythonw.exe`, so no
relay terminal window remains open.

`-NexusRoot` is recursive: the command above works with any active Nexus
session below `D:\ViconData`, without reinstalling the task when the participant
or session folder changes. Repeat `-NexusRoot` to cover additional data trees.
`-NexusSession` remains available as an optional fast initial hint; after each
capture the relay remembers the folder where Nexus actually wrote the files.
Nexus itself chooses the active session because the remote command deliberately
sends an empty `DatabasePath`.

Repeated phone/OpenCap trial labels are resolved safely instead of rejected:
if `trial_1` already exists in the active Nexus session, the relay sends
`trial_1_1`, then `trial_1_2`, and preserves the originally requested label in
the sync sidecar. Current OpenCap builds also include a short OpenCap trial-ID
suffix before sending the request, so the relay suffix is a compatibility
fallback for older builds.

To remove the scheduled task:

```powershell
Unregister-ScheduledTask -TaskName "OpenCap Vicon Sync Relay" -Confirm:$false
```

The relay appends structured diagnostics to `vicon_sync_events.jsonl` and
human-readable process messages to `vicon_sync_relay.log`. Both are ignored by
Git.

### Changing direction during lab use

Nexus's own **Send/Receive** setting still has to be changed in Nexus 2.20; its
supported network protocol does not provide a command for changing that UI
setting. With the scheduled relay installed in `auto` mode, no PowerShell or
task reinstall is needed:

- To use **Nexus controls phone**, first change Nexus to **Send**, then choose
  **Nexus controls phone** in the app and tap **Save and reconnect**.
- To use **Phone controls Nexus**, first choose **Phone controls Nexus** in the
  app and tap **Save and reconnect**. Then change Nexus to **Receive**, select
  **All**, verify port `49150`, click **Arm**, and enable **Lock**.

The order prevents Nexus and the relay from both trying to own UDP `49150`.
Direction changes are rejected while a capture is active. Stop or recover the
current trial first. When switching to Nexus-controlled mode, if Nexus is still
in Receive, the relay cannot claim UDP `49150`; change Nexus to Send and tap
**Save and reconnect** again.

The current iOS build marks **Save and reconnect** as an explicit direction
change. Ordinary Wi-Fi reconnects only report status, so other idle lab phones
cannot unexpectedly change the shared Nexus mode. The relay persists the last
selected direction across Windows restarts.

## Nexus 2.20 setup: Nexus controls the phone

1. Put Nexus in Live mode and prepare the correct database/session.
2. Open **Capture Tools > Auto Capture Setup > Show Advanced**.
3. Enable **Start/Stop over network**.
4. Select **Send** for `nexus-controls-phone`. For `phone-controls-nexus`,
   select **Receive**, **All**, then click **Arm** and enable **Lock**.
5. Select **All** or the adapter that produced the working notifications.
6. Set the UDP port to `49150`.
7. Keep **Capture before start** at `0` unless the same pre-trigger duration
   will be supplied to the frame-map conversion.
8. Prepare the trial name and normal Nexus data-source settings.

On the lab Nexus 2.20 build, values above the accepted port range can be
rewritten by the UI. `49150` is the tested value. The app, relay, and Nexus
directions must match. In Receive mode, keep overwrite disabled and select the
same session directory supplied to the relay. Nexus arm state is not
electronically verified.

## iPhone setup

### Install the current app build

The Windows PC cannot compile or install an iOS app. On a Mac with a current
Xcode installation:

1. Clone or pull [opencap-org/opencap-iphone](https://github.com/opencap-org/opencap-iphone)
   and open its Xcode project.
2. Connect the iPhone by USB, unlock it, tap **Trust**, and enable Developer
   Mode if iOS requests it.
3. Select the **OpenCap** scheme and the connected iPhone as the run
   destination.
4. In **Signing & Capabilities**, select the lab Apple Development team. If the
   existing bundle identifier is unavailable to that team, use a unique bundle
   identifier for the development build.
5. Press **Run**. Keep the phone connected until Xcode finishes installing and
   launches OpenCap.
6. Allow Camera, Photos, and Local Network access when prompted. If Local
   Network was previously denied, enable OpenCap under **Settings > Privacy &
   Security > Local Network**.

Installing this updated build is required for automatic interrupted-capture
reconciliation and the **Recover interrupted capture** button.

### Configure Vicon Sync in the app

1. Open **Vicon Sync** in OpenCap.
2. Enable **Vicon sync** and choose **Phone controls Nexus**.
3. Enter the Windows phone-facing address, normally `192.168.137.1` for a
   Windows hotspot.
4. Enter TCP port `51002` and the relay's pairing token.
5. Tap **Save and reconnect**.
6. Wait for **VICON READY**. Ready requires a connected relay, at least three
   clock probes, and no more than 8 ms estimated clock uncertainty.

The screen that is visible when Nexus starts determines the phone workflow:

- **Record Trial:** open the research Record Trial camera and wait for the model
  to be ready.
- **Baseline:** finish phone/camera positioning and leave the baseline screen
  waiting for Nexus.
- **Biofeedback:** finish setup and leave the biofeedback screen waiting for
  Nexus.

In `nexus-controls-phone`, do not tap an in-app start button. In
`phone-controls-nexus`, use the app's Record/Start and Stop controls; the app
waits for relay acceptance before changing to Recording.

### If the app is closed during recording

1. Reopen OpenCap and return to **Vicon Sync** while the phone remains on the
   same Wi-Fi.
2. The updated app reports that its local recording no longer exists. The
   relay automatically sends Nexus Stop if necessary and waits for the Nexus
   trial files.
3. Wait for **VICON COMPLETE**. If automatic recovery cannot finish and the
   settings screen shows **Recover interrupted capture**, tap it and confirm.
4. Do not use the interrupted phone package as a complete synchronized trial;
   app termination prevents recovery of the missing phone frames. Keep the
   relay/Nexus audit files for diagnosis and repeat the trial.

For a brief Wi-Fi dropout where the app was not terminated, do not tap the
recovery button. The matching local capture ID lets the relay reconnect and
continue the same trial automatically.

## Daily capture procedure

1. Confirm the relay is running.
2. Confirm Nexus is Live, the intended session is active, and the next trial
   name is correct.
3. Put the iPhone on the intended prepared capture screen.
4. Wait at least two seconds and confirm **VICON READY**. This also gives the
   camera-delivery estimator enough preview frames.
5. Click **Start** in Nexus. The Vicon badge turns red and the visible phone
   workflow begins automatically.
6. Click **Stop** in Nexus when the trial is finished.
7. Wait for Nexus's `CaptureComplete` event before opening or moving Nexus
   files. The phone stops and exports without an intermediate share sheet.
8. Keep the matching Nexus and phone packages together. Their event IDs, trial
   name, and capture ID provide an auditable link.

## How timing is calculated

The relay timestamps each Nexus packet with Windows wall time and a monotonic
performance clock. While connected, the phone exchanges two-way probes with the
relay and fits the Windows-to-iPhone monotonic-clock offset.

ARKit timestamps a frame at image exposure, but delivers that frame to the app
later. The app therefore measures camera-delivery latency continuously from the
latest preview frames, using a robust median of up to 120 recent samples. Start
and stop handlers are scheduled at the Nexus target plus this measured latency.

The Vicon ledger counts a frame only after that frame is enqueued into
`CaptureSessionRecorder`. Preview callbacks received before recorder setup are
not counted. Consequently:

- `cameraFrameCount` represents the recorded camera/odometry stream;
- `firstCameraFrameTimestamp` corresponds to `odometry.csv` frame `0`; and
- the first-frame delta can be used directly for automatic frame mapping.

The ledger describes frames submitted to the recorder. The capture performance
file remains the place to check encoder or pipeline drops.

## Output files

- `vicon_sync_events.jsonl`: append-only relay audit log on Windows.
- `<trial>.optigait-sync.json`: Nexus-side event and phone-message sidecar,
  written after `CaptureComplete` when Nexus supplies an accessible path. The
  legacy filename is retained for compatibility with existing analysis tools.
- `vicon_sync.json` or `<capture-id>-vicon_sync.json`: phone-side clock,
  scheduling, camera-delivery, first/last-frame, and completion metadata.
- `odometry.csv`: one row per recorded ARKit/camera frame, with time relative to
  recorded frame `0`.
- `ProcessedFrameSchedule.csv`: mapping of model-processed frames to the raw
  camera/video stream. Model inference can intentionally run below camera FPS.

Important phone-side quality fields include:

- `cameraFrameCount`;
- `firstCameraFrameDeltaFromScheduledStartMilliseconds`;
- `lastCameraFrameDeltaFromScheduledStopMilliseconds`;
- `clockSamples[*].uncertaintyNanoseconds`;
- `cameraDeliveryAtStart` and `cameraDeliveryAtStop`; and
- `completionReason`.

Missing markers are not a synchronization error. A C3D with zero points means
Nexus did not reconstruct/label marker trajectories even if raw Vicon frames
and synchronization metadata were captured successfully.

## Build the iPhone-to-Vicon frame map

For an `odometry.csv` row, the Nexus-relative exposure time is:

```text
nexus_seconds =
  firstCameraFrameDeltaFromScheduledStartMilliseconds / 1000
  + odometry.timestamp
```

With a Vicon rate, optional Nexus pre-trigger, and first C3D frame:

```text
vicon_frame_float =
  (nexus_seconds + vicon_pretrigger_seconds) * vicon_rate_hz
  + vicon_first_frame
```

Generate the complete lookup table from this directory:

```powershell
py -3 .\build_frame_sync.py `
  --vicon-sync "<iphone-export>\vicon_sync.json" `
  --odometry "<iphone-export>\odometry.csv" `
  --vicon-rate 120 `
  --vicon-first-frame 1 `
  --out "<iphone-export>\frame_sync.csv"
```

Replace `120` and `1` with the values in that trial. If Nexus used Capture
Before Start, also pass `--vicon-pretrigger-seconds`.

## Acceptance test

Before collecting study data:

1. Run at least ten short trials across every phone workflow in use.
2. Confirm `cameraFrameCount` equals the number of data rows in `odometry.csv`.
3. Confirm both first- and last-frame delta magnitudes are no more than one
   phone video frame.
4. Confirm clock uncertainty stays at or below 8 ms; the observed lab value is
   normally much lower.
5. Confirm the phone workflow, Nexus trial name, and event IDs agree across the
   two sidecars.
6. Confirm Nexus reports no dropped capture frames.
7. During a non-study test, briefly disable/re-enable iPhone Wi-Fi and verify
   reconnect/replay does not leave a recording active indefinitely.
8. During a second non-study test, terminate the app during a capture, reopen
   it, and verify Nexus stops, the relay reaches file completion, and no second
   Start/trial is created.

Run relay and frame-map tests with:

```powershell
py -3 -m unittest discover -s . -p "test_*.py" -v
```

Test without Nexus by running the relay in one terminal and the simulator in a
second:

```powershell
.\start_relay.ps1
py -3 .\simulate_nexus.py --duration 3 --duplicate-start
```

## Troubleshooting

### `Connection refused` on the iPhone

- Start or restart the Windows relay.
- Verify the phone uses the same Wi-Fi/hotspot as the Windows phone-facing NIC.
- Confirm the PC address with `ipconfig`.
- Confirm TCP `51002` is allowed on a Private network.
- Confirm the scheduled task is running if automatic startup is expected.

### The app is connected but Nexus Start does nothing

- For Nexus-controlled recording, Nexus must be in **Send** and all three
  direction settings must be `nexus-controls-phone`/Send.
- Confirm Start/Stop over network is checked and the UDP port is `49150`.
- Inspect `vicon_sync_events.jsonl` for a `nexus_event` record.
- Leave the phone app active on a prepared supported screen.
- Wait for **VICON READY**, not merely Connected.

### The app says the relay is not ready in phone-control mode

- A message that says **legacy Windows relay** means TCP/Wi-Fi is working, but
  the process holding port `51002` is an older `vicon_sync_relay.py`. USB
  attachment does not affect this connection.
- Restart or reinstall the Windows relay with
  `-Direction phone-controls-nexus -NexusRoot "D:\path\to\ViconData"`.
- In Nexus select **Receive**, port `49150`, **All**, then **Arm** and enable
  **Lock**.
- After every trial, confirm the button still reads **Disarm**. If it reads
  **Arm**, Nexus is no longer armed and will ignore the next network Start.
  Lock is what keeps Nexus automatically armed for consecutive trials.
- In the app select **Phone controls Nexus**, tap **Save and reconnect**, and
  wait until the readiness screen enables **START PHONE + NEXUS**.
- If the relay was installed as a scheduled task before phone-control support,
  reinstall the task with the phone-control arguments above. The updated
  installer stops the already-running legacy task/process before replacing it.
- Verify the exact process answering on TCP `51002`:

  ```powershell
  .\diagnose_relay.ps1 -PairingToken "your-pairing-token"
  ```

  It must report relay version `2` and direction `phone-controls-nexus`.

### The app remains on Vicon Recording after an interruption

- Install an app build containing interrupted-capture recovery and
  reinstall/restart the updated Windows relay. Updating only one side is not
  enough.
- Reopen **Vicon Sync** and tap **Save and reconnect**. Automatic recovery only
  occurs when the same controller phone explicitly reports that its local
  capture is gone.
- If **Recover interrupted capture** appears, tap it and confirm. This sends a
  guarded Stop only for the relay's matching active capture ID.
- If the relay process itself was restarted during the trial, its in-memory
  capture ownership is unavailable; stop Nexus locally, keep its files for
  diagnosis, and repeat the phone trial.

### Nexus changes the port value

Use the tested UDP value `49150`. TCP `51002` belongs only in the iPhone Vicon
Sync settings.

### Nexus starts, but the phone rejects the event

The phone acknowledges rejection reasons such as app inactive, camera not ready,
screen not ready, or already recording. Inspect the Nexus-side sidecar or relay
log for `event_ack` records and their `disposition`.

### No marker trajectories appear

Check Nexus calibration, subject selection, marker visibility, reconstruction,
and labeling. Network synchronization cannot create missing C3D points.

## Protocol reference

Vicon documents the UDP packet format, null termination, unique packet IDs,
Send/Receive modes, and the fact that Nexus cannot send and receive these
messages simultaneously in the official Nexus User Guide:

- [Vicon Nexus User Guide (PDF)](https://help.vicon.com/download/attachments/673091964/Vicon%20Nexus%20User%20Guide.pdf)
- [Vicon: Automatically start and stop capture](https://vicon-help.atlassian.net/wiki/spaces/Nexus215/pages/11377361/Automatically%2Bstart%2Band%2Bstop%2Bcapture)
