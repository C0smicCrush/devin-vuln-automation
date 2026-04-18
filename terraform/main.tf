data "aws_caller_identity" "current" {}

locals {
  runtime_secret         = jsondecode(var.runtime_secret_string)
  runtime_secret_name    = "${var.app_name}-runtime"
  lambda_role_name       = "${var.app_name}-lambda-role"
  metrics_bucket_name    = "${var.app_name}-metrics-${data.aws_caller_identity.current.account_id}-${var.aws_region}"
  buffer_queue_name      = "${var.app_name}-buffer.fifo"
  dlq_queue_name         = "${var.app_name}-dlq.fifo"
  poller_rule_name       = "${var.app_name}-poller-schedule"
  discovery_rule_name    = "${var.app_name}-discovery-schedule"
  package_zip_path       = abspath("${path.module}/../build/${var.app_name}.zip")
  discovery_target_input = "{\"event_type\": \"scheduled_discovery\", \"max_findings\": 1}"
  target_repo_owner      = try(local.runtime_secret.TARGET_REPO_OWNER, "C0smicCrush")
  target_repo_name       = try(local.runtime_secret.TARGET_REPO_NAME, "superset-remediation")

  lambda_environment = {
    AWS_APP_SECRET_NAME     = local.runtime_secret_name
    AWS_SQS_QUEUE_URL       = aws_sqs_queue.buffer.id
    AWS_METRICS_BUCKET      = local.metrics_bucket_name
    MAX_ACTIVE_REMEDIATIONS = var.lambda_max_active_remediations
    TARGET_REPO_OWNER       = local.target_repo_owner
    TARGET_REPO_NAME        = local.target_repo_name
  }

  lambda_definitions = {
    intake = {
      handler = "lambda_intake.handler"
      timeout = 300
      memory  = 512
    }
    worker = {
      handler = "lambda_worker.handler"
      timeout = 180
      memory  = 256
    }
    poller = {
      handler = "lambda_poller.handler"
      timeout = 300
      memory  = 256
    }
    discovery = {
      handler = "lambda_discovery.handler"
      timeout = 900
      memory  = 256
    }
  }
}

resource "aws_sqs_queue" "dlq" {
  name                        = local.dlq_queue_name
  fifo_queue                  = true
  content_based_deduplication = true
  visibility_timeout_seconds  = 30
  delay_seconds               = 0
  receive_wait_time_seconds   = 0
  deduplication_scope         = "queue"
  fifo_throughput_limit       = "perQueue"
  sqs_managed_sse_enabled     = true

  lifecycle {
    ignore_changes = [max_message_size]
  }
}

resource "aws_sqs_queue" "buffer" {
  name                        = local.buffer_queue_name
  fifo_queue                  = true
  content_based_deduplication = true
  delay_seconds               = var.queue_delay_seconds
  visibility_timeout_seconds  = 180
  receive_wait_time_seconds   = 20
  deduplication_scope         = "queue"
  fifo_throughput_limit       = "perQueue"
  sqs_managed_sse_enabled     = true
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = 3
  })

  lifecycle {
    ignore_changes = [max_message_size]
  }
}

resource "aws_s3_bucket" "metrics" {
  bucket = local.metrics_bucket_name
}

resource "aws_s3_bucket_server_side_encryption_configuration" "metrics" {
  bucket = aws_s3_bucket.metrics.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }

    bucket_key_enabled = false
  }
}

resource "aws_s3_bucket_public_access_block" "metrics" {
  bucket = aws_s3_bucket.metrics.id

  block_public_acls       = true
  ignore_public_acls      = true
  block_public_policy     = true
  restrict_public_buckets = true
}

resource "aws_secretsmanager_secret" "runtime" {
  name                           = local.runtime_secret_name
  recovery_window_in_days        = 30
  force_overwrite_replica_secret = false

  lifecycle {
    ignore_changes = [
      force_overwrite_replica_secret,
      recovery_window_in_days,
    ]
  }
}

resource "aws_secretsmanager_secret_version" "runtime" {
  secret_id     = aws_secretsmanager_secret.runtime.id
  secret_string = var.runtime_secret_string

  lifecycle {
    ignore_changes = [secret_string]
  }
}

resource "aws_iam_role" "lambda" {
  name = local.lambda_role_name

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "basic_execution" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "inline" {
  name = "${var.app_name}-inline"
  role = aws_iam_role.lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "sqs:SendMessage",
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes",
        ]
        Resource = [
          aws_sqs_queue.buffer.arn,
          aws_sqs_queue.dlq.arn,
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue",
        ]
        Resource = "arn:aws:secretsmanager:${var.aws_region}:${data.aws_caller_identity.current.account_id}:secret:${local.runtime_secret_name}*"
      },
      {
        Effect = "Allow"
        Action = [
          "s3:PutObject",
          "s3:GetObject",
          "s3:DeleteObject",
        ]
        Resource = "arn:aws:s3:::${local.metrics_bucket_name}/*"
      },
    ]
  })
}

resource "aws_lambda_function" "functions" {
  for_each = local.lambda_definitions

  function_name    = "${var.app_name}-${each.key}"
  role             = aws_iam_role.lambda.arn
  runtime          = "python3.12"
  handler          = each.value.handler
  filename         = local.package_zip_path
  source_code_hash = filebase64sha256(local.package_zip_path)
  timeout          = each.value.timeout
  memory_size      = each.value.memory
  architectures    = ["x86_64"]

  environment {
    variables = local.lambda_environment
  }

  depends_on = [
    aws_iam_role_policy.inline,
    aws_iam_role_policy_attachment.basic_execution,
  ]
}

resource "aws_lambda_event_source_mapping" "worker" {
  event_source_arn                   = aws_sqs_queue.buffer.arn
  function_name                      = aws_lambda_function.functions["worker"].arn
  batch_size                         = 1
  maximum_batching_window_in_seconds = 0
  scaling_config {
    maximum_concurrency = 2
  }
}

resource "aws_lambda_function_url" "intake" {
  function_name      = aws_lambda_function.functions["intake"].function_name
  authorization_type = "NONE"
  invoke_mode        = "BUFFERED"
}

resource "aws_lambda_permission" "intake_public_url" {
  statement_id           = "${var.app_name}-public-url"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.functions["intake"].function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

resource "aws_lambda_permission" "intake_public_invoke" {
  count = var.intake_allow_public_invoke_permission ? 1 : 0

  statement_id  = "${var.app_name}-public-invoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.functions["intake"].function_name
  principal     = "*"
}

resource "aws_cloudwatch_event_rule" "poller" {
  name                = local.poller_rule_name
  schedule_expression = "rate(5 minutes)"
}

resource "aws_cloudwatch_event_target" "poller" {
  rule      = aws_cloudwatch_event_rule.poller.name
  target_id = "1"
  arn       = aws_lambda_function.functions["poller"].arn
}

resource "aws_lambda_permission" "poller_schedule" {
  statement_id  = local.poller_rule_name
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.functions["poller"].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.poller.arn
}

resource "aws_cloudwatch_event_rule" "discovery" {
  name                = local.discovery_rule_name
  schedule_expression = var.discovery_schedule
}

resource "aws_cloudwatch_event_target" "discovery" {
  rule      = aws_cloudwatch_event_rule.discovery.name
  target_id = "1"
  arn       = aws_lambda_function.functions["discovery"].arn
  input     = local.discovery_target_input
}

resource "aws_lambda_permission" "discovery_schedule" {
  statement_id  = local.discovery_rule_name
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.functions["discovery"].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.discovery.arn
}
