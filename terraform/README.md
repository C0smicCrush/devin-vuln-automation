# Terraform Stack

This directory replaces the imperative AWS deploy flow in `infra/deploy_aws.sh` with Terraform that adopts the existing live stack.

## What It Manages

- SQS FIFO queue and DLQ
- S3 metrics bucket, SSE-S3 encryption, and public access block
- Secrets Manager secret and current runtime payload
- Lambda IAM role and policies
- Four Lambda functions
- Worker event source mapping
- Intake Lambda Function URL and public permissions
- EventBridge schedules and targets

## First-Time Cutover

1. Build the Lambda bundle:

```bash
bash terraform/scripts/build_lambda_bundle.sh "$PWD" "build/devin-vuln-automation.zip"
```

2. Generate a live tfvars file from the currently deployed secret and AWS settings:

```bash
python3 terraform/scripts/render_live_tfvars.py > terraform/live.auto.tfvars.json
```

3. Bootstrap the remote state backend:

```bash
terraform -chdir=terraform/bootstrap init
terraform -chdir=terraform/bootstrap apply -auto-approve
```

4. Initialize the main stack with the generated backend config:

```bash
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
AWS_REGION="${AWS_REGION:-us-east-1}"
terraform -chdir=terraform init \
  -backend-config="bucket=devin-vuln-automation-terraform-state-${ACCOUNT_ID}-${AWS_REGION}" \
  -backend-config="key=prod/terraform.tfstate" \
  -backend-config="region=${AWS_REGION}" \
  -backend-config="dynamodb_table=devin-vuln-automation-terraform-locks" \
  -backend-config="encrypt=true"
```

5. Import the live resources into Terraform state:

```bash
bash terraform/scripts/import_live_stack.sh
```

6. Review and apply:

```bash
terraform -chdir=terraform plan
terraform -chdir=terraform apply
```

## Runtime Secret Source

`render_live_tfvars.py` prefers the currently deployed Secrets Manager payload so the first Terraform apply keeps the existing values exactly as they are today. If the live secret does not exist yet, it falls back to environment variables.
