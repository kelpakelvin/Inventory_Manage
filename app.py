"""
Web front end for the inventory assistant.

Run:  streamlit run app.py
"""

import os

import pandas as pd
import streamlit as st

import altair as alt

import inventory_agent as ia
import lean_metrics as lm

# Chart colors: one series hue, muted reference lines, a reserved alert red
SERIES = "#3B6FD4"
REFERENCE = "#8A8F98"
ALERT = "#D64541"

st.set_page_config(page_title="Inventory Assistant", page_icon="📦", layout="wide")
ia.init_db()


@st.cache_resource
def get_agent():
    return ia.build_agent()


def has_api_key() -> bool:
    provider = ia.MODEL.split(":", 1)[0]
    env = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}.get(provider)
    return env is None or bool(os.getenv(env))


# ---------------------------------------------------------------------------
# Sidebar: summary numbers and CSV import / export
# ---------------------------------------------------------------------------

items = pd.DataFrame(ia.all_items(), columns=ia.CSV_COLUMNS)
low = items[items["quantity"] <= items["reorder_level"]] if not items.empty else items
total_value = float((items["quantity"] * items["unit_price"]).sum()) if not items.empty else 0.0

with st.sidebar:
    st.header("📦 Inventory")
    c1, c2 = st.columns(2)
    c1.metric("Products", len(items))
    c2.metric("Low stock", len(low))
    st.metric("Total value", f"${total_value:,.2f}")

    st.divider()
    st.subheader("Import CSV")
    st.caption("Columns: " + ", ".join(ia.CSV_COLUMNS) + ". Only sku and name are required. "
               "Existing SKUs are updated and their quantity is treated as a stock count.")
    upload = st.file_uploader("Stock list", type=["csv"], label_visibility="collapsed")
    if upload is not None and st.button("Import", type="primary", use_container_width=True):
        try:
            df = pd.read_csv(upload)
            df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
            missing = {"sku", "name"} - set(df.columns)
            if missing:
                st.error(f"Missing required column(s): {', '.join(sorted(missing))}")
            else:
                result = ia.import_rows(df.where(df.notna(), None).to_dict("records"))
                st.session_state.import_msg = result
                st.rerun()
        except Exception as e:  # bad encoding, not a CSV, etc.
            st.error(f"Could not read that file: {e}")

    if msg := st.session_state.pop("import_msg", None):
        st.success(f"Added {msg['added']}, updated {msg['updated']}.")
        for err in msg["errors"]:
            st.warning(err)

    template = pd.DataFrame([{
        "sku": "USBC-001", "name": "USB-C cable", "category": "cables", "quantity": 40,
        "unit_price": 6.50, "reorder_level": 10, "location": "Shelf A2",
    }])
    st.download_button("Download template", template.to_csv(index=False),
                       "inventory_template.csv", "text/csv", use_container_width=True)
    if not items.empty:
        st.download_button("Export inventory", items.to_csv(index=False),
                           "inventory_export.csv", "text/csv", use_container_width=True)


# ---------------------------------------------------------------------------
# Main area
# ---------------------------------------------------------------------------

chat_tab, stock_tab, low_tab, history_tab, lean_tab = st.tabs(
    ["💬 Assistant", "📋 Stock", f"⚠️ Low stock ({len(low)})", "🕓 History", "📈 Lean / Six Sigma"]
)

with chat_tab:
    if "messages" not in st.session_state:
        st.session_state.messages = []

    if not has_api_key():
        st.info(f"Set an API key for **{ia.MODEL}** to chat with the assistant "
                "(for example `export ANTHROPIC_API_KEY=...`). The other tabs work without one.")

    if not st.session_state.messages:
        st.caption("Try: *add 25 black mugs at $4 each, reorder at 5* · "
                   "*we sold 3 mugs* · *what's running low?* · *what's my stock worth?*")

    for m in st.session_state.messages:
        with st.chat_message(m["role"]):
            st.markdown(m["content"])

    if prompt := st.chat_input("Ask or tell the assistant something", disabled=not has_api_key()):
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)
        with st.chat_message("assistant"):
            with st.spinner("Working..."):
                try:
                    result = get_agent().invoke(
                        {"messages": [{"role": "user", "content": prompt}]},
                        {"configurable": {"thread_id": "web-session"}},
                    )
                    reply = result["messages"][-1].text
                except Exception as e:
                    reply = f"Something went wrong: {e}"
            st.markdown(reply)
        st.session_state.messages.append({"role": "assistant", "content": reply})
        st.rerun()  # refresh sidebar numbers and tables after any change

with stock_tab:
    if items.empty:
        st.info("No products yet. Import a CSV from the sidebar or ask the assistant to add some.")
    else:
        f1, f2 = st.columns([3, 1])
        q = f1.text_input("Search", placeholder="Name or SKU")
        cats = ["All"] + sorted(items["category"].dropna().unique().tolist())
        cat = f2.selectbox("Category", cats)
        view = items.copy()
        if q:
            mask = view["name"].str.contains(q, case=False) | view["sku"].str.contains(q, case=False)
            view = view[mask]
        if cat != "All":
            view = view[view["category"] == cat]
        view["value"] = view["quantity"] * view["unit_price"]
        view["status"] = ["Low" if r.quantity <= r.reorder_level else "OK" for r in view.itertuples()]
        st.dataframe(
            view, hide_index=True, use_container_width=True,
            column_config={
                "unit_price": st.column_config.NumberColumn("Unit price", format="$%.2f"),
                "value": st.column_config.NumberColumn("Value", format="$%.2f"),
                "reorder_level": "Reorder at",
            },
        )

        st.subheader("Quick stock adjustment")
        a1, a2, a3, a4 = st.columns([2, 1, 2, 1])
        sku = a1.selectbox("Product", items["sku"],
                           format_func=lambda s: f"{s} · {items.set_index('sku').loc[s, 'name']}")
        change = a2.number_input("Change", value=0, step=1, help="Positive to receive, negative to remove")
        reason = a3.selectbox("Reason", ["sale", "received shipment", "damaged", "count correction", "returned"])
        a4.write("")
        a4.write("")
        if a4.button("Apply", use_container_width=True, disabled=change == 0):
            out = ia.adjust_stock.invoke({"sku": sku, "change": int(change), "reason": reason})
            (st.error if out.startswith("Error") else st.toast)(out)
            if not out.startswith("Error"):
                st.rerun()

with low_tab:
    if low.empty:
        st.success("Nothing is at or below its reorder level.")
    else:
        lv = low.copy()
        lv["suggest_order"] = (lv["reorder_level"] * 2 - lv["quantity"]).clip(lower=1)
        st.dataframe(lv[["sku", "name", "category", "quantity", "reorder_level", "suggest_order", "location"]],
                     hide_index=True, use_container_width=True)

with history_tab:
    hist = pd.DataFrame(ia.all_movements())
    if hist.empty:
        st.info("No stock movements yet.")
    else:
        st.dataframe(hist, hide_index=True, use_container_width=True,
                     column_config={"change": st.column_config.NumberColumn(format="%+d")})

with lean_tab:
    items_df, mov = lm.load()
    if items_df.empty or mov.empty:
        st.info("Not enough history yet. Log some sales, or run `python seed_demo.py` "
                "to load 90 days of demo data.")
        st.stop()

    s1, s2, s3 = st.columns(3)
    lookback = s1.selectbox("Look back", [30, 60, 90], index=2, format_func=lambda d: f"{d} days")
    lead_time = s2.number_input("Supplier lead time (days)", min_value=1, max_value=90, value=7)
    level = s3.selectbox("Service level", list(lm.SERVICE_LEVELS), index=1,
                         help="Chance of not running out while waiting for a delivery")
    z = lm.SERVICE_LEVELS[level]

    demand = lm.daily_demand(mov, lookback)
    balance = lm.daily_balance(mov, lookback)
    turns = lm.turns(items_df, demand, balance)
    outs = lm.stockout_days(items_df, balance)
    errs = lm.count_error_stats(mov, lookback)
    dead = lm.dead_stock(items_df, mov, idle_days=min(60, lookback))
    selling = turns[turns > 0]

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Stockout days", int(outs.sum()), help="Item-days that ended with zero stock")
    k2.metric("Count errors / 100 moves", f"{errs['rate_per_100']:.1f}",
              help="Count corrections per 100 stock movements. Each is a defect.")
    k3.metric("Dead stock value", f"${dead['value_tied_up'].sum():,.0f}",
              help="Stock on hand with no sale in the last 60 days")
    k4.metric("Median turns / year", f"{selling.median():.1f}" if len(selling) else "n/a",
              help="Annualized units sold divided by average units on hand")

    # --- Reorder points (Lean pull / Kanban) -------------------------------
    st.subheader("Reorder points")
    st.caption(f"Reorder point = average daily demand × {lead_time}-day lead time + safety stock. "
               f"Safety stock = {z} × daily demand variation × √lead time ({level} service level).")
    rop = lm.reorder_table(items_df, demand, lead_time, z)
    rop["turns_per_year"] = rop["sku"].map(turns).round(1)
    rop["stockout_days"] = rop["sku"].map(outs)
    changed = rop[(rop["avg_daily_demand"] > 0) & (rop["suggested_reorder_at"] != rop["current_reorder_at"])]
    st.dataframe(
        rop.drop(columns=["demand_std"]), hide_index=True, use_container_width=True,
        column_config={
            "avg_daily_demand": "Avg daily demand", "safety_stock": "Safety stock",
            "current_reorder_at": "Reorder at (now)", "suggested_reorder_at": "Reorder at (suggested)",
            "days_of_supply": "Days of supply", "on_hand": "On hand",
            "turns_per_year": "Turns / year", "stockout_days": "Stockout days",
        },
    )
    if len(changed) and st.button(f"Apply {len(changed)} suggested reorder points", type="primary"):
        for r in changed.itertuples():
            ia.update_item.invoke({"sku": r.sku, "reorder_level": int(r.suggested_reorder_at)})
        st.toast(f"Updated reorder points for {len(changed)} items.")
        st.rerun()

    # --- Control chart (Six Sigma) -----------------------------------------
    st.subheader("Demand control chart")
    sold_skus = [s for s in items_df["sku"] if s in demand and demand[s].sum() > 0]
    if sold_skus:
        names = items_df.set_index("sku")["name"]
        def severity(s):
            # How far the worst day sits beyond the upper limit; slow movers are
            # skipped because an I chart over-flags low-count demand
            lim_ = lm.control_limits(demand[s])
            if demand[s].mean() < 2 or lim_["ucl"] <= 0:
                return 0.0
            return float(demand[s].max() / lim_["ucl"])
        # Open on the clearest out-of-control signal, else the best seller
        scores = [severity(s) for s in sold_skus]
        default = (scores.index(max(scores)) if max(scores) > 1
                   else int(demand[sold_skus].sum().argmax()))
        pick = st.selectbox("Item", sold_skus, index=default,
                            format_func=lambda s: f"{s} · {names[s]}")
        series = demand[pick]
        lim = lm.control_limits(series)
        cdf = pd.DataFrame({"date": series.index, "units": series.values})
        cdf["signal"] = (cdf["units"] > lim["ucl"]) | (cdf["units"] < lim["lcl"])
        n_out = int(cdf["signal"].sum())
        st.caption(f"Mean {lim['mean']:.1f} units/day · upper limit {lim['ucl']:.1f} · lower limit {lim['lcl']:.1f}. "
                   + (f"**{n_out} day(s) outside the limits**: look for a special cause "
                      "(promotion, data entry error, theft)." if n_out
                      else "All days within limits: demand is stable, variation is normal noise.")
                   + (" Low-volume item (under 2 a day): occasional spikes are expected, so treat signals with caution."
                      if lim["mean"] < 2 else ""))

        base = alt.Chart(cdf).encode(x=alt.X("date:T", title=None))
        line = base.mark_line(color=SERIES, strokeWidth=2).encode(
            y=alt.Y("units:Q", title="Units sold per day"))
        hover = base.mark_circle(size=90, opacity=0).encode(
            y="units:Q", tooltip=[alt.Tooltip("date:T", title="Date"), alt.Tooltip("units:Q", title="Units")])
        dots = base.transform_filter("datum.signal").mark_point(
            shape="diamond", size=110, filled=True, color=ALERT).encode(
            y="units:Q", tooltip=[alt.Tooltip("date:T", title="Out of control"), alt.Tooltip("units:Q", title="Units")])
        rules = pd.DataFrame({"y": [lim["mean"], lim["ucl"], lim["lcl"]],
                              "label": ["Mean", "UCL", "LCL"], "dash": [0, 1, 1]})
        rule = alt.Chart(rules).mark_rule(color=REFERENCE, strokeWidth=1).encode(
            y="y:Q", strokeDash=alt.condition("datum.dash == 1", alt.value([5, 4]), alt.value([1, 0])))
        text = alt.Chart(rules).mark_text(align="left", dx=4, dy=-6, color=REFERENCE, fontSize=11).encode(
            y="y:Q", x=alt.value(0), text="label:N")
        st.altair_chart((line + hover + dots + rule + text).properties(height=280),
                        use_container_width=True)

    # --- Pareto of count errors --------------------------------------------
    left, right = st.columns(2)
    with left:
        st.subheader("Where count errors come from")
        p = errs["pareto"]
        if p.empty:
            st.success("No count corrections in this period.")
        else:
            p = p.head(8).copy()
            p["label"] = p["cum_pct"].map(lambda v: f"{v:.0f}% cumulative")
            bars = alt.Chart(p).mark_bar(color=SERIES, cornerRadiusEnd=4, height=18).encode(
                y=alt.Y("sku:N", sort=None, title=None),
                x=alt.X("units:Q", title="Units corrected"),
                tooltip=[alt.Tooltip("sku:N", title="SKU"), alt.Tooltip("units:Q", title="Units corrected"),
                         alt.Tooltip("cum_pct:Q", title="Cumulative %")])
            labels = bars.mark_text(align="left", dx=6, color=REFERENCE, fontSize=11).encode(text="label:N")
            st.altair_chart((bars + labels).properties(height=34 * len(p) + 30), use_container_width=True)
            top = p.iloc[0]
            st.caption(f"{top['sku']} alone accounts for {top['cum_pct']:.0f}% of units corrected. "
                       "Start root-cause work there (labeling, shelf location, receiving process).")
    with right:
        st.subheader("Dead stock")
        if dead.empty:
            st.success("Everything in stock has sold recently.")
        else:
            st.dataframe(dead.drop(columns=["location"]), hide_index=True, use_container_width=True,
                         column_config={"value_tied_up": st.column_config.NumberColumn(
                             "Value tied up", format="$%.2f"), "last_sale": "Last sale"})
            st.caption("Lean treats overstock as waste: it ties up cash and space. "
                       "Consider discounting, returning to supplier, or not reordering.")

    with st.expander("How these are calculated"):
        st.markdown("""
- **Sales** are stock movements with a reason like "sale". **Count errors** are movements with "count" in the reason.
- **Stock levels by day** are rebuilt by adding up the movement log, so they are only as accurate as that log.
- **Reorder point** = average daily demand × lead time + safety stock. **Safety stock** = z × standard deviation of daily demand × √lead time.
- **Control chart** is an individuals (I) chart. Limits are mean ± 3σ, with σ estimated as the average day-to-day change ÷ 1.128.
- **Turns per year** = units sold in the period scaled to a year ÷ average units on hand.
""")
