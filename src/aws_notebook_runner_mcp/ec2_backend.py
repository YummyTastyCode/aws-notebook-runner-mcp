from __future__ import annotations

import importlib.util
import json
import shlex
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .config import RunnerPolicy
from .planner import summarize_notebook
from .pricing import estimate_ec2_cost


EC2_START_CONFIRMATION_TOKEN = "START_PAID_EC2_NOTEBOOK_RUN"
SSM_TERMINAL_STATUSES = {"Success", "Cancelled", "TimedOut", "Failed", "Cancelling"}
RUN_PHASE_PROGRESS = {
    "created": 0,
    "staged": 10,
    "launching": 20,
    "waiting_ssm": 35,
    "installing": 50,
    "executing": 70,
    "uploading": 90,
    "completed": 100,
    "failed": 100,
}


def dependency_status() -> dict[str, Any]:
    return {"boto3_installed": importlib.util.find_spec("boto3") is not None}


def build_ec2_smoke_plan(
    policy: RunnerPolicy,
    local_path: str,
    run_name: str,
    instance_type: str | None = None,
    max_runtime_seconds: int | None = None,
    ami_id: str | None = None,
    subnet_id: str | None = None,
    security_group_id: str | None = None,
    instance_profile_name: str | None = None,
) -> dict[str, Any]:
    selected_instance = instance_type or policy.default_instance_type
    runtime = max_runtime_seconds or policy.max_runtime_seconds
    warnings = policy.validate_run_request(selected_instance, runtime)
    notebook = summarize_notebook(policy, local_path)
    estimate = estimate_ec2_cost(selected_instance, runtime)
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
    return {
        "mode": "dry_run" if not policy.execution_enabled else "execution_enabled",
        "backend": "ec2_ssm",
        "will_start_compute": False,
        "reason": (
            "EC2 execution is disabled by default. Set "
            "AWS_NOTEBOOK_RUNNER_ENABLE_EXECUTION=true only after reviewing this plan."
        ),
        "policy": policy.as_dict(),
        "notebook": notebook.as_dict(),
        "run": {
            "run_name": run_name,
            "instance_type": selected_instance,
            "max_runtime_seconds": runtime,
            "ami_id": ami_id,
            "subnet_id": subnet_id,
            "security_group_id": security_group_id,
            "instance_profile_name": instance_profile_name,
            "s3_input_notebook": f"{s3_prefix}/ec2/{run_name}/input/{local_path}",
            "s3_output_prefix": f"{s3_prefix}/ec2/{run_name}/output",
            "cleanup_after_success": policy.cleanup_default,
        },
        "cost_estimate": estimate.as_dict(),
        "planned_steps": [
            "Upload notebook to the allowed S3 prefix.",
            "Launch a temporary EC2 instance with an SSM-capable instance profile.",
            "Use cloud-init or SSM RunCommand to install Python notebook dependencies.",
            "Execute the notebook with nbconvert or papermill.",
            "Upload executed notebook, logs, and artifacts to S3.",
            "Terminate the temporary EC2 instance.",
        ],
        "required_local_user_permissions": [
            "ec2:DescribeRegions",
            "ec2:DescribeVpcs",
            "ec2:DescribeSubnets",
            "ec2:DescribeImages",
            "ec2:DescribeInstances",
            "ec2:RunInstances",
            "ec2:TerminateInstances",
            "ec2:CreateTags",
            "iam:PassRole for the EC2 instance profile role",
            "ssm:SendCommand",
            "ssm:GetCommandInvocation",
            "ssm:DescribeInstanceInformation",
            "cloudwatch:GetMetricStatistics for optional EC2 metrics",
        ],
        "required_instance_role_permissions": [
            "AmazonSSMManagedInstanceCore",
            "s3:GetObject on the run input prefix",
            "s3:PutObject on the run output prefix",
        ],
        "warnings": warnings,
    }


def read_ec2_setup(policy: RunnerPolicy) -> dict[str, Any]:
    deps = dependency_status()
    if not deps["boto3_installed"]:
        return {
            "dependencies": deps,
            "checks": [],
            "ready": False,
            "message": "boto3 is not installed. Install with: pip install '.[aws]'",
        }
    import boto3
    from botocore.exceptions import ClientError

    session = boto3.Session(
        profile_name=policy.aws_profile or None,
        region_name=policy.aws_region,
    )
    checks = []
    for name, factory, call in [
        ("ec2_describe_vpcs", lambda: session.client("ec2"), lambda c: c.describe_vpcs(MaxResults=5)),
        (
            "ssm_describe_instance_information",
            lambda: session.client("ssm"),
            lambda c: c.describe_instance_information(MaxResults=5),
        ),
    ]:
        try:
            call(factory())
            checks.append({"name": name, "ok": True})
        except ClientError as exc:
            checks.append(
                {
                    "name": name,
                    "ok": False,
                    "error_code": exc.response.get("Error", {}).get("Code"),
                    "message": exc.response.get("Error", {}).get("Message"),
                }
            )
    return {
        "dependencies": deps,
        "checks": checks,
        "ready": all(check["ok"] for check in checks),
    }


def assert_can_start_ec2(
    policy: RunnerPolicy, plan: dict[str, Any], confirmation_token: str
) -> None:
    if not policy.execution_enabled:
        raise RuntimeError(
            "Paid EC2 compute is disabled. Set AWS_NOTEBOOK_RUNNER_ENABLE_EXECUTION=true "
            "only after reviewing the dry-run plan."
        )
    if confirmation_token != EC2_START_CONFIRMATION_TOKEN:
        raise RuntimeError(
            f"Refusing to start paid EC2 compute without confirmation_token="
            f"{EC2_START_CONFIRMATION_TOKEN!r}."
        )
    estimate = plan.get("cost_estimate", {})
    max_cost = estimate.get("max_compute_cost_usd")
    if max_cost is not None and max_cost > policy.max_estimated_cost_usd:
        raise RuntimeError(
            f"Estimated compute cost ${max_cost:.4f} exceeds policy cap "
            f"${policy.max_estimated_cost_usd:.4f}."
        )
    deps = dependency_status()
    if not deps["boto3_installed"]:
        raise RuntimeError("boto3 is not installed. Install with: pip install '.[aws]'")


def _latest_al2023_ami(ec2: Any) -> str:
    response = ec2.describe_images(
        Owners=["amazon"],
        Filters=[
            {"Name": "name", "Values": ["al2023-ami-2023*-kernel-6.1-x86_64"]},
            {"Name": "architecture", "Values": ["x86_64"]},
            {"Name": "virtualization-type", "Values": ["hvm"]},
        ],
    )
    images = sorted(response["Images"], key=lambda image: image["CreationDate"])
    if not images:
        raise RuntimeError("No Amazon Linux 2023 x86_64 AMI found in this region.")
    return images[-1]["ImageId"]


def _default_subnet_id(ec2: Any) -> str:
    response = ec2.describe_subnets(
        Filters=[
            {"Name": "default-for-az", "Values": ["true"]},
            {"Name": "state", "Values": ["available"]},
        ]
    )
    subnets = sorted(response["Subnets"], key=lambda subnet: subnet["SubnetId"])
    if not subnets:
        raise RuntimeError("No default available subnet found in this region.")
    return subnets[0]["SubnetId"]


def _wait_for_ssm_instance(ssm: Any, instance_id: str, timeout_seconds: int) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        response = ssm.describe_instance_information(
            Filters=[{"Key": "InstanceIds", "Values": [instance_id]}]
        )
        if response.get("InstanceInformationList"):
            return
        time.sleep(10)
    raise TimeoutError(f"Instance {instance_id} did not register with SSM in time.")


def _wait_for_command(
    ssm: Any, command_id: str, instance_id: str, timeout_seconds: int
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_response: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        try:
            last_response = ssm.get_command_invocation(
                CommandId=command_id,
                InstanceId=instance_id,
            )
        except Exception:
            time.sleep(5)
            continue
        if last_response.get("Status") in SSM_TERMINAL_STATUSES:
            return last_response
        time.sleep(10)
    raise TimeoutError(
        f"SSM command {command_id} did not finish in {timeout_seconds} seconds. "
        f"Last response: {last_response}"
    )


def _manifest_dir(policy: RunnerPolicy) -> Path:
    path = policy.local_root / ".aws-notebook-runs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _manifest_path(policy: RunnerPolicy, run_name: str) -> Path:
    return _manifest_dir(policy) / f"{run_name}.json"


def _write_manifest(policy: RunnerPolicy, manifest: dict[str, Any]) -> None:
    path = _manifest_path(policy, manifest["run_name"])
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True, default=str) + "\n")


def _read_manifest(policy: RunnerPolicy, run_name: str) -> dict[str, Any]:
    path = _manifest_path(policy, run_name)
    if not path.exists():
        raise FileNotFoundError(f"No local EC2 run manifest found for {run_name!r}.")
    return json.loads(path.read_text())


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _progress_key(root_prefix: str, run_name: str) -> str:
    return f"{root_prefix}/ec2/{run_name}/status/progress.json".lstrip("/")


def _read_progress(s3: Any, bucket: str, key: str) -> dict[str, Any] | None:
    try:
        response = s3.get_object(Bucket=bucket, Key=key)
    except Exception:
        return None
    return json.loads(response["Body"].read().decode("utf-8"))


def _list_s3_artifacts(s3: Any, bucket: str, prefix: str) -> list[dict[str, Any]]:
    response = s3.list_objects_v2(Bucket=bucket, Prefix=prefix.rstrip("/") + "/")
    return [
        {
            "key": item["Key"],
            "size": item["Size"],
            "last_modified": item["LastModified"].isoformat(),
        }
        for item in response.get("Contents", [])
    ]


def _estimate_current_cost(
    instance_type: str,
    launch_time: datetime | None,
    end_time: datetime | None = None,
) -> dict[str, Any]:
    estimate = estimate_ec2_cost(instance_type, 3600)
    if launch_time is None or estimate.hourly_price_usd is None:
        return {
            "hourly_price_usd": estimate.hourly_price_usd,
            "elapsed_seconds": None,
            "billable_seconds_estimate": None,
            "compute_cost_usd_estimate": None,
            "pricing_source": estimate.pricing_source,
        }
    stop = end_time or _now()
    elapsed = max(0, int((stop - launch_time).total_seconds()))
    billable = max(60, elapsed)
    return {
        "hourly_price_usd": estimate.hourly_price_usd,
        "elapsed_seconds": elapsed,
        "billable_seconds_estimate": billable,
        "compute_cost_usd_estimate": round(estimate.hourly_price_usd * billable / 3600, 8),
        "pricing_source": estimate.pricing_source,
        "note": "Estimate only. Linux EC2 is generally per-second with a 60 second minimum; excludes EBS, S3, data transfer, and taxes.",
    }


def _format_duration(seconds: int | None) -> str | None:
    if seconds is None:
        return None
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _progress_summary(
    phase: str,
    progress_percent: int,
    created_at: datetime | None,
    launch_time: datetime | None,
    max_runtime_seconds: int | None,
    terminal: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or _now()
    elapsed_wall = (
        max(0, int((current - created_at).total_seconds())) if created_at else None
    )
    elapsed_compute = (
        max(0, int((current - launch_time).total_seconds())) if launch_time else None
    )
    remaining_runtime_budget = None
    if max_runtime_seconds is not None and elapsed_compute is not None:
        remaining_runtime_budget = max(0, max_runtime_seconds - elapsed_compute)
    eta_seconds = None
    eta_basis = "unavailable"
    if terminal:
        eta_seconds = 0
        eta_basis = "terminal"
    elif elapsed_compute is not None and 0 < progress_percent < 100:
        eta_seconds = int(elapsed_compute * (100 - progress_percent) / progress_percent)
        if remaining_runtime_budget is not None:
            eta_seconds = min(eta_seconds, remaining_runtime_budget)
        eta_basis = "phase_progress_estimate"
    elif remaining_runtime_budget is not None:
        eta_seconds = remaining_runtime_budget
        eta_basis = "remaining_runtime_budget"
    summary = (
        f"{progress_percent}% {phase}; "
        f"elapsed wall {_format_duration(elapsed_wall) or 'unknown'}, "
        f"compute {_format_duration(elapsed_compute) or 'not started'}, "
        f"ETA {_format_duration(eta_seconds) or 'unknown'}"
    )
    return {
        "phase": phase,
        "percent_estimate": progress_percent,
        "elapsed_wall_seconds": elapsed_wall,
        "elapsed_wall": _format_duration(elapsed_wall),
        "elapsed_compute_seconds": elapsed_compute,
        "elapsed_compute": _format_duration(elapsed_compute),
        "max_runtime_seconds": max_runtime_seconds,
        "max_runtime": _format_duration(max_runtime_seconds),
        "remaining_runtime_budget_seconds": remaining_runtime_budget,
        "remaining_runtime_budget": _format_duration(remaining_runtime_budget),
        "eta_seconds_estimate": eta_seconds,
        "eta_estimate": _format_duration(eta_seconds),
        "eta_basis": eta_basis,
        "summary": summary,
        "note": "ETA is phase-based, not a precise notebook cell-level estimate.",
    }


def _cloudwatch_metrics(
    cloudwatch: Any,
    instance_id: str,
    launch_time: datetime | None,
) -> dict[str, Any]:
    if launch_time is None:
        return {"available": False, "reason": "launch_time_unknown"}
    end = _now()
    start = min(launch_time, end - timedelta(minutes=30))
    metric_specs = [
        ("CPUUtilization", "Percent", "Average"),
        ("NetworkIn", "Bytes", "Sum"),
        ("NetworkOut", "Bytes", "Sum"),
        ("DiskReadBytes", "Bytes", "Sum"),
        ("DiskWriteBytes", "Bytes", "Sum"),
    ]
    values: dict[str, Any] = {}
    for metric_name, unit, stat in metric_specs:
        try:
            response = cloudwatch.get_metric_statistics(
                Namespace="AWS/EC2",
                MetricName=metric_name,
                Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
                StartTime=start,
                EndTime=end,
                Period=60,
                Statistics=[stat],
                Unit=unit,
            )
        except Exception as exc:
            values[metric_name] = {"available": False, "error": str(exc)}
            continue
        datapoints = response.get("Datapoints", [])
        if not datapoints:
            values[metric_name] = {"available": False, "reason": "no_datapoints_yet"}
            continue
        if stat == "Sum":
            value = sum(point.get("Sum", 0.0) for point in datapoints)
        else:
            value = sum(point.get("Average", 0.0) for point in datapoints) / len(datapoints)
        values[metric_name] = {
            "available": True,
            "stat": stat,
            "value": round(value, 4),
            "datapoints": len(datapoints),
        }
    return {"available": True, "metrics": values}


def _shell_script(
    s3_input_notebook: str,
    s3_output_prefix: str,
    s3_progress_uri: str,
    notebook_name: str,
    max_runtime_seconds: int,
) -> str:
    input_uri = shlex.quote(s3_input_notebook)
    output_uri = shlex.quote(s3_output_prefix.rstrip("/") + "/")
    progress_uri = shlex.quote(s3_progress_uri)
    input_name = shlex.quote(notebook_name)
    timeout = int(max_runtime_seconds)
    return f"""#!/bin/bash
set -euo pipefail
export HOME=/root
export PATH="$HOME/.local/bin:$PATH"
WORKDIR=/tmp/aws-notebook-runner
mkdir -p "$WORKDIR/input" "$WORKDIR/output"
cd "$WORKDIR"
write_progress() {{
  phase="$1"
  message="$2"
  python3 - "$phase" "$message" > "$WORKDIR/progress.json" <<'PY'
import datetime
import json
import sys
phase = sys.argv[1]
message = sys.argv[2]
print(json.dumps({{
    "phase": phase,
    "message": message,
    "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
}}))
PY
  aws s3 cp "$WORKDIR/progress.json" {progress_uri} >/dev/null
}}
finish() {{
  rc=$?
  if [ "$rc" -eq 0 ]; then
    write_progress completed "Notebook execution completed."
  else
    write_progress failed "Notebook execution failed with exit code $rc."
  fi
  sudo shutdown -h now || true
  exit "$rc"
}}
trap finish EXIT
write_progress installing "Installing system and Python dependencies."
sudo dnf install -y python3-pip awscli
python3 -m venv "$WORKDIR/venv"
source "$WORKDIR/venv/bin/activate"
python -m pip install --upgrade pip
python -m pip install nbconvert nbformat ipykernel jupyter
aws s3 cp {input_uri} "input/{input_name}"
write_progress executing "Executing notebook with nbconvert."
python -m jupyter nbconvert \
  --to notebook \
  --execute "input/{input_name}" \
  --output executed.ipynb \
  --output-dir "$WORKDIR/output" \
  --ExecutePreprocessor.timeout={timeout}
write_progress uploading "Uploading executed notebook and artifacts."
aws s3 cp "$WORKDIR/output/" {output_uri} --recursive
"""


def start_ec2_smoke_run_async(
    policy: RunnerPolicy,
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
    plan = build_ec2_smoke_plan(
        policy,
        local_path=local_path,
        run_name=run_name,
        instance_type=instance_type,
        max_runtime_seconds=max_runtime_seconds,
        ami_id=ami_id,
        subnet_id=subnet_id,
        security_group_id=security_group_id,
        instance_profile_name=instance_profile_name,
    )
    assert_can_start_ec2(policy, plan, confirmation_token)

    import boto3

    session = boto3.Session(
        profile_name=policy.aws_profile or None,
        region_name=policy.aws_region,
    )
    ec2 = session.client("ec2")
    s3 = session.client("s3")
    ssm = session.client("ssm")

    notebook_path = policy.resolve_notebook(local_path)
    bucket, root_prefix = policy.s3_root_parts()
    s3_input_key = f"{root_prefix}/ec2/{run_name}/input/{local_path}".lstrip("/")
    s3_output_prefix = f"s3://{bucket}/{root_prefix}/ec2/{run_name}/output"
    s3_progress_key = _progress_key(root_prefix, run_name)
    s3_progress_uri = f"s3://{bucket}/{s3_progress_key}"
    selected_ami = ami_id or _latest_al2023_ami(ec2)
    selected_subnet = subnet_id or _default_subnet_id(ec2)
    selected_profile = instance_profile_name or "EC2NotebookRunnerRole"
    runtime = plan["run"]["max_runtime_seconds"]
    instance_id: str | None = None

    manifest: dict[str, Any] = {
        "backend": "ec2_ssm",
        "run_name": run_name,
        "phase": "created",
        "created_at": _iso(_now()),
        "updated_at": _iso(_now()),
        "local_path": local_path,
        "instance_type": plan["run"]["instance_type"],
        "max_runtime_seconds": runtime,
        "ami_id": selected_ami,
        "subnet_id": selected_subnet,
        "security_group_id": security_group_id,
        "instance_profile_name": selected_profile,
        "s3_input_notebook": f"s3://{bucket}/{s3_input_key}",
        "s3_output_prefix": s3_output_prefix,
        "s3_progress_uri": s3_progress_uri,
        "cleanup_default": policy.cleanup_default,
        "cost_estimate": plan["cost_estimate"],
    }
    _write_manifest(policy, manifest)
    s3.upload_file(str(notebook_path), bucket, s3_input_key)
    manifest["phase"] = "staged"
    manifest["updated_at"] = _iso(_now())
    _write_manifest(policy, manifest)
    try:
        network_interface: dict[str, Any] = {
            "DeviceIndex": 0,
            "SubnetId": selected_subnet,
            "AssociatePublicIpAddress": True,
        }
        if security_group_id:
            network_interface["Groups"] = [security_group_id]
        response = ec2.run_instances(
            ImageId=selected_ami,
            InstanceType=plan["run"]["instance_type"],
            MinCount=1,
            MaxCount=1,
            IamInstanceProfile={"Name": selected_profile},
            InstanceInitiatedShutdownBehavior="terminate",
            NetworkInterfaces=[network_interface],
            MetadataOptions={"HttpTokens": "required", "HttpEndpoint": "enabled"},
            TagSpecifications=[
                {
                    "ResourceType": "instance",
                    "Tags": [
                        {"Key": "created-by", "Value": "aws-notebook-runner-mcp"},
                        {"Key": "run-name", "Value": run_name},
                    ],
                }
            ],
        )
        instance_id = response["Instances"][0]["InstanceId"]
        launch_time = response["Instances"][0].get("LaunchTime")
        manifest.update(
            {
                "phase": "launching",
                "instance_id": instance_id,
                "launch_time": launch_time.isoformat() if launch_time else _iso(_now()),
                "updated_at": _iso(_now()),
            }
        )
        _write_manifest(policy, manifest)
        ec2.get_waiter("instance_running").wait(InstanceIds=[instance_id])
        manifest["phase"] = "waiting_ssm"
        manifest["updated_at"] = _iso(_now())
        _write_manifest(policy, manifest)
        _wait_for_ssm_instance(ssm, instance_id, timeout_seconds=420)
        script = _shell_script(
            f"s3://{bucket}/{s3_input_key}",
            s3_output_prefix,
            s3_progress_uri,
            notebook_path.name,
            runtime,
        )
        command = ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            TimeoutSeconds=runtime + 600,
            Parameters={"commands": [script]},
        )
        manifest.update(
            {
                "phase": "installing",
                "command_id": command["Command"]["CommandId"],
                "command_sent_at": _iso(_now()),
                "updated_at": _iso(_now()),
            }
        )
        _write_manifest(policy, manifest)
        return {
            "backend": "ec2_ssm",
            "status": "started",
            "run_name": run_name,
            "instance_id": instance_id,
            "command_id": manifest["command_id"],
            "s3_input_notebook": manifest["s3_input_notebook"],
            "s3_output_prefix": s3_output_prefix,
            "s3_progress_uri": s3_progress_uri,
            "ami_id": selected_ami,
            "subnet_id": selected_subnet,
            "instance_profile_name": selected_profile,
            "message": "Run started asynchronously. Poll get_ec2_smoke_run_status(run_name).",
        }
    except Exception:
        if instance_id and policy.cleanup_default:
            ec2.terminate_instances(InstanceIds=[instance_id])
        manifest["phase"] = "failed"
        manifest["updated_at"] = _iso(_now())
        _write_manifest(policy, manifest)
        raise


def get_ec2_smoke_run_status(
    policy: RunnerPolicy,
    run_name: str,
    include_cloudwatch: bool = True,
    terminate_if_finished: bool = True,
) -> dict[str, Any]:
    if not dependency_status()["boto3_installed"]:
        raise RuntimeError("boto3 is not installed. Install with: pip install '.[aws]'")
    import boto3
    from botocore.exceptions import ClientError

    manifest = _read_manifest(policy, run_name)
    session = boto3.Session(
        profile_name=policy.aws_profile or None,
        region_name=policy.aws_region,
    )
    ec2 = session.client("ec2")
    s3 = session.client("s3")
    ssm = session.client("ssm")
    cloudwatch = session.client("cloudwatch")
    bucket, root_prefix = policy.s3_root_parts()
    output_prefix = f"{root_prefix}/ec2/{run_name}/output"
    progress = _read_progress(s3, bucket, _progress_key(root_prefix, run_name))
    if progress and progress.get("phase"):
        manifest["phase"] = progress["phase"]
        manifest["progress_updated_at"] = progress.get("updated_at")
    instance_id = manifest.get("instance_id")
    command_id = manifest.get("command_id")
    instance_state = "unknown"
    launch_time = _parse_iso(manifest.get("launch_time"))
    state_transition_reason = None
    if instance_id:
        try:
            response = ec2.describe_instances(InstanceIds=[instance_id])
            instance = response["Reservations"][0]["Instances"][0]
            instance_state = instance["State"]["Name"]
            state_transition_reason = instance.get("StateTransitionReason")
            launch_time = instance.get("LaunchTime") or launch_time
        except ClientError as exc:
            instance_state = f"error:{exc.response.get('Error', {}).get('Code')}"
    invocation: dict[str, Any] | None = None
    if instance_id and command_id:
        try:
            invocation = ssm.get_command_invocation(
                CommandId=command_id,
                InstanceId=instance_id,
            )
        except ClientError:
            invocation = None
    ssm_status = invocation.get("Status") if invocation else None
    if ssm_status == "Success":
        manifest["phase"] = "completed"
    elif ssm_status in {"Cancelled", "TimedOut", "Failed", "Cancelling"}:
        manifest["phase"] = "failed"
    phase = manifest.get("phase", "unknown")
    terminal = phase in {"completed", "failed"} or instance_state in {"shutting-down", "terminated"}
    if (
        terminate_if_finished
        and manifest.get("cleanup_default")
        and instance_id
        and terminal
        and instance_state not in {"shutting-down", "terminated"}
    ):
        ec2.terminate_instances(InstanceIds=[instance_id])
        instance_state = "termination_requested"
    manifest["updated_at"] = _iso(_now())
    _write_manifest(policy, manifest)
    created_at = _parse_iso(manifest.get("created_at"))
    max_runtime_seconds = manifest.get("max_runtime_seconds")
    progress_percent = RUN_PHASE_PROGRESS.get(phase, 0)
    progress_summary = _progress_summary(
        phase=phase,
        progress_percent=progress_percent,
        created_at=created_at,
        launch_time=launch_time,
        max_runtime_seconds=max_runtime_seconds,
        terminal=terminal,
    )
    end_time = _now() if terminal else None
    cost = _estimate_current_cost(manifest["instance_type"], launch_time, end_time)
    artifacts = _list_s3_artifacts(s3, bucket, output_prefix)
    metrics = (
        _cloudwatch_metrics(cloudwatch, instance_id, launch_time)
        if include_cloudwatch and instance_id
        else {"available": False, "reason": "disabled_or_no_instance"}
    )
    return {
        "backend": "ec2_ssm",
        "run_name": run_name,
        "phase": phase,
        "progress_percent_estimate": progress_percent,
        "progress_summary": progress_summary,
        "progress": progress,
        "terminal": terminal,
        "instance": {
            "instance_id": instance_id,
            "state": instance_state,
            "state_transition_reason": state_transition_reason,
            "launch_time": launch_time.isoformat() if launch_time else None,
            "instance_type": manifest.get("instance_type"),
        },
        "ssm": {
            "command_id": command_id,
            "status": ssm_status,
            "status_details": invocation.get("StatusDetails") if invocation else None,
            "response_code": invocation.get("ResponseCode") if invocation else None,
            "stdout_tail": (invocation.get("StandardOutputContent", "")[-4000:] if invocation else ""),
            "stderr_tail": (invocation.get("StandardErrorContent", "")[-4000:] if invocation else ""),
        },
        "s3": {
            "input_notebook": manifest.get("s3_input_notebook"),
            "output_prefix": manifest.get("s3_output_prefix"),
            "progress_uri": manifest.get("s3_progress_uri"),
            "artifacts": artifacts,
        },
        "cost": cost,
        "cloudwatch": metrics,
    }


def start_ec2_smoke_run(
    policy: RunnerPolicy,
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
    started = start_ec2_smoke_run_async(
        policy,
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
    deadline = time.monotonic() + (max_runtime_seconds or policy.max_runtime_seconds) + 900
    last_status: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        last_status = get_ec2_smoke_run_status(policy, run_name)
        if last_status["terminal"]:
            return {
                **started,
                "status": "completed" if last_status["phase"] == "completed" else "failed",
                "final_status": last_status,
            }
        time.sleep(10)
    raise TimeoutError(f"Run {run_name!r} did not finish before local wait timeout.")
