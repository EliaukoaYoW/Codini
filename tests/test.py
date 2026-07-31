import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codini.evaluator import run_harness_regression_v2  # noqa: E402
from tests.metrics_v2 import (  # noqa: E402
    run_context_ablation_v2,
    run_memory_ablation_v2,
    run_recovery_ablation_v2,
    write_benchmark_core_report,
)
from tests.metrics_v3 import (  # noqa: E402
    run_context_allocation_ablation_v3,
    run_memory_mechanism_ablation_v3,
)

RUNNERS = {
    "harness-regression-v2": run_harness_regression_v2,
    "context-ablation-v2": run_context_ablation_v2,
    "memory-ablation-v2": run_memory_ablation_v2,
    "context-allocation-v3": run_context_allocation_ablation_v3,
    "memory-mechanism-v3": run_memory_mechanism_ablation_v3,
    "recovery-ablation-v2": run_recovery_ablation_v2,
    "benchmark-core-report": write_benchmark_core_report,
}
ACTIVE_EXPERIMENT = "memory-mechanism-v3"


if __name__ == "__main__":
    RUNNERS[ACTIVE_EXPERIMENT]()
