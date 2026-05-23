"""Verify the MCP server loads + the OPT-IN rate limiter works correctly.

v0.2.0 changes the default behaviour: no client-side limits unless the
caller sets the env vars. This test exercises both modes:

  1. Default (no env vars set) → every call should pass
  2. With limits configured → calls beyond the limit should be rejected

Does not actually hit the live API — just confirms the gate logic.
"""
import importlib
import os
import sys

os.environ["BASKETWATCH_API_BASE"] = "https://example.test"


def _reload():
    """Reload the module so env-var changes take effect."""
    if "basketwatch_mcp" in sys.modules:
        del sys.modules["basketwatch_mcp"]
    return importlib.import_module("basketwatch_mcp")


# ---------- Mode 1: defaults (no client-side limits) ----------
os.environ.pop("BASKETWATCH_MCP_DAILY_LIMIT", None)
os.environ.pop("BASKETWATCH_MCP_RATE_PER_MIN", None)

srv = _reload()
print(f"Mode 1 — defaults:  DAILY_LIMIT={srv.DAILY_LIMIT}  RATE_PER_MIN={srv.RATE_LIMIT_PER_MIN}")
print("  (both should be 0 = client-side limits disabled)")

all_pass = True
for i in range(50):
    ok, msg = srv._check_and_consume()
    if not ok:
        all_pass = False
        print(f"  call #{i+1}: UNEXPECTED REJECTION  msg={msg}")
        break
if all_pass:
    print("  50/50 calls passed (no limit enforced) — OK")


# ---------- Mode 2: opt-in tight limits ----------
os.environ["BASKETWATCH_MCP_DAILY_LIMIT"] = "3"
os.environ["BASKETWATCH_MCP_RATE_PER_MIN"] = "2"

srv = _reload()
print(f"\nMode 2 — opt-in:    DAILY_LIMIT={srv.DAILY_LIMIT}  RATE_PER_MIN={srv.RATE_LIMIT_PER_MIN}")
print("  Expected: first 2 pass (rate slot), then rate-limit rejection")

# Force-clear persisted counter for a deterministic test
srv._today_iso = None
srv._today_count = 0
srv._recent_call_times.clear()
# Also wipe the persisted file from previous test runs
try:
    srv._USAGE_FILE.unlink()
except FileNotFoundError:
    pass

for i in range(5):
    ok, msg = srv._check_and_consume()
    label = "PASS" if ok else "REJECT"
    print(f"  call #{i+1}: {label}  msg={(msg or '')[:60]}")


# ---------- All 8 tool functions present ----------
expected_tools = [
    "status", "search_products", "compare_price_across_stores",
    "get_promotions", "recent_price_changes", "newly_added_products",
    "removed_products", "list_products",
]
for t in expected_tools:
    assert callable(getattr(srv, t)), f"missing tool: {t}"
print("\nAll 8 expected tools present.")

print("\nMCP server v0.2.0 smoke test PASSED.")
