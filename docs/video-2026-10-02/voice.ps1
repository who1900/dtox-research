param([Parameter(Mandatory=$true)][string]$ManifestPath)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$manifest = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
$speech = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $speech.SelectVoice('Microsoft Zira Desktop')
    $speech.Rate = 1
    $speech.Volume = 100
    foreach ($scene in $manifest.scenes) {
        $speech.SetOutputToWaveFile($scene.wav)
        $speech.Speak($scene.voice)
        $speech.SetOutputToNull()
        Write-Output ('VOICE ' + $scene.id)
    }
} finally {
    $speech.Dispose()
}
