"""ostia-fabric Python bindings. See RFC-0001 §3.5."""

try:
    from ._native import telemetry_build_level
except ImportError as e:  # error-message contract (RFC-0001, Failure handling)
    raise ImportError(
        f"error: ostia.fabric native extension failed to load: {e}\n"
        "  fix: pixi run ostia-dev py-dev\n"
        "  see: RFC-0001 §3.5"
    ) from e

__all__ = ["telemetry_build_level"]
