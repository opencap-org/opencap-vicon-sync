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
$relayPath = Join-Path $PSScriptRoot "vicon_sync_relay.py"
$arguments = @(
    $relayPath,
    '--direction', $Direction,
    '--nexus-host', $NexusHost,
    '--nexus-port', $NexusPort,
    '--phone-port', $PhonePort,
    '--trigger-delay-ms', $TriggerDelayMS,
    '--token', $PairingToken
)
if ($Direction -in @("auto", "phone-controls-nexus")) {
    if ([string]::IsNullOrWhiteSpace($NexusSession) -and $NexusRoot.Count -eq 0) {
        throw "-NexusSession or at least one -NexusRoot is required when Direction is auto or phone-controls-nexus."
    }
    if (-not [string]::IsNullOrWhiteSpace($NexusSession) -and -not (Test-Path -LiteralPath $NexusSession -PathType Container)) {
        throw "Nexus session directory is not accessible: $NexusSession"
    }
    if (-not [string]::IsNullOrWhiteSpace($NexusSession)) {
        $arguments += @('--nexus-session', $NexusSession)
    }
    foreach ($root in $NexusRoot) {
        if (-not (Test-Path -LiteralPath $root -PathType Container)) {
            throw "Nexus data root is not accessible: $root"
        }
        $arguments += @('--nexus-root', $root)
    }
}
Write-Host "OpenCap relay direction: $Direction"
if ($Direction -in @("auto", "phone-controls-nexus")) {
    Write-Host "Nexus Receive destination: $NexusHost`:$NexusPort"
    if (-not [string]::IsNullOrWhiteSpace($NexusSession)) {
        Write-Host "Preferred Nexus session: $NexusSession"
    }
    foreach ($root in $NexusRoot) {
        Write-Host "Nexus data root: $root"
    }
}
py -3 @arguments
