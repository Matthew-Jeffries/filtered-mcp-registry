"""
Lightweight curated MCP Registry.

Serves the MCP Registry v0.1 read API (https://github.com/modelcontextprotocol/registry)
containing ONLY the servers approved in a YAML config file. Server metadata is pulled
from an upstream registry (default: https://api.mcp.github.com) and cached in memory.

Dependencies: Python 3.10+ stdlib + PyYAML. Runs as a WSGI app (gunicorn on Azure)
or standalone with `python app.py`.
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

log = logging.getLogger("mcp-registry")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
CONFIG_PATH = Path(os.environ.get("REGISTRY_CONFIG", BASE_DIR / "registry.yaml"))
OFFICIAL_META = "io.modelcontextprotocol.registry/official"
PUBLISHER_META = "io.modelcontextprotocol.registry/publisher-provided"
CURATION_META = "io.modelcontextprotocol.registry/curation"
USER_AGENT = "lightweight-mcp-registry/1.0"


# --------------------------------------------------------------------------- config

@dataclass
class ApprovedServer:
    name: str
    versions: list[str] | None = None  # None = every upstream version is allowed
    notes: str | None = None
    approved_by: str | None = None
    approved_on: str | None = None


@dataclass
class Config:
    upstream_url: str = "https://api.mcp.github.com"
    cache_ttl: int = 900
    request_timeout: float = 15.0
    title: str = "MCP Registry"
    servers: list[ApprovedServer] = field(default_factory=list)


def load_config(path: Path) -> Config:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    up = raw.get("upstream") or {}
    reg = raw.get("registry") or {}
    servers: list[ApprovedServer] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw.get("servers") or []):
        if isinstance(entry, str):
            entry = {"name": entry}
        if not isinstance(entry, dict) or not entry.get("name"):
            raise ValueError(f"servers[{i}] must be a string or a mapping with a 'name'")
        name = str(entry["name"]).strip()
        if name in seen:
            raise ValueError(f"duplicate server in config: {name}")
        seen.add(name)
        versions = entry.get("versions")
        if versions is not None:
            if isinstance(versions, (str, int, float)):
                versions = [versions]
            versions = [str(v) for v in versions]
        servers.append(
            ApprovedServer(
                name=name,
                versions=versions,
                notes=entry.get("notes"),
                approved_by=entry.get("approved_by"),
                approved_on=str(entry["approved_on"]) if entry.get("approved_on") else None,
            )
        )
    return Config(
        upstream_url=str(os.environ.get("UPSTREAM_URL") or up.get("url") or Config.upstream_url).rstrip("/"),
        cache_ttl=int(up.get("cache_ttl_seconds", Config.cache_ttl)),
        request_timeout=float(up.get("timeout_seconds", Config.request_timeout)),
        title=str(reg.get("title", Config.title)),
        servers=servers,
    )


# --------------------------------------------------------------------------- upstream

def http_get_json(url: str, timeout: float) -> Any:
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _published_at(entry: dict) -> str:
    return ((entry.get("_meta") or {}).get(OFFICIAL_META) or {}).get("publishedAt") or ""


class Registry:
    """Holds the config plus an in-memory cache of approved server entries."""

    def __init__(self, config_path: Path, fetch: Callable[[str, float], Any] = http_get_json):
        self.config_path = config_path
        self.fetch = fetch
        self._lock = threading.Lock()
        self._refreshing = False
        self._config_mtime = 0.0
        self.config = Config()
        self.config_error: str | None = None
        # name -> list of upstream version entries ({"server":..., "_meta":...}), newest first
        self.entries: dict[str, list[dict]] = {}
        self.errors: dict[str, str] = {}
        self.last_sync: float = 0.0
        self.last_sync_duration: float = 0.0
        self._maybe_reload_config()

    # ---- config hot-reload
    def _maybe_reload_config(self) -> bool:
        try:
            mtime = self.config_path.stat().st_mtime
        except OSError as e:
            self.config_error = f"cannot read config: {e}"
            return False
        if mtime == self._config_mtime:
            return False
        try:
            cfg = load_config(self.config_path)
        except Exception as e:  # keep serving the last good config
            self.config_error = f"invalid config ({self.config_path.name}): {e}"
            log.error(self.config_error)
            self._config_mtime = mtime
            return False
        self.config, self.config_error, self._config_mtime = cfg, None, mtime
        self.last_sync = 0.0  # force a resync
        log.info("loaded config: %d approved servers", len(cfg.servers))
        return True

    # ---- sync
    def ensure_fresh(self) -> None:
        """Lazy refresh: block on first load, otherwise stale-while-revalidate."""
        self._maybe_reload_config()
        if time.time() - self.last_sync < self.config.cache_ttl:
            return
        if self.last_sync == 0.0:
            self.sync()
        else:
            with self._lock:
                if self._refreshing:
                    return
                self._refreshing = True
            threading.Thread(target=self.sync, daemon=True).start()

    def _fetch_with_retry(self, url: str, attempts: int = 3) -> Any:
        for i in range(attempts):
            try:
                return self.fetch(url, self.config.request_timeout)
            except urllib.error.HTTPError as e:
                if e.code < 500 or i == attempts - 1:
                    raise
            except Exception:  # timeouts, truncated reads, connection resets
                if i == attempts - 1:
                    raise
            time.sleep(0.5 * (i + 1))

    def _fetch_versions(self, name: str) -> list[dict]:
        cfg = self.config
        base = f"{cfg.upstream_url}/v0.1/servers/{urllib.parse.quote(name, safe='')}/versions?limit=30"
        entries: list[dict] = []
        cursor = None
        for _ in range(50):  # upstream paginates version lists; follow cursors (bounded)
            url = base + (f"&cursor={urllib.parse.quote(cursor, safe='')}" if cursor else "")
            data = self._fetch_with_retry(url)
            entries.extend(data.get("servers") or [])
            cursor = (data.get("metadata") or {}).get("nextCursor")
            if not cursor:
                break
        entries.sort(key=_published_at, reverse=True)
        return entries

    def sync(self) -> None:
        cfg = self.config
        started = time.time()
        new_entries: dict[str, list[dict]] = {}
        new_errors: dict[str, str] = {}

        def work(s: ApprovedServer):
            try:
                return s, self._fetch_versions(s.name), None
            except urllib.error.HTTPError as e:
                msg = "not found in upstream registry" if e.code == 404 else f"upstream HTTP {e.code}"
                return s, None, msg
            except Exception as e:
                return s, None, f"upstream error: {e}"

        try:
            with ThreadPoolExecutor(max_workers=8) as pool:
                for s, versions, err in pool.map(work, cfg.servers):
                    if err is not None:
                        new_errors[s.name] = err
                        if s.name in self.entries:  # keep last good data
                            new_entries[s.name] = self.entries[s.name]
                        continue
                    allowed = self._apply_policy(s, versions or [])
                    if not allowed:
                        new_errors[s.name] = (
                            f"none of the approved versions {s.versions} exist upstream" if s.versions else "no versions upstream"
                        )
                        continue
                    new_entries[s.name] = allowed
            self.entries, self.errors = new_entries, new_errors
            self.last_sync = time.time()
            self.last_sync_duration = self.last_sync - started
            log.info("synced %d/%d servers in %.2fs", len(new_entries), len(cfg.servers), self.last_sync_duration)
        finally:
            with self._lock:
                self._refreshing = False

    @staticmethod
    def _apply_policy(s: ApprovedServer, versions: list[dict]) -> list[dict]:
        if s.versions is not None:
            wanted = set(s.versions)
            versions = [v for v in versions if v["server"].get("version") in wanted]
        out = []
        for i, v in enumerate(versions):
            v = json.loads(json.dumps(v))  # deep copy
            meta = v.setdefault("_meta", {})
            official = meta.setdefault(OFFICIAL_META, {})
            # When versions are pinned, "latest" means the newest *approved* version.
            official["isLatest"] = i == 0
            if i > 0:  # READMEs can be ~150 KB each; keep only the latest one to save memory
                (v["server"].get("_meta") or {}).get(PUBLISHER_META, {}).get("github", {}).pop("readme", None)
            meta[CURATION_META] = {
                k: val
                for k, val in {"approved": True, "notes": s.notes, "approvedBy": s.approved_by, "approvedOn": s.approved_on}.items()
                if val is not None
            }
            out.append(v)
        return out

    # ---- queries
    def all_entries(self) -> list[dict]:
        order = [s.name for s in self.config.servers]
        out = []
        for name in order:
            out.extend(self.entries.get(name, []))
        return out

    def versions_of(self, name: str) -> list[dict] | None:
        return self.entries.get(name)


# --------------------------------------------------------------------------- API helpers

def encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"o": offset}).encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> int:
    try:
        pad = "=" * (-len(cursor) % 4)
        return max(0, int(json.loads(base64.urlsafe_b64decode(cursor + pad))["o"]))
    except Exception:
        raise ValueError("invalid cursor")


def _matches(entry: dict, q: str) -> bool:
    s = entry["server"]
    hay = " ".join(str(s.get(k) or "") for k in ("name", "title", "description")).lower()
    return q in hay


def _parse_time(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def list_servers(reg: Registry, params: dict[str, str]) -> dict:
    version = params.get("version")
    entries = reg.all_entries()
    if version in (None, "", "latest"):
        # Matches api.mcp.github.com: one entry (the latest approved version) per server.
        entries = [e for e in entries if e["_meta"][OFFICIAL_META].get("isLatest")]
    else:
        entries = [e for e in entries if e["server"].get("version") == version]
    if q := (params.get("search") or "").strip().lower():
        entries = [e for e in entries if _matches(e, q)]
    if since := params.get("updated_since"):
        t = _parse_time(since)
        entries = [
            e for e in entries
            if (u := e["_meta"][OFFICIAL_META].get("updatedAt") or _published_at(e)) and _parse_time(u) > t
        ]
    limit = min(max(int(params.get("limit") or 30), 1), 100)
    offset = decode_cursor(params["cursor"]) if params.get("cursor") else 0
    page = entries[offset : offset + limit]
    meta: dict[str, Any] = {"count": len(page)}
    if offset + limit < len(entries):
        meta["nextCursor"] = encode_cursor(offset + limit)
    return {"servers": page, "metadata": meta}


# --------------------------------------------------------------------------- WSGI app

class HTTPError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


STATUS_TEXT = {200: "OK", 204: "No Content", 400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed",
               500: "Internal Server Error", 502: "Bad Gateway"}

CORS_HEADERS = [
    ("Access-Control-Allow-Origin", "*"),
    ("Access-Control-Allow-Methods", "GET, OPTIONS"),
    ("Access-Control-Allow-Headers", "Content-Type, Authorization"),
    ("Access-Control-Max-Age", "86400"),
]


def create_app(registry: Registry):
    def respond(start_response, status: int, body: bytes, content_type: str, extra: list | None = None):
        headers = [("Content-Type", content_type), ("Content-Length", str(len(body))), *CORS_HEADERS, *(extra or [])]
        start_response(f"{status} {STATUS_TEXT.get(status, '')}", headers)
        return [body]

    def json_response(start_response, status: int, payload: Any, cache: str = "public, max-age=60"):
        body = json.dumps(payload, separators=(",", ":")).encode()
        return respond(start_response, status, body, "application/json", [("Cache-Control", cache)])

    def static_file(start_response, rel: str):
        path = (STATIC_DIR / rel).resolve()
        if not str(path).startswith(str(STATIC_DIR)) or not path.is_file():
            raise HTTPError(404, "Not found")
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype == "application/javascript":
            ctype += "; charset=utf-8"
        return respond(start_response, 200, path.read_bytes(), ctype, [("Cache-Control", "public, max-age=300")])

    def route(environ, start_response):
        method = environ["REQUEST_METHOD"]
        # PATH_INFO is latin-1 decoded and already percent-decoded by the server; use RAW_URI when available
        raw = environ.get("RAW_URI") or environ.get("REQUEST_URI")
        path = urllib.parse.unquote(urllib.parse.urlsplit(raw).path) if raw else environ.get("PATH_INFO", "/").encode("latin-1").decode("utf-8")
        params = {k: v[0] for k, v in urllib.parse.parse_qs(environ.get("QUERY_STRING", "")).items()}

        if method == "OPTIONS":
            return respond(start_response, 204, b"", "text/plain")
        if method not in ("GET", "HEAD"):
            raise HTTPError(405, "Method not allowed")

        if path in ("/", "/index.html"):
            return static_file(start_response, "index.html")
        if path.startswith("/static/"):
            return static_file(start_response, path[len("/static/"):])
        if path in ("/v0/health", "/v0.1/health", "/health"):
            return json_response(start_response, 200, {"status": "ok"}, cache="no-store")
        if path in ("/v0/ping", "/v0.1/ping"):
            return json_response(start_response, 200, {"pong": True}, cache="no-store")

        # Registry API (v0.1 is current; v0 kept as an alias for older clients)
        for prefix in ("/v0.1", "/v0"):
            if path == f"{prefix}/servers":
                registry.ensure_fresh()
                try:
                    return json_response(start_response, 200, list_servers(registry, params))
                except ValueError as e:
                    raise HTTPError(400, str(e))
            if path.startswith(f"{prefix}/servers/"):
                registry.ensure_fresh()
                rest = path[len(f"{prefix}/servers/"):]
                if "/versions" not in rest:
                    raise HTTPError(404, "Server not found")
                name, _, tail = rest.rpartition("/versions")
                if tail and not tail.startswith("/"):
                    name, tail = rest, ""
                versions = registry.versions_of(name)
                if not versions:
                    raise HTTPError(404, "Server not found")
                if tail in ("", "/"):
                    return json_response(start_response, 200, {"servers": versions, "metadata": {"count": len(versions)}})
                wanted = urllib.parse.unquote(tail[1:])
                for v in versions:
                    if (wanted == "latest" and v["_meta"][OFFICIAL_META].get("isLatest")) or v["server"].get("version") == wanted:
                        return json_response(start_response, 200, v)
                raise HTTPError(404, "Server version not found")

        # Dashboard support endpoints
        if path == "/api/status":
            registry.ensure_fresh()
            cfg = registry.config
            return json_response(start_response, 200, {
                "title": cfg.title,
                "upstream": cfg.upstream_url,
                "approvedCount": len(cfg.servers),
                "availableCount": len(registry.entries),
                "lastSync": datetime.fromtimestamp(registry.last_sync, timezone.utc).isoformat() if registry.last_sync else None,
                "lastSyncSeconds": round(registry.last_sync_duration, 2),
                "cacheTtlSeconds": cfg.cache_ttl,
                "configError": registry.config_error,
                "errors": registry.errors,
                "approved": [
                    {"name": s.name, "versions": s.versions, "notes": s.notes, "approvedBy": s.approved_by, "approvedOn": s.approved_on}
                    for s in cfg.servers
                ],
            }, cache="no-store")
        if path == "/api/upstream/servers":
            # Proxy to upstream so admins can browse candidates and copy a YAML snippet.
            allowed = {k: params[k] for k in ("search", "cursor", "limit") if k in params}
            allowed.setdefault("limit", "30")
            url = f"{registry.config.upstream_url}/v0.1/servers?{urllib.parse.urlencode(allowed)}"
            try:
                data = registry.fetch(url, registry.config.request_timeout)
            except Exception as e:
                raise HTTPError(502, f"upstream error: {e}")
            approved = {s.name for s in registry.config.servers}
            for e in data.get("servers") or []:
                e["approved"] = e["server"].get("name") in approved
                # README blobs can be huge; the dashboard doesn't need them.
                (e["server"].get("_meta") or {}).get(PUBLISHER_META, {}).get("github", {}).pop("readme", None)
            return json_response(start_response, 200, data, cache="public, max-age=120")

        raise HTTPError(404, "Not found")

    def app(environ, start_response):
        try:
            return route(environ, start_response)
        except HTTPError as e:
            return json_response(start_response, e.status, {"error": e.message}, cache="no-store")
        except Exception:
            log.exception("unhandled error")
            return json_response(start_response, 500, {"error": "Internal server error"}, cache="no-store")

    return app


registry = Registry(CONFIG_PATH)
app = create_app(registry)


def check_config() -> int:
    """Validate the config and confirm every approved server resolves upstream (used in CI)."""
    if registry.config_error:
        print(f"ERROR: {registry.config_error}")
        return 1
    registry.sync()
    for s in registry.config.servers:
        if s.name in registry.errors:
            print(f"FAIL  {s.name}: {registry.errors[s.name]}")
        else:
            versions = [v["server"]["version"] for v in registry.entries[s.name]]
            print(f"OK    {s.name} ({len(versions)} version(s), latest {versions[0]})")
    return 1 if registry.errors else 0


if __name__ == "__main__":
    import sys

    if "--check" in sys.argv:
        sys.exit(check_config())

    from socketserver import ThreadingMixIn
    from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

    class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
        daemon_threads = True

    class QuietHandler(WSGIRequestHandler):
        def log_message(self, fmt, *args):
            log.debug(fmt, *args)

    port = int(os.environ.get("PORT", "8000"))
    with make_server("0.0.0.0", port, app, server_class=ThreadingWSGIServer, handler_class=QuietHandler) as httpd:
        log.info("serving on http://localhost:%d", port)
        httpd.serve_forever()
