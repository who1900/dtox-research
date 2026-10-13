param([Parameter(Mandatory=$true)][string]$ManifestPath)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$manifest = Get-Content -LiteralPath $ManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
$speech = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $speech.SelectVoice('Microsoft Zira Desktop')
    $speech.Rate = 0
    $speech.Volume = 100
    foreach ($scene in $manifest.scenes) {
        foreach ($sentence in $scene.sentences) {
            $speech.SetOutputToWaveFile($sentence.wav)
            $speech.Speak($sentence.spoken)
            $speech.SetOutputToNull()
        }
        Write-Output ('VOICE ' + $scene.id)
    }
} finally {
    $speech.Dispose()
}
