from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import nbformat

from .config import RunnerPolicy
from .pricing import estimate_sagemaker_cost


@dataclass(frozen=True)
class NotebookSummary:
    path: str
    size_bytes: int
    sha256: str
    cell_count: int
    code_cell_count: int
    parameter_cell_count: int
    output_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "cell_count": self.cell_count,
            "code_cell_count": self.code_cell_count,
            "parameter_cell_count": self.parameter_cell_count,
            "output_count": self.output_count,
        }


def summarize_notebook(policy: RunnerPolicy, relative_path: str) -> NotebookSummary:
    path = policy.resolve_notebook(relative_path)
    content = path.read_bytes()
    notebook = nbformat.read(path, as_version=4)
    parameter_cells = 0
    output_count = 0
    code_cells = 0
    for cell in notebook.cells:
        tags = cell.get("metadata", {}).get("tags", [])
        if "parameters" in tags:
            parameter_cells += 1
        if cell.cell_type == "code":
            code_cells += 1
            output_count += len(cell.get("outputs", []))
    return NotebookSummary(
        path=relative_path,
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        cell_count=len(notebook.cells),
        code_cell_count=code_cells,
        parameter_cell_count=parameter_cells,
        output_count=output_count,
    )


def build_run_plan(
    policy: RunnerPolicy,
    local_path: str,
    job_name: str,
    image_uri: str,
    kernel_name: str = "python3",
    instance_type: str | None = None,
    max_runtime_seconds: int | None = None,
    parameters: dict[str, Any] | None = None,
    cleanup: bool | None = None,
) -> dict[str, Any]:
    selected_instance = instance_type or policy.default_instance_type
    runtime = max_runtime_seconds or policy.max_runtime_seconds
    warnings = policy.validate_run_request(selected_instance, runtime)
    summary = summarize_notebook(policy, local_path)
    estimate = estimate_sagemaker_cost(selected_instance, runtime)
    if estimate.warning:
        warnings.append(estimate.warning)
    if (
        estimate.max_compute_cost_usd is not None
        and estimate.max_compute_cost_usd > policy.max_estimated_cost_usd
    ):
        warnings.append(
            f"Estimated compute cost ${estimate.max_compute_cost_usd:.4f} exceeds "
            f"policy cap ${policy.max_estimated_cost_usd:.4f}."
        )
    s3_prefix = policy.s3_root_uri.rstrip("/")
    staged_notebook = (
        f"{s3_prefix}/input/{Path(local_path).name}" if policy.s3_root_uri else None
    )
    output_prefix = f"{s3_prefix}/output/{job_name}" if policy.s3_root_uri else None
    return {
        "mode": "dry_run" if not policy.execution_enabled else "execution_enabled",
        "will_start_compute": False,
        "reason": (
            "Execution is disabled by default. Set AWS_NOTEBOOK_RUNNER_ENABLE_EXECUTION=true "
            "only after reviewing the dry-run plan and IAM/S3 policy."
        )
        if not policy.execution_enabled
        else "Execution mode is enabled, but this prototype still exposes dry-run planning only.",
        "policy": policy.as_dict(),
        "notebook": summary.as_dict(),
        "job": {
            "job_name": job_name,
            "pipeline_name": f"{job_name}-pipeline",
            "step_name": f"{job_name}-step",
            "image_uri": image_uri,
            "kernel_name": kernel_name,
            "instance_type": selected_instance,
            "max_runtime_seconds": runtime,
            "volume_size_gb": policy.volume_size_gb,
            "role_arn_configured": bool(policy.role_arn),
            "role_arn": policy.role_arn,
            "s3_root_uri": policy.s3_root_uri,
            "local_notebook_path": str(policy.resolve_notebook(local_path)),
            "s3_input_notebook": staged_notebook,
            "s3_output_prefix": output_prefix,
            "parameters": parameters or {},
            "cleanup_after_success": policy.cleanup_default if cleanup is None else cleanup,
            "tags": [
                {"Key": "created-by", "Value": "aws-notebook-runner-mcp"},
                {"Key": "job-name", "Value": job_name},
            ],
        },
        "cost_estimate": estimate.as_dict(),
        "planned_steps": [
            "Validate notebook stays under AWS_NOTEBOOK_RUNNER_ROOT.",
            "Upload notebook to the configured S3 root.",
            "Create a SageMaker NotebookJobStep in a one-step SageMaker Pipeline.",
            "Start the pipeline execution.",
            "Poll SageMaker pipeline/training status and CloudWatch logs.",
            "Download output notebook and artifacts from S3.",
            "Optionally remove temporary S3 artifacts created by this run.",
        ],
        "warnings": warnings,
    }
