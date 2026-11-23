[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Sdk,
    [Parameter(Mandatory)][string]$Jdk,
    [Parameter(Mandatory)][string]$Gradle,
    [Parameter(Mandatory)][string]$BuildPython,
    [Parameter(Mandatory)][string]$ToolCache,
    [Parameter(Mandatory)][string]$Output,
    [switch]$RefreshDependencies,
    [switch]$BuildAcceptance
)
$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
$cache = (Resolve-Path -LiteralPath $ToolCache).Path
$env:JAVA_HOME = (Resolve-Path -LiteralPath $Jdk).Path
$env:ANDROID_HOME = (Resolve-Path -LiteralPath $Sdk).Path
$env:ANDROID_SDK_ROOT = $env:ANDROID_HOME
$env:GRADLE_USER_HOME = Join-Path $cache 'gradle'
$env:ANDROID_USER_HOME = Join-Path $cache 'android-user'
$buildRoot = Join-Path $cache 'app-build'
$buildArgs = @('--no-daemon','--console=plain','-p',$project,
    "-PpcarPrepared=$cache/prepared", "-PpcarWheels=$cache/wheels", "-PpcarBuildDirectory=$buildRoot",
    "-PpcarBuildPython=$BuildPython")
if ($RefreshDependencies) {
    $proxy = Get-ItemProperty -LiteralPath 'HKCU:/Software/Microsoft/Windows/CurrentVersion/Internet Settings'
    if ($proxy.ProxyEnable -ne 1 -or $proxy.ProxyServer -notmatch '^([a-zA-Z0-9.-]+):(\d+)$') {
        throw 'A configured Windows manual HTTP proxy is required to resolve Android dependencies.'
    }
    $name = $Matches[1]
    $port = $Matches[2]
    $buildArgs += @("-Dhttp.proxyHost=$name", "-Dhttps.proxyHost=$name",
        "-Dhttp.proxyPort=$port", "-Dhttps.proxyPort=$port")
} else {
    $buildArgs += '--offline'
}
New-Item -ItemType Directory -Path $Output -Force | Out-Null
$tasks = @(':workbench:assembleEmulatorDebug', ':workbench:assembleArm64Debug')
if ($BuildAcceptance) { $tasks += ':workbench:assembleEmulatorDebugAndroidTest' }
& $Gradle @buildArgs @tasks 2>&1 |
    Tee-Object -FilePath (Join-Path $Output 'gradle-build.log')
if ($LASTEXITCODE -ne 0) { throw 'Android workbench APK build failed.' }
$checksums = foreach ($entry in @(@('emulator','x86_64'),@('arm64','arm64-v8a'))) {
    $flavor, $abi = $entry
    $source = Join-Path $buildRoot "workbench/outputs/apk/$flavor/debug/workbench-$flavor-debug.apk"
    $apk = Join-Path $Output "PCAR-Android-Stage3-$abi.apk"
    Copy-Item -LiteralPath $source -Destination $apk
    & "$env:ANDROID_HOME/build-tools/36.1.0/apksigner.bat" verify --verbose $apk |
        Set-Content -LiteralPath (Join-Path $Output "$abi-signature.txt") -Encoding utf8
    if ($LASTEXITCODE -ne 0) { throw "APK signature failed for $abi." }
    $hash = (Get-FileHash -LiteralPath $apk -Algorithm SHA256).Hash.ToLowerInvariant()
    "$hash  $([IO.Path]::GetFileName($apk))"
}
$checksums | Tee-Object -FilePath (Join-Path $Output 'SHA256SUMS.txt')
if ($BuildAcceptance) {
    $acceptance = Join-Path $Output 'acceptance'
    New-Item -ItemType Directory -Path $acceptance -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $buildRoot 'workbench/outputs/apk/androidTest/emulator/debug/workbench-emulator-debug-androidTest.apk') `
        -Destination (Join-Path $acceptance 'PCAR-Android-Stage3-Acceptance-x86_64.apk')
}
