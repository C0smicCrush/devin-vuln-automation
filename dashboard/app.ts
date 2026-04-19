type RepoLinks = {
  owner: string;
  name: string;
  url: string;
  issues_url: string;
  pulls_url: string;
};

type PullRequestLink = {
  url: string;
  number: number | null;
};

type SessionView = {
  phase: string;
  issue_number: number | null;
  issue_url: string | null;
  session_id: string | null;
  devin_url: string | null;
  status: string;
  status_detail: string;
  verdict: string;
  summary: string;
  pull_requests: PullRequestLink[];
};

type IssueRollupView = {
  issue_number: number | null;
  issue_url: string | null;
  title?: string;
  state?: string;
  remediation_sessions: number;
  verification_sessions: number;
  latest_verdict: string;
  verified: boolean;
  human_info_requested: boolean;
  human_comment_followups: number;
  latest_summary?: string;
  pull_requests?: PullRequestLink[];
  sessions?: SessionView[];
};

type DailyActivityPoint = {
  date: string;
  issues_created: number;
  issues_closed: number;
  prs_opened: number;
  prs_merged: number;
  prs_closed_unmerged: number;
};

type DashboardPayload = {
  repo: RepoLinks;
  generated_at: number | null;
  queue_depth: number;
  overview: Record<string, number>;
  verification_verdict_counts: Record<string, number>;
  repo_analytics: {
    tracked_issues_total: number;
    tracked_issues_open: number;
    tracked_issues_closed: number;
    issues_with_pr: number;
    issues_without_pr: number;
    issue_to_pr_conversion_rate: number | null;
    linked_prs_total: number;
    linked_prs_open: number;
    linked_prs_merged: number;
    linked_prs_closed_unmerged: number;
    attempted_issues_total: number;
    avg_remediation_iterations: number | null;
    avg_total_iterations: number | null;
    avg_human_followups: number | null;
    manual_intervention_rate: number | null;
    verified_issue_rate: number | null;
    avg_issue_to_first_pr_seconds: number | null;
    avg_issue_to_resolution_seconds: number | null;
    tracked_devin_sessions_total: number;
    total_devin_acus: number | null;
    remediation_devin_acus: number | null;
    verification_devin_acus: number | null;
    computed_from_devin: boolean;
    daily_activity: DailyActivityPoint[];
    computed_from_github: boolean;
    error: string;
  };
  recent_sessions: SessionView[];
  issue_rollups: IssueRollupView[];
};

type RepoAnalyticsCardKey =
  | "tracked_issues_total"
  | "issues_with_pr"
  | "issue_to_pr_conversion_rate"
  | "linked_prs_merged"
  | "avg_remediation_iterations"
  | "avg_total_iterations"
  | "manual_intervention_rate"
  | "avg_human_followups"
  | "avg_issue_to_first_pr_seconds"
  | "avg_issue_to_resolution_seconds"
  | "total_devin_acus"
  | "remediation_devin_acus"
  | "verification_devin_acus";

const OVERVIEW_KEYS: Array<[keyof DashboardPayload["overview"] | "queue_depth", string]> = [
  ["queue_depth", "Queued Work Items"],
  ["active_sessions", "Active Sessions"],
  ["completed_sessions", "Completed Sessions"],
  ["failed_sessions", "Failed Sessions"],
  ["blocked_sessions", "Open Unresolved Workflows"],
  ["pull_requests_opened", "PRs Opened"],
  ["tracked_items_total", "Tracked Items"],
  ["tracked_items_verified", "Verified Items"],
  ["tracked_items_verified_first_pass", "Verified First Pass"],
  ["tracked_items_needing_human_followup", "Needed Human Follow-Up"],
];

const VERDICT_KEYS: Array<[string, string]> = [
  ["verified", "Verified"],
  ["partially_fixed", "Partially Fixed"],
  ["not_fixed", "Not Fixed"],
  ["not_verified", "Not Verified"],
];

const VERDICT_COLORS: Record<string, string> = {
  verified: "#34d399",
  partially_fixed: "#fbbf24",
  not_fixed: "#f87171",
  not_verified: "#60a5fa",
};

const REPO_ANALYTICS_KEYS: Array<[RepoAnalyticsCardKey, string]> = [
  ["tracked_issues_total", "Tracked GitHub Issues"],
  ["issues_with_pr", "Issues With PR"],
  ["issue_to_pr_conversion_rate", "Issue -> PR Conversion"],
  ["linked_prs_merged", "Merged Linked PRs"],
  ["avg_remediation_iterations", "Avg Canonical Remediation Phases"],
  ["avg_total_iterations", "Avg Canonical Total Phases"],
  ["manual_intervention_rate", "Manual Intervention Rate"],
  ["avg_human_followups", "Avg Human Follow-Ups"],
  ["avg_issue_to_first_pr_seconds", "Avg Time To First PR"],
  ["avg_issue_to_resolution_seconds", "Avg Time To Resolution"],
  ["total_devin_acus", "Total Devin ACUs"],
  ["remediation_devin_acus", "Remediation ACUs"],
  ["verification_devin_acus", "Verification ACUs"],
];

const DAILY_ACTIVITY_SERIES: Array<
  [keyof DailyActivityPoint, string, string]
> = [
  ["issues_created", "Issues Created", "#60a5fa"],
  ["issues_closed", "Issues Closed", "#34d399"],
  ["prs_opened", "PRs Opened", "#fbbf24"],
  ["prs_merged", "PRs Merged", "#a78bfa"],
  ["prs_closed_unmerged", "PRs Closed Unmerged", "#f87171"],
];

let activeVerdictFilter = "all";
let latestIssueRollups: IssueRollupView[] = [];

function byId<T extends HTMLElement>(id: string): T {
  const element = document.getElementById(id);
  if (!element) {
    throw new Error(`Missing element: ${id}`);
  }
  return element as T;
}

function formatTimestamp(epochSeconds: number | null): string {
  if (!epochSeconds) {
    return "No metrics yet";
  }
  return new Date(epochSeconds * 1000).toLocaleString();
}

function formatDuration(seconds: number | null): string {
  if (seconds === null || Number.isNaN(seconds)) {
    return "n/a";
  }
  const hours = seconds / 3600;
  if (hours >= 24) {
    return `${(hours / 24).toFixed(1)}d`;
  }
  if (hours >= 1) {
    return `${hours.toFixed(1)}h`;
  }
  return `${Math.round(seconds / 60)}m`;
}

function formatMetricValue(
  key: keyof DashboardPayload["repo_analytics"],
  value: string | number | boolean | null,
): string {
  if (typeof value === "string") {
    return value || "n/a";
  }
  if (typeof value === "boolean") {
    return value ? "yes" : "no";
  }
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "n/a";
  }
  if (key.endsWith("_rate")) {
    return `${(value * 100).toFixed(1)}%`;
  }
  if (key.endsWith("_seconds")) {
    return formatDuration(value);
  }
  if (key.endsWith("_acus")) {
    return `${value.toFixed(2)} ACUs`;
  }
  if (key.startsWith("avg_")) {
    return value.toFixed(2);
  }
  return String(value);
}

function makeCard(label: string, value: string | number): HTMLElement {
  const article = document.createElement("article");
  article.className = "card";

  const number = document.createElement("div");
  number.className = "card-value";
  number.textContent = String(value);

  const title = document.createElement("div");
  title.className = "card-label";
  title.textContent = label;

  article.append(number, title);
  return article;
}

function setLinks(repo: RepoLinks): void {
  byId("repo-name").textContent = `${repo.owner}/${repo.name}`;
  (byId("repo-link") as HTMLAnchorElement).href = repo.url;
  (byId("issues-link") as HTMLAnchorElement).href = repo.issues_url;
  (byId("pulls-link") as HTMLAnchorElement).href = repo.pulls_url;
}

function renderOverview(payload: DashboardPayload): void {
  const cards = byId("overview-cards");
  cards.innerHTML = "";

  OVERVIEW_KEYS.forEach(([key, label]) => {
    const value = key === "queue_depth" ? payload.queue_depth : payload.overview[key] ?? 0;
    cards.appendChild(makeCard(label, value));
  });
}

function renderVerdicts(counts: Record<string, number>): void {
  const cards = byId("verdict-cards");
  cards.innerHTML = "";

  VERDICT_KEYS.forEach(([key, label]) => {
    cards.appendChild(makeCard(label, counts[key] ?? 0));
  });
}

function polarToCartesian(cx: number, cy: number, radius: number, angleInDegrees: number) {
  const angleInRadians = ((angleInDegrees - 90) * Math.PI) / 180.0;
  return {
    x: cx + radius * Math.cos(angleInRadians),
    y: cy + radius * Math.sin(angleInRadians),
  };
}

function describeArc(
  cx: number,
  cy: number,
  radius: number,
  startAngle: number,
  endAngle: number,
): string {
  const start = polarToCartesian(cx, cy, radius, endAngle);
  const end = polarToCartesian(cx, cy, radius, startAngle);
  const largeArcFlag = endAngle - startAngle <= 180 ? "0" : "1";
  return ["M", start.x, start.y, "A", radius, radius, 0, largeArcFlag, 0, end.x, end.y].join(" ");
}

function verdictLabel(key: string): string {
  return VERDICT_KEYS.find(([candidate]) => candidate === key)?.[1] ?? key;
}

function renderVerdictDonut(counts: Record<string, number>): void {
  const donut = byId("verdict-donut");
  const legend = byId("verdict-donut-legend");
  donut.innerHTML = "";
  legend.innerHTML = "";

  const entries = VERDICT_KEYS.map(([key, label]) => ({
    key,
    label,
    value: counts[key] ?? 0,
    color: VERDICT_COLORS[key] ?? "#94a3b8",
  })).filter(entry => entry.value > 0);

  const total = entries.reduce((sum, entry) => sum + entry.value, 0);
  if (!total) {
    donut.innerHTML = '<div class="empty-blocks">No verdict data yet.</div>';
    return;
  }

  const svgNS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(svgNS, "svg");
  svg.setAttribute("viewBox", "0 0 220 220");

  let currentAngle = 0;
  entries.forEach(entry => {
    const arcAngle = (entry.value / total) * 360;
    const circle = document.createElementNS(svgNS, "circle");
    circle.setAttribute("cx", "110");
    circle.setAttribute("cy", "110");
    circle.setAttribute("r", "74");
    circle.setAttribute("fill", "none");
    circle.setAttribute("stroke", entry.color);
    circle.setAttribute("stroke-width", "28");
    circle.setAttribute("stroke-linecap", "butt");
    circle.setAttribute("pathLength", "360");
    circle.setAttribute("stroke-dasharray", `${arcAngle} ${360 - arcAngle}`);
    circle.setAttribute("transform", `rotate(${currentAngle - 90} 110 110)`);
    circle.setAttribute(
      "class",
      `verdict-segment${activeVerdictFilter === "all" || activeVerdictFilter === entry.key ? " active" : " inactive"}`,
    );
    circle.addEventListener("click", () => {
      activeVerdictFilter = entry.key;
      renderVerdictDonut(counts);
      renderVerdictIssueBlocks(latestIssueRollups);
    });
    svg.appendChild(circle);
    currentAngle += arcAngle;
  });

  const inner = document.createElementNS(svgNS, "circle");
  inner.setAttribute("cx", "110");
  inner.setAttribute("cy", "110");
  inner.setAttribute("r", "50");
  inner.setAttribute("fill", "#0f172a");
  svg.appendChild(inner);

  const centerValue = document.createElementNS(svgNS, "text");
  centerValue.setAttribute("x", "110");
  centerValue.setAttribute("y", "104");
  centerValue.setAttribute("class", "verdict-center-value");
  centerValue.textContent = String(total);
  svg.appendChild(centerValue);

  const centerLabel = document.createElementNS(svgNS, "text");
  centerLabel.setAttribute("x", "110");
  centerLabel.setAttribute("y", "126");
  centerLabel.setAttribute("class", "verdict-center-label");
  centerLabel.textContent = activeVerdictFilter === "all" ? "issues" : verdictLabel(activeVerdictFilter);
  svg.appendChild(centerLabel);

  donut.appendChild(svg);

  entries.forEach(entry => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `verdict-legend-button${activeVerdictFilter === entry.key ? " active" : ""}`;
    button.innerHTML = `
      <span class="verdict-legend-left">
        <span class="legend-swatch" style="background:${entry.color}"></span>
        <span>${entry.label}</span>
      </span>
      <strong>${entry.value}</strong>
    `;
    button.addEventListener("click", () => {
      activeVerdictFilter = entry.key;
      renderVerdictDonut(counts);
      renderVerdictIssueBlocks(latestIssueRollups);
    });
    legend.appendChild(button);
  });
}

function renderRepoAnalytics(analytics: DashboardPayload["repo_analytics"]): void {
  const cards = byId("repo-analytics-cards");
  const status = byId("github-analytics-status");
  cards.innerHTML = "";

  if (!analytics.computed_from_github) {
    status.textContent = analytics.error
      ? `GitHub analytics unavailable: ${analytics.error}`
      : "GitHub analytics are disabled until `GH_TOKEN` is present in the dashboard environment.";
    return;
  }

  status.textContent = analytics.computed_from_devin
    ? `Using live GitHub issue/pull state for ${analytics.tracked_issues_total} tracked issues and live Devin ACU usage for ${analytics.tracked_devin_sessions_total} sessions.`
    : `Using live GitHub issue and pull request state for ${analytics.tracked_issues_total} tracked issues.`;
  REPO_ANALYTICS_KEYS.forEach(([key, label]) => {
    cards.appendChild(makeCard(label, formatMetricValue(key, analytics[key])));
  });
}

function renderDailyActivity(points: DailyActivityPoint[]): void {
  const legend = byId("daily-activity-legend");
  const chart = byId("daily-activity-chart");
  legend.innerHTML = "";
  chart.innerHTML = "";

  DAILY_ACTIVITY_SERIES.forEach(([, label, color]) => {
    const item = document.createElement("div");
    item.className = "legend-item";
    item.innerHTML = `<span class="legend-swatch" style="background:${color}"></span><span>${label}</span>`;
    legend.appendChild(item);
  });

  if (!points.length) {
    const empty = document.createElement("div");
    empty.className = "timeline-empty";
    empty.textContent = "No day-by-day GitHub activity yet.";
    chart.appendChild(empty);
    return;
  }

  const maxValue = Math.max(
    1,
    ...points.flatMap(point => DAILY_ACTIVITY_SERIES.map(([key]) => point[key] as number)),
  );

  points.forEach(point => {
    const day = document.createElement("div");
    day.className = "timeline-day";

    const bars = document.createElement("div");
    bars.className = "timeline-bars";

    DAILY_ACTIVITY_SERIES.forEach(([key, label, color]) => {
      const value = point[key] as number;
      const bar = document.createElement("div");
      bar.className = "timeline-bar";
      bar.style.background = color;
      bar.style.height = `${Math.max((value / maxValue) * 100, value > 0 ? 8 : 4)}%`;
      bar.title = `${point.date} · ${label}: ${value}`;
      bars.appendChild(bar);
    });

    const dateLabel = document.createElement("div");
    dateLabel.className = "timeline-label";
    dateLabel.textContent = new Date(`${point.date}T00:00:00Z`).toLocaleDateString(undefined, {
      month: "short",
      day: "numeric",
    });

    day.append(bars, dateLabel);
    chart.appendChild(day);
  });
}

function normalizeIssueVerdict(issue: IssueRollupView): string {
  return issue.latest_verdict || (issue.verified ? "verified" : "not_verified");
}

function truncateText(value: string, maxLength = 220): string {
  if (value.length <= maxLength) {
    return value;
  }
  return `${value.slice(0, maxLength - 1)}...`;
}

function displaySessionState(value: string): string {
  return value === "waiting_for_user" ? "running" : value;
}

function formatSessionBadge(status: string, statusDetail: string): string {
  const normalizedStatus = displaySessionState(status);
  const normalizedDetail = displaySessionState(statusDetail);
  if (!normalizedDetail || normalizedDetail === normalizedStatus) {
    return normalizedStatus;
  }
  return `${normalizedStatus} · ${normalizedDetail}`;
}

function isClosedVerifiedIssue(issue: IssueRollupView): boolean {
  return issue.state === "closed" && normalizeIssueVerdict(issue) === "verified";
}

function issueSessionBadge(issue: IssueRollupView, session: SessionView): string {
  if (isClosedVerifiedIssue(issue)) {
    return "completed";
  }
  return formatSessionBadge(session.status, session.status_detail);
}

function renderVerdictIssueBlocks(issues: IssueRollupView[]): void {
  const container = byId("verdict-issue-blocks");
  const title = byId("verdict-filter-title");
  container.innerHTML = "";

  const filtered = activeVerdictFilter === "all"
    ? issues
    : issues.filter(issue => normalizeIssueVerdict(issue) === activeVerdictFilter);

  title.textContent =
    activeVerdictFilter === "all"
      ? "All Tracked Issues"
      : `${verdictLabel(activeVerdictFilter)} Issues`;

  if (!filtered.length) {
    container.innerHTML = '<div class="empty-blocks">No tracked issues match this verdict.</div>';
    return;
  }

  filtered.forEach(issue => {
    const block = document.createElement("article");
    block.className = "issue-block";
    const prLinks = issue.pull_requests || [];
    const sessions = issue.sessions || [];
    const remediationSessions = sessions.filter(session => session.phase === "remediation");
    const earliestRemediation = remediationSessions[remediationSessions.length - 1];
    const latestRemediation = remediationSessions[0];
    const latestVerification = sessions.find(session => session.phase === "verification");
    const flowBlocks: string[] = [];

    if (earliestRemediation) {
      flowBlocks.push(`
        <div class="flow-block">
          <div class="flow-block-step">1</div>
          <div>
            <div class="flow-block-title">Remediation Started</div>
            <div class="flow-block-text">${truncateText(earliestRemediation.summary || "Remediation run started.")}</div>
            <div class="session-badges">
              <span class="badge">${issueSessionBadge(issue, earliestRemediation)}</span>
              ${earliestRemediation.devin_url ? `<a class="badge" href="${earliestRemediation.devin_url}" target="_blank" rel="noreferrer">Devin</a>` : ""}
            </div>
          </div>
        </div>
      `);
    }

    if (prLinks.length) {
      flowBlocks.push(`
        <div class="flow-block">
          <div class="flow-block-step">${flowBlocks.length + 1}</div>
          <div>
            <div class="flow-block-title">PR Opened</div>
            <div class="flow-block-text">${prLinks.map(pr => `Linked ${pr.number ? `PR #${pr.number}` : "PR"}`).join(" · ")}</div>
            <div class="session-badges">
              ${prLinks.map(pr => `<a class="badge" href="${pr.url}" target="_blank" rel="noreferrer">PR #${pr.number ?? "?"}</a>`).join("")}
            </div>
          </div>
        </div>
      `);
    }

    if (
      issue.human_comment_followups > 0 &&
      latestRemediation &&
      earliestRemediation &&
      latestRemediation.session_id !== earliestRemediation.session_id
    ) {
      flowBlocks.push(`
        <div class="flow-block">
          <div class="flow-block-step">${flowBlocks.length + 1}</div>
          <div>
            <div class="flow-block-title">Human Follow-Up</div>
            <div class="flow-block-text">${truncateText(latestRemediation.summary || "A human follow-up comment changed the workflow direction.")}</div>
            <div class="session-badges">
              <span class="badge">Human comments: ${issue.human_comment_followups}</span>
              ${latestRemediation.devin_url ? `<a class="badge" href="${latestRemediation.devin_url}" target="_blank" rel="noreferrer">Devin</a>` : ""}
            </div>
          </div>
        </div>
      `);
    }

    if (issue.human_info_requested) {
      flowBlocks.push(`
        <div class="flow-block">
          <div class="flow-block-step">${flowBlocks.length + 1}</div>
          <div>
            <div class="flow-block-title">Manual Input Needed</div>
            <div class="flow-block-text">${truncateText(issue.latest_summary || "Workflow is waiting on a human decision or comment.")}</div>
          </div>
        </div>
      `);
    }

    if (latestVerification) {
      flowBlocks.push(`
        <div class="flow-block">
          <div class="flow-block-step">${flowBlocks.length + 1}</div>
          <div>
            <div class="flow-block-title">Verification</div>
            <div class="flow-block-text">${truncateText(latestVerification.verdict || latestVerification.summary || "Verification ran.")}</div>
            <div class="session-badges">
              <span class="badge">${issueSessionBadge(issue, latestVerification)}</span>
              ${latestVerification.devin_url ? `<a class="badge" href="${latestVerification.devin_url}" target="_blank" rel="noreferrer">Devin</a>` : ""}
            </div>
          </div>
        </div>
      `);
    }

    flowBlocks.push(`
      <div class="flow-block">
        <div class="flow-block-step">${flowBlocks.length + 1}</div>
        <div>
          <div class="flow-block-title">Current Outcome</div>
          <div class="flow-block-text">${truncateText(issue.latest_summary || verdictLabel(normalizeIssueVerdict(issue)))}</div>
          <div class="session-badges">
            <span class="badge" style="border-color:${VERDICT_COLORS[normalizeIssueVerdict(issue)]};">${verdictLabel(normalizeIssueVerdict(issue))}</span>
          </div>
        </div>
      </div>
    `);

    block.innerHTML = `
      <div class="issue-block-header">
        <div>
          <div class="issue-block-title">
            ${issue.issue_url && issue.issue_number ? `<a href="${issue.issue_url}" target="_blank" rel="noreferrer">#${issue.issue_number}</a>` : "Issue"}
            ${issue.title ? ` · ${issue.title}` : ""}
          </div>
          <div class="issue-block-meta">${issue.state ?? ""}</div>
        </div>
        <span class="badge" style="border-color:${VERDICT_COLORS[normalizeIssueVerdict(issue)]};">${verdictLabel(normalizeIssueVerdict(issue))}</span>
      </div>
      <div class="issue-badges">
        <span class="badge">Current verdict: ${verdictLabel(normalizeIssueVerdict(issue))}</span>
        ${issue.human_info_requested ? '<span class="badge">Waiting on human now</span>' : ""}
        ${issue.human_comment_followups > 0 ? `<span class="badge">Human comments: ${issue.human_comment_followups}</span>` : ""}
      </div>
      <div class="flow-blocks">${flowBlocks.join("")}</div>
    `;
    container.appendChild(block);
  });
}

function renderIssues(issues: IssueRollupView[]): void {
  const body = byId("issues-body");
  body.innerHTML = "";

  if (!issues.length) {
    body.innerHTML = '<tr><td colspan="5">No tracked issue rollups yet.</td></tr>';
    return;
  }

  issues.forEach((issue) => {
    const row = document.createElement("tr");

    const issueCell = document.createElement("td");
    if (issue.issue_url && issue.issue_number) {
      issueCell.innerHTML = `<a href="${issue.issue_url}" target="_blank" rel="noreferrer">#${issue.issue_number}</a>`;
    } else {
      issueCell.textContent = "n/a";
    }

    const remediationCell = document.createElement("td");
    remediationCell.textContent = String(issue.remediation_sessions);

    const verificationCell = document.createElement("td");
    verificationCell.textContent = String(issue.verification_sessions);

    const verdictCell = document.createElement("td");
    verdictCell.textContent = issue.latest_verdict || (issue.verified ? "verified" : "n/a");

    const followupCell = document.createElement("td");
    followupCell.textContent = String(issue.human_comment_followups);
    if (issue.human_info_requested) {
      const detail = document.createElement("div");
      detail.className = "muted";
      detail.textContent = "human info requested";
      followupCell.appendChild(detail);
    }

    row.append(issueCell, remediationCell, verificationCell, verdictCell, followupCell);
    body.appendChild(row);
  });
}

async function refresh(): Promise<void> {
  const response = await fetch("/api/metrics");
  if (!response.ok) {
    throw new Error(`Dashboard metrics request failed: ${response.status}`);
  }

  const payload = (await response.json()) as DashboardPayload;
  latestIssueRollups = payload.issue_rollups || [];
  setLinks(payload.repo);
  renderOverview(payload);
  renderVerdicts(payload.verification_verdict_counts);
  renderVerdictDonut(payload.verification_verdict_counts);
  renderRepoAnalytics(payload.repo_analytics);
  renderDailyActivity(payload.repo_analytics.daily_activity || []);
  renderIssues(latestIssueRollups);
  renderVerdictIssueBlocks(latestIssueRollups);
  byId("last-updated").textContent = `Last updated: ${formatTimestamp(payload.generated_at)}`;
}

async function refreshWithFallback(): Promise<void> {
  try {
    await refresh();
  } catch (error) {
    byId("last-updated").textContent =
      error instanceof Error ? error.message : "Dashboard refresh failed";
  }
}

byId<HTMLButtonElement>("refresh-button").addEventListener("click", () => {
  void refreshWithFallback();
});

byId<HTMLButtonElement>("verdict-clear-filter").addEventListener("click", () => {
  activeVerdictFilter = "all";
  renderVerdictDonut(
    Object.fromEntries(VERDICT_KEYS.map(([key]) => [key, latestIssueRollups.filter(issue => normalizeIssueVerdict(issue) === key).length])),
  );
  renderVerdictIssueBlocks(latestIssueRollups);
});

void refreshWithFallback();
window.setInterval(() => {
  void refreshWithFallback();
}, 15000);
