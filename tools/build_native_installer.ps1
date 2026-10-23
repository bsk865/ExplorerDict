param(
    [string]$Compiler = 'artifacts/tooling/inno/ISCC.exe',
    [string]$PackageDirectory = 'artifacts/native-build/dist/探索词典-原生版',
    [string]$OutputDirectory = 'artifacts/native-installer',
    [string]$Version = '1.0.0'
)
$ErrorActionPreference = 'Stop'
$projectDirectory = Split-Path $PSScriptRoot -Parent
Push-Location $projectDirectory
try {
    $compilerPath = (Resolve-Path -LiteralPath $Compiler).Path
    $packagePath = (Resolve-Path -LiteralPath $PackageDirectory).Path
    $outputPath = [IO.Path]::GetFullPath((Join-Path $projectDirectory $OutputDirectory))
    if ($Version -notmatch '^\d+\.\d+\.\d+(\.\d+)?$') { throw 'Use a numeric release version.' }
    & .build-env/Scripts/python.exe tools/audit_native_package.py $packagePath --report (Join-Path $outputPath 'package-audit.json')
    if ($LASTEXITCODE -ne 0) { throw 'Package audit failed; installer was not created.' }
    & $compilerPath /Qp "/DPackageDir=$packagePath" "/DOutputDir=$outputPath" "/DAppVersion=$Version" tools/native_setup.iss
    if ($LASTEXITCODE -ne 0) { throw 'Installer compilation failed.' }
} finally {
    Pop-Location
}
