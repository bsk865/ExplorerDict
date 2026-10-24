$ErrorActionPreference = 'Stop'
$projectDir = Split-Path $PSScriptRoot -Parent
$buildPython = Join-Path $projectDir '.build-env\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $buildPython)) {
    throw 'Build environment missing: create .build-env and install tools/build-requirements.txt first.'
}
Push-Location $projectDir
try {
    & $buildPython tools/companion_manifest.py 'artifacts/package/探索词典' --snapshot artifacts/companion-build-sources.json
    if ($LASTEXITCODE -ne 0) { throw 'Could not record build sources.' }
    & (Join-Path $PSScriptRoot 'build_uia_native.ps1') -OutputDirectory artifacts/uia-native-build
    & $buildPython -m PyInstaller --noconfirm --distpath artifacts/package --workpath artifacts/pyinstaller ExplorerDict.spec
    if ($LASTEXITCODE -ne 0) { throw 'Application build failed.' }
    & $buildPython tools/companion_manifest.py 'artifacts/package/探索词典' --write --source-snapshot artifacts/companion-build-sources.json
    if ($LASTEXITCODE -ne 0) { throw 'Capture companion source manifest failed.' }
    Write-Output 'Built artifacts/package/探索词典/探索词典.exe. Keep its _runtime directory alongside it.'
} finally {
    Pop-Location
}
