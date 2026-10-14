<#
Stage 1c: OCR the extracted screenshots using the Windows built-in OCR engine
(Windows.Media.Ocr).

Runs entirely on-device against system WinRT assemblies, so nothing is installed
and nothing leaves the machine. That matters twice over here: ThreatLocker blocks
user-space packages, and the corpus is internal support material.

Writes one .txt per image plus 01-ocr/ocr.json for the merge step.

Run:  powershell -ExecutionPolicy Bypass -File pipeline\ocr.ps1
#>

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$imgDir = Join-Path $root '01-ocr\images'
$txtDir = Join-Path $root '01-ocr\text'

Add-Type -AssemblyName System.Runtime.WindowsRuntime

# WinRT async methods return IAsyncOperation<T>; PowerShell 5.1 cannot await them
# directly, so reflect out the generic AsTask overload and block on the Task.
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and
    $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
})[0]

function Await($op, $type) {
    $task = $asTaskGeneric.MakeGenericMethod($type).Invoke($null, @($op))
    $task.Wait(-1) | Out-Null
    $task.Result
}

$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]

$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if (-not $engine) { throw 'No OCR engine available for the user profile languages.' }
Write-Host "OCR engine: $($engine.RecognizerLanguage.LanguageTag)"

$images = Get-ChildItem -Recurse -File -Filter *.png $imgDir | Sort-Object FullName
Write-Host "$($images.Count) images to process`n"

$results = @()
$i = 0
foreach ($img in $images) {
    $i++
    $docId = Split-Path (Split-Path $img.FullName -Parent) -Leaf

    try {
        $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($img.FullName)) ([Windows.Storage.StorageFile])
        $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
        $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $bmp = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])

        # RecognizeAsync is picky about pixel format; Bgra8 is always accepted.
        if ($bmp.BitmapPixelFormat -ne [Windows.Graphics.Imaging.BitmapPixelFormat]::Bgra8) {
            $bmp = [Windows.Graphics.Imaging.SoftwareBitmap]::Convert(
                $bmp, [Windows.Graphics.Imaging.BitmapPixelFormat]::Bgra8)
        }

        $ocr = Await ($engine.RecognizeAsync($bmp)) ([Windows.Media.Ocr.OcrResult])

        # Keep line structure: UI screenshots are labels and menu items, and
        # collapsing them into one paragraph destroys the reading order.
        $lines = @($ocr.Lines | ForEach-Object { $_.Text.Trim() } | Where-Object { $_ })
        $text = $lines -join "`n"
        $words = ($ocr.Lines | ForEach-Object { $_.Words.Count } | Measure-Object -Sum).Sum

        $outDir = Join-Path $txtDir $docId
        if (-not (Test-Path $outDir)) { New-Item -ItemType Directory -Force $outDir | Out-Null }
        $outFile = Join-Path $outDir ($img.BaseName + '.txt')
        Set-Content -Path $outFile -Value $text -Encoding utf8

        $results += [pscustomobject]@{
            doc_id = $docId
            image  = $img.Name
            lines  = $lines.Count
            words  = [int]$words
            chars  = $text.Length
            text   = $text
        }
        Write-Host ("[{0,3}/{1}] {2,-9} {3,-28} lines={4,-4} words={5}" -f `
            $i, $images.Count, $docId, $img.BaseName, $lines.Count, $words)

        $stream.Dispose()
    }
    catch {
        Write-Host ("[{0,3}/{1}] {2,-9} {3,-28} FAILED: {4}" -f `
            $i, $images.Count, $docId, $img.BaseName, $_.Exception.Message)
        $results += [pscustomobject]@{
            doc_id = $docId; image = $img.Name; lines = 0; words = 0
            chars = 0; text = ''; error = $_.Exception.Message
        }
    }
}

$results | ConvertTo-Json -Depth 4 | Out-File (Join-Path $root '01-ocr\ocr.json') -Encoding utf8

$ok = @($results | Where-Object { $_.chars -gt 0 })
$totalChars = ($ok | Measure-Object -Property chars -Sum).Sum
Write-Host "`n--- summary ---"
Write-Host "  images OCR'd with text: $($ok.Count) / $($images.Count)"
Write-Host "  OCR text recovered: $('{0:N0}' -f $totalChars) chars (~$('{0:N0}' -f [int]($totalChars/4)) tokens)"
Write-Host "`nby article:"
$results | Group-Object doc_id | Sort-Object Name | ForEach-Object {
    $c = ($_.Group | Measure-Object -Property chars -Sum).Sum
    Write-Host ("  {0,-9} {1,2} images  {2,6} chars" -f $_.Name, $_.Count, $c)
}
Write-Host "`nwrote $txtDir and 01-ocr\ocr.json"
