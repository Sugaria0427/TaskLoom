"""Build immutable timing plans from the personal profile's demonstration settings."""

from taskloom.contracts.models import DemoSettings, DemoTiming
from taskloom.policies.demo_timing import mapped_seconds


def timing_plan(settings: DemoSettings | None, name: str, amount: int = 0) -> DemoTiming | None:
    if settings is None or not settings.enabled:
        return None
    model = name == "model"
    if not (settings.models if model else settings.tools):
        return None
    workload = settings.model_workload if model else name not in ("list_files", "run_command")
    reference = 1000 if model else 22 * 1024
    baseline = (
        settings.model_reference_ms
        if model
        else (settings.text_reference_ms if workload else settings.fixed_tool_ms)
    )
    return DemoTiming(
        kind="workload" if workload else "fixed",
        amount=amount if workload else 1,
        unit="tokens" if model and workload else "bytes" if workload else "call",
        reference_amount=reference if workload else 1,
        reference_ms=baseline,
        strength=settings.strength,
        exponent=settings.exponent,
        target_seconds=mapped_seconds(
            baseline,
            settings.strength,
            settings.exponent,
            amount,
            reference,
            workload=workload,
            cap_seconds=settings.cap_seconds,
        ),
    )
