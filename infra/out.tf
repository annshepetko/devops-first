output "bucket_name" { value = aws_s3_bucket.main.bucket }
output "queue_url" { value = aws_sqs_queue.main.url }
output "dlq_url" { value = aws_sqs_queue.dlq.url }
output "worker_instance_id" { value = aws_instance.worker.id }