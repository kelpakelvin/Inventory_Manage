"""
Lean and Six Sigma metrics computed from the inventory database.

Everything here is derived from two tables: items (current state) and
movements (every stock change with a timestamp and reason). No extra data
entry is needed: if the stock history is logged, these numbers follow.

Conventions
- A sale is a negative movement whose reason mentions "sale" or "sold".
- A count correction is any movement whose reason mentions "count". Each one
  means the system and the shelf disagreed, so it is treated as a defect.
"""

import math

import pandas as pd

import inventory_agent as ia

# Service level -> z score for one-sided normal protection against stockouts
SERVICE_LEVELS = {"90%": 1.28, "95%": 1.65, "97.5%": 1.96, "99%": 2.33}


def load() -> tuple[pd.DataFrame, pd.DataFrame]:
    items = pd.DataFrame(ia.all_items(), columns=ia.CSV_COLUMNS)
    with ia.db() as conn:
        mov = pd.read_sql_query("SELECT sku, change, reason, timestamp FROM movements", conn)
    mov["timestamp"] = pd.to_datetime(mov["timestamp"])
    mov["date"] = mov["timestamp"].dt.normalize()
    reason = mov["reason"].str.lower()
    mov["is_sale"] = (mov["change"] < 0) & reason.str.contains("sale|sold")
    mov["is_correction"] = reason.str.contains("count")
    return items, mov


def _window(lookback_days: int) -> pd.DatetimeIndex:
    end = pd.Timestamp.now().normalize()
    return pd.date_range(end - pd.Timedelta(days=lookback_days - 1), end, freq="D")


def daily_demand(mov: pd.DataFrame, lookback_days: int) -> pd.DataFrame:
    """Units sold per SKU per day over the window, zero-filled. Rows are dates."""
    days = _window(lookback_days)
    sales = mov[mov["is_sale"] & mov["date"].isin(days)]
    table = sales.pivot_table(index="date", columns="sku", values="change", aggfunc="sum")
    return (-table).reindex(days, fill_value=0).fillna(0)


def daily_balance(mov: pd.DataFrame, lookback_days: int) -> pd.DataFrame:
    """Closing stock per SKU per day, rebuilt by summing the movement log."""
    days = _window(lookback_days)
    net = mov.pivot_table(index="date", columns="sku", values="change", aggfunc="sum").fillna(0)
    full = pd.date_range(min(net.index.min(), days[0]), days[-1], freq="D") if len(net) else days
    return net.reindex(full, fill_value=0).cumsum().reindex(days).ffill().fillna(0)


def reorder_table(items: pd.DataFrame, demand: pd.DataFrame,
                  lead_time_days: float, z: float) -> pd.DataFrame:
    """Statistical reorder points: ROP = avg demand x lead time + safety stock,
    safety stock = z x std of daily demand x sqrt(lead time)."""
    rows = []
    for it in items.itertuples():
        d = demand[it.sku] if it.sku in demand else pd.Series(0.0, index=demand.index)
        avg, std = float(d.mean()), float(d.std(ddof=1)) if len(d) > 1 else 0.0
        safety = z * std * math.sqrt(lead_time_days)
        rop = math.ceil(avg * lead_time_days + safety) if avg > 0 else 0
        rows.append({
            "sku": it.sku, "name": it.name, "on_hand": it.quantity,
            "avg_daily_demand": round(avg, 2), "demand_std": round(std, 2),
            "safety_stock": math.ceil(safety), "current_reorder_at": it.reorder_level,
            "suggested_reorder_at": rop,
            "days_of_supply": round(it.quantity / avg, 1) if avg > 0 else float("nan"),
        })
    return pd.DataFrame(rows)


def turns(items: pd.DataFrame, demand: pd.DataFrame, balance: pd.DataFrame) -> pd.Series:
    """Annualized inventory turns per SKU: yearly units sold / average units on hand."""
    days = len(demand)
    out = {}
    for sku in items["sku"]:
        sold = float(demand[sku].sum()) if sku in demand else 0.0
        avg_on_hand = float(balance[sku].mean()) if sku in balance else 0.0
        out[sku] = (sold * 365 / days) / avg_on_hand if avg_on_hand > 0 else 0.0
    return pd.Series(out)


def stockout_days(items: pd.DataFrame, balance: pd.DataFrame) -> pd.Series:
    """Days in the window each SKU ended at zero stock."""
    return pd.Series({s: int((balance[s] <= 0).sum()) if s in balance else 0 for s in items["sku"]})


def dead_stock(items: pd.DataFrame, mov: pd.DataFrame, idle_days: int) -> pd.DataFrame:
    """Items with stock on hand but no sale in the last idle_days."""
    cutoff = pd.Timestamp.now().normalize() - pd.Timedelta(days=idle_days)
    last_sale = mov[mov["is_sale"]].groupby("sku")["timestamp"].max()
    df = items[items["quantity"] > 0].copy()
    df["last_sale"] = df["sku"].map(last_sale)
    df = df[df["last_sale"].isna() | (df["last_sale"] < cutoff)]
    df["value_tied_up"] = df["quantity"] * df["unit_price"]
    df["last_sale"] = df["last_sale"].dt.date.astype("object").where(df["last_sale"].notna(), "never")
    return df[["sku", "name", "quantity", "value_tied_up", "last_sale", "location"]] \
        .sort_values("value_tied_up", ascending=False)


def count_error_stats(mov: pd.DataFrame, lookback_days: int) -> dict:
    """Corrections per 100 stock movements, plus a Pareto of units corrected by SKU."""
    days = _window(lookback_days)
    w = mov[mov["date"].isin(days)]
    corrections = w[w["is_correction"]]
    pareto = (corrections.assign(units=corrections["change"].abs())
              .groupby("sku")["units"].sum().sort_values(ascending=False))
    cum = (pareto.cumsum() / pareto.sum() * 100).round(0) if len(pareto) else pareto
    return {
        "movements": len(w),
        "corrections": len(corrections),
        "rate_per_100": (len(corrections) / len(w) * 100) if len(w) else 0.0,
        "pareto": pd.DataFrame({"sku": pareto.index, "units": pareto.values,
                                "cum_pct": cum.values}),
    }


def control_limits(series: pd.Series) -> dict:
    """Individuals (I-MR) chart limits. Sigma is estimated from the average
    moving range divided by d2 = 1.128, the standard for an I chart."""
    mean = float(series.mean())
    mr_bar = float(series.diff().abs().mean()) if len(series) > 1 else 0.0
    sigma = mr_bar / 1.128
    return {"mean": mean, "ucl": mean + 3 * sigma, "lcl": max(0.0, mean - 3 * sigma)}
