# Run once from an elevated PowerShell prompt on the Nexus PC.
param(
    [int]$NexusPort = 49150,
    [int]$PhonePort = 51002
)

$ErrorActionPreference = "Stop"
$ruleGroup = "OpenCap Vicon Sync"

Get-NetFirewallRule -Group $ruleGroup -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule `
    -DisplayName "OpenCap Vicon UDP input" `
    -Group $ruleGroup `
    -Direction Inbound `
    -Action Allow `
    -Protocol UDP `
    -LocalPort $NexusPort `
    -Profile Private
New-NetFirewallRule `
    -DisplayName "OpenCap iPhone TCP connection" `
    -Group $ruleGroup `
    -Direction Inbound `
    -Action Allow `
    -Protocol TCP `
    -LocalPort $PhonePort `
    -Profile Private

New-NetFirewallRule `
    -DisplayName "OpenCap relay discovery" `
    -Group $ruleGroup `
    -Direction Inbound `
    -Action Allow `
    -Protocol UDP `
    -LocalPort 5353 `
    -RemoteAddress LocalSubnet `
    -Profile Private

Write-Host "Configured private-network firewall rules for UDP $NexusPort, TCP $PhonePort, and Bonjour UDP 5353."
