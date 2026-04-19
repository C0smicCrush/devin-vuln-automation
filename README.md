# Event-Driven Devin Remediation Control Plane

`devin-vuln-automation` is a thin control plane for turning engineering signals into governed Devin remediation runs against a target GitHub repository.

The core idea is simple:

`the control plane manages intake, ordering, status, and safety; Devin does the engineering work`

The default target repo in this project is `C0smicCrush/superset-remediation`.

## What This Repo Does

This repo accepts incoming work, buffers it, launches Devin remediation sessions, launches separate verification sessions for PRs, and publishes state to GitHub plus a lightweight dashboard.

At a high level:

1. An event reaches intake through `/github`, `/manual`, or `/linear`.
2. Intake normalizes the raw payload and enqueues it.
3. The worker shapes the remediation work item, creates or links the tracked GitHub issue, and launches Devin.
4. The poller tracks session progress and launches a separate verification session when a PR appears.
5. Metrics are written to `metrics/latest.json` locally or `reports/latest.json` in S3, and the dashboard exposes a human-readable view.

## Core Components

- `lambda_intake.py`: ingress handler that parses and enqueues incoming events
- `lambda_worker.py`: work-item shaping, dedupe, concurrency checks, issue creation/linking, remediation launch
- `lambda_poller.py`: session tracking, GitHub status updates, verification launch, metrics snapshots
- `lambda_discovery.py`: bounded discovery producer that files GitHub issues for accepted findings
- `aws_runtime.py`: runtime selection, queue helpers, GitHub/Devin/AWS glue, metrics persistence
- `common.py`: shared prompt, schema, GitHub, Devin, and shaping helpers
- `scripts/local_intake_server.py`, `scripts/local_worker.py`, `scripts/local_poller.py`: local wrappers around the same core logic
- `scripts/dashboard_server.py`: local dashboard and analytics API

## Runtime Modes

This repo supports both local and AWS-backed execution.

Runtime selection comes from `aws_runtime.py`:

- `RUNTIME_BACKEND=local` forces local mode
- if `RUNTIME_BACKEND` is unset and `AWS_APP_SECRET_NAME` is set, the runtime defaults to AWS mode
- if both are unset, the runtime defaults to local mode

Local mode uses:

- a file-backed queue under `LOCAL_STATE_DIR`
- local metrics in `LOCAL_METRICS_DIR/latest.json`
- Docker Compose for orchestration

AWS mode uses:

- Lambda
- SQS FIFO
- S3 metrics snapshots
- Secrets Manager
- EventBridge scheduling

## Local Quickstart

### 1. Configure `.env`

```bash
cp .env.example .env
```

Set at least:

- `GH_TOKEN`
- `DEVIN_API_KEY`
- `DEVIN_ORG_ID`
- `TARGET_REPO_OWNER`
- `TARGET_REPO_NAME`

If you already use `gh`, this is enough for the GitHub token:

```bash
GH_TOKEN="$(gh auth token)"
```

### 2. Start the local control plane

```bash
make docker-up
```

or:

```bash
docker compose up --build
```

### 3. Check the local surfaces

- intake health: `http://localhost:8000/health`
- dashboard: `http://localhost:8001`
- dashboard API: `http://localhost:8001/api/metrics`

### 4. Smoke test the local stack

```bash
make docker-smoke
```

### 5. Watch logs

```bash
make docker-logs
```

## Triggering Work

### Fastest local path: manual event

```bash
curl -sS -X POST "http://localhost:8000/manual" \
  -H "Content-Type: application/json" \
  --data @fixtures/manual.sample.json
```

### Hosted path: labeled GitHub issue

If the AWS stack and webhook are deployed, the cleanest reviewer-facing flow is:

1. create an issue on the target repo
2. add `devin-remediate`
3. let GitHub send the webhook to `/github`
4. watch the issue, PR, and dashboard update

Example:

```bash
gh issue create \
  --repo C0smicCrush/superset-remediation \
  --title "Bug: example remediation request" \
  --body "Describe the bug, repro, and expected fix." \
  --label devin-remediate
```

### Comment-driven follow-up

Comments on a tracked issue or linked PR are first-class follow-up signals. They re-enter the same control plane and can trigger another bounded remediation pass.

If a work item should end as manual review only, remove `devin-remediate` before posting the final close-out comment. Otherwise a human close-out comment is itself another valid follow-up signal.

## Dashboard And Analytics

The dashboard is served by `scripts/dashboard_server.py` at `http://localhost:8001`.

It always shows:

- queue depth
- metrics snapshot state from `metrics/latest.json`
- recent sessions and issue rollups when they are present in the metrics artifact

When `GH_TOKEN` is configured, the dashboard also enriches itself from live GitHub state:

- tracked issue counts
- issue-to-PR conversion
- PR state rollups
- follow-up and iteration counts
- daily activity windows

When `DEVIN_API_KEY` and `DEVIN_ORG_ID` are configured, the dashboard can also query Devin project sessions for ACU-style analytics.

The API surface is:

- `GET /health`
- `GET /api/metrics`

## Testing

Run tests on the host:

```bash
make test
```

Run tests inside Docker:

```bash
make docker-test
```

Run bounded discovery locally:

```bash
make discover-devin
```

or in Docker:

```bash
make docker-discover
```

## Deployment

Deploy the AWS stack with:

```bash
make deploy-aws
```

The deploy flow provisions or updates:

- intake Lambda and Function URL
- worker Lambda
- poller Lambda
- discovery Lambda
- SQS FIFO queue and DLQ
- S3 metrics bucket
- Secrets Manager runtime secret
- EventBridge schedules

The Terraform directory is also present for infrastructure management and migration work:

- `terraform/`
- `terraform/README.md`

## Security Notes

Current behavior to be aware of:

- `/github` supports webhook signature verification through `GITHUB_WEBHOOK_SECRET`
- if `GITHUB_WEBHOOK_SECRET` is empty, GitHub payloads are accepted unsigned
- `/manual` and `/linear` accept direct JSON payloads and are best treated as operator/demo paths, not hardened public endpoints
- `LINEAR_WEBHOOK_SECRET` exists for future hardening, but the current `/linear` path does not verify signatures

## Repository Layout

```text
.
├── aws_runtime.py
├── common.py
├── lambda_intake.py
├── lambda_worker.py
├── lambda_poller.py
├── lambda_discovery.py
├── config/
├── dashboard/
├── infra/
├── terraform/
├── scripts/
│   ├── local_intake_server.py
│   ├── local_worker.py
│   ├── local_poller.py
│   ├── dashboard_server.py
│   └── run_devin_discovery.py
├── fixtures/
├── tests/
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── package.json
├── tsconfig.json
└── Makefile
```

## Key Trade-Offs

- The worker stays intentionally thin, which keeps governance simple but puts more responsibility on Devin prompts and validation contracts.
- FIFO buffering reduces overlapping remediation attempts but adds latency.
- The dashboard is intentionally lightweight: it uses local metrics first and optionally enriches from live GitHub and Devin data rather than depending on a separate analytics platform.
- Manual and Linear intake paths make demos easy, but they are not yet production-hardened ingress surfaces.

## More Detail

See `ARCHITECTURE.md` for the implementation-oriented system design, lifecycle details, queueing model, observability model, and known limitations.
# Event-Driven Devin Remediation for Superset

## TL;DR

This repo is a thin control plane for governed Devin remediation work against `C0smicCrush/superset-remediation`.

The design principle is:

`AWS or the local runtime manages workflow, ordering, and observability. Devin does the engineering work.`

Core flow:

1. An event reaches intake through `/github`, `/manual`, or `/linear`.
2. Intake normalizes the raw event and enqueues it.
3. The worker creates or links the tracked GitHub issue if needed, then launches Devin remediation.
4. If Devin opens a PR, the poller launches a separate verification session.
5. GitHub, the dashboard, and the metrics snapshot show the current state.

## Repositories

- App repo: `C0smicCrush/superset-remediation`
- Control plane: `C0smicCrush/devin-vuln-automation`

## Architecture

There is one downstream remediation pipeline:

`intake -> SQS or local queue -> worker -> Devin remediation -> Devin verification -> metrics and status updates`

The current implementation accepts three intake shapes:

- `/github`: webhook-driven GitHub issue and comment events
- `/manual`: direct operator or demo POSTs
- `/linear`: direct Linear-style JSON payloads

GitHub issues labeled `devin-remediate` are the primary tracked artifact surface, but they are not the only way work first enters the system. Manual and Linear inputs hit intake directly, then the worker creates or links the canonical tracking issue before launching Devin.

Human comments on a tracked issue or linked PR are first-class follow-up signals. They re-enter the same control plane and can trigger another bounded remediation pass.

If an issue is intended to end as manual review only, remove `devin-remediate` before posting the final close-out comment. Otherwise that human close-out comment is itself another valid follow-up signal.

## Real Repo Flow

When the AWS stack and GitHub webhook are deployed, the cleanest reviewer-facing path is:

1. Open an issue on `C0smicCrush/superset-remediation`.
2. Add the label `devin-remediate`.
3. GitHub sends the `issues` webhook to `/github`.
4. Intake enqueues the work item.
5. Worker launches a Devin remediation session.
6. Devin investigates, fixes, validates, and opens or updates a PR.
7. Poller launches a second Devin verification session for that PR.
8. GitHub comments, PR links, and metrics update as the work progresses.

## Recent Demo Outcomes

The current demo state includes both successful remediation runs and a manual-review stop:

- `apache/superset#39364` -> `superset-remediation#95` -> `PR #103` -> `verified`
- `apache/superset#39144` -> `superset-remediation#96` -> `PR #101` -> `verified`
- `apache/superset#39429` -> `superset-remediation#97` -> `PR #102` -> `verified`
- `apache/superset#39431` -> `superset-remediation#98` -> `PR #100` -> `verified`
- `apache/superset#39464` -> `superset-remediation#104` -> `manual_review`, closed with captured human design decisions and intentionally no PR

That last item is the example of the comment-driven human-in-the-loop path: Devin stopped, asked for decisions, the human answered on the issue, and the follow-up run closed out as design triage rather than pretending a giant SIP should be auto-implemented.

## Local Demo Flow

The local stack runs the same intake, worker, poller, and dashboard logic, but uses Docker containers plus file-backed state instead of AWS-managed services.

### 1. Configure `.env`

```bash
cp .env.example .env
```

Set at least:

- `GH_TOKEN`
- `DEVIN_API_KEY`
- `DEVIN_ORG_ID`
- `TARGET_REPO_OWNER=C0smicCrush`
- `TARGET_REPO_NAME=superset-remediation`

If you already use `gh`, this is enough for the GitHub token:

```bash
GH_TOKEN="$(gh auth token)"
```

### 2. Start the local control plane

```bash
docker compose up --build
```

Useful endpoints:

- intake health: `http://localhost:8000/health`
- dashboard: `http://localhost:8001`
- metrics JSON: `http://localhost:8001/api/metrics`

### 3. Run a quick smoke check

```bash
make docker-smoke
```

### 4. Watch the live system

```bash
make docker-logs
```

## Triggering Work Locally

### Fastest path: manual event

This is the easiest deterministic local demo:

```bash
curl -sS -X POST "http://localhost:8000/manual" \
  -H "Content-Type: application/json" \
  --data @fixtures/manual.sample.json
```

### Real issue path

If you want to demonstrate the GitHub issue workflow, create an issue on `superset-remediation` with `devin-remediate`:

```bash
gh issue create \
  --repo C0smicCrush/superset-remediation \
  --title "Bug: example remediation request" \
  --body "Describe the bug, repro, and expected fix." \
  --label devin-remediate \
  --label bug
```

How that behaves depends on where you are running:

- Deployed AWS stack: the GitHub webhook automatically hits intake and the pipeline starts.
- Local Docker stack: the easiest deterministic path is still `/manual`, but you can also replay GitHub-style webhook payloads to `/github`.

## Observability And Analytics

The dashboard at `http://localhost:8001` is the main demo surface. It reads the poller-written metrics snapshot plus local queue state and shows:

- queue depth
- active remediation and verification sessions
- completed, blocked, and failed session counts
- issue-to-PR rollups
- verification verdict counts
- direct links to GitHub issues, PRs, and Devin sessions

Metrics are stored as:

- local: `metrics/latest.json`
- hosted: S3 `reports/latest.json`

## Runtime Configuration

Runtime selection works like this:

- `RUNTIME_BACKEND=local` forces the local filesystem-backed runtime
- `AWS_APP_SECRET_NAME=<secret>` loads the AWS runtime settings from Secrets Manager
- if neither is set, the runtime defaults to local mode

Related runtime flags include:

- `DEVIN_BYPASS_APPROVAL`
- `DEVIN_VERIFICATION_BYPASS_APPROVAL`
- `MAX_ACTIVE_REMEDIATIONS`
- `MAX_DISCOVERY_FINDINGS`

## Intake Security Notes

- `/github` supports webhook signature verification through `GITHUB_WEBHOOK_SECRET`
- if `GITHUB_WEBHOOK_SECRET` is empty, GitHub payloads are accepted unsigned in local/demo mode
- `/manual` and `/linear` currently accept direct JSON and are best treated as operator or demo paths, not production-hardened public endpoints
- `LINEAR_WEBHOOK_SECRET` is reserved for future hardening, but the current `/linear` path does not verify signatures

## Deployment

Deploy the hosted AWS version with:

```bash
make deploy-aws
```

That script provisions intake, worker, poller, discovery, queues, metrics storage, and the webhook-facing intake URL.

## Repository Layout

```text
.
├── aws_runtime.py
├── common.py
├── lambda_intake.py
├── lambda_worker.py
├── lambda_poller.py
├── lambda_discovery.py
├── config/
├── dashboard/
├── infra/
├── terraform/
├── scripts/
│   ├── common.py
│   ├── local_intake_server.py
│   ├── local_worker.py
│   ├── local_poller.py
│   ├── dashboard_server.py
│   └── run_devin_discovery.py
├── fixtures/
├── tests/
├── Dockerfile
├── docker-compose.yml
├── package.json
├── package-lock.json
├── requirements.txt
├── tsconfig.json
└── Makefile
```

## Notes

- The worker is intentionally thin. It routes, dedupes, shapes the work item, and launches Devin; it does not try to out-reason Devin.
- Verification is a separate Devin session, not a self-reported success flag.
- Discovery exists as an issue producer, but the clearest reviewer-facing story is still: open a labeled issue and watch the pipeline run.

For deeper design detail, trade-offs, and component responsibilities, see `ARCHITECTURE.md`.
