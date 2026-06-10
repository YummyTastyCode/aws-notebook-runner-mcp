from __future__ import annotations

import importlib.util
from typing import Any

from .config import RunnerPolicy

START_CONFIRMATION_TOKEN = "START_PAID_SAGEMAKER_NOTEBOOK_JOB"


def dependency_status() -> dict[str, Any]:
    return {
        "boto3_installed": importlib.util.find_spec("boto3") is not None,
        "sagemaker_installed": importlib.util.find_spec("sagemaker") is not None,
    }


def build_notebook_job_spec(plan: dict[str, Any]) -> dict[str, Any]:
    job = plan["job"]
    return {
        "sdk_class": "sagemaker.workflow.notebook_job_step.NotebookJobStep",
        "pipeline_name": job["pipeline_name"],
        "step_name": job["step_name"],
        "notebook_job_name": job["job_name"],
        "input_notebook": job["local_notebook_path"],
        "image_uri": job["image_uri"],
        "kernel_name": job["kernel_name"],
        "role": job["role_arn"],
        "s3_root_uri": job["s3_root_uri"],
        "parameters": job["parameters"],
        "instance_type": job["instance_type"],
        "volume_size": job["volume_size_gb"],
        "max_runtime_in_seconds": job["max_runtime_seconds"],
        "tags": job["tags"],
    }


def assert_can_start(
    policy: RunnerPolicy, plan: dict[str, Any], confirmation_token: str
) -> None:
    if not policy.execution_enabled:
        raise RuntimeError(
            "Paid compute is disabled. Set AWS_NOTEBOOK_RUNNER_ENABLE_EXECUTION=true "
            "only after reviewing the dry-run plan."
        )
    if confirmation_token != START_CONFIRMATION_TOKEN:
        raise RuntimeError(
            f"Refusing to start paid compute without confirmation_token="
            f"{START_CONFIRMATION_TOKEN!r}."
        )
    estimate = plan.get("cost_estimate", {})
    max_cost = estimate.get("max_compute_cost_usd")
    if max_cost is not None and max_cost > policy.max_estimated_cost_usd:
        raise RuntimeError(
            f"Estimated compute cost ${max_cost:.4f} exceeds policy cap "
            f"${policy.max_estimated_cost_usd:.4f}."
        )
    deps = dependency_status()
    if not deps["boto3_installed"] or not deps["sagemaker_installed"]:
        raise RuntimeError(
            "AWS execution dependencies are missing. Install with: "
            "pip install 'aws-notebook-runner-mcp[aws]'"
        )


def start_notebook_job(
    policy: RunnerPolicy, plan: dict[str, Any], confirmation_token: str
) -> dict[str, Any]:
    assert_can_start(policy, plan, confirmation_token)

    import boto3
    import sagemaker
    from sagemaker.workflow.notebook_job_step import NotebookJobStep
    from sagemaker.workflow.pipeline import Pipeline

    boto_session = boto3.Session(
        profile_name=policy.aws_profile or None,
        region_name=policy.aws_region,
    )
    default_bucket, default_bucket_prefix = policy.s3_root_parts()
    sagemaker_session = sagemaker.Session(
        boto_session=boto_session,
        default_bucket=default_bucket,
        default_bucket_prefix=default_bucket_prefix,
    )
    spec = build_notebook_job_spec(plan)
    notebook_step = NotebookJobStep(
        name=spec["step_name"],
        notebook_job_name=spec["notebook_job_name"],
        input_notebook=spec["input_notebook"],
        image_uri=spec["image_uri"],
        kernel_name=spec["kernel_name"],
        role=spec["role"],
        s3_root_uri=spec["s3_root_uri"],
        parameters=spec["parameters"],
        instance_type=spec["instance_type"],
        volume_size=spec["volume_size"],
        max_runtime_in_seconds=spec["max_runtime_in_seconds"],
        tags=spec["tags"],
    )
    pipeline = Pipeline(
        name=spec["pipeline_name"],
        steps=[notebook_step],
        sagemaker_session=sagemaker_session,
    )
    pipeline.upsert(role_arn=policy.role_arn)
    execution = pipeline.start()
    return {
        "backend": "sagemaker_notebook_job",
        "pipeline_name": spec["pipeline_name"],
        "pipeline_execution_arn": execution.arn,
        "status": "started",
    }


def get_pipeline_execution_status(policy: RunnerPolicy, pipeline_execution_arn: str) -> dict[str, Any]:
    deps = dependency_status()
    if not deps["boto3_installed"]:
        raise RuntimeError("boto3 is not installed. Install with: pip install '.[aws]'")
    import boto3

    session = boto3.Session(
        profile_name=policy.aws_profile or None,
        region_name=policy.aws_region,
    )
    client = session.client("sagemaker")
    response = client.describe_pipeline_execution(
        PipelineExecutionArn=pipeline_execution_arn
    )
    return {
        "pipeline_execution_arn": pipeline_execution_arn,
        "status": response.get("PipelineExecutionStatus"),
        "display_name": response.get("PipelineExecutionDisplayName"),
        "created_at": response.get("CreationTime"),
        "modified_at": response.get("LastModifiedTime"),
    }
