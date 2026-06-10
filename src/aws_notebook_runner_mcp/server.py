from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from .config import RunnerPolicy
from .ec2_backend import (
    EC2_START_CONFIRMATION_TOKEN,
    build_ec2_smoke_plan,
    get_ec2_smoke_run_status as get_ec2_status,
    read_ec2_setup,
)
from .planner import build_run_plan, summarize_notebook
from .pricing import estimate_ec2_cost, estimate_sagemaker_cost
from .sagemaker_backend import (
    START_CONFIRMATION_TOKEN,
    build_notebook_job_spec,
    dependency_status,
    get_pipeline_execution_status,
    start_notebook_job,
)


MCP_INSTRUCTIONS = """
This server is a local prototype for planning guarded AWS SageMaker notebook
job execution. It does not create paid AWS compute by default. Treat all tools
as dry-run/safety tools unless execution is explicitly enabled by environment
policy and the user has approved the run. Never edit IAM, broaden permissions,
launch arbitrary EC2, access unallowlisted S3 locations, or delete resources
outside the run plan.
""".strip()

mcp = FastMCP("aws-notebook-runner", instructions=MCP_INSTRUCTIONS)


def _policy() -> RunnerPolicy:
    return RunnerPolicy.from_env()


@mcp.tool()
def get_runner_status() -> dict[str, Any]:
    """Return local policy and dependency status without contacting AWS."""
    policy = _policy()
    return {
        "ready_for_dry_run": True,
        "ready_for_execution": (
            policy.execution_enabled and bool(policy.s3_root_uri) and bool(policy.role_arn)
        ),
        "policy": policy.as_dict(),
        "dependencies": dependency_status(),
        "message": (
            "Dry-run planning is available. Real AWS execution is intentionally disabled "
            "in this prototype."
        ),
    }


@mcp.tool()
def inspect_notebook(local_path: str) -> dict[str, Any]:
    """Inspect a local notebook under AWS_NOTEBOOK_RUNNER_ROOT."""
    return summarize_notebook(_policy(), local_path).as_dict()


@mcp.tool()
def plan_notebook_job(
    local_path: str,
    job_name: str,
    image_uri: str,
    kernel_name: str = "python3",
    instance_type: str | None = None,
    max_runtime_seconds: int | None = None,
    parameters: dict[str, Any] | None = None,
    cleanup: bool | None = None,
) -> dict[str, Any]:
    """Build a dry-run SageMaker notebook job plan; does not start AWS compute."""
    return build_run_plan(
        _policy(),
        local_path=local_path,
        job_name=job_name,
        image_uri=image_uri,
        kernel_name=kernel_name,
        instance_type=instance_type,
        max_runtime_seconds=max_runtime_seconds,
        parameters=parameters,
        cleanup=cleanup,
    )


@mcp.tool()
def estimate_notebook_job_cost(
    instance_type: str | None = None, max_runtime_seconds: int | None = None
) -> dict[str, Any]:
    """Estimate SageMaker notebook job compute cost from static or configured prices."""
    policy = _policy()
    selected_instance = instance_type or policy.default_instance_type
    runtime = max_runtime_seconds or policy.max_runtime_seconds
    policy.validate_run_request(selected_instance, runtime)
    estimate = (
        estimate_ec2_cost(selected_instance, runtime)
        if policy.backend == "ec2_ssm"
        else estimate_sagemaker_cost(selected_instance, runtime)
    )
    return {
        "estimate": estimate.as_dict(),
        "policy_cap_usd": policy.max_estimated_cost_usd,
        "within_policy_cap": (
            estimate.max_compute_cost_usd is None
            or estimate.max_compute_cost_usd <= policy.max_estimated_cost_usd
        ),
    }


@mcp.tool()
def check_ec2_setup() -> dict[str, Any]:
    """Read-only EC2/SSM setup checks; does not launch instances."""
    return read_ec2_setup(_policy())


@mcp.tool()
def plan_ec2_smoke_run(
    local_path: str,
    run_name: str,
    instance_type: str | None = None,
    max_runtime_seconds: int | None = None,
    ami_id: str | None = None,
    subnet_id: str | None = None,
    security_group_id: str | None = None,
    instance_profile_name: str | None = None,
) -> dict[str, Any]:
    """Build a dry-run EC2+SSM notebook smoke-run plan; does not launch EC2."""
    return build_ec2_smoke_plan(
        _policy(),
        local_path=local_path,
        run_name=run_name,
        instance_type=instance_type,
        max_runtime_seconds=max_runtime_seconds,
        ami_id=ami_id,
        subnet_id=subnet_id,
        security_group_id=security_group_id,
        instance_profile_name=instance_profile_name,
    )


@mcp.tool()
def start_ec2_smoke_run(
    local_path: str,
    run_name: str,
    confirmation_token: str,
    instance_type: str | None = None,
    max_runtime_seconds: int | None = None,
    ami_id: str | None = None,
    subnet_id: str | None = None,
    security_group_id: str | None = None,
    instance_profile_name: str | None = None,
) -> dict[str, Any]:
    """Start a guarded paid EC2+SSM smoke run and terminate the instance after completion."""
    from .ec2_backend import start_ec2_smoke_run as start_run

    return start_run(
        _policy(),
        local_path=local_path,
        run_name=run_name,
        confirmation_token=confirmation_token,
        instance_type=instance_type,
        max_runtime_seconds=max_runtime_seconds,
        ami_id=ami_id,
        subnet_id=subnet_id,
        security_group_id=security_group_id,
        instance_profile_name=instance_profile_name,
    )


@mcp.tool()
def start_ec2_smoke_run_async(
    local_path: str,
    run_name: str,
    confirmation_token: str,
    instance_type: str | None = None,
    max_runtime_seconds: int | None = None,
    ami_id: str | None = None,
    subnet_id: str | None = None,
    security_group_id: str | None = None,
    instance_profile_name: str | None = None,
) -> dict[str, Any]:
    """Start a guarded paid EC2+SSM smoke run and return immediately with run ids."""
    from .ec2_backend import start_ec2_smoke_run_async as start_run

    return start_run(
        _policy(),
        local_path=local_path,
        run_name=run_name,
        confirmation_token=confirmation_token,
        instance_type=instance_type,
        max_runtime_seconds=max_runtime_seconds,
        ami_id=ami_id,
        subnet_id=subnet_id,
        security_group_id=security_group_id,
        instance_profile_name=instance_profile_name,
    )


@mcp.tool()
def get_ec2_smoke_run_status(
    run_name: str,
    include_cloudwatch: bool = True,
    terminate_if_finished: bool = True,
) -> dict[str, Any]:
    """Return EC2/SSM/S3 progress, CloudWatch metrics, and current cost estimate."""
    return get_ec2_status(
        _policy(),
        run_name=run_name,
        include_cloudwatch=include_cloudwatch,
        terminate_if_finished=terminate_if_finished,
    )


@mcp.tool()
def get_sagemaker_notebook_job_spec(
    local_path: str,
    job_name: str,
    image_uri: str,
    kernel_name: str = "python3",
    instance_type: str | None = None,
    max_runtime_seconds: int | None = None,
    parameters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the SageMaker NotebookJobStep spec for a dry-run plan."""
    plan = build_run_plan(
        _policy(),
        local_path=local_path,
        job_name=job_name,
        image_uri=image_uri,
        kernel_name=kernel_name,
        instance_type=instance_type,
        max_runtime_seconds=max_runtime_seconds,
        parameters=parameters,
    )
    return {"plan": plan, "sagemaker_notebook_job_step": build_notebook_job_spec(plan)}


@mcp.tool()
def start_sagemaker_notebook_job(
    local_path: str,
    job_name: str,
    image_uri: str,
    confirmation_token: str,
    kernel_name: str = "python3",
    instance_type: str | None = None,
    max_runtime_seconds: int | None = None,
    parameters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Start paid SageMaker notebook execution only when policy and token allow it."""
    policy = _policy()
    plan = build_run_plan(
        policy,
        local_path=local_path,
        job_name=job_name,
        image_uri=image_uri,
        kernel_name=kernel_name,
        instance_type=instance_type,
        max_runtime_seconds=max_runtime_seconds,
        parameters=parameters,
    )
    return start_notebook_job(policy, plan, confirmation_token)


@mcp.tool()
def get_sagemaker_job_status(pipeline_execution_arn: str) -> dict[str, Any]:
    """Read SageMaker pipeline execution status for a started notebook job."""
    return get_pipeline_execution_status(_policy(), pipeline_execution_arn)


@mcp.tool()
def explain_existing_aws_options() -> dict[str, Any]:
    """Explain related AWS/AWS Labs tools and how this prototype differs."""
    return {
        "sagemaker_notebook_jobs": {
            "does": (
                "Runs Jupyter notebooks as noninteractive SageMaker jobs, on demand "
                "or on a schedule, using SageMaker pipelines/training infrastructure."
            ),
            "overlap": "This prototype plans that lifecycle but adds MCP-facing guardrails.",
        },
        "aws_api_mcp_server": {
            "does": "General AWS CLI/API access for AI assistants across AWS services.",
            "overlap": (
                "It can probably call the raw APIs, but it is broad. This prototype is "
                "narrow and policy-first."
            ),
        },
        "sagemaker_ai_mcp_server": {
            "does": "AWS Labs MCP focused on SageMaker AI resource management, currently HyperPod.",
            "overlap": "Not a focused notebook job runner lifecycle.",
        },
        "aws_samples_sagemaker_run_notebook": {
            "does": "CLI/library/JupyterLab extension for running notebooks with SageMaker jobs.",
            "overlap": "Similar execution target, but not an MCP server for AI clients.",
        },
        "start_confirmation_token": START_CONFIRMATION_TOKEN,
        "ec2_start_confirmation_token": EC2_START_CONFIRMATION_TOKEN,
    }


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
