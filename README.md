# Event-Driven Devin Vulnerability Remediation Control Plane

Thin control plane that points Devin at a target GitHub repository, has it hunt for real, code-grounded security vulnerabilities, and drives each confirmed finding through a governed remediation → PR → independent verification loop. Default target: `C0smicCrush/superset-remediation`. The short version: `the control plane governs intake, ordering, status, and guardrails; Devin hunts the vulnerabilities and does the engineering work to fix them`.

**This is the vulnerability instantiation of a more general pattern.** The hunt/remediate/verify loop, the Devin-files-the-tracked-artifact-itself convention, the FIFO queue keyed on `family_key`, the separate-session verification with silence-after-verdict, and the `rejected_findings` audit trail are all domain-agnostic primitives. Swap the `discovery` prompt in `config/prompts.yaml` and the `vuln:*` / `finding:*` label scheme, and the same scaffolding governs agent-created work for performance regressions, flaky-test triage, dead-code removal, dependency hygiene, dataset-quality audits, or any other bounded engineering task where you want a producing agent, a remediating agent, and an independent verifying agent with no human in the middle. See [Generalizing Beyond Vulnerabilities](#generalizing-beyond-vulnerabilities).

Discovery is explicitly posture-d as a security researcher reading source code, not as a wrapper around `npm audit` / `pip-audit`. Scanner output is background context; every accepted finding must be grounded in a specific call site (file, line, source → sink). Raw advisories with no reachable code path are rejected with an audit trail rather than promoted into tracked issues. See `config/prompts.yaml` (the `discovery` template) for the full contract.

## Reviewer Quickstart

Four ways to exercise this project, ordered from least to most setup. `GH_TOKEN` alone gets you the first two; Devin credentials unlock the full hunt-and-remediate loop; deployed AWS unlocks the webhook path.

| Path | What it shows | Requires |
| --- | --- | --- |
| **A — Dashboard-only** | Boot the stack and view the live dashboard against `superset-remediation` (tracked issues, PR rollups, conversion, daily activity) pulled from live GitHub + Devin APIs. | `GH_TOKEN` only |
| **B-lite — `/manual` creates a real GitHub issue** | `curl /manual` → intake → worker → `ensure_tracking_issue` opens a real labeled issue on `superset-remediation`. Devin launch step errors cleanly (expected without keys). Use this when you already know the vulnerability and just want to kick off remediation for it. | `GH_TOKEN` only |
| **B-vuln — Full hunt-and-remediate loop** | `curl /vuln-trigger` → bounded Devin vulnerability hunt against `superset-remediation` → accepted findings are filed as labeled tracked issues → each issue flows into the normal intake → worker → Devin remediation → PR path → poller fires a verification session once the PR appears → dashboard tracks every stage. This is the flagship demo. | `GH_TOKEN` + `DEVIN_API_KEY` + `DEVIN_ORG_ID` |
| **C — Webhook-driven (hosted)** | Label a GitHub issue `devin-remediate` → webhook → intake → Devin. Shown in the Loom; requires the AWS stack deployed and the webhook pointed at your Function URL. Also the path that scheduled EventBridge discovery runs feed into in production. | Deployed stack + admin on target repo |

### Path A — Dashboard-only

```bash
cp .env.example .env           # set GH_TOKEN only
make docker-up                  # builds + starts intake, worker, poller, dashboard
open http://localhost:8001     # dashboard
open http://localhost:8000/health
```

The dashboard reads live state from GitHub the moment it boots: tracked-issue counts, PR merge ratio, daily activity, Devin session history. No prior metrics snapshot needed.

### Path B-lite — `/manual` creates a real GitHub issue (PAT only)

Use this path when you already know the vulnerability and just want to exercise the intake → worker → GitHub integration without generating Devin keys.

```bash
make docker-up
curl -sS -X POST "http://localhost:8000/manual" \
  -H "Content-Type: application/json" \
  --data @fixtures/manual.sample.json
make docker-logs                # watch intake → worker → ensure_tracking_issue
```

A new issue will appear on `C0smicCrush/superset-remediation` with labels `devin-remediate`, `security-remediation`, `manual-source`, `aws-event-driven`. The worker will then error on the Devin API call (expected — no keys). This is the simplest way to see intake, queueing, worker normalization, and GitHub integration without generating a Devin key.

### Path B-vuln — Full hunt-and-remediate loop (flagship)

This is the end-to-end demo: Devin hunts for real, code-grounded vulnerabilities in the target repo, files each confirmed finding as a labeled tracked issue, and then the same pipeline picks those issues up and drives each one to a remediated PR with an independent verification session.

Add `DEVIN_API_KEY` and `DEVIN_ORG_ID` to `.env` (generate from `app.devin.ai` → org settings), then:

```bash
make docker-up
curl -sS -X POST "http://localhost:8000/vuln-trigger" \
  -H "Content-Type: application/json" \
  --data @fixtures/vuln_trigger.sample.json
# or:    make docker-vuln-trigger
make docker-logs               # watch discovery → issue creation → intake → remediation
```

What happens, in order:

1. `/vuln-trigger` short-circuits intake and invokes the discovery handler directly. No queue traffic at this step — discovery is a producer, not a queued work item.
2. Devin runs a bounded hunt in `superset-remediation` using the `discovery` prompt in `config/prompts.yaml`. It acts as a security researcher reading the source (authz gaps, injection sinks, SSRF, path traversal, unsafe deserialization, XSS, weak crypto, secrets in code, etc.), not as a scanner-output post-processor. Scanners like `npm audit` / `pip-audit` can be consulted for context, but any finding must cite a specific vulnerable call site in the repo and a source → sink data flow. Raw advisories with no reachable call site are logged in `rejected_findings` with a reason and never become issues.
3. **Devin files the tracked issue itself**, using the GitHub access it already has through its configured GitHub integration. For each accepted finding, Devin dedupes against open issues on `superset-remediation` (by `finding:<slug>` label and by title), provisions the labels it needs (`security-remediation`, `devin-remediate`, `devin-discovered`, `vuln:<class>`, `finding:<slug>`), drafts the issue body with the required sections (Problem Statement, Discovery Evidence, Likely Touched Files, Suggested Validation, Scope Tier, Discovery Notes), and opens the issue via the GitHub API. The control plane does not file issues — if Devin can't open one (permissions, API error), it reports `issue_creation_status: failed` on the finding with a specific error string, and the control plane surfaces that verbatim instead of silently recovering.
4. Each new `devin-remediate` issue is then picked up by the normal pipeline (via the GitHub webhook in the hosted path, or by re-invoking `/manual` locally with the issue number). The worker launches a remediation Devin session, the poller launches a separate verification session once a PR appears, and the dashboard shows every stage live.

To exercise just step 1 (the hunt itself) without the rest of the loop:

```bash
make docker-discover           # same Devin call, runs once against the target repo
```

The response payload from `/vuln-trigger` is the discovery-handler result. You can see findings counts, the list of issues Devin opened (`issues_opened_by_devin`), ones it skipped as duplicates of already-open issues (`issues_skipped_as_duplicate`), any failures it hit while trying to open an issue (`issue_creation_failures`), and the full `rejected_findings` audit trail inline in the curl output.

### Path C — Webhook-driven

```bash
gh issue create \
  --repo C0smicCrush/superset-remediation \
  --title "Bug: example remediation request" \
  --body "Describe the bug, repro, and expected fix." \
  --label devin-remediate
```

This path only fires end-to-end when the webhook on the target repo points at a reachable `/github` URL (the deployed AWS Function URL in this project). Running the stack on localhost won't intercept it without a tunnel + webhook admin. The Loom walks through this flow on the deployed stack.

## Need More Detail?

Everything below the `---` is the extended README (runtime modes, deployment, dashboard, security, trade-offs, generalization). `ARCHITECTURE.md` has the full system design.

---

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

### On-demand vulnerability hunt: `/vuln-trigger`

This is the primary producer for the vulnerability-remediation loop. A POST to `/vuln-trigger` short-circuits intake and runs a bounded Devin hunt against the configured target repo. Findings that clear the confidence and dedupe bar are filed as labeled tracked issues, and those issues then feed the normal remediation pipeline.

```bash
curl -sS -X POST "http://localhost:8000/vuln-trigger" \
  -H "Content-Type: application/json" \
  --data @fixtures/vuln_trigger.sample.json
```

Body is optional. The only currently-honored field is `max_findings` (integer), which caps how many accepted findings Devin will open issues for. Everything else — target repo, Devin org, prompt — is driven by runtime settings. This is deliberate: the endpoint is a "go look" button, not a way to steer Devin toward a preconceived finding. Devin opens the tracked issue on the target repo itself at the end of the hunt; there is no separate issue-creation step on the control-plane side.

In hosted mode, the same producer also runs on an EventBridge schedule (see `lambda_discovery.py`), so the vulnerability hunt happens continuously without a human in the loop. `/vuln-trigger` is the on-demand version of that same job.

### Specific, already-known issue: `/manual`

Use this when you already have a vulnerability description and want to drive it straight into remediation without going through the hunt step.

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
├── lambda_{intake,worker,poller,discovery}.py   # AWS handlers
├── aws_runtime.py, common.py                    # shared runtime + helpers
├── config/                                      # prompts.yaml, test_tiers.json
├── scripts/                                     # local wrappers, dashboard, run_devin_discovery.py
├── dashboard/, infra/, terraform/               # UI, deploy script, IaC
├── fixtures/, tests/                            # sample payloads, unit tests
└── docker-compose.yml, Dockerfile, Makefile     # local dev
```

## Generalizing Beyond Vulnerabilities

**The vulnerability loop is one concrete instantiation; the surrounding scaffolding is domain-agnostic.** If you want Devin (or a comparable coding agent) to produce any kind of bounded, auditable work against a target repo — not just security fixes — you keep most of this repo unchanged and swap a small, well-marked surface.

What stays the same for any creation task:

- **The three-role split.** A producer session hunts/generates candidate work items, a remediator session does the engineering, and an independent verifier session second-opinions the result. Verifier runs in a separate Devin session so the remediator isn't the sole source of truth — applies equally to "does this patch close the XSS" and "does this PR actually speed up the hot path" or "does this refactor preserve behavior."
- **Agent-owned tracked artifacts.** Devin files the tracked GitHub issue itself (dedupe, labels, body layout, outcome reporting via `issue_creation_status`). The control plane never reaches into the GitHub issues API. This pattern generalizes verbatim — any producer agent can own issue creation for its own domain.
- **FIFO queueing by `family_key`.** Ordering is per logical work item, not global. Same key design works whether the family is `finding-<slug>`, `perf-regression-<route>`, `flaky-test-<id>`, or `refactor-<module>`.
- **Separate-session verification with silence-after-verdict.** Once the verifier lands any of its four terminal verdicts (`verified`, `partially_fixed`, `not_fixed`, `not_verified`), the poller stops narrating on the issue and PR. The four-verdict shape is generic; "did the claimed thing actually happen" is not vuln-specific.
- **Rejection audit trail as first-class output.** The producer emits `rejected_findings` with an enumerated reason for anything it considered and declined to promote. This is what prevents a speculative-findings blowup; it applies to any producer that might otherwise over-report.

What you replace to retarget the same stack:

- **`config/prompts.yaml`** — swap the `discovery` prompt for your domain (evidence bar, in/out-of-scope classes, required `confidence` leg semantics, rejection reasons). The remediation and verification prompts are already generic enough that only minor wording needs to change.
- **Labels and slugs** — `vuln:xss` / `finding:<slug>` / `security-remediation` become e.g. `perf:hot-path` / `regression:<slug>` / `performance`, enforced in the prompt.
- **Scope tiers in `config/test_tiers.json`** — the tier names are already advisory (`tier0_auto_dependency_patch` → e.g. `tier0_auto_trivial`), and the worker passes the chosen tier to Devin as policy context rather than gating on it.
- **Structured-output schema** in `scripts/common.py::discovery_output_schema` — the finding shape (evidence, confidence, issue-creation status, family key) is generic; the per-finding content block is what varies.

What stays vulnerability-shaped on purpose and should be replaced wholesale (not massaged) when retargeting: the `discovery` prompt's vulnerability-class taxonomy and source→sink evidence model, the `vuln:*` label convention, and the security-researcher posture language. The producer/remediator/verifier split, queue, poller, and dashboard are not "a security tool" — they are a **generic governed-agent control plane with a security instantiation shipped.**

## Key Trade-Offs

- The worker stays thin, which keeps governance simple but puts more responsibility on Devin prompts and validation contracts.
- FIFO buffering reduces overlapping remediation attempts but adds latency.
- The dashboard uses local metrics first and optionally enriches from live GitHub + Devin rather than depending on a separate analytics platform.
- `/manual` and `/linear` make demos easy but are not yet production-hardened ingress surfaces.

See `ARCHITECTURE.md` for implementation-oriented system design, lifecycle, queueing, observability, and known limitations.
