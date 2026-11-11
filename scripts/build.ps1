param(
    [string]$Python = "python",
    [string]$Output = "",
    [switch]$SkipSmokeTest
)
$ErrorActionPreference = "Stop"
$arguments = @((Join-Path $PSScriptRoot "build_release.py"))
if ($Output) { $arguments += @("--output", $Output) }
if ($SkipSmokeTest) { $arguments += "--skip-smoke-test" }
& $Python @arguments
exit $LASTEXITCODE
