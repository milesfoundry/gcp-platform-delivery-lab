"""Exercise public content validation without registry access."""

import hashlib
from io import BytesIO
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from verify_anonymous_image import verify


class AnonymousImageTests(unittest.TestCase):
    def test_rejects_mutable_or_foreign_inputs_before_network(self):
        with patch("verify_anonymous_image.urlopen") as network:
            for image in ("ghcr.io/milesfoundry/report-workshop:latest",
                          "ghcr.io/other/report@sha256:" + "a" * 64):
                with self.assertRaises(ValueError):
                    verify(image)
            network.assert_not_called()

    def test_verifies_both_platforms_and_rejects_corrupt_content(self):
        content = {}

        def entry(raw):
            digest = "sha256:" + hashlib.sha256(raw).hexdigest()
            content[digest] = raw
            return {"digest": digest}

        config = entry(b"synthetic config")
        layer = entry(b"synthetic runtime layer")
        manifests = []
        for architecture in ("amd64", "arm64"):
            manifest = entry(json.dumps({"config": config, "layers": [layer],
                                         "architecture": architecture}).encode())
            manifest["platform"] = {"os": "linux", "architecture": architecture}
            manifests.append(manifest)
        index = entry(json.dumps({"manifests": manifests}).encode())
        image = "ghcr.io/milesfoundry/report-workshop@" + index["digest"]
        opener = SimpleNamespace(open=lambda request, timeout: BytesIO(
            content[request.full_url.rsplit("/", 1)[1]]))
        with patch("verify_anonymous_image.build_opener", return_value=opener), patch(
                "verify_anonymous_image.urlopen",
                side_effect=lambda *args, **kwargs: BytesIO(b'{"token":"public-only"}')):
            record = verify(image)
            self.assertTrue(record["anonymous_pull_verified"])
            self.assertEqual(record["platforms"], ["amd64", "arm64"])
            self.assertEqual(len(record["verified_digests"]), 5)
            content[layer["digest"]] = b"corrupt"
            with self.assertRaisesRegex(ValueError, "digest mismatch"):
                verify(image)


if __name__ == "__main__":
    unittest.main()
