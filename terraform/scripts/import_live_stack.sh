#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TF_DIR="${ROOT_DIR}/terraform"
APP_NAME="${APP_NAME:-devin-vuln-automation}"
REGION="${AWS_REGION:-us-east-1}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

BUFFER_QUEUE_NAME="${APP_NAME}-buffer.fifo"
DLQ_QUEUE_NAME="${APP_NAME}-dlq.fifo"
SECRET_NAME="${APP_NAME}-runtime"
ROLE_NAME="${APP_NAME}-lambda-role"
BUCKET_NAME="${APP_NAME}-metrics-${ACCOUNT_ID}-${REGION}"
POLLER_RULE_NAME="${APP_NAME}-poller-schedule"
DISCOVERY_RULE_NAME="${APP_NAME}-discovery-schedule"
BUFFER_QUEUE_URL="https://sqs.${REGION}.amazonaws.com/${ACCOUNT_ID}/${BUFFER_QUEUE_NAME}"
DLQ_QUEUE_URL="https://sqs.${REGION}.amazonaws.com/${ACCOUNT_ID}/${DLQ_QUEUE_NAME}"
SECRET_ARN="$(aws secretsmanager describe-secret --region "${REGION}" --secret-id "${SECRET_NAME}" --query ARN --output text)"
SECRET_VERSION_ID="$(
  aws secretsmanager list-secret-version-ids \
    --region "${REGION}" \
    --secret-id "${SECRET_NAME}" \
    --query "Versions[?contains(VersionStages, 'AWSCURRENT')].VersionId | [0]" \
    --output text
)"

tf() {
  terraform -chdir="${TF_DIR}" "$@"
}

state_has() {
  tf state show "$1" >/dev/null 2>&1
}

import_if_missing() {
  local address="$1"
  local resource_id="$2"

  if state_has "${address}"; then
    echo "Already imported: ${address}"
    return
  fi

  echo "Importing ${address}"
  tf import "${address}" "${resource_id}"
}

import_if_missing aws_sqs_queue.dlq "${DLQ_QUEUE_URL}"
import_if_missing aws_sqs_queue.buffer "${BUFFER_QUEUE_URL}"
import_if_missing aws_s3_bucket.metrics "${BUCKET_NAME}"
import_if_missing aws_s3_bucket_server_side_encryption_configuration.metrics "${BUCKET_NAME}"
import_if_missing aws_s3_bucket_public_access_block.metrics "${BUCKET_NAME}"
import_if_missing aws_secretsmanager_secret.runtime "${SECRET_ARN}"
import_if_missing aws_secretsmanager_secret_version.runtime "${SECRET_ARN}|${SECRET_VERSION_ID}"
import_if_missing aws_iam_role.lambda "${ROLE_NAME}"
import_if_missing aws_iam_role_policy.inline "${ROLE_NAME}:${APP_NAME}-inline"
import_if_missing aws_iam_role_policy_attachment.basic_execution "${ROLE_NAME}/arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
import_if_missing 'aws_lambda_function.functions["intake"]' "${APP_NAME}-intake"
import_if_missing 'aws_lambda_function.functions["worker"]' "${APP_NAME}-worker"
import_if_missing 'aws_lambda_function.functions["poller"]' "${APP_NAME}-poller"
import_if_missing 'aws_lambda_function.functions["discovery"]' "${APP_NAME}-discovery"

EVENT_SOURCE_MAPPING_ID="$(
  aws lambda list-event-source-mappings \
    --region "${REGION}" \
    --function-name "${APP_NAME}-worker" \
    --query "EventSourceMappings[?EventSourceArn=='arn:aws:sqs:${REGION}:${ACCOUNT_ID}:${BUFFER_QUEUE_NAME}'].UUID | [0]" \
    --output text
)"
import_if_missing aws_lambda_event_source_mapping.worker "${EVENT_SOURCE_MAPPING_ID}"
import_if_missing aws_lambda_function_url.intake "${APP_NAME}-intake"
import_if_missing aws_lambda_permission.intake_public_url "${APP_NAME}-intake/${APP_NAME}-public-url"

if [[ "$(python3 - "${TF_DIR}/live.auto.tfvars.json" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as handle:
    payload = json.load(handle)

print(str(payload.get("intake_allow_public_invoke_permission", False)).lower())
PY
)" == "true" ]]; then
  import_if_missing 'aws_lambda_permission.intake_public_invoke[0]' "${APP_NAME}-intake/${APP_NAME}-public-invoke"
fi

import_if_missing aws_cloudwatch_event_rule.poller "${POLLER_RULE_NAME}"
import_if_missing aws_cloudwatch_event_target.poller "${POLLER_RULE_NAME}/1"
import_if_missing aws_lambda_permission.poller_schedule "${APP_NAME}-poller/${POLLER_RULE_NAME}"
import_if_missing aws_cloudwatch_event_rule.discovery "${DISCOVERY_RULE_NAME}"
import_if_missing aws_cloudwatch_event_target.discovery "${DISCOVERY_RULE_NAME}/1"
import_if_missing aws_lambda_permission.discovery_schedule "${APP_NAME}-discovery/${DISCOVERY_RULE_NAME}"
