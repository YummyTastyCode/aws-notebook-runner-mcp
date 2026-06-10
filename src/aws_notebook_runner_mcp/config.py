from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass(frozen=True)
class RunnerPolicy:
    local_root: Path
    aws_profile: str
    aws_region: str
    s3_root_uri: str
    role_arn: str
    default_instance_type: str
    allowed_instance_types: tuple[str, ...]
    max_runtime_seconds: int
    execution_enabled: bool
    cleanup_default: bool
    backend: str
    max_estimated_cost_usd: float
    volume_size_gb: int

    @classmethod
    def from_env(cls) -> "RunnerPolicy":
        local_root = Path(os.environ.get("AWS_NOTEBOOK_RUNNER_ROOT", Path.cwd()))
        allowed = _split_csv(
            os.environ.get("AWS_NOTEBOOK_ALLOWED_INSTANCE_TYPES", "ml.m5.large")
        )
        default_instance = os.environ.get(
            "AWS_NOTEBOOK_DEFAULT_INSTANCE_TYPE", allowed[0] if allowed else "ml.m5.large"
        )
        return cls(
            local_root=local_root.expanduser().resolve(),
            aws_profile=os.environ.get("AWS_PROFILE", ""),
            aws_region=os.environ.get("AWS_REGION", "us-east-1"),
            s3_root_uri=os.environ.get("AWS_NOTEBOOK_S3_ROOT", ""),
            role_arn=os.environ.get("AWS_NOTEBOOK_ROLE_ARN", ""),
            default_instance_type=default_instance,
            allowed_instance_types=tuple(allowed),
            max_runtime_seconds=int(os.environ.get("AWS_NOTEBOOK_MAX_RUNTIME_SECONDS", "7200")),
            execution_enabled=os.environ.get(
                "AWS_NOTEBOOK_RUNNER_ENABLE_EXECUTION", "false"
            ).casefold()
            == "true",
            cleanup_default=os.environ.get("AWS_NOTEBOOK_CLEANUP_DEFAULT", "true").casefold()
            == "true",
            backend=os.environ.get("AWS_NOTEBOOK_BACKEND", "sagemaker_notebook_job"),
            max_estimated_cost_usd=float(
                os.environ.get("AWS_NOTEBOOK_MAX_ESTIMATED_COST_USD", "10.0")
            ),
            volume_size_gb=int(os.environ.get("AWS_NOTEBOOK_VOLUME_SIZE_GB", "30")),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "local_root": str(self.local_root),
            "aws_profile": self.aws_profile,
            "aws_region": self.aws_region,
            "s3_root_uri": self.s3_root_uri,
            "role_arn_configured": bool(self.role_arn),
            "default_instance_type": self.default_instance_type,
            "allowed_instance_types": list(self.allowed_instance_types),
            "max_runtime_seconds": self.max_runtime_seconds,
            "execution_enabled": self.execution_enabled,
            "cleanup_default": self.cleanup_default,
            "backend": self.backend,
            "max_estimated_cost_usd": self.max_estimated_cost_usd,
            "volume_size_gb": self.volume_size_gb,
        }

    def resolve_notebook(self, relative_path: str) -> Path:
        path = (self.local_root / relative_path).resolve()
        if not path.is_relative_to(self.local_root):
            raise ValueError(f"Notebook path must stay inside {self.local_root}")
        if path.suffix.lower() != ".ipynb":
            raise ValueError("Notebook path must end with .ipynb")
        if not path.is_file():
            raise FileNotFoundError(path)
        return path

    def validate_run_request(self, instance_type: str, max_runtime_seconds: int) -> list[str]:
        warnings: list[str] = []
        if instance_type not in self.allowed_instance_types:
            raise ValueError(
                "Instance type is not allowlisted. Configure "
                "AWS_NOTEBOOK_ALLOWED_INSTANCE_TYPES to permit it."
            )
        if max_runtime_seconds > self.max_runtime_seconds:
            raise ValueError(
                f"Requested runtime {max_runtime_seconds}s exceeds policy limit "
                f"{self.max_runtime_seconds}s."
            )
        if not self.s3_root_uri:
            warnings.append("AWS_NOTEBOOK_S3_ROOT is not configured; execution cannot start.")
        if not self.role_arn:
            warnings.append("AWS_NOTEBOOK_ROLE_ARN is not configured; execution cannot start.")
        if self.backend not in {"sagemaker_notebook_job", "ec2_ssm"}:
            raise ValueError(
                "AWS_NOTEBOOK_BACKEND must be sagemaker_notebook_job or ec2_ssm."
            )
        return warnings

    def s3_root_parts(self) -> tuple[str, str]:
        parsed = urlparse(self.s3_root_uri)
        if parsed.scheme != "s3" or not parsed.netloc:
            raise ValueError("AWS_NOTEBOOK_S3_ROOT must be an s3://bucket/prefix URI.")
        return parsed.netloc, parsed.path.strip("/")
