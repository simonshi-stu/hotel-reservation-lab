param(
    [Parameter(Mandatory=$true, Position=0)][string]$Module,
    [Parameter(ValueFromRemainingArguments=$true)][string[]]$ModuleArgs
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$runtimeRoot = Join-Path $projectRoot '.runtime'
New-Item -ItemType Directory -Path (Join-Path $runtimeRoot 'tmp') -Force | Out-Null
$env:TEMP = Join-Path $runtimeRoot 'tmp'
$env:TMP = $env:TEMP
$env:TMPDIR = $env:TEMP
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONPYCACHEPREFIX = Join-Path $runtimeRoot 'pycache'
$env:PIP_CACHE_DIR = Join-Path $runtimeRoot 'pip-cache'
$env:XDG_CACHE_HOME = Join-Path $runtimeRoot 'cache'
$env:PYTHONUTF8 = '1'
$pythonCommand = if ($env:HOTEL_PYTHON) { $env:HOTEL_PYTHON } else { 'C:\Users\Lenovo\AppData\Local\Programs\Python\Python310\python.exe' }
if (-not (Test-Path -LiteralPath $pythonCommand)) { throw 'Set HOTEL_PYTHON to an existing Python 3.10+ interpreter. No installation is performed.' }
& $pythonCommand -B -m $Module @ModuleArgs
exit $LASTEXITCODE

