# OpenCap + Vicon Nexus

Synchronize OpenCap recording with Vicon Nexus through a relay running on the
Windows Vicon PC. Either Nexus or OpenCap can control Start/Stop.

## Requirements

- Windows PC with Nexus (**2.20 tested**), Git, Python **3.10+** with the `py`
  launcher, and PowerShell **5.1+**. No Nexus Python SDK is required.
- An iPhone running an updated **Vicon-enabled OpenCap build**. The standard app
  may not include this integration. See [iPhone installation](#iphone-installation).
- Access to this repository and the [OpenCap iPhone repository](https://github.com/opencap-org/opencap-iphone),
  which currently require permission.
- A shared private network: connect the iPhone to the Windows Mobile Hotspot,
  or connect the PC and iPhone to the same lab network. Disable Wi-Fi client
  isolation and keep this network separate from the Vicon camera network.

## 1. Set up the Windows PC

Enable the hotspot or connect the capture-network adapter **before starting the
relay**. Set the phone-facing Windows network profile to **Private**.

Open normal PowerShell in a writable user folder:

```powershell
git clone https://github.com/opencap-org/opencap-vicon-sync.git
cd .\opencap-vicon-sync
py -3 --version
py -3 -m pip install -r requirements.txt
ipconfig
```

Find the IPv4 address of the adapter serving the iPhone's network. Edit these
values, then keep this PowerShell window open:

```powershell
$pcAddress = "192.168.137.1"    # Replace with this PC's capture-network address
$nexusDataRoot = "D:\ViconData" # Replace with the folder containing Nexus sessions
$relayToken = py -3 -c "import secrets; print(secrets.token_urlsafe(24))"
$relayToken
```

Copy the generated token into the iPhone settings in step 3 and keep it for
future launches. Tokens use letters, numbers, underscores, and hyphens.
`192.168.137.1` is an example hotspot address; **check each lab's PC**. Use the
PC's adapter address, not `127.0.0.1`, for Nexus Receive commands.

In a **second PowerShell window, opened as Administrator** in this repository,
configure the firewall once:

```powershell
.\configure_firewall.ps1
```

This allows Nexus UDP `49150`, phone TCP `51002`, and Bonjour discovery UDP
`5353` on Private networks. If Windows blocks the scripts, review them and run
`Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` in each affected window.
Managed PCs may require IT assistance.

Back in the **normal PowerShell window**, start the relay:

```powershell
.\start_relay.ps1 -Direction auto -NexusHost $pcAddress -NexusRoot $nexusDataRoot -PairingToken $relayToken
```

Leave it running during the test. `-NexusRoot` searches recursively; select an
active Nexus session inside that folder. For several data folders, use
`-NexusRoot "D:\ViconData", "E:\ViconData"`. A fixed session can instead be
supplied with `-NexusSession "D:\ViconData\Participant01\Session01"`.

## 2. Configure Nexus and choose a controller

In Nexus, select the intended session and go to **Live → Capture Tools → Auto
Capture Setup → Show Advanced**. Enable **Start/Stop over network**, select
**All** (or the appropriate adapter), and set **UDP port `49150`**.

| Who controls recording? | Nexus setting | OpenCap direction |
|---|---|---|
| Nexus Start/Stop | **Send** | **Nexus controls OpenCap** |
| OpenCap session controls | **Receive**, then **Arm** and enable **Lock** | **OpenCap controls Nexus** |

Keep **Capture before start = 0** and file overwrite disabled. Nexus cannot Send
and Receive simultaneously. **Nexus Arm/Lock must be checked manually**; the
relay cannot verify them electronically.

When changing direction while idle:

- **To Nexus control:** change Nexus to **Send** first, then select the direction
  in OpenCap and tap **Save and reconnect**.
- **To OpenCap control:** select the direction in OpenCap and tap **Save and
  reconnect** first, then set Nexus to **Receive → All → Arm → Lock**.

This order avoids a conflict over UDP `49150`. With relay mode `auto`, changing
direction does not require restarting PowerShell. Discovery and ordinary
reconnects do not change the shared direction automatically.

## 3. Pair the iPhone

1. Join the PC's hotspot or lab Wi-Fi. Allow OpenCap **Local Network** access.
2. Join and prepare your normal OpenCap capture session.
3. Open **Info (ⓘ) → Vicon Sync**. This entry is available on any network.
4. Enable **Vicon sync**, select the direction from the table, and enter the
   **same pairing token** used on Windows.
5. Leave **Find relay automatically** enabled, or tap **Find relay on Wi-Fi**.
   Select the intended PC if several appear. If discovery fails, enter the PC's
   IPv4 address manually and use TCP port **`51002`**.
6. Tap **Save and reconnect** and wait for the Vicon indicator to show **Ready**.

Discovery runs for roughly eight seconds when the app becomes active or Wi-Fi
changes, and stops during recording and in the background. It can be disabled
without affecting manual pairing. It discovers the address; it does not supply
the pairing token.

Configure Vicon Sync on each phone that should receive Nexus events. In
multicamera sessions, **Phone 1** bridges the OpenCap session with Nexus.

## 4. Record a test trial

1. Check the active Nexus session, trial name, matching directions, and **Ready**
   on the prepared phone. For Receive mode, verify **Arm** and **Lock**.
2. Start and stop from the chosen controller: **Nexus**, or the usual **OpenCap
   session controls** (web, or phone controls where supported).
3. Wait for completion before moving files. Nexus-controlled capture uses
   `CaptureComplete`; OpenCap-controlled capture waits for stable raw Nexus files.
4. Check both recordings and keep their matching trial IDs and sync metadata.

Before study use, run at least ten short trials in each mode you plan to use.
Check Nexus for dropped frames, confirm matching trial metadata, and test a
brief Wi-Fi interruption. For ARKit exports, verify that `cameraFrameCount`
matches the data rows in `odometry.csv`, clock uncertainty is at most **8 ms**,
and first/last-frame timing deltas meet your tolerance (the lab target is one
phone frame). Validate each new PC/phone combination; **Ready alone is not a
measurement of recording alignment**.

## 5. Enable automatic startup

After the test passes, press **Ctrl+C** to stop the interactive relay. In the
same normal PowerShell window, install the startup task:

```powershell
.\install_relay_task.ps1 -Direction auto -NexusHost $pcAddress -NexusRoot $nexusDataRoot -PairingToken $relayToken
```

The task starts immediately and at sign-in, without a terminal window. Install
it under the Windows account used for Nexus; that user must be signed in.
Re-running the command updates and restarts it. In a new PowerShell window,
restore the variables using the **same saved token**, address, and data folder.

To update, run `git pull`, reinstall `requirements.txt`, rerun the firewall
script if rules changed, and repeat the task installation command. Restart the
relay after changing the PC's capture-network address. Keep the whole repository
folder together and writable: the relay stores its logs and state there.

To uninstall the task:

```powershell
Stop-ScheduledTask -TaskName "OpenCap Vicon Sync Relay"
Unregister-ScheduledTask -TaskName "OpenCap Vicon Sync Relay" -Confirm:$false
```

## Files and troubleshooting

| File | Purpose |
|---|---|
| `<trial>.opencap-sync.json` | Nexus-side trial, events, and phone timing metadata |
| `vicon_sync.json` or `<capture-id>-vicon_sync.json` | Phone-side synchronization metadata |
| `vicon_sync_events.jsonl` | Relay event log |
| `vicon_sync_relay.log` | Relay startup/runtime diagnostics |

- **Cannot connect:** check the PC's current IPv4 address, shared network,
  matching token, Local Network permission, running relay, and Private firewall
  rules. USB does not carry the relay connection.
- **No discovery result:** use the manual PC address. Install `requirements.txt`,
  allow UDP `5353`, and restart the relay. Missing discovery packages or blocked
  Bonjour do not prevent manual TCP connections. `-DisableDiscovery` on either
  launcher disables advertising.
- **Connected, but no recording:** check matching Send/Receive directions, UDP
  `49150`, the prepared OpenCap session, and Nexus **Arm/Lock** in Receive mode.
  TCP `51002` belongs in OpenCap, not Nexus.
- **No Nexus file/completion:** check Arm/Lock and the active session inside
  `-NexusRoot`. The relay confirms raw file output, not completion of later C3D
  processing. Missing marker trajectories require checking Nexus reconstruction.
- **App closed during recording:** reopen it on the same Wi-Fi and reconnect.
  In OpenCap-controlled mode, use **Recover interrupted capture** if offered.
  If the relay restarted too, stop Nexus locally. Keep the audit files and
  repeat the interrupted trial; missing phone frames cannot be recovered.

Check the local relay handshake on Windows with:

```powershell
.\diagnose_relay.ps1 -PairingToken $relayToken
```

It should report relay version **2** and the selected direction. Run automated
tests with `py -3 -m unittest discover -s . -p "test_*.py" -v`.

<details>
<summary>Optional: frame mapping and app installation</summary>

### Frame mapping for ARKit exports

When the export contains `odometry.csv` and first-camera-frame timing, generate
the iPhone-to-Vicon frame lookup:

```powershell
py -3 .\build_frame_sync.py `
  --vicon-sync "<iphone-export>\vicon_sync.json" `
  --odometry "<iphone-export>\odometry.csv" `
  --vicon-rate 120 --vicon-first-frame 1 `
  --out "<iphone-export>\frame_sync.csv"
```

Use the actual Vicon rate and first frame for that trial. If Nexus used
pre-trigger capture, also pass `--vicon-pretrigger-seconds <seconds>`. This tool
requires ARKit camera-frame timing; video-only exports do not necessarily
contain those inputs.

### iPhone installation

On a Mac with Xcode and development signing access:

```sh
git clone --branch vicon_multicam https://github.com/opencap-org/opencap-iphone.git
cd opencap-iphone
open OpenCap.xcodeproj
```

Use the updated Vicon-enabled build; the default app branch may not include it.
In Xcode, select the **OpenCap** scheme, your connected/unlocked iPhone, and your
Apple Development team under **Signing & Capabilities**. Use a bundle identifier
available to that team and press **Run**. Trust the Mac and enable Developer Mode
if prompted. Allow Camera and Local Network access, then return to step 3.

</details>

For protocol and recovery details, see [phone-controlled Nexus](PHONE_CONTROLLED_NEXUS.md).
The capture network mechanism is documented in the [Vicon Nexus User Guide](https://help.vicon.com/download/attachments/673091964/Vicon%20Nexus%20User%20Guide.pdf).
