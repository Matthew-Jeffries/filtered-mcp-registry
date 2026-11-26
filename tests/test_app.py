"""Stdlib-only tests: python -m unittest discover -s tests"""

import io
import json
import os
import sys
import tempfile
import textwrap
import time
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("REGISTRY_CONFIG", str(Path(__file__).resolve().parents[1] / "registry.yaml"))

import app as appmod  # noqa: E402

UP = "https://upstream.test"


def entry(name, version, published, title=None, desc=""):
    return {
        "server": {"name": name, "version": version, "title": title, "description": desc},
        "_meta": {appmod.OFFICIAL_META: {"publishedAt": published, "updatedAt": published, "isLatest": False, "status": "active"}},
    }


UPSTREAM = {
    "io.github.acme/alpha": [
        entry("io.github.acme/alpha", "1.0.0", "2026-01-01T00:00:00Z", "Alpha", "first server"),
        entry("io.github.acme/alpha", "2.0.0", "2026-06-01T00:00:00Z", "Alpha", "first server"),
        entry("io.github.acme/alpha", "1.5.0", "2026-03-01T00:00:00Z", "Alpha", "first server"),
    ],
    "io.github.acme/beta": [entry("io.github.acme/beta", "0.1.0", "2026-02-01T00:00:00Z", "Beta", "database tools")],
    "io.github.acme/not-approved": [entry("io.github.acme/not-approved", "9.9.9", "2026-02-01T00:00:00Z")],
}


class FakeUpstream:
    def __init__(self):
        self.calls = []
        self.fail = set()

    def __call__(self, url, timeout):
        self.calls.append(url)
        import urllib.parse
        path = urllib.parse.urlsplit(url).path
        if path == "/v0.1/servers":
            return {"servers": [v[-1] for v in UPSTREAM.values()], "metadata": {"count": 3}}
        name = urllib.parse.unquote(path[len("/v0.1/servers/"):].rsplit("/versions", 1)[0])
        if name in self.fail:
            raise urllib.error.URLError("boom")
        if name not in UPSTREAM:
            raise urllib.error.HTTPError(url, 404, "nf", {}, io.BytesIO(b""))
        return {"servers": UPSTREAM[name], "metadata": {"count": len(UPSTREAM[name])}}


CONFIG = f"""
registry: {{title: Test Registry}}
upstream: {{url: {UP}, cache_ttl_seconds: 900}}
servers:
  - name: io.github.acme/alpha
    versions: ["1.0.0", "1.5.0"]
    notes: pinned
    approved_by: sec
  - io.github.acme/beta
  - name: io.github.acme/missing
"""


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Path(self.tmp.name) / "registry.yaml"
        self.cfg.write_text(CONFIG)
        self.fake = FakeUpstream()
        self.reg = appmod.Registry(self.cfg, fetch=self.fake)
        self.app = appmod.create_app(self.reg)

    def tearDown(self):
        self.tmp.cleanup()

    def get(self, raw_uri):
        path, _, qs = raw_uri.partition("?")
        environ = {"REQUEST_METHOD": "GET", "RAW_URI": raw_uri, "PATH_INFO": path, "QUERY_STRING": qs}
        out = {}

        def start_response(status, headers):
            out["status"] = int(status.split()[0])
            out["headers"] = dict(headers)

        body = b"".join(self.app(environ, start_response))
        ctype = out["headers"]["Content-Type"]
        return out["status"], (json.loads(body) if ctype.startswith("application/json") else body), out["headers"]

    def test_list_only_approved_latest(self):
        status, data, headers = self.get("/v0.1/servers")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Access-Control-Allow-Origin"], "*")
        got = [(e["server"]["name"], e["server"]["version"]) for e in data["servers"]]
        # 2.0.0 isn't approved, so 1.5.0 becomes latest; missing server is omitted
        self.assertEqual(got, [("io.github.acme/alpha", "1.5.0"), ("io.github.acme/beta", "0.1.0")])
        self.assertTrue(data["servers"][0]["_meta"][appmod.OFFICIAL_META]["isLatest"])
        self.assertEqual(data["servers"][0]["_meta"][appmod.CURATION_META]["approvedBy"], "sec")

    def test_versions_endpoints(self):
        name = "io.github.acme%2Falpha"
        s, data, _ = self.get(f"/v0.1/servers/{name}/versions")
        self.assertEqual([e["server"]["version"] for e in data["servers"]], ["1.5.0", "1.0.0"])
        s, data, _ = self.get(f"/v0.1/servers/{name}/versions/latest")
        self.assertEqual(data["server"]["version"], "1.5.0")
        s, data, _ = self.get(f"/v0/servers/{name}/versions/1.0.0")
        self.assertEqual((s, data["server"]["version"]), (200, "1.0.0"))
        s, _, _ = self.get(f"/v0.1/servers/{name}/versions/2.0.0")
        self.assertEqual(s, 404)
        s, _, _ = self.get("/v0.1/servers/io.github.acme%2Fnot-approved/versions/latest")
        self.assertEqual(s, 404)

    def test_search_version_and_pagination(self):
        _, data, _ = self.get("/v0.1/servers?search=database")
        self.assertEqual([e["server"]["name"] for e in data["servers"]], ["io.github.acme/beta"])
        _, data, _ = self.get("/v0.1/servers?version=1.0.0")
        self.assertEqual([e["server"]["version"] for e in data["servers"]], ["1.0.0"])
        _, p1, _ = self.get("/v0.1/servers?limit=1")
        self.assertEqual(p1["metadata"]["count"], 1)
        _, p2, _ = self.get(f"/v0.1/servers?limit=1&cursor={p1['metadata']['nextCursor']}")
        self.assertNotIn("nextCursor", p2["metadata"])
        self.assertNotEqual(p1["servers"][0]["server"]["name"], p2["servers"][0]["server"]["name"])
        s, _, _ = self.get("/v0.1/servers?cursor=garbage!")
        self.assertEqual(s, 400)
        _, data, _ = self.get("/v0.1/servers?updated_since=2026-02-15T00:00:00Z")
        self.assertEqual([e["server"]["name"] for e in data["servers"]], ["io.github.acme/alpha"])

    def test_status_reports_errors(self):
        _, st, _ = self.get("/api/status")
        self.assertEqual(st["title"], "Test Registry")
        self.assertEqual((st["approvedCount"], st["availableCount"]), (3, 2))
        self.assertIn("not found", st["errors"]["io.github.acme/missing"])

    def test_cache_and_stale_data_on_upstream_failure(self):
        self.get("/v0.1/servers")
        n = len(self.fake.calls)
        self.get("/v0.1/servers")
        self.assertEqual(len(self.fake.calls), n, "second request should hit cache")
        self.fake.fail.add("io.github.acme/alpha")
        self.reg.sync()
        _, data, _ = self.get("/v0.1/servers")
        self.assertIn("io.github.acme/alpha", [e["server"]["name"] for e in data["servers"]])
        self.assertIn("upstream error", self.reg.errors["io.github.acme/alpha"])

    def test_config_hot_reload_and_invalid_config(self):
        self.get("/v0.1/servers")
        self.cfg.write_text(CONFIG.replace("  - io.github.acme/beta\n", ""))
        os.utime(self.cfg, (time.time() + 5, time.time() + 5))
        _, data, _ = self.get("/v0.1/servers")
        self.assertEqual([e["server"]["name"] for e in data["servers"]], ["io.github.acme/alpha"])
        self.cfg.write_text("servers: [ {notes: no-name} ]")
        os.utime(self.cfg, (time.time() + 10, time.time() + 10))
        _, data, _ = self.get("/v0.1/servers")
        self.assertEqual(len(data["servers"]), 1, "keeps serving last good config")
        _, st, _ = self.get("/api/status")
        self.assertIn("invalid config", st["configError"])

    def test_retries_transient_errors_and_strips_old_readmes(self):
        for e in UPSTREAM["io.github.acme/alpha"]:
            e["server"]["_meta"] = {appmod.PUBLISHER_META: {"github": {"readme": "x" * 100}}}
        real, flaky = self.fake, {"n": 0}

        def fetch(url, timeout):
            if "alpha" in url and flaky["n"] < 2:
                flaky["n"] += 1
                raise ConnectionResetError("truncated")
            return real(url, timeout)

        self.reg.fetch = fetch
        self.reg.sync()
        self.assertNotIn("io.github.acme/alpha", self.reg.errors)
        latest, older = self.reg.versions_of("io.github.acme/alpha")
        gh = lambda v: v["server"]["_meta"][appmod.PUBLISHER_META]["github"]
        self.assertIn("readme", gh(latest))
        self.assertNotIn("readme", gh(older))
        for e in UPSTREAM["io.github.acme/alpha"]:
            e["server"].pop("_meta")

    def test_upstream_browse_marks_approved(self):
        _, data, _ = self.get("/api/upstream/servers?search=acme")
        flags = {e["server"]["name"]: e["approved"] for e in data["servers"]}
        self.assertEqual(flags["io.github.acme/beta"], True)
        self.assertEqual(flags["io.github.acme/not-approved"], False)

    def test_dashboard_and_health(self):
        s, body, h = self.get("/")
        self.assertEqual(s, 200)
        self.assertIn(b"<title>", body)
        self.assertEqual(self.get("/v0.1/health")[0], 200)
        self.assertEqual(self.get("/static/../app.py")[0], 404)
        self.assertEqual(self.get("/nope")[0], 404)


if __name__ == "__main__":
    unittest.main()
