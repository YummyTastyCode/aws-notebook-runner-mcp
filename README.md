# AWS Notebook Runner MCP

Run Jupyter notebooks on temporary AWS compute through an MCP server, with
dry-run planning, guardrails, progress reporting, cost estimates, S3 artifacts,
and automatic cleanup.

This is **not Google Colab automation** and it does not bypass provider limits.
It is an AI-facing wrapper for AWS notebook execution. The current working
execution backend is **EC2 + Systems Manager (SSM)**. A SageMaker Notebook Jobs
backend is included for planning and future execution, but it depends on your
AWS account quotas.

This project is not affiliated with, endorsed by, or sponsored by Amazon Web
Services. AWS and Amazon SageMaker are trademarks of Amazon.com, Inc. or its
affiliates.

## What It Can Do

- Inspect a local `.ipynb` under an allowlisted local root.
- Estimate compute cost before launch.
- Build dry-run plans without starting paid compute.
- Start a temporary EC2 instance for a notebook run.
- Execute the notebook through SSM with `nbconvert`.
- Upload the executed notebook and artifacts to S3.
- Report progress, elapsed time, ETA, SSM status, EC2 state, artifacts, and
  current compute cost estimate.
- Terminate the EC2 instance automatically after completion.
- Refuse paid compute unless both an environment flag and confirmation token are
  provided.

## What It Does Not Do

- It does not create or broaden IAM permissions.
- It does not manage arbitrary AWS resources.
- It does not open SSH ports.
- It does not provide exact cell-level progress yet.
- It does not include memory/filesystem metrics unless you add SSM snapshots or
  CloudWatch Agent support.
- It does not make AWS quota requests.

## Install

From PyPI, after publication:

```bash
pip install "aws-notebook-runner-mcp[aws]"
```

From a local checkout:

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[aws,test]"
```

Run the MCP server:

```bash
aws-notebook-runner-mcp
```

## Required AWS Resources

You need:

- An S3 bucket/prefix for notebook inputs, outputs, and status files.
- An IAM user or role for the local MCP server.
- An EC2 instance role/profile for temporary notebook instances.
- A default VPC/subnet or explicit subnet id.
- SSM access; no inbound SSH is required.

The tested setup used:

```text
AWS region: eu-north-1
S3 root: s3://YOUR_BUCKET/runs
EC2 instance profile: EC2NotebookRunnerRole
Instance type: t3.micro
```

## IAM: Local MCP User

Attach a managed policy to the IAM principal used by your local AWS profile.
Keep it scoped to your account and bucket where possible.

Minimal EC2/SSM/S3 policy shape:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "PassNotebookRunnerRole",
      "Effect": "Allow",
      "Action": "iam:PassRole",
      "Resource": "arn:aws:iam::123456789012:role/EC2NotebookRunnerRole"
    },
    {
      "Sid": "EC2NotebookRunnerControl",
      "Effect": "Allow",
      "Action": [
        "ec2:RunInstances",
        "ec2:TerminateInstances",
        "ec2:CreateTags",
        "ec2:DescribeInstances",
        "ec2:DescribeInstanceStatus",
        "ec2:DescribeImages",
        "ec2:DescribeSubnets",
        "ec2:DescribeVpcs",
        "ec2:DescribeSecurityGroups"
      ],
      "Resource": "*"
    },
    {
      "Sid": "SSMNotebookRunnerControl",
      "Effect": "Allow",
      "Action": [
        "ssm:SendCommand",
        "ssm:GetCommandInvocation",
        "ssm:DescribeInstanceInformation"
      ],
      "Resource": "*"
    },
    {
      "Sid": "NotebookRunnerS3Access",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:ListBucket"
      ],
      "Resource": [
        "arn:aws:s3:::YOUR_BUCKET",
        "arn:aws:s3:::YOUR_BUCKET/runs/*"
      ]
    },
    {
      "Sid": "OptionalCloudWatchMetrics",
      "Effect": "Allow",
      "Action": "cloudwatch:GetMetricStatistics",
      "Resource": "*"
    }
  ]
}
```

`cloudwatch:GetMetricStatistics` is optional. Without it, status still works,
but CPU/network/disk I/O metrics are reported as unavailable.

## IAM: EC2 Instance Role

Create an EC2 role, for example `EC2NotebookRunnerRole`, with:

- Trust policy for `ec2.amazonaws.com`.
- AWS managed policy: `AmazonSSMManagedInstanceCore`.
- S3 access to the run prefix.

Example inline S3 policy for the EC2 role:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "NotebookRunnerInstanceS3Access",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject"
      ],
      "Resource": "arn:aws:s3:::YOUR_BUCKET/runs/ec2/*"
    }
  ]
}
```

When you create the role through the AWS console, AWS usually creates an
instance profile with the same name as the role.

## Environment

Core settings:

```bash
export AWS_PROFILE=research
export AWS_REGION=eu-north-1
export AWS_NOTEBOOK_RUNNER_ROOT=/absolute/path/to/local/notebooks
export AWS_NOTEBOOK_S3_ROOT=s3://YOUR_BUCKET/runs
export AWS_NOTEBOOK_BACKEND=ec2_ssm
export AWS_NOTEBOOK_ROLE_ARN=arn:aws:iam::123456789012:role/EC2NotebookRunnerRole
export AWS_NOTEBOOK_ALLOWED_INSTANCE_TYPES=t3.micro,t3.small
export AWS_NOTEBOOK_DEFAULT_INSTANCE_TYPE=t3.micro
export AWS_NOTEBOOK_MAX_RUNTIME_SECONDS=1800
export AWS_NOTEBOOK_MAX_ESTIMATED_COST_USD=1
```

Paid compute is disabled unless you opt in:

```bash
export AWS_NOTEBOOK_RUNNER_ENABLE_EXECUTION=true
```

The MCP caller must also pass:

```text
confirmation_token = START_PAID_EC2_NOTEBOOK_RUN
```

SageMaker execution, when quotas are available, uses:

```text
confirmation_token = START_PAID_SAGEMAKER_NOTEBOOK_JOB
```

## MCP Client Configuration

Example stdio config:

```json
{
  "mcpServers": {
    "aws-notebook-runner": {
      "command": "aws-notebook-runner-mcp",
      "env": {
        "AWS_PROFILE": "research",
        "AWS_REGION": "eu-north-1",
        "AWS_NOTEBOOK_RUNNER_ROOT": "/absolute/path/to/notebooks",
        "AWS_NOTEBOOK_S3_ROOT": "s3://YOUR_BUCKET/runs",
        "AWS_NOTEBOOK_BACKEND": "ec2_ssm",
        "AWS_NOTEBOOK_ROLE_ARN": "arn:aws:iam::123456789012:role/EC2NotebookRunnerRole",
        "AWS_NOTEBOOK_ALLOWED_INSTANCE_TYPES": "t3.micro,t3.small",
        "AWS_NOTEBOOK_DEFAULT_INSTANCE_TYPE": "t3.micro",
        "AWS_NOTEBOOK_MAX_RUNTIME_SECONDS": "1800",
        "AWS_NOTEBOOK_MAX_ESTIMATED_COST_USD": "1"
      }
    }
  }
}
```

Only add `AWS_NOTEBOOK_RUNNER_ENABLE_EXECUTION=true` when you are ready to allow
paid compute, and keep the confirmation token gate.

## Tools

- `get_runner_status`: local policy and dependency status.
- `inspect_notebook`: validate and summarize a local notebook.
- `estimate_notebook_job_cost`: estimate SageMaker or EC2 compute cost.
- `plan_notebook_job`: dry-run SageMaker plan.
- `get_sagemaker_notebook_job_spec`: return a SageMaker `NotebookJobStep` spec.
- `start_sagemaker_notebook_job`: guarded SageMaker execution.
- `get_sagemaker_job_status`: read SageMaker pipeline execution status.
- `check_ec2_setup`: read-only EC2/SSM readiness checks.
- `plan_ec2_smoke_run`: dry-run EC2+SSM plan.
- `start_ec2_smoke_run`: guarded synchronous EC2+SSM run.
- `start_ec2_smoke_run_async`: guarded async EC2+SSM run.
- `get_ec2_smoke_run_status`: EC2/SSM/S3 progress, metrics, artifacts, and cost.
- `explain_existing_aws_options`: related AWS options and overlap.

## Typical EC2 Workflow

1. Inspect the notebook:

```text
inspect_notebook(local_path="notebooks/demo.ipynb")
```

2. Build a dry-run plan:

```text
plan_ec2_smoke_run(
  local_path="notebooks/demo.ipynb",
  run_name="demo-run",
  instance_type="t3.micro",
  max_runtime_seconds=900,
  instance_profile_name="EC2NotebookRunnerRole"
)
```

3. Start async execution:

```text
start_ec2_smoke_run_async(
  local_path="notebooks/demo.ipynb",
  run_name="demo-run",
  confirmation_token="START_PAID_EC2_NOTEBOOK_RUN",
  instance_type="t3.micro",
  max_runtime_seconds=900,
  instance_profile_name="EC2NotebookRunnerRole"
)
```

4. Poll status:

```text
get_ec2_smoke_run_status(run_name="demo-run")
```

Status includes:

- `progress_summary.summary`, for example:
  `70% executing; elapsed wall 4m 44s, compute 4m 42s, ETA 2m 0s`
- EC2 instance state.
- SSM command status.
- stdout/stderr tail.
- S3 artifacts.
- Current compute cost estimate.

## Progress Model

Progress is phase-based:

```text
created -> staged -> launching -> waiting_ssm -> installing -> executing -> uploading -> completed/failed
```

This is useful for UX and cost guardrails, but it is not exact cell-level
progress. A notebook that sleeps for five minutes will remain in `executing`
until it completes unless the notebook itself writes progress markers.

## Cost Model

Cost reporting is an estimate:

- EC2 compute is estimated from instance type, elapsed compute time, and a
  60-second minimum.
- EBS, S3 requests/storage, data transfer, and taxes are not included.
- Static prices can be overridden:

```bash
export AWS_NOTEBOOK_PRICE_OVERRIDES_JSON='{"t3.micro": 0.0104, "ml.m5.large": 0.115}'
```

Always verify official costs in AWS Billing or Cost Explorer.

## SageMaker Notes

SageMaker Notebook Jobs are the more managed AWS-native way to run notebooks,
but new AWS accounts may have a default training-job quota of zero for common
instance types. In that case, EC2+SSM is a practical fallback.

The SageMaker backend is included, but EC2+SSM is the path that has been tested
end-to-end in this package.

## Troubleshooting

`iam:PassRole` denied:

The local AWS principal needs permission to pass the EC2 instance role:

```text
iam:PassRole on arn:aws:iam::<account-id>:role/EC2NotebookRunnerRole
```

SSM command never starts:

- Check that the instance role has `AmazonSSMManagedInstanceCore`.
- Use an Amazon Linux AMI with SSM Agent.
- Ensure the subnet has outbound internet access or VPC endpoints for SSM.

CloudWatch metrics unavailable:

Add `cloudwatch:GetMetricStatistics` to the local AWS principal.

SageMaker fails with quota zero:

Request quota for the selected instance type, or use the EC2+SSM backend.

Inline IAM policy size exceeded:

Use customer managed policies attached to the user/role instead of adding more
inline policies.

## Safety

This server is intentionally conservative:

- Dry-run tools do not start compute.
- Execution requires `AWS_NOTEBOOK_RUNNER_ENABLE_EXECUTION=true`.
- Execution also requires a confirmation token.
- Instance types are allowlisted.
- Max runtime and max estimated cost are policy-controlled.
- EC2 instances are launched with instance-initiated shutdown behavior set to
  terminate.

Review IAM, S3 prefixes, instance allowlists, and cost caps before enabling
execution.
