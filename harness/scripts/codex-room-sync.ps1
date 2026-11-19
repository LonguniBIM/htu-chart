param(
    [ValidateSet("pass1", "pass2", "all")]
    [string]$Role = "all",
    [string]$Model = "",
    [switch]$DryRun,
    [switch]$VerifyOnly
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$argsList = @("$scriptDir\codex_room_sync.py", "--role", $Role)
if ($Model) { $argsList += @("--model", $Model) }
if ($DryRun) { $argsList += "--dry-run" }
if ($VerifyOnly) { $argsList += "--verify-only" }

& python @argsList
exit $LASTEXITCODE
