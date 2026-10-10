"""Resolve signed Ubuntu sources and convert them on GitHub-hosted Linux runners."""

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import urllib.error
import urllib.request

BASE = "https://cloud-images.ubuntu.com/daily"
STREAM = f"{BASE}/streams/v1/com.ubuntu.cloud:daily:download.json"
IMAGE = "resolute-server-cloudimg-amd64.img"
KEYRING = "/usr/share/keyrings/ubuntu-cloudimage-keyring.gpg"
WORK = Path("work")
DIST = Path("dist")


def run(*args):
    subprocess.run([str(arg) for arg in args], check=True)


def download(url, path):
    # curl retries transport errors; a failed attempt never becomes a final file.
    temporary = path.with_suffix(path.suffix + ".part")
    run("curl", "--fail", "--location", "--silent", "--show-error",
        "--proto", "=https", "--proto-redir", "=https", "--retry", "5",
        "--connect-timeout", "30", "--max-time", "1800",
        "--output", temporary, url)
    temporary.replace(path)


def resolve_source(feed):
    products = [p for p in feed["products"].values()
                if p.get("release") == "resolute" and p.get("arch") == "amd64"]
    if len(products) != 1:
        raise ValueError("Expected exactly one Resolute amd64 product")
    versions = products[0]["versions"]
    for serial in versions:
        if not re.fullmatch(r"\d{8}(?:\.\d+)?", serial):
            raise ValueError(f"Invalid source serial: {serial}")
        dt.datetime.strptime(serial[:8], "%Y%m%d")
    serial = max(versions, key=lambda value: (value[:8], int(value.split(".")[1]) if "." in value else 0))
    item = versions[serial]["items"]["disk1.img"]
    if item["path"] != f"server/resolute/{serial}/{IMAGE}":
        raise ValueError("Unexpected source disk path")
    if not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]):
        raise ValueError("Invalid source checksum")
    if not isinstance(item["size"], int) or item["size"] <= 0:
        raise ValueError("Invalid source size")
    return {"source_serial": serial,
            "source_date": dt.datetime.strptime(serial[:8], "%Y%m%d").date().isoformat(),
            "source_url": f"{BASE}/{item['path']}", "source_sha256": item["sha256"],
            "source_size_bytes": item["size"], "source_stream": STREAM,
            "architecture": "amd64", "release_tag": release_tag(item["sha256"])}


def image_checksum(manifest):
    matches = re.findall(
        rf"^([0-9a-f]{{64}}) [ *]{re.escape(IMAGE)}$", manifest, re.MULTILINE
    )
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one signed checksum for {IMAGE}")
    return matches[0]


def release_tag(checksum):
    # Content identity, independent of source date and workflow commit.
    return f"resolute-amd64-{checksum}"


def github_release(tag):
    repository = os.environ["GITHUB_REPOSITORY"]
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    request = urllib.request.Request(
        f"{api}/repos/{repository}/releases/tags/{tag}",
        headers={"Authorization": f"Bearer {os.environ['GH_TOKEN']}",
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        # Auth, rate-limit and server errors must not look like a new image.
        raise


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def prepare():
    WORK.mkdir(exist_ok=True)
    download(STREAM, WORK / "stream.json")
    download(STREAM + ".gpg", WORK / "stream.json.gpg")
    run("gpgv", "--keyring", KEYRING, WORK / "stream.json.gpg", WORK / "stream.json")
    metadata = resolve_source(json.loads((WORK / "stream.json").read_text()))
    serial = metadata["source_serial"]
    source_date = metadata["source_date"]
    source_url = metadata["source_url"]
    for name in ("SHA256SUMS", "SHA256SUMS.gpg"):
        download(source_url.rsplit("/", 1)[0] + "/" + name, WORK / name)
    run("gpgv", "--keyring", KEYRING, WORK / "SHA256SUMS.gpg", WORK / "SHA256SUMS")
    checksum = image_checksum((WORK / "SHA256SUMS").read_text())
    if checksum != metadata["source_sha256"]:
        raise ValueError("SimpleStreams and signed manifest checksums disagree")
    tag = metadata["release_tag"]
    release = github_release(tag)
    skip = release is not None and not release["draft"]
    write_json(WORK / "source.json", metadata)
    outputs = {"skip": str(skip).lower(), "tag": tag,
               "source_date": source_date,
               "draft_exists": str(release is not None and release["draft"]).lower()}
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
            stream.writelines(f"{key}={value}\n" for key, value in outputs.items())
    status = "Already published; skipping image download and conversion." if skip else "New image or unfinished draft; building."
    summary = f"Source date: {source_date} (serial {serial})\n\nSource: {source_url}\n\nSHA256: `{checksum}`\n\n{status}\n"
    print(summary)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as stream:
            stream.write(summary)


def convert(source, target):
    # Explicit formats prevent probing and preserve the source's virtual size.
    run("qemu-img", "check", "-f", "qcow2", source)
    run("qemu-img", "convert", "-p", "-f", "qcow2", "-O", "vhdx",
        "-o", "subformat=dynamic,block_size=1048576", source, target)
    run("qemu-img", "compare", "-f", "qcow2", "-F", "vhdx", source, target)
    info = json.loads(subprocess.check_output(
        ["qemu-img", "info", "--output=json", "-f", "vhdx", str(target)]))
    original = json.loads(subprocess.check_output(
        ["qemu-img", "info", "--output=json", "-f", "qcow2", str(source)]))
    if info["virtual-size"] != original["virtual-size"]:
        raise ValueError("Conversion changed the disk's virtual size")
    return info


def build():
    metadata = json.loads((WORK / "source.json").read_text())
    source = WORK / IMAGE
    download(metadata["source_url"], source)
    if source.stat().st_size != metadata["source_size_bytes"]:
        raise ValueError("Downloaded image size does not match SimpleStreams")
    if digest(source) != metadata["source_sha256"]:
        raise ValueError("Downloaded image does not match Ubuntu's signed checksum")
    DIST.mkdir(exist_ok=True)
    filename = f"ubuntu-resolute-{metadata['source_serial']}-amd64-hyperv.vhdx"
    target = DIST / filename
    info = convert(source, target)
    metadata.update({"output_format": "vhdx", "subformat": "dynamic",
                     "hyper_v_generation": 2, "virtual_size_bytes": info["virtual-size"],
                     "vhdx_filename": filename, "vhdx_sha256": digest(target),
                     "built_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                     "workflow_commit": os.environ.get("GITHUB_SHA"),
                     "qemu_version": subprocess.check_output(
                         ["qemu-img", "--version"], text=True).splitlines()[0]})
    # Compression keeps sparse disk payloads below GitHub's per-asset size limit.
    run("xz", "-T2", "-3", target)
    archive = DIST / (filename + ".xz")
    run("xz", "--test", archive)
    if archive.stat().st_size >= 2 * 1024**3:
        raise ValueError("Compressed disk exceeds GitHub's 2 GiB release asset limit")
    metadata.update({"archive_filename": archive.name, "archive_sha256": digest(archive)})
    for name in ("SHA256SUMS", "SHA256SUMS.gpg"):
        shutil.copyfile(WORK / name, DIST / ("upstream-" + name))
    write_json(DIST / "build.json", metadata)
    assets = [archive, DIST / "build.json", DIST / "upstream-SHA256SUMS",
              DIST / "upstream-SHA256SUMS.gpg"]
    (DIST / "SHA256SUMS").write_text("".join(
        f"{digest(path)}  {path.name}\n" for path in assets))
    notes = f"""Ubuntu Resolute amd64 cloud image converted to dynamic VHDX for Hyper-V Generation 2.

- **Upstream source date:** {metadata['source_date']} (serial `{metadata['source_serial']}`)
- **Source image:** {metadata['source_url']}
- **Source SHA256:** `{metadata['source_sha256']}`
- **Build commit:** `{metadata['workflow_commit']}`
- **Virtual disk size:** {info['virtual-size']} bytes (unchanged from upstream)

Download `{archive.name}` and extract it with 7-Zip or `xz -d`.
`SHA256SUMS` verifies the downloads; `build.json` also includes the extracted VHDX checksum.
The upstream checksum manifest and signature are attached for provenance.

Create a **Generation 2** VM and attach the extracted VHDX to its SCSI controller.
For Secure Boot, select **Microsoft UEFI Certificate Authority**.
Supply a NoCloud `cidata` seed ISO with your SSH public key; there is no default login password.
See the repository README for first-boot setup and disk expansion.

Validation: Ubuntu signature and image SHA256 verified; converted guest sectors compared
with the source; virtual disk size preserved; XZ integrity checked.
This workflow does not boot-test the image under Hyper-V.
"""
    (DIST / "release-notes.md").write_text(notes)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "build"))
    args = parser.parse_args()
    {"prepare": prepare, "build": build}[args.command]()
