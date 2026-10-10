#Requires -Version 5.1
<#
.SYNOPSIS
Interactively create a NoCloud cidata ISO for an Ubuntu Hyper-V VM.
.DESCRIPTION
Uses Windows IMAPI2 and an embedded .NET stream copier. No downloads, WSL,
optical drive, external modules, or administrator rights are needed.
Prompts for hostname, username, SSH public key (file or pasted key), and output.
Creates one ISO containing UTF-8 user-data and meta-data without a BOM.
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Read-SeedValue {
    param([string]$Label, [string]$Default, [scriptblock]$Validate)
    while ($true) {
        $prompt = $Label
        if ($Default) { $prompt += " [$Default]" }
        $value = (Read-Host $prompt).Trim()
        if (-not $value) { $value = $Default }
        try {
            if (-not $value) { throw 'Please enter a value.' }
            & $Validate $value
            return $value
        }
        catch { Write-Host $_.Exception.Message -ForegroundColor Yellow }
    }
}

function Get-SeedPublicKey {
    param([string]$Value)
    $candidate = $Value.Trim().Trim('"')
    if ($candidate -match 'PRIVATE KEY') { throw 'Use the public (.pub) key, never the private key.' }
    if ($candidate -notmatch '^(ssh-|ecdsa-|sk-)\S+\s') {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            $candidate = [IO.File]::ReadAllText((Resolve-Path -LiteralPath $candidate).ProviderPath).Trim()
        }
    }
    if ($candidate -match 'PRIVATE KEY') { throw 'Use the public (.pub) key, never the private key.' }
    if ($candidate -match '[\r\n]') { throw 'Supply one SSH public key, on a single line.' }
    $parts = $candidate -split '\s+', 3
    if ($parts.Count -lt 2 -or $parts[0] -notmatch '^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521)|sk-ssh-ed25519@openssh.com|sk-ecdsa-sha2-nistp256@openssh.com)$') {
        throw 'Enter an OpenSSH public key or the path to a .pub file.'
    }
    try { $bytes = [Convert]::FromBase64String($parts[1]) }
    catch { throw 'The SSH key has invalid Base64 data.' }
    if ($bytes.Length -lt 4) { throw 'The SSH public key is truncated.' }
    $typeLength = ([int]$bytes[0] * 16777216) + ([int]$bytes[1] * 65536) + ([int]$bytes[2] * 256) + $bytes[3]
    if ($typeLength -ne $parts[0].Length -or $bytes.Length -le (4 + $typeLength) -or
        [Text.Encoding]::ASCII.GetString($bytes, 4, $typeLength) -cne $parts[0]) {
        throw 'The SSH key type does not match its encoded data.'
    }
    # Comments are not needed for authentication.
    return "$($parts[0]) $($parts[1])"
}

function New-SeedIso {
    param(
        [string]$Hostname, [string]$Username, [string]$PublicKey,
        [string]$OutputPath, [switch]$Overwrite
    )
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
        throw 'Run this script on Windows, in Windows PowerShell 5.1 or PowerShell 7.'
    }
    $outputFile = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputPath)
    if ([IO.Path]::GetExtension($outputFile) -ine '.iso') { throw 'Output must have an .iso extension.' }
    if (Test-Path -LiteralPath $outputFile) {
        if (-not $Overwrite -or -not (Test-Path -LiteralPath $outputFile -PathType Leaf)) {
            throw "Output already exists: $outputFile"
        }
    }
    $parent = [IO.Path]::GetDirectoryName($outputFile)
    if (-not [IO.Directory]::Exists($parent)) { throw "Output directory does not exist: $parent" }
    $workDir = Join-Path ([IO.Path]::GetTempPath()) ('cloud-init-' + [Guid]::NewGuid())
    $partial = Join-Path $parent ('.seed-' + [Guid]::NewGuid() + '.tmp')
    $fsi = $root = $result = $stream = $null
    try {
        $null = [IO.Directory]::CreateDirectory($workDir)
        # JSON is valid YAML; serialization safely quotes all prompted values.
        $config = [ordered]@{
            users = @([ordered]@{
                name = $Username
                groups = @('adm', 'sudo')
                shell = '/bin/bash'
                sudo = 'ALL=(ALL) NOPASSWD:ALL'
                lock_passwd = $true
                ssh_authorized_keys = @($PublicKey)
            })
            disable_root = $true
            ssh_pwauth = $false
        }
        $instanceId = 'iid-' + [Guid]::NewGuid()
        $metadata = [ordered]@{ 'instance-id' = $instanceId; 'local-hostname' = $Hostname }
        $utf8 = New-Object Text.UTF8Encoding($false)
        $userData = "#cloud-config`n" + ($config | ConvertTo-Json -Depth 6) + "`n"
        $metaData = ($metadata | ConvertTo-Json) + "`n"
        [IO.File]::WriteAllText((Join-Path $workDir 'user-data'), $userData.Replace("`r`n", "`n"), $utf8)
        [IO.File]::WriteAllText((Join-Path $workDir 'meta-data'), $metaData.Replace("`r`n", "`n"), $utf8)

        if (-not ('CloudInitSeed.IsoStream' -as [type])) {
            Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;

namespace CloudInitSeed {
    public static class IsoStream {
        public static void Save(object source, string path, long expectedLength) {
            IStream stream = (IStream)source;
            IntPtr count = Marshal.AllocHGlobal(sizeof(int));
            try {
                stream.Seek(0, 0, IntPtr.Zero);
                using (FileStream output = new FileStream(path, FileMode.CreateNew, FileAccess.Write)) {
                    byte[] buffer = new byte[65536];
                    while (true) {
                        Marshal.WriteInt32(count, 0);
                        stream.Read(buffer, buffer.Length, count);
                        int read = Marshal.ReadInt32(count);
                        if (read == 0) break;
                        output.Write(buffer, 0, read);
                    }
                    if (output.Length != expectedLength)
                        throw new IOException("ISO stream length does not match IMAPI result size.");
                }
            } finally { Marshal.FreeHGlobal(count); }
        }
    }
}
'@
        }
        try { $fsi = New-Object -ComObject IMAPI2FS.MsftFileSystemImage }
        catch { throw 'Windows IMAPI2 is unavailable on this system. Use a Windows installation with IMAPI2 support.' }
        $fsi.ChooseImageDefaultsForMediaType(12) # IMAPI_MEDIA_TYPE_DISK; no physical drive required.
        $fsi.FileSystemsToCreate = 3 # ISO9660 + Joliet (preserves user-data/meta-data names).
        $fsi.VolumeName = 'cidata'
        $root = $fsi.Root
        $root.AddTree($workDir, $false)
        $result = $fsi.CreateResultImage()
        $stream = $result.ImageStream
        [CloudInitSeed.IsoStream]::Save($stream, $partial, ([long]$result.TotalBlocks * $result.BlockSize))
        if ($Overwrite -and [IO.File]::Exists($outputFile)) {
            [IO.File]::Replace($partial, $outputFile, $null)
        }
        else { [IO.File]::Move($partial, $outputFile) }
        Write-Host "Created: $outputFile" -ForegroundColor Green
        Write-Host "Instance ID: $instanceId"
        Write-Host "Attach this ISO to the Generation 2 VM's DVD drive before first boot."
        Write-Host "Boot from the VHDX, then connect with: ssh $Username@<VM-IP>"
        return Get-Item -LiteralPath $outputFile
    }
    finally {
        foreach ($com in @($stream, $result, $root, $fsi)) {
            if ($null -ne $com -and [Runtime.InteropServices.Marshal]::IsComObject($com)) {
                $null = [Runtime.InteropServices.Marshal]::ReleaseComObject($com)
            }
        }
        if ([IO.File]::Exists($partial)) { [IO.File]::Delete($partial) }
        if ([IO.Directory]::Exists($workDir)) { [IO.Directory]::Delete($workDir, $true) }
    }
}

function Invoke-SeedWizard {
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
        throw 'Run this script on Windows, in Windows PowerShell 5.1 or PowerShell 7.'
    }
    Write-Host 'Ubuntu Hyper-V cloud-init seed ISO' -ForegroundColor Cyan
    Write-Host 'Press Enter to accept a default. Ctrl+C cancels.'
    Write-Host 'The selected user gets SSH key login and passwordless sudo; password login is disabled.'
    $hostname = Read-SeedValue 'VM hostname' 'ubuntu-resolute' {
        param($v)
        if ($v -cnotmatch '^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$') {
            throw 'Use a lowercase hostname of 1-63 letters, digits or hyphens; no leading/trailing hyphen.'
        }
    }
    $username = Read-SeedValue 'Login username' 'ubuntu' {
        param($v)
        if ($v -eq 'root' -or $v -cnotmatch '^[a-z_][a-z0-9_-]{0,31}$') {
            throw 'Use a non-root Linux username (1-32 lowercase letters, digits, underscores or hyphens).'
        }
    }
    $defaultKey = Join-Path $env:USERPROFILE '.ssh\id_ed25519.pub'
    if (-not (Test-Path -LiteralPath $defaultKey -PathType Leaf)) { $defaultKey = '' }
    $keyInput = Read-SeedValue 'SSH public key (.pub path or paste key)' $defaultKey {
        param($v)
        $null = Get-SeedPublicKey $v
    }
    $publicKey = Get-SeedPublicKey $keyInput
    $outputPath = Read-SeedValue 'Output ISO path' (Join-Path (Get-Location).Path "$hostname-seed.iso") {
        param($v)
        $path = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($v.Trim('"'))
        if ([IO.Path]::GetExtension($path) -ine '.iso') { throw 'Use an .iso filename.' }
        if (-not [IO.Directory]::Exists([IO.Path]::GetDirectoryName($path))) {
            throw 'The output directory must already exist.'
        }
        if ([IO.Directory]::Exists($path)) { throw 'Output is a directory; choose an ISO filename.' }
    }
    $outputPath = $outputPath.Trim('"')
    $overwrite = $false
    if (Test-Path -LiteralPath $outputPath) {
        $answer = Read-Host 'That ISO already exists. Replace it? [y/N]'
        if ($answer -notmatch '^(y|yes)$') { Write-Host 'Cancelled.'; return }
        $overwrite = $true
    }
    New-SeedIso -Hostname $hostname -Username $username -PublicKey $publicKey -OutputPath $outputPath -Overwrite:$overwrite
}

if ($MyInvocation.InvocationName -ne '.') { Invoke-SeedWizard }
