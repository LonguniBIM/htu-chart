param(
    [string]$Commit = "HEAD",
    [string]$Base = "",
    [string]$TaskId = "adhoc",
    [string]$TaskPacket = "",
    [string]$Model = "",
    [switch]$DryRun,
    [switch]$FailOnBlocking
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$argsList = @("$scriptDir\codex_room_review.py", "--commit", $Commit, "--task-id", $TaskId)
if ($Base) { $argsList += @("--base", $Base) }
if ($TaskPacket) { $argsList += @("--task-packet", $TaskPacket) }
if ($Model) { $argsList += @("--model", $Model) }
if ($DryRun) { $argsList += "--dry-run" }
if ($FailOnBlocking) { $argsList += "--fail-on-blocking" }

& python @argsList
exit $LASTEXITCODE
