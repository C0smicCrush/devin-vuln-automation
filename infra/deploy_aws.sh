#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_NAME="${APP_NAME:-devin-vuln-automation}"
REGION="${AWS_REGION:-us-east-1}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
QUEUE_DELAY_SECONDS="${QUEUE_DELAY_SECONDS:-30}"
DISCOVERY_SCHEDULE="${DISCOVERY_SCHEDULE:-rate(1 day)}"
QUEUE_NAME="${APP_NAME}-buffer.fifo"
DLQ_NAME="${APP_NAME}-dlq.fifo"
SECRET_NAME="${APP_NAME}-runtime"
ROLE_NAME="${APP_NAME}-lambda-role"
BUCKET_NAME="${APP_NAME}-metrics-${ACCOUNT_ID}-${REGION}"
BUILD_DIR="${ROOT_DIR}/build/lambda"
ZIP_PATH="${ROOT_DIR}/build/${APP_NAME}.zip"

mkdir -p "${ROOT_DIR}/build"
rm -rf "${BUILD_DIR}" "${ZIP_PATH}"
mkdir -p "${BUILD_DIR}"

echo "Creating low-cost SQS resources in ${REGION}..."
DLQ_ATTRIBUTES="$(python3 - <<'PY'
import json
print(json.dumps({
    "FifoQueue": "true",
    "ContentBasedDeduplication": "true",
}))
PY
)"
DLQ_URL="$(aws sqs create-queue \
  --region "${REGION}" \
  --queue-name "${DLQ_NAME}" \
  --attributes "${DLQ_ATTRIBUTES}" \
  --query QueueUrl --output text)"
DLQ_ARN="$(aws sqs get-queue-attributes \
  --region "${REGION}" \
  --queue-url "${DLQ_URL}" \
  --attribute-names QueueArn \
  --query 'Attributes.QueueArn' --output text)"
QUEUE_ATTRIBUTES="$(python3 - <<PY
import json
print(json.dumps({
    "FifoQueue": "true",
    "ContentBasedDeduplication": "true",
    "DelaySeconds": "${QUEUE_DELAY_SECONDS}",
    "VisibilityTimeout": "180",
    "ReceiveMessageWaitTimeSeconds": "20",
    "RedrivePolicy": json.dumps({
        "deadLetterTargetArn": "${DLQ_ARN}",
        "maxReceiveCount": "3",
    }),
}))
PY
)"
QUEUE_URL="$(aws sqs create-queue \
  --region "${REGION}" \
  --queue-name "${QUEUE_NAME}" \
  --attributes "${QUEUE_ATTRIBUTES}" \
  --query QueueUrl --output text)"
QUEUE_ARN="$(aws sqs get-queue-attributes \
  --region "${REGION}" \
  --queue-url "${QUEUE_URL}" \
  --attribute-names QueueArn \
  --query 'Attributes.QueueArn' --output text)"

echo "Ensuring metrics bucket exists..."
if ! aws s3api head-bucket --bucket "${BUCKET_NAME}" 2>/dev/null; then
  aws s3api create-bucket --bucket "${BUCKET_NAME}" --region "${REGION}" >/dev/null
fi

echo "Ensuring Secrets Manager payload exists..."
if ! aws secretsmanager describe-secret --region "${REGION}" --secret-id "${SECRET_NAME}" >/dev/null 2>&1; then
  GH_TOKEN="$(gh auth token)"
  DEVIN_API_KEY="${DEVIN_API_KEY:-}"
  DEVIN_ORG_ID="${DEVIN_ORG_ID:-}"
  GITHUB_WEBHOOK_SECRET="${GITHUB_WEBHOOK_SECRET:-$(python3 - <<'PY'
import secrets
print(secrets.token_hex(32))
PY
)}"
  LINEAR_WEBHOOK_SECRET="${LINEAR_WEBHOOK_SECRET:-$(python3 - <<'PY'
import secrets
print(secrets.token_hex(32))
PY
)}"
  aws secretsmanager create-secret \
    --region "${REGION}" \
    --name "${SECRET_NAME}" \
    --secret-string "$(python3 - <<PY
import json
import os
print(json.dumps({
    "GH_TOKEN": os.environ.get("GH_TOKEN", "${GH_TOKEN}"),
    "DEVIN_API_KEY": os.environ.get("DEVIN_API_KEY", "${DEVIN_API_KEY}"),
    "DEVIN_ORG_ID": os.environ.get("DEVIN_ORG_ID", "${DEVIN_ORG_ID}"),
    "GITHUB_WEBHOOK_SECRET": os.environ.get("GITHUB_WEBHOOK_SECRET", "${GITHUB_WEBHOOK_SECRET}"),
    "LINEAR_WEBHOOK_SECRET": os.environ.get("LINEAR_WEBHOOK_SECRET", "${LINEAR_WEBHOOK_SECRET}"),
    "TARGET_REPO_OWNER": os.environ.get("TARGET_REPO_OWNER", "C0smicCrush"),
    "TARGET_REPO_NAME": os.environ.get("TARGET_REPO_NAME", "superset-remediation"),
    "AWS_METRICS_BUCKET": "${BUCKET_NAME}",
    "MAX_ACTIVE_REMEDIATIONS": 2,
    "MAX_DISCOVERY_FINDINGS": 1,
    "DISCOVERY_TIMEOUT_SECONDS": 900,
    "DISCOVERY_LOCK_TTL_SECONDS": 5400,
    "DEVIN_BYPASS_APPROVAL": "false",
}))
PY
)" >/dev/null
fi

echo "Ensuring IAM role exists..."
ASSUME_ROLE_POLICY="$(cat <<'JSON'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Service": "lambda.amazonaws.com"
      },
      "Action": "sts:AssumeRole"
    }
  ]
}
JSON
)"

if ! aws iam get-role --role-name "${ROLE_NAME}" >/dev/null 2>&1; then
  aws iam create-role \
    --role-name "${ROLE_NAME}" \
    --assume-role-policy-document "${ASSUME_ROLE_POLICY}" >/dev/null
  aws iam attach-role-policy \
    --role-name "${ROLE_NAME}" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole >/dev/null
fi

aws iam put-role-policy \
  --role-name "${ROLE_NAME}" \
  --policy-name "${APP_NAME}-inline" \
  --policy-document "$(cat <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "sqs:SendMessage",
        "sqs:ReceiveMessage",
        "sqs:DeleteMessage",
        "sqs:GetQueueAttributes"
      ],
      "Resource": [
        "${QUEUE_ARN}",
        "${DLQ_ARN}"
      ]
    },
    {
      "Effect": "Allow",
      "Action": [
        "secretsmanager:GetSecretValue"
      ],
      "Resource": "arn:aws:secretsmanager:${REGION}:${ACCOUNT_ID}:secret:${SECRET_NAME}*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "s3:PutObject",
        "s3:GetObject",
        "s3:DeleteObject"
      ],
      "Resource": "arn:aws:s3:::${BUCKET_NAME}/*"
    }
  ]
}
JSON
)" >/dev/null

echo "Packaging Lambda bundle..."
python3 -m pip install --quiet -r "${ROOT_DIR}/requirements.txt" -t "${BUILD_DIR}"
cp -R "${ROOT_DIR}/config" "${BUILD_DIR}/config"
cp -R "${ROOT_DIR}/scripts" "${BUILD_DIR}/scripts"
cp "${ROOT_DIR}/common.py" "${ROOT_DIR}/aws_runtime.py" "${ROOT_DIR}/lambda_intake.py" "${ROOT_DIR}/lambda_worker.py" "${ROOT_DIR}/lambda_poller.py" "${ROOT_DIR}/lambda_discovery.py" "${BUILD_DIR}/"
(cd "${BUILD_DIR}" && zip -qr "${ZIP_PATH}" .)

ROLE_ARN="$(aws iam get-role --role-name "${ROLE_NAME}" --query 'Role.Arn' --output text)"

deploy_lambda () {
  local function_name="$1"
  local handler_name="$2"
  local timeout_seconds="$3"
  local memory_size="$4"

  if aws lambda get-function --region "${REGION}" --function-name "${function_name}" >/dev/null 2>&1; then
    aws lambda update-function-code \
      --region "${REGION}" \
      --function-name "${function_name}" \
      --zip-file "fileb://${ZIP_PATH}" >/dev/null
    aws lambda wait function-updated-v2 \
      --region "${REGION}" \
      --function-name "${function_name}"
  else
    aws lambda create-function \
      --region "${REGION}" \
      --function-name "${function_name}" \
      --runtime python3.12 \
      --handler "${handler_name}" \
      --role "${ROLE_ARN}" \
      --zip-file "fileb://${ZIP_PATH}" \
      --timeout "${timeout_seconds}" \
      --memory-size "${memory_size}" \
      --environment "Variables={AWS_APP_SECRET_NAME=${SECRET_NAME},AWS_SQS_QUEUE_URL=${QUEUE_URL},AWS_METRICS_BUCKET=${BUCKET_NAME},MAX_ACTIVE_REMEDIATIONS=2,TARGET_REPO_OWNER=C0smicCrush,TARGET_REPO_NAME=superset-remediation}" >/dev/null
    aws lambda wait function-active-v2 \
      --region "${REGION}" \
      --function-name "${function_name}"
  fi

  aws lambda update-function-configuration \
    --region "${REGION}" \
    --function-name "${function_name}" \
    --timeout "${timeout_seconds}" \
    --memory-size "${memory_size}" \
    --environment "Variables={AWS_APP_SECRET_NAME=${SECRET_NAME},AWS_SQS_QUEUE_URL=${QUEUE_URL},AWS_METRICS_BUCKET=${BUCKET_NAME},MAX_ACTIVE_REMEDIATIONS=2,TARGET_REPO_OWNER=C0smicCrush,TARGET_REPO_NAME=superset-remediation}" >/dev/null

  aws lambda wait function-updated-v2 \
    --region "${REGION}" \
    --function-name "${function_name}"
}

deploy_lambda "${APP_NAME}-intake" "lambda_intake.handler" 300 512
deploy_lambda "${APP_NAME}-worker" "lambda_worker.handler" 180 256
deploy_lambda "${APP_NAME}-poller" "lambda_poller.handler" 300 256
deploy_lambda "${APP_NAME}-discovery" "lambda_discovery.handler" 900 256

if ! aws lambda list-event-source-mappings \
  --region "${REGION}" \
  --function-name "${APP_NAME}-worker" \
  --query "EventSourceMappings[?EventSourceArn=='${QUEUE_ARN}'].UUID" \
  --output text | grep -q .; then
  aws lambda create-event-source-mapping \
    --region "${REGION}" \
    --function-name "${APP_NAME}-worker" \
    --event-source-arn "${QUEUE_ARN}" \
    --batch-size 1 \
    --scaling-config MaximumConcurrency=2 \
    --maximum-batching-window-in-seconds 0 >/dev/null
fi

if ! aws lambda get-function-url-config --region "${REGION}" --function-name "${APP_NAME}-intake" >/dev/null 2>&1; then
  aws lambda create-function-url-config \
    --region "${REGION}" \
    --function-name "${APP_NAME}-intake" \
    --auth-type NONE >/dev/null
fi

if ! aws lambda get-policy --region "${REGION}" --function-name "${APP_NAME}-intake" >/dev/null 2>&1; then
  aws lambda add-permission \
    --region "${REGION}" \
    --function-name "${APP_NAME}-intake" \
    --statement-id "${APP_NAME}-public-url" \
    --action lambda:InvokeFunctionUrl \
    --principal "*" \
    --function-url-auth-type NONE >/dev/null
fi

RULE_NAME="${APP_NAME}-poller-schedule"
aws events put-rule \
  --region "${REGION}" \
  --name "${RULE_NAME}" \
  --schedule-expression "rate(5 minutes)" >/dev/null

POLLER_ARN="$(aws lambda get-function --region "${REGION}" --function-name "${APP_NAME}-poller" --query 'Configuration.FunctionArn' --output text)"
aws events put-targets \
  --region "${REGION}" \
  --rule "${RULE_NAME}" \
  --targets "Id"="1","Arn"="${POLLER_ARN}" >/dev/null

if ! aws lambda get-policy --region "${REGION}" --function-name "${APP_NAME}-poller" >/dev/null 2>&1 || ! aws lambda get-policy --region "${REGION}" --function-name "${APP_NAME}-poller" --query 'Policy' --output text | grep -q "${RULE_NAME}"; then
  aws lambda add-permission \
    --region "${REGION}" \
    --function-name "${APP_NAME}-poller" \
    --statement-id "${RULE_NAME}" \
    --action "lambda:InvokeFunction" \
    --principal events.amazonaws.com \
    --source-arn "arn:aws:events:${REGION}:${ACCOUNT_ID}:rule/${RULE_NAME}" >/dev/null || true
fi

DISCOVERY_RULE_NAME="${APP_NAME}-discovery-schedule"
aws events put-rule \
  --region "${REGION}" \
  --name "${DISCOVERY_RULE_NAME}" \
  --schedule-expression "${DISCOVERY_SCHEDULE}" >/dev/null

DISCOVERY_ARN="$(aws lambda get-function --region "${REGION}" --function-name "${APP_NAME}-discovery" --query 'Configuration.FunctionArn' --output text)"
aws events put-targets \
  --region "${REGION}" \
  --rule "${DISCOVERY_RULE_NAME}" \
  --targets "$(python3 - <<PY
import json
print(json.dumps([
    {
        "Id": "1",
        "Arn": "${DISCOVERY_ARN}",
        "Input": json.dumps({"event_type": "scheduled_discovery", "max_findings": 1}),
    }
]))
PY
)" >/dev/null

if ! aws lambda get-policy --region "${REGION}" --function-name "${APP_NAME}-discovery" >/dev/null 2>&1 || ! aws lambda get-policy --region "${REGION}" --function-name "${APP_NAME}-discovery" --query 'Policy' --output text | grep -q "${DISCOVERY_RULE_NAME}"; then
  aws lambda add-permission \
    --region "${REGION}" \
    --function-name "${APP_NAME}-discovery" \
    --statement-id "${DISCOVERY_RULE_NAME}" \
    --action "lambda:InvokeFunction" \
    --principal events.amazonaws.com \
    --source-arn "arn:aws:events:${REGION}:${ACCOUNT_ID}:rule/${DISCOVERY_RULE_NAME}" >/dev/null || true
fi

FUNCTION_URL="$(aws lambda get-function-url-config --region "${REGION}" --function-name "${APP_NAME}-intake" --query FunctionUrl --output text)"
GITHUB_WEBHOOK_SECRET="$(aws secretsmanager get-secret-value --region "${REGION}" --secret-id "${SECRET_NAME}" --query SecretString --output text | jq -r '.GITHUB_WEBHOOK_SECRET')"
HOOK_ID="$(gh api "repos/C0smicCrush/superset-remediation/hooks" --jq ".[] | select(.config.url == \"${FUNCTION_URL}github\") | .id" 2>/dev/null | head -n 1)"

if [ -n "${HOOK_ID}" ]; then
  gh api "repos/C0smicCrush/superset-remediation/hooks/${HOOK_ID}" \
    --method PATCH \
    -f name='web' \
    -F active=true \
    -f events[]='issues' \
    -f events[]='issue_comment' \
    -f events[]='pull_request_review_comment' \
    -f config[url]="${FUNCTION_URL}github" \
    -f config[content_type]='json' \
    -f config[secret]="${GITHUB_WEBHOOK_SECRET}" >/dev/null
else
  gh api "repos/C0smicCrush/superset-remediation/hooks" \
    --method POST \
    -f name='web' \
    -F active=true \
    -f events[]='issues' \
    -f events[]='issue_comment' \
    -f events[]='pull_request_review_comment' \
    -f config[url]="${FUNCTION_URL}github" \
    -f config[content_type]='json' \
    -f config[secret]="${GITHUB_WEBHOOK_SECRET}" >/dev/null
fi

cat <<EOF
Deployment complete.

Region: ${REGION}
Queue URL: ${QUEUE_URL}
DLQ URL: ${DLQ_URL}
Metrics bucket: ${BUCKET_NAME}
Secrets Manager secret: ${SECRET_NAME}
Lambda intake URL: ${FUNCTION_URL}
GitHub webhook target: ${FUNCTION_URL}github
Discovery schedule: ${DISCOVERY_SCHEDULE}

Use these paths on the Lambda URL:
- /github
- /linear
- /manual
EOF
