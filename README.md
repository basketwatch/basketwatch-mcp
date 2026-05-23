# BasketWatch MCP Server

A [Model Context Protocol](https://modelcontextprotocol.io/) server that
exposes the **BasketWatch Irish grocery data** API as tools your AI agent
can call directly. Works with **Claude Desktop**, **Continue.dev**,
**Cursor**, and any other MCP-compatible client.

> ~47,000 Irish supermarket SKUs across Aldi, Tesco, SuperValu and Dunnes
> Stores. Refreshed every Friday at 02:00 UTC. Now queryable by Claude.

## What your agent can do

Once installed, your agent has 8 grocery-aware tools:

| Tool | What it does |
|---|---|
| `status` | Cross-retailer health snapshot — SKU counts, last-scrape dates, run status |
| `search_products` | Find products by name in one or all stores |
| `compare_price_across_stores` | One-call comparison across all 4 supermarkets |
| `get_promotions` | List products currently on offer at a given store |
| `recent_price_changes` | Week-over-week price movements |
| `newly_added_products` | New listings in a configurable lookback window |
| `removed_products` | Delistings / range cuts |
| `list_products` | Paginated catalogue dump for walking the full assortment |

Example questions your agent can answer:

- *"Where can I buy Heinz Baked Beans cheapest this week?"*
- *"Build me a €40 weekly grocery list across the 4 supermarkets."*
- *"Which products got cheaper at Tesco this week?"*
- *"What promotions are running on chocolate at Dunnes right now?"*
- *"Track the price of Brennans bread for the next 12 weeks."*

## Install

```bash
pip install basketwatch-mcp
```

Or from source:

```bash
git clone https://github.com/basketwatch/basketwatch-mcp.git
cd basketwatch-mcp
pip install -e .
```

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `BASKETWATCH_API_BASE` | (required) | Origin of the BasketWatch API (e.g. `https://basketwatch.fly.dev`) |
| `BASKETWATCH_API_KEY` | (none) | API key for higher-tier usage. Free-tier limits apply without one |
| `BASKETWATCH_MCP_DAILY_LIMIT` | `100` | Free-tier cap — requests per UTC day |
| `BASKETWATCH_MCP_RATE_PER_MIN` | `20` | Sliding-window rate limit — requests per 60 seconds |

The daily counter is persisted to `~/.basketwatch-mcp/usage.json` so it
survives Claude Desktop restarts and resets at 00:00 UTC.

When either limit is hit, every tool returns:

```json
{
  "error": "Free-tier daily limit reached: 100 requests per day. Resets at 00:00 UTC...",
  "limit_hit": true,
  "hint": "Email basketwatchireland@gmail.com to get an API key with higher / unlimited usage."
}
```

The LLM surfacing this to the user is the lead funnel — heavy users
self-identify and email for a paid subscription.

Get an API key: email `basketwatchireland@gmail.com` for a direct
subscription, or use [BasketWatch on RapidAPI](https://rapidapi.com/) /
[BasketWatch on Apify](https://apify.com/) for usage-based billing.

## Hook it up to Claude Desktop

Edit your Claude Desktop config file:

- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`

Add a `basketwatch` entry under `mcpServers`:

```json
{
  "mcpServers": {
    "basketwatch": {
      "command": "basketwatch-mcp",
      "env": {
        "BASKETWATCH_API_BASE": "https://basketwatch.fly.dev",
        "BASKETWATCH_API_KEY":  "your-api-key-here"
      }
    }
  }
}
```

Restart Claude Desktop. You'll see "basketwatch" listed as an available
MCP server in Claude's MCP indicator. Ask Claude *"What's the cheapest
1L of milk in Ireland?"* — it'll use the tools automatically.

## Hook it up to Continue.dev / Cursor

These clients use the same MCP protocol over stdio. Add a server block to
your `~/.continue/config.json` (Continue) or equivalent Cursor config:

```json
{
  "mcpServers": {
    "basketwatch": {
      "command": "basketwatch-mcp",
      "env": {
        "BASKETWATCH_API_BASE": "https://basketwatch.fly.dev",
        "BASKETWATCH_API_KEY":  "your-api-key-here"
      }
    }
  }
}
```

## Local development

```bash
git clone https://github.com/basketwatch/basketwatch-mcp.git
cd basketwatch-mcp
python -m venv .venv && source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -e .

export BASKETWATCH_API_BASE="https://basketwatch.fly.dev"
export BASKETWATCH_API_KEY="your-key"

# Run the server directly to test (it speaks MCP over stdio — type JSON-RPC
# requests at it or use `mcp dev` from the MCP SDK for interactive testing).
python basketwatch_mcp.py
```

## Data freshness

- All four retailers are scraped every **Friday at 02:00 UTC**.
- Each tool response includes a `scrape_date` field per row so the agent
  knows exactly when each price was captured.
- Higher-cadence pulls (daily / twice-weekly) are available with a direct
  subscription — email `basketwatchireland@gmail.com`.

## Not affiliated

BasketWatch is not affiliated with Aldi Ireland, Tesco Ireland, Musgrave
SuperValu or Dunnes Stores. Data is collected from publicly available
sources for lawful market research and price comparison purposes.

## License

MIT.
