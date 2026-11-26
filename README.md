# Lightweight curated MCP Registry

A tiny, self-hosted [MCP Registry](https://github.com/modelcontextprotocol/registry) that exposes **only the MCP servers you approve** in a YAML file. Server metadata comes from the public GitHub MCP registry (`https://api.mcp.github.com`) and is cached in memory, so there's no database and nothing to sync.

- **Approve a server** by adding one line to [`registry.yaml`](registry.yaml) and merging the PR.
- **Developers** point VS Code / GitHub Copilot at this registry's URL and only see approved servers.
- **Dashboard** at `/` lists approved servers and lets admins browse upstream and copy a YAML snippet.
- **Runs on the Azure App Service Free (F1) tier.** Dependencies: Python stdlib + PyYAML (+ gunicorn on Azure).

```
 developers / VS Code / Copilot
            │  GET /v0.1/servers …
            ▼
 ┌──────────────────────────┐   per approved server, cached (default 15 min)
 │  app.py  (this registry) │ ─────────────────────────────────────────────▶  api.mcp.github.com
 │  registry.yaml allowlist │
 └──────────────────────────┘
```

## Approving servers

```yaml
servers:
  # every upstream version allowed; newest is "latest"
  - name: io.github.github/github-mcp-server
    notes: Approved for all engineers. Use fine-grained PATs.
    approved_by: platform-security
    approved_on: 2026-11-01

  # pinned to reviewed versions; "latest" = newest *approved* version
  - name: io.github.upstash/context7
    versions: ["4.7.3"]

  # shorthand
  - microsoft/markitdown
```

Server names must match the upstream registry exactly. Find them in the dashboard's **Browse upstream** tab (it has a *Copy YAML* button) or at `https://api.mcp.github.com/v0.1/servers?search=<term>`.

CI runs `python app.py --check` on every PR, which fails if an approved name or pinned version doesn't exist upstream:

```
OK    io.github.github/github-mcp-server (65 version(s), latest 2.3.0)
FAIL  microsoft/markitdownz: not found in upstream registry
```

The running app also hot-reloads `registry.yaml` when the file changes. If an edit is invalid, it keeps serving the last valid config and shows a warning on the dashboard.

## API

The registry implements the read-only subset of the MCP Registry API, under `/v0.1` (current) and `/v0` (alias):

| Endpoint | Notes |
|---|---|
| `GET /v0.1/servers` | `search`, `limit` (≤100), `cursor`, `version`, `updated_since`. Returns the latest approved version of each server by default, like `api.mcp.github.com` does. |
| `GET /v0.1/servers/{name}/versions` | All approved versions (`{name}` URL-encoded, e.g. `io.github.github%2Fgithub-mcp-server`) |
| `GET /v0.1/servers/{name}/versions/{version\|latest}` | One version |
| `GET /v0.1/health`, `/v0.1/ping` | Health checks |

Responses are the upstream entries unchanged, plus:
- `_meta["io.modelcontextprotocol.registry/official"].isLatest` is recomputed against the approved versions.
- `_meta["io.modelcontextprotocol.registry/curation"]` holds `approved`, `notes`, `approvedBy` and `approvedOn`.

CORS is open (`*`) so browser-based clients can call it.

## Pointing clients at it

- **GitHub Copilot (org/enterprise):** set the *MCP Registry URL* in your Copilot policy settings to `https://<app>.azurewebsites.net`. Pair it with the "registry only" access policy to restrict developers to approved servers.
- **VS Code:** `"chat.mcp.gallery.serviceUrl": "https://<app>.azurewebsites.net"` in settings (or via device management policy).

Check the current GitHub/VS Code docs for the exact policy names. They've been changing as the MCP registry features roll out.

## Run locally

```bash
pip install -r requirements.txt     # or just PyYAML
python app.py                       # http://localhost:8000
python -m unittest discover -s tests
```

Environment variables: `REGISTRY_CONFIG` (path to the YAML file), `UPSTREAM_URL` (overrides the config), `PORT`, `LOG_LEVEL`.

## Deploy to Azure (Free tier)

1. **Create the infrastructure** (Linux App Service plan, F1 by default):
   ```bash
   az group create -n rg-mcp-registry -l westeurope
   az deployment group create -g rg-mcp-registry -f infra/main.bicep -p appName=<unique-name>
   ```
2. **Set up GitHub → Azure OIDC.** Create an Entra app or managed identity with a federated credential for this repo and give it the *Website Contributor* role on the web app. Then add:
   - secrets `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`
   - variable `AZURE_WEBAPP_NAME`
   - a `production` environment (add required reviewers there if you want a second approval gate)
3. Push to `main`. [`.github/workflows/deploy.yml`](.github/workflows/deploy.yml) tests, validates and zip-deploys the app, and Azure runs `pip install` during the deploy.

Quick manual deploy without CI:
```bash
zip -r app.zip app.py registry.yaml requirements.txt static
az webapp deploy -g rg-mcp-registry -n <unique-name> --src-path app.zip --type zip
```

### Free tier (F1) trade-offs

| | F1 (Free) | B1 (~$13/mo) |
|---|---|---|
| Always On | ❌ sleeps after ~20 min idle; the first request after that takes a few seconds | ✅ |
| CPU quota | 60 CPU-min/day (plenty: requests are served from memory) | unlimited |
| Custom domain + TLS | ❌ (`*.azurewebsites.net` only) | ✅ |
| SLA | none | 99.95% |

To move up later, redeploy the Bicep file with `-p sku=B1`. Nothing else changes.

The app runs with **one gunicorn worker** (8 threads) so all requests share a single in-memory cache. Each sync makes one upstream call per approved server, in parallel. Upstream metadata is cached for `cache_ttl_seconds` and refreshed in the background (stale-while-revalidate). If upstream is down, the last good data keeps being served.

## Security notes

- The dashboard and API are read-only and public, like the upstream registry. To restrict access, enable App Service **Authentication (Easy Auth)** with Entra ID, but check first that your MCP clients can call an authenticated registry.
- Approval happens through PR review of `registry.yaml`, so use branch protection and CODEOWNERS on that file (for example `registry.yaml @your-org/platform-security`).
- Approving a server here doesn't vet its code. Pin `versions` for servers you've reviewed so a new upstream release isn't auto-approved.
