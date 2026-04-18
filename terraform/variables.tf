variable "aws_region" {
  description = "AWS region for the live stack."
  type        = string
  default     = "us-east-1"
}

variable "app_name" {
  description = "Application name prefix used for AWS resources."
  type        = string
  default     = "devin-vuln-automation"
}

variable "runtime_secret_string" {
  description = "Exact Secrets Manager JSON payload to store for the runtime secret."
  type        = string
  sensitive   = true
}

variable "queue_delay_seconds" {
  description = "Delay for the live FIFO buffer queue."
  type        = number
  default     = 30
}

variable "discovery_schedule" {
  description = "EventBridge schedule expression for discovery."
  type        = string
  default     = "rate(1 day)"
}

variable "lambda_max_active_remediations" {
  description = "Current MAX_ACTIVE_REMEDIATIONS value configured on the Lambda environment."
  type        = string
  default     = "2"
}

variable "intake_allow_public_invoke_permission" {
  description = "Preserve the current public lambda:InvokeFunction permission on intake."
  type        = bool
  default     = true
}
