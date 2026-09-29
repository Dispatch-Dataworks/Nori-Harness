# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

<#
  Start / stop / status Nori's server (nori/server.py) as a detached
  background process that survives closing the terminal.

    pwsh nori/nori_ctl.ps1 start
    pwsh nori/nori_ctl.ps1 stop
    pwsh nori/nori_ctl.ps1 status
    pwsh nori/nori_ctl.ps1 restart
    pwsh nori/nori_ctl.ps1 ensure     # start/restart the server if unhealthy

  Needs OPENROUTER_API_KEY in the sibling "<repo>-env" directory's
  nori.env -- kept outside this project folder entirely. See server.py's
  own ENV_PATH constant, and .env.example for every other key.

  No in-process watchdog, deliberately: an earlier version ran one
  alongside an OS-level scheduled task that ALSO called this same
  `ensure` action on its own independent cycle, with no coordination
  between the two -- which caused a real, sustained crash-loop: two
  uncoordinated supervisors, each free to force-kill whatever the other
  had just started. One supervisor, not a lock between two, is the
  actual fix -- a lock would just paper over a redundancy that
  shouldn't exist, and an in-process watchdog dies with the very
  process it's meant to guard, exactly when it's needed.

  This script does NOT register anything with the OS on its own --
  nothing brings a crashed server back until a person runs `start`
  again, or you set up exactly ONE external supervisor yourself (a
  Windows Scheduled Task, systemd timer, cron entry, or your own
  service manager) calling `pwsh nori_ctl.ps1 ensure` on a short
  interval. Set up only one; see the crash-loop reasoning above for why
  a second one is actively dangerous, not just redundant. See
  [Deployment & supervision](docs/deployment-and-watchdog.md) for the
  full walkthrough, including the Docker path, where the container
  runtime's own restart policy is that one supervisor and this script
  isn't needed at all.
#>
param(
  [Parameter(Mandatory)]
  [ValidateSet('start', 'stop', 'status', 'restart', 'ensure')]
  [string]$Action
)
$ErrorActionPreference = 'Stop'
$here      = $PSScriptRoot
$pidFile   = Join-Path $here '.nori.pid'
$errLog    = Join-Path $here '.nori.err'
$port      = if ($env:NORI_PORT) { $env:NORI_PORT } else { '8877' }
$healthUrl = "http://127.0.0.1:$port/healthz"
# Supervision record (after a real post-crash boot where the
# supervisor reported "ok" for an app that was never serving). Three
# things this script now owns
# that it didn't: an outcome log a person can read WITHOUT elevation
# (.supervision.jsonl, one JSON event per line -- what happened, why,
# how long it waited), the launch's own crash output (.nori.boot.err,
# written by boot.py -- an import-time crash used to vanish into a hidden
# process's stderr), and a lock so two callers can't race each other.
$eventLog  = Join-Path $here '.supervision.jsonl'
$bootErr   = Join-Path $here '.nori.boot.err'
$lockFile  = Join-Path $here '.ctl.lock'
$caller    = if ($env:SUPERVISOR_CALLER) { $env:SUPERVISOR_CALLER } else { 'manual' }
# How long start/ensure wait for the server to actually SERVE before
# calling it a failure. Generous on purpose: a cold boot after a hard
# power loss took well over a minute to bring up even the editor.
$startTimeoutS = if ($env:NORI_START_TIMEOUT_S) { [int]$env:NORI_START_TIMEOUT_S } else { 90 }

function Write-SupEvent([string]$kind, [bool]$ok, [string]$detail, [hashtable]$extra = @{}) {
  $rec = [ordered]@{ ts = (Get-Date -Format s); app = 'nori'; caller = $caller; action = $Action;
                     kind = $kind; ok = $ok; detail = $detail }
  foreach ($k in $extra.Keys) { $rec[$k] = $extra[$k] }
  $bytes = [Text.Encoding]::UTF8.GetBytes(($rec | ConvertTo-Json -Compress -Depth 4) + "`n")
  for ($i = 0; $i -lt 4; $i++) {
    try {
      $fs = [IO.File]::Open($eventLog, 'Append', 'Write', 'ReadWrite')
      try { $fs.Write($bytes, 0, $bytes.Length) } finally { $fs.Dispose() }
      break
    } catch { Start-Sleep -Milliseconds (100 * ($i + 1)) }
  }
  # Bounded, like every other log here.
  try { if ((Get-Item $eventLog).Length -gt 262144) { Set-Content $eventLog (Get-Content $eventLog -Tail 800) } } catch { }
}

$script:lockStream = $null
function Enter-Lock {
  # Exclusive-open lock file, released automatically when this process
  # ends (however it ends). Real race this closes: at boot, StartAll and
  # the periodic supervisor both drove this script at once (Task
  # Scheduler's own log shows the two instances overlapping), each free to
  # start or kill what the other was in the middle of starting.
  $deadline = (Get-Date).AddSeconds(150); $waited = $false
  while ($true) {
    try {
      $script:lockStream = [IO.File]::Open($lockFile, 'OpenOrCreate', 'ReadWrite', 'None')
      if ($waited) { Write-Host "got the control lock after another start/ensure finished" }
      return $true
    } catch {
      $ex = $_.Exception; if ($ex.InnerException) { $ex = $ex.InnerException }
      if ($ex -is [UnauthorizedAccessException]) {
        Write-SupEvent 'lock' $false 'control lock file not accessible to this account -- proceeding WITHOUT mutual exclusion'
        return $true
      }
      if (-not $waited) { Write-Host "another start/ensure for this app is in progress -- waiting for it"; $waited = $true }
      if ((Get-Date) -gt $deadline) {
        Write-SupEvent 'lock' $false 'gave up after 150s waiting for another start/ensure to release the control lock'
        Write-Host "gave up waiting for the control lock" -ForegroundColor Red
        return $false
      }
      Start-Sleep -Milliseconds 500
    }
  }
}

function Get-PidFrom($file) {
  # Real bug, found 2026-09-17 investigating "nori is down after a reboot
  # but status said running": this used to just check whether A process
  # with the recorded PID existed at all -- Windows reuses PIDs after a
  # reboot, and a stale pidfile pointed straight at an unrelated svchost
  # process, which "existed" and made status/ensure believe the (actually
  # dead) server was fine. Same fix www_ctl.ps1 already had: record the
  # process's own start-time ticks alongside its PID, and only trust a
  # match if BOTH agree -- a coincidentally-reused PID can't also have
  # the same start time.
  if (Test-Path $file) {
    try {
      $record = Get-Content $file -Raw | ConvertFrom-Json
      $proc = Get-Process -Id $record.id -ErrorAction SilentlyContinue
      if ($proc -and $proc.StartTime.ToUniversalTime().Ticks -eq $record.started_ticks) {
        return [int]$record.id
      }
    } catch { }
  }
  return $null
}
function Set-PidFile($file, $proc) {
  @{ id = $proc.Id; started_ticks = $proc.StartTime.ToUniversalTime().Ticks } | ConvertTo-Json | Set-Content $file
}
function Test-Healthy {
  try { return (Invoke-WebRequest -Uri $healthUrl -TimeoutSec 3 -UseBasicParsing).StatusCode -eq 200 }
  catch { return $false }
}

function Test-Listening {
  try { return [bool](Get-NetTCPConnection -LocalPort ([int]$port) -State Listen -ErrorAction Stop | Select-Object -First 1) }
  catch { return $false }
}

function Wait-Serving([int]$procId, [int]$timeoutS) {
  # The definition of "up" everywhere in this script: the process is alive
  # AND /healthz answers -- never "a process exists". Returns as soon as
  # it's true; fails early if the process dies; otherwise gives up after
  # $timeoutS with the reason. (Real bug this replaces: start's success
  # test was "the process still exists 2 seconds after launch," which a
  # cold, slow boot passes long before anything is listening -- and the
  # supervisor built its "ok" on top of that.)
  $sw = [Diagnostics.Stopwatch]::StartNew()
  while ($true) {
    if (Test-Healthy) { return @{ ok = $true; seconds = [math]::Round($sw.Elapsed.TotalSeconds, 1) } }
    if (-not (Get-Process -Id $procId -ErrorAction SilentlyContinue)) {
      return @{ ok = $false; seconds = [math]::Round($sw.Elapsed.TotalSeconds, 1); reason = 'the process exited before it started serving' }
    }
    if ($sw.Elapsed.TotalSeconds -ge $timeoutS) {
      return @{ ok = $false; seconds = [math]::Round($sw.Elapsed.TotalSeconds, 1)
               reason = "the process is alive but was not serving on port $port after ${timeoutS}s" }
    }
    Start-Sleep -Seconds 1
  }
}

function Read-BootCrash([long]$fromOffset) {
  # Only what boot.py wrote SINCE this launch -- the file is append-only
  # history, most of it about earlier launches.
  if (-not (Test-Path $bootErr)) { return '' }
  try {
    $fs = [IO.File]::Open($bootErr, 'Open', 'Read', 'ReadWrite')
    try {
      if ($fs.Length -le $fromOffset) { return '' }
      $null = $fs.Seek($fromOffset, 'Begin')
      $text = [IO.StreamReader]::new($fs).ReadToEnd().Trim()
      if ($text.Length -gt 1800) { $text = $text.Substring($text.Length - 1800) }
      return $text
    } finally { $fs.Dispose() }
  } catch { return '' }
}

function Start-Nori {
  # Returns $true only when the server is actually serving; $false (with
  # the reason on screen AND in .supervision.jsonl) otherwise. Callers
  # turn that into their exit code -- this never exits the script itself.
  $tracked = Get-PidFrom $pidFile
  if ($tracked) {
    $w = Wait-Serving $tracked $startTimeoutS
    if ($w.ok) { Write-Host "server already running (pid $tracked)"; return $true }
    Write-Host "server (pid $tracked) is tracked but NOT serving: $($w.reason)" -ForegroundColor Red
    Write-SupEvent 'start' $false "tracked server (pid $tracked) is not serving: $($w.reason)" @{ pid = $tracked; waited_s = $w.seconds }
    return $false
  }
  # Second, independent gate (2026-09-17, found investigating a post-
  # reboot check): the pidfile check above only catches a process THIS
  # script itself started and is still tracking. It says nothing about
  # whether something else already has the port -- a stray process from
  # a different launch path, or the OS not having released the port yet.
  # Python's own allow_reuse_address means Windows can let a SECOND
  # process bind and LISTEN on the same port alongside a first one that's
  # still alive, rather than refusing outright -- confirmed for real, not
  # theoretical: this happened here, producing two simultaneously-
  # listening server.py processes after a stale pidfile went untracked.
  # Checking real health, not just the port, catches a listener that's
  # bound but not actually answering.
  if (Test-Healthy) {
    Write-Host "port $port is already answering healthy -- not starting a second server. If you're sure that's stale, stop whatever's using it first." -ForegroundColor Yellow
    return $true
  }
  # server writes its own .nori.log/.err (server.py's _redirect_logs) --
  # no -RedirectStandardOutput/Error here, same reason a sibling application's launcher doesn't:
  # that form makes Start-Process hang this launcher until the child exits.
  # What that leaves uncovered -- a crash BEFORE _redirect_logs runs (any
  # import-time failure) -- is what boot.py exists for: it runs server.py
  # in-process (same PID, so the pidfile logic is unchanged) and writes any
  # such crash to .nori.boot.err.
  # NORI_LIVE=1 (2026-09-13, live-data incident #5's guard -- store.py now
  # refuses to open a data directory without this or NORI_DATA_DIR set).
  # Set here, in the ONE place this script actually launches the real
  # server -- Start-Process inherits the current session's environment,
  # so the child process picks it up automatically.
  $env:NORI_LIVE = '1'
  $bootOffset = if (Test-Path $bootErr) { (Get-Item $bootErr).Length } else { 0 }
  $proc = Start-Process -FilePath python -ArgumentList (Join-Path $here 'boot.py') `
    -WorkingDirectory $here -WindowStyle Hidden -PassThru
  Start-Sleep -Milliseconds 500
  $live = Get-Process -Id $proc.Id -ErrorAction SilentlyContinue
  if ($live) { try { Set-PidFile $pidFile $live } catch { } }
  $w = Wait-Serving $proc.Id $startTimeoutS
  if ($w.ok) {
    Write-Host "server started (pid $($proc.Id)), serving after $($w.seconds)s - http://127.0.0.1:$port"
    Write-SupEvent 'start' $true "started and serving after $($w.seconds)s" @{ pid = $proc.Id; waited_s = $w.seconds }
    return $true
  }
  $crash = Read-BootCrash $bootOffset
  Write-Host "server FAILED to start: $($w.reason)" -ForegroundColor Red
  if ($crash) { Write-Host $crash }
  $dead = -not (Get-Process -Id $proc.Id -ErrorAction SilentlyContinue)
  if ($dead) { Remove-Item $pidFile -ErrorAction SilentlyContinue }
  Write-SupEvent 'start' $false $w.reason @{ pid = $proc.Id; waited_s = $w.seconds; exited = $dead; crash = $crash }
  return $false
}
function Stop-One($file, $label) {
  # Verify identity immediately before killing, not just when the
  # pidfile was last read (added after a real crash-loop investigation)
  # -- inlined here rather than calling
  # Get-PidFrom and killing its result a moment later, so there's no
  # separate lookup-then-later-act gap: read the pidfile, verify the
  # live process's start-time ticks still match, and kill that exact
  # verified process, in one sequence. A stale or reused PID that no
  # longer matches is treated as "nothing to stop," never force-killed
  # on the strength of a bare number alone.
  if (-not (Test-Path $file)) { Write-Host "$label not running"; return }
  try {
    $record = Get-Content $file -Raw | ConvertFrom-Json
    $proc = Get-Process -Id $record.id -ErrorAction SilentlyContinue
    if ($proc -and $proc.StartTime.ToUniversalTime().Ticks -eq $record.started_ticks) {
      Stop-Process -Id $proc.Id -Force
      Write-Host "$label stopped (pid $($proc.Id))"
    } else {
      Write-Host "$label not running (pidfile was stale)"
    }
  } catch {
    Write-Host "$label not running"
  }
  Remove-Item $file -ErrorAction SilentlyContinue
}
function Stop-LegacyWatchdogIfPresent {
  # One-time cleanup for the retired in-process watchdog (2026-09-18) --
  # a machine that started nori before this change may still have one
  # running; this stops it so retiring it doesn't just leave an orphan
  # process behind. Safe to call every time: a no-op once none is left.
  $legacy = Join-Path $here '.watchdog.pid'
  if (Test-Path $legacy) { Stop-One $legacy 'legacy watchdog' }
}

switch ($Action) {
  # Exit codes are the contract callers (start-all, ensure-all) build their
  # own verdicts on, so they mean what they say: 0 = the server is serving,
  # 1 = it is not (reason on screen and in .supervision.jsonl), 2 = crash-
  # loop backoff (ensure only), 3 = couldn't get the control lock.
  'start'   { if (-not (Enter-Lock)) { exit 3 }
              Stop-LegacyWatchdogIfPresent
              if (Start-Nori) { exit 0 } else { exit 1 } }
  'stop'    { if (-not (Enter-Lock)) { exit 3 }
              Stop-LegacyWatchdogIfPresent; Stop-One $pidFile 'server' }
  'restart' { if (-not (Enter-Lock)) { exit 3 }
              Stop-LegacyWatchdogIfPresent; Stop-One $pidFile 'server'; Start-Sleep 1
              if (Start-Nori) { exit 0 } else { exit 1 } }
  'status'  {
    $s = Get-PidFrom $pidFile; $h = Test-Healthy
    Write-Host ("server: " + $(if ($s) { "running (pid $s)" } else { "DOWN" }) + "  listening=$(Test-Listening)  healthz=$h")
    Write-Host "url:    http://127.0.0.1:$port"
  }
  'ensure'  {
    if ((Get-PidFrom $pidFile) -and (Test-Healthy)) { exit 0 }
    # Not healthy: take the lock, then look AGAIN -- whoever held it may
    # have just fixed it (StartAll racing the supervisor at boot).
    if (-not (Enter-Lock)) { exit 3 }
    $before = @{ tracked_pid = (Get-PidFrom $pidFile); listening = (Test-Listening); healthy = (Test-Healthy) }
    if ($before.tracked_pid -and $before.healthy) {
      Write-SupEvent 'ensure' $true 'became healthy while waiting for the lock -- another caller had just started it' @{ before = $before }
      exit 0
    }
    $t = (Get-Date -Format s)
    # Decision log, one line per ensure call -- named .watchdog.log for
    # historical reasons (an in-process watchdog used to write it; see
    # this script's own top comment for why that was retired in favor of
    # a single external supervisor). The crash-loop guard below reads
    # its own recent history from this same file regardless of what
    # calls `ensure` now.
    $wl = Join-Path $here '.watchdog.log'
    # crash-loop guard: >=5 of the last 6 log lines are restarts -> back off
    $tail = @(); if (Test-Path $wl) { $tail = @(Get-Content $wl -Tail 6) }
    if ((@($tail | Where-Object { $_ -match 'ensure: restarting' })).Count -ge 5) {
      "$t  ensure: CRASHLOOP - backing off, not restarting" | Add-Content $wl
      Write-SupEvent 'ensure' $false 'CRASHLOOP backoff -- 5 of the last 6 decisions were restarts, not restarting again' @{ before = $before }
      exit 2
    }
    "$t  ensure: restarting (pid=$($before.tracked_pid) healthy=$($before.healthy))" | Add-Content $wl
    Stop-One $pidFile 'server' | Out-Null
    Start-Sleep 1
    $started = Start-Nori
    # Verdict from the same test everywhere: serving, not "a process exists".
    # Used to end here WITHOUT setting an exit code, so a run that logged
    # "STILL DOWN after restart" still exited 0 -- and the supervisor,
    # which trusts that exit code, recorded "ok" for an app that wasn't up.
    $up = $started -and (Test-Healthy)
    "$t  ensure: " + $(if ($up) { 'back up' } else { 'STILL DOWN after restart' }) | Add-Content $wl
    Write-SupEvent 'ensure' $up $(if ($up) { 'was not serving; restarted and confirmed serving' } else { 'was not serving; restart did NOT bring it up -- see the start event just before this one for why' }) @{ before = $before }
    if ($up) { exit 0 } else { exit 1 }
  }
}
