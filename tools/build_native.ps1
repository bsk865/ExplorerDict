param([string]$OutputDirectory = 'artifacts/native-build')
$ErrorActionPreference = 'Stop'
$projectDirectory = Split-Path $PSScriptRoot -Parent
$outputPath = [IO.Path]::GetFullPath((Join-Path $projectDirectory $OutputDirectory))
$pythonPath = Join-Path $projectDirectory '.build-env/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Missing build environment.' }
Push-Location $projectDirectory
try {
    & $pythonPath -m PyInstaller --noconfirm --distpath (Join-Path $outputPath 'dist') --workpath (Join-Path $outputPath 'work') NativeWorkspace.spec
    if ($LASTEXITCODE -ne 0) { throw 'Native build failed.' }
    $packagePath = Join-Path $outputPath 'dist/探索词典-原生版'
    foreach ($component in @('探索词典.exe', '_runtime')) {
        $sourcePath = Join-Path $projectDirectory ('artifacts/package/探索词典/' + $component)
        if (-not (Test-Path -LiteralPath $sourcePath)) { $sourcePath = Join-Path $projectDirectory $component }
        if (-not (Test-Path -LiteralPath $sourcePath)) { throw "Missing companion: $component" }
        Copy-Item -LiteralPath $sourcePath -Destination $packagePath -Recurse -Force
    }
    & $pythonPath tools/audit_native_package.py $packagePath --report (Join-Path $outputPath 'package-audit.json')
    if ($LASTEXITCODE -ne 0) { throw 'Native package audit failed.' }
    Write-Output $packagePath
} finally {
    Pop-Location
}
