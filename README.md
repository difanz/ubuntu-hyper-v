# Ubuntu Resolute for Hyper-V

GitHub Actions converts Canonical's Ubuntu Resolute **amd64** cloud image into
a dynamically expanding **VHDX for Hyper-V Generation 2 (UEFI)** and publishes it
to [GitHub Releases](https://github.com/difanz/ubuntu-hyper-v/releases).

## Builds and updates

The workflow checks daily at **06:37 UTC**, on pushes to `main`, and via
**Actions → Build Resolute Hyper-V image → Run workflow**. Pull requests run
tests only. No personal token or repository secret is required; the release job
uses `GITHUB_TOKEN` with `contents: write`.

Source discovery uses Canonical's [daily SimpleStreams download API](https://cloud-images.ubuntu.com/daily/streams/v1/com.ubuntu.cloud:daily:download.json).
It selects the newest Resolute `amd64` version and its `disk1.img` item, which
provides a dated download path, source serial, size, and SHA256. These are the
same image builds published under [resolute/](https://cloud-images.ubuntu.com/resolute/).
The [daily locator](https://cloud-images.ubuntu.com/locator/daily/) lists cloud
provider image IDs; the download stream supplies the disk artifacts needed here.

1. Verify the SimpleStreams signature using Ubuntu's packaged cloud-image keyring.
2. Verify the dated `SHA256SUMS` signature and require its image checksum to match
   SimpleStreams.
3. Look up the release tag `resolute-amd64-<full source SHA256>`. If it is already
   published, **skip downloading the disk, conversion, and release creation**.
   Changing the workflow or source date alone does not rebuild identical bytes.
4. Download and verify the QCOW2 size and checksum; convert with `qemu-img`;
   compare guest-visible disk contents and virtual size against the original.
5. Compress and validate the VHDX, upload all assets to a draft, then publish it.
   Interrupted drafts are retried. Runs are serialized to avoid duplicate releases.

Authentication, signature, metadata, and API errors fail the job instead of
being treated as a new upstream image. No image is mounted or modified in CI.

## Release assets

- `ubuntu-resolute-<source serial>-amd64-hyperv.vhdx.xz`: extract with 7-Zip or `xz -d`.
  XZ keeps the disk download below GitHub's 2 GiB per-asset limit.
- `SHA256SUMS`: checksums of the downloadable assets.
- `build.json`: **upstream source date**, serial, source URL and checksum,
  output checksums, virtual size, QEMU version, build time and workflow commit.
- `upstream-SHA256SUMS` and `upstream-SHA256SUMS.gpg`: Canonical's signed checksums.

The release title and notes show the **source date**, independently of when the
workflow ran. On Linux, verify the downloads with `sha256sum -c SHA256SUMS`;
the checksum of the extracted VHDX is in `build.json`.

## First boot on Hyper-V

Create a **Generation 2** VM with at least 2 GiB RAM and attach the extracted
VHDX to its SCSI controller. Connect a virtual switch with DHCP. If Secure Boot
is enabled, select **Microsoft UEFI Certificate Authority** in the VM's Security
settings. Copy the base VHDX before creating each VM.

This is a cloud image: it has **no default login password**. Before booting,
attach a NoCloud seed ISO labeled `cidata` containing `user-data` and `meta-data`.
Use the interactive [New-CloudInitSeed.ps1](scripts/New-CloudInitSeed.ps1) wizard
on Windows. It works with Windows PowerShell 5.1 and PowerShell 7 and uses
Windows' built-in IMAPI2 components. No WSL, ISO utility, module installation,
optical drive, or administrator rights are required to create the ISO.

From a checkout:

```powershell
.\scripts\New-CloudInitSeed.ps1
```

Or download just the self-contained script and run it:

```powershell
Invoke-WebRequest -Uri 'https://raw.githubusercontent.com/difanz/ubuntu-hyper-v/main/scripts/New-CloudInitSeed.ps1' -OutFile .\New-CloudInitSeed.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\New-CloudInitSeed.ps1
```

The wizard prompts for:

1. VM hostname (default `ubuntu-resolute`).
2. Login username (default `ubuntu`).
3. An SSH public key: paste an OpenSSH key or enter a `.pub` file path. If present,
   `%USERPROFILE%\.ssh\id_ed25519.pub` is offered as the default.
4. Output ISO path (default `<hostname>-seed.iso` in the current directory).

It creates **one `cidata` ISO containing both `user-data` and `meta-data`**, with
a new unique instance ID, UTF-8 encoding without a BOM, and Joliet filenames.
The chosen user gets passwordless sudo; password login and root SSH login are
disabled. Existing ISOs are replaced only after an explicit prompt, and temporary
files are cleaned up. Ctrl+C cancels the wizard.

Attach the ISO through **VM Settings → SCSI Controller → DVD Drive → Image file**
before first boot. Boot from the VHDX, then SSH using the chosen username and
the private key matching the public key. The script leaves VM creation and ISO
attachment to you. DHCP is used by the stock cloud image.

Generate a separate seed for each new VM. Changing the seed on an already
initialized VM is not a substitute for editing that VM's configuration.
Keep private keys and passwords out of this repository and its release artifacts.

The virtual disk retains Ubuntu's original capacity. Before first boot, expand
it using Hyper-V's **Edit Disk → Expand** if you need more space; cloud-init
normally grows the root partition and filesystem at first boot.

CI verifies image integrity and conversion equivalence. **It does not boot-test
under Hyper-V**; firmware, networking, Secure Boot and first-boot provisioning
still require validation on your host.

## Development

Tests use Python's standard library; on Ubuntu install `qemu-utils` to include
the real sparse-disk conversion test:

```sh
python3 -m unittest discover -s tests -v
```

Windows CI also runs `tests/seed-smoke.ps1` under both PowerShell 5.1 and 7.
It drives the prompts, checks invalid input and overwrite handling, and parses
the resulting ISO to verify its label, filenames, encoding and cloud-init data.

References: [Ubuntu image artifacts and verification](https://ubuntu.com/docs/public-images/public-images-reference/artifacts/),
[QEMU disk formats](https://www.qemu.org/docs/master/system/images.html),
[Hyper-V VM generations](https://learn.microsoft.com/en-us/windows-server/virtualization/hyper-v/plan/should-i-create-a-generation-1-or-2-virtual-machine-in-hyper-v).
