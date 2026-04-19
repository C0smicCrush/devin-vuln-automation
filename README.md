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
