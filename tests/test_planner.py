from pathlib import Path
from datetime import UTC, datetime, timedelta

import nbformat
import pytest

from aws_notebook_runner_mcp.config import RunnerPolicy
from aws_notebook_runner_mcp.ec2_backend import (
    EC2_START_CONFIRMATION_TOKEN,
    RUN_PHASE_PROGRESS,
    _estimate_current_cost,
    _progress_summary,
    assert_can_start_ec2,
    build_ec2_smoke_plan,
)
from aws_notebook_runner_mcp.planner import build_run_plan, summarize_notebook
from aws_notebook_runner_mcp.pricing import estimate_sagemaker_cost
from aws_notebook_runner_mcp.sagemaker_backend import (
    START_CONFIRMATION_TOKEN,
    assert_can_start,
    build_notebook_job_spec,
)


def policy(tmp_path: Path) -> RunnerPolicy:
    return RunnerPolicy(
        local_root=tmp_path,
        aws_profile="",
        aws_region="us-east-1",
        s3_root_uri="s3://example-bucket/runs",
        role_arn="arn:aws:iam::123456789012:role/SageMakerNotebookRunner",
        default_instance_type="ml.m5.large",
        allowed_instance_types=("ml.m5.large",),
        max_runtime_seconds=3600,
        execution_enabled=False,
        cleanup_default=True,
        backend="sagemaker_notebook_job",
        max_estimated_cost_usd=10.0,
        volume_size_gb=30,
    )


def write_notebook(path: Path) -> None:
    notebook = nbformat.v4.new_notebook(
        cells=[
            nbformat.v4.new_code_cell(
                "alpha = 1", metadata={"tags": ["parameters"]}
            ),
            nbformat.v4.new_markdown_cell("done"),
        ]
    )
    nbformat.write(notebook, path)


def test_summarize_notebook(tmp_path: Path) -> None:
    write_notebook(tmp_path / "demo.ipynb")

    summary = summarize_notebook(policy(tmp_path), "demo.ipynb")

    assert summary.cell_count == 2
    assert summary.code_cell_count == 1
    assert summary.parameter_cell_count == 1
    assert len(summary.sha256) == 64


def test_build_run_plan_is_dry_run(tmp_path: Path) -> None:
    write_notebook(tmp_path / "demo.ipynb")

    plan = build_run_plan(
        policy(tmp_path),
        local_path="demo.ipynb",
        job_name="demo-job",
        image_uri="123456789012.dkr.ecr.us-east-1.amazonaws.com/notebook:latest",
    )

    assert plan["mode"] == "dry_run"
    assert plan["will_start_compute"] is False
    assert plan["job"]["s3_input_notebook"] == "s3://example-bucket/runs/input/demo.ipynb"
    assert plan["job"]["s3_output_prefix"] == "s3://example-bucket/runs/output/demo-job"
    assert plan["cost_estimate"]["max_compute_cost_usd"] == 0.115


def test_disallows_unapproved_instance_type(tmp_path: Path) -> None:
    write_notebook(tmp_path / "demo.ipynb")

    with pytest.raises(ValueError, match="allowlisted"):
        build_run_plan(
            policy(tmp_path),
            local_path="demo.ipynb",
            job_name="demo-job",
            image_uri="image",
            instance_type="ml.p4d.24xlarge",
        )


def test_path_must_stay_inside_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="inside"):
        summarize_notebook(policy(tmp_path), "../demo.ipynb")


def test_policy_parses_s3_root() -> None:
    assert policy(Path("/tmp")).s3_root_parts() == ("example-bucket", "runs")


def test_estimate_sagemaker_cost_known_instance() -> None:
    estimate = estimate_sagemaker_cost("ml.m5.large", 7200)

    assert estimate.hourly_price_usd == 0.115
    assert estimate.max_compute_cost_usd == 0.23


def test_build_sagemaker_notebook_job_spec(tmp_path: Path) -> None:
    write_notebook(tmp_path / "demo.ipynb")
    plan = build_run_plan(
        policy(tmp_path),
        local_path="demo.ipynb",
        job_name="demo-job",
        image_uri="123456789012.dkr.ecr.us-east-1.amazonaws.com/notebook:latest",
    )

    spec = build_notebook_job_spec(plan)

    assert spec["sdk_class"] == "sagemaker.workflow.notebook_job_step.NotebookJobStep"
    assert spec["notebook_job_name"] == "demo-job"
    assert spec["instance_type"] == "ml.m5.large"
    assert spec["max_runtime_in_seconds"] == 3600


def test_start_guard_refuses_when_execution_disabled(tmp_path: Path) -> None:
    write_notebook(tmp_path / "demo.ipynb")
    run_plan = build_run_plan(
        policy(tmp_path),
        local_path="demo.ipynb",
        job_name="demo-job",
        image_uri="image",
    )

    with pytest.raises(RuntimeError, match="Paid compute is disabled"):
        assert_can_start(policy(tmp_path), run_plan, START_CONFIRMATION_TOKEN)


def test_start_guard_requires_confirmation_token(tmp_path: Path) -> None:
    write_notebook(tmp_path / "demo.ipynb")
    enabled = RunnerPolicy(
        local_root=tmp_path,
        aws_profile="",
        aws_region="us-east-1",
        s3_root_uri="s3://example-bucket/runs",
        role_arn="arn:aws:iam::123456789012:role/SageMakerNotebookRunner",
        default_instance_type="ml.m5.large",
        allowed_instance_types=("ml.m5.large",),
        max_runtime_seconds=3600,
        execution_enabled=True,
        cleanup_default=True,
        backend="sagemaker_notebook_job",
        max_estimated_cost_usd=10.0,
        volume_size_gb=30,
    )
    run_plan = build_run_plan(
        enabled,
        local_path="demo.ipynb",
        job_name="demo-job",
        image_uri="image",
    )

    with pytest.raises(RuntimeError, match="confirmation_token"):
        assert_can_start(enabled, run_plan, "wrong")


def test_build_ec2_smoke_plan(tmp_path: Path) -> None:
    write_notebook(tmp_path / "demo.ipynb")
    ec2_policy = RunnerPolicy(
        local_root=tmp_path,
        aws_profile="",
        aws_region="us-east-1",
        s3_root_uri="s3://example-bucket/runs",
        role_arn="",
        default_instance_type="t3.micro",
        allowed_instance_types=("t3.micro",),
        max_runtime_seconds=1800,
        execution_enabled=False,
        cleanup_default=True,
        backend="ec2_ssm",
        max_estimated_cost_usd=1.0,
        volume_size_gb=8,
    )

    plan = build_ec2_smoke_plan(ec2_policy, "demo.ipynb", "smoke", max_runtime_seconds=900)

    assert plan["backend"] == "ec2_ssm"
    assert plan["will_start_compute"] is False
    assert plan["run"]["s3_input_notebook"] == "s3://example-bucket/runs/ec2/smoke/input/demo.ipynb"
    assert plan["cost_estimate"]["instance_type"] == "t3.micro"


def test_start_ec2_guard_refuses_when_execution_disabled(tmp_path: Path) -> None:
    write_notebook(tmp_path / "demo.ipynb")
    ec2_policy = RunnerPolicy(
        local_root=tmp_path,
        aws_profile="",
        aws_region="us-east-1",
        s3_root_uri="s3://example-bucket/runs",
        role_arn="arn:aws:iam::123456789012:role/EC2NotebookRunnerRole",
        default_instance_type="t3.micro",
        allowed_instance_types=("t3.micro",),
        max_runtime_seconds=1800,
        execution_enabled=False,
        cleanup_default=True,
        backend="ec2_ssm",
        max_estimated_cost_usd=1.0,
        volume_size_gb=8,
    )
    run_plan = build_ec2_smoke_plan(ec2_policy, "demo.ipynb", "smoke")

    with pytest.raises(RuntimeError, match="Paid EC2 compute is disabled"):
        assert_can_start_ec2(ec2_policy, run_plan, EC2_START_CONFIRMATION_TOKEN)


def test_start_ec2_guard_requires_confirmation_token(tmp_path: Path) -> None:
    write_notebook(tmp_path / "demo.ipynb")
    ec2_policy = RunnerPolicy(
        local_root=tmp_path,
        aws_profile="",
        aws_region="us-east-1",
        s3_root_uri="s3://example-bucket/runs",
        role_arn="arn:aws:iam::123456789012:role/EC2NotebookRunnerRole",
        default_instance_type="t3.micro",
        allowed_instance_types=("t3.micro",),
        max_runtime_seconds=1800,
        execution_enabled=True,
        cleanup_default=True,
        backend="ec2_ssm",
        max_estimated_cost_usd=1.0,
        volume_size_gb=8,
    )
    run_plan = build_ec2_smoke_plan(ec2_policy, "demo.ipynb", "smoke")

    with pytest.raises(RuntimeError, match="confirmation_token"):
        assert_can_start_ec2(ec2_policy, run_plan, "wrong")


def test_ec2_phase_progress_is_monotonic() -> None:
    phases = [
        "created",
        "staged",
        "launching",
        "waiting_ssm",
        "installing",
        "executing",
        "uploading",
        "completed",
    ]

    values = [RUN_PHASE_PROGRESS[phase] for phase in phases]

    assert values == sorted(values)
    assert RUN_PHASE_PROGRESS["completed"] == 100
    assert RUN_PHASE_PROGRESS["failed"] == 100


def test_ec2_current_cost_uses_sixty_second_minimum() -> None:
    launch_time = datetime.now(UTC) - timedelta(seconds=10)

    estimate = _estimate_current_cost("t3.micro", launch_time, datetime.now(UTC))

    assert estimate["billable_seconds_estimate"] == 60
    assert estimate["compute_cost_usd_estimate"] > 0


def test_progress_summary_reports_elapsed_and_remaining_time() -> None:
    now = datetime(2026, 1, 1, 12, 10, tzinfo=UTC)
    created_at = now - timedelta(minutes=10)
    launch_time = now - timedelta(minutes=5)

    summary = _progress_summary(
        phase="installing",
        progress_percent=50,
        created_at=created_at,
        launch_time=launch_time,
        max_runtime_seconds=900,
        terminal=False,
        now=now,
    )

    assert summary["elapsed_wall_seconds"] == 600
    assert summary["elapsed_compute_seconds"] == 300
    assert summary["remaining_runtime_budget_seconds"] == 600
    assert summary["eta_seconds_estimate"] == 300
    assert "50% installing" in summary["summary"]


def test_progress_summary_terminal_eta_is_zero() -> None:
    now = datetime(2026, 1, 1, 12, 10, tzinfo=UTC)

    summary = _progress_summary(
        phase="completed",
        progress_percent=100,
        created_at=now - timedelta(minutes=10),
        launch_time=now - timedelta(minutes=5),
        max_runtime_seconds=900,
        terminal=True,
        now=now,
    )

    assert summary["eta_seconds_estimate"] == 0
    assert summary["eta_basis"] == "terminal"
