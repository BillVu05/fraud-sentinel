output "stream_name" {
  value = aws_kinesis_stream.txn.name
}

output "ecr_repository_url" {
  value = aws_ecr_repository.scorer.repository_url
}

output "decision_bucket" {
  value = aws_s3_bucket.decisions.id
}

output "state_table" {
  value = aws_dynamodb_table.state.name
}

output "log_group" {
  value = aws_cloudwatch_log_group.scorer.name
}
