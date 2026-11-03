param(
    [Parameter(Mandatory = $true)][string]$FramesPath,
    [Parameter(Mandatory = $true)][string]$OutputPath,
    [int]$BudgetSeconds = 55
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
[void][Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
[void][Windows.Storage.Streams.IRandomAccessStreamWithContentType, Windows.Storage.Streams, ContentType = WindowsRuntime]
[void][Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
[void][Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
[void][Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
[void][Windows.Media.Ocr.OcrResult, Windows.Foundation, ContentType = WindowsRuntime]

$asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.IsGenericMethodDefinition -and
    $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
} | Select-Object -First 1

function Wait-WinRtOperation {
    param([object]$Operation, [Type]$ResultType)
    $task = $asTask.MakeGenericMethod($ResultType).Invoke($null, @($Operation))
    if (-not $task.Wait(5000)) {
        throw 'Windows OCR operation exceeded five seconds.'
    }
    return $task.GetAwaiter().GetResult()
}

$language = [Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages |
    Where-Object { $_.LanguageTag -like 'zh*' } | Select-Object -First 1
if ($null -eq $language) {
    throw 'Windows OCR Chinese language resources are unavailable.'
}
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($language)
if ($null -eq $engine) {
    throw 'Windows OCR could not create the Chinese recognizer.'
}
$files = @(Get-ChildItem -LiteralPath $FramesPath -Filter 'frame-*.jpg' -File | Sort-Object Name)
if ($files.Count -eq 0 -or $files.Count -gt 1200) {
    throw 'Windows OCR expects between one and 1200 owned sample frames.'
}
$clock = [System.Diagnostics.Stopwatch]::StartNew()
$frames = [System.Collections.Generic.List[object]]::new()
$status = 'complete'
$processed = 0
foreach ($file in $files) {
    if ($clock.Elapsed.TotalSeconds -ge $BudgetSeconds) {
        $status = 'time-limit'
        break
    }
    $stream = $null
    $bitmap = $null
    try {
        $storage = Wait-WinRtOperation ([Windows.Storage.StorageFile]::GetFileFromPathAsync($file.FullName)) ([Windows.Storage.StorageFile])
        $stream = Wait-WinRtOperation ($storage.OpenReadAsync()) ([Windows.Storage.Streams.IRandomAccessStreamWithContentType])
        $decoder = Wait-WinRtOperation ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $bitmap = Wait-WinRtOperation ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
        $recognized = Wait-WinRtOperation ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
        $lines = @($recognized.Lines | ForEach-Object { $_.Text })
        $frames.Add([ordered]@{ at=$processed * 0.5; lines=$lines })
        $processed += 1
    }
    finally {
        if ($null -ne $bitmap) { $bitmap.Dispose() }
        if ($null -ne $stream) { $stream.Dispose() }
    }
}
[ordered]@{
    status=$status; language=$language.LanguageTag; expected=$files.Count;
    processed=$processed; elapsed_seconds=$clock.Elapsed.TotalSeconds; frames=@($frames.ToArray())
} | ConvertTo-Json -Depth 6 -Compress | Set-Content -LiteralPath $OutputPath -Encoding UTF8
