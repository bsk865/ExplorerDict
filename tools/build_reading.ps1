$ErrorActionPreference = 'Stop'
$readingRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $readingRoot
& '.build-env\Scripts\python.exe' -m PyInstaller --clean --noconfirm --distpath artifacts/reading-package --workpath artifacts/reading-build ReadingWorkspace.spec
if ($LASTEXITCODE -ne 0) { throw 'Reading workspace build failed' }

# The native capture companion resolves scripts under its own _runtime.
$companionScripts = Join-Path $readingRoot "artifacts/reading-package/探索词典-初版1/_runtime/scripts"
New-Item -ItemType Directory -Path $companionScripts -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $readingRoot "scripts/uia_helper.ps1") -Destination (Join-Path $companionScripts "uia_helper.ps1") -Force
