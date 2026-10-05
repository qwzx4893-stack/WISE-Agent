# PowerShell Windows.Media.Ocr Extraction Script for WISE
# Native, zero-dependency, local Windows OCR Engine
param(
    [Parameter(Mandatory=$true)][string]$ImagePath
)

$ErrorActionPreference = 'Stop'

try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    Add-Type -AssemblyName System.Drawing

    [Windows.Media.Ocr.OcrEngine, Windows.Foundation.UniversalApiContract, ContentType = WindowsRuntime] | Out-Null
    [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation.UniversalApiContract, ContentType = WindowsRuntime] | Out-Null
    [Windows.Storage.StorageFile, Windows.Foundation.UniversalApiContract, ContentType = WindowsRuntime] | Out-Null

    function Await($asyncOp, $asTaskGeneric, $type) {
        $netTask = $asTaskGeneric.MakeGenericMethod($type).Invoke($null, @($asyncOp))
        $netTask.Wait(-1) | Out-Null
        return $netTask.Result
    }

    $asTaskGeneric = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { 
        $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' 
    }

    $resolvedPath = (Resolve-Path $ImagePath).Path
    $fileOp = [Windows.Storage.StorageFile]::GetFileFromPathAsync($resolvedPath)
    $file = Await $fileOp $asTaskGeneric ([Windows.Storage.StorageFile])

    $streamOp = $file.OpenAsync([Windows.Storage.FileAccessMode]::Read)
    $stream = Await $streamOp $asTaskGeneric ([Windows.Storage.Streams.IRandomAccessStream])

    $decoderOp = [Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)
    $decoder = Await $decoderOp $asTaskGeneric ([Windows.Graphics.Imaging.BitmapDecoder])

    $softBmpOp = $decoder.GetSoftwareBitmapAsync()
    $softBmp = Await $softBmpOp $asTaskGeneric ([Windows.Graphics.Imaging.SoftwareBitmap])

    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
    if (-not $engine) {
        $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new("en-US"))
    }

    if (-not $engine) {
        Write-Output '{"full_text":"","lines":[],"error":"No OCR language pack available"}'
        exit 0
    }

    $ocrOp = $engine.RecognizeAsync($softBmp)
    $ocrResult = Await $ocrOp $asTaskGeneric ([Windows.Media.Ocr.OcrResult])

    $lines = @()
    foreach ($line in $ocrResult.Lines) {
        $words = @()
        $minX = 999999
        $minY = 999999
        $maxX = 0
        $maxY = 0

        foreach ($w in $line.Words) {
            $wx = [int]$w.BoundingRect.X
            $wy = [int]$w.BoundingRect.Y
            $ww = [int]$w.BoundingRect.Width
            $wh = [int]$w.BoundingRect.Height

            if ($wx -lt $minX) { $minX = $wx }
            if ($wy -lt $minY) { $minY = $wy }
            if ($wx + $ww -gt $maxX) { $maxX = $wx + $ww }
            if ($wy + $wh -gt $maxY) { $maxY = $wy + $wh }

            $words += @{
                text = $w.Text
                x = $wx
                y = $wy
                width = $ww
                height = $wh
            }
        }

        $lines += @{
            text = $line.Text
            x = if ($minX -lt 999999) { $minX } else { 0 }
            y = if ($minY -lt 999999) { $minY } else { 0 }
            width = if ($maxX -gt $minX) { $maxX - $minX } else { 0 }
            height = if ($maxY -gt $minY) { $maxY - $minY } else { 0 }
            words = $words
        }
    }

    $output = @{
        full_text = $ocrResult.Text
        lines = $lines
    }

    $output | ConvertTo-Json -Depth 5 -Compress
} catch {
    $errObj = @{
        full_text = ""
        lines = @()
        error = $_.ToString()
    }
    $errObj | ConvertTo-Json -Compress
}
