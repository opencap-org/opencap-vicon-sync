# Run in Windows PowerShell 5.1. This never installs tasks or firewall rules.
$ErrorActionPreference = 'Stop'
$repositoryRoot = Split-Path -Parent $PSScriptRoot
foreach ($file in Get-ChildItem -LiteralPath $repositoryRoot -Filter '*.ps1') {
    $tokens = $null
    $parseErrors = $null
    $null = [System.Management.Automation.Language.Parser]::ParseFile(
        $file.FullName, [ref]$tokens, [ref]$parseErrors
    )
    if ($parseErrors.Count -gt 0) {
        throw "PowerShell syntax errors in $($file.Name): $parseErrors"
    }
}

# Load only the quoting function; do not execute the installer.
$tokens = $null
$parseErrors = $null
$installer = [System.Management.Automation.Language.Parser]::ParseFile(
    (Join-Path $repositoryRoot 'install_relay_task.ps1'), [ref]$tokens, [ref]$parseErrors
)
$quoteFunction = $installer.Find({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq 'ConvertTo-WindowsArgument'
}, $true)
if ($null -eq $quoteFunction) { throw 'Task argument quoting function not found.' }
Invoke-Expression $quoteFunction.Extent.Text

# Exercise the same Windows argv parsing used by pythonw.exe and Task Scheduler.
$expected = @('C:\Vicon Data\', 'C:\', 'C:\Research Data\Session 1',
    'lab_token-123', 'embedded"quote', 'backslash\"quote', '')
$arguments = @('-c', 'import json, sys; print(json.dumps(sys.argv[1:]))') + $expected
$start = New-Object System.Diagnostics.ProcessStartInfo
$start.FileName = (Get-Command python.exe -ErrorAction Stop).Source
$start.Arguments = ($arguments | ForEach-Object { ConvertTo-WindowsArgument $_ }) -join ' '
$start.UseShellExecute = $false
$start.RedirectStandardOutput = $true
$start.RedirectStandardError = $true
$process = New-Object System.Diagnostics.Process
$process.StartInfo = $start
try {
    $null = $process.Start()
    if (-not $process.WaitForExit(10000)) {
        $process.Kill()
        throw 'Python argument check timed out.'
    }
    $output = $process.StandardOutput.ReadToEnd()
    if ($process.ExitCode -ne 0) { throw $process.StandardError.ReadToEnd() }
    # Windows PowerShell 5.1 returns the JSON array as one pipeline object.
    # Direct assignment preserves that array without adding an outer array.
    $actual = $output | ConvertFrom-Json
    if ($actual.Count -ne $expected.Count) { throw "Incorrect argv count: $output" }
    for ($index = 0; $index -lt $expected.Count; $index++) {
        if ($actual[$index] -cne $expected[$index]) {
            throw "Task argument $index was not preserved: $output"
        }
    }
} finally {
    $process.Dispose()
}
Write-Host 'Windows script syntax and task argument checks passed.'
