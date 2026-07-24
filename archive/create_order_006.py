#!/usr/bin/env python3
"""
Create sneaker order ЗК-006 in Supabase with smart size allocation.

Usage:
    python sneaker-order/create_order_006.py
    python sneaker-order/create_order_006.py --dry-run   # print without uploading
"""

import argparse
import math
import os
import sys
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Paths & credentials
# ---------------------------------------------------------------------------
PNLPOWER_DIR = Path(__file__).parent.parent
DB_PATH = PNLPOWER_DIR / "data" / "pnlpower.duckdb"
ENV_PATH = PNLPOWER_DIR / ".env"

SUPABASE_URL = ""
SUPABASE_KEY = ""
SUPABASE_SERVICE_KEY = ""

SITE_URL = "https://yerlannof.github.io/sneaker-order"
SNAP_TABLE = "inventory_snapshot_stores_20260320"

# ---------------------------------------------------------------------------
# Order spec
# ---------------------------------------------------------------------------
ORDER_ID = "ЗК-006"
ORDER_DATE = "2026-03-22"
SEASON_COEFF = 1.35  # March peak

ARTICLES = [
    "202358", "202359", "202313", "202100", "202654", "202276", "201948",
    "202544", "202267", "202554", "202534", "202574", "202549", "202553",
    "202576", "202425", "202653", "201812", "201055", "202245", "202575",
    "202068", "201913", "202541", "202090", "202013", "202644", "201914",
    "201702", "202543", "202535",
]

# Gender classification
WOMEN_ONLY = {"202276", "202541", "201948"}   # sizes 36-41
MEN_ONLY   = {"201913", "202574"}             # sizes 40-44
# everything else = UNISEX (36-44)

# ---------------------------------------------------------------------------
# Size weights (from 6-month real sales data as provided)
# ---------------------------------------------------------------------------
WOMEN_WEIGHTS = {
    "36": 0.117, "37": 0.151, "38": 0.274,
    "39": 0.216, "40": 0.181, "41": 0.061,
}
MEN_WEIGHTS = {
    "40": 0.069, "41": 0.158, "42": 0.240,
    "43": 0.231, "44": 0.177, "45": 0.100,
    # 45 rarely stocked; included for completeness
}
# UNISEX: women×0.45 for 36-39, men×0.55 for 40-44
UNISEX_WEIGHTS = {}
for sz, w in WOMEN_WEIGHTS.items():
    if int(sz) <= 39:
        UNISEX_WEIGHTS[sz] = w * 0.45
UNISEX_WEIGHTS["40"] = MEN_WEIGHTS["40"] * 0.55 + WOMEN_WEIGHTS.get("40", 0.181) * 0.45
for sz in ["41", "42", "43", "44"]:
    UNISEX_WEIGHTS[sz] = MEN_WEIGHTS[sz] * 0.55
# Normalise to 1.0
_total = sum(UNISEX_WEIGHTS.values())
UNISEX_WEIGHTS = {k: v / _total for k, v in UNISEX_WEIGHTS.items()}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_env():
    global SUPABASE_URL, SUPABASE_KEY, SUPABASE_SERVICE_KEY
    for env_path in [Path(__file__).parent / ".env", ENV_PATH]:
        if not env_path.exists():
            continue
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("SUPABASE_URL="):
                SUPABASE_URL = SUPABASE_URL or line.split("=", 1)[1].strip().strip('"')
            elif line.startswith("SUPABASE_KEY="):
                SUPABASE_KEY = SUPABASE_KEY or line.split("=", 1)[1].strip().strip('"')
            elif line.startswith("SUPABASE_SERVICE_KEY="):
                SUPABASE_SERVICE_KEY = SUPABASE_SERVICE_KEY or line.split("=", 1)[1].strip().strip('"')


def get_weights(article: str) -> dict:
    if article in WOMEN_ONLY:
        return WOMEN_WEIGHTS
    if article in MEN_ONLY:
        return MEN_WEIGHTS
    return UNISEX_WEIGHTS


def size_range(article: str) -> list:
    if article in WOMEN_ONLY:
        return [str(s) for s in range(36, 42)]
    if article in MEN_ONLY:
        return [str(s) for s in range(40, 45)]
    return [str(s) for s in range(36, 45)]


def wos_penalty(stock: int, wos: float) -> float:
    """Return the fraction of stock to subtract as penalty based on WOS."""
    if wos < 4:
        return 0.0   # no penalty
    if wos < 8:
        return 0.30  # subtract 30 % of stock
    return 0.60      # subtract 60 % of stock


def calc_total_pairs(weekly_rate: float) -> int:
    adj = weekly_rate * SEASON_COEFF
    if adj > 3.0:
        return 54
    elif adj >= 1.5:
        return 42
    elif adj >= 0.5:
        return 30
    else:
        return 24


def calc_size_qty(
    article: str,
    size_sold: dict,   # size -> qty sold (6 months)
    size_stock: dict,  # size -> current stock
    weekly_rate: float,
) -> dict:
    """
    Smart size allocation:
    1. Base target = total_pairs × weight
    2. Subtract penalised stock
    3. Rule: size 36 with any stock → 0
    4. Scale result back to total_pairs
    5. Rounding surplus → hottest size (42 men / 38 women)
    """
    sizes = size_range(article)
    weights = get_weights(article)

    total_stock_all = sum(size_stock.get(s, 0) for s in sizes)
    total_sold_6mo = sum(size_sold.get(s, 0) for s in sizes) or 1
    weekly_rate_adj = weekly_rate * SEASON_COEFF

    # WOS = total stock / adj weekly rate
    wos_model = total_stock_all / weekly_rate_adj if weekly_rate_adj > 0 else 999.0

    total_pairs = calc_total_pairs(weekly_rate)

    # --- per-size need calculation ---
    raw_needs = {}
    for sz in sizes:
        w = weights.get(sz, 0.01)
        base_target = total_pairs * w

        stock = size_stock.get(sz, 0)
        penalty_pct = wos_penalty(stock, wos_model)
        effective_stock = stock * (1.0 - penalty_pct)

        need = max(0.0, base_target - effective_stock)

        # rule: size 36 with any stock → 0
        if sz == "36" and stock > 0:
            need = 0.0

        raw_needs[sz] = need

    # Scale to total_pairs
    raw_total = sum(raw_needs.values())
    if raw_total <= 0:
        # All stock > needs; distribute evenly but still place the order
        raw_needs = {sz: weights.get(sz, 0.01) for sz in sizes}
        raw_total = sum(raw_needs.values())

    scale = total_pairs / raw_total
    result = {}
    for sz in sizes:
        # Don't place size 36 if it has stock
        if sz == "36" and size_stock.get(sz, 0) > 0:
            result[sz] = 0
        else:
            result[sz] = max(0, round(raw_needs[sz] * scale))

    # Fix rounding to exactly total_pairs
    diff = total_pairs - sum(result.values())
    # Hottest sizes for surplus/deficit adjustment
    if article in WOMEN_ONLY:
        hot_order = sorted(sizes, key=lambda s: weights.get(s, 0), reverse=True)
    elif article in MEN_ONLY:
        hot_order = sorted(sizes, key=lambda s: weights.get(s, 0), reverse=True)
    else:
        # Unisex: prefer 42 (men hottest) and 38 (women hottest)
        hot_order = sorted(sizes, key=lambda s: weights.get(s, 0), reverse=True)

    i = 0
    max_iter = len(sizes) * 20
    while diff != 0 and i < max_iter:
        sz = hot_order[i % len(hot_order)]
        if diff > 0:
            result[sz] += 1
            diff -= 1
        elif diff < 0 and result[sz] > 0:
            result[sz] -= 1
            diff += 1
        i += 1

    return result, total_pairs, wos_model


def check_photo(article: str) -> str:
    url = f"{SUPABASE_URL}/storage/v1/object/public/photos/{article}.jpg"
    try:
        r = requests.head(url, timeout=8)
        if r.status_code == 200:
            return url
    except Exception:
        pass
    return ""


def upload_order(items: list, dry_run: bool = False) -> str:
    key = SUPABASE_SERVICE_KEY or SUPABASE_KEY
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    payload = {
        "id": ORDER_ID,
        "status": "draft",
        "items": items,
        "meta": {
            "date": ORDER_DATE,
            "season": SEASON_COEFF,
        },
    }
    if dry_run:
        import json
        print("\n[DRY-RUN] Would POST:")
        print(json.dumps(payload, ensure_ascii=False, indent=2)[:2000])
        return ORDER_ID

    resp = requests.post(
        f"{SUPABASE_URL}/rest/v1/orders",
        headers=headers,
        json=payload,
        timeout=30,
    )
    if resp.status_code not in (200, 201):
        print(f"Upload error {resp.status_code}: {resp.text[:500]}")
        sys.exit(1)
    return ORDER_ID


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Print plan without uploading")
    args = parser.parse_args()

    load_env()
    if not SUPABASE_URL or not (SUPABASE_KEY or SUPABASE_SERVICE_KEY):
        print("Missing SUPABASE_URL / SUPABASE_KEY in .env")
        sys.exit(1)

    import duckdb
    con = duckdb.connect(str(DB_PATH), read_only=True)

    # -----------------------------------------------------------------------
    # Pull model names + weekly rate (6 months)
    # -----------------------------------------------------------------------
    SALES_START = "2025-09-22"
    WEEKS_WINDOW = 26.0  # 6 months

    model_data = {}
    for art in ARTICLES:
        row = con.execute("""
            SELECT
                LAST(REGEXP_REPLACE(product_name, ',\\s*\\d+(\\.\\d+)?$', '') ORDER BY sale_datetime) as model,
                CAST(SUM(CASE WHEN price>0 THEN quantity ELSE 0 END) AS INT) as qty_6mo
            FROM sales WHERE article=?
        """, [art]).fetchone()
        model_name = row[0] if row and row[0] else f"Article {art}"
        qty_6mo = int(row[1]) if row and row[1] else 0
        weekly_rate = round(qty_6mo / WEEKS_WINDOW, 2)
        model_data[art] = {"model": model_name, "qty_6mo": qty_6mo, "weekly_rate": weekly_rate}

    # -----------------------------------------------------------------------
    # Pull per-size sales (6 months)
    # -----------------------------------------------------------------------
    size_sold_data = {}
    for art in ARTICLES:
        rows = con.execute("""
            SELECT REGEXP_EXTRACT(product_name, ',\\s*(\\d+\\.?\\d*)$', 1) as size,
                   CAST(SUM(quantity) AS INT) as sold
            FROM sales
            WHERE article=? AND price>0 AND sale_datetime >= ?
            GROUP BY 1
        """, [art, SALES_START]).fetchall()
        size_sold_data[art] = {r[0]: r[1] for r in rows if r[0]}

    # -----------------------------------------------------------------------
    # Pull per-size stock from latest snapshot
    # -----------------------------------------------------------------------
    size_stock_data = {}
    size_msk_data = {}
    size_tsum_data = {}
    size_aru_data = {}
    for art in ARTICLES:
        rows = con.execute(f"""
            SELECT REGEXP_EXTRACT(product_name, ',\\s*(\\d+\\.?\\d*)$', 1) as size,
                   CAST(SUM(moscow) AS INT) as msk,
                   CAST(SUM(tsum + online) AS INT) as tsum_onl,
                   CAST(SUM(astana_aruzhan) AS INT) as aru,
                   CAST(SUM(main_warehouse) AS INT) as wh,
                   CAST(SUM(moscow+tsum+online+astana_aruzhan+main_warehouse) AS INT) as total
            FROM {SNAP_TABLE}
            WHERE article=?
            GROUP BY 1
        """, [art]).fetchall()
        ss, sm, st, sa = {}, {}, {}, {}
        for r in rows:
            if r[0]:
                ss[r[0]] = int(r[5])
                sm[r[0]] = int(r[1])
                st[r[0]] = int(r[2])
                sa[r[0]] = int(r[3])
        size_stock_data[art] = ss
        size_msk_data[art] = sm
        size_tsum_data[art] = st
        size_aru_data[art] = sa

    con.close()

    # -----------------------------------------------------------------------
    # Check photos in Supabase Storage
    # -----------------------------------------------------------------------
    print(f"Checking {len(ARTICLES)} photos in Supabase storage...")
    photos = {}
    for art in ARTICLES:
        photos[art] = check_photo(art)
        status = "OK" if photos[art] else "missing"
        print(f"  {art}: {status}")

    # -----------------------------------------------------------------------
    # Build items with size allocation
    # -----------------------------------------------------------------------
    print(f"\n{'='*80}")
    print(f"ORDER ЗК-006  |  date={ORDER_DATE}  |  season={SEASON_COEFF}x")
    print(f"{'='*80}")
    header = f"{'#':>2}  {'Article':<8}  {'Model':<40}  {'Rate':>5}  {'WOS':>5}  {'Zone':<8}  {'Pairs':>5}"
    print(header)
    print("-" * len(header))

    items = []
    total_pairs_all = 0

    for idx, art in enumerate(ARTICLES, 1):
        md = model_data[art]
        model_name = md["model"]
        weekly_rate = md["weekly_rate"]
        size_sold = size_sold_data[art]
        size_stock = size_stock_data[art]
        sizes = size_range(art)
        weights = get_weights(art)

        size_qty, total_pairs, wos_val = calc_size_qty(
            art, size_sold, size_stock, weekly_rate
        )

        # zone
        if wos_val < 3:
            zone = "critical"
        elif wos_val < 6:
            zone = "soon"
        else:
            zone = "nice"

        total_pairs_all += total_pairs

        # Build size dicts covering all sizes in the model's range
        def make_dict(source, sz_list):
            return {sz: source.get(sz, 0) for sz in sz_list}

        item = {
            "article": art,
            "model": model_name,
            "photo_url": photos[art],
            "size_qty": make_dict(size_qty, sizes),
            "size_stock": make_dict(size_stock, sizes),
            "size_sold": make_dict(size_sold, sizes),
            "size_msk": make_dict(size_msk_data[art], sizes),
            "size_tsum": make_dict(size_tsum_data[art], sizes),
            "size_aru": make_dict(size_aru_data[art], sizes),
            "pairs": total_pairs,
            "buy_price": 10200,
            "order_mode": "sizes",
            "zone": zone,
            "weekly_rate": weekly_rate,
            "adj_rate": round(weekly_rate * SEASON_COEFF, 2),
            "wos": round(wos_val, 1),
        }
        items.append(item)

        # Print summary line
        wos_str = f"{wos_val:.1f}" if wos_val < 999 else "∞"
        model_short = (model_name[:38] + "..") if len(model_name) > 40 else model_name
        print(f"{idx:>2}  {art:<8}  {model_short:<40}  {weekly_rate:>5.2f}  {wos_str:>5}  {zone:<8}  {total_pairs:>5}")

        # Print size allocation
        qty_line  = "    qty:   " + "  ".join(f"{sz}:{size_qty.get(sz,0):>2}" for sz in sizes)
        sold_line = "    sold:  " + "  ".join(f"{sz}:{size_sold.get(sz,0):>2}" for sz in sizes)
        stk_line  = "    stock: " + "  ".join(f"{sz}:{size_stock.get(sz,0):>2}" for sz in sizes)
        print(qty_line)
        print(sold_line)
        print(stk_line)
        print()

    print(f"{'='*80}")
    print(f"TOTAL: {len(items)} models, {total_pairs_all} pairs")
    print(f"{'='*80}")

    # -----------------------------------------------------------------------
    # Upload to Supabase
    # -----------------------------------------------------------------------
    if not args.dry_run:
        print(f"\nUploading order {ORDER_ID} to Supabase...")
        order_id = upload_order(items, dry_run=False)
        print(f"\nOrder created: {order_id}")
        print(f"\nBuyer link:")
        print(f"  {SITE_URL}/?id={ORDER_ID}&role=buyer")
        print(f"\nSupplier link (send to supplier):")
        print(f"  {SITE_URL}/?id={ORDER_ID}&role=supplier")
    else:
        upload_order(items, dry_run=True)
        print("\n[DRY-RUN] Order NOT uploaded.")


if __name__ == "__main__":
    main()
