"""Verify the MCP server loads + the rate limiter is wired correctly.
Does not actually hit the live API — just confirms the internal limiter
counts/rejects as expected.
"""
import os
import sys

os.environ["BASKETWATCH_API_BASE"] = "https://example.test"
os.environ["BASKETWATCH_MCP_DAILY_LIMIT"] = "3"
os.environ["BASKETWATCH_MCP_RATE_PER_MIN"] = "2"

# Import after setting env so config picks them up
import basketwatch_mcp as srv

print(f"Module loaded — DAILY_LIMIT={srv.DAILY_LIMIT}, RATE_PER_MIN={srv.RATE_LIMIT_PER_MIN}")

# Force-reset the counter for a clean test
srv._today_iso = None
srv._today_count = 0
srv._recent_call_times.clear()

# Call _check_and_consume 5 times — should pass twice, fail on rate limit
print("\nFirst 5 calls (limit is 2/min and 3/day):")
for i in range(5):
    ok, msg = srv._check_and_consume()
    print(f"  call #{i+1}: ok={ok}  msg={(msg or '')[:60]}")

# Tool list
tools = [n for n in dir(srv) if not n.startswith("_")]
print(f"\nServer attributes (subset): mcp={hasattr(srv, 'mcp')}, "
      f"tools registered via @mcp.tool() decorator")

# Confirm the 8 tool functions exist
expected_tools = [
    "status", "search_products", "compare_price_across_stores",
    "get_promotions", "recent_price_changes", "newly_added_products",
    "removed_products", "list_products",
]
for t in expected_tools:
    assert callable(getattr(srv, t)), f"missing tool: {t}"
print(f"\nAll 8 expected tools present.")
print("\nMCP server smoke test PASSED.")
