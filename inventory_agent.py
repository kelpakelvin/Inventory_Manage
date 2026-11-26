"""
Inventory management assistant built on LangChain v1.

A SQLite database holds products and a stock movement log. An agent built
with create_agent gets a set of tools over that database, so you can manage
inventory in plain English:

    > add 40 USB-C cables at $6.50 each, reorder when below 10
    > we sold 12 USB-C cables today
    > what's running low?

Run:  python inventory_agent.py
"""

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime

from langchain.agents import create_agent
from langchain.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

DB_PATH = os.getenv("INVENTORY_DB", "inventory.db")
MODEL = os.getenv("INVENTORY_MODEL", "anthropic:claude-sonnet-4-6")


# ---------------------------------------------------------------------------
# Database layer (plain Python, testable without an LLM)
# ---------------------------------------------------------------------------

@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS items (
                sku           TEXT PRIMARY KEY,
                name          TEXT NOT NULL,
                category      TEXT DEFAULT 'general',
                quantity      INTEGER NOT NULL DEFAULT 0 CHECK (quantity >= 0),
                unit_price    REAL NOT NULL DEFAULT 0,
                reorder_level INTEGER NOT NULL DEFAULT 0,
                location      TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS movements (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                sku       TEXT NOT NULL REFERENCES items(sku),
                change    INTEGER NOT NULL,
                reason    TEXT NOT NULL,
                timestamp TEXT NOT NULL
            );
            """
        )


def _fmt(row: sqlite3.Row) -> str:
    flag = "  LOW STOCK" if row["quantity"] <= row["reorder_level"] else ""
    loc = f", location {row['location']}" if row["location"] else ""
    return (
        f"{row['sku']} | {row['name']} ({row['category']}) | qty {row['quantity']} "
        f"| ${row['unit_price']:.2f} | reorder at {row['reorder_level']}{loc}{flag}"
    )


def _log(conn, sku: str, change: int, reason: str) -> None:
    conn.execute(
        "INSERT INTO movements (sku, change, reason, timestamp) VALUES (?, ?, ?, ?)",
        (sku, change, reason, datetime.now().isoformat(timespec="seconds")),
    )


# ---------------------------------------------------------------------------
# Tools exposed to the agent
# ---------------------------------------------------------------------------

@tool
def add_item(
    sku: str,
    name: str,
    quantity: int = 0,
    unit_price: float = 0.0,
    category: str = "general",
    reorder_level: int = 0,
    location: str = "",
) -> str:
    """Add a new product to inventory. SKU must be unique (e.g. 'USBC-001')."""
    if quantity < 0 or unit_price < 0 or reorder_level < 0:
        return "Error: quantity, price and reorder level must be non-negative."
    with db() as conn:
        if conn.execute("SELECT 1 FROM items WHERE sku = ?", (sku,)).fetchone():
            return f"Error: SKU {sku} already exists. Use adjust_stock or update_item."
        conn.execute(
            "INSERT INTO items VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sku, name, category, quantity, unit_price, reorder_level, location),
        )
        if quantity:
            _log(conn, sku, quantity, "initial stock")
    return f"Added {name} ({sku}) with {quantity} units."


@tool
def adjust_stock(sku: str, change: int, reason: str) -> str:
    """Change stock for a SKU. Use positive numbers for receiving stock and
    negative numbers for sales, damage or shrinkage. Always give a reason."""
    with db() as conn:
        row = conn.execute("SELECT * FROM items WHERE sku = ?", (sku,)).fetchone()
        if not row:
            return f"Error: no item with SKU {sku}. Try search_items first."
        new_qty = row["quantity"] + change
        if new_qty < 0:
            return f"Error: only {row['quantity']} {row['name']} in stock, cannot remove {-change}."
        conn.execute("UPDATE items SET quantity = ? WHERE sku = ?", (new_qty, sku))
        _log(conn, sku, change, reason)
    warn = " This is at or below the reorder level." if new_qty <= row["reorder_level"] else ""
    return f"{row['name']}: {row['quantity']} -> {new_qty} ({reason}).{warn}"


@tool
def update_item(
    sku: str,
    name: str | None = None,
    unit_price: float | None = None,
    category: str | None = None,
    reorder_level: int | None = None,
    location: str | None = None,
) -> str:
    """Update product details (not quantity). Only the fields you pass change."""
    fields = {
        "name": name, "unit_price": unit_price, "category": category,
        "reorder_level": reorder_level, "location": location,
    }
    fields = {k: v for k, v in fields.items() if v is not None}
    if not fields:
        return "Nothing to update."
    sets = ", ".join(f"{k} = ?" for k in fields)
    with db() as conn:
        cur = conn.execute(f"UPDATE items SET {sets} WHERE sku = ?", (*fields.values(), sku))
        if cur.rowcount == 0:
            return f"Error: no item with SKU {sku}."
    return f"Updated {sku}: " + ", ".join(f"{k}={v}" for k, v in fields.items())


@tool
def search_items(query: str = "", category: str = "") -> str:
    """Find products by partial name or SKU, optionally filtered by category.
    Call with no arguments to list everything."""
    sql = "SELECT * FROM items WHERE (name LIKE ? OR sku LIKE ?)"
    params: list = [f"%{query}%", f"%{query}%"]
    if category:
        sql += " AND category = ?"
        params.append(category)
    with db() as conn:
        rows = conn.execute(sql + " ORDER BY name", params).fetchall()
    return "\n".join(_fmt(r) for r in rows) if rows else "No matching items."


@tool
def low_stock_report() -> str:
    """List items at or below their reorder level, with suggested order quantities."""
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM items WHERE quantity <= reorder_level ORDER BY quantity"
        ).fetchall()
    if not rows:
        return "Nothing is at or below its reorder level."
    lines = []
    for r in rows:
        suggest = max(r["reorder_level"] * 2 - r["quantity"], 1)
        lines.append(f"{_fmt(r)} | suggest ordering {suggest}")
    return "\n".join(lines)


@tool
def inventory_value() -> str:
    """Total inventory value, overall and by category."""
    with db() as conn:
        rows = conn.execute(
            "SELECT category, SUM(quantity) AS units, SUM(quantity * unit_price) AS value "
            "FROM items GROUP BY category ORDER BY value DESC"
        ).fetchall()
    if not rows:
        return "Inventory is empty."
    total = sum(r["value"] for r in rows)
    lines = [f"{r['category']}: {r['units']} units, ${r['value']:,.2f}" for r in rows]
    return "\n".join(lines) + f"\nTOTAL: ${total:,.2f}"


@tool
def stock_history(sku: str, limit: int = 20) -> str:
    """Show recent stock movements for a SKU (receipts, sales, adjustments)."""
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM movements WHERE sku = ? ORDER BY id DESC LIMIT ?", (sku, limit)
        ).fetchall()
    if not rows:
        return f"No movements recorded for {sku}."
    return "\n".join(f"{r['timestamp']} | {r['change']:+d} | {r['reason']}" for r in rows)


@tool
def delete_item(sku: str) -> str:
    """Permanently remove a product and its history. Only use when the user
    has explicitly confirmed they want it deleted."""
    with db() as conn:
        conn.execute("DELETE FROM movements WHERE sku = ?", (sku,))
        cur = conn.execute("DELETE FROM items WHERE sku = ?", (sku,))
    return f"Deleted {sku}." if cur.rowcount else f"Error: no item with SKU {sku}."


# ---------------------------------------------------------------------------
# Bulk helpers used by the web app (not exposed to the agent)
# ---------------------------------------------------------------------------

CSV_COLUMNS = ["sku", "name", "category", "quantity", "unit_price", "reorder_level", "location"]


def import_rows(rows: list[dict]) -> dict:
    """Insert new SKUs and update existing ones. For an existing SKU the
    quantity in the file is treated as a stock count, and the difference is
    logged as a movement so history stays accurate."""
    def clean(v):
        # Blank spreadsheet cells arrive as None or NaN depending on the reader
        if v is None or (isinstance(v, float) and v != v):
            return None
        return v

    added = updated = 0
    errors: list[str] = []
    with db() as conn:
        for i, raw in enumerate(rows, start=2):  # row 1 is the header
            raw = {k: clean(v) for k, v in raw.items()}
            try:
                sku = str(raw.get("sku") or "").strip()
                name = str(raw.get("name") or "").strip()
                if not sku or not name:
                    raise ValueError("sku and name are required")
                qty = int(float(raw.get("quantity") or 0))
                price = float(raw.get("unit_price") or 0)
                reorder = int(float(raw.get("reorder_level") or 0))
                if qty < 0 or price < 0 or reorder < 0:
                    raise ValueError("numbers must be non-negative")
                category = str(raw.get("category") or "general").strip() or "general"
                location = str(raw.get("location") or "").strip()
            except (ValueError, TypeError) as e:
                errors.append(f"Row {i}: {e}")
                continue
            row = conn.execute("SELECT quantity FROM items WHERE sku = ?", (sku,)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO items VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (sku, name, category, qty, price, reorder, location),
                )
                if qty:
                    _log(conn, sku, qty, "CSV import")
                added += 1
            else:
                conn.execute(
                    "UPDATE items SET name=?, category=?, quantity=?, unit_price=?, "
                    "reorder_level=?, location=? WHERE sku=?",
                    (name, category, qty, price, reorder, location, sku),
                )
                if qty != row["quantity"]:
                    _log(conn, sku, qty - row["quantity"], "CSV import count")
                updated += 1
    return {"added": added, "updated": updated, "errors": errors}


def all_items() -> list[dict]:
    with db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM items ORDER BY name")]


def all_movements(limit: int = 500) -> list[dict]:
    with db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT m.timestamp, m.sku, i.name, m.change, m.reason FROM movements m "
            "LEFT JOIN items i ON i.sku = m.sku ORDER BY m.id DESC LIMIT ?", (limit,)
        )]


TOOLS = [add_item, adjust_stock, update_item, search_items,
         low_stock_report, inventory_value, stock_history, delete_item]

SYSTEM_PROMPT = """You are an inventory management assistant for a small business.
Use the tools to read and change inventory; never invent stock numbers.
- If the user names a product but not a SKU, use search_items to find the SKU first.
- If several items match, ask which one they mean before changing anything.
- When adding an item without a SKU, generate a short uppercase one (e.g. 'MUG-BLK-01').
- Every stock change needs a reason (sale, received shipment, damaged, count correction).
- Before delete_item, confirm with the user in a separate turn.
- Mention low-stock warnings when a tool returns them.
Keep answers short and use plain text tables when listing several items."""


def build_agent():
    init_db()
    return create_agent(
        model=MODEL,
        tools=TOOLS,
        system_prompt=SYSTEM_PROMPT,
        checkpointer=InMemorySaver(),  # remembers the conversation within a session
    )


def main() -> None:
    agent = build_agent()
    config = {"configurable": {"thread_id": "inventory-session"}}
    print(f"Inventory assistant ({MODEL}). Type 'quit' to exit.\n")
    while True:
        try:
            user = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if user.lower() in {"quit", "exit"}:
            break
        if not user:
            continue
        result = agent.invoke({"messages": [{"role": "user", "content": user}]}, config)
        print(result["messages"][-1].text, "\n")


if __name__ == "__main__":
    main()
