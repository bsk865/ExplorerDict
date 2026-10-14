$ErrorActionPreference = 'Stop'
$projectDir = Split-Path $PSScriptRoot -Parent
$source = Join-Path $projectDir 'scripts\UiaNative.cs'
$output = Join-Path $projectDir 'scripts\ExplorerDict.Native.dll'
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $compiler)) {
    $compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe'
}
if (-not (Test-Path -LiteralPath $compiler)) { throw 'Missing .NET Framework C# compiler.' }
& $compiler /nologo /target:library /optimize+ "/out:$output" $source
if ($LASTEXITCODE -ne 0) { throw 'UIA native helper build failed.' }
Write-Output 'Built scripts/ExplorerDict.Native.dll from scripts/UiaNative.cs.'
