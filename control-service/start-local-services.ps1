$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$consoleRoot = Join-Path $projectRoot 'console'
$brokerConfig = Join-Path $PSScriptRoot 'mosquitto\mosquitto.windows.conf'
$brokerExe = 'C:\Program Files\mosquitto\mosquitto.exe'
$pythonExe = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) {
    $pythonExe = (Get-Command python.exe -ErrorAction Stop).Source
}

function Test-AirProjectListener([int]$Port, [string]$Address = '') {
    # Reading the system TCP table can require elevation on some Windows
    # installations. A real loopback connection is both permission-free and
    # a stronger proof that the service is reachable; it also prevents this
    # startup script from launching duplicate API processes.
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $pending = $client.ConnectAsync('127.0.0.1', $Port)
        if (-not $pending.Wait(400)) { return $false }
        return $client.Connected
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

# The LAN broker must listen on all interfaces. A separate loopback-only
# Mosquitto installation, if present, does not satisfy this check.
if (-not (Test-AirProjectListener 1883)) {
    Start-Process -WindowStyle Hidden -FilePath $brokerExe -ArgumentList '-c', $brokerConfig, '-v'
}

if (-not (Test-AirProjectListener 8780 '127.0.0.1')) {
    Start-Process -WindowStyle Hidden -FilePath $pythonExe -ArgumentList 'dev_server.py' -WorkingDirectory $PSScriptRoot
}

if (-not (Test-AirProjectListener 8765)) {
    Start-Process -WindowStyle Hidden -FilePath $pythonExe -ArgumentList '-m', 'http.server', '8765', '--directory', $consoleRoot -WorkingDirectory $projectRoot
}
