# Optional: install the relay as a per-user task that starts at Windows sign-in.
param(
    [ValidatePattern('\A[A-Za-z0-9_-]+\z')]
    [ValidateNotNullOrEmpty()]
    [string]$PairingToken = "opencap",
    [ValidateSet("auto", "nexus-controls-phone", "phone-controls-nexus")]
    [string]$Direction = "nexus-controls-phone",
    [string]$NexusHost = "",
    [string]$NexusSession = "",
    [string[]]$NexusRoot = @(),
    [int]$NexusPort = 49150,
    [int]$PhonePort = 51002,
    [int]$TriggerDelayMS = 250,
    [switch]$DisableDiscovery
)

$ErrorActionPreference = "Stop"
$taskName = "OpenCap Vicon Sync Relay"
function ConvertTo-WindowsArgument([string]$Value) {
    # Windows doubles backslashes before quotes, including a closing quote.
    # This preserves paths with spaces and data roots ending in a backslash.
    $escaped = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
    return '"' + $escaped + '"'
}

$pythonExecutable = ([string](& py.exe -3 -c "import sys; sys.exit('Python 3.10 or newer is required') if sys.version_info < (3, 10) else print(sys.executable)")).Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($pythonExecutable)) {
    throw "Python could not start. Install Python 3.10 or newer with the Windows py launcher."
}
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
if ($Direction -in @("auto", "phone-controls-nexus") -and [string]::IsNullOrWhiteSpace($NexusHost)) {
    throw "-NexusHost must specify this PC's Nexus adapter address when Direction is auto or phone-controls-nexus."
}
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
if (-not [string]::IsNullOrWhiteSpace($NexusSession)) {
    $NexusSession = (Get-Item -LiteralPath $NexusSession).FullName
}
$NexusRoot = @($NexusRoot | ForEach-Object { (Get-Item -LiteralPath $_).FullName })
$arguments = $pythonPrefixArguments + @(
    $relayPath,
    '--direction', $Direction,
    '--nexus-port', $NexusPort,
    '--phone-port', $PhonePort,
    '--trigger-delay-ms', $TriggerDelayMS,
    "--token=$PairingToken",
    '--log', $logPath
)
if (-not [string]::IsNullOrWhiteSpace($NexusHost)) {
    $arguments += @('--nexus-host', $NexusHost)
}
if ($DisableDiscovery) { $arguments += '--no-discovery' }
if (-not [string]::IsNullOrWhiteSpace($NexusSession)) {
    $arguments += @('--nexus-session', $NexusSession)
}
foreach ($root in $NexusRoot) {
    $arguments += @('--nexus-root', $root)
}
$arguments = ($arguments | ForEach-Object { ConvertTo-WindowsArgument ([string]$_) }) -join ' '

# Updating a task's action does not replace an already-running instance. Stop
# the task and any manually launched copy of this exact relay first,
# otherwise the previous process keeps ownership of TCP 51002 and the phone never
# receives the direction handshake.
$existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($null -ne $existingTask) {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        $state = (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue).State
        if ($state -ne 'Running') { break }
        Start-Sleep -Milliseconds 250
    }
}
$relayProcesses = Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -and $_.CommandLine.IndexOf($relayPath, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
}
foreach ($process in $relayProcesses) {
    Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
}

$action = New-ScheduledTaskAction `
    -Execute $pythonLauncher `
    -Argument $arguments `
    -WorkingDirectory $PSScriptRoot
$windowsUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $windowsUser
$principal = New-ScheduledTaskPrincipal -UserId $windowsUser -LogonType Interactive -RunLevel Limited
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
    -Principal $principal `
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
