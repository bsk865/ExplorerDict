param([string]$OutputDirectory = '')
$ErrorActionPreference = 'Stop'
$projectDir = Split-Path $PSScriptRoot -Parent
$source = Join-Path $projectDir 'scripts\UiaNative.cs'
$targetDirectory = if ($OutputDirectory) { [IO.Path]::GetFullPath((Join-Path $projectDir $OutputDirectory)) } else { Join-Path $projectDir 'scripts' }
New-Item -ItemType Directory -Path $targetDirectory -Force | Out-Null
$output = Join-Path $targetDirectory 'ExplorerDict.Native.dll'
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $compiler)) {
    $compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe'
}
if (-not (Test-Path -LiteralPath $compiler)) { throw 'Missing .NET Framework C# compiler.' }
& $compiler /nologo /target:library /optimize+ "/out:$output" $source
if ($LASTEXITCODE -ne 0) { throw 'UIA native helper build failed.' }
(Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash.ToLowerInvariant() | Set-Content -LiteralPath (Join-Path $targetDirectory 'source.sha256') -Encoding ascii
Write-Output "Built $output from scripts/UiaNative.cs."
