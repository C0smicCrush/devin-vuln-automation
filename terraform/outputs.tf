output "intake_function_url" {
  description = "Public Lambda Function URL for intake."
  value       = aws_lambda_function_url.intake.function_url
}

output "metrics_bucket_name" {
  description = "Metrics bucket name."
  value       = aws_s3_bucket.metrics.bucket
}

output "buffer_queue_url" {
  description = "Primary FIFO queue URL."
  value       = aws_sqs_queue.buffer.id
}

output "runtime_secret_name" {
  description = "Runtime Secrets Manager secret name."
  value       = aws_secretsmanager_secret.runtime.name
}
