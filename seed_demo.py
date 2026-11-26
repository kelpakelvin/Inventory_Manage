"""
Fill the database with 90 days of realistic demo history so the Lean / Six
Sigma tab has something to analyze.

    python seed_demo.py           # adds demo data (refuses if items exist)
    python seed_demo.py --reset   # wipes the database first

The simulation is simple on purpose: daily sales follow a Poisson draw per
item, stock is reordered when it falls to the reorder level and arrives after
the item's lead time, and occasional cycle counts find discrepancies. A few
patterns are planted so each chart has a story:
- HDMI-002 has frequent count errors (it dominates the Pareto chart)
- USBC-001 has a demand spike 12 days ago (an out-of-control point)
- LAMP-GRN-01 has not sold in months (dead stock)
- MUG-BLK-01 runs out a few times (stockout days)
"""

import random
import sys
from datetime import datetime, timedelta

import numpy as np

import inventory_agent as ia

DAYS = 90
# sku, name, category, unit_price, mean daily demand, reorder_level, order qty, lead days, location, count error prob
PRODUCTS = [
    ("USBC-001", "USB-C cable", "cables", 6.50, 4.0, 25, 80, 5, "Shelf A2", 0.02),
    ("HDMI-002", "HDMI cable 2m", "cables", 9.99, 2.5, 15, 50, 7, "Shelf A3", 0.25),
    ("MUG-BLK-01", "Black mug", "kitchen", 4.00, 3.0, 6, 30, 10, "Shelf C1", 0.02),
    ("MUG-WHT-01", "White mug", "kitchen", 4.00, 1.5, 8, 30, 10, "Shelf C1", 0.03),
    ("NOTE-A5", "A5 notebook", "office", 3.25, 5.0, 30, 100, 6, "Shelf B1", 0.04),
    ("PEN-BLU-12", "Blue pens (12)", "office", 5.75, 2.0, 12, 40, 6, "Shelf B2", 0.02),
    ("CHGR-65W", "65W USB-C charger", "electronics", 39.00, 0.8, 5, 15, 14, "Cage 1", 0.01),
    ("LAMP-GRN-01", "Green desk lamp", "office", 29.00, 0.0, 2, 0, 14, "Back room", 0.0),
]


def main() -> None:
    random.seed(7)
    rng = np.random.default_rng(7)
    ia.init_db()
    with ia.db() as conn:
        if "--reset" in sys.argv:
            conn.execute("DELETE FROM movements")
            conn.execute("DELETE FROM items")
        elif conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]:
            sys.exit("Database already has items. Run with --reset to replace them with demo data.")

        start = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0) - timedelta(days=DAYS)

        def log(sku, change, reason, day, hour=9):
            ts = (start + timedelta(days=day, hours=hour - 9, minutes=random.randint(0, 59)))
            conn.execute("INSERT INTO movements (sku, change, reason, timestamp) VALUES (?,?,?,?)",
                         (sku, int(change), reason, ts.isoformat(timespec="seconds")))

        for sku, name, cat, price, mean, reorder, order_qty, lead, loc, err in PRODUCTS:
            qty = order_qty if order_qty else 14
            log(sku, qty, "initial stock", 0, 8)
            on_order: list[tuple[int, int]] = []  # (arrival day, qty)
            for day in range(DAYS):
                for arrival, q in [o for o in on_order if o[0] == day]:
                    qty += q
                    log(sku, q, "received shipment", day, 8)
                on_order = [o for o in on_order if o[0] != day]

                demand = rng.poisson(mean * (0.6 if day % 7 in (5, 6) else 1.1)) if mean else 0
                if sku == "USBC-001" and day == DAYS - 12:
                    demand += 22  # planted spike
                sold = min(demand, qty)
                if sold:
                    qty -= sold
                    log(sku, -sold, "sale", day, random.randint(10, 17))

                if order_qty and qty <= reorder and not on_order:
                    on_order.append((day + lead, order_qty))

                if err and random.random() < err and qty > 2:
                    diff = random.choice([-3, -2, -2, -1, -1, 1])
                    qty += diff
                    log(sku, diff, "count correction", day, 18)

            conn.execute("INSERT INTO items VALUES (?,?,?,?,?,?,?)",
                         (sku, name, cat, qty, price, reorder, loc))
    print(f"Seeded {len(PRODUCTS)} products with {DAYS} days of history into {ia.DB_PATH}.")


if __name__ == "__main__":
    main()
