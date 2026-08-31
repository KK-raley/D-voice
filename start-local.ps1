param(
    [ValidateSet('listen', 'serve', 'enroll', 'check')]
    [string]$Mode = 'listen',
    [string]$User = 'you',
    [switch]$TextOnly,
    [string]$DeploymentDir = 'D:\qwen-deployment',
    [string]$ModelFile = 'models/qwen3-4b-q4_k_m.gguf',
    [string]$Python = 'python'
)

# Local state stays in this checkout. The model deployment is read-only.
# This script does not install dependencies or download models.
$ErrorActionPreference = 'Stop'
$env:VOCALIS_HOME = Join-Path $PSScriptRoot '.vocalis'
$env:PYTHONIOENCODING = 'utf-8'
$env:HF_HUB_OFFLINE = '1'
$env:TRANSFORMERS_OFFLINE = '1'
$dependencyPaths = @($PSScriptRoot)
foreach ($relativePath in @('.tools\voice-deps', '.tools\pydeps')) {
    $dependencyPath = Join-Path $PSScriptRoot $relativePath
    if (Test-Path -LiteralPath $dependencyPath -PathType Container) {
        $dependencyPaths += $dependencyPath
    }
}
if ($env:PYTHONPATH) {
    $dependencyPaths += $env:PYTHONPATH
}
$env:PYTHONPATH = $dependencyPaths -join [IO.Path]::PathSeparator

Push-Location -LiteralPath $PSScriptRoot
try {
    if ($Mode -eq 'check') {
        # No config creation, startup, speech recording, or inference.
        & $Python -m vocalis.cli local-qwen --check --deployment-dir $DeploymentDir --model-file $ModelFile
        exit $LASTEXITCODE
    }

    $setupArgs = @('-m', 'vocalis.cli', 'local-qwen', '--deployment-dir', $DeploymentDir,
        '--model-file', $ModelFile, '--local-audio')
    if ($Mode -in @('listen', 'serve')) {
        $setupArgs += '--start'
    }
    & $Python @setupArgs
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    switch ($Mode) {
        'enroll' {
            & $Python -m vocalis.cli enroll --user $User
        }
        'serve' {
            if ($TextOnly) {
                Write-Host 'TextOnly applies to listen mode. HUD microphone replies use configured local speech.'
            }
            & $Python -m vocalis.cli serve --host 127.0.0.1 --port 8642
        }
        'listen' {
            $listenArgs = @('-m', 'vocalis.cli', 'listen')
            if ($TextOnly) { $listenArgs += '--text-only' }
            & $Python @listenArgs
        }
    }
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
