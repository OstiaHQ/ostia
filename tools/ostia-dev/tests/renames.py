"""Every old pixi task and tool path with its ostia-dev replacement (RFC-0005 §2.2, §2.3).

The single source for the CHANGELOG check and the scan that keeps old names from coming
back. Each new command is the argv after `ostia-dev`.
"""

import re

CHECK_CUDA = [
    "remote",
    "container",
    "--env",
    "cuda-12",
    "--env",
    "cuda-13",
    "--suite",
    "cuda-compile",
]

TASKS: dict[str, list[str]] = {
    "build": ["build"],
    "test": ["test"],
    "test-cpp": ["test", "cpp"],
    "test-py": ["test", "py"],
    "test-rebuild": ["test", "rebuild"],
    "test-multiprocess": ["test", "-L", "multiprocess"],
    "test-preset": ["test", "--preset"],
    "test-levels": ["test", "--levels"],
    "sanitize-asan": ["test", "--sanitize", "asan-ubsan"],
    "sanitize-tsan": ["test", "--sanitize", "tsan"],
    "install-native": ["py-dev"],
    "py-dev": ["py-dev"],
    "lint": ["lint"],
    "fmt": ["fmt"],
    "check": ["check"],
    "check-graph": ["check", "graph"],
    "check-macros": ["check", "macros"],
    "tidy": ["check", "tidy"],
    "doctor": ["doctor"],
    "clean": ["clean"],
    "hooks": ["hooks"],
    "docs-index": ["docs", "index"],
    "docs-as-test": ["check", "docs-as-test"],
    "check-cuda": CHECK_CUDA,
    "bench": ["bench"],
    "compare": ["bench", "compare"],
}

# Old script → (new command, module or file it moved to under tools/ostia-dev/src/).
# lint.py's mode argument (lint|fmt) is itself the new verb.
SCRIPTS: dict[str, tuple[list[str], str]] = {
    "tools/ci/lint.py": ([], "ostia_dev/ci/lint.py"),
    "tools/ci/check_layering.py": (["check", "layering"], "ostia_dev/ci/check_layering.py"),
    "tools/ci/check_cpm_pins.py": (["check", "cpm-pins"], "ostia_dev/ci/check_cpm_pins.py"),
    "tools/ci/check_exports.py": (["check", "exports"], "ostia_dev/ci/check_exports.py"),
    "tools/ci/check_graph.py": (["check", "graph"], "ostia_dev/ci/check_graph.py"),
    "tools/ci/check_telemetry_macros.py": (
        ["check", "macros"],
        "ostia_dev/ci/check_telemetry_macros.py",
    ),
    "tools/ci/check_comments.py": (["check", "comments"], "ostia_dev/ci/check_comments.py"),
    "tools/ci/docs_as_test.py": (["check", "docs-as-test"], "ostia_dev/ci/docs_as_test.py"),
    "tools/ci/_contract.py": ([], "ostia_dev/contract.py"),
    "tools/dev/py_dev.py": (["py-dev"], "ostia_dev/dev/py_dev.py"),
    "tools/dev/doctor.py": (["doctor"], "ostia_dev/dev/doctor.py"),
    "tools/dev/clean.py": (["clean"], "ostia_dev/dev/clean.py"),
    "tools/dev/_paths.py": ([], "ostia_dev/paths.py"),
    "tools/dev/check_cuda.py": (CHECK_CUDA, ""),
    "tools/docs/gen_index.py": (["docs", "index"], "ostia_dev/docs/gen_index.py"),
    "tools/docs/render-figures.js": (["docs", "figures"], "ostia_dev/docs/render-figures.js"),
    "tools/bench/ostia_bench.py": (["bench"], "ostia_dev/bench/ostia_bench.py"),
    "tools/bench/compare.py": (["bench", "compare"], "ostia_dev/bench/compare.py"),
    "tools/bench/overhead.py": (["bench", "overhead"], "ostia_dev/bench/overhead.py"),
    "tools/bench/oracles.py": (["bench", "oracles"], "ostia_dev/bench/oracles.py"),
    "tools/bench/bounds.py": (["bench", "bounds"], "ostia_dev/bench/bounds.py"),
    "tools/bench/capabilities.py": (["bench", "capabilities"], "ostia_dev/bench/capabilities.py"),
    "tools/bench/evidence.py": (["bench", "evidence"], "ostia_dev/bench/evidence.py"),
    "tools/bench/schema.py": ([], "ostia_dev/bench/schema.py"),
    "tools/bench/schema-v1.json": ([], "ostia_dev/bench/schema-v1.json"),
}


def task_pattern(task: str) -> re.Pattern:
    """`pixi run [-e ENV] [--frozen] <task>`, not followed by more of a task name."""
    return re.compile(r"pixi run((?: -e \S+| --frozen)*) " + re.escape(task) + r"(?![\w-])")
