# Event-Driven Devin Remediation Control Plane

Thin control plane that turns engineering signals into governed Devin remediation runs against a target GitHub repository. Default target: `C0smicCrush/superset-remediation`.

## Quickstart (Docker)

1. Copy `.env.example` to `.env` and set `GH_TOKEN`, `DEVIN_API_KEY`, `DEVIN_ORG_ID`.
2. `make docker-up`
3. Check: intake `http://localhost:8000/health` · dashboard `http://localhost:8001`

## Truly Test It

Create a GitHub issue on the target repo **and add the `devin-remediate` label**. That is the real end-to-end path — intake accepts it, the worker launches Devin, and the dashboard reflects progress.

```bash
gh issue create \
  --repo C0smicCrush/superset-remediation \
  --title "Bug: example remediation request" \
  --body "Describe the bug, repro, and expected fix." \
  --label devin-remediate
```

## Need More Detail?

Everything below the `---` is the extended README. **Paste it into Cursor** and ask — it covers architecture, runtime modes, deployment, dashboard, security, and trade-offs.

---

## Expanded README

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

## Triggering Work

### Fastest local path: manual event

```bash
curl -sS -X POST "http://localhost:8000/manual" \
  -H "Content-Type: application/json" \
  --data @fixtures/manual.sample.json
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
