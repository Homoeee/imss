param([Parameter(Mandatory=$true)][string]$SourceDirectory, [Parameter(Mandatory=$true)][string]$OutputDirectory)
$ErrorActionPreference = 'Stop'
$taskSource = (Resolve-Path -LiteralPath $SourceDirectory).Path
[void](New-Item -ItemType Directory -Path $OutputDirectory -Force)
$taskOutput = (Resolve-Path -LiteralPath $OutputDirectory).Path
$taskConverter = Join-Path $PSScriptRoot 'word_convert.ps1'
foreach ($taskFile in (Get-ChildItem -LiteralPath $taskSource -Filter '*.docx' -File)) {
    if ($taskFile.Name.StartsWith('~$')) { continue }
    $taskDestination = Join-Path $taskOutput ($taskFile.BaseName + '.pdf')
    if (Test-Path -LiteralPath $taskDestination) { continue }
    $taskTemporary = Join-Path $taskOutput ([guid]::NewGuid().ToString('N') + '.pdf')
    Write-Output ('Exporting: ' + $taskFile.Name)
    try {
        # Share the production converter's typed arguments and Word lifecycle.
        . $taskConverter -Source ([string]$taskFile.FullName) -Destination ([string]$taskTemporary)
        Move-Item -LiteralPath $taskTemporary -Destination $taskDestination
    } finally {
        if (Test-Path -LiteralPath $taskTemporary) { Remove-Item -LiteralPath $taskTemporary }
    }
}
