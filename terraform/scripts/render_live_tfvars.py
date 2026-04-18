#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any


def aws_cli(*args: str) -> str:
    return subprocess.check_output(["aws", *args], text=True).strip()


def aws_json(*args: str) -> Any:
    return json.loads(aws_cli(*args, "--output", "json"))


def try_aws(*args: str) -> str | None:
    try:
        return aws_cli(*args)
    except subprocess.CalledProcessError:
        return None


def main() -> int:
    region = os.environ.get("AWS_REGION", "us-east-1")
    app_name = os.environ.get("APP_NAME", "devin-vuln-automation")
    secret_name = f"{app_name}-runtime"
    buffer_queue_name = f"{app_name}-buffer.fifo"
    discovery_rule_name = f"{app_name}-discovery-schedule"
    intake_function_name = f"{app_name}-intake"

    secret_string = try_aws(
        "secretsmanager",
        "get-secret-value",
        "--region",
        region,
        "--secret-id",
        secret_name,
        "--query",
        "SecretString",
        "--output",
        "text",
    )

    if secret_string is None:
        payload = {
            "GH_TOKEN": os.environ.get("GH_TOKEN", ""),
            "DEVIN_API_KEY": os.environ.get("DEVIN_API_KEY", ""),
            "DEVIN_ORG_ID": os.environ.get("DEVIN_ORG_ID", ""),
            "GITHUB_WEBHOOK_SECRET": os.environ.get("GITHUB_WEBHOOK_SECRET", ""),
            "LINEAR_WEBHOOK_SECRET": os.environ.get("LINEAR_WEBHOOK_SECRET", ""),
            "TARGET_REPO_OWNER": os.environ.get("TARGET_REPO_OWNER", "C0smicCrush"),
            "TARGET_REPO_NAME": os.environ.get("TARGET_REPO_NAME", "superset-remediation"),
            "AWS_METRICS_BUCKET": os.environ.get("AWS_METRICS_BUCKET", ""),
            "MAX_ACTIVE_REMEDIATIONS": int(os.environ.get("MAX_ACTIVE_REMEDIATIONS", "2")),
            "DEVIN_BYPASS_APPROVAL": os.environ.get("DEVIN_BYPASS_APPROVAL", "false"),
        }
        secret_string = json.dumps(payload)

    queue_delay_seconds = 30
    queue_url = try_aws(
        "sqs",
        "get-queue-url",
        "--region",
        region,
        "--queue-name",
        buffer_queue_name,
        "--query",
        "QueueUrl",
        "--output",
        "text",
    )
    if queue_url:
        queue_attrs = aws_json(
            "sqs",
            "get-queue-attributes",
            "--region",
            region,
            "--queue-url",
            queue_url,
            "--attribute-names",
            "DelaySeconds",
        )
        queue_delay_seconds = int(queue_attrs["Attributes"]["DelaySeconds"])

    discovery_schedule = "rate(1 day)"
    rule_description = try_aws(
        "events",
        "describe-rule",
        "--region",
        region,
        "--name",
        discovery_rule_name,
        "--query",
        "ScheduleExpression",
        "--output",
        "text",
    )
    if rule_description:
        discovery_schedule = rule_description

    intake_allow_public_invoke_permission = False
    lambda_max_active_remediations = "2"
    policy_text = try_aws(
        "lambda",
        "get-policy",
        "--region",
        region,
        "--function-name",
        intake_function_name,
        "--query",
        "Policy",
        "--output",
        "text",
    )
    if policy_text:
        policy = json.loads(policy_text)
        intake_allow_public_invoke_permission = any(
            statement.get("Sid") == f"{app_name}-public-invoke"
            for statement in policy.get("Statement", [])
        )

    function_config_text = try_aws(
        "lambda",
        "get-function-configuration",
        "--region",
        region,
        "--function-name",
        intake_function_name,
        "--query",
        "Environment.Variables.MAX_ACTIVE_REMEDIATIONS",
        "--output",
        "text",
    )
    if function_config_text and function_config_text != "None":
        lambda_max_active_remediations = function_config_text

    tfvars = {
        "aws_region": region,
        "app_name": app_name,
        "runtime_secret_string": secret_string,
        "queue_delay_seconds": queue_delay_seconds,
        "discovery_schedule": discovery_schedule,
        "lambda_max_active_remediations": lambda_max_active_remediations,
        "intake_allow_public_invoke_permission": intake_allow_public_invoke_permission,
    }

    json.dump(tfvars, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
