[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Adb,
    [Parameter(Mandatory)][ValidatePattern('^emulator-\d+$')][string]$Serial,
    [Parameter(Mandatory)][string]$ExpectedAvd,
    [Parameter(Mandatory)][ValidateSet(4096,16384)][int]$PageSize,
    [Parameter(Mandatory)][string]$Apk,
    [Parameter(Mandatory)][string]$Output
)
$ErrorActionPreference = 'Stop'
$package = 'io.github.foxmoreland.pcar.runtimecheck'
$report = 'files/stage1-report.json'
if (-not (@(& $Adb -s $Serial emu avd name) -contains $ExpectedAvd)) {
    throw 'Stage-one test requires the specified Android Emulator.'
}
if ((& $Adb -s $Serial shell getprop sys.boot_completed) -ne '1' -or
    (& $Adb -s $Serial shell getconf PAGESIZE) -ne "$PageSize") {
    throw 'Emulator boot or page size does not match the test.'
}
New-Item -ItemType Directory -Force -Path $Output | Out-Null
& $Adb -s $Serial shell cmd connectivity airplane-mode enable
if ($LASTEXITCODE -ne 0) { throw 'Cannot enable airplane mode.' }
& $Adb -s $Serial shell svc wifi disable
& $Adb -s $Serial shell svc data disable
$offlineBy = [DateTime]::UtcNow.AddSeconds(25)
do {
    $network = @(& $Adb -s $Serial shell dumpsys connectivity | Select-String 'Active default network')
    if ($network -match 'Active default network:\s*none\b') { break }
    Start-Sleep -Milliseconds 500
} while ([DateTime]::UtcNow -lt $offlineBy)
if (-not ($network -match 'Active default network:\s*none\b')) {
    throw 'Emulator still has a default network.'
}
$network | Set-Content -LiteralPath (Join-Path $Output 'network.txt') -Encoding utf8

& $Adb -s $Serial install -r $Apk
if ($LASTEXITCODE -ne 0) { throw 'APK installation failed.' }
& $Adb -s $Serial shell am force-stop $package
& $Adb -s $Serial shell run-as $package rm -f $report
if ($LASTEXITCODE -ne 0) { throw 'Cannot prepare app report.' }
$launch = @(& $Adb -s $Serial shell am start -W -n "$package/.MainActivity" --ez stage1 true)
$launch | Set-Content -LiteralPath (Join-Path $Output 'launch.txt') -Encoding utf8
if ($LASTEXITCODE -ne 0 -or -not ($launch -match 'Status: ok')) { throw 'Runtime check app did not open.' }
$until = [DateTime]::UtcNow.AddMinutes(5)
do {
    $running = @(& $Adb -s $Serial shell pidof $package)
    if (-not $running) { break }
    $snapshot = @(& $Adb -s $Serial shell run-as $package cat $report 2>$null)
    if ($snapshot -match '"status"\s*:\s*"(ok|failed)"') { break }
    Start-Sleep -Milliseconds 500
} while ([DateTime]::UtcNow -lt $until)
$null = & $Adb -s $Serial shell run-as $package test -s $report
$hasReport = $LASTEXITCODE -eq 0
& $Adb -s $Serial logcat -d -s 'PCARStage1:V' 'AndroidRuntime:E' '*:S' |
    Set-Content -LiteralPath (Join-Path $Output 'logcat.txt') -Encoding utf8
& $Adb -s $Serial shell dumpsys activity exit-info $package |
    Set-Content -LiteralPath (Join-Path $Output 'exit-info.txt') -Encoding utf8
if (-not $hasReport) { throw 'Runtime check app produced no result.' }

$start = [Diagnostics.ProcessStartInfo]::new()
$start.FileName = (Resolve-Path -LiteralPath $Adb).Path
foreach ($argument in @('-s',$Serial,'exec-out','run-as',$package,'cat',$report)) {
    $start.ArgumentList.Add($argument)
}
$start.UseShellExecute = $false
$start.CreateNoWindow = $true
$start.RedirectStandardOutput = $true
$process = [Diagnostics.Process]::Start($start)
$file = Join-Path $Output 'stage1-report.json'
try {
    $fileStream = [IO.File]::Create($file)
    try { $process.StandardOutput.BaseStream.CopyTo($fileStream) } finally { $fileStream.Dispose() }
    $process.WaitForExit()
    if ($process.ExitCode -ne 0) { throw 'Cannot retrieve runtime check report.' }
} finally { $process.Dispose() }
$result = Get-Content -LiteralPath $file -Raw -Encoding utf8 | ConvertFrom-Json
$primaryAbi = (& $Adb -s $Serial shell dumpsys package $package |
    Select-String 'primaryCpuAbi=' | Select-Object -First 1).Line.Trim().Split('=')[-1]
[ordered]@{serial=$Serial; avd=$ExpectedAvd; deviceAbi=(& $Adb -s $Serial shell getprop ro.product.cpu.abi);
    packageAbi=$primaryAbi; runtimePageBytes=$result.page_bytes;
    apkSha256=(Get-FileHash -LiteralPath $Apk -Algorithm SHA256).Hash.ToLowerInvariant()} |
    ConvertTo-Json | Set-Content -LiteralPath (Join-Path $Output 'device.json') -Encoding utf8
if ($result.status -eq 'running') {
    & $Adb -s $Serial logcat -d -b crash |
        Set-Content -LiteralPath (Join-Path $Output 'native-crash.log') -Encoding utf8
    throw "Runtime check did not finish in $($result.stage). See the partial report and process exit record."
}
if ($result.status -ne 'ok') { throw "Runtime check failed in $($result.stage): $($result.error)" }
if ($result.page_bytes -ne $PageSize) { throw 'Embedded runtime saw an unexpected page size.' }
"PASS: CPython $($result.python) on $primaryAbi, $PageSize bytes per page"

