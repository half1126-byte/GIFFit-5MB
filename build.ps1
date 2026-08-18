$ErrorActionPreference = 'Stop'

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $ProjectRoot '.venv312\Scripts\python.exe'
$DistRoot = Join-Path $ProjectRoot 'dist'
$BuildRoot = Join-Path $ProjectRoot 'build'
$AppDist = Join-Path $DistRoot 'GIFFit_5MB'
$ProjectLicense = Join-Path $ProjectRoot 'LICENSE'
if (-not (Test-Path -LiteralPath $ProjectLicense -PathType Leaf)) {
    $ProjectLicense = Join-Path $ProjectRoot 'LICENSE.txt'
}

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Python build environment not found: $Python"
}

$required = @(
    (Join-Path $ProjectRoot 'app.py'),
    (Join-Path $ProjectRoot 'converter.py'),
    (Join-Path $ProjectRoot 'tools\ffmpeg.exe'),
    (Join-Path $ProjectRoot 'tools\ffprobe.exe'),
    (Join-Path $ProjectRoot 'tools\gifsicle.exe')
)
foreach ($path in $required) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Required build file missing: $path"
    }
}

& $Python -m PyInstaller `
    --noconfirm `
    --clean `
    --windowed `
    --onedir `
    --name 'GIFFit_5MB' `
    --version-file (Join-Path $ProjectRoot 'version_info.txt') `
    --distpath $DistRoot `
    --workpath $BuildRoot `
    --specpath $ProjectRoot `
    --add-binary "$(Join-Path $ProjectRoot 'tools\ffmpeg.exe');tools" `
    --add-binary "$(Join-Path $ProjectRoot 'tools\ffprobe.exe');tools" `
    --add-binary "$(Join-Path $ProjectRoot 'tools\gifsicle.exe');tools" `
    (Join-Path $ProjectRoot 'app.py')

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}

$LicenseDist = Join-Path $AppDist 'licenses'
New-Item -ItemType Directory -Force -Path $LicenseDist | Out-Null
Copy-Item -LiteralPath (Join-Path $ProjectRoot 'README_KO.md') -Destination $AppDist
Copy-Item -LiteralPath (Join-Path $ProjectRoot 'THIRD_PARTY_NOTICES.txt') -Destination $AppDist
Copy-Item -LiteralPath $ProjectLicense -Destination (Join-Path $AppDist 'LICENSE.txt')
Copy-Item -Path (Join-Path $ProjectRoot 'licenses\*') -Destination $LicenseDist

$PythonBase = & $Python -c 'import sys; print(sys.base_prefix)'
$runtimeLicenses = @(
    @{ Source = (Join-Path $PythonBase 'LICENSE.txt'); Name = 'Python-3.12-License.txt' },
    @{ Source = (Join-Path $PythonBase 'tcl\tk8.6\license.terms'); Name = 'Tcl-Tk-8.6-License.txt' }
)
foreach ($license in $runtimeLicenses) {
    if (Test-Path -LiteralPath $license.Source -PathType Leaf) {
        Copy-Item -LiteralPath $license.Source -Destination (Join-Path $LicenseDist $license.Name)
    }
}

Write-Output "Build complete. Distribute the entire folder: $AppDist"
