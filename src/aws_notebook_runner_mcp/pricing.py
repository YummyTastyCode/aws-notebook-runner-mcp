from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any


DEFAULT_SAGEMAKER_PRICES_USD_PER_HOUR = {
    # Point-in-time public examples. Override with AWS_NOTEBOOK_PRICE_OVERRIDES_JSON
    # before relying on these for real launch decisions.
    "ml.m5.large": 0.115,
    "ml.m5.xlarge": 0.23,
    "ml.m5.2xlarge": 0.46,
    "ml.m5.4xlarge": 0.922,
    "ml.g4dn.xlarge": 0.736,
    "ml.g5.xlarge": 1.006,
}

DEFAULT_EC2_PRICES_USD_PER_HOUR = {
    # Conservative on-demand examples for planning. Override before real launches.
    "t3.micro": 0.0104,
    "t3.small": 0.0208,
    "t3.medium": 0.0416,
    "t4g.micro": 0.0084,
    "t4g.small": 0.0168,
    "m7i.large": 0.1008,
    "r7i.large": 0.141,
}


@dataclass(frozen=True)
class CostEstimate:
    backend: str
    instance_type: str
    hourly_price_usd: float | None
    max_runtime_seconds: int
    max_compute_cost_usd: float | None
    pricing_source: str
    warning: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "instance_type": self.instance_type,
            "hourly_price_usd": self.hourly_price_usd,
            "max_runtime_seconds": self.max_runtime_seconds,
            "max_runtime_hours": round(self.max_runtime_seconds / 3600, 4),
            "max_compute_cost_usd": self.max_compute_cost_usd,
            "pricing_source": self.pricing_source,
            "warning": self.warning,
        }


def _price_table() -> tuple[dict[str, float], str]:
    override = os.environ.get("AWS_NOTEBOOK_PRICE_OVERRIDES_JSON", "")
    if not override:
        return dict(DEFAULT_SAGEMAKER_PRICES_USD_PER_HOUR), "built_in_static_examples"
    payload = json.loads(override)
    return {str(key): float(value) for key, value in payload.items()}, "env_override"


def _ec2_price_table() -> tuple[dict[str, float], str]:
    override = os.environ.get("AWS_NOTEBOOK_EC2_PRICE_OVERRIDES_JSON", "")
    if not override:
        return dict(DEFAULT_EC2_PRICES_USD_PER_HOUR), "built_in_static_examples"
    payload = json.loads(override)
    return {str(key): float(value) for key, value in payload.items()}, "env_override"


def estimate_sagemaker_cost(instance_type: str, max_runtime_seconds: int) -> CostEstimate:
    prices, source = _price_table()
    hourly = prices.get(instance_type)
    if hourly is None:
        return CostEstimate(
            backend="sagemaker_notebook_job",
            instance_type=instance_type,
            hourly_price_usd=None,
            max_runtime_seconds=max_runtime_seconds,
            max_compute_cost_usd=None,
            pricing_source=source,
            warning=(
                "No static price is configured for this instance type. Verify current "
                "SageMaker training pricing before launch."
            ),
        )
    return CostEstimate(
        backend="sagemaker_notebook_job",
        instance_type=instance_type,
        hourly_price_usd=hourly,
        max_runtime_seconds=max_runtime_seconds,
        max_compute_cost_usd=round(hourly * max_runtime_seconds / 3600, 4),
        pricing_source=source,
    )


def estimate_ec2_cost(instance_type: str, max_runtime_seconds: int) -> CostEstimate:
    prices, source = _ec2_price_table()
    hourly = prices.get(instance_type)
    if hourly is None:
        return CostEstimate(
            backend="ec2_ssm",
            instance_type=instance_type,
            hourly_price_usd=None,
            max_runtime_seconds=max_runtime_seconds,
            max_compute_cost_usd=None,
            pricing_source=source,
            warning=(
                "No static EC2 price is configured for this instance type. Verify "
                "current EC2 pricing before launch."
            ),
        )
    return CostEstimate(
        backend="ec2_ssm",
        instance_type=instance_type,
        hourly_price_usd=hourly,
        max_runtime_seconds=max_runtime_seconds,
        max_compute_cost_usd=round(hourly * max_runtime_seconds / 3600, 4),
        pricing_source=source,
    )
