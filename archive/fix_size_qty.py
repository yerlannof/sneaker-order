#!/usr/bin/env python3
"""
Fix size_qty in ЗК-005: use REAL per-size sales and stock from DuckDB.
"""
import json, os, sys, math
from pathlib import Path
import requests

sys.path.insert(0, str(Path(__file__).parent.parent))
import duckdb

DB_PATH = Path(__file__).parent.parent / "data" / "pnlpower.duckdb"
ENV_PATH = Path(__file__).parent.parent / ".env"

# Load env
for line in ENV_PATH.read_text().splitlines():
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"'))

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}

ORDER_ID = "ЗК-005"


def get_order():
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/orders?id=eq.{ORDER_ID}&select=*",
        headers={**HEADERS, "Accept": "application/vnd.pgrst.object+json"},
    )
    r.raise_for_status()
    return r.json()


def update_order(items):
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/orders?id=eq.{ORDER_ID}",
        headers={**HEADERS, "Prefer": "return=minimal"},
        json={"items": items, "status": "draft"},
    )
    r.raise_for_status()


def extract_size(product_name: str) -> str | None:
    """Extract size from product name like 'Nike Dunk Low, 42'"""
    parts = product_name.rsplit(",", 1)
    if len(parts) == 2:
        s = parts[1].strip()
        try:
            float(s)
            return s
        except ValueError:
            return None
    return None


def get_real_size_data(con, article: str, snap_table: str):
    """Get real per-size sales and stock from DuckDB."""

    # Sales by size (all time, price > 0 = real sales)
    sales_rows = con.execute("""
        SELECT product_name, SUM(quantity) as sold
        FROM sales
        WHERE article = ? AND price > 0
        GROUP BY product_name
    """, [article]).fetchall()

    size_sold = {}
    for name, sold in sales_rows:
        sz = extract_size(name)
        if sz:
            size_sold[sz] = size_sold.get(sz, 0) + int(sold)

    # Stock by size from latest snapshot
    stock_rows = con.execute(f"""
        SELECT product_name,
               moscow, tsum + online as tsum_onl,
               astana_aruzhan as aru, main_warehouse as wh
        FROM {snap_table}
        WHERE article = ?
    """, [article]).fetchall()

    size_stock = {}
    size_msk = {}
    size_tsum = {}
    size_aru = {}
    for name, msk, tsum, aru, wh in stock_rows:
        sz = extract_size(name)
        if sz:
            size_stock[sz] = size_stock.get(sz, 0) + int(msk + tsum + aru + wh)
            size_msk[sz] = size_msk.get(sz, 0) + int(msk)
            size_tsum[sz] = size_tsum.get(sz, 0) + int(tsum)
            size_aru[sz] = size_aru.get(sz, 0) + int(aru)

    return size_sold, size_stock, size_msk, size_tsum, size_aru


def determine_range(all_sizes: set[str]) -> list[str]:
    """Sneakers are almost always unisex — always include 40-44.
    If women's sizes (36-39) exist in data, include those too."""
    nums = sorted(int(s) for s in all_sizes)
    if not nums:
        return [str(s) for s in range(40, 45)]

    has_women = any(s < 40 for s in nums)

    # Always include men's range 40-44 (sneakers are unisex)
    # Add women's range if any women's sizes in data
    rmin = 36 if has_women else 40
    rmax = 44

    return [str(s) for s in range(rmin, rmax + 1)]


def calc_size_qty(sizes, size_sold, size_stock, total_pairs, global_weights):
    """Smart size_qty using SELL-THROUGH based weights + per-model data."""
    num_sizes = len(sizes)

    # Per-model total sold — need 20+ sales for model data to matter
    model_total = sum(size_sold.get(sz, 0) for sz in sizes)
    blend = min(1.0, max(0, (model_total - 5) / 20))  # <5 sales = pure global

    # Global weight total for this range
    global_total = sum(global_weights.get(sz, 0.01) for sz in sizes)

    needs = {}
    total_need = 0

    for sz in sizes:
        sold = size_sold.get(sz, 0)
        stock = size_stock.get(sz, 0)
        global_pct = global_weights.get(sz, 0.01) / global_total

        # Model-specific factor (capped to avoid noise from 1-2 sales)
        avg_model = model_total / num_sizes if model_total > 0 else 1
        model_factor = min(2.5, max(0.5, sold / avg_model)) if sold > 0 else 0.7

        # Combined weight: sparse models = pure global ST
        weight = global_pct * (1 + (model_factor - 1) * blend)

        # Raw allocation based on weight
        raw = total_pairs * weight * num_sizes

        # --- Stock penalty ---
        if stock >= 8:
            need = raw * 0.05
        elif stock >= 5:
            need = raw * 0.15
        elif stock >= 3:
            need = raw * 0.35
        elif stock >= 1:
            need = max(0.3, raw - stock * 1.5)
        else:
            need = max(0.3, raw)

        # --- Global ST penalty: cut slow sizes, boost hot ---
        global_st = global_weights.get(sz, 0.3)
        if global_st < 0.20:
            need *= 0.1   # ST<20% (size 36) → near zero
        elif global_st < 0.30:
            need *= 0.4   # ST<30% (size 37) → heavily reduce
        elif global_st >= 0.42:
            need *= 1.4   # ST>42% (sizes 39, 42) → boost

        needs[sz] = need
        total_need += need

    # Scale to match total
    scale = total_pairs / total_need if total_need > 0 else 1
    result = {}
    for sz in sizes:
        stock = size_stock.get(sz, 0)
        global_st = global_weights.get(sz, 0.3)
        min_qty = 0 if (stock >= 8 or global_st < 0.20) else 1
        result[sz] = max(min_qty, round(needs[sz] * scale))

    # Fix rounding
    new_total = sum(result.values())
    diff = total_pairs - new_total
    by_need = sorted(sizes, key=lambda s: needs[s], reverse=True)
    i = 0
    while diff != 0 and i < 200:
        sz = by_need[i % len(by_need)]
        if diff > 0:
            result[sz] += 1
            diff -= 1
        elif result[sz] > 1:
            result[sz] -= 1
            diff += 1
        i += 1

    return result


def main():
    con = duckdb.connect(str(DB_PATH), read_only=True)

    # Latest snapshot
    snap = con.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_name LIKE 'inventory_snapshot_stores_%' "
        "ORDER BY table_name DESC LIMIT 1"
    ).fetchone()[0]
    print(f"Using snapshot: {snap}")

    # Load order
    order = get_order()
    items = order["items"]
    print(f"Order {ORDER_ID}: {len(items)} items")

    # Step 1: Calculate GLOBAL sell-through weights from ALL sneaker sales
    st_data = con.execute(f"""
        WITH sales_by_size AS (
            SELECT
                TRY_CAST(split_part(product_name, ', ', -1) AS INT) as size,
                SUM(quantity)::FLOAT as sold
            FROM sales
            WHERE price > 0 AND sale_datetime >= '2025-12-22'
              AND TRY_CAST(split_part(product_name, ', ', -1) AS INT) BETWEEN 36 AND 46
            GROUP BY 1
        ),
        stock_by_size AS (
            SELECT
                TRY_CAST(split_part(product_name, ', ', -1) AS INT) as size,
                SUM(moscow + tsum + online + astana_aruzhan + main_warehouse)::FLOAT as stock
            FROM {snap}
            WHERE TRY_CAST(split_part(product_name, ', ', -1) AS INT) BETWEEN 36 AND 46
            GROUP BY 1
        )
        SELECT s.size, s.sold, COALESCE(i.stock, 0) as stock,
               s.sold / (s.sold + COALESCE(i.stock, 0)) as sell_through
        FROM sales_by_size s
        LEFT JOIN stock_by_size i ON s.size = i.size
        ORDER BY s.size
    """).fetchall()

    # Use sell-through as weight (higher ST = order more)
    global_weights = {}
    print("\nGlobal sell-through weights:")
    for size, sold, stock, st in st_data:
        global_weights[str(size)] = st
        print(f"  {size}: ST={st:.1%} (sold={sold:.0f}, stock={stock:.0f})")

    # Step 1b: Get per-item data
    item_data = []
    for item in items:
        article = item.get("article", "")
        if not article:
            item_data.append(None)
            continue

        sold, stock, msk, tsum, aru = get_real_size_data(con, article, snap)
        item_data.append({
            "sold": sold, "stock": stock,
            "msk": msk, "tsum": tsum, "aru": aru,
        })

    print(f"\nGlobal weights (sell-through based):")
    for sz in sorted(global_weights.keys(), key=lambda x: int(x)):
        print(f"  {sz}: {global_weights[sz]:.1%}")

    # Step 2: Recalculate each item
    for i, item in enumerate(items):
        data = item_data[i]
        if data is None:
            continue

        sold = data["sold"]
        stock = data["stock"]

        # All known sizes
        all_known = set(sold.keys()) | set(stock.keys())
        sizes = determine_range(all_known)

        # Total pairs (keep original)
        total_pairs = sum(item.get("size_qty", {}).values())
        if total_pairs <= 0:
            total_pairs = item.get("pairs", 0)
        if total_pairs <= 0:
            continue

        # Calculate
        new_qty = calc_size_qty(sizes, sold, stock, total_pairs, global_weights)

        # Update item
        item["size_qty"] = new_qty
        item["size_sold"] = {sz: sold.get(sz, 0) for sz in sizes}
        item["size_stock"] = {sz: stock.get(sz, 0) for sz in sizes}
        item["size_msk"] = {sz: data["msk"].get(sz, 0) for sz in sizes}
        item["size_tsum"] = {sz: data["tsum"].get(sz, 0) for sz in sizes}
        item["size_aru"] = {sz: data["aru"].get(sz, 0) for sz in sizes}
        item["pairs"] = total_pairs
        item["order_mode"] = "sizes"

        # Report
        qty_str = " ".join(f"{sz}={new_qty[sz]}" for sz in sizes)
        sold_str = " ".join(f"s{sold.get(sz,0)}" for sz in sizes)
        stock_str = " ".join(f"i{stock.get(sz,0)}" for sz in sizes)
        print(f"\n{i}. {item['model'][:35]} ({total_pairs}п) [{sizes[0]}-{sizes[-1]}]")
        print(f"   qty:   {qty_str}")
        print(f"   sold:  {sold_str}")
        print(f"   stock: {stock_str}")

    # Step 3: Save to Supabase
    update_order(items)
    print(f"\n✅ Saved {len(items)} items to Supabase")

    con.close()


if __name__ == "__main__":
    main()
