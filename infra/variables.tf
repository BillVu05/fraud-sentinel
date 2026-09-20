variable "region" {
  description = "Sydney. Keeps transaction data and Bedrock inference inside the Australian boundary."
  type        = string
  default     = "ap-southeast-2"
}

variable "image_tag" {
  description = "ECR tag for the scorer image. Bump on every model change so a decision's model_version is traceable to an image."
  type        = string
  default     = "latest"
}

variable "model_version" {
  description = "Stamped onto every decision written to S3."
  type        = string
  default     = "dev"
}

variable "alert_threshold" {
  description = "Score at or above which a transaction alerts. Comes from artifacts/metrics.json -> model.threshold."
  type        = string
  default     = "0.9"
}

variable "spend_alarm_usd" {
  description = "Estimated monthly charges that trip the billing alarm."
  type        = number
  default     = 10
}
