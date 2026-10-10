import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from scripts import image


def feed(serial="20260927", checksum="a" * 64):
    return {"products": {"ubuntu": {"release": "resolute", "arch": "amd64",
            "versions": {serial: {"items": {"disk1.img": {
                "path": f"server/resolute/{serial}/{image.IMAGE}",
                "sha256": checksum, "size": 1024}}}}}}}


class SourceTests(unittest.TestCase):
    def test_select_latest_numeric_revision_and_exact_architecture(self):
        data = feed("20260927.9")
        data["products"]["ubuntu"]["versions"].update(
            feed("20260927.10")["products"]["ubuntu"]["versions"])
        data["products"]["v3"] = copy.deepcopy(data["products"]["ubuntu"])
        data["products"]["v3"]["arch"] = "amd64v3"
        source = image.resolve_source(data)
        self.assertEqual(source["source_serial"], "20260927.10")
        self.assertEqual(source["source_date"], "2026-09-27")

    def test_same_bytes_different_date_has_same_release(self):
        self.assertEqual(image.resolve_source(feed())["release_tag"],
                         image.resolve_source(feed("20260928"))["release_tag"])
        self.assertNotEqual(image.resolve_source(feed())["release_tag"],
                            image.resolve_source(feed(checksum="b" * 64))["release_tag"])

    def test_bad_date_and_path_fail(self):
        with self.assertRaises(ValueError):
            image.resolve_source(feed("20260230"))
        data = feed()
        data["products"]["ubuntu"]["versions"]["20260927"]["items"]["disk1.img"]["path"] = "https://example.com/disk"
        with self.assertRaises(ValueError):
            image.resolve_source(data)

    def test_checksum_matches_only_exact_filename(self):
        line = f"{'a' * 64} *{image.IMAGE}\n"
        self.assertEqual(image.image_checksum(line), "a" * 64)
        for invalid in (line + line, line.replace("amd64.img", "amd64v3.img"), ""):
            with self.assertRaises(ValueError):
                image.image_checksum(invalid)

    def test_only_404_means_missing_release(self):
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "owner/repo", "GH_TOKEN": "test"}):
            for status in (404, 403, 401, 500):
                error = urllib.error.HTTPError("https://api.github.com", status, "error", {}, None)
                with patch("urllib.request.urlopen", side_effect=error):
                    if status == 404:
                        self.assertIsNone(image.github_release("tag"))
                    else:
                        with self.assertRaises(urllib.error.HTTPError):
                            image.github_release("tag")

    def test_published_skips_but_draft_and_absent_retry(self):
        for release, skip, draft in (({"draft": False}, "true", "false"),
                                     ({"draft": True}, "false", "true"),
                                     (None, "false", "false")):
            with self.subTest(release=release), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)

                def download(url, path):
                    if url.endswith("download.json"):
                        path.write_text(json.dumps(feed()))
                    elif url.endswith("SHA256SUMS"):
                        path.write_text(f"{'a' * 64} *{image.IMAGE}\n")
                    else:
                        path.write_text("signature")

                with patch.object(image, "WORK", root / "work"), \
                     patch.object(image, "download", side_effect=download) as downloads, \
                     patch.object(image, "run"), \
                     patch.object(image, "github_release", return_value=release), \
                     patch.dict(os.environ, {"GITHUB_OUTPUT": str(root / "outputs"),
                                             "GITHUB_STEP_SUMMARY": str(root / "summary")}):
                    image.prepare()
                outputs = (root / "outputs").read_text()
                self.assertIn(f"skip={skip}\n", outputs)
                self.assertIn(f"draft_exists={draft}\n", outputs)
                self.assertTrue(all(not call.args[0].endswith(".img")
                                    for call in downloads.call_args_list))

    def test_invalid_signature_stops_before_release_lookup(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(image, "WORK", Path(tmp)), \
             patch.object(image, "download"), \
             patch.object(image, "run", side_effect=subprocess.CalledProcessError(1, "gpgv")), \
             patch.object(image, "github_release") as lookup:
            with self.assertRaises(subprocess.CalledProcessError):
                image.prepare()
            lookup.assert_not_called()


@unittest.skipUnless(shutil.which("qemu-img"), "qemu-img is required for integration test")
class ConversionTests(unittest.TestCase):
    def test_sparse_disk_round_trip_preserves_data_and_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw, qcow, vhdx = (root / name for name in ("disk.raw", "disk.qcow2", "disk.vhdx"))
            with raw.open("wb") as stream:
                stream.truncate(32 * 1024 * 1024)
                stream.seek(512)
                stream.write(b"test boot sectors" * 100)
                stream.seek(31 * 1024 * 1024)
                stream.write(b"test last block" * 100)
            image.run("qemu-img", "convert", "-f", "raw", "-O", "qcow2", raw, qcow)
            info = image.convert(qcow, vhdx)
            self.assertEqual(info["virtual-size"], 32 * 1024 * 1024)
            image.run("qemu-img", "compare", "-f", "raw", "-F", "vhdx", raw, vhdx)


if __name__ == "__main__":
    unittest.main()
