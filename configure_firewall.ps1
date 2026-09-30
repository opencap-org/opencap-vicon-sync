# Run once from an elevated PowerShell prompt on the Nexus PC.
param(
    [int]$NexusPort = 49150,
    [int]$PhonePort = 51002
)

$ErrorActionPreference = "Stop"
$ruleGroup = "OpenCap Vicon Sync"
$legacyRuleGroup = "OptiGait Vicon Sync"

Get-NetFirewallRule -Group $ruleGroup -ErrorAction SilentlyContinue | Remove-NetFirewallRule
Get-NetFirewallRule -Group $legacyRuleGroup -ErrorAction SilentlyContinue | Remove-NetFirewallRule
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

Write-Host "Configured private-network firewall rules for UDP $NexusPort and TCP $PhonePort."
