variable "aws_region" {
  description = "AWS region for the Terraform backend."
  type        = string
  default     = "us-east-1"
}

variable "app_name" {
  description = "Application prefix for backend naming."
  type        = string
  default     = "devin-vuln-automation"
}

variable "lock_table_name" {
  description = "DynamoDB table name for Terraform state locking."
  type        = string
  default     = "devin-vuln-automation-terraform-locks"
}
