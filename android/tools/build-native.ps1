[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$ToolCache,
    [Parameter(Mandatory)][string]$Ndk,
    [Parameter(Mandatory)][string]$VsDevShell
)
$ErrorActionPreference = 'Stop'
$android = Split-Path -Parent $PSScriptRoot
$cache = (Resolve-Path -LiteralPath $ToolCache).Path
$bin = Join-Path (Resolve-Path -LiteralPath $Ndk).Path 'toolchains/llvm/prebuilt/windows-x86_64/bin'
$env:CARGO_HOME = Join-Path $cache 'cargo'
$env:RUSTUP_HOME = Join-Path $cache 'rustup'
$env:CARGO_TARGET_DIR = Join-Path $cache 'cargo-build'
& $VsDevShell -Arch amd64 -HostArch amd64 -SkipAutomaticLocation | Out-Null
foreach ($entry in @(@('x86_64-linux-android','x86_64'), @('aarch64-linux-android','arm64-v8a'))) {
    $target, $abi = $entry
    $lower = $target.Replace('-', '_')
    $upper = $lower.ToUpperInvariant()
    [Environment]::SetEnvironmentVariable("CARGO_TARGET_$($upper)_LINKER", "$bin/clang.exe", 'Process')
    [Environment]::SetEnvironmentVariable("CARGO_TARGET_$($upper)_RUSTFLAGS",
        "-C link-arg=--target=$($target)31 -C link-arg=-Wl,-z,max-page-size=16384", 'Process')
    [Environment]::SetEnvironmentVariable("CC_$lower", "$bin/clang.exe", 'Process')
    [Environment]::SetEnvironmentVariable("AR_$lower", "$bin/llvm-ar.exe", 'Process')
    [Environment]::SetEnvironmentVariable("CFLAGS_$lower", "--target=$($target)31", 'Process')
    $cargoArgs = @('build','--offline','--locked','--release','--target',$target,
        '--manifest-path',"$android/native/Cargo.toml")
    & "$env:CARGO_HOME/bin/cargo.exe" @cargoArgs
    if ($LASTEXITCODE -ne 0) { throw "Cannot compile the tokenizer for $abi." }
    $out = Join-Path $cache "prepared/jniLibs/$abi"
    New-Item -ItemType Directory -Path $out -Force | Out-Null
    Copy-Item -LiteralPath "$env:CARGO_TARGET_DIR/$target/release/libpcar_tokenizers.so" -Destination $out
}

$notices = Join-Path $cache 'prepared/assets/licenses'
New-Item -ItemType Directory -Path $notices -Force | Out-Null
foreach ($name in @('tokenizers-0.23.2', 'jni-0.21.1', 'onig-6.5.3', 'onig_sys-69.9.3')) {
    $crate = Get-ChildItem -LiteralPath "$env:CARGO_HOME/registry/src" -Directory |
        ForEach-Object { Join-Path $_.FullName $name } |
        Where-Object { Test-Path -LiteralPath $_ -PathType Container } |
        Select-Object -First 1
    if (-not $crate) { throw "Cargo source is missing for $name." }
    Get-ChildItem -LiteralPath $crate -File | Where-Object Name -Match '^(LICENSE|COPYING|NOTICE)' |
        ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $notices "$name-$($_.Name)") }
}
