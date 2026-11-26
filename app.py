"""
Web front end for the inventory assistant.

Run:  streamlit run app.py
"""

import os

import pandas as pd
import streamlit as st

import inventory_agent as ia

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

chat_tab, stock_tab, low_tab, history_tab = st.tabs(
    ["💬 Assistant", "📋 Stock", f"⚠️ Low stock ({len(low)})", "🕓 History"]
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
