# Optional: install the relay as a per-user task that starts at Windows sign-in.
param(
    [string]$PairingToken = "optigait-vicon-sync",
    [ValidateSet("auto", "nexus-controls-phone", "phone-controls-nexus")]
    [string]$Direction = "nexus-controls-phone",
    [string]$NexusHost = "127.0.0.1",
    [string]$NexusSession = "",
    [string[]]$NexusRoot = @(),
    [int]$NexusPort = 49150,
    [int]$PhonePort = 51002,
    [int]$TriggerDelayMS = 250
)

$ErrorActionPreference = "Stop"
$taskName = "OpenCap Vicon Sync Relay"
$legacyTaskName = "OptiGait Vicon Sync Relay"
$pythonExecutable = (& py.exe -3 -c "import sys; print(sys.executable)").Trim()
$pythonWindowless = Join-Path (Split-Path -Parent $pythonExecutable) "pythonw.exe"
if (Test-Path -LiteralPath $pythonWindowless -PathType Leaf) {
    $pythonLauncher = $pythonWindowless
    $pythonPrefixArguments = @()
} else {
    $pythonLauncher = (Get-Command pyw.exe -ErrorAction Stop).Source
    $pythonPrefixArguments = @('-3')
}
$relayPath = Join-Path $PSScriptRoot "vicon_sync_relay.py"
$logPath = Join-Path $PSScriptRoot "vicon_sync_events.jsonl"
if ($Direction -in @("auto", "phone-controls-nexus") -and [string]::IsNullOrWhiteSpace($NexusSession) -and $NexusRoot.Count -eq 0) {
    throw "-NexusSession or at least one -NexusRoot is required when Direction is auto or phone-controls-nexus."
}
if (-not [string]::IsNullOrWhiteSpace($NexusSession) -and -not (Test-Path -LiteralPath $NexusSession -PathType Container)) {
    throw "Nexus session directory is not accessible: $NexusSession"
}
foreach ($root in $NexusRoot) {
    if (-not (Test-Path -LiteralPath $root -PathType Container)) {
        throw "Nexus data root is not accessible: $root"
    }
}
$arguments = $pythonPrefixArguments + @(
    ('"{0}"' -f $relayPath),
    '--direction', $Direction,
    '--nexus-host', ('"{0}"' -f $NexusHost),
    '--nexus-port', $NexusPort,
    '--phone-port', $PhonePort,
    '--trigger-delay-ms', $TriggerDelayMS,
    '--token', ('"{0}"' -f $PairingToken),
    '--log', ('"{0}"' -f $logPath)
)
if (-not [string]::IsNullOrWhiteSpace($NexusSession)) {
    $arguments += @('--nexus-session', ('"{0}"' -f $NexusSession))
}
foreach ($root in $NexusRoot) {
    $arguments += @('--nexus-root', ('"{0}"' -f $root))
}
$arguments = $arguments -join ' '

# Updating a task's action does not replace an already-running instance. Stop
# both the old task and any manually launched copy of this exact relay first,
# otherwise the legacy process keeps ownership of TCP 51002 and the phone never
# receives the direction handshake.
$managedTaskNames = @($taskName, $legacyTaskName)
foreach ($managedTaskName in $managedTaskNames) {
    $existingTask = Get-ScheduledTask -TaskName $managedTaskName -ErrorAction SilentlyContinue
    if ($null -ne $existingTask) {
        Stop-ScheduledTask -TaskName $managedTaskName -ErrorAction SilentlyContinue
        for ($attempt = 0; $attempt -lt 20; $attempt++) {
            $state = (Get-ScheduledTask -TaskName $managedTaskName -ErrorAction SilentlyContinue).State
            if ($state -ne 'Running') { break }
            Start-Sleep -Milliseconds 250
        }
    }
}
$relayProcesses = Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -and $_.CommandLine.Contains('vicon_sync_relay.py')
}
foreach ($process in $relayProcesses) {
    Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
}
if ($null -ne (Get-ScheduledTask -TaskName $legacyTaskName -ErrorAction SilentlyContinue)) {
    Unregister-ScheduledTask -TaskName $legacyTaskName -Confirm:$false
}

$action = New-ScheduledTaskAction `
    -Execute $pythonLauncher `
    -Argument $arguments `
    -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Days 3650) `
    -RestartCount 10 `
    -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Synchronizes Vicon Nexus and the OpenCap iPhone app ($Direction)." `
    -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
Start-Sleep -Seconds 1
$taskState = (Get-ScheduledTask -TaskName $taskName).State
if ($taskState -ne 'Running') {
    throw "The relay task exited during startup. Check the Nexus session path and vicon_sync_relay.log."
}
Write-Host "Installed and started scheduled task: $taskName"
Write-Host "Direction: $Direction"
if (-not [string]::IsNullOrWhiteSpace($NexusSession)) {
    Write-Host "Nexus session: $NexusSession"
}
foreach ($root in $NexusRoot) {
    Write-Host "Nexus data root: $root"
}
