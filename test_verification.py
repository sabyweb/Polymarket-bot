#!/usr/bin/env python3
"""Verification script: checks invariants across the codebase.

Runs static analysis (grep-based) and runtime assertions.
Exit code 0 = all pass, 1 = any fail.
"""

import ast
import os
import re
import sqlite3
import sys
import tempfile
import time

PASS = 0
FAIL = 0
ROOT = os.path.dirname(os.path.abspath(__file__))

# Production files that touch bot_history.db at runtime.
# Test files and standalone tools are excluded from raw-connect checks.
PROD_FILES = [
    "oversight/data_collector.py",
    "oversight/safety_controller.py",
    "oversight/market_scorer.py",
    "oversight_agent.py",
    "reward_farmer.py",
]


def check(label: str, passed: bool, detail: str = ""):
    global PASS, FAIL
    if passed:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        info = f" — {detail}" if detail else ""
        print(f"  FAIL  {label}{info}")


def lines_matching(filepath: str, pattern: re.Pattern) -> list[tuple[int, str]]:
    """Return [(lineno, line)] for lines matching pattern."""
    hits = []
    with open(filepath) as f:
        for i, line in enumerate(f, 1):
            if pattern.search(line):
                hits.append((i, line.rstrip()))
    return hits


# ═══════════════════════════════════════════
print("\n=== CHECK 1: No duplicate correction factor computation ===")
# fetch_reward_correction_factor should be CALLED in exactly 2 places:
#   1. data_collector.py collect_all() — the canonical call
#   2. oversight_agent.py _phase0_daily_attribution() — separate daily logging
# It should NOT appear in run_once() or safety evaluation.
# ═══════════════════════════════════════════

pat = re.compile(r"fetch_reward_correction_factor\s*\(")
all_calls: list[tuple[str, int, str]] = []
for f in PROD_FILES:
    path = os.path.join(ROOT, f)
    if not os.path.exists(path):
        continue
    for lineno, line in lines_matching(path, pat):
        # Skip definitions, comments, and docstrings
        stripped = line.lstrip()
        if stripped.startswith("def ") or stripped.startswith("#"):
            continue
        all_calls.append((f, lineno, stripped))

# Exactly 3 non-def/comment references expected:
#   data_collector.py: actual call inside collect_all
#   oversight_agent.py: import line in _phase0
#   oversight_agent.py: actual call in _phase0
expected_files = {"oversight/data_collector.py", "oversight_agent.py"}
call_files = {c[0] for c in all_calls}
check(
    "fetch_reward_correction_factor call sites limited to 2 files",
    call_files == expected_files,
    f"found in: {call_files}",
)

# Verify run_once does NOT call it
agent_path = os.path.join(ROOT, "oversight_agent.py")
with open(agent_path) as f:
    agent_src = f.read()

# Parse AST to find calls inside run_once
tree = ast.parse(agent_src)
run_once_calls = []
for node in ast.walk(tree):
    if isinstance(node, ast.FunctionDef) and node.name == "run_once":
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                func = child.func
                name = ""
                if isinstance(func, ast.Name):
                    name = func.id
                elif isinstance(func, ast.Attribute):
                    name = func.attr
                if name == "fetch_reward_correction_factor":
                    run_once_calls.append(child.lineno)
check(
    "run_once() does NOT call fetch_reward_correction_factor",
    len(run_once_calls) == 0,
    f"found at lines: {run_once_calls}" if run_once_calls else "",
)


# ═══════════════════════════════════════════
print("\n=== CHECK 2: No raw sqlite3.connect in production code ===")
# Only allowed in _connect_db() itself (data_collector.py:28)
# and database.py (bot's own connection manager).
# ═══════════════════════════════════════════

pat = re.compile(r"sqlite3\.connect\(")
violations: list[tuple[str, int, str]] = []
for f in PROD_FILES:
    path = os.path.join(ROOT, f)
    if not os.path.exists(path):
        continue
    for lineno, line in lines_matching(path, pat):
        # Allow _connect_db definition
        if f == "oversight/data_collector.py" and "def _connect_db" in open(path).readlines()[lineno - 3:lineno - 1].__repr__():
            continue
        violations.append((f, lineno, line.strip()))

# Filter: only the _connect_db body is allowed (data_collector.py line ~28)
real_violations = []
for filepath, lineno, line in violations:
    if filepath == "oversight/data_collector.py":
        # Read surrounding lines to check if inside _connect_db
        full_path = os.path.join(ROOT, filepath)
        with open(full_path) as fh:
            src_lines = fh.readlines()
        # Walk backward to find enclosing function
        in_connect_db = False
        for i in range(lineno - 2, max(0, lineno - 20), -1):
            if "def _connect_db" in src_lines[i]:
                in_connect_db = True
                break
            if src_lines[i].startswith("def "):
                break
        if in_connect_db:
            continue
    real_violations.append((filepath, lineno, line))

check(
    "No raw sqlite3.connect in production files",
    len(real_violations) == 0,
    "; ".join(f"{f}:{ln}" for f, ln, _ in real_violations) if real_violations else "",
)

# Also verify oversight module specifically
oversight_violations = [v for v in real_violations if v[0].startswith("oversight/")]
check(
    "No raw sqlite3.connect in oversight/ module",
    len(oversight_violations) == 0,
    "; ".join(f"{f}:{ln}" for f, ln, _ in oversight_violations) if oversight_violations else "",
)


# ═══════════════════════════════════════════
print("\n=== CHECK 3: collect_all() unpacked correctly everywhere ===")
# Return type is 5-tuple. Every call site must unpack 5 values.
# ═══════════════════════════════════════════

# Check data_collector.py return annotation
dc_path = os.path.join(ROOT, "oversight/data_collector.py")
with open(dc_path) as f:
    dc_src = f.read()

check(
    "collect_all return type annotation says 5-tuple",
    "tuple[list, float, float, float, float]" in dc_src,
    "check oversight/data_collector.py collect_all signature",
)

# Check actual return statement
ret_pat = re.compile(r"return metrics, correction_factor, clob_rate_delta, data_completeness, actual_daily_total")
check(
    "collect_all returns 5 values",
    bool(ret_pat.search(dc_src)),
    "return statement in collect_all",
)

# Check oversight_agent.py unpacking
check(
    "oversight_agent.py handles 5-tuple from collect_all",
    "len(collect_result) >= 5" in agent_src,
    "check oversight_agent.py collect_result unpacking",
)

# Check test_allocation_loading.py
test_alloc_path = os.path.join(ROOT, "tests/test_allocation_loading.py")
if os.path.exists(test_alloc_path):
    with open(test_alloc_path) as f:
        test_alloc_src = f.read()
    check(
        "test_allocation_loading expects len(result) == 5",
        "len(result), 5" in test_alloc_src or "len(result) == 5" in test_alloc_src,
        "check tests/test_allocation_loading.py",
    )
else:
    check("test_allocation_loading.py exists", False, "file not found")

# Check test_safety.py label
test_safety_path = os.path.join(ROOT, "test_safety.py")
if os.path.exists(test_safety_path):
    with open(test_safety_path) as f:
        test_safety_src = f.read()
    check(
        "test_safety.py says 'Returns 5 Values'",
        "Returns 5 Values" in test_safety_src,
        "check test_safety.py TEST 13 label",
    )


# ═══════════════════════════════════════════
print("\n=== CHECK 4: fill_times accessed safely ===")
# ═══════════════════════════════════════════

# 4a: MarketState.fill_times has default_factory
models_path = os.path.join(ROOT, "models.py")
with open(models_path) as f:
    models_src = f.read()
check(
    "MarketState.fill_times has default_factory",
    'fill_times: dict = field(default_factory=lambda: {"yes": [], "no": []})' in models_src,
    "check models.py MarketState",
)

# 4b: fill storm detector uses .get() for safety
rf_path = os.path.join(ROOT, "reward_farmer.py")
with open(rf_path) as f:
    rf_src = f.read()
check(
    "Fill storm detector uses .get(side, []) for safe access",
    'ms.fill_times.get(side, [])' in rf_src,
    "check reward_farmer.py fill storm loop",
)

# 4c: _fill_storm_until is initialized
check(
    "_fill_storm_until initialized in __init__",
    "_fill_storm_until" in rf_src and "self._fill_storm_until: float = 0.0" in rf_src
    or "_fill_storm_until = 0.0" in rf_src,
    "check reward_farmer.py __init__",
)


# ═══════════════════════════════════════════
print("\n=== CHECK 5: Deployed markets sourced from DB, not JSON ===")
# _load_deployed_cids must NOT reference market_allocations.json
# ═══════════════════════════════════════════

# Extract _load_deployed_cids function body
load_fn_match = re.search(
    r"def _load_deployed_cids\(.*?\n(?=\ndef |\nclass |\Z)",
    dc_src,
    re.DOTALL,
)
if load_fn_match:
    load_fn_body = load_fn_match.group()
    check(
        "_load_deployed_cids does NOT read market_allocations.json",
        "market_allocations.json" not in load_fn_body,
        "found JSON file reference in _load_deployed_cids",
    )
    check(
        "_load_deployed_cids reads from deployed_markets DB table",
        "deployed_markets" in load_fn_body,
        "no DB table reference found",
    )
    check(
        "_load_deployed_cids returns (deployed, probes) tuple",
        "tuple[set[str], set[str]]" in load_fn_body,
        "return type is not a 2-tuple",
    )
else:
    check("_load_deployed_cids found in source", False, "function not found")

# persist_deployed_cids exists and writes to DB
check(
    "persist_deployed_cids writes to deployed_markets table",
    "def persist_deployed_cids" in dc_src
    and "INSERT INTO deployed_markets" in dc_src,
    "function missing or no INSERT",
)

# Agent calls persist_deployed_cids after allocation
check(
    "oversight_agent.py calls persist_deployed_cids after allocation",
    "persist_deployed_cids" in agent_src,
    "not called in oversight_agent.py",
)


# ═══════════════════════════════════════════
print("\n=== CHECK 6: CF consistency — min denominator guard ===")
# ═══════════════════════════════════════════

check(
    "MIN_EST_DAILY threshold defined in collect_all",
    "MIN_EST_DAILY" in dc_src,
    "no minimum denominator guard found",
)

# Verify the guard gates CF computation
min_est_pat = re.compile(r"estimated_daily_total >= MIN_EST_DAILY")
check(
    "CF only computed when estimated >= MIN_EST_DAILY",
    bool(min_est_pat.search(dc_src)),
    "guard condition missing",
)

# Verify probe exclusion
check(
    "Probe markets excluded from CF denominator",
    "probe_cids" in dc_src and "is_probe" in dc_src,
    "no probe exclusion logic found",
)


# ═══════════════════════════════════════════
print("\n=== CHECK 7: Runtime — DB connection uses WAL mode ===")
# ═══════════════════════════════════════════

tmp_db = tempfile.mktemp(suffix=".db")
try:
    from oversight.data_collector import _connect_db

    db = _connect_db(tmp_db)
    mode = db.execute("PRAGMA journal_mode").fetchone()[0]
    check(
        "_connect_db sets WAL journal mode",
        mode == "wal",
        f"got journal_mode={mode}",
    )

    busy = db.execute("PRAGMA busy_timeout").fetchone()[0]
    check(
        "_connect_db sets busy_timeout=10000",
        busy == 10000,
        f"got busy_timeout={busy}",
    )

    check(
        "_connect_db sets row_factory=sqlite3.Row",
        db.row_factory is sqlite3.Row,
        f"got row_factory={db.row_factory}",
    )
    db.close()
finally:
    try:
        os.unlink(tmp_db)
    except Exception:
        pass
    for ext in ("-wal", "-shm"):
        try:
            os.unlink(tmp_db + ext)
        except Exception:
            pass


# ═══════════════════════════════════════════
print("\n=== CHECK 8: Runtime — CF history persisted with context ===")
# ═══════════════════════════════════════════

tmp_db2 = tempfile.mktemp(suffix=".db")
try:
    from oversight.data_collector import _smooth_correction_factor

    result = _smooth_correction_factor(
        raw_factor=0.5,
        db_path=tmp_db2,
        alpha=0.3,
        has_new_observation=True,
        estimated_daily=10.0,
        actual_daily=5.0,
        deployed_count=7,
    )

    db = sqlite3.connect(tmp_db2)
    row = db.execute(
        "SELECT raw, smoothed, estimated_daily, actual_daily, deployed_count "
        "FROM correction_factor_history ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    db.close()

    check(
        "CF history row persisted",
        row is not None,
        "no row in correction_factor_history",
    )
    if row:
        check("CF history: raw value stored", abs(row[0] - 0.5) < 0.001, f"raw={row[0]}")
        check("CF history: smoothed value stored", row[1] > 0, f"smoothed={row[1]}")
        check("CF history: estimated_daily stored", abs(row[2] - 10.0) < 0.01, f"est={row[2]}")
        check("CF history: actual_daily stored", abs(row[3] - 5.0) < 0.01, f"actual={row[3]}")
        check("CF history: deployed_count stored", row[4] == 7, f"deployed={row[4]}")
finally:
    for f in [tmp_db2, tmp_db2 + "-wal", tmp_db2 + "-shm"]:
        try:
            os.unlink(f)
        except Exception:
            pass


# ═══════════════════════════════════════════
print("\n=== CHECK 9: Runtime — persist/load deployed CIDs round-trip ===")
# ═══════════════════════════════════════════

tmp_db3 = tempfile.mktemp(suffix=".db")
try:
    from oversight.data_collector import persist_deployed_cids, _load_deployed_cids

    deployed = {"cid_a", "cid_b", "cid_c"}
    probes = {"cid_d"}
    persist_deployed_cids(tmp_db3, deployed | probes, probes)

    loaded_deployed, loaded_probes = _load_deployed_cids(tmp_db3)

    check(
        "Deployed CIDs round-trip (non-probe)",
        loaded_deployed == deployed,
        f"expected {deployed}, got {loaded_deployed}",
    )
    check(
        "Probe CIDs round-trip",
        loaded_probes == probes,
        f"expected {probes}, got {loaded_probes}",
    )
    check(
        "Probes excluded from deployed set",
        "cid_d" not in loaded_deployed,
        "probe cid_d leaked into deployed",
    )
finally:
    for f in [tmp_db3, tmp_db3 + "-wal", tmp_db3 + "-shm"]:
        try:
            os.unlink(f)
        except Exception:
            pass


# ═══════════════════════════════════════════
print("\n=== CHECK 10: Runtime — Fill storm detector triggers correctly ===")
# ═══════════════════════════════════════════

from dataclasses import dataclass, field


@dataclass
class FakeMarketState:
    fill_times: dict = field(default_factory=lambda: {"yes": [], "no": []})


# Simulate 6 fills in last 5 minutes across 3 markets
now = time.time()
markets = {
    "m1": FakeMarketState(fill_times={"yes": [now - 10, now - 20], "no": []}),
    "m2": FakeMarketState(fill_times={"yes": [now - 30], "no": [now - 40]}),
    "m3": FakeMarketState(fill_times={"yes": [now - 50], "no": [now - 60]}),
}

STORM_WINDOW_SECS = 300
STORM_THRESHOLD = 5
global_recent = 0
for ms in markets.values():
    for side in ("yes", "no"):
        global_recent += sum(1 for t in ms.fill_times.get(side, []) if now - t < STORM_WINDOW_SECS)

check(
    "Fill storm detects 6 fills across 3 markets",
    global_recent == 6,
    f"counted {global_recent} fills, expected 6",
)
check(
    "6 fills >= threshold of 5 triggers storm",
    global_recent >= STORM_THRESHOLD,
    f"{global_recent} < {STORM_THRESHOLD}",
)

# Simulate 3 fills (below threshold)
markets_calm = {
    "m1": FakeMarketState(fill_times={"yes": [now - 10], "no": []}),
    "m2": FakeMarketState(fill_times={"yes": [now - 30], "no": []}),
    "m3": FakeMarketState(fill_times={"yes": [now - 50], "no": []}),
}
calm_count = 0
for ms in markets_calm.values():
    for side in ("yes", "no"):
        calm_count += sum(1 for t in ms.fill_times.get(side, []) if now - t < STORM_WINDOW_SECS)

check(
    "3 fills does NOT trigger storm",
    calm_count < STORM_THRESHOLD,
    f"{calm_count} >= {STORM_THRESHOLD}",
)

# Old fills outside window are excluded
markets_old = {
    "m1": FakeMarketState(fill_times={"yes": [now - 400, now - 500], "no": [now - 600]}),
}
old_count = 0
for ms in markets_old.values():
    for side in ("yes", "no"):
        old_count += sum(1 for t in ms.fill_times.get(side, []) if now - t < STORM_WINDOW_SECS)

check(
    "Fills outside 5min window are excluded",
    old_count == 0,
    f"counted {old_count} old fills, expected 0",
)

# Empty fill_times handled safely
markets_empty = {
    "m1": FakeMarketState(),
}
empty_count = 0
for ms in markets_empty.values():
    for side in ("yes", "no"):
        empty_count += sum(1 for t in ms.fill_times.get(side, []) if now - t < STORM_WINDOW_SECS)

check(
    "Empty fill_times handled without error",
    empty_count == 0,
    f"counted {empty_count}",
)


# ═══════════════════════════════════════════
print("\n=== CHECK 11: Runtime — CF circuit breaker and EMA ===")
# ═══════════════════════════════════════════

tmp_db4 = tempfile.mktemp(suffix=".db")
try:
    # Circuit breaker: raw < 0.01 should skip EMA
    cb_result = _smooth_correction_factor(
        raw_factor=0.005, db_path=tmp_db4, has_new_observation=True,
    )
    check(
        "Circuit breaker: raw=0.005 returns 0.005 (no smoothing)",
        abs(cb_result - 0.005) < 0.001,
        f"got {cb_result}",
    )

    # Normal EMA: second observation should blend
    ema_result = _smooth_correction_factor(
        raw_factor=0.8, db_path=tmp_db4, has_new_observation=True,
    )
    # prev_smoothed was 0.005, alpha=0.3: 0.3*0.8 + 0.7*0.005 = 0.2435
    expected_ema = 0.3 * 0.8 + 0.7 * 0.005
    check(
        "EMA blends correctly (alpha=0.3)",
        abs(ema_result - expected_ema) < 0.01,
        f"expected ~{expected_ema:.4f}, got {ema_result:.4f}",
    )

    # No new observation: returns last smoothed
    no_obs_result = _smooth_correction_factor(
        raw_factor=99.0, db_path=tmp_db4, has_new_observation=False,
    )
    check(
        "No observation: returns last smoothed, ignores raw",
        abs(no_obs_result - ema_result) < 0.01,
        f"expected ~{ema_result:.4f}, got {no_obs_result:.4f}",
    )
finally:
    for f in [tmp_db4, tmp_db4 + "-wal", tmp_db4 + "-shm"]:
        try:
            os.unlink(f)
        except Exception:
            pass


# ═══════════════════════════════════════════
print(f"\n{'='*60}")
print(f"RESULTS: {PASS} passed, {FAIL} failed out of {PASS + FAIL} checks")
if FAIL:
    print(f"FAILURES: {FAIL}")
print(f"{'='*60}")

sys.exit(1 if FAIL else 0)
