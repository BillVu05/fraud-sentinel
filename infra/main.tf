terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      Project   = "fraud-sentinel"
      ManagedBy = "terraform"
    }
  }
}

data "aws_caller_identity" "current" {}

locals {
  name = "fraud-sentinel"
}

# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------

# Provisioned, 1 shard: ~$0.36/day. On-demand bills ~$0.04/hour per stream even
# when idle, which is ~$28.80/month for a demo that runs minutes a week.
# `make destroy` after every session; this is the only line item that matters.
resource "aws_kinesis_stream" "txn" {
  name             = "${local.name}-txn"
  shard_count      = 1
  retention_period = 24

  stream_mode_details {
    stream_mode = "PROVISIONED"
  }
}

# ---------------------------------------------------------------------------
# State and decision log
# ---------------------------------------------------------------------------

resource "aws_dynamodb_table" "state" {
  name         = "${local.name}-card-state"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "card_id"

  attribute {
    name = "card_id"
    type = "S"
  }

  # Velocity state is worthless once it ages out of the 24h window, and a TTL
  # costs nothing where a cleanup Lambda would cost attention.
  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }
}

resource "aws_s3_bucket" "decisions" {
  bucket        = "${local.name}-decisions-${data.aws_caller_identity.current.account_id}"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "decisions" {
  bucket                  = aws_s3_bucket.decisions.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "decisions" {
  bucket = aws_s3_bucket.decisions.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Decisions are the audit trail: keep them versioned so a rewrite is visible.
resource "aws_s3_bucket_versioning" "decisions" {
  bucket = aws_s3_bucket.decisions.id
  versioning_configuration {
    status = "Enabled"
  }
}

# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

resource "aws_ecr_repository" "scorer" {
  name         = local.name
  force_delete = true

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_lambda_function" "scorer" {
  function_name = "${local.name}-scorer"
  role          = aws_iam_role.lambda.arn
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.scorer.repository_url}:${var.image_tag}"
  memory_size   = 1024
  timeout       = 30

  environment {
    variables = {
      STATE_TABLE      = aws_dynamodb_table.state.name
      DECISION_BUCKET  = aws_s3_bucket.decisions.id
      MODEL_VERSION    = var.model_version
      ALERT_THRESHOLD  = var.alert_threshold
    }
  }

  depends_on = [aws_cloudwatch_log_group.scorer]
}

resource "aws_lambda_event_source_mapping" "kinesis" {
  event_source_arn  = aws_kinesis_stream.txn.arn
  function_name     = aws_lambda_function.scorer.arn
  starting_position = "LATEST"
  batch_size        = 100

  # Without this, one malformed record blocks the shard until it expires.
  # handler.py returns the failed sequence numbers; only those get retried.
  function_response_types        = ["ReportBatchItemFailures"]
  bisect_batch_on_function_error = true
  maximum_retry_attempts         = 2
}

resource "aws_cloudwatch_log_group" "scorer" {
  name              = "/aws/lambda/${local.name}-scorer"
  retention_in_days = 7
}

# handler.py prints a JSON line per batch; this turns per_txn_ms into a metric
# so p50/p95 come from CloudWatch rather than from a spreadsheet.
resource "aws_cloudwatch_log_metric_filter" "latency" {
  name           = "${local.name}-per-txn-ms"
  log_group_name = aws_cloudwatch_log_group.scorer.name
  pattern        = "{ $.per_txn_ms = * }"

  metric_transformation {
    name      = "PerTransactionMs"
    namespace = "FraudSentinel"
    value     = "$.per_txn_ms"
  }
}

resource "aws_cloudwatch_log_metric_filter" "alerts" {
  name           = "${local.name}-alerts"
  log_group_name = aws_cloudwatch_log_group.scorer.name
  pattern        = "{ $.alerts = * }"

  metric_transformation {
    name      = "Alerts"
    namespace = "FraudSentinel"
    value     = "$.alerts"
  }
}

# ---------------------------------------------------------------------------
# IAM: least privilege, scoped to the four resources above
# ---------------------------------------------------------------------------

resource "aws_iam_role" "lambda" {
  name = "${local.name}-lambda"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "lambda" {
  role = aws_iam_role.lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.scorer.arn}:*"
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem"]
        Resource = aws_dynamodb_table.state.arn
      },
      {
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = "${aws_s3_bucket.decisions.arn}/*"
      },
      {
        Effect = "Allow"
        Action = [
          "kinesis:GetRecords",
          "kinesis:GetShardIterator",
          "kinesis:DescribeStream",
          "kinesis:ListShards",
        ]
        Resource = aws_kinesis_stream.txn.arn
      },
    ]
  })
}

# ---------------------------------------------------------------------------
# Cost guardrail
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "spend" {
  alarm_name          = "${local.name}-monthly-spend"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "EstimatedCharges"
  namespace           = "AWS/Billing"
  period              = 21600
  statistic           = "Maximum"
  threshold           = var.spend_alarm_usd
  alarm_description   = "fraud-sentinel: estimated monthly charges exceeded threshold"

  # Billing metrics only publish to us-east-1. Wire an SNS topic here if you
  # want an email; the alarm state alone is visible in the console.
  dimensions = { Currency = "USD" }
}
