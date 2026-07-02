<#
fpbrowser2api Windows PowerShell service script.

Usage:
  powershell -ExecutionPolicy Bypass -File .\fpbrowser2api_service.ps1 start|stop|restart|status

Optional environment variables:
  APP_BIN
  PYTHON_BIN
  PID_FILE
  LOG_FILE
  LOG_ERR_FILE
  DEBUG_LOG_FILE
  APP_LOG_FILE
  LOGS_DIR
#>

[CmdletBinding()]
param(
  [Parameter(Position = 0)]
  [ValidateSet("start", "stop", "restart", "status")]
  [string]$Command = ""
)

$ErrorActionPreference = 'Stop'

function Get-AppDir {
  if ($PSScriptRoot) { return $PSScriptRoot }
  return (Split-Path -Parent $MyInvocation.MyCommand.Path)
}

$APP_DIR = Get-AppDir

function Get-EnvOrDefault([string]$name, [string]$defaultValue) {
  $v = [Environment]::GetEnvironmentVariable($name)
  if ([string]::IsNullOrWhiteSpace($v)) { return $defaultValue }
  return $v
}

$PID_FILE       = Get-EnvOrDefault 'PID_FILE'       (Join-Path $APP_DIR 'fpbrowser2api.pid')
$LOG_FILE       = Get-EnvOrDefault 'LOG_FILE'       (Join-Path $APP_DIR 'fpbrowser2api.out')
$LOG_ERR_FILE   = Get-EnvOrDefault 'LOG_ERR_FILE'   ($LOG_FILE + '.err')
$LOGS_DIR       = Get-EnvOrDefault 'LOGS_DIR'       (Join-Path $APP_DIR 'logs')
$DEBUG_LOG_FILE = Get-EnvOrDefault 'DEBUG_LOG_FILE' (Join-Path $APP_DIR 'logs.txt')
$APP_LOG_FILE   = Get-EnvOrDefault 'APP_LOG_FILE'   (Join-Path $APP_DIR 'app.log')

function Resolve-PythonBin {
  $py = [Environment]::GetEnvironmentVariable('PYTHON_BIN')
  if (-not [string]::IsNullOrWhiteSpace($py)) { return $py }

  $candidates = @(
    (Join-Path $APP_DIR '.venv\Scripts\python.exe'),
    (Join-Path $APP_DIR 'venv\Scripts\python.exe'),
    (Join-Path $APP_DIR '.venv\bin\python'),
    (Join-Path $APP_DIR 'venv\bin\python')
  )

  foreach ($c in $candidates) {
    if (Test-Path $c) { return $c }
  }

  # 最后兜底：PATH 里的 python
  return 'python'
}

$PYTHON_BIN = Resolve-PythonBin

function Resolve-AppLaunch {
  $appBin = [Environment]::GetEnvironmentVariable('APP_BIN')
  if (-not [string]::IsNullOrWhiteSpace($appBin)) {
    if (-not (Test-Path $appBin)) {
      throw ('APP_BIN not found: {0}' -f $appBin)
    }
    return @{
      FilePath = $appBin
      ArgumentList = @()
      Display = $appBin
    }
  }

  # 打包发布模式：优先运行同目录下的 fpbrowser2api.exe
  $exe = Join-Path $APP_DIR 'fpbrowser2api.exe'
  if (Test-Path $exe) {
    return @{
      FilePath = $exe
      ArgumentList = @()
      Display = $exe
    }
  }

  # 源码开发模式：回退到 python main.py
  $mainPy = Join-Path $APP_DIR 'main.py'
  if (Test-Path $mainPy) {
    return @{
      FilePath = $PYTHON_BIN
      ArgumentList = @($mainPy)
      Display = ('{0} {1}' -f $PYTHON_BIN, $mainPy)
    }
  }

  throw ('未找到可运行入口：{0} 或 {1}；打包发布目录应包含 fpbrowser2api.exe' -f $exe, $mainPy)
}

function Get-PidFromFile {
  if (-not (Test-Path $PID_FILE)) { return $null }
  $raw = (Get-Content -Path $PID_FILE -ErrorAction SilentlyContinue | Select-Object -First 1)
  if ([string]::IsNullOrWhiteSpace($raw)) { return $null }
  $procIdValue = 0
  if (-not [int]::TryParse($raw.Trim(), [ref]$procIdValue)) { return $null }
  return $procIdValue
}

function Test-IsRunning {
  $procId = Get-PidFromFile
  if (-not $procId) { return $false }
  try {
    $p = Get-Process -Id $procId -ErrorAction Stop
    return ($null -ne $p)
  } catch {
    return $false
  }
}

function Ensure-FileExists([string]$path) {
  $dir = Split-Path -Parent $path
  if (-not [string]::IsNullOrWhiteSpace($dir)) {
    New-Item -ItemType Directory -Path $dir -Force | Out-Null
  }
  if (-not (Test-Path $path)) {
    New-Item -ItemType File -Path $path -Force | Out-Null
  }
}

function Truncate-File([string]$path) {
  Ensure-FileExists $path
  Set-Content -Path $path -Value '' -Encoding UTF8
}

function Rotate-And-Truncate([string]$src, [string]$prefix) {
  New-Item -ItemType Directory -Path $LOGS_DIR -Force | Out-Null
  Ensure-FileExists $src

  $item = Get-Item -Path $src -ErrorAction SilentlyContinue
  if ($item -and $item.Length -gt 0) {
    $ts = Get-Date -Format 'yyyyMMdd_HHmmss'
    $rand = Get-Random -Minimum 10000 -Maximum 99999
    $dest = Join-Path $LOGS_DIR ('{0}_{1}_{2}.txt' -f $prefix, $ts, $rand)
    Move-Item -Path $src -Destination $dest -Force
    Write-Host ('Rotated log: {0} -> {1}' -f $src, $dest)
  }

  Truncate-File $src
}

function Start-ServiceProcess {
  if (Test-IsRunning) {
    $procId = Get-PidFromFile
    Write-Host ('fpbrowser2api already running (pid={0})' -f $procId)
    return
  }

  Set-Location -Path $APP_DIR

  # 启动前：备份并清空调试日志（logs.txt）
  Rotate-And-Truncate $DEBUG_LOG_FILE "logs"

  # 启动前：备份并清空 app.log（当 log_to_file=true 才会写入）
  if (Test-Path $APP_LOG_FILE) {
    Rotate-And-Truncate $APP_LOG_FILE "app"
  }

  # 启动前：清空服务输出日志
  Truncate-File $LOG_FILE
  Truncate-File $LOG_ERR_FILE

  $launch = Resolve-AppLaunch

  # 后台运行：
  # - 打包发布目录：fpbrowser2api.exe
  # - 源码开发目录：python main.py
  $startParams = @{
    FilePath               = $launch.FilePath
    ArgumentList           = $launch.ArgumentList
    WorkingDirectory       = $APP_DIR
    WindowStyle            = 'Hidden'
    RedirectStandardOutput = $LOG_FILE
    RedirectStandardError  = $LOG_ERR_FILE
    PassThru               = $true
  }
  $proc = Start-Process @startParams

  Set-Content -Path $PID_FILE -Value $proc.Id -Encoding ASCII

  Start-Sleep -Seconds 1
  if (Test-IsRunning) {
    Write-Host ('fpbrowser2api started (pid={0}), cmd={1}, log={2}, err={3}' -f (Get-PidFromFile), $launch.Display, $LOG_FILE, $LOG_ERR_FILE)
    return
  }

  throw ('fpbrowser2api start failed, see logs: {0} / {1}' -f $LOG_FILE, $LOG_ERR_FILE)
}

function Stop-ServiceProcess {
  $procId = Get-PidFromFile
  if (-not $procId) {
    if (Test-Path $PID_FILE) { Remove-Item -Path $PID_FILE -Force -ErrorAction SilentlyContinue }
    Write-Host ('fpbrowser2api not running (pid file missing/empty: {0})' -f $PID_FILE)
    return
  }

  try {
    $p = Get-Process -Id $procId -ErrorAction Stop
  } catch {
    Remove-Item -Path $PID_FILE -Force -ErrorAction SilentlyContinue
    Write-Host 'fpbrowser2api process not found, pid file cleared'
    return
  }

  Write-Host ('Stopping fpbrowser2api (pid={0})...' -f $procId)
  try { Stop-Process -Id $procId -ErrorAction SilentlyContinue } catch {}

  $deadline = (Get-Date).AddSeconds(30)
  while ((Get-Date) -lt $deadline) {
    try {
      Get-Process -Id $procId -ErrorAction Stop | Out-Null
      Start-Sleep -Seconds 1
    } catch {
      break
    }
  }

  $stillRunning = $false
  try {
    Get-Process -Id $procId -ErrorAction Stop | Out-Null
    $stillRunning = $true
  } catch {
    $stillRunning = $false
  }

  if ($stillRunning) {
    Write-Host ('Stop timeout, force kill (pid={0})' -f $procId)
    try { Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue } catch {}
  }

  Remove-Item -Path $PID_FILE -Force -ErrorAction SilentlyContinue
  Write-Host 'fpbrowser2api stopped'
}

function Show-Status {
  if (Test-IsRunning) {
    Write-Host ('fpbrowser2api running (pid={0})' -f (Get-PidFromFile))
  } else {
    Write-Host 'fpbrowser2api not running'
  }
}

function Show-Usage {
  Write-Host 'Usage:'
  Write-Host '  powershell -ExecutionPolicy Bypass -File .\fpbrowser2api_service.ps1 start|stop|restart|status'
  Write-Host ''
  Write-Host 'Optional environment variables:'
  Write-Host '  APP_BIN=...\fpbrowser2api.exe'
  Write-Host '  PYTHON_BIN=...\python.exe'
  Write-Host '  PID_FILE=...\fpbrowser2api.pid'
  Write-Host '  LOG_FILE=...\fpbrowser2api.out'
  Write-Host '  LOG_ERR_FILE=...\fpbrowser2api.err'
  Write-Host '  DEBUG_LOG_FILE=...\logs.txt'
  Write-Host '  APP_LOG_FILE=...\app.log'
  Write-Host '  LOGS_DIR=...\logs'
}

switch ($Command) {
  "start"   { Start-ServiceProcess }
  "stop"    { Stop-ServiceProcess }
  "restart" { Stop-ServiceProcess; Start-ServiceProcess }
  "status"  { Show-Status }
  default   { Show-Usage; exit 1 }
}

