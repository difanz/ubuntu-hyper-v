# Exercise the interactive wizard with queued answers, then inspect the actual ISO.
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot/../scripts/New-CloudInitSeed.ps1"
$testDir = Join-Path ([IO.Path]::GetTempPath()) ('seed-test-' + [Guid]::NewGuid())
$null = [IO.Directory]::CreateDirectory($testDir)
try {
    $keyPath = Join-Path $testDir 'test key.pub'
    $iso = Join-Path $testDir 'test seed.iso'
    $blob = [byte[]](@(0, 0, 0, 11) + [Text.Encoding]::ASCII.GetBytes('ssh-ed25519') + @(0, 0, 0, 32) + (1..32))
    $publicKey = 'ssh-ed25519 ' + [Convert]::ToBase64String($blob)
    [IO.File]::WriteAllText($keyPath, "$publicKey test-comment")
    $script:Answers = New-Object 'Collections.Generic.Queue[string]'
    function Read-Host {
        param([string]$Prompt)
        if ($script:Answers.Count -eq 0) { throw "Unexpected prompt: $Prompt" }
        return $script:Answers.Dequeue()
    }
    foreach ($answer in @('INVALID HOST', 'test-vm', 'root', 'tester',
                          '-----BEGIN OPENSSH PRIVATE KEY-----', $keyPath, $iso)) {
        $script:Answers.Enqueue($answer)
    }
    $null = Invoke-SeedWizard
    if ($script:Answers.Count -ne 0) { throw 'Wizard did not consume expected answers.' }
    & python "$PSScriptRoot/verify_seed_iso.py" $iso 'test-vm' 'tester' $publicKey
    if ($LASTEXITCODE -ne 0) { throw 'ISO validation failed.' }
    $originalHash = (Get-FileHash -LiteralPath $iso).Hash

    # Refusing replacement must leave the existing ISO intact.
    foreach ($answer in @('test-vm', 'tester', $publicKey, $iso, 'n')) { $script:Answers.Enqueue($answer) }
    $null = Invoke-SeedWizard
    if ((Get-FileHash -LiteralPath $iso).Hash -ne $originalHash) { throw 'Cancelled overwrite changed the ISO.' }

    # Reuse the embedded helper in this session and replace an ISO explicitly.
    foreach ($answer in @('second-vm', '', $publicKey, $iso, 'y')) { $script:Answers.Enqueue($answer) }
    $null = Invoke-SeedWizard
    & python "$PSScriptRoot/verify_seed_iso.py" $iso 'second-vm' 'ubuntu' $publicKey
    if ($LASTEXITCODE -ne 0) { throw 'Replacement ISO validation failed.' }
    if ($script:Answers.Count -ne 0) { throw 'Wizard did not consume expected answers.' }
    Write-Host "Seed ISO smoke tests passed on PowerShell $($PSVersionTable.PSVersion)."
}
finally { Remove-Item -LiteralPath $testDir -Recurse -Force }
