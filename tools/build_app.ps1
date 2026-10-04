$ErrorActionPreference = 'Stop'
$projectDir = Split-Path $PSScriptRoot -Parent
$buildPython = Join-Path $projectDir '.build-env\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $buildPython)) {
    throw 'Build environment missing: create .build-env and install tools/build-requirements.txt first.'
}
Push-Location $projectDir
try {
    & $buildPython -m PyInstaller --noconfirm --distpath artifacts/package --workpath artifacts/pyinstaller ExplorerDict.spec
    if ($LASTEXITCODE -ne 0) { throw 'Application build failed.' }
    Write-Output 'Built artifacts/package/探索词典/探索词典.exe. Keep its _runtime directory alongside it.'
} finally {
    Pop-Location
}
