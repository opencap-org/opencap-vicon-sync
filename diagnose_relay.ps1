param(
    [string]$RelayHost = "127.0.0.1",
    [int]$PhonePort = 51002,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('\A[A-Za-z0-9_-]+\z')]
    [ValidateNotNullOrEmpty()]
    [string]$PairingToken,
    [ValidateSet("", "nexus-controls-phone", "phone-controls-nexus")]
    [string]$RequestedDirection = "",
    [int]$TimeoutMilliseconds = 3000
)

$ErrorActionPreference = "Stop"
$client = [System.Net.Sockets.TcpClient]::new()
try {
    $pending = $client.BeginConnect($RelayHost, $PhonePort, $null, $null)
    if (-not $pending.AsyncWaitHandle.WaitOne($TimeoutMilliseconds)) {
        throw "Timed out connecting to $RelayHost`:$PhonePort."
    }
    $client.EndConnect($pending)
    $client.ReceiveTimeout = $TimeoutMilliseconds
    $client.SendTimeout = $TimeoutMilliseconds
    $stream = $client.GetStream()
    $encoding = [System.Text.UTF8Encoding]::new($false)
    $writer = [System.IO.StreamWriter]::new($stream, $encoding)
    $writer.NewLine = "`n"
    $writer.AutoFlush = $true
    $reader = [System.IO.StreamReader]::new($stream, $encoding)
    $helloObject = @{
        type = "hello"
        protocol = 1
        client_id = "windows-relay-diagnostic"
        app_version = "diagnostic"
        token = $PairingToken
    }
    if (-not [string]::IsNullOrWhiteSpace($RequestedDirection)) {
        $helloObject.requested_direction = $RequestedDirection
        $helloObject.request_direction_change = $true
    }
    $hello = $helloObject | ConvertTo-Json -Compress
    $writer.WriteLine($hello)
    $line = $reader.ReadLine()
    if ([string]::IsNullOrWhiteSpace($line)) {
        throw "Relay closed the connection without a hello response. Check the pairing token."
    }
    $response = $line | ConvertFrom-Json
    if ($response.type -eq "error") {
        throw "Relay error: $($response.message)"
    }
    if ($response.type -ne "hello_ack") {
        throw "Unexpected relay response: $line"
    }
    if ($null -eq $response.relay_version) {
        throw "LEGACY RELAY DETECTED on $RelayHost`:$PhonePort. Stop the old task/process and install the updated relay."
    }
    Write-Host "Relay version: $($response.relay_version)"
    Write-Host "Direction: $($response.direction)"
    Write-Host "Nexus state: $($response.nexus_state)"
    Write-Host "Capabilities: $($response.capabilities -join ', ')"
    if (-not [string]::IsNullOrWhiteSpace($RequestedDirection) -and $response.direction -ne $RequestedDirection) {
        throw "Relay did not enter $RequestedDirection. Direction switch error: $($response.direction_switch_error)"
    }
    Write-Host "Relay handshake is ready." -ForegroundColor Green
} finally {
    $client.Dispose()
}
