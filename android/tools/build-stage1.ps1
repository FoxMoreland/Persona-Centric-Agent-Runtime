[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Sdk,
    [Parameter(Mandatory)][string]$Jdk,
    [Parameter(Mandatory)][string]$Gradle,
    [Parameter(Mandatory)][string]$BuildPython,
    [Parameter(Mandatory)][string]$ToolCache,
    [Parameter(Mandatory)][string]$Output,
    [switch]$RefreshDependencies
)
$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
$cache = (Resolve-Path -LiteralPath $ToolCache).Path
$prepared = Join-Path $cache 'prepared'
foreach ($required in @('jniLibs/x86_64/libpcar_tokenizers.so','jniLibs/arm64-v8a/libpcar_tokenizers.so',
        'assets/bge/model_optimized.onnx','assets/stage1/flash-reference.json','assets/stage1/loopback.key',
        'onnxruntime-android-1.31.1.aar')) {
    if (-not (Test-Path -LiteralPath (Join-Path $prepared $required) -PathType Leaf)) {
        throw "Missing prepared stage-one file $required."
    }
}

$env:JAVA_HOME = (Resolve-Path -LiteralPath $Jdk).Path
$env:ANDROID_HOME = (Resolve-Path -LiteralPath $Sdk).Path
$env:ANDROID_SDK_ROOT = $env:ANDROID_HOME
$env:GRADLE_USER_HOME = Join-Path $cache 'gradle'
$env:ANDROID_USER_HOME = Join-Path $cache 'android-user'
$buildRoot = Join-Path $cache 'app-build'
$buildArgs = @('--no-daemon','--console=plain','-p',$project,
    "-PpcarPrepared=$prepared", "-PpcarWheels=$cache/wheels", "-PpcarBuildDirectory=$buildRoot",
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
& $Gradle @buildArgs ':app:assembleEmulatorDebug' ':app:assembleArm64Debug' 2>&1 |
    Tee-Object -FilePath (Join-Path $Output 'gradle-build.log')
if ($LASTEXITCODE -ne 0) { throw 'Stage-one APK build failed.' }
foreach ($entry in @(@('emulator','x86_64'),@('arm64','arm64-v8a'))) {
    $flavor, $abi = $entry
    $source = Join-Path $buildRoot "outputs/apk/$flavor/debug/app-$flavor-debug.apk"
    $apk = Join-Path $Output "PCAR-Android-Stage1-$abi.apk"
    Copy-Item -LiteralPath $source -Destination $apk
    & "$env:ANDROID_HOME/build-tools/36.1.0/apksigner.bat" verify --verbose $apk |
        Set-Content -LiteralPath (Join-Path $Output "$abi-signature.txt") -Encoding utf8
    if ($LASTEXITCODE -ne 0) { throw "APK signature failed for $abi." }
    $hash = (Get-FileHash -LiteralPath $apk -Algorithm SHA256).Hash.ToLowerInvariant()
    "$hash  $([IO.Path]::GetFileName($apk))"
}
