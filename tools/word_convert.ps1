param([Parameter(Mandatory=$true)][string]$Source, [Parameter(Mandatory=$true)][string]$Destination)
$ErrorActionPreference = 'Stop'
$taskApplication = $null
$taskDocument = $null
try {
    $taskApplication = New-Object -ComObject Word.Application
    $taskApplication.Visible = $false
    $taskApplication.DisplayAlerts = 0
    $taskApplication.AutomationSecurity = 3
    $taskApplication.Options.UpdateLinksAtOpen = $false
    $taskDocument = $taskApplication.Documents.Open($Source, $false, $true, $false)
    $taskDocument.ExportAsFixedFormat($Destination, 17)
} finally {
    if ($null -ne $taskDocument) {
        $taskDocument.Close(0)
        [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($taskDocument)
    }
    if ($null -ne $taskApplication) {
        $taskApplication.Quit()
        [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($taskApplication)
    }
}
