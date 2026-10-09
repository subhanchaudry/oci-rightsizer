#!/usr/bin/env python3
"""
OCI VM rightsizing CLI for a FinOps-first workflow.

Primary workflow
- `scan`: high-level portfolio savings summary for a scoped fleet
- `recommend`: ranked VM action plan for one instance or a scoped fleet
- `apply`: preview or explicitly execute deterministic changes
- `run`: orchestrate scan/recommend, optional apply, verification, and
  estimated-savings summary for verified configuration changes

Cloud Shell launch note
- Running `python3 rightsizer.py` starts the guided workflow.
- To use `resizing` as a practical Cloud Shell command without root access,
  place a small wrapper or symlink in `$HOME/bin`, for example a shell script
  that execs `python3 /path/to/rightsizer.py "$@"`.
- A literal `/resizing` shell command normally requires alias or shell config
  outside this script, so `resizing` is the practical command name.

Defaults
- Pricing is attempted by default for `scan`, `recommend`, `apply`, and `run`.
  It remains best-effort and non-blocking.
- Deterministic policy, pricing math, resize logic, and verification remain the
  source of truth.
- Generative AI is narrative-only and never changes decisions or savings.

Examples
- `python3 rightsizer.py`
- `python3 rightsizer.py scan --compartment-id <compartment_ocid>`
- `python3 rightsizer.py recommend --compartment-id <compartment_ocid>`
- `python3 rightsizer.py recommend --instance-id <instance_ocid>`
- `python3 rightsizer.py apply --compartment-id <compartment_ocid> --regions <region> --apply --acknowledge-reboot --max-changes 1 --wait`

Hidden developer tools remain available for troubleshooting, including
`discover`, `metrics`, `pricing-debug`, `resize`, `verify`, and advanced policy
tuning flags.
"""

from __future__ import annotations

import argparse
import builtins
import json
import logging
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Event, Lock
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    import oci
except ImportError:  # pragma: no cover - depends on runtime environment
    oci = None


LOG = logging.getLogger("rightsizer")

__version__ = "1.0.0"
AUTH_MODES = ("auto", "api_key", "security_token", "instance_obo_user")
RESIZABLE_FLEX_PREFIXES = ("VM.Standard.", "VM.Optimized")


def terminal_safe_text(value: Any) -> str:
    """Neutralize terminal controls in cloud metadata and model narratives."""
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", lambda match: f"\\x{ord(match.group()):02x}", str(value))


def print(*values: Any, **kwargs: Any) -> None:
    """Keep human-readable stdout safe; JSON remains escaped by json.dumps."""
    builtins.print(*(terminal_safe_text(value) for value in values), **kwargs)


def supports_resize(shape: Any) -> bool:
    """Limit execution to Standard and Optimized Flex VMs, excluding DenseIO/BM/GPU."""
    name = str(shape or "")
    return name.endswith(".Flex") and name.startswith(RESIZABLE_FLEX_PREFIXES)

METRIC_NAMESPACE = "oci_computeagent"
CPU_METRIC = "CpuUtilization"
MEM_METRIC = "MemoryUtilization"
ACTION_AUTO_APPLY = "AUTO_APPLY"
ACTION_REVIEW_REQUIRED = "REVIEW_REQUIRED"
ACTION_OBSERVE = "OBSERVE"
ACTION_GROUP_KEYS = {
    ACTION_AUTO_APPLY: "auto_apply",
    ACTION_REVIEW_REQUIRED: "review_required",
    ACTION_OBSERVE: "observe",
}
ACTION_TIER_PRIORITY = {
    ACTION_AUTO_APPLY: 0,
    ACTION_REVIEW_REQUIRED: 1,
    ACTION_OBSERVE: 2,
}
ACTION_TIER_TO_SAFETY_CLASS = {
    ACTION_AUTO_APPLY: "SAFE_TO_APPLY",
    ACTION_REVIEW_REQUIRED: "REVIEW_RECOMMENDED",
    ACTION_OBSERVE: "OBSERVATION_ONLY",
}
PRODUCTION_LIKE_VALUES = {"prod", "production", "prd", "live"}
DEFAULT_CONFIG_FILE = os.getenv("OCI_CLI_CONFIG_FILE") or os.path.expanduser("~/.oci/config")
if not os.getenv("OCI_CLI_CONFIG_FILE"):
    if os.path.exists("/etc/oci/config"):
        DEFAULT_CONFIG_FILE = "/etc/oci/config"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso_z(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def utc_midnight(value: datetime) -> datetime:
    value = value.astimezone(timezone.utc)
    return value.replace(hour=0, minute=0, second=0, microsecond=0)


def safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def ceil_units(value: float, step: float = 1.0, minimum: Optional[float] = None) -> float:
    if step <= 0:
        step = 1.0
    rounded = math.ceil(float(value) / step) * step
    if minimum is not None:
        rounded = max(float(minimum), rounded)
    return float(rounded)


def ceil_units_bounded(
    value: float,
    step: float = 1.0,
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
    base: Optional[float] = None,
) -> float:
    step = safe_float(step) or 1.0
    if step <= 0:
        step = 1.0

    minimum_value = safe_float(minimum)
    maximum_value = safe_float(maximum)
    anchor = safe_float(base)
    if anchor is None:
        anchor = minimum_value if minimum_value is not None else 0.0

    if minimum_value is not None:
        value = max(float(value), minimum_value)
    if maximum_value is not None:
        value = min(float(value), maximum_value)

    aligned = anchor + math.ceil((float(value) - anchor) / step) * step
    if minimum_value is not None and aligned < minimum_value:
        aligned = anchor + math.ceil((minimum_value - anchor) / step) * step
    if maximum_value is not None and aligned > maximum_value:
        aligned = anchor + math.floor((maximum_value - anchor) / step) * step
    if minimum_value is not None and aligned < minimum_value:
        aligned = minimum_value
    if maximum_value is not None and aligned > maximum_value:
        aligned = maximum_value
    return float(aligned)


def clamp(value: float, minimum: Optional[float], maximum: Optional[float]) -> float:
    if minimum is not None:
        value = max(value, float(minimum))
    if maximum is not None:
        value = min(value, float(maximum))
    return float(value)


def parse_filter_args(entries: Sequence[str]) -> List[Tuple[str, str]]:
    parsed: List[Tuple[str, str]] = []
    for entry in entries:
        text = str(entry).strip()
        if "=" not in text:
            raise SystemExit(f"Invalid --filter value '{entry}'. Expected key=value.")
        key, value = text.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or not value:
            raise SystemExit(f"Invalid --filter value '{entry}'. Expected key=value.")
        parsed.append((key, value))
    return parsed


def percentile(sorted_values: Sequence[float], pct: float) -> Optional[float]:
    if not sorted_values:
        return None
    index = max(0, math.ceil(float(pct) * len(sorted_values)) - 1)
    return float(sorted_values[index])


def summarize_values(values: Sequence[float]) -> Dict[str, Optional[float]]:
    if not values:
        return {
            "count": 0,
            "latest": None,
            "min": None,
            "max": None,
            "avg": None,
            "p95": None,
        }
    ordered = sorted(float(v) for v in values)
    return {
        "count": len(ordered),
        "latest": float(values[-1]),
        "min": float(ordered[0]),
        "max": float(ordered[-1]),
        "avg": round(sum(ordered) / len(ordered), 4),
        "p95": percentile(ordered, 0.95),
    }


def load_config(profile: str, config_file: str, region: Optional[str]) -> Dict[str, Any]:
    ensure_oci_sdk()
    config = oci.config.from_file(file_location=config_file, profile_name=profile)
    if region:
        config["region"] = region
    return config


def build_signer(config: Dict[str, Any], auth: str = "auto") -> Optional[Any]:
    """Read credentials only from the operator's OCI configuration, never from source."""
    mode = auth
    if mode == "auto":
        mode = os.getenv("OCI_CLI_AUTH") or (
            "instance_obo_user" if config.get("delegation_token_file") else
            "security_token" if config.get("security_token_file") else "api_key"
        )
    if mode not in AUTH_MODES or mode == "auto":
        raise ValueError(f"Unsupported authentication mode. Choose one of: {', '.join(AUTH_MODES[1:])}.")
    if mode == "api_key":
        return None
    key = "delegation_token_file" if mode == "instance_obo_user" else "security_token_file"
    token_path = config.get(key)
    if not token_path:
        raise ValueError(f"Authentication mode {mode} requires {key} in the OCI profile.")
    with open(os.path.expanduser(str(token_path)), encoding="utf-8") as token_file:
        token = token_file.read().strip()
    if not token:
        raise ValueError(f"Authentication mode {mode} requires a nonempty token file.")
    if mode == "instance_obo_user":
        return oci.auth.signers.InstancePrincipalsDelegationTokenSigner(delegation_token=token)
    private_key = oci.signer.load_private_key_from_file(
        os.path.expanduser(config["key_file"]), config.get("pass_phrase")
    )
    return oci.auth.signers.SecurityTokenSigner(token, private_key)


def ensure_oci_sdk() -> None:
    if oci is None:
        raise SystemExit(
            "The OCI Python SDK is not installed in this environment. "
            "Install `oci` or run this script in OCI Cloud Shell."
        )


def json_print(payload: Dict[str, Any]) -> None:
    json.dump(payload, sys.stdout, indent=2, sort_keys=False)
    sys.stdout.write("\n")


def format_number(value: Any, decimals: int = 1) -> str:
    number = safe_float(value)
    if number is None:
        return "n/a"
    if math.isclose(number, round(number), rel_tol=0.0, abs_tol=1e-9):
        return str(int(round(number)))
    return f"{number:.{decimals}f}"


def format_percent(value: Any) -> str:
    number = safe_float(value)
    if number is None:
        return "n/a"
    return f"{number:.1f}%"


def effective_coverage_ratio(coverage: Optional[Dict[str, Any]]) -> float:
    if not coverage:
        return 0.0
    adjusted = safe_float(coverage.get("runtime_adjusted_coverage_ratio"))
    if adjusted is not None:
        return adjusted
    return safe_float(coverage.get("coverage_ratio")) or 0.0


def effective_expected_points(coverage: Optional[Dict[str, Any]]) -> int:
    if not coverage:
        return 0
    adjusted = safe_float(coverage.get("runtime_adjusted_expected_points"))
    if adjusted is not None:
        return max(0, int(round(adjusted)))
    return max(0, int(coverage.get("expected_points", 0) or 0))


def active_observed_days(coverage: Optional[Dict[str, Any]]) -> int:
    if not coverage:
        return 0
    return max(0, int(coverage.get("active_observed_days", coverage.get("distinct_days", 0)) or 0))


def coverage_runtime_label(coverage: Optional[Dict[str, Any]]) -> Optional[str]:
    state = str((coverage or {}).get("runtime_classification") or "").strip().lower()
    mapping = {
        "insufficient_runtime": "insufficient active runtime in selected window",
        "intermittent_or_recently_active": "stopped or intermittently-running workload",
        "sparse_while_active": "sparse telemetry while active",
        "no_telemetry": "no telemetry observed",
    }
    return mapping.get(state)


def format_effective_coverage_percent(coverage: Optional[Dict[str, Any]]) -> str:
    if not coverage:
        return "n/a"
    ratio = effective_coverage_ratio(coverage)
    text = format_percent(ratio * 100.0)
    raw_ratio = safe_float(coverage.get("coverage_ratio"))
    if (
        str(coverage.get("coverage_basis") or "") == "observed_metric_span"
        and raw_ratio is not None
        and not math.isclose(ratio, raw_ratio, rel_tol=0.0, abs_tol=1e-9)
    ):
        return f"{text} active-window"
    return text


def coverage_scale_down_guard(
    coverage: Optional[Dict[str, Any]],
    minimum_active_days: int,
    metric_label: str,
) -> Dict[str, str]:
    observed_points = int((coverage or {}).get("observed_points", 0) or 0)
    required_days = max(1, int(minimum_active_days or 1))
    active_days = active_observed_days(coverage)
    runtime_ratio = effective_coverage_ratio(coverage)
    label = str(metric_label or "metric").strip()

    if observed_points <= 0:
        return {
            "status": "no_telemetry",
            "reason": f"No long-window {label} telemetry was observed in the selected window.",
        }
    if active_days < required_days:
        return {
            "status": "insufficient_runtime",
            "reason": (
                f"Only {active_days} active observed days in the selected window; "
                f"need at least {required_days} active days for {label} scale-down."
            ),
        }
    if runtime_ratio < 0.5:
        return {
            "status": "sparse_while_active",
            "reason": (
                f"Long-window {label} telemetry is sparse while the VM appears active, "
                f"so {label} scale-down is intentionally blocked."
            ),
        }
    return {"status": "ok", "reason": ""}


def format_coverage(coverage: Dict[str, Any]) -> str:
    observed = coverage.get("observed_points", 0)
    expected = coverage.get("expected_points", 0)
    ratio = safe_float(coverage.get("coverage_ratio")) or 0.0
    days = active_observed_days(coverage)
    runtime_expected = effective_expected_points(coverage)
    runtime_ratio = effective_coverage_ratio(coverage)
    active_hours = safe_float(coverage.get("active_observed_hours"))

    if (
        str(coverage.get("coverage_basis") or "") == "observed_metric_span"
        and runtime_expected > 0
        and runtime_expected != expected
    ):
        text = (
            f"{observed}/{expected} points ({ratio * 100:.1f}% full-window), "
            f"{days} active observed days, "
            f"active window {observed}/{runtime_expected} ({runtime_ratio * 100:.1f}%)"
        )
        if active_hours is not None:
            text += f" over {format_number(active_hours)} hours"
    else:
        text = f"{observed}/{runtime_expected or expected} points ({runtime_ratio * 100:.1f}%), {days} active observed days"

    runtime_label = coverage_runtime_label(coverage)
    if runtime_label:
        text += f", {runtime_label}"
    return text


def format_advisory_percent(value: Any, coverage: Optional[Dict[str, Any]] = None) -> str:
    text = format_percent(value)
    if text == "n/a" or not coverage:
        return text
    runtime_label = coverage_runtime_label(coverage)
    if runtime_label == "insufficient active runtime in selected window":
        return f"{text} (advisory only; insufficient active runtime)"
    if runtime_label == "no telemetry observed":
        return f"{text} (advisory only; no telemetry)"
    ratio = effective_coverage_ratio(coverage)
    if ratio < 0.5:
        return f"{text} (advisory only; sparse active telemetry)"
    return text


def format_money(value: Any, currency_code: str = "USD", decimals: int = 2) -> str:
    number = safe_float(value)
    if number is None:
        return "n/a"
    decimals = max(0, int(decimals))
    if str(currency_code or "USD").upper() == "USD":
        return f"${number:,.{decimals}f}"
    return f"{str(currency_code).upper()} {number:,.{decimals}f}"


def rounded_money(value: Any) -> Optional[float]:
    number = safe_float(value)
    if number is None:
        return None
    return round(number, 2)


def summarize_currency(values: Sequence[Any]) -> Optional[float]:
    total = 0.0
    seen = False
    for value in values:
        number = safe_float(value)
        if number is None:
            continue
        total += number
        seen = True
    if not seen:
        return None
    return round(total, 2)


def normalize_tag_key(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def flatten_instance_tags(record: Dict[str, Any]) -> Dict[str, str]:
    flattened: Dict[str, str] = {}

    def add_tag(key: Any, value: Any) -> None:
        normalized_key = normalize_tag_key(key)
        if not normalized_key or value is None:
            return
        text = str(value).strip()
        if not text:
            return
        flattened.setdefault(normalized_key, text)

    for key, value in (record.get("freeform_tags") or {}).items():
        add_tag(key, value)
    for _, values in (record.get("defined_tags") or {}).items():
        if not isinstance(values, dict):
            continue
        for key, value in values.items():
            add_tag(key, value)
    return flattened


def derive_owner_or_cost_center(record: Dict[str, Any]) -> str:
    tags = flatten_instance_tags(record)
    ordered_keys = [
        "owner",
        "team",
        "application",
        "app",
        "cost_center",
        "costcenter",
        "project",
        "service",
    ]
    for key in ordered_keys:
        value = tags.get(key)
        if value:
            return value
    return str(record.get("compartment_name") or record.get("compartment_id") or "unknown")


def has_production_like_tag(record: Dict[str, Any]) -> bool:
    tags = flatten_instance_tags(record)
    for key in ("env", "environment", "stage", "tier", "lifecycle"):
        value = str(tags.get(key) or "").strip().lower()
        if value in PRODUCTION_LIKE_VALUES or value.startswith("prod"):
            return True
    return False


def build_explanation_basis(metrics_summary: Dict[str, Any]) -> Dict[str, Any]:
    short_window = metrics_summary.get("short_window", {})
    long_window = metrics_summary.get("long_window", {})
    return {
        "short_cpu_p95": short_window.get("cpu", {}).get("p95"),
        "long_cpu_p95": long_window.get("cpu", {}).get("p95"),
        "short_memory_p95": short_window.get("memory", {}).get("p95"),
        "long_memory_p95": long_window.get("memory", {}).get("p95"),
        "long_cpu_coverage_ratio": effective_coverage_ratio(long_window.get("coverage", {}).get("cpu", {})),
        "long_memory_coverage_ratio": effective_coverage_ratio(long_window.get("coverage", {}).get("memory", {})),
    }


def classify_metrics_coverage(metrics_summary: Dict[str, Any]) -> str:
    short_window = metrics_summary.get("short_window", {})
    long_window = metrics_summary.get("long_window", {})
    short_cpu_count = safe_float(short_window.get("cpu", {}).get("count")) or 0.0
    short_memory_count = safe_float(short_window.get("memory", {}).get("count")) or 0.0
    long_cpu_coverage = long_window.get("coverage", {}).get("cpu", {})
    long_memory_coverage = long_window.get("coverage", {}).get("memory", {})
    long_cpu_ratio = effective_coverage_ratio(long_cpu_coverage)
    long_memory_ratio = effective_coverage_ratio(long_memory_coverage)
    long_cpu_runtime = str(long_cpu_coverage.get("runtime_classification") or "")
    long_memory_runtime = str(long_memory_coverage.get("runtime_classification") or "")

    if short_cpu_count <= 0 or short_memory_count <= 0:
        return "sparse"
    if long_cpu_runtime in {"no_telemetry", "sparse_while_active"} or long_memory_runtime in {"no_telemetry", "sparse_while_active"}:
        return "sparse"
    if long_cpu_runtime == "insufficient_runtime" or long_memory_runtime == "insufficient_runtime":
        return "review"
    if long_cpu_ratio >= 0.8 and long_memory_ratio >= 0.8:
        return "strong"
    if long_cpu_ratio >= 0.5 and long_memory_ratio >= 0.5:
        return "review"
    return "sparse"


def determine_next_step(action_tier: str, action_reasons: Sequence[str]) -> str:
    if action_tier == ACTION_AUTO_APPLY:
        return "Eligible for automated apply and post-change verification."
    if action_tier == ACTION_REVIEW_REQUIRED:
        reason = str(action_reasons[0]) if action_reasons else "Review the recommendation before execution."
        return f"Human review required before apply: {reason}"
    if action_tier == ACTION_OBSERVE:
        reason = str(action_reasons[0]) if action_reasons else "No safe automated change is available."
        return f"Observe only for now: {reason}"
    return "Observe only for now; no safe automated change is available."


def determine_action_tier(
    inventory: Dict[str, Any],
    recommendation: Dict[str, Any],
    metrics_summary: Dict[str, Any],
    pricing_estimate: Optional[Dict[str, Any]] = None,
    cpu_scale_down_required_days: int = 7,
    memory_scale_down_required_days: int = 7,
) -> Dict[str, Any]:
    pricing = pricing_estimate or {}
    explanation_basis = build_explanation_basis(metrics_summary)
    short_window = metrics_summary.get("short_window", {})
    long_window = metrics_summary.get("long_window", {})
    short_cpu_count = safe_float(short_window.get("cpu", {}).get("count")) or 0.0
    short_memory_count = safe_float(short_window.get("memory", {}).get("count")) or 0.0
    long_cpu_ratio = safe_float(explanation_basis.get("long_cpu_coverage_ratio")) or 0.0
    long_memory_ratio = safe_float(explanation_basis.get("long_memory_coverage_ratio")) or 0.0
    long_cpu_coverage = long_window.get("coverage", {}).get("cpu", {})
    long_memory_coverage = long_window.get("coverage", {}).get("memory", {})
    cpu_runtime_guard = coverage_scale_down_guard(long_cpu_coverage, cpu_scale_down_required_days, "CPU")
    memory_runtime_guard = coverage_scale_down_guard(long_memory_coverage, memory_scale_down_required_days, "Memory")

    blockers: List[str] = []
    reasons: List[str] = []

    if recommendation.get("overall_decision") != "RESIZE":
        runtime_blockers = clean_text_items(
            [
                guard.get("reason")
                for guard in (cpu_runtime_guard, memory_runtime_guard)
                if guard.get("status") in {"insufficient_runtime", "no_telemetry", "sparse_while_active"}
            ]
        )
        if runtime_blockers:
            blockers.extend(runtime_blockers)
        else:
            blockers.append("No actionable resize recommendation is available.")
    if not supports_resize(inventory.get("shape")):
        blockers.append("Automatic resize is supported only for Standard and Optimized Flex VMs.")
    validation_errors = [str(item) for item in (recommendation.get("validation_errors") or [])]
    if validation_errors:
        blockers.extend(validation_errors)
    if short_cpu_count <= 0 or short_memory_count <= 0:
        blockers.append("Missing short-window CPU or memory metrics.")

    if blockers:
        return {
            "action_tier": ACTION_OBSERVE,
            "action_reasons": blockers,
            "next_step": determine_next_step(ACTION_OBSERVE, blockers),
            "metrics_coverage_classification": classify_metrics_coverage(metrics_summary),
            "governance_flags": [],
        }

    governance_flags: List[str] = []
    if long_cpu_ratio < 0.5 or long_memory_ratio < 0.5:
        governance_flags.append("sparse_long_window_coverage")
        reasons.append("Long-window telemetry is sparse while the VM appears active, so human review is required.")
    if recommendation.get("cpu_decision") == "SCALE_UP" or recommendation.get("memory_decision") == "SCALE_UP":
        governance_flags.append("scale_up_recommendation")
        reasons.append("This recommendation includes a scale-up and should be reviewed.")
    if has_production_like_tag(inventory):
        governance_flags.append("production_like_tag")
        reasons.append("Production-like tagging suggests governance review before apply.")
    if pricing and pricing.get("pricing_attempted", True) and not pricing.get("pricing_enabled"):
        governance_flags.append("pricing_unavailable")
        reasons.append("Pricing is unavailable, so savings should be reviewed manually.")

    action_tier = ACTION_REVIEW_REQUIRED if reasons else ACTION_AUTO_APPLY
    if not reasons:
        reasons.append("Strong evidence supports a deterministic Flex resize with no blocking validation issues.")

    return {
        "action_tier": action_tier,
        "action_reasons": reasons,
        "next_step": determine_next_step(action_tier, reasons),
        "metrics_coverage_classification": classify_metrics_coverage(metrics_summary),
        "governance_flags": governance_flags,
    }


def build_financial_summary(pricing_estimate: Dict[str, Any], monthly_hours: float) -> Dict[str, Any]:
    pricing = pricing_estimate or {}
    current_hourly = safe_float(pricing.get("current_hourly_cost"))
    optimized_hourly = safe_float(pricing.get("estimated_new_hourly_cost"))
    monthly_savings = rounded_money(pricing.get("estimated_monthly_savings"))
    current_monthly_cost = rounded_money(
        pricing.get("current_monthly_cost")
        if pricing.get("current_monthly_cost") is not None
        else (current_hourly * float(monthly_hours) if current_hourly is not None else None)
    )
    optimized_monthly_cost = rounded_money(
        pricing.get("optimized_monthly_cost")
        if pricing.get("optimized_monthly_cost") is not None
        else (optimized_hourly * float(monthly_hours) if optimized_hourly is not None else None)
    )
    return {
        "current_monthly_cost": current_monthly_cost,
        "optimized_monthly_cost": optimized_monthly_cost,
        "monthly_savings": monthly_savings,
        "annualized_savings": rounded_money((monthly_savings or 0.0) * 12.0) if monthly_savings is not None else None,
        "pricing_confidence": pricing.get("pricing_confidence"),
        "pricing_source": pricing.get("pricing_source"),
    }


def candidate_sort_key(row: Dict[str, Any]) -> Tuple[int, int, float, str, str, str]:
    monthly_savings = safe_float(row.get("estimated_monthly_savings"))
    return (
        ACTION_TIER_PRIORITY.get(str(row.get("action_tier") or ACTION_OBSERVE), 9),
        0 if monthly_savings is not None else 1,
        -(monthly_savings or 0.0),
        str(row.get("region") or ""),
        str(row.get("display_name") or ""),
        str(row.get("instance_id") or ""),
    )


def build_candidate_groups(rows: Sequence[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    groups: Dict[str, List[Dict[str, Any]]] = {
        "auto_apply": [],
        "review_required": [],
        "observe": [],
    }
    for row in sorted(rows, key=candidate_sort_key):
        groups[ACTION_GROUP_KEYS.get(str(row.get("action_tier") or ACTION_OBSERVE), "observe")].append(row)
    return groups


def build_savings_funnel(candidate_groups: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Dict[str, Any]]:
    funnel: Dict[str, Dict[str, Any]] = {}
    for key in ("auto_apply", "review_required", "observe"):
        rows = candidate_groups.get(key, []) or []
        funnel[key] = {
            "count": len(rows),
            "monthly_savings": summarize_currency(row.get("estimated_monthly_savings") for row in rows),
        }
    return funnel


def build_breakdown_entry(rows: Sequence[Dict[str, Any]], extra: Dict[str, Any]) -> Dict[str, Any]:
    candidate_groups = build_candidate_groups(rows)
    return {
        **extra,
        "instance_count": len(rows),
        "actionable_count": sum(1 for row in rows if row.get("overall_decision") == "RESIZE"),
        "auto_apply_count": len(candidate_groups.get("auto_apply", [])),
        "review_required_count": len(candidate_groups.get("review_required", [])),
        "observe_count": len(candidate_groups.get("observe", [])),
        "current_monthly_run_rate": summarize_currency(row.get("current_monthly_cost") for row in rows),
        "optimized_monthly_run_rate": summarize_currency(row.get("optimized_monthly_cost") for row in rows),
        "monthly_savings": summarize_currency(row.get("estimated_monthly_savings") for row in rows),
    }


def build_breakdowns(rows: Sequence[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    by_region_map: Dict[str, List[Dict[str, Any]]] = {}
    by_compartment_map: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    by_owner_map: Dict[str, List[Dict[str, Any]]] = {}

    for row in rows:
        region = str(row.get("region") or "unknown")
        compartment_name = str(row.get("compartment_name") or row.get("compartment_id") or "unknown")
        compartment_id = str(row.get("compartment_id") or "")
        owner_label = str(row.get("owner_or_cost_center") or compartment_name)
        by_region_map.setdefault(region, []).append(row)
        by_compartment_map.setdefault((compartment_name, compartment_id), []).append(row)
        by_owner_map.setdefault(owner_label, []).append(row)

    by_region = sorted(
        [
            build_breakdown_entry(group_rows, {"region": key})
            for key, group_rows in by_region_map.items()
        ],
        key=lambda item: (-(safe_float(item.get("monthly_savings")) or 0.0), str(item.get("region"))),
    )
    by_compartment = sorted(
        [
            build_breakdown_entry(
                group_rows,
                {
                    "compartment_name": name,
                    "compartment_id": compartment_id,
                },
            )
            for (name, compartment_id), group_rows in by_compartment_map.items()
        ],
        key=lambda item: (-(safe_float(item.get("monthly_savings")) or 0.0), str(item.get("compartment_name"))),
    )
    by_owner_or_cost_center = sorted(
        [
            build_breakdown_entry(group_rows, {"owner_or_cost_center": key})
            for key, group_rows in by_owner_map.items()
        ],
        key=lambda item: (-(safe_float(item.get("monthly_savings")) or 0.0), str(item.get("owner_or_cost_center"))),
    )
    return {
        "by_region": by_region,
        "by_compartment": by_compartment,
        "by_owner_or_cost_center": by_owner_or_cost_center,
    }


def build_portfolio_summary(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    metrics_classes = [str(row.get("metrics_coverage_classification") or "sparse") for row in rows]
    pricing_available_count = sum(1 for row in rows if row.get("pricing_enabled"))
    gross_monthly_savings = summarize_currency(row.get("estimated_monthly_savings") for row in rows)
    return {
        "current_monthly_run_rate": summarize_currency(row.get("current_monthly_cost") for row in rows),
        "optimized_monthly_run_rate": summarize_currency(row.get("optimized_monthly_cost") for row in rows),
        "gross_monthly_savings": gross_monthly_savings,
        "annualized_savings": rounded_money((gross_monthly_savings or 0.0) * 12.0) if gross_monthly_savings is not None else None,
        "pricing_coverage_ratio": round((pricing_available_count / len(rows)), 4) if rows else 0.0,
        "strong_metrics_coverage_count": sum(1 for item in metrics_classes if item == "strong"),
        "review_metrics_coverage_count": sum(1 for item in metrics_classes if item == "review"),
        "sparse_metrics_coverage_count": sum(1 for item in metrics_classes if item == "sparse"),
    }


def build_recommend_summary(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    gross_monthly_savings = summarize_currency(row.get("estimated_monthly_savings") for row in rows)
    auto_apply_count = sum(1 for row in rows if str(row.get("action_tier")) == ACTION_AUTO_APPLY)
    review_required_count = sum(1 for row in rows if str(row.get("action_tier")) == ACTION_REVIEW_REQUIRED)
    observe_count = sum(1 for row in rows if str(row.get("action_tier")) == ACTION_OBSERVE)
    actionable_workloads = sum(
        1
        for row in rows
        if str(row.get("action_tier")) in {ACTION_AUTO_APPLY, ACTION_REVIEW_REQUIRED}
        and str(row.get("overall_decision")) == "RESIZE"
    )
    return {
        "workloads_considered": len(rows),
        "actionable_workloads": actionable_workloads,
        "auto_apply_count": auto_apply_count,
        "review_required_count": review_required_count,
        "observe_count": observe_count,
        "gross_monthly_savings": gross_monthly_savings,
        "annualized_savings": rounded_money((gross_monthly_savings or 0.0) * 12.0) if gross_monthly_savings is not None else None,
    }


def apply_tier_allows(action_tier: str, apply_tier: str) -> bool:
    selected = str(apply_tier or "auto").lower()
    normalized_tier = str(action_tier or ACTION_OBSERVE)
    if selected == "auto":
        return normalized_tier == ACTION_AUTO_APPLY
    if selected == "review":
        return normalized_tier in {ACTION_AUTO_APPLY, ACTION_REVIEW_REQUIRED}
    return normalized_tier in {ACTION_AUTO_APPLY, ACTION_REVIEW_REQUIRED, ACTION_OBSERVE}


def append_kv(lines: List[str], title: str, pairs: Iterable[Tuple[str, Any]]) -> None:
    lines.append(title)
    for key, value in pairs:
        lines.append(f"  {key}: {value}")


def action_tier_label(value: Any) -> str:
    mapping = {
        ACTION_AUTO_APPLY: "Auto-apply",
        ACTION_REVIEW_REQUIRED: "Review required",
        ACTION_OBSERVE: "Observe",
    }
    return mapping.get(str(value or ""), str(value or "n/a"))


def safety_class_label(value: Any) -> str:
    mapping = {
        "SAFE_TO_APPLY": "Safe to apply",
        "REVIEW_RECOMMENDED": "Review recommended",
        "OBSERVATION_ONLY": "Observation only",
    }
    return mapping.get(str(value or ""), str(value or "n/a"))


def config_text(ocpus: Any, memory_gb: Any) -> str:
    return f"{format_number(ocpus)} OCPU / {format_number(memory_gb)} GB"


def clean_text_items(values: Sequence[Any]) -> List[str]:
    cleaned: List[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value).strip()
        if not text or text in seen:
            continue
        cleaned.append(text)
        seen.add(text)
    return cleaned


def short_resize_justification(row: Dict[str, Any]) -> str:
    cpu_decision = str(row.get("cpu_decision") or "")
    memory_decision = str(row.get("memory_decision") or "")
    if cpu_decision == "SCALE_DOWN" and memory_decision == "SCALE_DOWN":
        return "Low sustained CPU and memory utilization support a smaller shape."
    if cpu_decision == "SCALE_DOWN" and memory_decision == "NO_CHANGE":
        return "Long-window CPU utilization supports reducing OCPUs."
    if cpu_decision == "NO_CHANGE" and memory_decision == "SCALE_DOWN":
        return "Long-window memory utilization supports reducing memory."
    if cpu_decision == "SCALE_UP" or memory_decision == "SCALE_UP":
        return "Recent sustained utilization pressure suggests this workload needs more capacity."

    rationale = str(row.get("rationale_summary") or "").strip()
    if rationale:
        first_sentence = rationale.split(". ", 1)[0].strip()
        return first_sentence if first_sentence.endswith(".") else f"{first_sentence}."

    reasons = clean_text_items(row.get("action_reasons", []) or [])
    if reasons:
        first_reason = reasons[0]
        return first_reason if first_reason.endswith(".") else f"{first_reason}."
    return "Deterministic policy logic identified this recommendation."


def filter_rows_by_compartment(rows: Sequence[Dict[str, Any]], compartment_id: str) -> List[Dict[str, Any]]:
    return [row for row in rows if str(row.get("compartment_id") or "") == str(compartment_id)]


def filter_actionable_rows(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    actionable_tiers = {ACTION_AUTO_APPLY, ACTION_REVIEW_REQUIRED}
    return [
        row
        for row in rows
        if str(row.get("overall_decision")) == "RESIZE"
        and str(row.get("action_tier")) in actionable_tiers
    ]


def build_actionable_compartment_choices(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for row in filter_actionable_rows(rows):
        key = (
            str(row.get("compartment_id") or ""),
            str(row.get("compartment_name") or row.get("compartment_id") or "unknown"),
        )
        item = grouped.setdefault(
            key,
            {
                "compartment_id": key[0],
                "compartment_name": key[1],
                "rows": [],
                "monthly_savings": 0.0,
            },
        )
        item["rows"].append(row)
        item["monthly_savings"] += safe_float(row.get("estimated_monthly_savings")) or 0.0

    choices = list(grouped.values())
    for item in choices:
        item["actionable_count"] = len(item["rows"])
        item["monthly_savings"] = round(float(item["monthly_savings"]), 2)
    return sorted(
        choices,
        key=lambda item: (
            -(safe_float(item.get("monthly_savings")) or 0.0),
            str(item.get("compartment_name") or ""),
        ),
    )


def build_interactive_recommendation_summary(
    rows: Sequence[Dict[str, Any]],
    currency_code: str = "USD",
) -> Optional[str]:
    if not rows:
        return None

    actionable_rows = filter_actionable_rows(rows)
    auto_apply_count = sum(1 for row in actionable_rows if str(row.get("action_tier")) == ACTION_AUTO_APPLY)
    review_required_count = sum(1 for row in actionable_rows if str(row.get("action_tier")) == ACTION_REVIEW_REQUIRED)
    observe_count = max(0, len(rows) - len(actionable_rows))
    monthly_savings = summarize_currency(row.get("estimated_monthly_savings") for row in actionable_rows)

    summary = (
        f"Focused review for {len(rows)} workload{'s' if len(rows) != 1 else ''} in this compartment: "
        f"{len(actionable_rows)} actionable candidate{'s' if len(actionable_rows) != 1 else ''} "
        f"({auto_apply_count} auto-apply, {review_required_count} review required)"
    )
    if monthly_savings is not None:
        summary += f" representing {format_money(monthly_savings, currency_code)} / mo potential savings"
    summary += "."
    if observe_count:
        summary += f" {observe_count} workload{'s' if observe_count != 1 else ''} remain in observe."
    return summary


def parse_analysis_timeframe(raw_value: str) -> Tuple[int, str]:
    text = " ".join(str(raw_value or "").strip().lower().split())
    if not text:
        raise ValueError("Enter a timeframe like 1 week, 2 weeks, 10 days, or 3 months.")

    if text.isdigit():
        quantity = int(text)
        if quantity <= 0:
            raise ValueError("Timeframe must be greater than zero.")
        unit_label = "month" if quantity == 1 else "months"
        return quantity * 30, f"{quantity} {unit_label}"

    match = re.fullmatch(r"(\d+)\s*([a-z]+)", text)
    if not match:
        raise ValueError("Enter a timeframe like 1 week, 2 weeks, 10 days, or 3 months.")

    quantity = int(match.group(1))
    if quantity <= 0:
        raise ValueError("Timeframe must be greater than zero.")

    unit_aliases = {
        "d": "day",
        "day": "day",
        "days": "day",
        "w": "week",
        "wk": "week",
        "wks": "week",
        "week": "week",
        "weeks": "week",
        "m": "month",
        "mo": "month",
        "mos": "month",
        "mon": "month",
        "month": "month",
        "months": "month",
    }
    normalized_unit = unit_aliases.get(match.group(2))
    if normalized_unit is None:
        raise ValueError("Supported units are days, weeks, and months.")

    unit_days = {
        "day": 1,
        "week": 7,
        "month": 30,
    }
    unit_label = normalized_unit if quantity == 1 else f"{normalized_unit}s"
    return quantity * unit_days[normalized_unit], f"{quantity} {unit_label}"


def prompt_yes_no(prompt: str, default: Optional[bool] = None) -> bool:
    suffix = " [y/n]: "
    if default is True:
        suffix = " [Y/n]: "
    elif default is False:
        suffix = " [y/N]: "

    while True:
        try:
            raw = input(f"{prompt}{suffix}").strip().lower()
        except EOFError as exc:
            raise SystemExit("Input stream closed.") from exc
        if not raw and default is not None:
            return bool(default)
        if raw in {"y", "yes"}:
            return True
        if raw in {"n", "no"}:
            return False
        print("Please enter y or n.")


def prompt_timeframe() -> Tuple[int, str]:
    while True:
        try:
            raw = input(
                "Analysis timeframe (for example 1 week, 2 weeks, 10 days, 3 months): "
            ).strip()
        except EOFError as exc:
            raise SystemExit("Input stream closed.") from exc
        try:
            return parse_analysis_timeframe(raw)
        except ValueError as exc:
            print(str(exc))


def prompt_genai_model_id() -> Optional[str]:
    while True:
        try:
            raw = input("GenAI model OCID (leave blank to continue without GenAI): ").strip()
        except EOFError as exc:
            raise SystemExit("Input stream closed.") from exc
        if not raw:
            if prompt_yes_no("Proceed without GenAI?", default=True):
                return None
            continue
        error = validate_genai_model_id(raw)
        if error is None:
            return raw
        print(error)


def prompt_choice(prompt: str, options: Sequence[Dict[str, Any]], formatter: Callable[[int, Dict[str, Any]], str]) -> Dict[str, Any]:
    if not options:
        raise SystemExit("No selectable options are available.")
    while True:
        print(prompt)
        for index, item in enumerate(options, start=1):
            print(formatter(index, item))
        try:
            raw = input(f"Select an option [1-{len(options)}]: ").strip()
        except EOFError as exc:
            raise SystemExit("Input stream closed.") from exc
        if raw.isdigit():
            selected = int(raw)
            if 1 <= selected <= len(options):
                return options[selected - 1]
        print("Please enter a valid number from the list.")


def prompt_instance_multi_select(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    ordered = list(rows)
    if not ordered:
        return []

    while True:
        try:
            raw = input(
                "Select workloads to resize by index (for example 1,3,5), all, or none: "
            ).strip().lower()
        except EOFError as exc:
            raise SystemExit("Input stream closed.") from exc
        if raw == "all":
            return ordered
        if raw in {"none", "n"}:
            return []
        selected_indexes: List[int] = []
        valid = True
        for part in [item.strip() for item in raw.split(",") if item.strip()]:
            if not part.isdigit():
                valid = False
                break
            index = int(part)
            if index < 1 or index > len(ordered):
                valid = False
                break
            selected_indexes.append(index - 1)
        if valid and selected_indexes:
            seen: set[int] = set()
            return [ordered[index] for index in selected_indexes if not (index in seen or seen.add(index))]
        print("Enter valid indexes separated by commas, or type all or none.")


def render_interactive_scan_summary(payload: Dict[str, Any], include_header: bool = True) -> None:
    portfolio_summary = payload.get("portfolio_summary", {}) or {}
    savings_funnel = payload.get("savings_funnel", {}) or {}
    breakdowns = payload.get("breakdowns", {}) or {}
    currency_code = str(payload.get("currency_code") or "USD").upper()
    business_summary = payload.get("business_summary", {}) or {}
    parsed = business_summary.get("parsed") if isinstance(business_summary, dict) else None

    lines: List[str] = []
    if include_header:
        lines.extend(["OCI Rightsizer for Compute Workloads", "===================================="])
    if isinstance(parsed, dict) and str(parsed.get("summary") or "").strip():
        lines.append(str(parsed.get("summary")).strip())
    append_kv(
        lines,
        "Portfolio financial summary",
        [
            ("Current monthly run rate", format_money(portfolio_summary.get("current_monthly_run_rate"), currency_code)),
            ("Rightsized monthly run rate", format_money(portfolio_summary.get("optimized_monthly_run_rate"), currency_code)),
            ("Gross monthly savings", format_money(portfolio_summary.get("gross_monthly_savings"), currency_code)),
            ("Annualized savings", format_money(portfolio_summary.get("annualized_savings"), currency_code)),
        ],
    )
    append_kv(
        lines,
        "Savings funnel",
        [
            (
                "Auto-apply",
                f"{format_money(savings_funnel.get('auto_apply', {}).get('monthly_savings'), currency_code)} / mo | "
                f"{savings_funnel.get('auto_apply', {}).get('count', 0)} workloads",
            ),
            (
                "Review required",
                f"{format_money(savings_funnel.get('review_required', {}).get('monthly_savings'), currency_code)} / mo | "
                f"{savings_funnel.get('review_required', {}).get('count', 0)} workloads",
            ),
            (
                "Observe",
                f"{format_money(savings_funnel.get('observe', {}).get('monthly_savings'), currency_code)} / mo | "
                f"{savings_funnel.get('observe', {}).get('count', 0)} workloads",
            ),
        ],
    )
    lines.append("Rightsizing logic")
    lines.append("  - Short-window CPU and memory pressure is used to detect sustained scale-up signals.")
    lines.append("  - Long-window CPU and memory underutilization is used to identify scale-down candidates.")
    lines.append("  - Coverage is judged against the workload's active observed window, not stopped or off time.")
    lines.append("  - Sparse telemetry or too little active runtime blocks scale-down for that axis.")
    lines.append("  - Automated resize requires a Standard or Optimized Flex VM and explicit reboot acknowledgment.")
    lines.append("  - Generative AI is narrative-only and never changes deterministic decisions or savings.")
    lines.append("Top savings by compartments")
    compartment_items = (breakdowns.get("by_compartment", []) or [])[:5]
    if compartment_items:
        for item in compartment_items:
            lines.append(
                f"  - {item.get('compartment_name', 'n/a')}: {format_money(item.get('monthly_savings'), currency_code)} / mo | "
                f"{item.get('instance_count', 0)} workloads"
            )
    else:
        lines.append("  - None.")
    lines.append("Top savings by regions")
    region_items = (breakdowns.get("by_region", []) or [])[:5]
    if region_items:
        for item in region_items:
            lines.append(
                f"  - {item.get('region', 'n/a')}: {format_money(item.get('monthly_savings'), currency_code)} / mo | "
                f"{item.get('instance_count', 0)} workloads"
            )
    else:
        lines.append("  - None.")
    print("\n".join(lines).rstrip())


def render_interactive_recommendations(
    compartment: Dict[str, Any],
    rows: Sequence[Dict[str, Any]],
    currency_code: str = "USD",
    summary_text: Optional[str] = None,
) -> None:
    lines = ["", "Recommendation action plan", "--------------------------"]
    lines.append(f"Compartment: {compartment.get('compartment_name', 'n/a')}")
    if summary_text:
        lines.append(f"Summary: {summary_text}")
    auto_rows = [row for row in rows if str(row.get("action_tier")) == ACTION_AUTO_APPLY]
    review_rows = [row for row in rows if str(row.get("action_tier")) == ACTION_REVIEW_REQUIRED]

    def append_group(title: str, group_rows: Sequence[Dict[str, Any]], offset: int) -> int:
        if not group_rows:
            return offset
        lines.append(title)
        for row in group_rows:
            lines.append(
                f"{offset}. {row.get('display_name', 'n/a')} | "
                f"{format_money(row.get('estimated_monthly_savings'), currency_code)} / mo | "
                f"Safety category: {safety_class_label(row.get('safety_class'))}"
            )
            lines.append(f"   Current configuration: {config_text(row.get('ocpus'), row.get('memory_gb'))}")
            lines.append(f"   Recommended resize: {config_text(row.get('recommended_ocpus'), row.get('recommended_memory_gb'))}")
            lines.append(
                f"   Metrics: CPU p95 short {format_percent(row.get('short_cpu_p95'))} / "
                f"long {format_advisory_percent(row.get('long_cpu_p95'), row.get('long_cpu_coverage', {}))}, "
                f"Memory p95 short {format_percent(row.get('short_memory_p95'))} / "
                f"long {format_advisory_percent(row.get('long_memory_p95'), row.get('long_memory_coverage', {}))}"
            )
            lines.append(f"   Resize justification: {short_resize_justification(row)}")
            offset += 1
        return offset

    next_index = 1
    next_index = append_group("Auto-apply candidates", auto_rows, next_index)
    next_index = append_group("Review-required candidates", review_rows, next_index)
    print("\n".join(lines).rstrip())


def render_interactive_submission_summary(execution_payload: Dict[str, Any]) -> None:
    execution_results = execution_payload.get("execution_results", []) or []
    execution_summary = execution_payload.get("execution_summary", {}) or {}
    currency_code = str(execution_payload.get("currency_code") or "USD").upper()
    submitted_total = execution_summary.get("expected_monthly_savings_submitted")
    if submitted_total is None:
        submitted_total = execution_summary.get("expected_monthly_savings_identified")
    lines = ["", "Resize submission summary", "-------------------------"]
    for item in execution_results:
        lines.append(
            f"- {item.get('display_name', 'n/a')}: {config_text(item.get('recommended_ocpus'), item.get('recommended_memory_gb'))} | "
            f"{item.get('status', 'n/a')} | {format_money(item.get('estimated_monthly_savings'), currency_code)} / mo"
        )
    lines.append(f"Estimated monthly savings for submitted changes: {format_money(submitted_total, currency_code)}")
    print("\n".join(lines).rstrip())


def render_interactive_verification_summary(execution_payload: Dict[str, Any]) -> None:
    execution_results = [
        item
        for item in (execution_payload.get("execution_results", []) or [])
        if item.get("api_call_submitted") or item.get("verification")
    ]
    execution_summary = execution_payload.get("execution_summary", {}) or {}
    currency_code = str(execution_payload.get("currency_code") or "USD").upper()
    lines = ["", "Verification summary", "--------------------"]
    for item in execution_results:
        verification = item.get("verification") or {}
        final_shape = verification.get("final_shape_config", {}) or {}
        lines.append(
            f"- {item.get('display_name', 'n/a')}: {verification.get('final_state', 'n/a')} | "
            f"{config_text(final_shape.get('ocpus'), final_shape.get('memory_gb'))} | "
            f"{'VERIFIED' if verification.get('ok') else 'FAILED'}"
        )
    lines.append(
        f"Estimated monthly savings for verified configurations: {format_money(execution_summary.get('expected_monthly_savings_verified'), currency_code)}"
    )
    print("\n".join(lines).rstrip())


def render_metrics_block(summary: Dict[str, Any]) -> List[str]:
    return [
        f"count={summary.get('count', 0)}",
        f"latest={format_percent(summary.get('latest'))}",
        f"min={format_percent(summary.get('min'))}",
        f"max={format_percent(summary.get('max'))}",
        f"avg={format_percent(summary.get('avg'))}",
        f"p95={format_percent(summary.get('p95'))}",
    ]


def pretty_print(payload: Dict[str, Any], command: str, developer_details: bool = False) -> None:
    if payload.get("scope_complete") is False:
        print("Assessment is incomplete. Resolve scan errors or the instance limit before fleet execution.")
    def clean_list(values: Sequence[Any]) -> List[str]:
        cleaned: List[str] = []
        seen: set[str] = set()
        for value in values:
            text = str(value).strip()
            if not text or text in seen:
                continue
            cleaned.append(text)
            seen.add(text)
        return cleaned

    def user_visible_pricing_watchouts(pricing_payload: Dict[str, Any]) -> List[str]:
        pricing_payload = pricing_payload or {}
        warnings = clean_list(pricing_payload.get("warnings", []) or [])
        if developer_details:
            return warnings
        if pricing_payload.get("pricing_enabled"):
            return []
        if pricing_payload.get("pricing_attempted", True):
            return ["Pricing is unavailable, so savings should be reviewed manually."]
        return []

    def action_tier_label(value: Any) -> str:
        mapping = {
            ACTION_AUTO_APPLY: "Auto-apply",
            ACTION_REVIEW_REQUIRED: "Review required",
            ACTION_OBSERVE: "Observe",
        }
        return mapping.get(str(value or ""), str(value or "n/a"))

    def owner_label(item: Dict[str, Any]) -> str:
        return str(
            item.get("owner_or_cost_center")
            or item.get("compartment_name")
            or item.get("compartment_id")
            or "unassigned"
        )

    def config_text(ocpus: Any, memory_gb: Any) -> str:
        return f"{format_number(ocpus)} OCPU / {format_number(memory_gb)} GB"

    def annual_suffix(monthly_value: Any, annual_value: Any, currency_code: str) -> str:
        if safe_float(annual_value) is None and safe_float(monthly_value) is not None:
            annual_value = (safe_float(monthly_value) or 0.0) * 12.0
        if safe_float(annual_value) is None:
            return ""
        return f" | annual {format_money(annual_value, currency_code)}"

    def rationale_text(item: Dict[str, Any]) -> str:
        summary = str(item.get("rationale_summary") or "").strip()
        if summary:
            return summary
        reasons = clean_list(item.get("action_reasons", []) or [])
        if reasons:
            return reasons[0]
        return str(item.get("next_step") or "No rationale available.").strip()

    def coverage_percent_text(coverage: Dict[str, Any]) -> str:
        return format_effective_coverage_percent(coverage)

    def coverage_counts_text(coverage: Dict[str, Any]) -> str:
        coverage = coverage or {}
        observed = coverage.get("observed_points", 0)
        effective_expected = effective_expected_points(coverage)
        raw_expected = int(coverage.get("expected_points", effective_expected) or effective_expected)
        if (
            str(coverage.get("coverage_basis") or "") == "observed_metric_span"
            and effective_expected > 0
            and effective_expected != raw_expected
        ):
            raw_ratio = safe_float(coverage.get("coverage_ratio")) or 0.0
            return (
                f"{observed}/{effective_expected} ({coverage_percent_text(coverage)}; "
                f"{observed}/{raw_expected} full-window at {format_percent(raw_ratio * 100.0)})"
            )
        return f"{observed}/{effective_expected or raw_expected} ({coverage_percent_text(coverage)})"

    def metrics_quality_text(summary: Dict[str, Any]) -> str:
        return (
            f"{summary.get('strong_metrics_coverage_count', 0)} strong / "
            f"{summary.get('review_metrics_coverage_count', 0)} review / "
            f"{summary.get('sparse_metrics_coverage_count', 0)} sparse"
        )

    def top_reason_counts(rows: Sequence[Dict[str, Any]], limit: int = 3) -> List[Tuple[str, int]]:
        counts: Dict[str, int] = {}
        for row in rows:
            reason = rationale_text(row)
            counts[reason] = counts.get(reason, 0) + 1
        return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]

    def build_key_caveats(rows: Sequence[Dict[str, Any]], extra: Sequence[Any]) -> List[str]:
        caveats = clean_list(extra)
        for row in rows:
            if str(row.get("action_tier") or "") != ACTION_AUTO_APPLY:
                reasons = clean_list(row.get("action_reasons", []) or [])
                if reasons:
                    caveats.append(reasons[0])
        return clean_list(caveats)[:6]

    def format_daily_p95(day_map: Any, limit: int = 5) -> str:
        if not isinstance(day_map, dict) or not day_map:
            return "none"
        items = sorted((str(key), safe_float(value)) for key, value in day_map.items())
        rendered = [
            f"{day}={value:.1f}%"
            for day, value in items[:limit]
            if value is not None
        ]
        if len(items) > limit:
            rendered.append("...")
        return ", ".join(rendered) if rendered else "none"

    def append_watchouts(lines: List[str], title: str, values: Sequence[Any]) -> None:
        cleaned = clean_list(values)
        lines.append(title)
        if not cleaned:
            lines.append("  None.")
            return
        for item in cleaned:
            lines.append(f"  - {item}")

    def append_business_summary(lines: List[str]) -> None:
        business_summary = payload.get("business_summary") or {}
        parsed = business_summary.get("parsed") if isinstance(business_summary, dict) else None
        if isinstance(parsed, dict):
            summary_text = str(parsed.get("summary") or "").strip()
            if summary_text:
                lines.append(f"Summary: {summary_text}")

    def business_watchouts() -> List[str]:
        watchouts: List[Any] = []
        business_summary = payload.get("business_summary") or {}
        if isinstance(business_summary, dict):
            parsed = business_summary.get("parsed")
            if isinstance(parsed, dict):
                watchouts.extend(parsed.get("watchouts", []) or [])
            watchouts.extend(business_summary.get("warnings", []) or [])
        return clean_list(watchouts)

    def append_top_breakdown(lines: List[str], title: str, items: Sequence[Dict[str, Any]], key_name: str, currency_code: str) -> None:
        lines.append(title)
        if not items:
            lines.append("  None.")
            return
        for item in list(items)[:3]:
            lines.append(
                f"  - {item.get(key_name, 'n/a')}: {format_money(item.get('monthly_savings'), currency_code)} / mo | "
                f"{item.get('instance_count', 0)} workloads | {item.get('auto_apply_count', 0)} auto-apply"
            )

    def append_rightsizing_logic(lines: List[str]) -> None:
        lines.append("Rightsizing logic")
        lines.append("  - Short-window CPU and memory pressure is used to detect sustained scale-up signals.")
        lines.append("  - Long-window CPU and memory underutilization is used to identify scale-down candidates.")
        lines.append("  - Sparse data blocks downsizing for that axis.")
        lines.append("  - Automated resize requires a Standard or Optimized Flex VM and explicit reboot acknowledgment.")
        lines.append("  - Generative AI is summary-only and never changes deterministic decisions or savings.")

    def append_candidate_group(
        lines: List[str],
        title: str,
        rows: Sequence[Dict[str, Any]],
        *,
        currency_code: str,
        limit: int,
        include_next_step: bool,
    ) -> None:
        lines.append(title)
        if not rows:
            lines.append("  None.")
            return
        for index, row in enumerate(list(rows)[:limit], start=1):
            lines.append(
                f"{index}. {row.get('display_name', 'n/a')} | {owner_label(row)} | "
                f"{config_text(row.get('ocpus'), row.get('memory_gb'))} -> "
                f"{config_text(row.get('recommended_ocpus'), row.get('recommended_memory_gb'))} | "
                f"{format_money(row.get('estimated_monthly_savings'), currency_code)} / mo | "
                f"{action_tier_label(row.get('action_tier'))}"
            )
            lines.append(
                f"   Basis: CPU {format_percent(row.get('short_cpu_p95'))} short / {format_percent(row.get('long_cpu_p95'))} long, "
                f"Memory {format_percent(row.get('short_memory_p95'))} short / {format_percent(row.get('long_memory_p95'))} long, "
                f"coverage CPU {coverage_percent_text(row.get('long_cpu_coverage', {}))} / "
                f"Memory {coverage_percent_text(row.get('long_memory_coverage', {}))}, "
                f"pricing {row.get('pricing_source', 'n/a')} / {row.get('pricing_confidence', 'n/a')}"
            )
            rationale_line = f"   Rationale: {rationale_text(row)}"
            if include_next_step:
                rationale_line += f" Next: {row.get('next_step', 'n/a')}"
            lines.append(rationale_line)
            if developer_details:
                lines.append(
                    f"   Coverage counts: short CPU {coverage_counts_text(row.get('short_cpu_coverage', {}))}, "
                    f"short Memory {coverage_counts_text(row.get('short_memory_coverage', {}))}, "
                    f"long CPU {coverage_counts_text(row.get('long_cpu_coverage', {}))}, "
                    f"long Memory {coverage_counts_text(row.get('long_memory_coverage', {}))}"
                )
                lines.append(
                    f"   Sample counts: short CPU {format_number(row.get('short_cpu_count'), 0)}, "
                    f"short Memory {format_number(row.get('short_memory_count'), 0)}, "
                    f"long CPU {format_number(row.get('long_cpu_count'), 0)}, "
                    f"long Memory {format_number(row.get('long_memory_count'), 0)}"
                )
                lines.append(
                    f"   Daily p95: CPU {format_daily_p95(row.get('long_cpu_daily_p95'))} | "
                    f"Memory {format_daily_p95(row.get('long_memory_daily_p95'))}"
                )
                if row.get("detected_part_numbers"):
                    part_numbers = row.get("detected_part_numbers") or {}
                    lines.append(
                        f"   Pricing internals: CPU part {part_numbers.get('cpu') or 'n/a'}, "
                        f"Memory part {part_numbers.get('memory') or 'n/a'}"
                    )
                if row.get("validation_errors"):
                    lines.append(f"   Validation errors: {', '.join(str(item) for item in row.get('validation_errors', []))}")
                if row.get("shape_limits"):
                    limits = row.get("shape_limits") or {}
                    lines.append(
                        f"   Shape limits: OCPUs {format_number(limits.get('min_ocpus'))}-{format_number(limits.get('max_ocpus'))}, "
                        f"Memory {format_number(limits.get('min_memory_gb'))}-{format_number(limits.get('max_memory_gb'))} GB"
                    )
                if row.get("policy_thresholds"):
                    thresholds = row.get("policy_thresholds") or {}
                    lines.append(
                        f"   Thresholds: CPU down {format_percent(thresholds.get('cpu_scale_down_threshold'))}, "
                        f"Memory down {format_percent(thresholds.get('memory_scale_down_threshold'))}, "
                        f"upsize factor {format_number(thresholds.get('upsize_factor'))}"
                    )
                raw_warnings = clean_list(row.get("warnings", []) or [])
                if raw_warnings:
                    lines.append(f"   Raw warnings: {' | '.join(raw_warnings[:3])}")
                pricing_warnings = clean_list(row.get("pricing_warnings", []) or [])
                if pricing_warnings:
                    lines.append(f"   Pricing warnings: {' | '.join(pricing_warnings[:3])}")

    def append_execution_results(lines: List[str], results: Sequence[Dict[str, Any]], currency_code: str, limit: int) -> None:
        lines.append("Per-instance results")
        shown = [
            item
            for item in results
            if item.get("status") in {"SUBMITTED", "VERIFIED", "FAILED"}
        ]
        if not shown:
            lines.append("  No submitted or failed changes.")
            return
        for index, item in enumerate(shown[:limit], start=1):
            lines.append(
                f"{index}. {item.get('display_name', 'n/a')} | {owner_label(item)} | "
                f"{item.get('status', 'n/a')} | {format_money(item.get('estimated_monthly_savings'), currency_code)} / mo"
            )
            lines.append(
                f"   Config: {config_text(item.get('ocpus'), item.get('memory_gb'))} -> "
                f"{config_text(item.get('recommended_ocpus'), item.get('recommended_memory_gb'))} | "
                f"pricing {item.get('pricing_source', 'n/a')} / {item.get('pricing_confidence', 'n/a')}"
            )
            lines.append(
                f"   Basis: CPU {format_percent(item.get('short_cpu_p95'))} short / {format_percent(item.get('long_cpu_p95'))} long, "
                f"Memory {format_percent(item.get('short_memory_p95'))} short / {format_percent(item.get('long_memory_p95'))} long, "
                f"coverage CPU {coverage_percent_text(item.get('long_cpu_coverage', {}))} / "
                f"Memory {coverage_percent_text(item.get('long_memory_coverage', {}))}"
            )
            lines.append(f"   Rationale: {rationale_text(item)}")
            if item.get("message"):
                lines.append(f"   Result: {item['message']}")
            if item.get("verification"):
                verification = item.get("verification") or {}
                lines.append(
                    f"   Verification: {'OK' if verification.get('ok') else 'FAILED'} | "
                    f"state {verification.get('final_state', 'n/a')} | "
                    f"final {config_text(verification.get('final_shape_config', {}).get('ocpus'), verification.get('final_shape_config', {}).get('memory_gb'))}"
                )
            if developer_details:
                if item.get("validation_errors"):
                    lines.append(f"   Validation errors: {', '.join(str(v) for v in item.get('validation_errors', []))}")
                if item.get("shape_limits"):
                    limits = item.get("shape_limits") or {}
                    lines.append(
                        f"   Shape limits: OCPUs {format_number(limits.get('min_ocpus'))}-{format_number(limits.get('max_ocpus'))}, "
                        f"Memory {format_number(limits.get('min_memory_gb'))}-{format_number(limits.get('max_memory_gb'))} GB"
                    )
                pricing_warnings = clean_list(item.get("pricing_warnings", []) or [])
                if pricing_warnings:
                    lines.append(f"   Pricing warnings: {' | '.join(pricing_warnings[:3])}")

    def append_single_instance_developer_details(lines: List[str], payload_data: Dict[str, Any]) -> None:
        instance = payload_data.get("instance", {})
        recommendation = payload_data.get("recommendation", {})
        metrics_summary = payload_data.get("metrics_summary", {})
        short_window = metrics_summary.get("short_window", {})
        long_window = metrics_summary.get("long_window", {})
        pricing = payload_data.get("pricing_estimate", {})
        rationale = recommendation.get("rationale", {}) or {}
        lines.append("Developer details")
        lines.append(
            f"  Coverage counts: short CPU {coverage_counts_text(short_window.get('coverage', {}).get('cpu', {}))}, "
            f"short Memory {coverage_counts_text(short_window.get('coverage', {}).get('memory', {}))}, "
            f"long CPU {coverage_counts_text(long_window.get('coverage', {}).get('cpu', {}))}, "
            f"long Memory {coverage_counts_text(long_window.get('coverage', {}).get('memory', {}))}"
        )
        lines.append(
            f"  Sample counts: short CPU {format_number(short_window.get('cpu', {}).get('count'), 0)}, "
            f"short Memory {format_number(short_window.get('memory', {}).get('count'), 0)}, "
            f"long CPU {format_number(long_window.get('cpu', {}).get('count'), 0)}, "
            f"long Memory {format_number(long_window.get('memory', {}).get('count'), 0)}"
        )
        lines.append(
            f"  Daily p95: CPU {format_daily_p95(rationale.get('cpu', {}).get('long_daily_p95'))} | "
            f"Memory {format_daily_p95(rationale.get('memory', {}).get('long_daily_p95'))}"
        )
        lines.append(
            f"  Decisions: overall={recommendation.get('overall_decision', 'n/a')}, "
            f"CPU={recommendation.get('cpu_decision', 'n/a')}, Memory={recommendation.get('memory_decision', 'n/a')}, "
            f"safety={recommendation.get('safety_class', 'n/a')}"
        )
        if recommendation.get("shape_limits"):
            limits = recommendation.get("shape_limits") or {}
            lines.append(
                f"  Shape limits: OCPUs {format_number(limits.get('min_ocpus'))}-{format_number(limits.get('max_ocpus'))}, "
                f"step {format_number(limits.get('ocpu_step'))}; Memory {format_number(limits.get('min_memory_gb'))}-"
                f"{format_number(limits.get('max_memory_gb'))} GB, step {format_number(limits.get('memory_step_gb'))}"
            )
        if rationale.get("policy"):
            policy = rationale.get("policy") or {}
            lines.append(
                f"  Thresholds: CPU up {format_percent(policy.get('cpu_scale_up_threshold'))}, "
                f"CPU down {format_percent(policy.get('cpu_scale_down_threshold'))}, "
                f"Memory up {format_percent(policy.get('memory_scale_up_threshold'))}, "
                f"Memory down {format_percent(policy.get('memory_scale_down_threshold'))}, "
                f"upsize factor {format_number(policy.get('upsize_factor'))}"
            )
        if pricing:
            lines.append(
                f"  Pricing internals: current hourly {format_money(pricing.get('current_hourly_cost'), str(pricing.get('currency_code') or 'USD').upper(), 4)}, "
                f"optimized hourly {format_money(pricing.get('estimated_new_hourly_cost'), str(pricing.get('currency_code') or 'USD').upper(), 4)}, "
                f"delta {format_money(pricing.get('estimated_hourly_delta'), str(pricing.get('currency_code') or 'USD').upper(), 4)}"
            )
            if pricing.get("detected_part_numbers"):
                parts = pricing.get("detected_part_numbers") or {}
                lines.append(
                    f"  Detected part numbers: CPU {parts.get('cpu') or 'n/a'}, "
                    f"Memory {parts.get('memory') or 'n/a'}"
                )
        raw_warnings = clean_list(
            list(recommendation.get("warnings", []) or [])
            + list(pricing.get("warnings", []) or [])
            + list(payload_data.get("warnings", []) or [])
        )
        if raw_warnings:
            lines.append(f"  Raw warnings: {' | '.join(raw_warnings[:5])}")
        if recommendation.get("validation_errors"):
            lines.append(f"  Validation errors: {', '.join(str(item) for item in recommendation.get('validation_errors', []))}")
        lines.append(f"  Instance: {instance.get('instance_id', 'n/a')}")

    def append_single_instance_basis(lines: List[str], payload_data: Dict[str, Any], currency_code: str, indent: str = "  ") -> None:
        instance = payload_data.get("instance", {}) or {}
        recommendation = payload_data.get("recommendation", {}) or {}
        explanation_basis = payload_data.get("explanation_basis", {}) or {}
        financial_summary = payload_data.get("financial_summary", {}) or {}
        lines.append(
            f"{indent}Recommendation: {config_text(instance.get('ocpus'), instance.get('memory_gb'))} -> "
            f"{config_text(recommendation.get('recommended_ocpus'), recommendation.get('recommended_memory_gb'))} | "
            f"{action_tier_label(payload_data.get('action_summary', {}).get('action_tier'))}"
        )
        lines.append(
            f"{indent}Basis: CPU {format_percent(explanation_basis.get('short_cpu_p95'))} short / {format_percent(explanation_basis.get('long_cpu_p95'))} long, "
            f"Memory {format_percent(explanation_basis.get('short_memory_p95'))} short / {format_percent(explanation_basis.get('long_memory_p95'))} long, "
            f"coverage CPU {coverage_percent_text(payload_data.get('metrics_summary', {}).get('long_window', {}).get('coverage', {}).get('cpu', {}))} / "
            f"Memory {coverage_percent_text(payload_data.get('metrics_summary', {}).get('long_window', {}).get('coverage', {}).get('memory', {}))}"
        )
        lines.append(
            f"{indent}Pricing: {financial_summary.get('pricing_source', 'n/a')} / {financial_summary.get('pricing_confidence', 'n/a')} | "
            f"savings {format_money(financial_summary.get('monthly_savings'), currency_code)} / mo"
            f"{annual_suffix(financial_summary.get('monthly_savings'), financial_summary.get('annualized_savings'), currency_code)}"
        )
        lines.append(f"{indent}Rationale: {rationale_text(recommendation)}")

    if command == "discover":
        lines = ["OCI Rightsizer Discovery", "======================="]
        items = payload.get("items", [])
        lines.append(f"Instances found: {len(items)}")
        for index, item in enumerate(items, start=1):
            lines.append(
                f"{index}. {item.get('display_name', 'n/a')} | {item.get('lifecycle_state', 'n/a')} | "
                f"{item.get('shape', 'n/a')} | OCPUs={format_number(item.get('ocpus'))} | "
                f"MemoryGB={format_number(item.get('memory_gb'))}"
            )
        append_watchouts(lines, "Watch-outs", payload.get("warnings", []) or [])
        print("\n".join(lines).rstrip())
        return

    if command == "metrics":
        metrics = payload.get("metrics", {})
        lines = ["OCI Rightsizer Metrics", "====================="]
        append_kv(
            lines,
            "Instance",
            [
                ("Name", payload.get("display_name", "n/a")),
                ("OCID", payload.get("instance_id", "n/a")),
                ("Compartment", payload.get("compartment_id", "n/a")),
            ],
        )
        append_kv(
            lines,
            "Window",
            [
                ("Start", payload.get("window", {}).get("start_time", "n/a")),
                ("End", payload.get("window", {}).get("end_time", "n/a")),
                ("Resolution", payload.get("window", {}).get("resolution", "n/a")),
            ],
        )
        append_kv(
            lines,
            "Coverage",
            [
                ("CPU", format_coverage(payload.get("coverage", {}).get("cpu", {}))),
                ("Memory", format_coverage(payload.get("coverage", {}).get("memory", {}))),
            ],
        )
        for label, metric_name in [("CPU", CPU_METRIC), ("Memory", MEM_METRIC)]:
            summary = metrics.get(metric_name, {}).get("summary", {})
            lines.append(f"{label}: " + ", ".join(render_metrics_block(summary)))
        append_watchouts(lines, "Watch-outs", payload.get("warnings", []) or [])
        print("\n".join(lines).rstrip())
        return

    if command == "scan" and payload.get("scope_mode") == "scoped":
        portfolio_summary = payload.get("portfolio_summary", {}) or {}
        savings_funnel = payload.get("savings_funnel", {}) or {}
        breakdowns = payload.get("breakdowns", {}) or {}
        currency_code = str(payload.get("currency_code") or "USD").upper()
        lines = ["OCI Rightsizer for Compute Workloads", "===================================="]
        append_business_summary(lines)
        append_kv(
            lines,
            "Portfolio financial summary",
            [
                ("Current monthly run rate", format_money(portfolio_summary.get("current_monthly_run_rate"), currency_code)),
                ("Rightsized monthly run rate", format_money(portfolio_summary.get("optimized_monthly_run_rate"), currency_code)),
                ("Gross monthly savings", format_money(portfolio_summary.get("gross_monthly_savings"), currency_code)),
                (
                    "Annualized savings",
                    format_money(portfolio_summary.get("annualized_savings"), currency_code),
                ),
            ],
        )
        append_kv(
            lines,
            "Savings funnel",
            [
                (
                    "Auto-apply",
                    f"{format_money(savings_funnel.get('auto_apply', {}).get('monthly_savings'), currency_code)} / mo | "
                    f"{savings_funnel.get('auto_apply', {}).get('count', 0)} workloads",
                ),
                (
                    "Review required",
                    f"{format_money(savings_funnel.get('review_required', {}).get('monthly_savings'), currency_code)} / mo | "
                    f"{savings_funnel.get('review_required', {}).get('count', 0)} workloads",
                ),
                (
                    "Observe",
                    f"{format_money(savings_funnel.get('observe', {}).get('monthly_savings'), currency_code)} / mo | "
                    f"{savings_funnel.get('observe', {}).get('count', 0)} workloads",
                ),
            ],
        )
        append_rightsizing_logic(lines)
        append_top_breakdown(
            lines,
            "Top savings by compartments",
            breakdowns.get("by_compartment", []) or [],
            "compartment_name",
            currency_code,
        )
        append_top_breakdown(
            lines,
            "Top savings by regions",
            breakdowns.get("by_region", []) or [],
            "region",
            currency_code,
        )
        print("\n".join(lines).rstrip())
        return

    if command == "recommend" and payload.get("scope_mode") == "scoped":
        currency_code = str(payload.get("currency_code") or "USD").upper()
        candidate_groups = payload.get("candidate_groups", {}) or {}
        recommend_summary = payload.get("recommend_summary") or build_recommend_summary(payload.get("rows", []) or [])
        top_limit = payload.get("top_limit", 10)

        def append_action_plan_group(lines: List[str], title: str, rows: Sequence[Dict[str, Any]]) -> None:
            lines.append(title)
            if not rows:
                lines.append("  None.")
                return
            for index, row in enumerate(list(rows)[:top_limit], start=1):
                lines.append(
                    f"{index}. {row.get('display_name', 'n/a')} | "
                    f"{format_money(row.get('estimated_monthly_savings'), currency_code)} / mo | "
                    f"Safety category: {safety_class_label(row.get('safety_class'))}"
                )
                lines.append(f"   Current configuration: {config_text(row.get('ocpus'), row.get('memory_gb'))}")
                lines.append(f"   Recommended resize: {config_text(row.get('recommended_ocpus'), row.get('recommended_memory_gb'))}")
                lines.append(
                    f"   Metrics: CPU p95 short {format_percent(row.get('short_cpu_p95'))} / "
                    f"long {format_advisory_percent(row.get('long_cpu_p95'), row.get('long_cpu_coverage', {}))}, "
                    f"Memory p95 short {format_percent(row.get('short_memory_p95'))} / "
                    f"long {format_advisory_percent(row.get('long_memory_p95'), row.get('long_memory_coverage', {}))}"
                )
                lines.append(f"   Resize justification: {short_resize_justification(row)}")
                if developer_details:
                    lines.append(
                        f"   Pricing: {row.get('pricing_source', 'n/a')} / {row.get('pricing_confidence', 'n/a')} | "
                        f"Next: {row.get('next_step', 'n/a')}"
                    )
                    raw_reasons = clean_list(row.get("action_reasons", []) or [])
                    if raw_reasons:
                        lines.append(f"   Review note: {raw_reasons[0]}")
                    raw_warnings = clean_list(row.get("warnings", []) or [])
                    if raw_warnings:
                        lines.append(f"   Raw warnings: {' | '.join(raw_warnings[:3])}")

        lines = ["OCI Rightsizer Recommendation - Ranked Action Plan", "=============================================="]
        append_business_summary(lines)
        append_kv(
            lines,
            "Action-plan summary",
            [
                ("Actionable workloads", recommend_summary.get("actionable_workloads", 0)),
                ("Auto-apply count", recommend_summary.get("auto_apply_count", 0)),
                ("Review-required count", recommend_summary.get("review_required_count", 0)),
                ("Gross monthly savings", format_money(recommend_summary.get("gross_monthly_savings"), currency_code)),
                ("Annualized savings", format_money(recommend_summary.get("annualized_savings"), currency_code)),
            ],
        )
        append_action_plan_group(lines, "Auto-apply candidates", candidate_groups.get("auto_apply", []) or [])
        append_action_plan_group(lines, "Review-required candidates", candidate_groups.get("review_required", []) or [])
        print("\n".join(lines).rstrip())
        return

    if command == "recommend" and payload.get("scope_mode") != "scoped":
        instance = payload.get("instance", {}) or {}
        recommendation = payload.get("recommendation", {}) or {}
        financial_summary = payload.get("financial_summary", {}) or {}
        action_summary = payload.get("action_summary", {}) or {}
        explanation_basis = payload.get("explanation_basis", {}) or {}
        pricing = payload.get("pricing_estimate", {}) or {}
        pricing_currency = str(pricing.get("currency_code") or payload.get("currency_code") or "USD").upper()
        lines = ["OCI Rightsizer Recommendation - Action Plan", "========================================"]
        lines.append(
            f"Workload: {instance.get('display_name', 'n/a')} | {owner_label(payload)} | "
            f"{instance.get('region', payload.get('region', 'n/a'))}"
        )
        append_business_summary(lines)
        append_kv(
            lines,
            "Financial impact",
            [
                ("Current monthly cost", format_money(financial_summary.get("current_monthly_cost"), pricing_currency)),
                ("Optimized monthly cost", format_money(financial_summary.get("optimized_monthly_cost"), pricing_currency)),
                (
                    "Monthly savings",
                    f"{format_money(financial_summary.get('monthly_savings'), pricing_currency)}"
                    f"{annual_suffix(financial_summary.get('monthly_savings'), financial_summary.get('annualized_savings'), pricing_currency)}",
                ),
                ("Pricing source", financial_summary.get("pricing_source", "n/a")),
                ("Pricing confidence", financial_summary.get("pricing_confidence", "n/a")),
            ],
        )
        append_kv(
            lines,
            "Recommended change",
            [
                ("Current config", config_text(instance.get("ocpus"), instance.get("memory_gb"))),
                (
                    "Recommended config",
                    config_text(recommendation.get("recommended_ocpus"), recommendation.get("recommended_memory_gb")),
                ),
                ("Action tier", action_tier_label(action_summary.get("action_tier"))),
                ("Next step", action_summary.get("next_step", "n/a")),
            ],
        )
        append_kv(
            lines,
            "Decision basis",
            [
                ("Short CPU p95", format_percent(explanation_basis.get("short_cpu_p95"))),
                ("Long CPU p95", format_percent(explanation_basis.get("long_cpu_p95"))),
                ("Short memory p95", format_percent(explanation_basis.get("short_memory_p95"))),
                ("Long memory p95", format_percent(explanation_basis.get("long_memory_p95"))),
                ("Long CPU coverage", coverage_percent_text(payload.get("metrics_summary", {}).get("long_window", {}).get("coverage", {}).get("cpu", {}))),
                ("Long memory coverage", coverage_percent_text(payload.get("metrics_summary", {}).get("long_window", {}).get("coverage", {}).get("memory", {}))),
            ],
        )
        lines.append("Why this recommendation exists")
        lines.append(f"  {rationale_text(recommendation)}")
        caveats = clean_list(recommendation.get("action_reasons", []) or [])
        caveats.extend(clean_list(recommendation.get("warnings", []) or []))
        caveats.extend(user_visible_pricing_watchouts(pricing))
        caveats.extend(clean_list(payload.get("warnings", []) or []))
        append_watchouts(lines, "Caveats / watch-outs", caveats + business_watchouts())
        if developer_details:
            append_single_instance_developer_details(lines, payload)
        print("\n".join(lines).rstrip())
        return

    if command == "apply":
        execution_summary = payload.get("execution_summary", {}) or {}
        currency_code = str(payload.get("currency_code") or payload.get("pricing_estimate", {}).get("currency_code") or "USD").upper()
        lines = ["OCI Rightsizer Apply - Execution Summary", "========================================"]
        append_business_summary(lines)
        append_kv(
            lines,
            "Changes",
            [
                ("Considered", execution_summary.get("candidates_considered", 0)),
                ("Eligible", execution_summary.get("candidates_eligible", 0)),
                ("Submitted", execution_summary.get("changes_submitted", 0)),
                ("Verified", execution_summary.get("changes_verified", 0)),
                ("Failed", execution_summary.get("changes_failed", 0)),
                ("Skipped", execution_summary.get("changes_skipped", 0)),
            ],
        )
        if payload.get("scope_mode") != "scoped":
            append_single_instance_basis(lines, payload, currency_code)
        append_kv(
            lines,
            "Expected savings",
            [
                (
                    "Identified",
                    f"{format_money(execution_summary.get('expected_monthly_savings_identified'), currency_code)} / mo"
                    f"{annual_suffix(execution_summary.get('expected_monthly_savings_identified'), None, currency_code)}",
                ),
                (
                    "Submitted",
                    f"{format_money(execution_summary.get('expected_monthly_savings_submitted'), currency_code)} / mo"
                    f"{annual_suffix(execution_summary.get('expected_monthly_savings_submitted'), None, currency_code)}",
                ),
                (
                    "Verified",
                    f"{format_money(execution_summary.get('expected_monthly_savings_verified'), currency_code)} / mo"
                    f"{annual_suffix(execution_summary.get('expected_monthly_savings_verified'), None, currency_code)}",
                ),
                (
                    "Deferred",
                    f"{format_money(execution_summary.get('expected_monthly_savings_deferred'), currency_code)} / mo"
                    f"{annual_suffix(execution_summary.get('expected_monthly_savings_deferred'), None, currency_code)}",
                ),
            ],
        )
        append_execution_results(lines, payload.get("execution_results", []) or [], currency_code, 10)
        verification_items = [
            item for item in (payload.get("execution_results", []) or []) if item.get("verification")
        ]
        lines.append("Short verification summary")
        if verification_items:
            ok_count = sum(1 for item in verification_items if item.get("verification", {}).get("ok"))
            fail_count = len(verification_items) - ok_count
            lines.append(
                f"  Verified {ok_count} workloads; {fail_count} workloads still need attention."
            )
        else:
            lines.append("  No post-change verification was performed.")
        blockers = build_key_caveats(
            payload.get("execution_results", []) or [],
            list(payload.get("warnings", []) or []) + business_watchouts(),
        )
        append_watchouts(lines, "Key caveats", blockers)
        if developer_details and payload.get("scope_mode") != "scoped":
            append_single_instance_developer_details(lines, payload)
        print("\n".join(lines).rstrip())
        return

    if command == "run":
        execution_summary = payload.get("execution_summary", {}) or {}
        currency_code = str(payload.get("currency_code") or payload.get("pricing_estimate", {}).get("currency_code") or "USD").upper()
        lines = ["OCI Rightsizer Run - FinOps Workflow Summary", "=========================================="]
        append_business_summary(lines)
        lines.append("Identified opportunity")
        lines.append(
            f"  {format_money(execution_summary.get('expected_monthly_savings_identified'), currency_code)} / mo"
            f"{annual_suffix(execution_summary.get('expected_monthly_savings_identified'), None, currency_code)}"
            f" across {execution_summary.get('candidates_considered', 0)} workloads considered."
        )
        lines.append("Eligible for action")
        lines.append(
            f"  {format_money(execution_summary.get('expected_monthly_savings_eligible'), currency_code)} / mo | "
            f"{execution_summary.get('candidates_eligible', 0)} eligible workloads."
        )
        if payload.get("scope_mode") != "scoped":
            append_single_instance_basis(lines, payload, currency_code)
        lines.append("Submitted changes")
        lines.append(
            f"  {format_money(execution_summary.get('expected_monthly_savings_submitted'), currency_code)} / mo | "
            f"{execution_summary.get('changes_submitted', 0)} submitted changes."
        )
        lines.append("Verified changes")
        lines.append(
            f"  {format_money(execution_summary.get('expected_monthly_savings_verified'), currency_code)} / mo | "
            f"{execution_summary.get('changes_verified', 0)} verified changes."
        )
        lines.append("Deferred savings")
        lines.append(
            f"  {format_money(execution_summary.get('expected_monthly_savings_deferred'), currency_code)} / mo | "
            f"{execution_summary.get('changes_skipped', 0)} skipped or pending workloads."
        )
        blockers = build_key_caveats(
            payload.get("execution_results", []) or [],
            list(payload.get("warnings", []) or []) + business_watchouts(),
        )
        append_watchouts(lines, "Key blockers", blockers)
        lines.append("Next step")
        if execution_summary.get("changes_failed", 0) > 0:
            lines.append("  Investigate failed changes before the next apply wave.")
        elif execution_summary.get("changes_submitted", 0) > execution_summary.get("changes_verified", 0):
            lines.append("  Continue verification until all submitted changes settle.")
        elif execution_summary.get("candidates_eligible", 0) > execution_summary.get("changes_submitted", 0):
            lines.append("  Review deferred candidates and approve the next execution batch.")
        elif execution_summary.get("changes_verified", 0) > 0:
            lines.append("  Measure actual billing and application health after resizing; review the remaining backlog.")
        else:
            lines.append("  Review the observe and review-required backlog before taking action.")
        if developer_details:
            append_execution_results(lines, payload.get("execution_results", []) or [], currency_code, 10)
        if developer_details and payload.get("scope_mode") != "scoped":
            append_single_instance_developer_details(lines, payload)
        print("\n".join(lines).rstrip())
        return

    if command == "resize":
        lines = ["OCI Rightsizer Resize", "===================="]
        instance = payload.get("instance", {})
        append_kv(
            lines,
            "Instance",
            [
                ("Name", instance.get("display_name", "n/a")),
                ("OCID", instance.get("instance_id", "n/a")),
                ("Shape", instance.get("shape", "n/a")),
                ("Lifecycle", instance.get("lifecycle_state", "n/a")),
            ],
        )
        append_kv(
            lines,
            "Requested Shape Config",
            [
                ("OCPUs", format_number(payload.get("requested_shape_config", {}).get("ocpus"))),
                ("Memory (GB)", format_number(payload.get("requested_shape_config", {}).get("memory_gb"))),
            ],
        )
        lines.append(f"Status: {payload.get('message', 'n/a')}")
        for key in ("warnings", "validation_errors"):
            values = payload.get(key, []) or []
            if values:
                lines.append(key.replace("_", " ").title())
                for item in values:
                    lines.append(f"  - {item}")
        verification = payload.get("verification")
        if verification:
            lines.append("Verification")
            lines.append(f"  Result: {'OK' if verification.get('ok') else 'FAILED'}")
            if verification.get("error"):
                lines.append(f"  Error: {verification['error']}")
        print("\n".join(lines).rstrip())
        return

    if command == "verify":
        lines = ["OCI Rightsizer Verification", "=========================="]
        lines.append(f"Result: {'OK' if payload.get('ok') else 'FAILED'}")
        lines.append(f"Final state: {payload.get('final_state', 'n/a')}")
        lines.append(f"Final OCPUs: {format_number(payload.get('final_shape_config', {}).get('ocpus'))}")
        lines.append(f"Final Memory (GB): {format_number(payload.get('final_shape_config', {}).get('memory_gb'))}")
        if payload.get("error"):
            lines.append(f"Error: {payload['error']}")
        print("\n".join(lines).rstrip())
        return

    if command == "pricing-debug":
        lines = ["OCI Rightsizer Pricing Debug", "==========================="]
        request = payload.get("request", {})
        custom_filters = request.get("custom_filters", []) or []
        append_kv(
            lines,
            "Request",
            [
                ("Instance ID", payload.get("instance_id", "n/a")),
                ("Lookback days", payload.get("lookback_days", "n/a")),
                ("Time usage started", request.get("time_usage_started", "n/a")),
                ("Time usage ended", request.get("time_usage_ended", "n/a")),
                ("Granularity", request.get("granularity", "n/a")),
                ("Query type", request.get("query_type", "n/a")),
                ("Group by", ", ".join(request.get("group_by", []) or []) or "n/a"),
                ("Use default filters", request.get("use_default_filters", False)),
                (
                    "Custom filters",
                    ", ".join(f"{item[0]}={item[1]}" for item in custom_filters) if custom_filters else "none",
                ),
                ("Filter", json.dumps(request.get("filter"), separators=(",", ":")) if request.get("filter") is not None else "null"),
                ("Item count", payload.get("item_count", 0)),
            ],
        )
        if payload.get("error"):
            lines.append(f"Error: {payload['error']}")
        items = payload.get("items", []) or []
        if not items:
            lines.append("No Usage API rows returned.")
        else:
            lines.append("Sample rows")
            for item in items[:5]:
                lines.append(f"  {json.dumps(item, separators=(',', ':'))}")
        print("\n".join(lines).rstrip())
        return

    json_print(payload)


def parse_resolution_to_minutes(resolution: str) -> int:
    text = str(resolution).strip().lower()
    if text.endswith("m"):
        return max(1, int(text[:-1]))
    if text.endswith("h"):
        return max(1, int(text[:-1]) * 60)
    raise ValueError(f"Unsupported resolution format: {resolution}")


def expected_points_for_minutes(total_minutes: float, resolution: str) -> int:
    resolution_minutes = parse_resolution_to_minutes(resolution)
    return max(1, math.ceil(max(0.0, float(total_minutes)) / resolution_minutes))


def expected_points(hours: int, resolution: str) -> int:
    return expected_points_for_minutes(hours * 60, resolution)


@dataclass
class ShapeLimits:
    min_ocpus: Optional[float] = None
    max_ocpus: Optional[float] = None
    ocpu_step: float = 1.0
    min_memory_gb: Optional[float] = None
    max_memory_gb: Optional[float] = None
    memory_step_gb: float = 1.0
    min_memory_per_ocpu_gb: Optional[float] = None
    max_memory_per_ocpu_gb: Optional[float] = None
    default_memory_per_ocpu_gb: Optional[float] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "min_ocpus": self.min_ocpus,
            "max_ocpus": self.max_ocpus,
            "ocpu_step": self.ocpu_step,
            "min_memory_gb": self.min_memory_gb,
            "max_memory_gb": self.max_memory_gb,
            "memory_step_gb": self.memory_step_gb,
            "min_memory_per_ocpu_gb": self.min_memory_per_ocpu_gb,
            "max_memory_per_ocpu_gb": self.max_memory_per_ocpu_gb,
            "default_memory_per_ocpu_gb": self.default_memory_per_ocpu_gb,
        }

    def clamp(self, ocpus: float, memory_gb: float) -> Dict[str, float]:
        ocpus = ceil_units_bounded(
            float(ocpus),
            step=self.ocpu_step,
            minimum=self.min_ocpus,
            maximum=self.max_ocpus,
            base=self.min_ocpus,
        )

        effective_min_memory = self.min_memory_gb
        effective_max_memory = self.max_memory_gb
        if self.min_memory_per_ocpu_gb is not None:
            per_ocpu_min = ocpus * float(self.min_memory_per_ocpu_gb)
            effective_min_memory = per_ocpu_min if effective_min_memory is None else max(float(effective_min_memory), per_ocpu_min)
        if self.max_memory_per_ocpu_gb is not None:
            per_ocpu_max = ocpus * float(self.max_memory_per_ocpu_gb)
            effective_max_memory = per_ocpu_max if effective_max_memory is None else min(float(effective_max_memory), per_ocpu_max)

        if (
            effective_min_memory is not None
            and effective_max_memory is not None
            and float(effective_min_memory) > float(effective_max_memory)
        ):
            effective_min_memory = effective_max_memory

        memory_gb = ceil_units_bounded(
            float(memory_gb),
            step=self.memory_step_gb,
            minimum=effective_min_memory,
            maximum=effective_max_memory,
            base=self.min_memory_gb if self.min_memory_gb is not None else effective_min_memory,
        )

        return {"ocpus": float(ocpus), "memory_gb": float(memory_gb)}


class OCIContext:
    def __init__(self, profile: str, config_file: str, region: Optional[str] = None, auth: str = "auto"):
        self.config = load_config(profile=profile, config_file=config_file, region=region)
        self.config["log_requests"] = False
        self.tenancy_id = self.config["tenancy"]
        signer = build_signer(self.config, auth)
        self._client_options = {"signer": signer} if signer is not None else {}
        self.identity = oci.identity.IdentityClient(self.config, **self._client_options)
        self.compute = oci.core.ComputeClient(self.config, **self._client_options)
        self.monitoring = oci.monitoring.MonitoringClient(self.config, **self._client_options)
        self.usage = oci.usage_api.UsageapiClient(self.config, **self._client_options)
        self.genai: Optional[Any] = None
        self._regional_lock = Lock()
        self._regional_clients = {self.region: {
            "config": self.config, "compute": self.compute,
            "monitoring": self.monitoring, "usage": self.usage,
        }}

    @property
    def region(self) -> str:
        return self.config.get("region", "")

    def clients_for_region(self, region: str) -> Dict[str, Any]:
        active_region = region or self.region
        with self._regional_lock:
            if active_region not in self._regional_clients:
                regional_config = dict(self.config, region=active_region)
                self._regional_clients[active_region] = {
                    "config": regional_config,
                    "compute": oci.core.ComputeClient(regional_config, **self._client_options),
                    "monitoring": oci.monitoring.MonitoringClient(regional_config, **self._client_options),
                    "usage": oci.usage_api.UsageapiClient(regional_config, **self._client_options),
                }
            return self._regional_clients[active_region]

    def get_genai_client(self) -> Any:
        if self.genai is None:
            self.genai = oci.generative_ai_inference.GenerativeAiInferenceClient(self.config, **self._client_options)
        return self.genai


class InventoryAgent:
    def __init__(self, ctx: OCIContext):
        self.ctx = ctx
        self._shape_limits_cache: Dict[Tuple[str, str, str], ShapeLimits] = {}
        self._shape_limits_lock = Lock()

    def get_instance(self, instance_id: str) -> Any:
        return self.ctx.compute.get_instance(instance_id).data

    def list_subscribed_regions(self) -> List[str]:
        response = self.ctx.identity.list_region_subscriptions(self.ctx.tenancy_id).data
        if not response:
            return [self.ctx.region] if self.ctx.region else []
        return [
            item.region_name
            for item in response
            if getattr(item, "status", None) == "READY" and getattr(item, "region_name", None)
        ]

    def _resolve_compartment_name(self, compartment_id: str) -> str:
        try:
            if compartment_id == self.ctx.tenancy_id:
                return self.ctx.identity.get_tenancy(self.ctx.tenancy_id).data.name
            return self.ctx.identity.get_compartment(compartment_id).data.name
        except Exception:
            if compartment_id == self.ctx.tenancy_id:
                return "root"
            return compartment_id

    def list_accessible_compartments(self, root_compartment_id: Optional[str]) -> List[Dict[str, str]]:
        root_id = root_compartment_id or self.ctx.tenancy_id
        seen: Dict[str, Dict[str, str]] = {
            root_id: {"id": root_id, "name": self._resolve_compartment_name(root_id)}
        }

        if root_id == self.ctx.tenancy_id:
            compartments = oci.pagination.list_call_get_all_results(
                self.ctx.identity.list_compartments,
                compartment_id=root_id,
                compartment_id_in_subtree=True,
                access_level="ACCESSIBLE",
                lifecycle_state="ACTIVE",
            ).data
            for compartment in compartments:
                comp_id = getattr(compartment, "id", None)
                if not comp_id or comp_id in seen:
                    continue
                seen[comp_id] = {
                    "id": comp_id,
                    "name": getattr(compartment, "name", comp_id),
                }
            return list(seen.values())

        pending = [root_id]
        while pending:
            parent_id = pending.pop()
            compartments = oci.pagination.list_call_get_all_results(
                self.ctx.identity.list_compartments,
                compartment_id=parent_id,
                access_level="ACCESSIBLE",
                lifecycle_state="ACTIVE",
            ).data
            for compartment in compartments:
                comp_id = getattr(compartment, "id", None)
                if not comp_id or comp_id in seen:
                    continue
                seen[comp_id] = {
                    "id": comp_id,
                    "name": getattr(compartment, "name", comp_id),
                }
                pending.append(comp_id)
        return list(seen.values())

    def list_instances(self, compartment_id: str) -> List[Any]:
        return oci.pagination.list_call_get_all_results(
            self.ctx.compute.list_instances,
            compartment_id=compartment_id,
        ).data

    def list_instances_in_region(self, compute_client: Any, compartment_id: str) -> List[Any]:
        return oci.pagination.list_call_get_all_results(
            compute_client.list_instances,
            compartment_id=compartment_id,
        ).data

    def get_shape_details(self, compartment_id: str, shape_name: str, compute_client: Optional[Any] = None) -> Optional[Any]:
        active_compute = compute_client or self.ctx.compute
        shapes = oci.pagination.list_call_get_all_results(
            active_compute.list_shapes,
            compartment_id=compartment_id,
            shape=shape_name,
        ).data
        for shape in shapes:
            if getattr(shape, "shape", None) == shape_name:
                return shape
        return None

    def to_record(self, instance: Any) -> Dict[str, Any]:
        shape_config = getattr(instance, "shape_config", None)
        return {
            "instance_id": instance.id,
            "display_name": instance.display_name,
            "compartment_id": instance.compartment_id,
            "availability_domain": instance.availability_domain,
            "lifecycle_state": instance.lifecycle_state,
            "shape": instance.shape,
            "ocpus": getattr(shape_config, "ocpus", None) if shape_config else None,
            "memory_gb": getattr(shape_config, "memory_in_gbs", None) if shape_config else None,
            "freeform_tags": getattr(instance, "freeform_tags", {}) or {},
            "defined_tags": getattr(instance, "defined_tags", {}) or {},
        }

    def to_record_with_region(self, instance: Any, region: str, compartment_name: Optional[str] = None) -> Dict[str, Any]:
        record = self.to_record(instance)
        record["region"] = region
        record["compartment_name"] = compartment_name
        return record

    def discover(self, compartment_id: Optional[str], instance_id: Optional[str]) -> Dict[str, Any]:
        if instance_id:
            instances = [self.get_instance(instance_id)]
        else:
            target_compartment = compartment_id or self.ctx.tenancy_id
            instances = self.list_instances(target_compartment)
        return {
            "generated_at": iso_z(now_utc()),
            "region": self.ctx.region,
            "items": [self.to_record(instance) for instance in instances],
            "warnings": [
                "'discover' is deprecated. Use scan for a fleet view, recommend for an action plan, or apply/run with an explicit --instance-id or scope."
            ],
        }

    def get_shape_limits(self, inventory: Dict[str, Any], compute_client: Optional[Any] = None) -> ShapeLimits:
        region = str(inventory.get("region") or self.ctx.region or "")
        cache_key = (region, str(inventory["compartment_id"]), str(inventory["shape"]))
        with self._shape_limits_lock:
            cached = self._shape_limits_cache.get(cache_key)
        if cached is not None:
            return cached

        shape_details = self.get_shape_details(inventory["compartment_id"], inventory["shape"], compute_client=compute_client)
        if not shape_details:
            limits = ShapeLimits()
        else:
            ocpu_options = getattr(shape_details, "ocpu_options", None)
            memory_options = getattr(shape_details, "memory_options", None)
            limits = ShapeLimits(
                min_ocpus=safe_float(getattr(ocpu_options, "min", None)),
                max_ocpus=safe_float(getattr(ocpu_options, "max", None)),
                ocpu_step=safe_float(getattr(ocpu_options, "increment", None)) or 1.0,
                min_memory_gb=safe_float(getattr(memory_options, "min_in_g_bs", None)),
                max_memory_gb=safe_float(getattr(memory_options, "max_in_g_bs", None)),
                memory_step_gb=safe_float(getattr(memory_options, "increment_in_g_bs", None)) or 1.0,
                min_memory_per_ocpu_gb=safe_float(getattr(memory_options, "min_per_ocpu_in_g_bs", None)),
                max_memory_per_ocpu_gb=safe_float(getattr(memory_options, "max_per_ocpu_in_g_bs", None)),
                default_memory_per_ocpu_gb=safe_float(getattr(memory_options, "default_per_ocpu_in_g_bs", None)),
            )

        with self._shape_limits_lock:
            self._shape_limits_cache[cache_key] = limits
        return limits


def choose_instance(ctx: OCIContext, compartment_id: str) -> str:
    inventory = InventoryAgent(ctx)
    instances = inventory.list_instances(compartment_id)
    if not instances:
        raise SystemExit("No instances found in the selected compartment.")

    print("\nAvailable instances:", file=sys.stderr)
    for index, instance in enumerate(instances, start=1):
        shape_config = getattr(instance, "shape_config", None)
        ocpus = getattr(shape_config, "ocpus", None) if shape_config else None
        memory_gb = getattr(shape_config, "memory_in_gbs", None) if shape_config else None
        print(
            f"  [{index}] {instance.display_name} | {instance.lifecycle_state} | {instance.shape} | "
            f"OCPUs={ocpus} | MemoryGB={memory_gb} | AD={instance.availability_domain}",
            file=sys.stderr,
        )

    while True:
        selected = input(f"Select instance [1-{len(instances)}] or q to quit: ").strip()
        if selected.lower() in {"q", "quit", "exit"}:
            raise SystemExit("Cancelled.")
        if selected.isdigit():
            chosen_index = int(selected)
            if 1 <= chosen_index <= len(instances):
                chosen = instances[chosen_index - 1]
                print(f"Selected instance: {chosen.display_name} ({chosen.id})", file=sys.stderr)
                return chosen.id
        print("Invalid selection.", file=sys.stderr)


class MetricsAgent:
    def __init__(self, ctx: OCIContext):
        self.ctx = ctx
        self.inventory = InventoryAgent(ctx)

    def fetch_metric(
        self,
        compartment_id: str,
        instance_id: str,
        metric_name: str,
        start_time: datetime,
        end_time: datetime,
        resolution: str,
        monitoring_client: Optional[Any] = None,
    ) -> Dict[str, Any]:
        details = oci.monitoring.models.SummarizeMetricsDataDetails(
            namespace=METRIC_NAMESPACE,
            query=f'{metric_name}[{resolution}]{{resourceId = "{instance_id}"}}.mean()',
            start_time=start_time,
            end_time=end_time,
            resolution=resolution,
        )
        active_monitoring = monitoring_client or self.ctx.monitoring
        response = active_monitoring.summarize_metrics_data(
            compartment_id=compartment_id,
            summarize_metrics_data_details=details,
        ).data

        datapoints: List[Dict[str, Any]] = []
        numeric_values: List[float] = []
        first_series = response[0] if response else None
        if first_series:
            ordered = sorted(first_series.aggregated_datapoints or [], key=lambda item: item.timestamp)
            for item in ordered:
                value = safe_float(getattr(item, "value", None))
                datapoints.append({"timestamp": iso_z(item.timestamp), "value": value})
                if value is not None:
                    numeric_values.append(value)

        return {
            "metric": metric_name,
            "query": f'{metric_name}[{resolution}]{{resourceId = "{instance_id}"}}.mean()',
            "datapoints": datapoints,
            "summary": summarize_values(numeric_values),
        }

    def coverage_assessment(
        self,
        datapoints: List[Dict[str, Any]],
        hours: int,
        resolution: str,
        minimum_active_days: Optional[int] = None,
    ) -> Dict[str, Any]:
        observed_values = [dp for dp in datapoints if dp.get("value") is not None]
        observed_datetimes = [
            parsed
            for parsed in (parse_iso_z(dp.get("timestamp")) for dp in observed_values)
            if parsed is not None
        ]
        days = sorted({parsed.date().isoformat() for parsed in observed_datetimes})
        expected = expected_points(hours, resolution)
        ratio = (len(observed_values) / expected) if expected else 0.0

        runtime_expected = expected
        runtime_ratio = ratio
        observed_span_hours = 0.0
        active_observed_hours = 0.0
        coverage_basis = "full_window"
        runtime_classification = "no_telemetry" if not observed_values else "full_window"
        first_observed_timestamp = None
        last_observed_timestamp = None

        if observed_datetimes:
            resolution_minutes = parse_resolution_to_minutes(resolution)
            first_observed = min(observed_datetimes)
            last_observed = max(observed_datetimes)
            first_observed_timestamp = iso_z(first_observed)
            last_observed_timestamp = iso_z(last_observed)
            observed_span_minutes = max(
                float(resolution_minutes),
                ((last_observed - first_observed).total_seconds() / 60.0) + float(resolution_minutes),
            )
            observed_span_hours = round(observed_span_minutes / 60.0, 2)
            distinct_day_hours = float(len(days) * 24)
            active_observed_hours = min(float(hours), observed_span_hours, distinct_day_hours or float(hours))
            runtime_expected = expected_points_for_minutes(active_observed_hours * 60.0, resolution)
            runtime_ratio = (len(observed_values) / runtime_expected) if runtime_expected else 0.0
            if runtime_expected < expected:
                coverage_basis = "observed_metric_span"
            if minimum_active_days is not None and len(days) < max(1, int(minimum_active_days)):
                runtime_classification = "insufficient_runtime"
            elif runtime_ratio < 0.5:
                runtime_classification = "sparse_while_active"
            elif coverage_basis == "observed_metric_span":
                runtime_classification = "intermittent_or_recently_active"
            else:
                runtime_classification = "full_window"

        return {
            "expected_points": expected,
            "observed_points": len(observed_values),
            "coverage_ratio": round(ratio, 4),
            "distinct_days": len(days),
            "active_observed_days": len(days),
            "active_observed_hours": round(active_observed_hours, 2),
            "full_window_hours": hours,
            "observed_span_hours": observed_span_hours,
            "first_observed_timestamp": first_observed_timestamp,
            "last_observed_timestamp": last_observed_timestamp,
            "runtime_adjusted_expected_points": runtime_expected,
            "runtime_adjusted_coverage_ratio": round(runtime_ratio, 4),
            "coverage_basis": coverage_basis,
            "minimum_active_days_required": minimum_active_days,
            "runtime_classification": runtime_classification,
        }

    def run(
        self,
        instance_id: str,
        hours: int,
        resolution: str,
        include_datapoints: bool = False,
        minimum_active_days: Optional[int] = None,
        instance: Optional[Any] = None,
        monitoring_client: Optional[Any] = None,
        region: Optional[str] = None,
    ) -> Dict[str, Any]:
        active_instance = instance or self.inventory.get_instance(instance_id)
        end_time = now_utc()
        start_time = end_time - timedelta(hours=hours)
        cpu = self.fetch_metric(
            active_instance.compartment_id,
            active_instance.id,
            CPU_METRIC,
            start_time,
            end_time,
            resolution,
            monitoring_client=monitoring_client,
        )
        memory = self.fetch_metric(
            active_instance.compartment_id,
            active_instance.id,
            MEM_METRIC,
            start_time,
            end_time,
            resolution,
            monitoring_client=monitoring_client,
        )

        warnings: List[str] = []
        cpu_coverage = self.coverage_assessment(cpu["datapoints"], hours, resolution, minimum_active_days=minimum_active_days)
        memory_coverage = self.coverage_assessment(memory["datapoints"], hours, resolution, minimum_active_days=minimum_active_days)
        cpu_runtime_guard = coverage_scale_down_guard(cpu_coverage, minimum_active_days or 1, "CPU")
        memory_runtime_guard = coverage_scale_down_guard(memory_coverage, minimum_active_days or 1, "Memory")

        if minimum_active_days is not None and cpu_runtime_guard["status"] == "insufficient_runtime":
            warnings.append(cpu_runtime_guard["reason"])
        elif cpu_coverage["observed_points"] <= 0:
            warnings.append(f"No CPU telemetry was observed in the selected {hours}-hour window.")
        elif effective_coverage_ratio(cpu_coverage) < 0.5:
            warnings.append(
                f"CPU telemetry is sparse while the VM appears active in the {hours}-hour window "
                f"({cpu_coverage['observed_points']} of ~{effective_expected_points(cpu_coverage)} active-window points)."
            )
        if minimum_active_days is not None and memory_runtime_guard["status"] == "insufficient_runtime":
            warnings.append(memory_runtime_guard["reason"])
        elif memory_coverage["observed_points"] <= 0:
            warnings.append(f"No memory telemetry was observed in the selected {hours}-hour window.")
        elif effective_coverage_ratio(memory_coverage) < 0.5:
            warnings.append(
                f"Memory telemetry is sparse while the VM appears active in the {hours}-hour window "
                f"({memory_coverage['observed_points']} of ~{effective_expected_points(memory_coverage)} active-window points)."
            )

        if not include_datapoints:
            cpu["datapoints"] = []
            memory["datapoints"] = []

        return {
            "generated_at": iso_z(end_time),
            "region": region or self.ctx.region,
            "instance_id": active_instance.id,
            "display_name": active_instance.display_name,
            "compartment_id": active_instance.compartment_id,
            "window": {
                "start_time": iso_z(start_time),
                "end_time": iso_z(end_time),
                "hours": hours,
                "resolution": resolution,
            },
            "coverage": {
                "cpu": cpu_coverage,
                "memory": memory_coverage,
            },
            "warnings": warnings,
            "metrics": {
                CPU_METRIC: cpu,
                MEM_METRIC: memory,
            },
        }


class PricingCatalogError(ValueError):
    pass


def normalize_lookup_key(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def normalize_model_key(value: Any) -> str:
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


def tokenize_lookup_text(value: Any) -> List[str]:
    normalized: List[str] = []
    for ch in str(value or "").lower():
        normalized.append(ch if ch.isalnum() else " ")
    return [token for token in "".join(normalized).split() if token]


def load_pricing_catalog(path: str) -> Dict[str, Any]:
    resolved_path = os.path.abspath(os.path.expanduser(str(path or "").strip()))
    if not resolved_path:
        raise PricingCatalogError("Missing pricing catalog path.")
    if not os.path.exists(resolved_path):
        raise PricingCatalogError(f"Pricing catalog file not found: {resolved_path}")

    try:
        with open(resolved_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except json.JSONDecodeError as exc:
        raise PricingCatalogError(
            f"Pricing catalog '{resolved_path}' is not valid JSON: {exc.msg} "
            f"(line {exc.lineno}, column {exc.colno})."
        ) from exc
    except OSError as exc:
        raise PricingCatalogError(f"Could not read pricing catalog '{resolved_path}': {exc}") from exc

    return validate_pricing_catalog(payload, source=resolved_path)


def validate_pricing_catalog(data: Any, source: str = "pricing catalog") -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise PricingCatalogError(f"{source} must be a JSON object with a top-level 'items' list.")

    items = data.get("items")
    if items is None:
        raise PricingCatalogError(f"{source} is missing the required top-level 'items' list.")
    if not isinstance(items, list):
        raise PricingCatalogError(f"{source} field 'items' must be a list.")

    for item_index, item in enumerate(items):
        if not isinstance(item, dict):
            raise PricingCatalogError(f"{source} item at index {item_index} must be an object.")

        localizations = item.get("currencyCodeLocalizations")
        if localizations is not None:
            if not isinstance(localizations, list):
                raise PricingCatalogError(
                    f"{source} item {item_index} field 'currencyCodeLocalizations' must be a list when present."
                )
            for localization_index, localization in enumerate(localizations):
                if not isinstance(localization, dict):
                    raise PricingCatalogError(
                        f"{source} item {item_index} currencyCodeLocalizations[{localization_index}] must be an object."
                    )
                prices = localization.get("prices")
                if prices is not None and not isinstance(prices, list):
                    raise PricingCatalogError(
                        f"{source} item {item_index} currencyCodeLocalizations[{localization_index}].prices must be a list when present."
                    )
                if isinstance(prices, list):
                    for price_index, price in enumerate(prices):
                        if not isinstance(price, dict):
                            raise PricingCatalogError(
                                f"{source} item {item_index} currencyCodeLocalizations[{localization_index}].prices[{price_index}] must be an object."
                            )

        prices = item.get("prices")
        if prices is not None:
            if not isinstance(prices, list):
                raise PricingCatalogError(f"{source} item {item_index} field 'prices' must be a list when present.")
            for price_index, price in enumerate(prices):
                if not isinstance(price, dict):
                    raise PricingCatalogError(f"{source} item {item_index} prices[{price_index}] must be an object.")
                nested_prices = price.get("prices")
                if nested_prices is not None and not isinstance(nested_prices, list):
                    raise PricingCatalogError(
                        f"{source} item {item_index} prices[{price_index}].prices must be a list when present."
                    )
                if isinstance(nested_prices, list):
                    for nested_index, nested_price in enumerate(nested_prices):
                        if not isinstance(nested_price, dict):
                            raise PricingCatalogError(
                                f"{source} item {item_index} prices[{price_index}].prices[{nested_index}] must be an object."
                            )

    return data


def extract_price_value(price: Dict[str, Any]) -> Optional[float]:
    for key in ("value", "unitPrice", "unit_price", "price", "listPrice", "list_price"):
        if price.get(key) is not None:
            maybe_number = safe_float(price.get(key))
            if maybe_number is not None:
                return maybe_number
    return None


def iter_price_records(data: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    validated = validate_pricing_catalog(data)
    items = validated.get("items", [])
    for item_index, item in enumerate(items):
        base_record = {
            "item_index": item_index,
            "part_number": item.get("partNumber") or item.get("part_number"),
            "display_name": item.get("displayName") or item.get("display_name") or item.get("name"),
            "description": item.get("description"),
            "metric_name": item.get("metricName") or item.get("metric_name") or item.get("metricDisplayName"),
            "service_category": item.get("serviceCategory") or item.get("service_category") or item.get("serviceCategoryDisplayName"),
            "raw_item": item,
        }

        emitted = False
        groups = item.get("currencyCodeLocalizations")
        if not isinstance(groups, list):
            groups = item.get("prices") if isinstance(item.get("prices"), list) else []

        for group_index, group in enumerate(groups):
            if not isinstance(group, dict):
                continue

            group_currency = group.get("currencyCode") or item.get("currencyCode")
            nested_prices = group.get("prices")
            if isinstance(nested_prices, list):
                if not nested_prices:
                    emitted = True
                    yield {
                        **base_record,
                        "group_index": group_index,
                        "price_index": None,
                        "currency_code": group_currency,
                        "pricing_model": None,
                        "unit_price": None,
                        "range_min": None,
                        "range_max": None,
                        "raw_group": group,
                        "raw_price": None,
                    }
                    continue

                for price_index, price in enumerate(nested_prices):
                    if not isinstance(price, dict):
                        continue
                    emitted = True
                    yield {
                        **base_record,
                        "group_index": group_index,
                        "price_index": price_index,
                        "currency_code": group_currency,
                        "pricing_model": (
                            price.get("model")
                            or price.get("pricingModel")
                            or price.get("pricing_model")
                            or price.get("purchaseModel")
                        ),
                        "unit_price": extract_price_value(price),
                        "range_min": safe_float(price.get("rangeMin") if price.get("rangeMin") is not None else price.get("range_min")),
                        "range_max": safe_float(price.get("rangeMax") if price.get("rangeMax") is not None else price.get("range_max")),
                        "raw_group": group,
                        "raw_price": price,
                    }
                continue

            emitted = True
            yield {
                **base_record,
                "group_index": group_index,
                "price_index": None,
                "currency_code": group_currency,
                "pricing_model": (
                    group.get("model")
                    or group.get("pricingModel")
                    or group.get("pricing_model")
                    or group.get("purchaseModel")
                ),
                "unit_price": extract_price_value(group),
                "range_min": safe_float(group.get("rangeMin") if group.get("rangeMin") is not None else group.get("range_min")),
                "range_max": safe_float(group.get("rangeMax") if group.get("rangeMax") is not None else group.get("range_max")),
                "raw_group": group,
                "raw_price": group,
            }

        if not emitted:
            yield {
                **base_record,
                "group_index": None,
                "price_index": None,
                "currency_code": item.get("currencyCode"),
                "pricing_model": None,
                "unit_price": None,
                "range_min": None,
                "range_max": None,
                "raw_group": None,
                "raw_price": None,
            }


def build_pricing_index(data: Dict[str, Any]) -> Dict[str, Any]:
    validated = validate_pricing_catalog(data)
    records = list(iter_price_records(validated))
    items: List[Dict[str, Any]] = []
    items_by_index: Dict[int, Dict[str, Any]] = {}

    for record in records:
        item_index = int(record["item_index"])
        entry = items_by_index.get(item_index)
        if entry is None:
            entry = {
                "item_index": item_index,
                "part_number": record.get("part_number"),
                "display_name": record.get("display_name"),
                "description": record.get("description"),
                "metric_name": record.get("metric_name"),
                "service_category": record.get("service_category"),
                "raw_item": record.get("raw_item"),
                "price_records": [],
            }
            items_by_index[item_index] = entry
            items.append(entry)
        entry["price_records"].append(record)

    def add_entry(index: Dict[str, List[Any]], value: Any, entry: Any) -> None:
        key = normalize_lookup_key(value)
        if key:
            index.setdefault(key, []).append(entry)

    by_part_number: Dict[str, List[Dict[str, Any]]] = {}
    by_display_name: Dict[str, List[Dict[str, Any]]] = {}
    by_service_category: Dict[str, List[Dict[str, Any]]] = {}
    by_metric_name: Dict[str, List[Dict[str, Any]]] = {}
    by_currency_code: Dict[str, List[Dict[str, Any]]] = {}
    by_pricing_model: Dict[str, List[Dict[str, Any]]] = {}

    for item in items:
        add_entry(by_part_number, item.get("part_number"), item)
        add_entry(by_display_name, item.get("display_name"), item)
        add_entry(by_service_category, item.get("service_category"), item)
        add_entry(by_metric_name, item.get("metric_name"), item)

    for record in records:
        add_entry(by_currency_code, record.get("currency_code"), record)
        add_entry(by_pricing_model, record.get("pricing_model"), record)

    return {
        "data": validated,
        "items": items,
        "records": records,
        "by_part_number": by_part_number,
        "by_display_name": by_display_name,
        "by_service_category": by_service_category,
        "by_metric_name": by_metric_name,
        "by_currency_code": by_currency_code,
        "by_pricing_model": by_pricing_model,
    }


def select_price(
    item: Any,
    currency_code: str,
    model: Optional[str] = None,
    quantity: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    if isinstance(item, list):
        records = [entry for entry in item if isinstance(entry, dict)]
    elif isinstance(item, dict) and isinstance(item.get("price_records"), list):
        records = [entry for entry in item["price_records"] if isinstance(entry, dict)]
    elif isinstance(item, dict) and ("raw_price" in item or "raw_item" in item):
        records = [item]
    elif isinstance(item, dict):
        records = list(iter_price_records({"items": [item]}))
    else:
        records = []

    currency_key = normalize_lookup_key(currency_code)
    if not currency_key:
        return None

    candidates = [
        record
        for record in records
        if normalize_lookup_key(record.get("currency_code")) == currency_key
    ]
    candidates = [record for record in candidates if safe_float(record.get("unit_price")) is not None]
    if not candidates:
        return None

    if model is not None:
        model_key = normalize_model_key(model)
        candidates = [
            record
            for record in candidates
            if normalize_model_key(record.get("pricing_model")) == model_key
        ]
        if not candidates:
            return None

    def is_quantity_match(record: Dict[str, Any], qty: float) -> bool:
        range_min = safe_float(record.get("range_min"))
        range_max = safe_float(record.get("range_max"))
        if range_min is not None and qty < range_min:
            return False
        if range_max is not None and qty > range_max:
            return False
        return True

    def is_tiered(record: Dict[str, Any]) -> bool:
        return record.get("range_min") is not None or record.get("range_max") is not None

    if quantity is not None:
        quantity_value = float(quantity)
        quantity_matches = [record for record in candidates if is_quantity_match(record, quantity_value)]
        if quantity_matches:
            candidates = quantity_matches
        else:
            candidates = [record for record in candidates if not is_tiered(record)]
            if not candidates:
                return None
    else:
        non_tiered = [record for record in candidates if not is_tiered(record)]
        if non_tiered:
            candidates = non_tiered

    def candidate_sort_key(record: Dict[str, Any]) -> Tuple[int, float, float, int, int]:
        range_min = safe_float(record.get("range_min"))
        range_max = safe_float(record.get("range_max"))
        width = (
            (range_max - range_min)
            if range_min is not None and range_max is not None
            else float("inf")
        )
        return (
            0 if not is_tiered(record) else 1,
            range_min if range_min is not None else -1.0,
            width,
            int(record.get("item_index") or 0),
            int(record.get("price_index") or 0),
        )

    return sorted(candidates, key=candidate_sort_key)[0] if candidates else None


class UsageCostEstimator:
    def __init__(self, ctx: OCIContext):
        self.ctx = ctx
        self.list_price = ListPriceEstimator()

    def _build_filter(
        self,
        instance_id: Optional[str],
        use_default_filters: bool,
        custom_filters: Sequence[Tuple[str, str]],
    ) -> Optional[Any]:
        models = oci.usage_api.models
        dimensions: List[Any] = []
        if use_default_filters:
            dimensions.append(models.Dimension(key="service", value="Compute"))
            if instance_id:
                dimensions.append(models.Dimension(key="resourceId", value=instance_id))
        for key, value in custom_filters:
            dimensions.append(models.Dimension(key=key, value=value))
        if not dimensions:
            return None
        return models.Filter(operator="AND", dimensions=dimensions)

    def _request_metadata(
        self,
        request: Any,
        use_default_filters: bool,
        custom_filters: Sequence[Tuple[str, str]],
    ) -> Dict[str, Any]:
        return {
            "time_usage_started": iso_z(request.time_usage_started),
            "time_usage_ended": iso_z(request.time_usage_ended),
            "granularity": request.granularity,
            "query_type": request.query_type,
            "group_by": list(request.group_by or []),
            "use_default_filters": bool(use_default_filters),
            "custom_filters": list(custom_filters),
            "filter": oci.util.to_dict(request.filter) if getattr(request, "filter", None) is not None else None,
        }

    def _build_request(
        self,
        compartment_id: str,
        region: str,
        lookback_days: int,
        group_by: Optional[Sequence[str]] = None,
    ) -> Any:
        models = oci.usage_api.models
        end_time = utc_midnight(now_utc()) + timedelta(days=1)
        start_time = end_time - timedelta(days=int(lookback_days))
        return models.RequestSummarizedUsagesDetails(
            tenant_id=self.ctx.tenancy_id,
            time_usage_started=start_time,
            time_usage_ended=end_time,
            granularity="DAILY",
            query_type="COST",
            group_by=list(group_by or ["service", "skuPartNumber", "unit"]),
            compartment_depth=2,
            is_aggregate_by_time=False,
            filter=models.Filter(
                operator="AND",
                dimensions=[
                    models.Dimension(key="service", value="COMPUTE"),
                    models.Dimension(key="compartmentId", value=compartment_id),
                    models.Dimension(key="region", value=region),
                ],
            ),
        )

    def debug_request(
        self,
        instance_id: Optional[str],
        lookback_days: int,
        group_by: Sequence[str],
        query_type: str,
        granularity: str,
        use_default_filters: bool,
        custom_filters: List[Tuple[str, str]],
        usage_client: Optional[Any] = None,
    ) -> Dict[str, Any]:
        models = oci.usage_api.models
        end_time = utc_midnight(now_utc()) + timedelta(days=1)
        start_time = end_time - timedelta(days=int(lookback_days))
        filter_obj = self._build_filter(
            instance_id=instance_id,
            use_default_filters=use_default_filters,
            custom_filters=custom_filters,
        )
        request = models.RequestSummarizedUsagesDetails(
            tenant_id=self.ctx.tenancy_id,
            time_usage_started=start_time,
            time_usage_ended=end_time,
            granularity=granularity,
            query_type=query_type,
            group_by=list(group_by),
            compartment_depth=2,
            is_aggregate_by_time=False,
            filter=filter_obj,
        )
        request_payload = self._request_metadata(
            request=request,
            use_default_filters=use_default_filters,
            custom_filters=custom_filters,
        )
        active_usage = usage_client or self.ctx.usage
        try:
            response = active_usage.request_summarized_usages(request).data
            result = oci.util.to_dict(response)
            items = result.get("items", []) if isinstance(result, dict) else []
            return {
                "generated_at": iso_z(now_utc()),
                "region": self.ctx.region,
                "instance_id": instance_id,
                "lookback_days": lookback_days,
                "request": request_payload,
                "item_count": len(items or []),
                "items": list(items or []),
            }
        except Exception as exc:  # pragma: no cover - depends on OCI service behavior
            return {
                "generated_at": iso_z(now_utc()),
                "region": self.ctx.region,
                "instance_id": instance_id,
                "lookback_days": lookback_days,
                "request": request_payload,
                "item_count": 0,
                "items": [],
                "error": str(exc),
            }

    def _request_items(
        self,
        compartment_id: str,
        region: str,
        lookback_days: int,
        usage_client: Optional[Any] = None,
    ) -> Tuple[List[Dict[str, Any]], int, int, List[str]]:
        warnings: List[str] = []
        last_error: Optional[Exception] = None
        active_usage = usage_client or self.ctx.usage
        windows = []
        for candidate in [lookback_days, min(14, lookback_days), min(7, lookback_days)]:
            if candidate > 0 and candidate not in windows:
                windows.append(candidate)

        for window_days in windows:
            for attempt in range(1, 4):
                try:
                    request = self._build_request(
                        compartment_id=compartment_id,
                        region=region,
                        lookback_days=window_days,
                    )
                    response = active_usage.request_summarized_usages(request).data
                    result = oci.util.to_dict(response)
                    items = result.get("items", []) if isinstance(result, dict) else []
                    if window_days != lookback_days:
                        warnings.append(
                            f"Pricing lookup fell back from {lookback_days} days to {window_days} days after prior failures."
                        )
                    if attempt > 1:
                        warnings.append(f"Pricing lookup succeeded after {attempt} attempts.")
                    return list(items or []), window_days, attempt, warnings
                except Exception as exc:  # pragma: no cover - depends on OCI service behavior
                    last_error = exc
                    if attempt < 3:
                        time.sleep(2 ** (attempt - 1))
            has_narrower_window = window_days != windows[-1]
            if has_narrower_window:
                LOG.debug(
                    "Pricing request failed for %s-day window after retries; trying a narrower fallback window.",
                    window_days,
                )
            else:
                LOG.debug(
                    "Pricing request failed for %s-day window after retries; no narrower fallback windows remain.",
                    window_days,
                )

        if last_error:
            raise last_error
        return [], lookback_days, 1, warnings

    def _extract_row_fields(self, item: Dict[str, Any]) -> Dict[str, Any]:
        part_number = (
            item.get("sku_part_number")
            or item.get("skuPartNumber")
            or item.get("part_number")
            or item.get("partNumber")
            or item.get("sku")
        )
        unit = item.get("unit") or item.get("usage_unit") or item.get("usageUnit")
        amount = (
            safe_float(item.get("computed_amount"))
            if item.get("computed_amount") is not None
            else None
        )
        if amount is None:
            amount = safe_float(item.get("attributed_cost"))
        if amount is None:
            amount = safe_float(item.get("computedAmount"))
        if amount is None:
            amount = safe_float(item.get("attributedCost"))
        quantity = (
            safe_float(item.get("computed_quantity"))
            if item.get("computed_quantity") is not None
            else None
        )
        if quantity is None:
            quantity = safe_float(item.get("attributed_usage"))
        if quantity is None:
            quantity = safe_float(item.get("computedQuantity"))
        if quantity is None:
            quantity = safe_float(item.get("attributedUsage"))
        return {
            "part_number": str(part_number) if part_number else None,
            "unit": str(unit) if unit is not None else None,
            "amount": amount,
            "quantity": quantity,
        }

    def _classify_unit(self, unit: Optional[str]) -> Optional[str]:
        normalized = "".join(ch for ch in str(unit or "").lower() if ch.isalnum())
        if normalized in {"ocpuhour", "ocpuhours"}:
            return "cpu"
        if normalized in {"gbhour", "gbhours"}:
            return "memory"
        return None

    def _choose_preferred_part_number(
        self,
        part_numbers: Sequence[str],
        weighted_quantities: Dict[str, float],
    ) -> Optional[str]:
        positive = {
            str(part_number): float(quantity)
            for part_number, quantity in weighted_quantities.items()
            if part_number and safe_float(quantity) and float(quantity) > 0.0
        }
        if not positive:
            return None
        max_quantity = max(positive.values())
        for part_number in part_numbers:
            if positive.get(part_number) == max_quantity:
                return part_number
        return next(iter(positive.keys()), None)

    def _derive_effective_rates_from_items(
        self,
        items: List[Dict[str, Any]],
        include_effective_rates: bool = True,
    ) -> Dict[str, Any]:
        warnings: List[str] = []
        aggregates: Dict[str, Dict[str, Any]] = {
            "cpu": {
                "amount": 0.0,
                "quantity": 0.0,
                "part_numbers": [],
                "part_number_set": set(),
                "positive_cost_quantities": {},
                "positive_quantities": {},
            },
            "memory": {
                "amount": 0.0,
                "quantity": 0.0,
                "part_numbers": [],
                "part_number_set": set(),
                "positive_cost_quantities": {},
                "positive_quantities": {},
            },
        }

        for item in items:
            fields = self._extract_row_fields(item)
            classification = self._classify_unit(fields.get("unit"))
            if classification is None:
                continue

            quantity = safe_float(fields.get("quantity"))
            if quantity is None or quantity <= 0.0:
                continue

            bucket = aggregates[classification]
            part_number = fields.get("part_number")
            if part_number:
                if part_number not in bucket["part_number_set"]:
                    bucket["part_number_set"].add(part_number)
                    bucket["part_numbers"].append(part_number)
                bucket["positive_quantities"][part_number] = bucket["positive_quantities"].get(part_number, 0.0) + quantity

            amount = safe_float(fields.get("amount"))
            if amount is not None and amount > 0.0 and part_number:
                bucket["positive_cost_quantities"][part_number] = (
                    bucket["positive_cost_quantities"].get(part_number, 0.0) + quantity
                )

            if not include_effective_rates:
                continue

            if amount is None or amount <= 0.0:
                continue

            bucket["amount"] += amount
            bucket["quantity"] += quantity

        cpu_rate = (
            aggregates["cpu"]["amount"] / aggregates["cpu"]["quantity"]
            if include_effective_rates and aggregates["cpu"]["amount"] > 0.0 and aggregates["cpu"]["quantity"] > 0.0
            else None
        )
        memory_rate = (
            aggregates["memory"]["amount"] / aggregates["memory"]["quantity"]
            if include_effective_rates and aggregates["memory"]["amount"] > 0.0 and aggregates["memory"]["quantity"] > 0.0
            else None
        )

        if include_effective_rates and cpu_rate is None:
            warnings.append("Could not derive a positive effective CPU rate from compartment+region Usage API rows.")
        if include_effective_rates and memory_rate is None:
            warnings.append("Could not derive a positive effective memory rate from compartment+region Usage API rows.")

        return {
            "cpu_rate": cpu_rate,
            "memory_rate": memory_rate,
            "cpu_part_numbers": list(aggregates["cpu"]["part_numbers"]),
            "memory_part_numbers": list(aggregates["memory"]["part_numbers"]),
            "preferred_cpu_part_number": self._choose_preferred_part_number(
                aggregates["cpu"]["part_numbers"],
                aggregates["cpu"]["positive_cost_quantities"] or aggregates["cpu"]["positive_quantities"],
            ),
            "preferred_memory_part_number": self._choose_preferred_part_number(
                aggregates["memory"]["part_numbers"],
                aggregates["memory"]["positive_cost_quantities"] or aggregates["memory"]["positive_quantities"],
            ),
            "warnings": warnings,
        }

    def _estimate_from_effective_rates(
        self,
        current_ocpus: float,
        current_memory_gb: float,
        recommended_ocpus: float,
        recommended_memory_gb: float,
        monthly_hours: float,
        effective_days: int,
        attempts: int,
        rate_info: Dict[str, Any],
        warnings: Sequence[str],
        currency_code: str,
    ) -> Dict[str, Any]:
        cpu_rate = safe_float(rate_info.get("cpu_rate"))
        memory_rate = safe_float(rate_info.get("memory_rate"))
        if cpu_rate is None or memory_rate is None:
            effective_warnings = list(warnings)
            if not rate_info.get("warnings"):
                effective_warnings.append(
                    "Could not derive both CPU and memory effective rates from compartment+region Usage API rows."
                )
            return {
                "pricing_enabled": False,
                "pricing_attempted": True,
                "pricing_source": "usage_api_compartment_region_effective_rate_best_effort",
                "pricing_confidence": "unavailable",
                "currency_code": str(currency_code).upper(),
                "lookback_days": effective_days,
                "attempts": attempts,
                "warnings": effective_warnings,
                "detected_part_numbers": {
                    "cpu": rate_info.get("preferred_cpu_part_number"),
                    "memory": rate_info.get("preferred_memory_part_number"),
                },
            }

        current_hourly = (float(current_ocpus) * cpu_rate) + (float(current_memory_gb) * memory_rate)
        estimated_new_hourly = (float(recommended_ocpus) * cpu_rate) + (float(recommended_memory_gb) * memory_rate)
        hourly_delta = estimated_new_hourly - current_hourly
        monthly_delta = hourly_delta * float(monthly_hours)

        return {
            "pricing_enabled": True,
            "pricing_attempted": True,
            "pricing_source": "usage_api_compartment_region_effective_rate_best_effort",
            "pricing_confidence": "effective",
            "currency_code": str(currency_code).upper(),
            "lookback_days": effective_days,
            "attempts": attempts,
            "cpu_rate": round(cpu_rate, 6),
            "memory_rate": round(memory_rate, 6),
            "current_hourly_cost": round(current_hourly, 6),
            "estimated_new_hourly_cost": round(estimated_new_hourly, 6),
            "estimated_hourly_delta": round(hourly_delta, 6),
            "estimated_monthly_delta": round(monthly_delta, 2),
            "estimated_hourly_savings": round(max(0.0, -hourly_delta), 6),
            "estimated_monthly_savings": round(max(0.0, -monthly_delta), 2),
            "estimated_hourly_increase": round(max(0.0, hourly_delta), 6),
            "estimated_monthly_increase": round(max(0.0, monthly_delta), 2),
            "warnings": list(warnings),
            "detected_part_numbers": {
                "cpu": rate_info.get("preferred_cpu_part_number"),
                "memory": rate_info.get("preferred_memory_part_number"),
                "cpu_candidates": list(rate_info.get("cpu_part_numbers") or []),
                "memory_candidates": list(rate_info.get("memory_part_numbers") or []),
            },
        }

    def _estimate_from_list_pricing(
        self,
        current_ocpus: float,
        current_memory_gb: float,
        recommended_ocpus: float,
        recommended_memory_gb: float,
        monthly_hours: float,
        shape: str,
        currency_code: str,
        warnings: Sequence[str],
        effective_days: int,
        attempts: int,
        pricing_catalog_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        combined_warnings = list(warnings)
        currency = str(currency_code or "USD").upper()
        current_resolved = self.list_price.resolve_compute_shape_rates(
            shape=shape,
            currency_code=currency,
            model="PAY_AS_YOU_GO",
            cpu_quantity=current_ocpus,
            memory_quantity=current_memory_gb,
            catalog_path=pricing_catalog_path,
        )
        if not current_resolved.get("found"):
            combined_warnings.append(
                str(current_resolved.get("warning") or f"List pricing could not be resolved for shape {shape}.")
            )
            return {
                "pricing_enabled": False,
                "pricing_attempted": True,
                "pricing_source": "oracle_list_pricing",
                "pricing_confidence": "unavailable",
                "currency_code": currency,
                "lookback_days": effective_days,
                "attempts": attempts,
                "warnings": combined_warnings,
                "resolved_shape": shape,
                "catalog_matches": {
                    "cpu": current_resolved.get("cpu", []),
                    "memory": current_resolved.get("memory", []),
                },
            }

        recommended_resolved = self.list_price.resolve_compute_shape_rates(
            shape=shape,
            currency_code=currency,
            model="PAY_AS_YOU_GO",
            cpu_quantity=recommended_ocpus,
            memory_quantity=recommended_memory_gb,
            catalog_path=pricing_catalog_path,
        )
        if not recommended_resolved.get("found"):
            combined_warnings.append(
                str(
                    recommended_resolved.get("warning")
                    or f"List pricing could not be resolved for the recommended {shape} quantities."
                )
            )
            return {
                "pricing_enabled": False,
                "pricing_attempted": True,
                "pricing_source": "oracle_list_pricing",
                "pricing_confidence": "unavailable",
                "currency_code": currency,
                "lookback_days": effective_days,
                "attempts": attempts,
                "warnings": combined_warnings,
                "resolved_shape": shape,
            }

        current_cpu_price = current_resolved["cpu"]
        current_memory_price = current_resolved["memory"]
        recommended_cpu_price = recommended_resolved["cpu"]
        recommended_memory_price = recommended_resolved["memory"]

        current_cpu_rate = safe_float(current_cpu_price.get("unit_price"))
        current_memory_rate = safe_float(current_memory_price.get("unit_price"))
        recommended_cpu_rate = safe_float(recommended_cpu_price.get("unit_price"))
        recommended_memory_rate = safe_float(recommended_memory_price.get("unit_price"))
        if (
            current_cpu_rate is None
            or current_memory_rate is None
            or recommended_cpu_rate is None
            or recommended_memory_rate is None
        ):
            combined_warnings.append(
                f"Pricing catalog returned matches for {shape}, but one or more unit prices could not be parsed."
            )
            return {
                "pricing_enabled": False,
                "pricing_attempted": True,
                "pricing_source": "oracle_list_pricing",
                "pricing_confidence": "unavailable",
                "currency_code": currency,
                "lookback_days": effective_days,
                "attempts": attempts,
                "warnings": combined_warnings,
                "resolved_shape": shape,
            }

        combined_warnings.append(
            "Oracle list pricing is a planning estimate and does not represent effective billed cost."
        )
        current_hourly = (float(current_ocpus) * current_cpu_rate) + (float(current_memory_gb) * current_memory_rate)
        estimated_new_hourly = (float(recommended_ocpus) * recommended_cpu_rate) + (
            float(recommended_memory_gb) * recommended_memory_rate
        )
        hourly_delta = estimated_new_hourly - current_hourly
        monthly_delta = hourly_delta * float(monthly_hours)

        return {
            "pricing_enabled": True,
            "pricing_attempted": True,
            "pricing_source": "oracle_list_pricing",
            "pricing_confidence": "planning",
            "currency_code": currency,
            "lookback_days": effective_days,
            "attempts": attempts,
            "cpu_rate": round(current_cpu_rate, 6),
            "memory_rate": round(current_memory_rate, 6),
            "recommended_cpu_rate": round(recommended_cpu_rate, 6),
            "recommended_memory_rate": round(recommended_memory_rate, 6),
            "current_hourly_cost": round(current_hourly, 6),
            "estimated_new_hourly_cost": round(estimated_new_hourly, 6),
            "estimated_hourly_delta": round(hourly_delta, 6),
            "estimated_monthly_delta": round(monthly_delta, 2),
            "estimated_hourly_savings": round(max(0.0, -hourly_delta), 6),
            "estimated_monthly_savings": round(max(0.0, -monthly_delta), 2),
            "estimated_hourly_increase": round(max(0.0, hourly_delta), 6),
            "estimated_monthly_increase": round(max(0.0, monthly_delta), 2),
            "warnings": combined_warnings,
            "resolved_shape": shape,
            "detected_part_numbers": {
                "cpu": current_cpu_price.get("part_number"),
                "memory": current_memory_price.get("part_number"),
            },
            "list_price_matches": {
                "cpu": {
                    "part_number": current_cpu_price.get("part_number"),
                    "display_name": current_cpu_price.get("display_name"),
                    "metric_name": current_cpu_price.get("metric_name"),
                    "current_unit_price": round(current_cpu_rate, 6),
                    "recommended_unit_price": round(recommended_cpu_rate, 6),
                },
                "memory": {
                    "part_number": current_memory_price.get("part_number"),
                    "display_name": current_memory_price.get("display_name"),
                    "metric_name": current_memory_price.get("metric_name"),
                    "current_unit_price": round(current_memory_rate, 6),
                    "recommended_unit_price": round(recommended_memory_rate, 6),
                },
            },
        }

    def estimate(
        self,
        current_ocpus: float,
        current_memory_gb: float,
        recommended_ocpus: float,
        recommended_memory_gb: float,
        shape: str,
        compartment_id: str,
        region: str,
        pricing_source: str,
        currency_code: str,
        lookback_days: int,
        monthly_hours: float,
        pricing_catalog_path: Optional[str] = None,
        usage_client: Optional[Any] = None,
    ) -> Dict[str, Any]:
        warnings = [
            "Pricing is best-effort and non-blocking."
        ]
        selected_source = str(pricing_source or "auto").lower()
        currency = str(currency_code or "USD").upper()
        if selected_source == "list":
            try:
                return self._estimate_from_list_pricing(
                    current_ocpus=current_ocpus,
                    current_memory_gb=current_memory_gb,
                    recommended_ocpus=recommended_ocpus,
                    recommended_memory_gb=recommended_memory_gb,
                    monthly_hours=monthly_hours,
                    shape=shape,
                    currency_code=currency,
                    warnings=warnings,
                    effective_days=max(1, int(lookback_days)),
                    attempts=1,
                    pricing_catalog_path=pricing_catalog_path,
                )
            except Exception as exc:  # pragma: no cover - depends on pricing source behavior
                warnings.append(f"List pricing lookup failed: {exc}")
                return {
                    "pricing_enabled": False,
                    "pricing_attempted": True,
                    "pricing_source": "oracle_list_pricing",
                    "pricing_confidence": "unavailable",
                    "currency_code": currency,
                    "warnings": warnings,
                }

        try:
            if not compartment_id:
                warnings.append("Pricing lookup requires an instance compartment OCID.")
                return {
                    "pricing_enabled": False,
                    "pricing_attempted": True,
                    "pricing_source": "disabled",
                    "pricing_confidence": "unavailable",
                    "currency_code": currency,
                    "warnings": warnings,
                }
            if not region:
                warnings.append("Pricing lookup requires a target region.")
                return {
                    "pricing_enabled": False,
                    "pricing_attempted": True,
                    "pricing_source": "disabled",
                    "pricing_confidence": "unavailable",
                    "currency_code": currency,
                    "warnings": warnings,
                }

            items, effective_days, attempts, request_warnings = self._request_items(
                compartment_id=compartment_id,
                region=region,
                lookback_days=lookback_days,
                usage_client=usage_client,
            )
            warnings.extend(request_warnings)
            if not items:
                warnings.append("Usage API returned no compartment+region Compute rows for the selected lookback window.")
                if selected_source == "auto":
                    warnings.append(
                        "Falling back to Oracle list pricing because effective compartment+region data was unavailable."
                    )
                    return self._estimate_from_list_pricing(
                        current_ocpus=current_ocpus,
                        current_memory_gb=current_memory_gb,
                        recommended_ocpus=recommended_ocpus,
                        recommended_memory_gb=recommended_memory_gb,
                        monthly_hours=monthly_hours,
                        shape=shape,
                        currency_code=currency,
                        warnings=warnings,
                        effective_days=effective_days,
                        attempts=attempts,
                        pricing_catalog_path=pricing_catalog_path,
                    )
                return {
                    "pricing_enabled": False,
                    "pricing_attempted": True,
                    "pricing_source": "usage_api_compartment_region_effective_rate_best_effort",
                    "pricing_confidence": "unavailable",
                    "currency_code": currency,
                    "lookback_days": effective_days,
                    "warnings": warnings,
                }

            rate_info = self._derive_effective_rates_from_items(items)
            warnings.extend(rate_info.get("warnings", []) or [])
            effective_result = self._estimate_from_effective_rates(
                current_ocpus=current_ocpus,
                current_memory_gb=current_memory_gb,
                recommended_ocpus=recommended_ocpus,
                recommended_memory_gb=recommended_memory_gb,
                monthly_hours=monthly_hours,
                effective_days=effective_days,
                attempts=attempts,
                rate_info=rate_info,
                warnings=warnings,
                currency_code=currency,
            )
            if selected_source == "effective" or effective_result.get("pricing_enabled"):
                return effective_result

            fallback_warnings = list(effective_result.get("warnings", []) or [])
            fallback_warnings.append(
                "Falling back to Oracle list pricing because effective compartment+region rates could not be derived."
            )
            return self._estimate_from_list_pricing(
                current_ocpus=current_ocpus,
                current_memory_gb=current_memory_gb,
                recommended_ocpus=recommended_ocpus,
                recommended_memory_gb=recommended_memory_gb,
                monthly_hours=monthly_hours,
                shape=shape,
                currency_code=currency,
                warnings=fallback_warnings,
                effective_days=effective_days,
                attempts=attempts,
                pricing_catalog_path=pricing_catalog_path,
            )
        except Exception as exc:  # pragma: no cover - depends on OCI service behavior
            warnings.append(f"Usage API pricing lookup failed: {exc}")
            if selected_source == "auto":
                warnings.append(
                    "Falling back to Oracle list pricing because the Usage API request did not complete successfully."
                )
                try:
                    return self._estimate_from_list_pricing(
                        current_ocpus=current_ocpus,
                        current_memory_gb=current_memory_gb,
                        recommended_ocpus=recommended_ocpus,
                        recommended_memory_gb=recommended_memory_gb,
                        monthly_hours=monthly_hours,
                        shape=shape,
                        currency_code=currency,
                        warnings=warnings,
                        effective_days=max(1, int(lookback_days)),
                        attempts=1,
                        pricing_catalog_path=pricing_catalog_path,
                    )
                except Exception as fallback_exc:  # pragma: no cover - depends on pricing source behavior
                    warnings.append(f"List pricing lookup failed: {fallback_exc}")
                    return {
                        "pricing_enabled": False,
                        "pricing_attempted": True,
                        "pricing_source": "oracle_list_pricing",
                        "pricing_confidence": "unavailable",
                        "currency_code": currency,
                        "warnings": warnings,
                    }
            return {
                "pricing_enabled": False,
                "pricing_attempted": True,
                "pricing_source": "usage_api_compartment_region_effective_rate_best_effort",
                "pricing_confidence": "unavailable",
                "currency_code": currency,
                "warnings": warnings,
            }


class ListPriceEstimator:
    ENDPOINT = "https://apexapps.oracle.com/pls/apex/cetools/api/v1/products/"
    SHAPE_GENERIC_TOKENS = {"vm", "bm", "shape", "instance"}
    CPU_TOKENS = {"ocpu", "vcpu", "cpu", "core", "cores"}
    MEMORY_TOKENS = {"memory", "ram", "gb", "gib", "gigabyte", "gigabytes"}
    HOURLY_TOKENS = {"hour", "hours", "hr", "hrs", "hourly"}

    def __init__(self):
        self._cache: Dict[Tuple[str, str, str, str], Dict[str, Any]] = {}
        self._catalog_cache: Dict[str, Dict[str, Any]] = {}
        self._catalog_lock = Lock()

    def _normalize_currency(self, value: Any) -> str:
        return str(value or "").strip().upper()

    def _catalog_source_key(self, currency_code: str, catalog_path: Optional[str]) -> str:
        if catalog_path:
            return f"file:{os.path.abspath(os.path.expanduser(str(catalog_path)))}"
        return f"endpoint:{self._normalize_currency(currency_code or 'USD')}"

    def _coerce_remote_catalog_payload(self, payload: Any) -> Dict[str, Any]:
        if isinstance(payload, dict) and isinstance(payload.get("items"), list):
            return payload
        if isinstance(payload, list):
            return {"items": payload}
        if isinstance(payload, dict):
            for key in ("products", "data", "result"):
                candidate = payload.get(key)
                if isinstance(candidate, list):
                    return {"items": candidate}
        raise PricingCatalogError(
            "Oracle list pricing response is incompatible with the expected catalog schema."
        )

    def _load_remote_catalog(self, currency_code: str) -> Dict[str, Any]:
        currency = self._normalize_currency(currency_code or "USD")
        params = urllib.parse.urlencode({"currencyCode": currency})
        url = f"{self.ENDPOINT}?{params}"
        request = urllib.request.Request(url, headers={"Accept": "application/json"})

        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise PricingCatalogError(
                f"Oracle list pricing response for currency {currency} is not valid JSON: "
                f"{exc.msg} (line {exc.lineno}, column {exc.colno})."
            ) from exc
        except Exception as exc:  # pragma: no cover - depends on external service behavior
            raise PricingCatalogError(f"Oracle list pricing lookup failed: {exc}") from exc

        return validate_pricing_catalog(
            self._coerce_remote_catalog_payload(payload),
            source=f"Oracle list pricing response ({currency})",
        )

    def _get_pricing_index(self, currency_code: str, catalog_path: Optional[str] = None) -> Dict[str, Any]:
        source_key = self._catalog_source_key(currency_code, catalog_path)
        with self._catalog_lock:
            cached = self._catalog_cache.get(source_key)
        if cached is not None:
            return cached

        if catalog_path:
            catalog = load_pricing_catalog(catalog_path)
        else:
            catalog = self._load_remote_catalog(currency_code)

        index = build_pricing_index(catalog)
        with self._catalog_lock:
            self._catalog_cache[source_key] = index
        return index

    def _item_search_text(self, item: Dict[str, Any]) -> str:
        parts = [
            item.get("part_number"),
            item.get("display_name"),
            item.get("description"),
            item.get("metric_name"),
            item.get("service_category"),
        ]
        return " ".join(str(part) for part in parts if part)

    # The catalog does not explicitly label which row is "CPU rate" versus
    # "memory rate" for VM rightsizing math, so we keep this small text-based
    # classifier isolated here.
    def _resource_kind(self, item: Dict[str, Any]) -> Optional[str]:
        tokens = set(tokenize_lookup_text(self._item_search_text(item)))
        has_hourly_signal = any(token in tokens for token in self.HOURLY_TOKENS)
        cpu_hits = sum(1 for token in self.CPU_TOKENS if token in tokens)
        memory_hits = sum(1 for token in self.MEMORY_TOKENS if token in tokens)
        if has_hourly_signal and cpu_hits > 0 and cpu_hits > memory_hits:
            return "cpu"
        if has_hourly_signal and memory_hits > 0 and memory_hits > cpu_hits:
            return "memory"
        return None

    def _shape_match_score(self, shape: str, item: Dict[str, Any]) -> int:
        shape_tokens = tokenize_lookup_text(shape)
        if not shape_tokens:
            return 0

        item_tokens = set(tokenize_lookup_text(self._item_search_text(item)))
        specific_tokens = [
            token
            for token in shape_tokens
            if token not in self.SHAPE_GENERIC_TOKENS and (any(ch.isdigit() for ch in token) or len(token) > 2)
        ]
        if not specific_tokens:
            specific_tokens = [token for token in shape_tokens if token not in self.SHAPE_GENERIC_TOKENS]

        matched_specific = sum(1 for token in specific_tokens if token in item_tokens)
        if specific_tokens and matched_specific == 0:
            return 0

        matched_all = sum(1 for token in shape_tokens if token in item_tokens)
        return (matched_specific * 10) + matched_all

    def _format_price_result(
        self,
        item: Optional[Dict[str, Any]],
        price_record: Optional[Dict[str, Any]],
        *,
        found: bool,
        currency_code: str,
        part_number: Optional[str] = None,
        warning: Optional[str] = None,
        error: Optional[str] = None,
    ) -> Dict[str, Any]:
        return {
            "found": found,
            "part_number": item.get("part_number") if item else part_number,
            "currency_code": self._normalize_currency(currency_code or ""),
            "display_name": item.get("display_name") if item else None,
            "metric_name": item.get("metric_name") if item else None,
            "service_category": item.get("service_category") if item else None,
            "unit_price": safe_float(price_record.get("unit_price")) if price_record else None,
            "pricing_model": price_record.get("pricing_model") if price_record else None,
            "range_min": price_record.get("range_min") if price_record else None,
            "range_max": price_record.get("range_max") if price_record else None,
            "raw": {
                "item": item.get("raw_item") if item else None,
                "price": price_record.get("raw_price") if price_record else None,
            },
            **({"warning": warning} if warning else {}),
            **({"error": error} if error else {}),
        }

    def get_payg_price(
        self,
        part_number: str,
        currency_code: str = "USD",
        catalog_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        normalized_part = str(part_number or "").strip()
        normalized_currency = self._normalize_currency(currency_code or "USD")
        source_key = self._catalog_source_key(normalized_currency, catalog_path)
        cache_key = ("part", source_key, normalized_part, normalized_currency)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return dict(cached)

        if not normalized_part:
            return self._format_price_result(
                None,
                None,
                found=False,
                currency_code=normalized_currency,
                part_number=normalized_part,
                warning="Missing part number for list pricing lookup.",
            )

        try:
            index = self._get_pricing_index(normalized_currency, catalog_path=catalog_path)
        except PricingCatalogError as exc:
            return self._format_price_result(
                None,
                None,
                found=False,
                currency_code=normalized_currency,
                part_number=normalized_part,
                error=str(exc),
            )

        items = index["by_part_number"].get(normalize_lookup_key(normalized_part), [])
        for item in items:
            if str(item.get("part_number") or "").strip().lower() != normalized_part.lower():
                continue
            selected_price = select_price(item, normalized_currency, model="PAY_AS_YOU_GO")
            if selected_price is None:
                result = self._format_price_result(
                    item,
                    None,
                    found=False,
                    currency_code=normalized_currency,
                    part_number=normalized_part,
                    warning=(
                        f"No PAY_AS_YOU_GO price was found for part number {normalized_part} "
                        f"in currency {normalized_currency}."
                    ),
                )
                self._cache[cache_key] = dict(result)
                return result
            result = self._format_price_result(
                item,
                selected_price,
                found=True,
                currency_code=normalized_currency,
                part_number=normalized_part,
            )
            self._cache[cache_key] = dict(result)
            return result

        result = self._format_price_result(
            None,
            None,
            found=False,
            currency_code=normalized_currency,
            part_number=normalized_part,
            warning=f"Pricing catalog did not return a matching item for part number {normalized_part}.",
        )
        self._cache[cache_key] = dict(result)
        return result

    def resolve_compute_shape_rates(
        self,
        shape: str,
        currency_code: str = "USD",
        model: Optional[str] = None,
        cpu_quantity: Optional[float] = None,
        memory_quantity: Optional[float] = None,
        catalog_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        normalized_currency = self._normalize_currency(currency_code or "USD")
        if not str(shape or "").strip():
            return {
                "found": False,
                "shape": shape,
                "currency_code": normalized_currency,
                "cpu": [],
                "memory": [],
                "warning": "Shape is required for catalog-based list pricing lookup.",
            }

        index = self._get_pricing_index(normalized_currency, catalog_path=catalog_path)
        cpu_candidates: List[Dict[str, Any]] = []
        memory_candidates: List[Dict[str, Any]] = []

        for item in index["items"]:
            match_score = self._shape_match_score(shape, item)
            if match_score <= 0:
                continue

            resource_kind = self._resource_kind(item)
            if resource_kind == "cpu":
                selected_price = select_price(item, normalized_currency, model=model, quantity=cpu_quantity)
            elif resource_kind == "memory":
                selected_price = select_price(item, normalized_currency, model=model, quantity=memory_quantity)
            else:
                continue

            if selected_price is None:
                continue

            candidate = {
                "part_number": item.get("part_number"),
                "display_name": item.get("display_name"),
                "metric_name": item.get("metric_name"),
                "service_category": item.get("service_category"),
                "currency_code": normalized_currency,
                "pricing_model": selected_price.get("pricing_model"),
                "unit_price": safe_float(selected_price.get("unit_price")),
                "range_min": selected_price.get("range_min"),
                "range_max": selected_price.get("range_max"),
                "match_score": match_score,
                "raw": {
                    "item": item.get("raw_item"),
                    "price": selected_price.get("raw_price"),
                },
            }
            if resource_kind == "cpu":
                cpu_candidates.append(candidate)
            elif resource_kind == "memory":
                memory_candidates.append(candidate)

        def candidate_sort_key(candidate: Dict[str, Any]) -> Tuple[int, int, int]:
            return (
                -int(candidate.get("match_score") or 0),
                int(candidate.get("range_min") is not None),
                0 if candidate.get("unit_price") is not None else 1,
            )

        cpu_candidates = sorted(cpu_candidates, key=candidate_sort_key)
        memory_candidates = sorted(memory_candidates, key=candidate_sort_key)
        if not cpu_candidates or not memory_candidates:
            return {
                "found": False,
                "shape": shape,
                "currency_code": normalized_currency,
                "cpu": cpu_candidates[:5],
                "memory": memory_candidates[:5],
                "warning": (
                    f"Could not infer both CPU and memory hourly prices for shape {shape} "
                    f"from the pricing catalog in currency {normalized_currency}."
                ),
            }

        return {
            "found": True,
            "shape": shape,
            "currency_code": normalized_currency,
            "cpu": cpu_candidates[0],
            "memory": memory_candidates[0],
            "cpu_candidates": cpu_candidates[:5],
            "memory_candidates": memory_candidates[:5],
        }


def validate_genai_model_id(model_id: Optional[str]) -> Optional[str]:
    if not model_id:
        return "Missing --genai-model-id."
    if not str(model_id).startswith("ocid1.generativeaimodel."):
        return "GenAI model OCID must start with ocid1.generativeaimodel."
    return None


class GenAIRecommender:
    def __init__(self, ctx: OCIContext):
        self.ctx = ctx

    def _validate_chat_models(self) -> Any:
        models = oci.generative_ai_inference.models
        missing_classes = [
            name
            for name in (
                "ChatDetails",
                "OnDemandServingMode",
                "GenericChatRequest",
                "UserMessage",
                "TextContent",
            )
            if not hasattr(models, name)
        ]
        if missing_classes:
            raise RuntimeError(
                "Current OCI Python SDK lacks Generative AI chat model classes: "
                + ", ".join(missing_classes)
            )
        return models

    def _extract_text(self, response_data: Any) -> str:
        try:
            data = oci.util.to_dict(response_data)
        except Exception:
            data = {}

        collected: List[str] = []

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                for key, nested in value.items():
                    if key in {"text", "output_text"} and isinstance(nested, str):
                        collected.append(nested)
                    else:
                        walk(nested)
            elif isinstance(value, list):
                for nested in value:
                    walk(nested)

        walk(data)
        return "\n".join(chunk for chunk in collected if chunk).strip()

    def _chat_json(
        self,
        prompt: str,
        compartment_id: str,
        model_id: str,
        temperature: float,
        max_tokens: int,
    ) -> Dict[str, Any]:
        models = self._validate_chat_models()
        details = models.ChatDetails(
            compartment_id=compartment_id,
            serving_mode=models.OnDemandServingMode(serving_type="ON_DEMAND", model_id=model_id),
            chat_request=models.GenericChatRequest(
                messages=[models.UserMessage(content=[models.TextContent(text=prompt)])],
                temperature=temperature,
                max_tokens=max_tokens,
                top_p=0.75,
            ),
        )

        warnings: List[str] = []
        last_error: Optional[Exception] = None
        raw_text = ""
        for attempt in range(1, 4):
            try:
                response = self.ctx.get_genai_client().chat(chat_details=details)
                raw_text = self._extract_text(response.data)
                break
            except Exception as exc:  # pragma: no cover - depends on OCI service behavior
                last_error = exc
                if attempt < 3:
                    time.sleep(2 ** (attempt - 1))
        else:
            raise last_error if last_error else RuntimeError("Unknown Generative AI failure.")

        parsed = None
        if raw_text:
            try:
                parsed = json.loads(raw_text)
            except Exception as exc:
                warnings.append(f"Generative AI response was not valid JSON: {exc}")

        return {
            "raw_text": raw_text,
            "parsed": parsed,
            "warnings": warnings,
        }

    def explain(
        self,
        policy_output: Dict[str, Any],
        compartment_id: str,
        model_id: str,
        temperature: float,
        max_tokens: int,
    ) -> Dict[str, Any]:
        reduced_input = {
            "instance": {
                "instance_id": policy_output.get("instance", {}).get("instance_id"),
                "shape": policy_output.get("instance", {}).get("shape"),
                "ocpus": policy_output.get("instance", {}).get("ocpus"),
                "memory_gb": policy_output.get("instance", {}).get("memory_gb"),
            },
            "metrics_summary": policy_output.get("metrics_summary"),
            "recommendation": policy_output.get("recommendation"),
        }
        prompt = (
            "You are explaining an OCI compute rightsizing recommendation.\n"
            "The deterministic policy output is the source of truth.\n"
            "Return only compact JSON with keys rationale_summary and warnings.\n"
            "Do not change recommended_ocpus, recommended_memory_gb, or any decision field.\n"
            "If you see risk or sparse data, say so briefly.\n"
            "Input:\n"
            + json.dumps(reduced_input, separators=(",", ":"))
        )
        return self._chat_json(
            prompt=prompt,
            compartment_id=compartment_id,
            model_id=model_id,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    def summarize_finops_payload(
        self,
        *,
        summary_type: str,
        summary_input: Dict[str, Any],
        compartment_id: str,
        model_id: str,
        temperature: float,
        max_tokens: int,
    ) -> Dict[str, Any]:
        prompt = (
            "You are writing a concise business-language summary for an OCI VM rightsizing workflow.\n"
            "The deterministic policy, pricing, action tiers, and verification results are the source of truth.\n"
            "Return only compact JSON with keys summary and watchouts.\n"
            "Do not change any decision, action tier, recommendation, or savings number.\n"
            "Keep summary to 1-2 sentences and watchouts to at most 3 short strings.\n"
            f"Summary type: {summary_type}\n"
            "Input:\n"
            + json.dumps(summary_input, separators=(",", ":"))
        )
        return self._chat_json(
            prompt=prompt,
            compartment_id=compartment_id,
            model_id=model_id,
            temperature=temperature,
            max_tokens=max_tokens,
        )


class RecommendationAgent:
    def __init__(self, ctx: OCIContext):
        self.ctx = ctx
        self.inventory = InventoryAgent(ctx)
        self.metrics = MetricsAgent(ctx)
        self.pricing = UsageCostEstimator(ctx)
        self.genai = GenAIRecommender(ctx)

    def count_high_periods(
        self,
        datapoints: List[Dict[str, Any]],
        threshold: float,
        sustained_minutes: int,
        resolution_minutes: int,
    ) -> int:
        needed = max(1, math.ceil(sustained_minutes / max(1, resolution_minutes)))
        run_length = 0
        periods = 0
        counted_current_run = False
        for point in datapoints:
            value = safe_float(point.get("value"))
            if value is not None and value > float(threshold):
                run_length += 1
                if run_length >= needed and not counted_current_run:
                    periods += 1
                    counted_current_run = True
            else:
                run_length = 0
                counted_current_run = False
        return periods

    def daily_p95s(self, datapoints: List[Dict[str, Any]]) -> Dict[str, float]:
        grouped: Dict[str, List[float]] = {}
        for point in datapoints:
            timestamp = point.get("timestamp")
            value = safe_float(point.get("value"))
            if not timestamp or value is None:
                continue
            grouped.setdefault(str(timestamp)[:10], []).append(value)

        result: Dict[str, float] = {}
        for day, values in grouped.items():
            ordered = sorted(values)
            maybe_p95 = percentile(ordered, 0.95)
            if maybe_p95 is not None:
                result[day] = maybe_p95
        return result

    def cpu_recommendation(
        self,
        current_ocpus: float,
        short_cpu: Dict[str, Any],
        long_cpu: Dict[str, Any],
        long_coverage: Dict[str, Any],
        args: argparse.Namespace,
    ) -> Dict[str, Any]:
        resolution_minutes = parse_resolution_to_minutes(args.cpu_scale_up_resolution)
        short_p95 = short_cpu["summary"].get("p95")
        long_p95 = long_cpu["summary"].get("p95")
        high_periods = self.count_high_periods(
            short_cpu.get("datapoints", []),
            threshold=args.cpu_scale_up_threshold,
            sustained_minutes=args.cpu_scale_up_sustained_minutes,
            resolution_minutes=resolution_minutes,
        )
        daily_p95 = self.daily_p95s(long_cpu.get("datapoints", []))
        low_days = sum(1 for value in daily_p95.values() if value < float(args.cpu_scale_down_threshold))

        decision = "NO_CHANGE"
        recommended_ocpus = float(current_ocpus)
        reason = "CPU thresholds not met."
        runtime_guard = coverage_scale_down_guard(long_coverage, int(args.cpu_scale_down_required_days), "CPU")

        if short_p95 is not None and high_periods >= int(args.cpu_scale_up_required_periods):
            decision = "SCALE_UP"
            recommended_ocpus = max(
                float(current_ocpus) + 1.0,
                ceil_units(float(current_ocpus) * float(args.upsize_factor), 1.0),
            )
            reason = (
                f"Short-window CPU p95 is {short_p95:.1f}% with "
                f"{high_periods} sustained high-utilization periods."
            )
        elif runtime_guard["status"] != "ok":
            reason = runtime_guard["reason"]
        elif long_p95 is not None and low_days >= int(args.cpu_scale_down_required_days):
            decision = "SCALE_DOWN"
            target_utilization = max(1.0, float(args.cpu_scale_down_target))
            recommended_ocpus = float(current_ocpus) * (float(long_p95) / target_utilization)
            recommended_ocpus = min(float(current_ocpus), ceil_units(recommended_ocpus, 1.0, 1.0))
            recommended_ocpus = max(1.0, recommended_ocpus)
            reason = (
                f"Daily CPU p95 stayed below {args.cpu_scale_down_threshold:.1f}% for "
                f"{low_days} days in the long window."
            )

        return {
            "decision": decision,
            "reason": reason,
            "recommended": float(recommended_ocpus),
            "high_periods": high_periods,
            "low_days": low_days,
            "short_p95": short_p95,
            "long_p95": long_p95,
            "long_daily_p95": daily_p95,
            "scale_down_guard": runtime_guard,
        }

    def memory_recommendation(
        self,
        current_memory_gb: float,
        current_ocpus: float,
        cpu_axis: Dict[str, Any],
        shape: str,
        short_memory: Dict[str, Any],
        long_memory: Dict[str, Any],
        long_coverage: Dict[str, Any],
        args: argparse.Namespace,
    ) -> Dict[str, Any]:
        resolution_minutes = parse_resolution_to_minutes(args.cpu_scale_up_resolution)
        short_p95 = short_memory["summary"].get("p95")
        long_p95 = long_memory["summary"].get("p95")
        high_periods = self.count_high_periods(
            short_memory.get("datapoints", []),
            threshold=args.memory_scale_up_threshold,
            sustained_minutes=args.memory_scale_up_sustained_minutes,
            resolution_minutes=resolution_minutes,
        )
        daily_p95 = self.daily_p95s(long_memory.get("datapoints", []))
        low_days = sum(1 for value in daily_p95.values() if value < float(args.memory_scale_down_threshold))

        min_memory_floor = max(1.0, float(current_ocpus) * float(args.min_memory_per_ocpu_gb))
        decision = "NO_CHANGE"
        recommended_memory_gb = float(current_memory_gb)
        reason = "Memory thresholds not met."
        runtime_guard = coverage_scale_down_guard(long_coverage, int(args.memory_scale_down_required_days), "Memory")

        if short_p95 is not None and high_periods >= int(args.memory_scale_up_required_periods):
            decision = "SCALE_UP"
            recommended_memory_gb = max(
                float(current_memory_gb) + float(args.min_memory_per_ocpu_gb),
                float(current_memory_gb) * float(args.upsize_factor),
            )
            recommended_memory_gb = ceil_units(recommended_memory_gb, 1.0)
            reason = (
                f"Short-window memory p95 is {short_p95:.1f}% with "
                f"{high_periods} sustained high-utilization periods."
            )
        elif runtime_guard["status"] == "ok" and long_p95 is not None and low_days >= int(args.memory_scale_down_required_days):
            if float(long_p95) < float(args.scale_down_memory_p95_max_percent):
                decision = "SCALE_DOWN"
                target_utilization = max(1.0, float(args.memory_scale_down_target))
                recommended_memory_gb = float(current_memory_gb) * (float(long_p95) / target_utilization)
                recommended_memory_gb = min(
                    float(current_memory_gb),
                    ceil_units(recommended_memory_gb, 1.0, min_memory_floor),
                )
                reason = (
                    f"Daily memory p95 stayed below {args.memory_scale_down_threshold:.1f}% for "
                    f"{low_days} days in the long window."
                )
            else:
                reason = (
                    f"Memory scale-down was suppressed because long-window memory p95 "
                    f"({long_p95:.1f}%) exceeds the safe limit of "
                    f"{args.scale_down_memory_p95_max_percent:.1f}%."
                )
        elif runtime_guard["status"] != "ok":
            reason = runtime_guard["reason"]

        return {
            "decision": decision,
            "reason": reason,
            "recommended": float(recommended_memory_gb),
            "high_periods": high_periods,
            "low_days": low_days,
            "short_p95": short_p95,
            "long_p95": long_p95,
            "long_daily_p95": daily_p95,
            "scale_down_guard": runtime_guard,
        }

    def summarize_rationale(self, cpu_axis: Dict[str, Any], memory_axis: Dict[str, Any]) -> str:
        cpu_text = f"CPU: {cpu_axis.get('decision')} ({cpu_axis.get('reason')})"
        memory_text = f"Memory: {memory_axis.get('decision')} ({memory_axis.get('reason')})"
        return f"{cpu_text} {memory_text}"

    def determine_safety_class(
        self,
        inventory: Dict[str, Any],
        recommendation: Dict[str, Any],
        short_metrics: Dict[str, Any],
        long_metrics: Dict[str, Any],
        genai_used: bool,
        pricing_attempted: bool,
    ) -> str:
        del short_metrics, long_metrics, genai_used, pricing_attempted
        action_tier = recommendation.get("action_tier") or ACTION_OBSERVE
        return ACTION_TIER_TO_SAFETY_CLASS.get(str(action_tier), "REVIEW_RECOMMENDED")

    def policy_recommendation(
        self,
        inventory: Dict[str, Any],
        short_metrics: Dict[str, Any],
        long_metrics: Dict[str, Any],
        args: argparse.Namespace,
        compute_client: Optional[Any] = None,
    ) -> Dict[str, Any]:
        current_ocpus = safe_float(inventory.get("ocpus")) or 1.0
        current_memory_gb = safe_float(inventory.get("memory_gb")) or 1.0
        shape_limits = self.inventory.get_shape_limits(inventory, compute_client=compute_client)

        short_cpu = short_metrics["metrics"][CPU_METRIC]
        short_memory = short_metrics["metrics"][MEM_METRIC]
        long_cpu = long_metrics["metrics"][CPU_METRIC]
        long_memory = long_metrics["metrics"][MEM_METRIC]
        long_cpu_coverage = long_metrics["coverage"]["cpu"]
        long_memory_coverage = long_metrics["coverage"]["memory"]

        warnings: List[str] = []
        warnings.extend(short_metrics.get("warnings", []) or [])
        warnings.extend(long_metrics.get("warnings", []) or [])

        cpu_runtime_guard = coverage_scale_down_guard(long_cpu_coverage, int(args.cpu_scale_down_required_days), "CPU")
        memory_runtime_guard = coverage_scale_down_guard(long_memory_coverage, int(args.memory_scale_down_required_days), "Memory")
        if cpu_runtime_guard["status"] == "insufficient_runtime" or memory_runtime_guard["status"] == "insufficient_runtime":
            warnings.append(
                "Insufficient active runtime was observed in the selected long window. "
                "Scale-down stays in observe until the workload has enough active days."
            )
        elif cpu_runtime_guard["status"] in {"no_telemetry", "sparse_while_active"} or memory_runtime_guard["status"] in {"no_telemetry", "sparse_while_active"}:
            warnings.append(
                "Long-window Monitoring telemetry is missing or sparse. "
                "Downsizing an axis with insufficient telemetry is blocked."
            )

        cpu_axis = self.cpu_recommendation(
            current_ocpus=current_ocpus,
            short_cpu=short_cpu,
            long_cpu=long_cpu,
            long_coverage=long_cpu_coverage,
            args=args,
        )
        memory_axis = self.memory_recommendation(
            current_memory_gb=current_memory_gb,
            current_ocpus=current_ocpus,
            cpu_axis=cpu_axis,
            shape=inventory["shape"],
            short_memory=short_memory,
            long_memory=long_memory,
            long_coverage=long_memory_coverage,
            args=args,
        )

        candidate_memory = max(
            float(memory_axis["recommended"]),
            float(cpu_axis["recommended"]) * float(args.min_memory_per_ocpu_gb),
        )
        candidate = shape_limits.clamp(float(cpu_axis["recommended"]), candidate_memory)

        validation_errors: List[str] = []
        resize_supported = supports_resize(inventory.get("shape"))
        if not resize_supported:
            validation_errors.append(
                "Instance is not a supported Standard or Optimized Flex VM. Automatic resize is blocked."
            )
        # Shape constraints must never introduce a reduction that telemetry blocked.
        if candidate["memory_gb"] < current_memory_gb and memory_runtime_guard["status"] != "ok":
            validation_errors.append("Memory reduction is blocked by insufficient telemetry, including reductions imposed by shape constraints.")
        if candidate["ocpus"] < current_ocpus and cpu_runtime_guard["status"] != "ok":
            validation_errors.append("CPU reduction is blocked by insufficient telemetry.")

        overall_decision = "NO_CHANGE"
        if (
            cpu_axis["decision"] != "NO_CHANGE"
            or memory_axis["decision"] != "NO_CHANGE"
        ):
            if (
                math.isclose(candidate["ocpus"], current_ocpus, rel_tol=0.0, abs_tol=1e-9)
                and math.isclose(candidate["memory_gb"], current_memory_gb, rel_tol=0.0, abs_tol=1e-9)
            ):
                warnings.append(
                    "A resize condition was detected, but shape validation/clamping kept the final recommendation at the current configuration."
                )
            else:
                overall_decision = "RESIZE"

        if overall_decision == "NO_CHANGE":
            candidate = {"ocpus": float(current_ocpus), "memory_gb": float(current_memory_gb)}

        return {
            "shape": inventory["shape"],
            "overall_decision": overall_decision,
            "cpu_decision": cpu_axis["decision"],
            "memory_decision": memory_axis["decision"],
            "recommended_ocpus": candidate["ocpus"],
            "recommended_memory_gb": candidate["memory_gb"],
            "shape_limits": shape_limits.as_dict(),
            "validation_errors": validation_errors,
            "warnings": warnings,
            "safety_class": "REVIEW_RECOMMENDED",
            "rationale_summary": self.summarize_rationale(cpu_axis, memory_axis),
            "rationale": {
                "source": "policy_engine",
                "cpu": cpu_axis,
                "memory": memory_axis,
                "coverage": {
                    "short_cpu": short_metrics["coverage"]["cpu"],
                    "short_memory": short_metrics["coverage"]["memory"],
                    "long_cpu": long_cpu_coverage,
                    "long_memory": long_memory_coverage,
                },
                "policy": {
                    "cpu_scale_up_threshold": args.cpu_scale_up_threshold,
                    "cpu_scale_up_sustained_minutes": args.cpu_scale_up_sustained_minutes,
                    "cpu_scale_up_required_periods": args.cpu_scale_up_required_periods,
                    "cpu_scale_down_threshold": args.cpu_scale_down_threshold,
                    "cpu_scale_down_required_days": args.cpu_scale_down_required_days,
                    "cpu_scale_down_target": args.cpu_scale_down_target,
                    "memory_scale_up_threshold": args.memory_scale_up_threshold,
                    "memory_scale_up_sustained_minutes": args.memory_scale_up_sustained_minutes,
                    "memory_scale_up_required_periods": args.memory_scale_up_required_periods,
                    "memory_scale_down_threshold": args.memory_scale_down_threshold,
                    "memory_scale_down_required_days": args.memory_scale_down_required_days,
                    "memory_scale_down_target": args.memory_scale_down_target,
                    "scale_down_memory_p95_max_percent": args.scale_down_memory_p95_max_percent,
                    "upsize_factor": args.upsize_factor,
                    "min_memory_per_ocpu_gb": args.min_memory_per_ocpu_gb,
                },
            },
        }

    def apply_genai_explanation(
        self,
        policy_output: Dict[str, Any],
        inventory: Dict[str, Any],
        args: argparse.Namespace,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        genai_payload: Dict[str, Any] = {"enabled": True, "warnings": []}
        recommendation = dict(policy_output["recommendation"])
        model_error = validate_genai_model_id(args.genai_model_id)
        if model_error:
            recommendation["warnings"] = list(recommendation.get("warnings", []) or []) + [model_error]
            genai_payload["warnings"].append(model_error)
            return recommendation, genai_payload

        try:
            result = self.genai.explain(
                policy_output=policy_output,
                compartment_id=args.genai_compartment_id or inventory["compartment_id"],
                model_id=args.genai_model_id,
                temperature=args.genai_temperature,
                max_tokens=args.genai_max_tokens,
            )
            genai_payload.update(result)
            parsed = result.get("parsed")
            if isinstance(parsed, dict):
                rationale_summary = parsed.get("rationale_summary")
                if isinstance(rationale_summary, str) and rationale_summary.strip():
                    recommendation["rationale_summary"] = rationale_summary.strip()
                parsed_warnings = parsed.get("warnings")
                if isinstance(parsed_warnings, list):
                    recommendation["warnings"] = list(recommendation.get("warnings", []) or []) + [
                        str(item) for item in parsed_warnings
                    ]
            elif result.get("warnings"):
                recommendation["warnings"] = list(recommendation.get("warnings", []) or []) + list(result["warnings"])
        except Exception as exc:  # pragma: no cover - depends on OCI service behavior
            message = f"OCI Generative AI explanation failed: {exc}"
            recommendation["warnings"] = list(recommendation.get("warnings", []) or []) + [message]
            genai_payload["warnings"].append(message)

        return recommendation, genai_payload

    def run(
        self,
        inventory: Dict[str, Any],
        args: argparse.Namespace,
        compute_client: Optional[Any] = None,
        monitoring_client: Optional[Any] = None,
        usage_client: Optional[Any] = None,
        enable_pricing: bool = True,
        instance_obj: Optional[Any] = None,
    ) -> Dict[str, Any]:
        short_metrics = self.metrics.run(
            instance_id=inventory["instance_id"],
            hours=args.cpu_scale_up_hours,
            resolution=args.cpu_scale_up_resolution,
            include_datapoints=True,
            instance=instance_obj,
            monitoring_client=monitoring_client,
            region=inventory.get("region"),
        )
        long_metrics = self.metrics.run(
            instance_id=inventory["instance_id"],
            hours=args.cpu_scale_down_days_window * 24,
            resolution=args.cpu_scale_down_resolution,
            include_datapoints=True,
            minimum_active_days=max(
                int(getattr(args, "cpu_scale_down_required_days", 1) or 1),
                int(getattr(args, "memory_scale_down_required_days", 1) or 1),
            ),
            instance=instance_obj,
            monitoring_client=monitoring_client,
            region=inventory.get("region"),
        )

        recommendation = self.policy_recommendation(
            inventory=inventory,
            short_metrics=short_metrics,
            long_metrics=long_metrics,
            args=args,
            compute_client=compute_client,
        )
        payload = {
            "generated_at": iso_z(now_utc()),
            "instance": inventory,
            "metrics_summary": {
                "short_window": {
                    "window": short_metrics["window"],
                    "coverage": short_metrics["coverage"],
                    "cpu": short_metrics["metrics"][CPU_METRIC]["summary"],
                    "memory": short_metrics["metrics"][MEM_METRIC]["summary"],
                },
                "long_window": {
                    "window": long_metrics["window"],
                    "coverage": long_metrics["coverage"],
                    "cpu": long_metrics["metrics"][CPU_METRIC]["summary"],
                    "memory": long_metrics["metrics"][MEM_METRIC]["summary"],
                },
            },
            "recommendation": recommendation,
        }

        if args.use_genai:
            updated_recommendation, genai_payload = self.apply_genai_explanation(payload, inventory, args)
            payload["recommendation"] = updated_recommendation
            payload["genai"] = genai_payload
        pricing_attempted = bool(enable_pricing)

        if enable_pricing:
            payload["pricing_estimate"] = self.pricing.estimate(
                current_ocpus=safe_float(inventory.get("ocpus")) or 1.0,
                current_memory_gb=safe_float(inventory.get("memory_gb")) or 1.0,
                recommended_ocpus=safe_float(payload["recommendation"].get("recommended_ocpus")) or 1.0,
                recommended_memory_gb=safe_float(payload["recommendation"].get("recommended_memory_gb")) or 1.0,
                shape=str(inventory.get("shape") or ""),
                compartment_id=inventory["compartment_id"],
                region=str(inventory.get("region") or self.ctx.region or ""),
                pricing_source=args.pricing_source,
                currency_code=args.pricing_currency,
                lookback_days=args.pricing_lookback_days,
                monthly_hours=args.monthly_hours,
                pricing_catalog_path=getattr(args, "pricing_catalog", None),
                usage_client=usage_client,
            )
        else:
            payload["pricing_estimate"] = {
                "pricing_enabled": False,
                "pricing_attempted": False,
                "pricing_source": "disabled",
                "pricing_confidence": None,
                "warnings": [],
            }
        payload["owner_or_cost_center"] = derive_owner_or_cost_center(inventory)
        payload["explanation_basis"] = build_explanation_basis(payload["metrics_summary"])
        payload["financial_summary"] = build_financial_summary(
            payload["pricing_estimate"],
            monthly_hours=float(getattr(args, "monthly_hours", 730.0) or 730.0),
        )
        action_details = determine_action_tier(
            inventory=inventory,
            recommendation=payload["recommendation"],
            metrics_summary=payload["metrics_summary"],
            pricing_estimate=payload.get("pricing_estimate"),
            cpu_scale_down_required_days=int(getattr(args, "cpu_scale_down_required_days", 7) or 7),
            memory_scale_down_required_days=int(getattr(args, "memory_scale_down_required_days", 7) or 7),
        )
        payload["action_summary"] = {
            "action_tier": action_details["action_tier"],
            "next_step": action_details["next_step"],
        }
        payload["recommendation"]["action_tier"] = action_details["action_tier"]
        payload["recommendation"]["action_reasons"] = list(action_details.get("action_reasons") or [])
        payload["recommendation"]["next_step"] = action_details["next_step"]
        payload["recommendation"]["governance_flags"] = list(action_details.get("governance_flags") or [])
        payload["recommendation"]["metrics_coverage_classification"] = action_details["metrics_coverage_classification"]
        payload["recommendation"]["safety_class"] = self.determine_safety_class(
            inventory=inventory,
            recommendation=payload["recommendation"],
            short_metrics=short_metrics,
            long_metrics=long_metrics,
            genai_used=bool(args.use_genai),
            pricing_attempted=pricing_attempted,
        )
        return payload


class ResizeAgent:
    def __init__(self, ctx: OCIContext):
        self.ctx = ctx
        self.inventory = InventoryAgent(ctx)

    def run(
        self, instance_id: str, ocpus: float, memory_gb: float, apply: bool,
        region: Optional[str] = None, acknowledge_reboot: bool = False,
        expected_ocpus: Optional[float] = None, expected_memory_gb: Optional[float] = None,
        expected_shape: Optional[str] = None,
    ) -> Dict[str, Any]:
        active_compute = self.ctx.clients_for_region(region or self.ctx.region)["compute"]
        response = active_compute.get_instance(instance_id)
        instance = response.data
        inventory = self.inventory.to_record(instance)
        inventory["region"] = region or self.ctx.region
        shape_limits = self.inventory.get_shape_limits(inventory, compute_client=active_compute)
        validation_errors: List[str] = []
        warnings: List[str] = []

        if not supports_resize(instance.shape):
            validation_errors.append("Automatic resize is supported only for Standard and Optimized Flex VMs.")
        if not all(math.isfinite(float(value)) and float(value) > 0 for value in (ocpus, memory_gb)):
            raise ValueError("OCPUs and memory must be finite, positive numbers.")
        if any(value is None for value in (
            shape_limits.min_ocpus, shape_limits.max_ocpus,
            shape_limits.min_memory_gb, shape_limits.max_memory_gb,
            shape_limits.min_memory_per_ocpu_gb, shape_limits.max_memory_per_ocpu_gb,
        )):
            validation_errors.append("Complete live shape limits could not be resolved; resize is blocked.")

        clamped = shape_limits.clamp(ocpus, memory_gb)
        requested_ocpus = clamped["ocpus"]
        requested_memory = clamped["memory_gb"]
        if not math.isclose(requested_ocpus, float(ocpus), rel_tol=0.0, abs_tol=1e-9) or not math.isclose(
            requested_memory, float(memory_gb), rel_tol=0.0, abs_tol=1e-9
        ):
            validation_errors.append("Requested configuration is outside live shape limits. Review the clamped configuration before retrying.")

        current_ocpus = safe_float(inventory.get("ocpus"))
        current_memory = safe_float(inventory.get("memory_gb"))
        for expected, current in ((expected_ocpus, current_ocpus), (expected_memory_gb, current_memory)):
            if expected is not None and (current is None or not math.isclose(float(expected), current, rel_tol=0.0, abs_tol=1e-9)):
                validation_errors.append("Instance configuration changed after assessment; reassess before applying.")
                break
        if expected_shape is not None and expected_shape != instance.shape:
            validation_errors.append("Instance shape changed after assessment; reassess before applying.")
        if apply and instance.lifecycle_state != "RUNNING":
            validation_errors.append("Execution is limited to RUNNING instances; stopped or transitioning instances require manual handling.")
        if current_ocpus is not None and current_memory is not None:
            if math.isclose(requested_ocpus, current_ocpus, rel_tol=0.0, abs_tol=1e-9) and math.isclose(
                requested_memory, current_memory, rel_tol=0.0, abs_tol=1e-9
            ):
                warnings.append("Requested shape config matches the current instance configuration.")

        if instance.lifecycle_state == "RUNNING":
            warnings.append("Resizing a RUNNING instance reboots it. Schedule a maintenance window and validate the application afterward.")
            if apply and not acknowledge_reboot:
                validation_errors.append("Use --acknowledge-reboot to confirm the planned interruption.")
        etag = response.headers.get("etag")
        if apply and not etag:
            validation_errors.append("Instance ETag is unavailable; conditional resize cannot be submitted.")

        payload = {
            "generated_at": iso_z(now_utc()),
            "apply": bool(apply),
            "api_call_submitted": False,
            "instance": inventory,
            "current_shape_config": {
                "ocpus": current_ocpus,
                "memory_gb": current_memory,
            },
            "requested_shape_config": {
                "ocpus": requested_ocpus,
                "memory_gb": requested_memory,
            },
            "shape_limits": shape_limits.as_dict(),
            "warnings": warnings,
            "validation_errors": validation_errors,
        }

        if validation_errors:
            payload["message"] = "Validation failed. Resize request was not submitted."
            return payload
        if not apply:
            payload["message"] = "Dry run only. Use --apply to submit the resize."
            return payload
        if current_ocpus is not None and current_memory is not None:
            if math.isclose(requested_ocpus, current_ocpus, rel_tol=0.0, abs_tol=1e-9) and math.isclose(
                requested_memory, current_memory, rel_tol=0.0, abs_tol=1e-9
            ):
                payload["message"] = "Requested shape config matches current configuration. No API call submitted."
                return payload

        response = active_compute.update_instance(
            instance_id,
            oci.core.models.UpdateInstanceDetails(
                shape_config=oci.core.models.UpdateInstanceShapeConfigDetails(
                    ocpus=requested_ocpus,
                    memory_in_gbs=requested_memory,
                )
            ),
            if_match=etag,
        )
        updated = response.data
        payload["message"] = "Resize request submitted."
        payload["api_call_submitted"] = True
        payload["opc_request_id"] = response.headers.get("opc-request-id")
        payload["etag"] = response.headers.get("etag")
        payload["updated_instance"] = self.inventory.to_record(updated)
        return payload


class VerifyAgent:
    def __init__(self, ctx: OCIContext):
        self.ctx = ctx
        self.inventory = InventoryAgent(ctx)

    def run(
        self,
        instance_id: str,
        expected_ocpus: Optional[float],
        expected_memory_gb: Optional[float],
        timeout_seconds: int,
        poll_seconds: int,
        region: Optional[str] = None,
    ) -> Dict[str, Any]:
        start_time = time.monotonic()
        checks: List[Dict[str, Any]] = []
        active_compute = self.ctx.clients_for_region(region or self.ctx.region)["compute"]

        while True:
            instance = active_compute.get_instance(instance_id).data
            inventory = self.inventory.to_record(instance)
            current_ocpus = safe_float(inventory.get("ocpus"))
            current_memory = safe_float(inventory.get("memory_gb"))
            lifecycle_state = inventory.get("lifecycle_state")
            check = {
                "timestamp": iso_z(now_utc()),
                "lifecycle_state": lifecycle_state,
                "ocpus": current_ocpus,
                "memory_gb": current_memory,
            }
            checks.append(check)

            cpu_ok = expected_ocpus is None or (
                current_ocpus is not None and math.isclose(current_ocpus, float(expected_ocpus), rel_tol=0.0, abs_tol=1e-9)
            )
            memory_ok = expected_memory_gb is None or (
                current_memory is not None and math.isclose(current_memory, float(expected_memory_gb), rel_tol=0.0, abs_tol=1e-9)
            )

            if lifecycle_state == "RUNNING" and cpu_ok and memory_ok:
                return {
                    "generated_at": iso_z(now_utc()),
                    "ok": True,
                    "instance_id": instance_id,
                    "final_state": lifecycle_state,
                    "final_shape_config": {
                        "ocpus": current_ocpus,
                        "memory_gb": current_memory,
                    },
                    "checks": checks,
                }

            if time.monotonic() - start_time >= timeout_seconds:
                return {
                    "generated_at": iso_z(now_utc()),
                    "ok": False,
                    "instance_id": instance_id,
                    "final_state": lifecycle_state,
                    "final_shape_config": {
                        "ocpus": current_ocpus,
                        "memory_gb": current_memory,
                    },
                    "error": "Timed out waiting for RUNNING with the expected shape configuration.",
                    "checks": checks,
                }

            time.sleep(max(1, poll_seconds))


class Planner:
    def __init__(self, ctx: OCIContext):
        self.ctx = ctx
        self.inventory = InventoryAgent(ctx)
        self.metrics = MetricsAgent(ctx)
        self.recommendation = RecommendationAgent(ctx)
        self.resize = ResizeAgent(ctx)
        self.verify = VerifyAgent(ctx)
        self.genai = GenAIRecommender(ctx)

    def _pricing_enabled(self, args: argparse.Namespace) -> bool:
        return not bool(getattr(args, "no_pricing", False))

    def _top_limit(self, args: argparse.Namespace) -> int:
        raw = int(safe_float(getattr(args, "top", 10)) or 10)
        return raw if raw > 0 else 10

    def _resolve_regions(self, args: argparse.Namespace) -> List[str]:
        selection = str(getattr(args, "regions", "current")).strip().lower()
        if selection == "current":
            regions = [self.ctx.region] if self.ctx.region else []
        elif selection == "all":
            regions = self.inventory.list_subscribed_regions()
            if not regions:
                regions = [self.ctx.region] if self.ctx.region else []
        else:
            regions = [part.strip() for part in str(args.regions).split(",") if part.strip()]
        if not regions and self.ctx.region:
            regions = [self.ctx.region]
        return regions

    def _maybe_business_summary(
        self,
        args: argparse.Namespace,
        *,
        summary_type: str,
        summary_input: Dict[str, Any],
        compartment_id: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        if not getattr(args, "use_genai", False):
            return None
        model_error = validate_genai_model_id(getattr(args, "genai_model_id", None))
        if model_error:
            return {"enabled": True, "warnings": [model_error]}
        try:
            return self.genai.summarize_finops_payload(
                summary_type=summary_type,
                summary_input=summary_input,
                compartment_id=args.genai_compartment_id or compartment_id or self.ctx.tenancy_id,
                model_id=args.genai_model_id,
                temperature=args.genai_temperature,
                max_tokens=args.genai_max_tokens,
            )
        except Exception as exc:  # pragma: no cover - depends on OCI service behavior
            return {
                "enabled": True,
                "warnings": [f"OCI Generative AI {summary_type} summary failed: {exc}"],
            }

    def _scan_row_from_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        instance = payload.get("instance", {})
        recommendation = payload.get("recommendation", {})
        metrics_summary = payload.get("metrics_summary", {})
        short_window = metrics_summary.get("short_window", {})
        long_window = metrics_summary.get("long_window", {})
        pricing = payload.get("pricing_estimate", {})
        rationale = recommendation.get("rationale", {}) or {}
        financial_summary = payload.get("financial_summary") or build_financial_summary(
            pricing,
            monthly_hours=float(payload.get("monthly_hours") or 730.0),
        )
        action_summary = payload.get("action_summary", {})

        warnings = list(recommendation.get("warnings", []) or [])
        warnings.extend(pricing.get("warnings", []) or [])
        if payload.get("genai", {}).get("warnings"):
            warnings.extend(payload["genai"]["warnings"])

        row = {
            "region": instance.get("region") or payload.get("region"),
            "compartment_id": instance.get("compartment_id"),
            "compartment_name": instance.get("compartment_name"),
            "availability_domain": instance.get("availability_domain"),
            "instance_id": instance.get("instance_id"),
            "display_name": instance.get("display_name"),
            "lifecycle_state": instance.get("lifecycle_state"),
            "shape": instance.get("shape"),
            "ocpus": instance.get("ocpus"),
            "memory_gb": instance.get("memory_gb"),
            "owner_or_cost_center": payload.get("owner_or_cost_center") or derive_owner_or_cost_center(instance),
            "overall_decision": recommendation.get("overall_decision"),
            "cpu_decision": recommendation.get("cpu_decision"),
            "memory_decision": recommendation.get("memory_decision"),
            "recommended_ocpus": recommendation.get("recommended_ocpus"),
            "recommended_memory_gb": recommendation.get("recommended_memory_gb"),
            "safety_class": recommendation.get("safety_class"),
            "action_tier": action_summary.get("action_tier") or recommendation.get("action_tier"),
            "next_step": action_summary.get("next_step") or recommendation.get("next_step"),
            "rationale_summary": recommendation.get("rationale_summary"),
            "action_reasons": list(recommendation.get("action_reasons", []) or []),
            "governance_flags": list(recommendation.get("governance_flags", []) or []),
            "validation_errors": list(recommendation.get("validation_errors", []) or []),
            "metrics_coverage_classification": recommendation.get("metrics_coverage_classification"),
            "short_cpu_coverage": short_window.get("coverage", {}).get("cpu", {}),
            "short_memory_coverage": short_window.get("coverage", {}).get("memory", {}),
            "short_cpu_p95": short_window.get("cpu", {}).get("p95"),
            "short_cpu_count": short_window.get("cpu", {}).get("count"),
            "short_memory_p95": short_window.get("memory", {}).get("p95"),
            "short_memory_count": short_window.get("memory", {}).get("count"),
            "long_cpu_p95": long_window.get("cpu", {}).get("p95"),
            "long_cpu_count": long_window.get("cpu", {}).get("count"),
            "long_memory_p95": long_window.get("memory", {}).get("p95"),
            "long_memory_count": long_window.get("memory", {}).get("count"),
            "long_cpu_coverage": long_window.get("coverage", {}).get("cpu", {}),
            "long_memory_coverage": long_window.get("coverage", {}).get("memory", {}),
            "long_cpu_daily_p95": rationale.get("cpu", {}).get("long_daily_p95"),
            "long_memory_daily_p95": rationale.get("memory", {}).get("long_daily_p95"),
            "shape_limits": recommendation.get("shape_limits"),
            "policy_thresholds": rationale.get("policy"),
            "current_monthly_cost": financial_summary.get("current_monthly_cost"),
            "optimized_monthly_cost": financial_summary.get("optimized_monthly_cost"),
            "annualized_savings": financial_summary.get("annualized_savings"),
            "warnings": warnings,
            "pricing_enabled": bool(pricing.get("pricing_enabled")),
            "pricing_attempted": bool(pricing.get("pricing_attempted", False)),
            "pricing_source": pricing.get("pricing_source"),
            "pricing_confidence": pricing.get("pricing_confidence"),
            "pricing_warnings": list(pricing.get("warnings", []) or []),
            "detected_part_numbers": pricing.get("detected_part_numbers"),
            "metrics_available": bool(
                (safe_float(short_window.get("cpu", {}).get("count")) or 0) > 0
                and (safe_float(short_window.get("memory", {}).get("count")) or 0) > 0
            ),
        }
        if pricing.get("currency_code"):
            row["pricing_currency_code"] = pricing.get("currency_code")
        if pricing.get("pricing_enabled"):
            if pricing.get("estimated_monthly_delta") is not None:
                row["estimated_monthly_delta"] = pricing.get("estimated_monthly_delta")
            if pricing.get("estimated_monthly_savings") is not None:
                row["estimated_monthly_savings"] = pricing.get("estimated_monthly_savings")
        return row

    def _scan_error_row(self, inventory: Dict[str, Any], error: Exception) -> Dict[str, Any]:
        return {
            "error": type(error).__name__,
            "region": inventory.get("region"),
            "compartment_id": inventory.get("compartment_id"),
            "compartment_name": inventory.get("compartment_name"),
            "availability_domain": inventory.get("availability_domain"),
            "instance_id": inventory.get("instance_id"),
            "display_name": inventory.get("display_name"),
            "lifecycle_state": inventory.get("lifecycle_state"),
            "shape": inventory.get("shape"),
            "ocpus": inventory.get("ocpus"),
            "memory_gb": inventory.get("memory_gb"),
            "owner_or_cost_center": derive_owner_or_cost_center(inventory),
            "overall_decision": "ERROR",
            "cpu_decision": "ERROR",
            "memory_decision": "ERROR",
            "recommended_ocpus": inventory.get("ocpus"),
            "recommended_memory_gb": inventory.get("memory_gb"),
            "rationale_summary": None,
            "short_cpu_p95": None,
            "short_cpu_count": None,
            "short_memory_p95": None,
            "short_memory_count": None,
            "long_cpu_p95": None,
            "long_cpu_count": None,
            "long_memory_p95": None,
            "long_memory_count": None,
            "short_cpu_coverage": {},
            "short_memory_coverage": {},
            "long_cpu_coverage": {},
            "long_memory_coverage": {},
            "long_cpu_daily_p95": {},
            "long_memory_daily_p95": {},
            "warnings": [f"Recommendation failed: {error}"],
            "pricing_enabled": False,
            "pricing_attempted": False,
            "pricing_source": "disabled",
            "pricing_confidence": None,
            "pricing_warnings": [],
            "detected_part_numbers": None,
            "metrics_available": False,
            "safety_class": "OBSERVATION_ONLY",
            "action_tier": ACTION_OBSERVE,
            "next_step": "Observe only for now; the recommendation workflow failed.",
            "action_reasons": [f"Recommendation failed: {error}"],
            "validation_errors": [],
            "metrics_coverage_classification": "sparse",
            "shape_limits": None,
            "policy_thresholds": None,
            "current_monthly_cost": None,
            "optimized_monthly_cost": None,
            "annualized_savings": None,
        }

    def _collect_scope_rows(self, args: argparse.Namespace) -> Dict[str, Any]:
        regions = self._resolve_regions(args)
        root_compartment_id = args.compartment_id or self.ctx.tenancy_id
        compartments = self.inventory.list_accessible_compartments(root_compartment_id=root_compartment_id)
        exclude_stopped = False if args.include_stopped else (True if not args.include_stopped and not args.exclude_stopped else bool(args.exclude_stopped))
        max_instances = args.max_instances if args.max_instances and args.max_instances > 0 else None
        scan_args = argparse.Namespace(**vars(args))
        scan_args.use_genai = False

        rows: List[Dict[str, Any]] = []
        scan_warnings: List[str] = []
        if not args.compartment_id:
            scan_warnings.append("No --compartment-id supplied. Scanning tenancy root.")
        counter_lock = Lock()
        stop_event = Event()
        processed_state = {"count": 0}

        def claim_slot() -> bool:
            with counter_lock:
                if max_instances is not None and processed_state["count"] >= max_instances:
                    stop_event.set()
                    return False
                processed_state["count"] += 1
                if max_instances is not None and processed_state["count"] >= max_instances:
                    stop_event.set()
                return True

        def scan_region(region: str) -> Dict[str, Any]:
            local_rows: List[Dict[str, Any]] = []
            local_warnings: List[str] = []
            try:
                region_clients = self.ctx.clients_for_region(region)
            except Exception as exc:
                return {"rows": [], "warnings": [f"Failed to initialize clients for region {region}: {exc}"]}

            for compartment in compartments:
                if stop_event.is_set() and max_instances is not None:
                    break
                try:
                    instances = self.inventory.list_instances_in_region(region_clients["compute"], compartment["id"])
                except Exception as exc:
                    local_warnings.append(
                        f"Failed to list instances in region {region} for compartment {compartment['id']}: {exc}"
                    )
                    continue

                for instance in instances:
                    if stop_event.is_set() and max_instances is not None:
                        break
                    lifecycle_state = str(getattr(instance, "lifecycle_state", "") or "")
                    if lifecycle_state in {"TERMINATED", "TERMINATING"}:
                        continue
                    if exclude_stopped and lifecycle_state == "STOPPED":
                        continue
                    if not claim_slot():
                        break

                    inventory = self.inventory.to_record_with_region(
                        instance,
                        region=region,
                        compartment_name=compartment.get("name"),
                    )
                    try:
                        recommendation_payload = self.recommendation.run(
                            inventory=inventory,
                            args=scan_args,
                            compute_client=region_clients["compute"],
                            monitoring_client=region_clients["monitoring"],
                            usage_client=region_clients["usage"],
                            enable_pricing=self._pricing_enabled(args),
                            instance_obj=instance,
                        )
                        recommendation_payload["monthly_hours"] = getattr(args, "monthly_hours", 730.0)
                        local_rows.append(self._scan_row_from_payload(recommendation_payload))
                    except Exception as exc:
                        local_rows.append(self._scan_error_row(inventory, exc))

            return {"rows": local_rows, "warnings": local_warnings}

        with ThreadPoolExecutor(max_workers=max(1, int(args.threads or 1))) as executor:
            futures = [executor.submit(scan_region, region) for region in regions]
            for future in as_completed(futures):
                try:
                    result = future.result()
                except Exception as exc:
                    scan_warnings.append(f"Region scan worker failed: {exc}")
                    continue
                rows.extend(result.get("rows", []))
                scan_warnings.extend(result.get("warnings", []))

        rows = sorted(
            rows,
            key=lambda row: (
                str(row.get("region", "")),
                str(row.get("compartment_name", "")),
                str(row.get("display_name", "")),
                str(row.get("instance_id", "")),
            ),
        )
        return {
            "root_compartment_id": root_compartment_id,
            "scope_complete": not any(
                message.startswith(("Failed to", "Region scan worker failed")) for message in scan_warnings
            ) and not any(row.get("error") for row in rows)
            and not (max_instances is not None and processed_state["count"] >= max_instances),
            "regions": regions,
            "compartments": compartments,
            "rows": rows,
            "warnings": scan_warnings,
        }

    def _build_scope_payload(
        self,
        args: argparse.Namespace,
        *,
        mode: str,
        collected: Dict[str, Any],
        include_business_summary: bool = True,
    ) -> Dict[str, Any]:
        rows = list(collected.get("rows", []) or [])
        top_limit = self._top_limit(args)

        def is_resize_decision(value: Any) -> bool:
            return str(value) in {"SCALE_UP", "SCALE_DOWN"}

        instances_with_metrics = sum(1 for row in rows if row.get("metrics_available"))
        instances_without_metrics = len(rows) - instances_with_metrics
        instances_with_sparse_long_window = sum(
            1
            for row in rows
            if effective_coverage_ratio(row.get("long_cpu_coverage", {})) < 0.5
            or effective_coverage_ratio(row.get("long_memory_coverage", {})) < 0.5
        )
        instances_review_recommended = sum(1 for row in rows if row.get("safety_class") == "REVIEW_RECOMMENDED")
        cpu_only_candidates = sum(
            1
            for row in rows
            if is_resize_decision(row.get("cpu_decision")) and not is_resize_decision(row.get("memory_decision"))
        )
        memory_only_candidates = sum(
            1
            for row in rows
            if not is_resize_decision(row.get("cpu_decision")) and is_resize_decision(row.get("memory_decision"))
        )
        combined_candidates = sum(
            1
            for row in rows
            if is_resize_decision(row.get("cpu_decision")) and is_resize_decision(row.get("memory_decision"))
        )
        no_change_count = sum(1 for row in rows if row.get("overall_decision") == "NO_CHANGE")
        pricing_available_count = sum(1 for row in rows if row.get("pricing_enabled"))
        candidate_groups = build_candidate_groups(rows)
        portfolio_summary = build_portfolio_summary(rows)
        recommend_summary = build_recommend_summary(rows) if mode == "recommend" else None
        savings_funnel = build_savings_funnel(candidate_groups)
        breakdowns = build_breakdowns(rows)
        top_savings_candidates = sorted(
            [
                row
                for row in rows
                if row.get("pricing_enabled") and (safe_float(row.get("estimated_monthly_savings")) or 0.0) > 0.0
            ],
            key=lambda row: safe_float(row.get("estimated_monthly_savings")) or 0.0,
            reverse=True,
        )[:top_limit]

        payload = {
            "generated_at": iso_z(now_utc()),
            "mode": mode,
            "scope_mode": "scoped",
            "tenancy_id": self.ctx.tenancy_id,
            "currency_code": str(getattr(args, "pricing_currency", "USD") or "USD").upper(),
            "root_compartment_id": collected.get("root_compartment_id"),
            "scope_complete": collected.get("scope_complete", True),
            "top_limit": top_limit,
            "regions": collected.get("regions", []),
            "compartments_scanned": len(collected.get("compartments", []) or []),
            "instances_scanned": len(rows),
            "instances_with_metrics": instances_with_metrics,
            "instances_without_metrics": instances_without_metrics,
            "instances_with_sparse_long_window": instances_with_sparse_long_window,
            "instances_review_recommended": instances_review_recommended,
            "cpu_only_candidates": cpu_only_candidates,
            "memory_only_candidates": memory_only_candidates,
            "combined_candidates": combined_candidates,
            "no_change_count": no_change_count,
            "pricing_available_count": pricing_available_count,
            "portfolio_summary": portfolio_summary,
            "savings_funnel": savings_funnel,
            "breakdowns": breakdowns,
            "candidate_groups": candidate_groups,
            "top_savings_candidates": top_savings_candidates,
            "rows": rows,
            "warnings": list(collected.get("warnings", []) or []),
        }
        if recommend_summary is not None:
            payload["recommend_summary"] = recommend_summary
        if include_business_summary:
            if mode == "recommend":
                summary_input = {
                    "recommend_summary": recommend_summary,
                    "candidate_counts": {
                        "auto_apply": len(candidate_groups.get("auto_apply", []) or []),
                        "review_required": len(candidate_groups.get("review_required", []) or []),
                        "observe": len(candidate_groups.get("observe", []) or []),
                    },
                    "top_candidates": [
                        {
                            "display_name": row.get("display_name"),
                            "region": row.get("region"),
                            "action_tier": row.get("action_tier"),
                            "estimated_monthly_savings": row.get("estimated_monthly_savings"),
                            "next_step": row.get("next_step"),
                        }
                        for row in top_savings_candidates[:5]
                    ],
                    "warnings": payload["warnings"][:5],
                }
            else:
                summary_input = {
                    "portfolio_summary": portfolio_summary,
                    "savings_funnel": savings_funnel,
                    "top_breakdowns": {
                        "compartments": [
                            {
                                "compartment_name": item.get("compartment_name"),
                                "monthly_savings": item.get("monthly_savings"),
                                "instance_count": item.get("instance_count"),
                            }
                            for item in (breakdowns.get("by_compartment", []) or [])[:3]
                        ],
                        "regions": [
                            {
                                "region": item.get("region"),
                                "monthly_savings": item.get("monthly_savings"),
                                "instance_count": item.get("instance_count"),
                            }
                            for item in (breakdowns.get("by_region", []) or [])[:3]
                        ],
                    },
                    "warnings": payload["warnings"][:5],
                }
            payload["business_summary"] = self._maybe_business_summary(
                args,
                summary_type=mode,
                summary_input=summary_input,
                compartment_id=args.compartment_id or self.ctx.tenancy_id,
            )
        return payload

    def _single_instance_recommendation(self, args: argparse.Namespace) -> Dict[str, Any]:
        instance = self.inventory.get_instance(args.instance_id)
        inventory = self.inventory.to_record(instance)
        inventory["region"] = self.ctx.region
        inventory["compartment_name"] = self.inventory._resolve_compartment_name(inventory["compartment_id"])
        payload = self.recommendation.run(
            inventory=inventory,
            args=args,
            enable_pricing=self._pricing_enabled(args),
        )
        payload["mode"] = "recommend"
        payload["scope_mode"] = "single_instance"
        payload["currency_code"] = str(getattr(args, "pricing_currency", "USD") or "USD").upper()
        payload["monthly_hours"] = getattr(args, "monthly_hours", 730.0)
        payload["business_summary"] = self._maybe_business_summary(
            args,
            summary_type="recommend",
            summary_input={
                "instance": {
                    "display_name": payload.get("instance", {}).get("display_name"),
                    "shape": payload.get("instance", {}).get("shape"),
                    "owner_or_cost_center": payload.get("owner_or_cost_center"),
                },
                "financial_summary": payload.get("financial_summary"),
                "action_summary": payload.get("action_summary"),
                "explanation_basis": payload.get("explanation_basis"),
            },
            compartment_id=payload.get("instance", {}).get("compartment_id"),
        )
        return payload

    def recommend(self, args: argparse.Namespace) -> Dict[str, Any]:
        if getattr(args, "instance_id", None):
            return self._single_instance_recommendation(args)
        collected = self._collect_scope_rows(args)
        return self._build_scope_payload(args, mode="recommend", collected=collected, include_business_summary=True)

    def scan(self, args: argparse.Namespace) -> Dict[str, Any]:
        collected = self._collect_scope_rows(args)
        return self._build_scope_payload(args, mode="scan", collected=collected, include_business_summary=True)

    def _build_execution_plan(
        self,
        rows: Sequence[Dict[str, Any]],
        *,
        apply_tier: str,
        max_changes: Optional[int],
    ) -> List[Dict[str, Any]]:
        plan: List[Dict[str, Any]] = []
        selected_count = 0
        for row in sorted(rows, key=candidate_sort_key):
            entry = {
                "region": row.get("region"),
                "compartment_id": row.get("compartment_id"),
                "compartment_name": row.get("compartment_name"),
                "instance_id": row.get("instance_id"),
                "display_name": row.get("display_name"),
                "shape": row.get("shape"),
                "owner_or_cost_center": row.get("owner_or_cost_center"),
                "ocpus": row.get("ocpus"),
                "memory_gb": row.get("memory_gb"),
                "action_tier": row.get("action_tier"),
                "overall_decision": row.get("overall_decision"),
                "recommended_ocpus": row.get("recommended_ocpus"),
                "recommended_memory_gb": row.get("recommended_memory_gb"),
                "estimated_monthly_savings": row.get("estimated_monthly_savings"),
                "pricing_currency_code": row.get("pricing_currency_code"),
                "pricing_source": row.get("pricing_source"),
                "pricing_confidence": row.get("pricing_confidence"),
                "next_step": row.get("next_step"),
                "rationale_summary": row.get("rationale_summary"),
                "action_reasons": list(row.get("action_reasons", []) or []),
                "short_cpu_p95": row.get("short_cpu_p95"),
                "long_cpu_p95": row.get("long_cpu_p95"),
                "short_memory_p95": row.get("short_memory_p95"),
                "long_memory_p95": row.get("long_memory_p95"),
                "long_cpu_coverage": row.get("long_cpu_coverage"),
                "long_memory_coverage": row.get("long_memory_coverage"),
                "validation_errors": list(row.get("validation_errors", []) or []),
                "shape_limits": row.get("shape_limits"),
                "policy_thresholds": row.get("policy_thresholds"),
                "pricing_warnings": list(row.get("pricing_warnings", []) or []),
                "detected_part_numbers": row.get("detected_part_numbers"),
                "eligible": False,
                "selected_for_execution": False,
                "status": "SKIPPED",
                "message": None,
                "skip_reason": None,
            }
            if row.get("overall_decision") != "RESIZE":
                entry["skip_reason"] = "No actionable resize recommendation."
            elif row.get("validation_errors"):
                entry["skip_reason"] = "Validation errors block automated apply."
            elif str(row.get("action_tier") or ACTION_OBSERVE) == ACTION_OBSERVE:
                entry["skip_reason"] = "Observe tier is not executable."
            elif not apply_tier_allows(str(row.get("action_tier") or ACTION_OBSERVE), apply_tier):
                entry["skip_reason"] = f"Excluded by --apply-tier={apply_tier}."
            else:
                entry["eligible"] = True
                if max_changes is not None and max_changes >= 0 and selected_count >= max_changes:
                    entry["skip_reason"] = "Deferred by --max-changes cap."
                else:
                    entry["selected_for_execution"] = True
                    entry["status"] = "PLANNED"
                    selected_count += 1
            plan.append(entry)
        return plan

    def _finalize_execution_summary(self, execution_results: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        identified = summarize_currency(
            item.get("estimated_monthly_savings")
            for item in execution_results
            if item.get("overall_decision") == "RESIZE"
        )
        eligible = summarize_currency(
            item.get("estimated_monthly_savings")
            for item in execution_results
            if item.get("eligible")
        )
        submitted = summarize_currency(
            item.get("estimated_monthly_savings")
            for item in execution_results
            if item.get("api_call_submitted")
        )
        verified = summarize_currency(
            item.get("estimated_monthly_savings")
            for item in execution_results
            if item.get("verification", {}).get("ok")
        )
        deferred = rounded_money(max(0.0, (identified or 0.0) - (verified or 0.0))) if identified is not None else None
        return {
            "candidates_considered": len(execution_results),
            "candidates_eligible": sum(1 for item in execution_results if item.get("eligible")),
            "changes_submitted": sum(1 for item in execution_results if item.get("api_call_submitted")),
            "changes_verified": sum(1 for item in execution_results if item.get("verification", {}).get("ok")),
            "changes_failed": sum(1 for item in execution_results if item.get("status") == "FAILED"),
            "changes_skipped": sum(
                1 for item in execution_results if item.get("status") in {"SKIPPED", "ELIGIBLE_NOT_APPLIED"}
            ),
            "expected_monthly_savings_identified": identified,
            "expected_monthly_savings_eligible": eligible,
            "expected_monthly_savings_submitted": submitted,
            "expected_monthly_savings_verified": verified,
            "expected_monthly_savings_deferred": deferred,
        }

    def _execute_plan(
        self,
        plan: Sequence[Dict[str, Any]],
        *,
        do_apply: bool,
        wait_for_verify: bool,
        timeout_seconds: int,
        poll_seconds: int,
        acknowledge_reboot: bool = False,
    ) -> Dict[str, Any]:
        execution_results: List[Dict[str, Any]] = []
        warnings: List[str] = []

        for original in plan:
            item = dict(original)
            if not item.get("selected_for_execution"):
                item["status"] = "SKIPPED"
                item["message"] = item.get("skip_reason")
                execution_results.append(item)
                continue
            if not do_apply:
                item["status"] = "ELIGIBLE_NOT_APPLIED"
                item["message"] = "Apply not requested. Candidate remains deferred."
                execution_results.append(item)
                continue
            try:
                resize_payload = self.resize.run(
                    instance_id=item["instance_id"],
                    ocpus=float(item["recommended_ocpus"]),
                    memory_gb=float(item["recommended_memory_gb"]),
                    apply=True,
                    region=item.get("region"),
                    acknowledge_reboot=acknowledge_reboot,
                    expected_ocpus=item.get("ocpus"),
                    expected_memory_gb=item.get("memory_gb"),
                    expected_shape=item.get("shape"),
                )
                item["resize"] = resize_payload
                item["api_call_submitted"] = bool(resize_payload.get("api_call_submitted"))
                item["message"] = resize_payload.get("message")
                if resize_payload.get("validation_errors"):
                    item["status"] = "SKIPPED"
                    item["skip_reason"] = "Validation errors block automated apply."
                elif resize_payload.get("api_call_submitted"):
                    item["status"] = "SUBMITTED"
                    if wait_for_verify:
                        verification = self.verify.run(
                            instance_id=item["instance_id"],
                            expected_ocpus=float(item["recommended_ocpus"]),
                            expected_memory_gb=float(item["recommended_memory_gb"]),
                            timeout_seconds=timeout_seconds,
                            poll_seconds=poll_seconds,
                            region=item.get("region"),
                        )
                        item["verification"] = verification
                        item["status"] = "VERIFIED" if verification.get("ok") else "FAILED"
                    execution_results.append(item)
                    continue
                else:
                    item["status"] = "SKIPPED"
                    item["skip_reason"] = resize_payload.get("message")
            except Exception as exc:
                item["status"] = "FAILED"
                item["message"] = f"Resize execution failed: {exc}"
                warnings.append(item["message"])
            execution_results.append(item)

        return {
            "execution_results": execution_results,
            "execution_summary": self._finalize_execution_summary(execution_results),
            "warnings": warnings,
        }

    def _verify_execution_results(
        self,
        execution_results: Sequence[Dict[str, Any]],
        *,
        timeout_seconds: int,
        poll_seconds: int,
    ) -> Dict[str, Any]:
        updated_results: List[Dict[str, Any]] = []
        warnings: List[str] = []
        for original in execution_results:
            item = dict(original)
            if item.get("api_call_submitted") and not item.get("verification"):
                try:
                    verification = self.verify.run(
                        instance_id=item["instance_id"],
                        expected_ocpus=float(item["recommended_ocpus"]),
                        expected_memory_gb=float(item["recommended_memory_gb"]),
                        timeout_seconds=timeout_seconds,
                        poll_seconds=poll_seconds,
                        region=item.get("region"),
                    )
                    item["verification"] = verification
                    item["status"] = "VERIFIED" if verification.get("ok") else "FAILED"
                except Exception as exc:
                    item["status"] = "FAILED"
                    item["verification"] = {
                        "ok": False,
                        "final_state": None,
                        "final_shape_config": {
                            "ocpus": None,
                            "memory_gb": None,
                        },
                        "error": f"Verification failed: {exc}",
                    }
                    warnings.append(f"Verification failed for {item.get('display_name', item.get('instance_id'))}: {exc}")
            updated_results.append(item)
        return {
            "execution_results": updated_results,
            "execution_summary": self._finalize_execution_summary(updated_results),
            "warnings": warnings,
        }

    def _attach_execution(
        self,
        payload: Dict[str, Any],
        *,
        rows: Sequence[Dict[str, Any]],
        args: argparse.Namespace,
        do_apply: bool,
        summary_type: str,
        compartment_id: Optional[str],
    ) -> Dict[str, Any]:
        if do_apply and payload.get("scope_complete") is False:
            payload["validation_errors"] = ["Fleet assessment is incomplete. Resolve errors or execute a separately assessed instance."]
            return payload
        plan = self._build_execution_plan(
            rows,
            apply_tier=getattr(args, "apply_tier", "auto"),
            max_changes=getattr(args, "max_changes", None),
        )
        execution_payload = self._execute_plan(
            plan,
            do_apply=do_apply,
            wait_for_verify=bool(getattr(args, "wait", False)),
            timeout_seconds=int(getattr(args, "timeout_seconds", 1800) or 1800),
            poll_seconds=int(getattr(args, "poll_seconds", 15) or 15),
            acknowledge_reboot=bool(getattr(args, "acknowledge_reboot", False)),
        )
        payload["execution_results"] = execution_payload["execution_results"]
        payload["execution_summary"] = execution_payload["execution_summary"]
        combined_warnings = list(payload.get("warnings", []) or [])
        combined_warnings.extend(execution_payload.get("warnings", []) or [])
        payload["warnings"] = combined_warnings
        payload["business_summary"] = self._maybe_business_summary(
            args,
            summary_type=summary_type,
            summary_input={
                "execution_summary": payload["execution_summary"],
                "portfolio_summary": payload.get("portfolio_summary"),
                "action_summary": payload.get("action_summary"),
                "top_results": [
                    {
                        "display_name": item.get("display_name"),
                        "status": item.get("status"),
                        "action_tier": item.get("action_tier"),
                        "estimated_monthly_savings": item.get("estimated_monthly_savings"),
                    }
                    for item in payload["execution_results"][:5]
                ],
            },
            compartment_id=compartment_id,
        )
        payload["realization_summary"] = {
            "identified_monthly_savings": payload["execution_summary"].get("expected_monthly_savings_identified"),
            "submitted_monthly_savings": payload["execution_summary"].get("expected_monthly_savings_submitted"),
            "verified_monthly_savings": payload["execution_summary"].get("expected_monthly_savings_verified"),
            "deferred_monthly_savings": payload["execution_summary"].get("expected_monthly_savings_deferred"),
            "basis": "Estimated monthly run-rate savings for configuration changes; not measured billing savings.",
        }
        return payload

    def apply(self, args: argparse.Namespace) -> Dict[str, Any]:
        if getattr(args, "instance_id", None):
            payload = self._single_instance_recommendation(args)
            payload["mode"] = "apply"
            row = self._scan_row_from_payload(payload)
            return self._attach_execution(
                payload,
                rows=[row],
                args=args,
                do_apply=bool(args.apply),
                summary_type="apply",
                compartment_id=payload.get("instance", {}).get("compartment_id"),
            )

        collected = self._collect_scope_rows(args)
        # Scoped execution acts on the same explicit recommendation candidates that
        # power the user-facing `recommend` command, not the scan presentation.
        payload = self._build_scope_payload(args, mode="recommend", collected=collected, include_business_summary=False)
        payload["mode"] = "apply"
        return self._attach_execution(
            payload,
            rows=payload.get("rows", []),
            args=args,
            do_apply=bool(args.apply),
            summary_type="apply",
            compartment_id=args.compartment_id or self.ctx.tenancy_id,
        )

    def run(self, args: argparse.Namespace) -> Dict[str, Any]:
        if getattr(args, "instance_id", None):
            payload = self._single_instance_recommendation(args)
            payload["mode"] = "run"
            row = self._scan_row_from_payload(payload)
            return self._attach_execution(
                payload,
                rows=[row],
                args=args,
                do_apply=bool(getattr(args, "apply", False)),
                summary_type="run",
                compartment_id=payload.get("instance", {}).get("compartment_id"),
            )

        collected = self._collect_scope_rows(args)
        # Scoped workflow execution starts from the same ranked recommendation
        # candidate set used by `recommend`.
        payload = self._build_scope_payload(args, mode="recommend", collected=collected, include_business_summary=False)
        payload["mode"] = "run"
        return self._attach_execution(
            payload,
            rows=payload.get("rows", []),
            args=args,
            do_apply=bool(getattr(args, "apply", False)),
            summary_type="run",
            compartment_id=args.compartment_id or self.ctx.tenancy_id,
        )


def interactive_main(ctx: Optional[OCIContext], planner: Optional[Planner], args: argparse.Namespace) -> None:
    print("OCI Rightsizer for Compute Workloads")
    print("====================================")

    timeframe_days, timeframe_label = prompt_timeframe()
    args.pricing_lookback_days = timeframe_days
    args.cpu_scale_down_days_window = timeframe_days
    args.analysis_timeframe_days = timeframe_days
    args.analysis_timeframe_label = timeframe_label
    args.instance_id = None
    args.output = "pretty"

    model_id = prompt_genai_model_id()
    args.use_genai = bool(model_id)
    args.genai_model_id = model_id

    if ctx is None or planner is None:
        ctx = OCIContext(profile=args.profile, config_file=args.config_file, region=args.region, auth=args.auth)
        planner = Planner(ctx)

    scan_payload = planner.scan(args)
    render_interactive_scan_summary(scan_payload, include_header=False)
    if scan_payload.get("scope_complete") is False:
        print("Assessment is incomplete. Resolve scan errors or narrow the scope before continuing.")
        return

    compartment_choices = build_actionable_compartment_choices(scan_payload.get("rows", []) or [])
    if not compartment_choices:
        print("\nNo actionable resize candidates were found across the scanned tenancy scope.")
        return

    selected_compartment = prompt_choice(
        "\nChoose a compartment to inspect for recommendations:",
        compartment_choices,
        formatter=lambda index, item: (
            f"  [{index}] {item.get('compartment_name', 'n/a')} | "
            f"{item.get('actionable_count', 0)} actionable workloads | "
            f"{format_money(item.get('monthly_savings'), str(scan_payload.get('currency_code') or 'USD').upper())} / mo"
        ),
    )

    recommend_args = argparse.Namespace(**vars(args))
    recommend_args.compartment_id = selected_compartment["compartment_id"]
    recommend_args.instance_id = None
    recommend_payload = planner.recommend(recommend_args)
    if recommend_payload.get("scope_complete") is False:
        print("Recommendation assessment is incomplete. No changes submitted.")
        return
    interactive_currency_code = str(recommend_payload.get("currency_code") or scan_payload.get("currency_code") or "USD").upper()

    compartment_rows = filter_rows_by_compartment(
        recommend_payload.get("rows", []) or [],
        selected_compartment["compartment_id"],
    )
    actionable_rows = filter_actionable_rows(compartment_rows)
    auto_rows = [row for row in sorted(actionable_rows, key=candidate_sort_key) if str(row.get("action_tier")) == ACTION_AUTO_APPLY]
    review_rows = [row for row in sorted(actionable_rows, key=candidate_sort_key) if str(row.get("action_tier")) == ACTION_REVIEW_REQUIRED]
    displayed_rows = auto_rows + review_rows
    if not displayed_rows:
        print("\nNo actionable workloads were found in the selected compartment.")
        return

    render_interactive_recommendations(
        selected_compartment,
        displayed_rows,
        currency_code=interactive_currency_code,
        summary_text=build_interactive_recommendation_summary(
            compartment_rows,
            currency_code=interactive_currency_code,
        ),
    )
    selected_rows = prompt_instance_multi_select(displayed_rows)
    if not selected_rows:
        print("\nNo workloads were selected.")
        return

    print("\nResizing reboots running instances. Validate backups, maintenance approval and application recovery first.")
    print("This run submits at most one selected change.")
    if not prompt_yes_no("Submit the selected resize during the approved maintenance window?", default=False):
        print("No changes submitted.")
        return
    plan = planner._build_execution_plan(selected_rows, apply_tier="review", max_changes=1)
    execution_payload = planner._execute_plan(
        plan,
        do_apply=True,
        wait_for_verify=False,
        timeout_seconds=int(getattr(args, "timeout_seconds", 1800) or 1800),
        poll_seconds=int(getattr(args, "poll_seconds", 15) or 15),
        acknowledge_reboot=True,
    )
    execution_payload["currency_code"] = scan_payload.get("currency_code", "USD")
    render_interactive_submission_summary(execution_payload)

    submitted_count = sum(1 for item in execution_payload.get("execution_results", []) if item.get("api_call_submitted"))
    if submitted_count == 0:
        print("\nNo resize requests were submitted.")
        return

    if not prompt_yes_no("Wait for validation?", default=False):
        print("Validation was skipped by user choice.")
        return

    verification_payload = planner._verify_execution_results(
        execution_payload.get("execution_results", []),
        timeout_seconds=int(getattr(args, "timeout_seconds", 1800) or 1800),
        poll_seconds=int(getattr(args, "poll_seconds", 15) or 15),
    )
    verification_payload["currency_code"] = execution_payload.get("currency_code", "USD")
    render_interactive_verification_summary(verification_payload)


def ensure_instance_id(ctx: OCIContext, args: argparse.Namespace) -> None:
    if hasattr(args, "instance_id") and not args.instance_id:
        compartment_id = getattr(args, "compartment_id", None) or ctx.tenancy_id
        args.instance_id = choose_instance(ctx, compartment_id)


def add_policy_args(parser: argparse.ArgumentParser) -> None:
    hidden = argparse.SUPPRESS
    parser.add_argument("--cpu-scale-up-hours", type=int, default=24, help=hidden)
    parser.add_argument("--cpu-scale-up-resolution", default="5m", help=hidden)
    parser.add_argument("--cpu-scale-up-threshold", type=float, default=75.0, help=hidden)
    parser.add_argument("--cpu-scale-up-sustained-minutes", type=int, default=15, help=hidden)
    parser.add_argument("--cpu-scale-up-required-periods", type=int, default=3, help=hidden)
    parser.add_argument("--cpu-scale-down-days-window", type=int, default=14, help=hidden)
    parser.add_argument("--cpu-scale-down-resolution", default="1h", help=hidden)
    parser.add_argument("--cpu-scale-down-threshold", type=float, default=30.0, help=hidden)
    parser.add_argument("--cpu-scale-down-required-days", type=int, default=7, help=hidden)
    parser.add_argument("--cpu-scale-down-target", type=float, default=50.0, help=hidden)
    parser.add_argument("--memory-scale-up-threshold", type=float, default=80.0, help=hidden)
    parser.add_argument("--memory-scale-up-sustained-minutes", type=int, default=15, help=hidden)
    parser.add_argument("--memory-scale-up-required-periods", type=int, default=3, help=hidden)
    parser.add_argument("--memory-scale-down-threshold", type=float, default=40.0, help=hidden)
    parser.add_argument("--memory-scale-down-required-days", type=int, default=7, help=hidden)
    parser.add_argument("--memory-scale-down-target", type=float, default=50.0, help=hidden)
    parser.add_argument("--scale-down-memory-p95-max-percent", type=float, default=70.0, help=hidden)
    parser.add_argument("--upsize-factor", type=float, default=1.5, help=hidden)
    parser.add_argument("--min-memory-per-ocpu-gb", type=float, default=4.0, help=hidden)
    parser.add_argument("--pricing-lookback-days", type=int, default=30, help=hidden)
    parser.add_argument("--monthly-hours", type=float, default=730.0, help=hidden)
    parser.add_argument(
        "--pricing-source",
        choices=["auto", "effective", "list"],
        default="auto",
        help="Savings pricing source: auto, effective billed rates, or list pricing.",
    )
    parser.add_argument("--pricing-currency", default="USD", help="Currency code for pricing output.")
    parser.add_argument(
        "--pricing-catalog",
        default=os.getenv("RIGHTSIZER_PRICING_CATALOG"),
        help="Optional OCI pricing catalog JSON file for list-pricing estimates.",
    )
    parser.add_argument("--pricing", action="store_true", help=hidden)
    parser.add_argument("--no-pricing", action="store_true", help=hidden)
    parser.add_argument(
        "--use-genai",
        action="store_true",
        help="Add a short business-language summary without changing deterministic decisions.",
    )
    parser.add_argument("--genai-compartment-id", help="OCI compartment OCID used for Generative AI requests.")
    parser.add_argument("--genai-model-id", help="OCI Generative AI model OCID for narrative summaries.")
    parser.add_argument("--genai-temperature", type=float, default=0.1, help=hidden)
    parser.add_argument("--genai-max-tokens", type=int, default=300, help=hidden)


def add_scope_args(parser: argparse.ArgumentParser, *, include_top: bool = True) -> None:
    hidden = argparse.SUPPRESS
    parser.add_argument("--regions", default="current", help="Configured region (current), comma-separated OCI regions, or explicitly all.")
    parser.add_argument("--compartment-id", help="Root compartment OCID to scope the workflow. Defaults to tenancy root.")
    parser.add_argument("--threads", type=int, default=4, help=hidden)
    parser.add_argument("--include-stopped", action="store_true", help=hidden)
    parser.add_argument("--exclude-stopped", action="store_true", help=hidden)
    parser.add_argument("--max-instances", type=int, help=hidden)
    if include_top:
        parser.add_argument("--top", type=int, default=10, help="Number of candidates to surface in pretty output.")


def add_execution_args(parser: argparse.ArgumentParser, *, include_apply_switch: bool) -> None:
    if include_apply_switch:
        parser.add_argument("--apply", action="store_true", help="Submit eligible deterministic changes.")
    parser.add_argument(
        "--apply-tier",
        choices=["auto", "review", "all"],
        default="auto",
        help="auto = AUTO_APPLY only; review = AUTO_APPLY plus REVIEW_REQUIRED; all = all actionable tiers.",
    )
    parser.add_argument("--max-changes", type=int, default=1, help="Maximum submitted changes. Default: 1.")
    parser.add_argument("--acknowledge-reboot", action="store_true", help="Confirm that running instances will reboot during resizing.")
    parser.add_argument("--wait", action="store_true", help="Verify submitted changes after apply.")
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--poll-seconds", type=int, default=15)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=os.path.basename(sys.argv[0]) or "rightsizer.py",
        description=(
            "FinOps-first OCI VM rightsizing CLI. Use scan for portfolio savings, "
            "recommend for prioritized actions, apply for execution, and run for end-to-end assessment and verification. "
            "Run without a subcommand to launch the guided workflow. Pricing is attempted by default."
        ),
    )
    parser.add_argument("--profile", default=os.getenv("OCI_CLI_PROFILE") or os.getenv("OCI_PROFILE") or "DEFAULT")
    parser.add_argument("--config-file", default=DEFAULT_CONFIG_FILE)
    parser.add_argument("--region")
    parser.add_argument("--auth", choices=AUTH_MODES, default="auto", help="Authentication mode; auto honors OCI_CLI_AUTH or token entries in the profile.")
    parser.add_argument("--version", action="version", version=f"OCI Right-Sizer {__version__}")
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"))
    parser.add_argument("--output", choices=["pretty", "json"], default=os.getenv("RIGHTSIZER_OUTPUT", "pretty"))
    parser.add_argument(
        "--developer-details",
        action="store_true",
        help="Expand pretty output with deeper metrics and internal recommendation details.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True, metavar="{scan,recommend,apply,run}")

    scan = subparsers.add_parser("scan", help="Portfolio savings view for a scoped OCI VM fleet.")
    scan.description = "Portfolio-level savings summary for a scoped OCI VM fleet. Pretty output stays high-level and omits VM-specific recommendations."
    add_scope_args(scan, include_top=True)
    add_policy_args(scan)

    recommend = subparsers.add_parser(
        "recommend",
        help="Ranked VM action plan for one instance or a scoped fleet.",
    )
    recommend.description = "Show explicit VM recommendations grouped by action tier for FinOps review and approval."
    recommend.add_argument("--instance-id")
    add_scope_args(recommend, include_top=True)
    add_policy_args(recommend)

    apply = subparsers.add_parser(
        "apply",
        help="Preview candidates; submit only with --apply and --acknowledge-reboot.",
    )
    apply.description = "Preview candidates by default. Explicitly submit eligible changes and report estimated savings for their configurations."
    apply.add_argument("--instance-id")
    add_scope_args(apply, include_top=True)
    add_policy_args(apply)
    add_execution_args(apply, include_apply_switch=True)

    run = subparsers.add_parser(
        "run",
        help="Assess, optionally execute, and verify configuration changes.",
    )
    run.description = "Preview or explicitly apply candidates, verify configurations, and summarize estimated savings."
    run.add_argument("--instance-id")
    add_scope_args(run, include_top=True)
    add_policy_args(run)
    add_execution_args(run, include_apply_switch=True)

    interactive = subparsers.add_parser("interactive", help=argparse.SUPPRESS)
    add_scope_args(interactive, include_top=True)
    add_policy_args(interactive)
    interactive.add_argument("--timeout-seconds", type=int, default=1800, help=argparse.SUPPRESS)
    interactive.add_argument("--poll-seconds", type=int, default=15, help=argparse.SUPPRESS)

    discover = subparsers.add_parser("discover", help=argparse.SUPPRESS)
    discover.add_argument("--compartment-id")
    discover.add_argument("--instance-id")

    metrics = subparsers.add_parser("metrics", help=argparse.SUPPRESS)
    metrics.add_argument("--compartment-id")
    metrics.add_argument("--instance-id")
    metrics.add_argument("--hours", type=int, default=24)
    metrics.add_argument("--resolution", default="5m")
    metrics.add_argument("--include-datapoints", action="store_true")

    pricing_debug = subparsers.add_parser(
        "pricing-debug",
        help=argparse.SUPPRESS,
    )
    pricing_debug.add_argument("--compartment-id")
    pricing_debug.add_argument("--instance-id")
    pricing_debug.add_argument("--lookback-days", type=int, default=30)
    pricing_debug.add_argument(
        "--group-by",
        nargs="+",
        default=["service", "skuPartNumber", "resourceId"],
        help="Grouping fields for Usage API summarized usage output.",
    )
    pricing_debug.add_argument(
        "--query-type",
        choices=["COST", "USAGE"],
        default="COST",
        help="Usage API query type.",
    )
    pricing_debug.add_argument(
        "--granularity",
        choices=["DAILY", "MONTHLY", "TOTAL"],
        default="DAILY",
        help="Usage API aggregation granularity.",
    )
    pricing_debug.add_argument(
        "--filter",
        action="append",
        default=[],
        help="Additional filter in key=value form. May be repeated.",
    )
    pricing_debug.add_argument(
        "--no-default-filters",
        action="store_true",
        help="Disable the default legacy debug filters (service=Compute and resourceId=<instance_id>).",
    )

    resize = subparsers.add_parser("resize", help=argparse.SUPPRESS)
    resize.add_argument("--compartment-id")
    resize.add_argument("--instance-id")
    resize.add_argument("--ocpus", type=float, required=True)
    resize.add_argument("--memory-gb", type=float, required=True)
    resize.add_argument("--apply", action="store_true")
    resize.add_argument("--acknowledge-reboot", action="store_true")
    resize.add_argument("--wait", action="store_true")
    resize.add_argument("--timeout-seconds", type=int, default=1800)
    resize.add_argument("--poll-seconds", type=int, default=15)

    verify = subparsers.add_parser("verify", help=argparse.SUPPRESS)
    verify.add_argument("--compartment-id")
    verify.add_argument("--instance-id")
    verify.add_argument("--expected-ocpus", type=float)
    verify.add_argument("--expected-memory-gb", type=float)
    verify.add_argument("--timeout-seconds", type=int, default=1800)
    verify.add_argument("--poll-seconds", type=int, default=15)

    hidden_commands = {"interactive", "discover", "metrics", "pricing-debug", "resize", "verify"}
    subparsers._choices_actions = [
        action for action in subparsers._choices_actions if getattr(action, "dest", None) not in hidden_commands
    ]

    return parser


def has_explicit_command(argv_tokens: Sequence[str]) -> bool:
    command_names = {"scan", "recommend", "apply", "run", "interactive", "discover", "metrics", "pricing-debug", "resize", "verify"}
    global_options_with_values = {"--profile", "--config-file", "--region", "--log-level", "--output", "--auth"}
    global_flags = {"--developer-details", "--version"}

    index = 0
    while index < len(argv_tokens):
        token = argv_tokens[index]
        if token in {"-h", "--help", "--version"}:
            return True
        if token in global_options_with_values:
            index += 2
            continue
        if any(token.startswith(f"{option}=") for option in global_options_with_values):
            index += 1
            continue
        if token in global_flags:
            index += 1
            continue
        return token in command_names
    return False


def interactive_argv(argv_tokens: Sequence[str]) -> List[str]:
    """Insert the default command after global flags, before scope flags."""
    options = {"--profile", "--config-file", "--region", "--log-level", "--output", "--auth"}
    index = 0
    while index < len(argv_tokens):
        token = argv_tokens[index]
        if token in options:
            index += 2
        elif token == "--developer-details" or any(token.startswith(option + "=") for option in options):
            index += 1
        else:
            break
    return [*argv_tokens[:index], "interactive", *argv_tokens[index:]]


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, str(level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    # SDK HTTP debug output can contain signed headers. Keep it out of CLI diagnostics.
    logging.getLogger("oci").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def validate_arguments(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Reject unsafe numeric inputs before building any cloud clients."""
    positive_integers = (
        "threads", "top", "max_instances", "timeout_seconds", "poll_seconds", "hours",
        "cpu_scale_up_hours", "cpu_scale_up_sustained_minutes", "cpu_scale_up_required_periods",
        "cpu_scale_down_days_window", "cpu_scale_down_required_days", "memory_scale_up_sustained_minutes",
        "memory_scale_up_required_periods", "memory_scale_down_required_days", "pricing_lookback_days",
        "lookback_days", "genai_max_tokens",
    )
    for name in positive_integers:
        value = getattr(args, name, None)
        if value is not None and value <= 0:
            parser.error(f"--{name.replace('_', '-')} must be greater than zero.")
    if getattr(args, "threads", 1) > 32:
        parser.error("--threads must be at most 32.")
    if getattr(args, "max_changes", 1) < 0:
        parser.error("--max-changes must be zero or greater.")
    for name, value in vars(args).items():
        if isinstance(value, float) and (not math.isfinite(value) or value < 0 or (value == 0 and name != "genai_temperature")):
            parser.error(f"--{name.replace('_', '-')} must be a finite positive number.")
    for name in (
        "cpu_scale_up_threshold", "cpu_scale_down_threshold", "cpu_scale_down_target",
        "memory_scale_up_threshold", "memory_scale_down_threshold", "memory_scale_down_target",
        "scale_down_memory_p95_max_percent",
    ):
        if getattr(args, name, 1) > 100:
            parser.error(f"--{name.replace('_', '-')} must be at most 100 percent.")
    for name in ("resolution", "cpu_scale_up_resolution", "cpu_scale_down_resolution"):
        value = getattr(args, name, None)
        if value is not None and not re.fullmatch(r"[1-9][0-9]*[mh]", str(value)):
            parser.error(f"--{name.replace('_', '-')} must be a positive number followed by m or h.")
    if getattr(args, "apply", False):
        if not (getattr(args, "instance_id", None) or getattr(args, "compartment_id", None)):
            parser.error("Execution requires an explicit --instance-id or --compartment-id.")
        if not getattr(args, "acknowledge_reboot", False):
            parser.error("Execution requires --acknowledge-reboot for the planned interruption.")


def payload_exit_code(payload: Dict[str, Any]) -> int:
    if payload.get("scope_complete") is False:
        return 1
    if payload.get("validation_errors") or payload.get("error") or payload.get("ok") is False:
        return 1
    if payload.get("verification", {}).get("ok") is False:
        return 1
    if any(item.get("status") == "FAILED" or item.get("resize", {}).get("validation_errors") for item in payload.get("execution_results", [])):
        return 1
    if any(row.get("error") for row in payload.get("rows", [])):
        return 1
    return 0


def main() -> None:
    parser = build_parser()
    argv_tokens = sys.argv[1:]
    if not argv_tokens:
        args = parser.parse_args(["interactive"])
    elif has_explicit_command(argv_tokens):
        args = parser.parse_args()
    else:
        args = parser.parse_args(interactive_argv(argv_tokens))
    configure_logging(args.log_level)
    validate_arguments(parser, args)

    if args.command == "interactive":
        interactive_main(None, None, args)
        return
    ctx = OCIContext(profile=args.profile, config_file=args.config_file, region=args.region, auth=args.auth)
    planner = Planner(ctx)
    if args.command == "discover":
        payload = planner.inventory.discover(compartment_id=args.compartment_id, instance_id=args.instance_id)
    elif args.command == "metrics":
        ensure_instance_id(ctx, args)
        payload = planner.metrics.run(
            instance_id=args.instance_id,
            hours=args.hours,
            resolution=args.resolution,
            include_datapoints=args.include_datapoints,
        )
    elif args.command == "recommend":
        payload = planner.recommend(args)
    elif args.command == "scan":
        payload = planner.scan(args)
    elif args.command == "apply":
        payload = planner.apply(args)
    elif args.command == "pricing-debug":
        custom_filters = parse_filter_args(args.filter)
        has_custom_resource_id = any(key == "resourceId" for key, _ in custom_filters)
        require_instance_id = (not args.no_default_filters) and not has_custom_resource_id
        if require_instance_id and not args.instance_id:
            ensure_instance_id(ctx, args)
        payload = UsageCostEstimator(ctx).debug_request(
            instance_id=args.instance_id,
            lookback_days=args.lookback_days,
            group_by=args.group_by,
            query_type=args.query_type,
            granularity=args.granularity,
            use_default_filters=not args.no_default_filters,
            custom_filters=custom_filters,
        )
    elif args.command == "resize":
        ensure_instance_id(ctx, args)
        payload = planner.resize.run(
            instance_id=args.instance_id,
            ocpus=args.ocpus,
            memory_gb=args.memory_gb,
            apply=args.apply,
            acknowledge_reboot=args.acknowledge_reboot,
        )
        if args.apply and args.wait and payload.get("api_call_submitted") and not payload.get("validation_errors"):
            payload["verification"] = planner.verify.run(
                instance_id=args.instance_id,
                expected_ocpus=payload.get("requested_shape_config", {}).get("ocpus"),
                expected_memory_gb=payload.get("requested_shape_config", {}).get("memory_gb"),
                timeout_seconds=args.timeout_seconds,
                poll_seconds=args.poll_seconds,
            )
    elif args.command == "verify":
        ensure_instance_id(ctx, args)
        payload = planner.verify.run(
            instance_id=args.instance_id,
            expected_ocpus=args.expected_ocpus,
            expected_memory_gb=args.expected_memory_gb,
            timeout_seconds=args.timeout_seconds,
            poll_seconds=args.poll_seconds,
        )
    elif args.command == "run":
        payload = planner.run(args)
    else:  # pragma: no cover - argparse enforces command choices
        parser.error("Unknown command")
        return

    if args.output == "json":
        json_print(payload)
    else:
        pretty_print(payload, args.command, developer_details=bool(getattr(args, "developer_details", False)))
    raise SystemExit(payload_exit_code(payload))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Interrupted. Check OCI for any requests already submitted.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        # Exception text from SDK configuration may contain sensitive profile values.
        print(f"Right-Sizer failed ({type(exc).__name__}). Check authentication, IAM, region and connectivity.", file=sys.stderr)
        raise SystemExit(1)
