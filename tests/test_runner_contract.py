"""Verify the same signed wire fixtures that the real Worker handler consumes."""
import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from commandcode_bot import task


FIXTURE = Path(__file__).resolve().parents[1] / "worker" / "test" / "fixtures" / "runner-contract.json"


class RunnerContractTests(unittest.TestCase):
    def test_worker_wire_fixtures_match_python_fetch_and_publish(self):
        # Expand the long Unicode answer before signing/comparing the wire bytes.
        for example in json.loads(FIXTURE.read_text(encoding="utf-8").replace("__CONTRACT_TEXT__", "\U0001f680" * 60000)):
            with self.subTest(mode=example["mode"]), tempfile.TemporaryDirectory() as temp:
                out = Path(temp) / "out"
                home = Path(temp) / "home"
                captured = []

                class Response:
                    def __init__(self, status, body):
                        self.status, self.body = status, body

                    def read(self):
                        return self.body

                    def __enter__(self):
                        return self

                    def __exit__(self, *args):
                        return False

                def urlopen(request, **kwargs):
                    captured.append(dict(url=request.full_url, method=request.get_method(),
                                         headers=dict(request.header_items()),
                                         body=(request.data or b"").decode("utf-8")))
                    return (Response(200, json.dumps(example["task"]).encode())
                            if request.get_method() == "GET" else Response(204, b""))

                with patch.dict(os.environ, GITHUB_RUN_ID="123", GITHUB_REPOSITORY="owner/app"), \
                        patch.object(task.time, "time", return_value=example["timestamp"]), \
                        patch.object(task.urllib.request, "urlopen", side_effect=urlopen):
                    fetched = task.fetch_task("https://worker", "test-secret", example["task"]["id"], out, home=home)
                    if example["mode"] != "missing":
                        (out / "result.json").write_text(json.dumps(example["artifact_result"]), encoding="utf-8")
                        if example["mode"] == "empty":
                            (out / "changes.patch").write_bytes(b"")
                        else:
                            blob = base64.b64decode(example["session"])
                            (out / "session.tar.gz").write_bytes(blob)
                            task.safe_extract(blob, home)
                            for suffix in (".jsonl", ".meta.json", ".checkpoints.jsonl"):
                                self.assertTrue((home / "projects/slug" / ("session-1" + suffix)).is_file())
                    task.publish("https://worker", "test-secret", fetched["id"], fetched, out, Path(temp))
                self.assertEqual(captured, example["requests"])


if __name__ == "__main__":
    unittest.main()
