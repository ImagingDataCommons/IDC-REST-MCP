# IDC API — Deploying to Cloud Run

The service is **stateless** and needs **no secrets, GCP data access, or external
database**: the read-only DuckDB file is baked into the image from the bundled `idc-index`
Parquet (see [Dockerfile](../Dockerfile)). That makes Cloud Run a natural fit —
scale-to-zero, one container, public unauthenticated access.

This guide covers the **REST API**. The optional remote **MCP** service is at the end.

## Prerequisites

- `gcloud` CLI authenticated (`gcloud auth login`) and a project with billing enabled.
- Roles to deploy: `roles/run.admin`, `roles/artifactregistry.admin` (or writer),
  `roles/cloudbuild.builds.editor`, and `roles/iam.serviceAccountUser`.

Set shimmable variables for the commands below:

```bash
export PROJECT_ID=your-project
export REGION=us-central1
export REPO=idc                       # Artifact Registry repo name
export IMAGE=$REGION-docker.pkg.dev/$PROJECT_ID/$REPO/idc-api-v3:latest
gcloud config set project "$PROJECT_ID"
```

## 1. One-time project setup

```bash
gcloud services enable run.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com

gcloud artifacts repositories create "$REPO" \
  --repository-format=docker --location="$REGION" \
  --description="IDC API images"
```

## 2. Build & push the image

**Cloud Build (no local Docker):**

```bash
gcloud builds submit --config dev/cloudbuild.yaml --substitutions _IMAGE="$IMAGE"
```

**Or local Docker:**

```bash
gcloud auth configure-docker "$REGION-docker.pkg.dev"
docker build -t "$IMAGE" .
docker push "$IMAGE"
```

## 3. Deploy the REST API

```bash
gcloud run deploy idc-api-v3 \
  --image "$IMAGE" \
  --region "$REGION" \
  --allow-unauthenticated \
  --cpu 2 --memory 4Gi \
  --concurrency 40 \
  --min-instances 0 --max-instances 5 \
  --cpu-boost \
  --set-env-vars IDC_API_DUCKDB_MEMORY_LIMIT=3GB,IDC_API_DUCKDB_THREADS=2,IDC_API_BUILD=$(git rev-parse --short HEAD)
```

> `IDC_API_BUILD` (a short git SHA / image tag) is stamped into the software version reported at
> `GET /v3/version` (`build`), `GET /`, and OpenAPI `info.version`, so you can confirm which build
> a hosted REST instance is running — the same mechanism the MCP service uses for
> `serverInfo.version` (see *Which build is live?* below). Omit it and `/v3/version` reports
> `build: null` (the package version alone, static across redeploys of a release).

`--allow-unauthenticated` is correct here — all IDC data is open. Cloud Run injects `PORT`
(8080); the container already listens on `0.0.0.0:$PORT`. The default compute service account
is fine — the service needs **no** GCP permissions (no BigQuery/GCS access; downloads are
client-side). For least privilege you may attach a dedicated SA with no roles via
`--service-account`.

### ⚠️ Sizing: DuckDB memory must fit the container

`IDC_API_DUCKDB_MEMORY_LIMIT` defaults to **4GB**. If that exceeds the Cloud Run memory the
container will be OOM-killed under load. **Always set it below `--memory`, leaving headroom for
Python/uvicorn:**

| `--memory` | set `IDC_API_DUCKDB_MEMORY_LIMIT` | `IDC_API_DUCKDB_THREADS` |
|---|---|---|
| `2Gi` | `1200MB` | match `--cpu` (e.g. `1`–`2`) |
| `4Gi` | `3GB` | `2` |

Set `IDC_API_DUCKDB_THREADS` ≈ `--cpu` so concurrent requests don't oversubscribe the CPU
(each query is already capped to this many threads). Other tunables:
`IDC_API_SQL_MAX_ROWS`, `IDC_API_SQL_TIMEOUT_SECONDS`, `IDC_API_MANIFEST_HARD_CAP`
(see [settings.py](../src/idc_api/settings.py)). Leave `IDC_API_DUCKDB_PATH` as baked.

> Avoid setting `IDC_API_CORS_ALLOW_ORIGINS` via `--set-env-vars`: it's a list and
> pydantic-settings expects JSON (`["https://app.example.com"]`), which is awkward to quote in
> gcloud. The default `["*"]` is appropriate for an open API.

### Rate limiting / abuse protection

`--allow-unauthenticated` means anyone can call `run_sql`/manifest endpoints; `--concurrency`
and `--max-instances` above bound the *total* damage (cost, availability) a burst of traffic can
do, but they are not a per-caller rate limit — one abusive IP can still consume the whole
`--max-instances` budget and starve everyone else. Each query is already capped (statement
timeout, row limits — see `IDC_API_SQL_*` above), but many queries at once still hurt. If abuse
becomes a real concern, add a per-IP rate limit **at the edge**, not in the app:

- **Cloud Armor** (attach to a Cloud Run + external Application Load Balancer setup) — rate-based
  bans per IP, the standard GCP-native option.
- **API Gateway / Apigee** in front of Cloud Run — if you also want API keys or quotas.

Both sit in front of the container and need no code change. The structured request/tool-call
logs (`idc_api.rest` / `idc_api.mcp` loggers, shipped to Cloud Logging automatically) are the
signal to watch for "is someone abusing this" before reaching for either.

## 4. Verify

```bash
URL=$(gcloud run services describe idc-api-v3 --region "$REGION" --format='value(status.url)')
curl -s "$URL/health"; echo
curl -s "$URL/v3/version"; echo
open "$URL/docs"   # Swagger UI
```

> **Don't use `/healthz` as a health-check path on Cloud Run's default `*.run.app` domain.**
> Google's front end reserves that exact path and returns its own generic 404 page for it
> before the request ever reaches your container — a well-known Cloud Run gotcha (other
> frameworks, e.g. Streamlit, have hit the same thing). The app exposes `/health` instead.

## 5. Updating for a new IDC release

The image bakes whatever `idc-index-data` resolves at build time. To publish a new IDC version,
**rebuild and redeploy** (steps 2–3). For reproducibility, pin the version in
[pyproject.toml](../pyproject.toml) (e.g. `idc-index==0.12.2`, which pulls a specific
`idc-index-data`) so a rebuild is deterministic; bump the pin to move IDC versions. The running
version is always reported at `/v3/version`.

## Optional: remote MCP service (HTTP)

Deploy the same image with the MCP command to expose the tools over MCP streamable-http
(download is disabled in hosted mode — manifests/URLs only):

```bash
gcloud run deploy idc-mcp-v3 \
  --image "$IMAGE" \
  --region "$REGION" \
  --allow-unauthenticated \
  --cpu 2 --memory 4Gi \
  --min-instances 0 --max-instances 5 \
  --command idc-mcp \
  --args=--http,--host,0.0.0.0,--port,8080 \
  --set-env-vars IDC_API_DUCKDB_MEMORY_LIMIT=3GB,IDC_API_DUCKDB_THREADS=2,IDC_API_BUILD=$(git rev-parse --short HEAD)
```

The MCP endpoint is then `https://<service-url>/mcp` (note the `/mcp` path).

> **Which build is live?** The MCP `initialize` handshake reports `serverInfo.version`. Left
> unset it would echo the MCP SDK's own version, so set `IDC_API_BUILD` (above) to a short git
> SHA: the server appends it to the package version as a PEP 440 local segment (e.g.
> `3.0.0.dev0+a1b2c3d`), giving a version string that moves on every redeploy. Read it back with
> a `tools/list`-less initialize probe:
>
> ```bash
> curl -s -X POST "$URL/mcp" -H 'Content-Type: application/json' \
>   -H 'Accept: application/json, text/event-stream' \
>   -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}' \
>   | sed -n 's/.*"version":"\([^"]*\)".*/\1/p'
> ```
>
> The **REST** service reports the same software version with no handshake needed: `GET /v3/version`
> returns `api_version` + `build`, and the combined string is also the OpenAPI `info.version` at
> `/openapi.json` (and `server_version` at `GET /`).

> **Host-header / DNS-rebinding protection.** The MCP streamable-HTTP transport ships with
> DNS-rebinding protection that allow-lists the `Host` header to localhost only, which would
> reject a Cloud Run domain with **HTTP 421 "Invalid Host header."** Because this service is
> public, unauthenticated, and read-only, that protection is **disabled by default**
> (`mcp_dns_rebinding_protection=False`; see [settings.py](../src/idc_api/settings.py)), so the
> hosted endpoint works out of the box. To re-enable it, set
> `IDC_API_MCP_DNS_REBINDING_PROTECTION=true` and
> `IDC_API_MCP_ALLOWED_HOSTS=["your-host"]` (JSON). If you still see a 421, you're running an
> image built before this default — rebuild and redeploy.

> **Stateless by design.** The MCP HTTP transport is configured stateless
> (`stateless_http=True`, `json_response=True` in [mcp/server.py](../src/idc_api/mcp/server.py)),
> so each request is self-contained and the service **autoscales across instances like the REST
> API** — no session affinity or single-instance pin needed. This is safe because the server
> exposes only client-initiated tools + static resources (no server→client sampling,
> elicitation, subscriptions, or streamed progress, which are the only things that would need a
> persistent session). The local **stdio** MCP remains the primary path for end users — it's the
> only mode that can download files to the user's machine.

## Notes

- **Cost:** with `--min-instances 0` the service scales to zero and costs nothing idle; cold
  starts are fast because the DuckDB file is prebuilt in the image.
- **Custom domain / CDN:** map a domain via Cloud Run domain mappings, or front it with a load
  balancer + Cloud CDN. Discovery responses change only per IDC release, so they cache well —
  add `Cache-Control` if you put a CDN in front. See [caching_and_cdn.md](caching_and_cdn.md)
  for a primer and the proposed (not-yet-implemented) cache-header enhancement.
- **CI/CD:** deployment is automated across dev / test / prod tiers with GitHub
  Actions — see [CI/CD: dev / test / prod tiers](#cicd-dev--test--prod-tiers) below.
  The manual steps 2–3 above remain the ground truth for what those workflows run and for
  first-time / out-of-band deploys.

## CI/CD: dev / test / prod tiers

Three GitHub Actions workflows share one reusable deploy job and use **GitHub Environments**
(`dev`, `test`, `prod`) for per-tier config and governance. Each tier is a **separate GCP
project** — matching the legacy IDC-API (CircleCI) convention of one project per tier.

**Build once, promote the same digest.** Because the read-only DuckDB index is baked into the
image *at build time*, the image is built **once** (on merge to `main`), pushed to a single
shared Artifact Registry, and the *same immutable `@sha256:` digest* is promoted dev → test →
prod. No tier rebuilds, so test validates the exact bytes prod will run. Pinning
`idc-index` in [pyproject.toml](../pyproject.toml) makes a rebuild *deterministic*; promoting one
digest makes rebuilds *unnecessary* — a stronger guarantee.

### Coming from the CircleCI pipeline?

If you maintained the legacy `.circleci/config.yml`, here is the mental-model mapping. The two
things that move the most: **tier selection** and **the approval gate** both leave the pipeline
file — the first becomes the *trigger*, the second becomes an *Environment setting*.

| Legacy CircleCI | Here (GitHub Actions) |
|---|---|
| Branch name picks the tier (`idc-prod` / `idc-uat` / `idc-test` / `master`) | The **trigger** picks the tier: push `main` → dev, manual dispatch → test, `v*` tag → prod |
| Per-tier secrets as context env vars (`DEPLOYMENT_*_IDC_<TIER>`) | Per-tier **Environment** Secrets/Variables (`GCP_PROJECT_ID`, `GCP_SA_KEY`, sizing) |
| A `type: approval` hold **job in `config.yml`** gates the deploy | A **Required reviewers** rule on the `prod` **Environment**, set in repo *Settings* — **not** in any YAML (see [Configuring the prod approval gate](#configuring-the-prod-approval-gate)) |
| Deploy auto-runs on every matching branch | dev auto-runs; test is manual; prod waits for reviewer approval |
| Rebuild per branch | Build once on `main`, promote the same digest |

| Workflow | Trigger | Result |
|---|---|---|
| [build-and-deploy-dev.yml](../.github/workflows/build-and-deploy-dev.yml) | push to `main` (image paths) or manual | build + push image, deploy to **dev** |
| [promote.yml](../.github/workflows/promote.yml) (dispatch) | manual, pick a build SHA | promote that digest to **test** |
| [promote.yml](../.github/workflows/promote.yml) (tag) | push a `v*` tag | promote the tagged commit's digest to **prod** (behind the required-reviewer gate) |
| [deploy.yml](../.github/workflows/deploy.yml) | reusable (`workflow_call`) | the shared deploy job the two callers invoke |

The image for a promoted SHA must already exist in the shared registry (i.e. it built on
`main`). Release commits normally bump the version in `pyproject.toml`, which is in the build
path filter, so they build; to promote a commit that didn't (e.g. a docs-only tag), run
`build-and-deploy-dev.yml`'s `workflow_dispatch` on it first.

### One-time setup

**1. Shared Artifact Registry (built once, read by all tiers).** Pick one project to host the
image repo — reusing the dev project is fine. Under *Settings → Secrets and variables → Actions*
set repo-level **Variables**:

- `BUILD_PROJECT_ID` — project hosting the shared registry
- `BUILD_REGION` — its region (default `us-central1`)
- `AR_REPO` — Artifact Registry repo name (default `idc`, created in step 1)

and a repo-level **Secret** `BUILD_SA_KEY` — a JSON key for a builder SA in `BUILD_PROJECT_ID`
with `roles/cloudbuild.builds.editor`, `roles/artifactregistry.writer`, `roles/storage.admin`,
`roles/serviceusage.serviceUsageConsumer`. If `BUILD_PROJECT_ID` is the dev project, this can be
the dev deployer key from step 2.

**2. One GitHub Environment per tier** (`dev`, `test`, `prod`). For each, add:

- Environment **Secrets**: `GCP_PROJECT_ID` and `GCP_SA_KEY` — a deployer SA in *that tier's*
  project (roles below; create with the snippet, once per project).
- Environment **Variables** (all optional — the workflow applies the same defaults as the manual
  deploy above if unset): `REGION`, `CPU`, `MEMORY`, `CONCURRENCY`, `MIN_INSTANCES`,
  `MAX_INSTANCES`, `DUCKDB_MEMORY_LIMIT`, `DUCKDB_THREADS`. Run prod hotter than dev by setting
  e.g. `MIN_INSTANCES=1` and a higher `MAX_INSTANCES` on `prod` only.
- On **`prod`**: add the **Required reviewers** approval gate — the step-by-step is in
  [Configuring the prod approval gate](#configuring-the-prod-approval-gate) below. (Optionally
  also restrict its deployment branches/tags to `v*`.)

Per-tier deployer SA (the same roles as the manual deploy — the tier no longer *builds*, but
`run.admin` + `iam.serviceAccountUser` deploy, and `artifactregistry.reader` on the shared
registry lets it resolve the digest; `serviceusage` lets it call APIs):

```bash
# Run once per tier, against that tier's project.
gcloud iam service-accounts create idc-api-deployer --display-name="IDC API deployer"
SA="idc-api-deployer@$PROJECT_ID.iam.gserviceaccount.com"
for role in roles/run.admin roles/iam.serviceAccountUser \
            roles/serviceusage.serviceUsageConsumer; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$SA" --role="$role"
done
gcloud iam service-accounts keys create sa-key.json --iam-account="$SA"
# Paste sa-key.json into the tier's GCP_SA_KEY environment secret, then delete the local file.
```

The **build** project's SA additionally needs `roles/artifactregistry.writer`,
`roles/cloudbuild.builds.editor`, and `roles/storage.admin` (the same build/push roles as the
manual deploy — `storage.admin` because `gcloud builds submit` calls `storage.buckets.get` on the
auto-created `<PROJECT_ID>_cloudbuild` bucket, which `roles/storage.objectAdmin` does *not*
cover).

**3. Cross-project registry read.** Each tier deploys the image *from the shared registry*, so on
`BUILD_PROJECT_ID`'s Artifact Registry grant, for every tier:

- the tier's **deployer SA** `roles/artifactregistry.reader` (to resolve the digest at deploy), and
- the tier's Cloud Run **service agent**
  (`service-<PROJECT_NUMBER>@serverless-robot-prod.iam.gserviceaccount.com`)
  `roles/artifactregistry.reader` (to pull the image at deploy / cold start).

```bash
# From BUILD_PROJECT_ID; repeat --member for each tier's deployer SA and service agent.
gcloud artifacts repositories add-iam-policy-binding "$AR_REPO" \
  --project "$BUILD_PROJECT_ID" --location "$BUILD_REGION" \
  --member="serviceAccount:idc-api-deployer@<TIER_PROJECT_ID>.iam.gserviceaccount.com" \
  --role=roles/artifactregistry.reader
gcloud artifacts repositories add-iam-policy-binding "$AR_REPO" \
  --project "$BUILD_PROJECT_ID" --location "$BUILD_REGION" \
  --member="serviceAccount:service-<TIER_PROJECT_NUMBER>@serverless-robot-prod.iam.gserviceaccount.com" \
  --role=roles/artifactregistry.reader
```

> **Max-isolation variant.** If org policy forbids cross-project image pulls, drop the grants in
> step 3 and instead have the promote job copy the digest into each tier's own registry
> (`gcloud artifacts docker images copy SRC@sha256:… DST@sha256:…` — same bytes) before
> deploying from there. More IAM and a copy step, but no shared-registry dependency at runtime.

IAM bindings can take a minute or two to propagate — if a read/`buckets.get` fails right after
granting, wait ~60s and retry before assuming the role is wrong.

### Configuring the prod approval gate

**This is the piece with no `.yml` equivalent — it lives entirely in repo Settings.** In CircleCI
you gated a deploy by adding a `type: approval` hold *job* to `config.yml`. GitHub Actions works
the other way round: the gate is a property of the **Environment**, and the workflow only *opts
in* by naming that environment. There is deliberately **no way to require reviewers from the
workflow file** — so a pull request can't weaken it. All [deploy.yml](../.github/workflows/deploy.yml)
does is declare `environment: prod` on its deploy job; the rule itself you set here, once:

1. Repo → **Settings → Environments**. Create an environment named exactly **`prod`** if it
   doesn't exist (the name must match what [promote.yml](../.github/workflows/promote.yml) passes
   on a `v*` tag).
2. Tick **Required reviewers** and add the users/teams allowed to approve prod deploys (up to 6).
   Optionally also tick **Prevent self-review** so the person who cut the tag can't approve their
   own deploy.
3. *(Optional)* Under **Deployment branches and tags** choose **Selected** and add the `v*`
   pattern, so only tag-triggered runs can ever target `prod`.
4. **Save protection rules.**

**What this looks like at deploy time.** Pushing a `v*` tag starts `promote.yml`: the `resolve`
job runs, then the reusable deploy job — bound to `prod` — **pauses before any deploy step**, in a
*"Waiting — review required"* state. A designated reviewer opens the run in the **Actions** tab,
clicks **Review deployments → prod → Approve and deploy** (or Reject). Only on approval do the
digest-resolve and `gcloud run deploy` steps run — and the `prod` secrets aren't exposed to the
job until then either. dev and test have no such rule, so they deploy without a pause. GitHub also
records who approved each prod deployment under the repo's **Deployments** view.

> ⚠️ **The gate is opt-in and off by default.** If you skip this — e.g. the `prod` environment
> exists but has no Required-reviewers rule — `environment: prod` still resolves and the prod
> deploy runs **unattended**. The YAML cannot enforce the gate; only this setting does. (Required
> reviewers are free on **public** repos, which this is; on private/internal repos they need
> GitHub Pro/Team/Enterprise.)

> **Long-lived keys vs WIF.** These workflows authenticate with JSON service-account keys
> (`google-github-actions/auth@v3` + `credentials_json`). To avoid long-lived keys, swap each
> `auth` step for Workload Identity Federation — the roles above are unchanged; only the `auth`
> step's inputs differ.
