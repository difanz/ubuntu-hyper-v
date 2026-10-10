"""Read the ISO's Joliet directory directly, without mounting or dependencies."""
import json
from pathlib import Path
import struct
import sys
import uuid


def verify(path, hostname, username, public_key):
    data = Path(path).read_bytes()
    descriptor = None
    for offset in range(16 * 2048, len(data), 2048):
        block = data[offset:offset + 2048]
        assert block[1:6] == b"CD001", "Invalid ISO volume descriptor"
        if block[0] == 2 and block[88:91] in (b"%/@", b"%/C", b"%/E"):
            descriptor = block
        if block[0] == 255:
            break
    assert descriptor is not None, "Joliet is required for the cloud-init filenames"
    assert descriptor[40:72].decode("utf-16-be").strip().lower() == "cidata"
    root = descriptor[156:190]
    extent, length = struct.unpack_from("<I", root, 2)[0], struct.unpack_from("<I", root, 10)[0]
    directory = data[extent * 2048:extent * 2048 + length]
    files = {}
    offset = 0
    while offset < len(directory):
        size = directory[offset]
        if size == 0:
            offset = (offset // 2048 + 1) * 2048
            continue
        record = directory[offset:offset + size]
        offset += size
        name = record[33:33 + record[32]]
        if name in (b"\x00", b"\x01"):
            continue
        name = name.decode("utf-16-be").removesuffix(";1")
        start = struct.unpack_from("<I", record, 2)[0] * 2048
        count = struct.unpack_from("<I", record, 10)[0]
        files[name] = data[start:start + count]
    assert set(files) == {"user-data", "meta-data"}, files.keys()
    for content in files.values():
        assert not content.startswith(b"\xef\xbb\xbf"), "Unexpected UTF-8 BOM"
        assert b"\r" not in content, "Expected LF line endings"
    assert files["user-data"].startswith(b"#cloud-config\n")
    config = json.loads(files["user-data"].split(b"\n", 1)[1])
    user = config["users"][0]
    assert user["name"] == username
    assert user["ssh_authorized_keys"] == [public_key]
    assert user["lock_passwd"] is True
    assert user["sudo"] == "ALL=(ALL) NOPASSWD:ALL"
    assert config["ssh_pwauth"] is False
    assert config["disable_root"] is True
    metadata = json.loads(files["meta-data"])
    assert metadata["local-hostname"] == hostname
    uuid.UUID(metadata["instance-id"].removeprefix("iid-"))
    print(f"Verified cidata label, Joliet filenames, UTF-8 encoding and cloud-init data: {path}")


if __name__ == "__main__":
    verify(*sys.argv[1:])
