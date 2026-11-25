# Event-driven pipeline: S3 → SQS → EC2 worker → S3

A small event-driven pipeline in AWS, fully described in Terraform.
A file uploaded to `incoming/` triggers an S3 event notification to SQS.
A worker on EC2 reads the message, downloads the file and writes
`results/<file name>.json` with the file name, size in bytes, SHA-256 hash
and processing time. A file that cannot be processed (for example, an empty
file) is moved by SQS to a dead-letter queue (DLQ) after 3 failed attempts.

```
            upload                 event                  poll
  you ───────────────► S3 bucket ─────────► SQS queue ─────────► EC2 worker
                       incoming/              │                   │
                                              │ 3 failed tries    │ writes
                                              ▼                   ▼
                                           SQS DLQ           S3 bucket
                                                              results/
```

## Repository layout

```
bootstrap/   Terraform root that creates the S3 bucket for Terraform state
infra/       Main Terraform root (bucket, queues, worker, IAM, budget)
worker/      Worker source code (worker.py)
```

## Prerequisites

- Terraform >= 1.10 (the S3 backend lock file needs it)
- AWS CLI, configured for an IAM user (not root): `aws sts get-caller-identity`
- Region: `eu-central-1` (variable `region`, default `eu-central-1`)

## Deploy from zero

1. Create the state bucket:

```bash
cd bootstrap
terraform init
terraform apply
# note the bucket name from the output
```

2. Deploy the pipeline (replace the bucket name with the one from step 1):

```bash
cd ../infra
terraform init \
  -backend-config="bucket=<state-bucket-name>" \
  -backend-config="region=eu-central-1"
terraform apply -var owner="<your-name>" -var alert_email="<your-email>"
```

`terraform.tfvars` can hold `owner` and `alert_email` instead of `-var`
(it is in `.gitignore`; see `terraform.tfvars.example`).
After `apply`, give the instance 1-2 minutes to install dependencies and
start the worker service.

## Test

```bash
BUCKET=$(terraform output -raw bucket_name)

# normal file: result appears in results/
echo "hello" > ok.txt
aws s3 cp ok.txt s3://$BUCKET/incoming/ok.txt
sleep 20
aws s3 cp s3://$BUCKET/results/ok.txt.json -

# broken file: ends up in the DLQ after 3 failed receives (~2 minutes)
touch empty.txt
aws s3 cp empty.txt s3://$BUCKET/incoming/empty.txt
aws sqs receive-message --queue-url $(terraform output -raw dlq_url)
```

Worker logs: CloudWatch Logs group `/aws/ec2/event-pipeline-dev-worker`.
If there are no logs, check the instance system log in the EC2 console
(Actions → Monitor and troubleshoot → Get system log).

## Destroy

```bash
cd infra
terraform destroy
cd ../bootstrap
terraform destroy
```

Destroy `infra` first and `bootstrap` last: the state of `infra` lives in the
bootstrap bucket. Buckets use `force_destroy = true`, so objects (and object
versions) do not block the destroy.
If the budget action has fired, detach the deny policy from the worker role
before destroying (it was attached outside Terraform).

## How it works

- **Terraform state:** `bootstrap/` creates a versioned, encrypted bucket with
  public access blocked. `infra/` uses the S3 backend with `use_lockfile = true`
  (no DynamoDB). The bucket name is passed with `-backend-config`, because
  backend blocks cannot use variables.
- **Bucket:** one bucket with prefixes `incoming/` and `results/`, public
  access blocked, SSE-S3 encryption.
- **Notification:** S3 sends `ObjectCreated` events for the `incoming/` prefix
  only (so writing results does not trigger the pipeline again). The queue
  policy allows only `s3.amazonaws.com` from this bucket and this account.
- **Queue and DLQ:** `maxReceiveCount = 3`. The worker deletes a message only
  after successful processing. On any error (for example, an empty file) it
  leaves the message, SQS makes it visible again after the visibility timeout,
  and after the third receive moves it to the DLQ. Nothing is lost and nothing
  loops forever.
- **Configuration:** nothing is hardcoded. The region is a variable, the
  account ID comes from `aws_caller_identity`, ARNs come from resource
  attributes. Tags `project`, `owner`, `environment` are set with
  `default_tags` (the instance volume gets them through `volume_tags`).
- **Outputs:** bucket name, queue URL, DLQ URL, worker instance ID.

## Worker choice: EC2 t3.micro

**Why EC2.** The worker is a small Python service (`worker/worker.py`) that
long-polls SQS and runs under systemd on one t3.micro instance. EC2 t3.micro
is one of the allowed options. I first planned to use Lambda, but on my
account the Lambda API returned `AccessDeniedException`, so I moved the worker
to EC2.

**Design.**
- Own IAM role (instance profile) with access only to the exact resources:
  `s3:GetObject` on `<bucket>/incoming/*`, `s3:PutObject` on `<bucket>/results/*`,
  receive/delete/get-attributes on this queue, and log writes to this log group.
- Security group with no inbound rules; outbound HTTPS (443) only.
- IMDSv2 required, encrypted gp3 root volume.
- No NAT Gateway and no interface VPC endpoints (not allowed). The instance
  sits in a default-VPC public subnet with a public IP so it can reach S3, SQS
  and CloudWatch Logs.
- Logs go to CloudWatch Logs (retention 7 days), written by the worker itself.

**What I would pick for production.** Lambda with an SQS event source mapping
for small, short jobs (no servers to patch, scales to zero, built-in retry
and DLQ behaviour). For heavier or longer processing, ECS on a managed
capacity provider, or an Auto Scaling group with a scaling policy based on
queue depth.

## Expected monthly cost

Estimates only; check the AWS pricing pages for the current numbers.

| Item | Estimate |
|------|----------|
| EC2 t3.micro, eu-central-1 | about $0.01 per hour |
| Public IPv4 address | about $0.005 per hour |
| 8 GB gp3 volume | under $1 per month |
| S3, SQS, CloudWatch Logs at test volume | cents, inside free tiers |

If the instance runs all month, this is more than the $1 budget (roughly
$8-10, unless the account has free-tier or credit coverage). So the setup is
meant to be applied for the test and the call, and destroyed afterwards.
A few hours of testing costs cents.

## Usage limits and cost guardrails

- **Worker cap:** exactly one `aws_instance`. No Auto Scaling group, so the
  worker cannot scale out.
- **Retention:** SQS retention 1 day (DLQ 4 days), S3 lifecycle deletes objects
  after 3 days (and aborts incomplete multipart uploads after 1 day), log
  retention 7 days.
- **Budget:** AWS Budget of $1 per month, created in Terraform, email alerts
  at 50% and 100% of actual cost.
- **Budget action:** at 100% an `APPLY_IAM_POLICY` action attaches a deny-all
  policy to the worker role. The worker then gets `AccessDenied` on SQS and S3
  and cannot do any more work. It logs the error and retries every 30 seconds
  instead of hammering the API.
- **How to test the budget action:** temporarily set `budget_limit_usd` to a
  tiny value (for example `0.01`) and wait for the budget to update (budgets
  refresh a few times per day, so it is not instant), then check that the
  policy is attached to the worker role. A faster check of the effect:
  attach the deny policy to the role by hand, upload a file and look for
  `AccessDenied` in the worker logs. Detach it afterwards.
- **Not used:** NAT Gateway, load balancers, RDS/Aurora, EKS, Fargate,
  SageMaker endpoints, interface VPC endpoints, Elastic IPs, customer-managed
  KMS keys.

## What I would change before production

- Lambda (or ECS / Auto Scaling group with queue-depth scaling) instead of a
  single instance; no single point of failure.
- Private subnets with VPC gateway endpoints for S3 and the needed interface
  endpoints, instead of a public IP.
- Customer-managed KMS keys for S3, SQS and logs, with key policies.
- CloudWatch alarms on DLQ depth and queue age, plus a redrive process for
  failed messages; structured logs.
- Idempotent processing and a size limit for input files.
- CI/CD (GitHub Actions with OIDC, plan on pull requests, apply behind
  approval), modules, separate environments and accounts.
- A narrower IAM policy for the user or role that runs Terraform instead of
  administrator access; S3 access logging and bucket versioning for results.

## AI tools used

I used an AI assistant (Claude) to understand the task, to plan the solution
step by step, and to write the code (Terraform and the worker). I did not
accept the generated code blindly: I went through the resources and the worker
logic to understand them.

What I checked by hand:
- <fill in honestly: for example `terraform plan` output, IAM permissions,
  that an empty file reaches the DLQ, that `destroy` leaves nothing behind>

## Test status

<fill in honestly: what you actually ran and what the result was, with the
screenshots of the Budget and of one processed file in `results/`, and the
output of your last `terraform destroy`>