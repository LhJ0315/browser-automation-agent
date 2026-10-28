# Ensure Redis in WSL is reachable FROM WINDOWS (not just inside WSL).
# Usage:
#   .\scripts\start-redis-wsl.ps1
#
# Root cause of the worker error cascade (10053 → 10061):
#   WSL2 auto-shuts the VM after ~20s with no interactive console.
#   That kills redis-server mid-connection. Fix: %USERPROFILE%\.wslconfig
#   with vmIdleTimeout=-1 (this script creates/updates it, then may need
#   `wsl --shutdown` once for the setting to take effect).

$ErrorActionPreference = "Stop"

function Test-RedisTcp([string]$HostName, [int]$TimeoutMs = 1500) {
  try {
    $c = New-Object Net.Sockets.TcpClient
    $iar = $c.BeginConnect($HostName, 6379, $null, $null)
    $ok = $iar.AsyncWaitHandle.WaitOne($TimeoutMs, $false)
    if (-not $ok) { try { $c.Close() } catch {}; return $false }
    $c.EndConnect($iar)
    $c.Close()
    return $true
  } catch {
    return $false
  }
}

function Ensure-WslIdleDisabled {
  $path = Join-Path $env:USERPROFILE ".wslconfig"
  $marker = "vmIdleTimeout=-1"
  $needed = $true
  if (Test-Path $path) {
    $text = Get-Content $path -Raw
    if ($text -match '(?m)^\s*vmIdleTimeout\s*=\s*-1\s*$') {
      $needed = $false
    }
  }
  if (-not $needed) {
    Write-Host "[redis] .wslconfig already has vmIdleTimeout=-1"
    return $false
  }

  $block = @"
# Keep WSL2 alive while Redis / background services run.
# Default idle timeout (~20s with no interactive console) shuts the whole VM,
# which kills redis-server and produces Windows Error 10053 / 10061.
[wsl2]
vmIdleTimeout=-1
"@
  if (Test-Path $path) {
    $existing = Get-Content $path -Raw
    if ($existing -match '(?m)^\s*vmIdleTimeout\s*=') {
      $existing = [regex]::Replace($existing, '(?m)^\s*vmIdleTimeout\s*=.*$', 'vmIdleTimeout=-1')
    } elseif ($existing -match '(?m)^\s*\[wsl2\]\s*$') {
      $existing = [regex]::Replace($existing, '(?m)^\s*\[wsl2\]\s*$', "[wsl2]`r`nvmIdleTimeout=-1")
    } else {
      $existing = $existing.TrimEnd() + "`r`n`r`n[wsl2]`r`nvmIdleTimeout=-1`r`n"
    }
    Set-Content -Path $path -Value $existing -Encoding utf8
  } else {
    Set-Content -Path $path -Value $block -Encoding utf8
  }
  Write-Host "[redis] wrote $path (vmIdleTimeout=-1)"
  Write-Host "[redis] THIS CHANGE NEEDS: wsl --shutdown   then re-run this script"
  return $true
}

$idleChanged = Ensure-WslIdleDisabled
if ($idleChanged) {
  Write-Host "[redis] shutting down WSL so .wslconfig takes effect..."
  wsl --shutdown
  Start-Sleep -Seconds 2
}

Write-Host "[redis] starting service inside WSL Ubuntu..."
wsl -d Ubuntu -u root -- bash -lc @"
set -e
sed -i 's/^daemonize yes/daemonize no/' /etc/redis/redis.conf
sed -i 's/^protected-mode yes/protected-mode no/' /etc/redis/redis.conf
grep -q '^bind 0.0.0.0' /etc/redis/redis.conf || sed -i 's/^bind .*/bind 0.0.0.0/' /etc/redis/redis.conf
if grep -q '^supervised ' /etc/redis/redis.conf; then
  sed -i 's/^supervised .*/supervised systemd/' /etc/redis/redis.conf
else
  echo 'supervised systemd' >> /etc/redis/redis.conf
fi
service redis-server start
sleep 1
redis-cli CONFIG SET protected-mode no >/dev/null
# Hold an interactive-ish session open briefly so the distro stays warm
# while Windows localhost proxy binds (extra safety even with vmIdleTimeout).
redis-cli ping
"@ | Out-Host

$wslip = ((wsl -d Ubuntu -- hostname -I).ToString().Trim() -split '\s+')[0]
Write-Host "[redis] WSL eth0 IP = $wslip"

$localhostOk = Test-RedisTcp '127.0.0.1'
$wslOk = if ($wslip) { Test-RedisTcp $wslip } else { $false }

Write-Host "[redis] Windows -> 127.0.0.1:6379 = $localhostOk"
Write-Host "[redis] Windows -> ${wslip}:6379 = $wslOk"

if ($localhostOk) {
  $url = 'redis://127.0.0.1:6379/0'
} elseif ($wslOk) {
  $url = "redis://${wslip}:6379/0"
  Write-Host "[redis] localhost forwarding is broken; use WSL IP instead."
  Write-Host "[redis] set in this shell (and .env if you want it sticky):"
  Write-Host "  `$env:REDIS_URL = '$url'"
} else {
  throw @"
Redis is up inside WSL (redis-cli ping), but Windows cannot TCP-connect to either
  127.0.0.1:6379 or ${wslip}:6379
This is a WSL networking / localhost-forwarding problem, not an RQ bug.
Try: wsl --shutdown  then re-run this script; or install Docker Desktop Redis.
"@
}

$env:PYTHONPATH = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
python -c "from redis import Redis; import os; u=os.environ.get('REDIS_URL','$url'); print('python ping', u, Redis.from_url(u, socket_connect_timeout=2).ping())"

Write-Host "[redis] OK for worker/API with REDIS_URL=$url"
if (-not $localhostOk -and $wslOk) {
  $env:REDIS_URL = $url
  Write-Host "[redis] REDIS_URL exported for THIS PowerShell session only."
}

Write-Host "[redis] tip: keep one WSL window open OR rely on vmIdleTimeout=-1 so Redis is not killed after ~20s."
