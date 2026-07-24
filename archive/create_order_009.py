#!/usr/bin/env python3
"""
Create sneaker order ЗК-009 with per-size analytics and transit deduction.

Based on weekly-reorder skill + create_order_006.py approach.
Auto-detects articles from sales data, applies W1 check, transit from ЗК-008.

Usage:
    python sneaker-order/create_order_009.py --dry-run
    python sneaker-order/create_order_009.py
"""

import argparse
import math
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import requests

PNLPOWER_DIR = Path(__file__).parent.parent
DB_PATH = PNLPOWER_DIR / "data" / "pnlpower.duckdb"
ENV_PATH = PNLPOWER_DIR / ".env"

SUPABASE_URL = ""
SUPABASE_KEY = ""
SUPABASE_SERVICE_KEY = ""
SITE_URL = "https://yerlannof.github.io/sneaker-order"

TODAY = date.today()
ORDER_ID = "ЗК-009"
SEASON_COEFF = {
    1: 0.59, 2: 0.79, 3: 1.35, 4: 1.26, 5: 1.00, 6: 0.99,
    7: 0.79, 8: 1.33, 9: 1.08, 10: 1.06, 11: 0.91, 12: 0.86,
}[TODAY.month]

TRANSIT_ORDERS = ["ЗК-008"]
MIN_SOLD = 2
SALES_WINDOW = 35  # days for recent sales
TARGET_WEEKS = 8

EXCLUDE_PATTERNS = [
    '%Пакет%', '%Носки%', '%Футболк%', '%Штан%', '%Куртк%', '%Худи%',
    '%Шорт%', '%Рюкзак%', '%Сумк%', '%Шапк%', '%АКЦИЯ 1=2%', '%Обувь%',
    '%Очки%', '%Ремень%', '%Кепк%', '%Брюки%', '%Джоггер%', '%Доставка%',
    '%Zip Lock%', '%one size%', '%Ветровк%', '%Джинсы%', '%Бандана%',
    '%Longsleeve%', '%Трико%', '%Hoodie%', '%Костюм%', '%Рубашк%',
    '%Жилетк%', '%Fleece%', '%GO SEX%', '%Толстовк%', '%Свитшот%',
    '%Олимпийк%', '%Майк%', '%Поло%', '%Пальто%', '%Плащ%',
    '%Спортивн%', '%Костюм%', '%полосат%', '%на молнии%',
]

# Only include sneaker brands — additional filter after SQL
SNEAKER_BRANDS = [
    'nike', 'jordan', 'adidas', 'dunk', 'air max', 'air force',
    'puma', 'new balance', 'asics', 'converse', 'vans', 'reebok',
    'salomon', 'saucony', 'on cloud', 'yeezy', 'travis', 'nb ',
    'samba', 'superstar', 'blazer', 'cortez', 'pegasus', 'vomero',
    'initiator', 'v2k', 'v5 rnr', 'off-white',
]

# Article merges: old_article → new_article
# When articles were merged in MoySklad, old sales should count toward the new article
ARTICLE_MERGES = {
    '200404': '202644',  # AF1 Low White Z/K → AF1 Low White In
}

# Size weights from real 6-month sales data
WOMEN_WEIGHTS = {"36": 0.117, "37": 0.151, "38": 0.274, "39": 0.216, "40": 0.181, "41": 0.061}
MEN_WEIGHTS = {"40": 0.069, "41": 0.158, "42": 0.240, "43": 0.231, "44": 0.177, "45": 0.100}
UNISEX_WEIGHTS = {}
for sz, w in WOMEN_WEIGHTS.items():
    if int(sz) <= 39:
        UNISEX_WEIGHTS[sz] = w * 0.45
UNISEX_WEIGHTS["40"] = MEN_WEIGHTS["40"] * 0.55 + WOMEN_WEIGHTS.get("40", 0.181) * 0.45
for sz in ["41", "42", "43", "44"]:
    UNISEX_WEIGHTS[sz] = MEN_WEIGHTS[sz] * 0.55
_total = sum(UNISEX_WEIGHTS.values())
UNISEX_WEIGHTS = {k: v / _total for k, v in UNISEX_WEIGHTS.items()}


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


def fetch_transit():
    """Fetch transit stock from Supabase orders.
    Returns:
        transit_totals: {article: total_pairs}
        transit_details: {article: [{order_id, pairs, size_qty, buy_price}]}
    """
    if not SUPABASE_URL or not SUPABASE_KEY:
        return {}, {}
    transit_totals = {}
    transit_details = {}  # article → list of {order_id, pairs, size_qty, ...}
    for oid in TRANSIT_ORDERS:
        try:
            resp = requests.get(
                f"{SUPABASE_URL}/rest/v1/orders?select=items,confirmed_items,supplier_items&id=eq.{oid}",
                headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
                timeout=15,
            )
            if resp.ok and resp.json():
                o = resp.json()[0]
                items = o.get('confirmed_items') or o.get('supplier_items') or o.get('items', [])
                for it in items:
                    art = it.get('article', '')
                    pairs = it.get('pairs', 0)
                    if art and pairs > 0:
                        transit_totals[art] = transit_totals.get(art, 0) + pairs
                        if art not in transit_details:
                            transit_details[art] = []
                        transit_details[art].append({
                            "order_id": oid,
                            "pairs": pairs,
                            "size_qty": it.get('size_qty', {}),
                            "women_boxes": it.get('women_boxes', 0),
                            "men_boxes": it.get('men_boxes', 0),
                            "buy_price": it.get('buy_price', 0),
                        })
        except Exception as e:
            print(f"⚠️  Transit fetch error: {e}")
    return transit_totals, transit_details


def detect_gender(size_sold, size_stock):
    """Detect gender from size data."""
    all_sizes = set()
    for s in list(size_sold.keys()) + list(size_stock.keys()):
        try:
            all_sizes.add(int(float(s)))
        except (ValueError, TypeError):
            pass
    has_women = bool(all_sizes & {36, 37})
    has_men = bool(all_sizes & {43, 44, 45})
    if has_women and not has_men:
        return "women"
    elif has_men and not has_women:
        return "men"
    return "unisex"


def get_weights(gender):
    if gender == "women":
        return WOMEN_WEIGHTS
    elif gender == "men":
        return MEN_WEIGHTS
    return UNISEX_WEIGHTS


def size_range(gender):
    if gender == "women":
        return [str(s) for s in range(36, 42)]
    elif gender == "men":
        return [str(s) for s in range(40, 46)]
    return [str(s) for s in range(36, 45)]


def calc_total_pairs(adj_rate, w1):
    """Determine total pairs based on adjusted rate and W1."""
    if w1 <= 2:
        return 12  # test model — 2 boxes max
    if adj_rate >= 6:
        return 60  # super-hit: 10 boxes
    elif adj_rate >= 3:
        return 48  # hit: 8 boxes
    elif adj_rate >= 1.5:
        return 30  # good: 5 boxes
    elif adj_rate >= 0.5:
        return 18  # normal: 3 boxes
    return 12  # minimal: 2 boxes


actual_weeks_global = 4.71  # will be updated in main()

def calc_size_qty(gender, size_sold, size_stock, weekly_rate, w1, transit_pairs=0):
    """Smart size allocation with transit deduction and per-size WOS check."""
    sizes = size_range(gender)
    weights = get_weights(gender)

    total_stock = sum(size_stock.get(s, 0) for s in sizes)
    adj_rate = weekly_rate * SEASON_COEFF
    wos = (total_stock + transit_pairs) / adj_rate if adj_rate > 0 else 999.0

    # Don't order if effective WOS >= 10
    if wos >= 10:
        return {s: 0 for s in sizes}, 0, wos

    total_pairs = calc_total_pairs(adj_rate, w1)

    # Subtract transit from need
    effective_stock = total_stock + transit_pairs
    need = max(0, round(adj_rate * TARGET_WEEKS) - effective_stock)
    if need == 0 and wos < 8:
        need = 6  # at least 1 box
    elif need == 0:
        return {s: 0 for s in sizes}, 0, wos

    # Cap total_pairs to actual need
    total_pairs = min(total_pairs, max(6, math.ceil(need / 6) * 6))

    # Per-size allocation with per-size WOS check
    raw_needs = {}
    for sz in sizes:
        w = weights.get(sz, 0.01)
        base_target = total_pairs * w
        stock = size_stock.get(sz, 0)
        sold = size_sold.get(sz, 0)
        sz_weekly = sold / actual_weeks_global if actual_weeks_global > 0 else 0
        sz_wos = stock / (sz_weekly * SEASON_COEFF) if sz_weekly > 0 else (999 if stock > 0 else 0)

        # Skip sizes with WOS > 15 and stock > 2 (overstocked)
        if sz_wos > 15 and stock > 2:
            raw_needs[sz] = 0.0
            continue

        # Penalty: if model WOS > 4, discount existing stock less
        penalty = 0.0 if wos < 4 else (0.3 if wos < 8 else 0.6)
        effective = stock * (1.0 - penalty)
        raw_need = max(0.0, base_target - effective)
        # Size 36 with any stock → 0
        if sz == "36" and stock > 0:
            raw_need = 0.0
        raw_needs[sz] = raw_need

    raw_total = sum(raw_needs.values())
    if raw_total <= 0:
        raw_needs = {sz: weights.get(sz, 0.01) for sz in sizes}
        raw_total = sum(raw_needs.values())

    scale = total_pairs / raw_total
    result = {}
    for sz in sizes:
        if sz == "36" and size_stock.get(sz, 0) > 0:
            result[sz] = 0
        else:
            result[sz] = max(0, round(raw_needs[sz] * scale))

    # Fix rounding
    diff = total_pairs - sum(result.values())
    hot = sorted(sizes, key=lambda s: weights.get(s, 0), reverse=True)
    i = 0
    while diff != 0 and i < len(sizes) * 20:
        sz = hot[i % len(hot)]
        if diff > 0:
            result[sz] += 1
            diff -= 1
        elif diff < 0 and result[sz] > 0:
            result[sz] -= 1
            diff += 1
        i += 1

    return result, total_pairs, wos


def check_photo(article):
    url = f"{SUPABASE_URL}/storage/v1/object/public/photos/{article}.jpg"
    try:
        r = requests.head(url, timeout=8)
        if r.status_code == 200:
            return url
    except Exception:
        pass
    return ""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    load_env()
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("Missing SUPABASE_URL / SUPABASE_KEY")
        sys.exit(1)

    import duckdb
    con = duckdb.connect(str(DB_PATH), read_only=True)

    # Latest snapshot
    snap = con.execute("""
        SELECT table_name FROM information_schema.tables
        WHERE table_name LIKE 'inventory_snapshot_stores_%'
        ORDER BY table_name DESC LIMIT 1
    """).fetchone()[0]
    print(f"Снапшот: {snap}")

    # Transit
    transit, transit_details = fetch_transit()
    if transit:
        print(f"📦 В пути ({', '.join(TRANSIT_ORDERS)}): {len(transit)} артикулов, {sum(transit.values())} пар")

    # Find articles with sales >= MIN_SOLD in last SALES_WINDOW days
    start = (TODAY - timedelta(days=SALES_WINDOW)).isoformat()
    excl = " AND ".join(f"product_name NOT LIKE '{p}'" for p in EXCLUDE_PATTERNS)

    # Calculate actual days in window for accurate weekly rate
    last_sale_date = con.execute("SELECT MAX(sale_datetime)::DATE FROM sales").fetchone()[0]
    start_date = TODAY - timedelta(days=SALES_WINDOW)
    actual_days = (last_sale_date - start_date).days if last_sale_date else SALES_WINDOW
    actual_weeks = round(actual_days / 7.0, 2)
    print(f"Окно продаж: {start_date} → {last_sale_date} ({actual_days} дней, {actual_weeks} нед)")

    articles_data = con.execute(f"""
        SELECT article,
               LAST(REGEXP_REPLACE(product_name, ',\\s*\\d+(\\.\\d+)?$', '') ORDER BY sale_datetime) as model,
               CAST(SUM(quantity) AS INT) as sold_35d,
               ROUND(SUM(quantity) / {actual_weeks}, 2) as weekly_rate,
               ROUND(AVG(CASE WHEN price > 0 THEN price END)) as avg_price,
               ROUND(SUM(profit) * 100.0 / NULLIF(SUM(revenue), 0), 1) as margin,
               ROUND(AVG(CASE WHEN price > 0 THEN cogs END)) as avg_cogs
        FROM sales
        WHERE sale_datetime >= '{start}' AND price > 0 AND {excl}
          AND article IS NOT NULL AND article != ''
        GROUP BY article
        HAVING SUM(quantity) >= {MIN_SOLD}
        ORDER BY weekly_rate DESC
    """).fetchall()

    # Filter: only sneaker brands
    articles_data = [
        row for row in articles_data
        if any(b in (row[1] or '').lower() for b in SNEAKER_BRANDS)
    ]

    # Merge deprecated articles: combine sales into target article
    if ARTICLE_MERGES:
        merged_out = set()
        articles_by_art = {row[0]: row for row in articles_data}
        for old_art, new_art in ARTICLE_MERGES.items():
            if old_art in articles_by_art:
                old = articles_by_art[old_art]
                merged_out.add(old_art)
                if new_art in articles_by_art:
                    new = articles_by_art[new_art]
                    # Combine: sold, weekly_rate — sum; price, margin, cogs — keep new
                    combined_sold = old[2] + new[2]
                    combined_rate = round(old[3] + new[3], 2)
                    articles_by_art[new_art] = (
                        new[0], new[1], combined_sold, combined_rate,
                        new[4], new[5], new[6],
                    )
                    print(f"  🔗 Объединён {old_art} → {new_art}: +{old[2]} продаж (итого {combined_sold})")
                else:
                    # Old article exists but new doesn't — rename
                    articles_by_art[new_art] = (
                        new_art, old[1], old[2], old[3], old[4], old[5], old[6],
                    )
                    print(f"  🔗 Переименован {old_art} → {new_art}")
        articles_data = [row for row in articles_data if row[0] not in merged_out]
        # Update the merged target
        articles_data = [articles_by_art.get(row[0], row) for row in articles_data]

    print(f"Артикулов кроссовок с продажами >= {MIN_SOLD}: {len(articles_data)}")

    # Update global for per-size WOS calc
    global actual_weeks_global
    actual_weeks_global = actual_weeks

    # W1: first week sales for each article
    w1_data = {}
    for row in articles_data:
        art = row[0]
        first_sale = con.execute(f"""
            SELECT MIN(sale_datetime) FROM sales WHERE article = '{art}' AND price > 0
        """).fetchone()[0]
        if first_sale:
            w1 = con.execute(f"""
                SELECT COALESCE(SUM(quantity), 0) FROM sales
                WHERE article = '{art}' AND price > 0
                  AND sale_datetime >= '{first_sale}' AND sale_datetime < '{first_sale}'::TIMESTAMP + INTERVAL 7 DAY
            """).fetchone()[0]
            w1_data[art] = int(w1)
        else:
            w1_data[art] = 0

    # H1/H2 acceleration: compare first half vs second half of sales window
    midpoint = (start_date + timedelta(days=actual_days // 2)).isoformat()
    h2_start = midpoint
    accel_data = {}
    for row in articles_data:
        art = row[0]
        all_arts = [art] + [old for old, new in ARTICLE_MERGES.items() if new == art]
        af = ",".join(f"'{a}'" for a in all_arts)
        h = con.execute(f"""
            SELECT
                SUM(CASE WHEN sale_datetime < '{h2_start}' THEN quantity ELSE 0 END) as h1,
                SUM(CASE WHEN sale_datetime >= '{h2_start}' THEN quantity ELSE 0 END) as h2
            FROM sales WHERE article IN ({af}) AND price > 0 AND sale_datetime >= '{start}'
        """).fetchone()
        h1, h2 = int(h[0] or 0), int(h[1] or 0)
        accel_data[art] = (h1, h2)

    # Per-size data
    print("Загрузка размерных данных...")
    items = []
    total_pairs_all = 0
    skipped_wos = 0
    skipped_zero = 0

    for row in articles_data:
        art, model, sold_35d, weekly_rate, avg_price, margin, avg_cogs = row
        w1 = w1_data.get(art, 0)
        in_transit = transit.get(art, 0)

        # Acceleration adjustment: if H2 >> H1, use H2-based rate
        h1, h2 = accel_data.get(art, (0, 0))
        h2_days = actual_days - actual_days // 2
        h1_days = actual_days // 2
        accel_ratio = (h2 / h2_days) / (h1 / h1_days) if h1 > 0 and h1_days > 0 else (2.0 if h2 > 0 else 1.0)

        if accel_ratio >= 2.0 and h2 >= 3:
            # Model is accelerating — use H2 rate (more recent, more representative)
            weekly_rate_adj = round(h2 / (h2_days / 7.0), 2)
            rate_note = f"⚡ H2 rate {weekly_rate_adj}/нед (was {weekly_rate}, accel {accel_ratio:.1f}x)"
        elif accel_ratio <= 0.4 and h1 >= 3:
            # Model is decelerating — use H2 rate (lower, more honest)
            weekly_rate_adj = round(h2 / (h2_days / 7.0), 2)
            rate_note = f"📉 H2 rate {weekly_rate_adj}/нед (was {weekly_rate}, decel {accel_ratio:.1f}x)"
        else:
            weekly_rate_adj = weekly_rate
            rate_note = ""
        weekly_rate = weekly_rate_adj

        # Per-size sales (last 35 days) — include merged old articles
        all_articles = [art] + [old for old, new in ARTICLE_MERGES.items() if new == art]
        art_filter = ",".join(f"'{a}'" for a in all_articles)
        size_sold = {}
        for r in con.execute(f"""
            SELECT REGEXP_EXTRACT(product_name, ',\\s*(\\d+\\.?\\d*)$', 1) as sz,
                   CAST(SUM(quantity) AS INT) as sold
            FROM sales WHERE article IN ({art_filter}) AND price > 0 AND sale_datetime >= '{start}'
            GROUP BY 1
        """).fetchall():
            if r[0]:
                size_sold[r[0]] = r[1]

        # Per-size stock
        size_stock, size_msk, size_tsum, size_aru = {}, {}, {}, {}
        for r in con.execute(f"""
            SELECT REGEXP_EXTRACT(product_name, ',\\s*(\\d+\\.?\\d*)$', 1) as sz,
                   CAST(SUM(moscow+tsum+online+astana_aruzhan+main_warehouse) AS INT) as total,
                   CAST(SUM(moscow) AS INT) as msk,
                   CAST(SUM(tsum+online) AS INT) as tsum_onl,
                   CAST(SUM(astana_aruzhan) AS INT) as aru
            FROM {snap} WHERE article = '{art}' GROUP BY 1
        """).fetchall():
            if r[0]:
                # Clamp negatives to 0 — negative stock = MoySklad data issue
                size_stock[r[0]] = max(0, r[1])
                size_msk[r[0]] = max(0, r[2])
                size_tsum[r[0]] = max(0, r[3])
                size_aru[r[0]] = max(0, r[4])

        gender = detect_gender(size_sold, size_stock)
        sizes = size_range(gender)

        size_qty, total_pairs, wos = calc_size_qty(
            gender, size_sold, size_stock, weekly_rate, w1, in_transit
        )

        if wos >= 10:
            skipped_wos += 1
            continue
        if total_pairs == 0 or sum(size_qty.values()) == 0:
            skipped_zero += 1
            continue

        zone = "critical" if wos < 3 else ("soon" if wos < 6 else "nice")

        # Buy price from supply
        bp_row = con.execute(f"""
            SELECT LAST(price ORDER BY supply_moment)
            FROM supply_positions
            WHERE product_article = '{art}' AND agent_name = 'Поставщик In' AND supply_moment >= '2025-06-01'
        """).fetchone()
        buy_price = float(bp_row[0]) if bp_row and bp_row[0] else 0

        def make_dict(source, sz_list):
            return {sz: source.get(sz, 0) for sz in sz_list}

        item = {
            "article": art,
            "model": model,
            "photo_url": "",  # filled later
            "size_qty": make_dict(size_qty, sizes),
            "size_stock": make_dict(size_stock, sizes),
            "size_sold": make_dict(size_sold, sizes),
            "size_msk": make_dict(size_msk, sizes),
            "size_tsum": make_dict(size_tsum, sizes),
            "size_aru": make_dict(size_aru, sizes),
            "pairs": total_pairs,
            "buy_price": buy_price,
            "order_mode": "sizes",
            "zone": zone,
            "sold": sold_35d,
            "sold_period": f"{actual_days}д",
            "stock": sum(size_stock.values()),
            "in_transit": in_transit,
            "transit_detail": transit_details.get(art, []),
            "weekly_rate": float(weekly_rate),
            "adj_rate": round(float(weekly_rate) * SEASON_COEFF, 2),
            "wos": round(wos, 1),
            "w1": w1,
            "h1": h1,
            "h2": h2,
            "accel": round(accel_ratio, 1),
            "margin": float(margin) if margin else 0,
            "price": float(avg_price) if avg_price else 0,
            "cogs": float(avg_cogs) if avg_cogs else 0,
            "moscow": sum(size_msk.get(s, 0) for s in sizes),
            "tsum_online": sum(size_tsum.get(s, 0) for s in sizes),
            "aruzhan": sum(size_aru.get(s, 0) for s in sizes),
            "warehouse": 0,
        }
        items.append(item)
        total_pairs_all += total_pairs

    # Watchlist: accelerating models with WOS 8-20 that didn't make the order
    # These are shown to buyer with 0 pairs so they can manually add
    midpoint_date = (start_date + timedelta(days=actual_days // 2)).isoformat()
    h2_days_watch = actual_days - actual_days // 2
    ordered_articles = {it["article"] for it in items}

    watch_rows = con.execute(f"""
    WITH sa AS (
        SELECT article,
               LAST(REGEXP_REPLACE(product_name, ',\\s*\\d+(\\.\\d+)?$', '') ORDER BY sale_datetime) as model,
               CAST(SUM(quantity) AS INT) as sold,
               CAST(SUM(CASE WHEN sale_datetime < '{midpoint_date}' THEN quantity ELSE 0 END) AS INT) as h1,
               CAST(SUM(CASE WHEN sale_datetime >= '{midpoint_date}' THEN quantity ELSE 0 END) AS INT) as h2,
               ROUND(SUM(quantity) / {actual_weeks}, 2) as weekly_rate,
               ROUND(AVG(CASE WHEN price > 0 THEN price END)) as avg_price,
               ROUND(SUM(profit) * 100.0 / NULLIF(SUM(revenue), 0), 1) as margin,
               ROUND(AVG(CASE WHEN price > 0 THEN cogs END)) as avg_cogs
        FROM sales
        WHERE sale_datetime >= '{start}' AND price > 0 AND {excl}
          AND article IS NOT NULL AND article != ''
        GROUP BY article
        HAVING SUM(CASE WHEN sale_datetime >= '{midpoint_date}' THEN quantity ELSE 0 END) >= 5
    ),
    st AS (
        SELECT article,
               CAST(SUM(moscow+tsum+online+astana_aruzhan+main_warehouse) AS INT) as stock,
               CAST(SUM(moscow) AS INT) as msk,
               CAST(SUM(tsum+online) AS INT) as tsum_onl,
               CAST(SUM(astana_aruzhan) AS INT) as aru
        FROM {snap} WHERE article IS NOT NULL GROUP BY article
    )
    SELECT sa.article, sa.model, sa.sold, sa.h1, sa.h2,
           COALESCE(st.stock, 0), COALESCE(st.msk, 0), COALESCE(st.tsum_onl, 0), COALESCE(st.aru, 0),
           sa.weekly_rate, sa.avg_price, sa.margin, sa.avg_cogs
    FROM sa LEFT JOIN st ON sa.article = st.article
    WHERE (sa.h1 = 0 AND sa.h2 >= 5) OR (sa.h1 > 0 AND sa.h2 * 1.0 / sa.h1 >= 2.5 AND sa.h2 >= 5)
    ORDER BY sa.h2 DESC
    """).fetchall()

    watch_items = []
    for wr in watch_rows:
        w_art = wr[0]
        if w_art in ordered_articles or w_art in ARTICLE_MERGES:
            continue
        w_model = wr[1]
        if not any(b in (w_model or '').lower() for b in SNEAKER_BRANDS):
            continue
        w_stock = max(0, wr[5])
        w_h2 = wr[4]
        h2_weekly = round(w_h2 / (h2_days_watch / 7.0), 2)
        h2_adj = h2_weekly * SEASON_COEFF
        wos_h2 = round(w_stock / h2_adj, 1) if h2_adj > 0 else 999
        if wos_h2 > 20 or w_stock < 3:
            continue
        in_transit_w = transit.get(w_art, 0)
        wos_with_transit = round((w_stock + in_transit_w) / h2_adj, 1) if h2_adj > 0 else 999

        # Get size data for watchlist items
        w_size_sold, w_size_stock, w_size_msk, w_size_tsum, w_size_aru = {}, {}, {}, {}, {}
        for sr in con.execute(f"""
            SELECT REGEXP_EXTRACT(product_name, ',\\s*(\\d+\\.?\\d*)$', 1),
                   CAST(SUM(quantity) AS INT)
            FROM sales WHERE article = '{w_art}' AND price > 0 AND sale_datetime >= '{start}'
            GROUP BY 1
        """).fetchall():
            if sr[0]: w_size_sold[sr[0]] = sr[1]
        for sr in con.execute(f"""
            SELECT REGEXP_EXTRACT(product_name, ',\\s*(\\d+\\.?\\d*)$', 1),
                   CAST(SUM(moscow+tsum+online+astana_aruzhan+main_warehouse) AS INT),
                   CAST(SUM(moscow) AS INT), CAST(SUM(tsum+online) AS INT), CAST(SUM(astana_aruzhan) AS INT)
            FROM {snap} WHERE article = '{w_art}' GROUP BY 1
        """).fetchall():
            if sr[0]:
                w_size_stock[sr[0]] = max(0, sr[1])
                w_size_msk[sr[0]] = max(0, sr[2])
                w_size_tsum[sr[0]] = max(0, sr[3])
                w_size_aru[sr[0]] = max(0, sr[4])

        gender = detect_gender(w_size_sold, w_size_stock)
        sizes = size_range(gender)
        def make_d(src, szl): return {sz: src.get(sz, 0) for sz in szl}

        bp_row = con.execute(f"""
            SELECT LAST(price ORDER BY supply_moment) FROM supply_positions
            WHERE product_article = '{w_art}' AND agent_name = 'Поставщик In' AND supply_moment >= '2025-06-01'
        """).fetchone()

        watch_items.append({
            "article": w_art,
            "model": w_model,
            "photo_url": "",
            "size_qty": {sz: 0 for sz in sizes},
            "size_stock": make_d(w_size_stock, sizes),
            "size_sold": make_d(w_size_sold, sizes),
            "size_msk": make_d(w_size_msk, sizes),
            "size_tsum": make_d(w_size_tsum, sizes),
            "size_aru": make_d(w_size_aru, sizes),
            "pairs": 0,
            "buy_price": float(bp_row[0]) if bp_row and bp_row[0] else 0,
            "order_mode": "sizes",
            "zone": "watch",
            "sold": wr[2],
            "sold_period": f"{actual_days}д",
            "stock": w_stock,
            "in_transit": in_transit_w,
            "transit_detail": transit_details.get(w_art, []),
            "weekly_rate": float(wr[9]) if wr[9] else 0,
            "adj_rate": round(h2_adj, 2),
            "wos": round(wos_with_transit, 1),
            "w1": 0,
            "h1": wr[3],
            "h2": wr[4],
            "accel": round(wr[4] / max(wr[3], 0.5), 1),
            "margin": float(wr[11]) if wr[11] else 0,
            "price": float(wr[10]) if wr[10] else 0,
            "cogs": float(wr[12]) if wr[12] else 0,
            "moscow": max(0, wr[6]),
            "tsum_online": max(0, wr[7]),
            "aruzhan": max(0, wr[8]),
            "warehouse": 0,
        })

    # Check photos for watchlist
    for wi in watch_items:
        wi["photo_url"] = check_photo(wi["article"])

    if watch_items:
        items.extend(watch_items)
        print(f"👀 Новинки на разгоне: {len(watch_items)} моделей (0 пар, для ручной корректировки)")

    con.close()

    print(f"Пропущено: {skipped_wos} (WOS >= 10), {skipped_zero} (0 пар)")

    # Photos
    print(f"Проверка {len(items)} фото...")
    found = 0
    for item in items:
        url = check_photo(item["article"])
        if url:
            item["photo_url"] = url
            found += 1
    print(f"  Фото найдено: {found}/{len(items)}")

    # Missing photos — try to upload from MoySklad
    ms_token = os.environ.get("MOYSKLAD_TOKEN") or os.environ.get("MS_TOKEN")
    if not ms_token:
        with open(ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if line.startswith("MOYSKLAD_TOKEN=") or line.startswith("MS_TOKEN="):
                    ms_token = line.split("=", 1)[1].strip().strip('"').strip("'")

    missing_photos = [it for it in items if not it["photo_url"]]
    if missing_photos and ms_token:
        print(f"Загрузка {len(missing_photos)} недостающих фото из МойСклад...")
        storage_key = SUPABASE_SERVICE_KEY or SUPABASE_KEY
        uploaded = 0
        for it in missing_photos:
            art = it["article"]
            headers = {"Authorization": f"Bearer {ms_token}", "Accept-Encoding": "gzip"}
            try:
                r = requests.get(
                    f"https://api.moysklad.ru/api/remap/1.2/entity/product?limit=1&filter=article={art}",
                    headers=headers, timeout=10)
                if not r.ok or not r.json().get("rows"):
                    continue
                images_meta = r.json()["rows"][0].get("images", {}).get("meta", {})
                if not images_meta.get("href") or images_meta.get("size", 0) == 0:
                    continue
                img_r = requests.get(images_meta["href"], headers=headers, timeout=10)
                if not img_r.ok:
                    continue
                img_rows = img_r.json().get("rows", [])
                if not img_rows:
                    continue
                download_url = img_rows[0].get("meta", {}).get("downloadHref")
                if not download_url:
                    continue
                img_data = requests.get(download_url, headers=headers, timeout=15)
                if not img_data.ok:
                    continue
                try:
                    from PIL import Image as PILImage
                    from io import BytesIO
                    img = PILImage.open(BytesIO(img_data.content))
                    img.thumbnail((800, 800))
                    buf = BytesIO()
                    img.save(buf, "JPEG", quality=92)
                    img_bytes = buf.getvalue()
                except Exception:
                    img_bytes = img_data.content
                up = requests.post(
                    f"{SUPABASE_URL}/storage/v1/object/photos/{art}.jpg",
                    headers={
                        "Authorization": f"Bearer {storage_key}",
                        "Content-Type": "image/jpeg",
                        "x-upsert": "true",
                    },
                    data=img_bytes, timeout=15,
                )
                if up.status_code in (200, 201):
                    it["photo_url"] = f"{SUPABASE_URL}/storage/v1/object/public/photos/{art}.jpg"
                    uploaded += 1
            except Exception:
                pass
        print(f"  Загружено: {uploaded} новых фото")

    # Sort by zone priority, then adj_rate
    zone_order = {"critical": 0, "soon": 1, "nice": 2}
    items.sort(key=lambda x: (zone_order.get(x["zone"], 3), -x["adj_rate"]))

    # Print summary
    print(f"\n{'='*100}")
    print(f"ЗАКАЗ {ORDER_ID}  |  {TODAY}  |  сезон={SEASON_COEFF}x  |  транзит={sum(transit.values())}п")
    print(f"{'='*100}")
    print(f"{'#':>3} {'Арт':>8} {'Модель':40s} {'Прод':>4} {'H1':>3}{'→H2':>4} {'Ост':>4} {'Путь':>4} {'WOS':>5} {'Зона':8s} {'Пар':>4}")
    print("-" * 110)

    for i, it in enumerate(items, 1):
        m = it['model'][:40]
        accel_mark = "⚡" if it.get('accel', 1) >= 2.0 else ("📉" if it.get('accel', 1) <= 0.4 else "  ")
        print(f"{i:3d} {it['article']:>8s} {m:40s} {it['sold']:4d} {it.get('h1',0):3d}→{it.get('h2',0):3d} {it['stock']:4d} {it['in_transit']:4d} {it['wos']:5.1f} {it['zone']:8s} {it['pairs']:4d} {accel_mark}")
        sizes = list(it['size_qty'].keys())
        qty_line  = "     заказ: " + " ".join(f"{s}:{it['size_qty'].get(s,0):>2}" for s in sizes)
        sold_line = "     прод:  " + " ".join(f"{s}:{it['size_sold'].get(s,0):>2}" for s in sizes)
        stk_line  = "     ост:   " + " ".join(f"{s}:{it['size_stock'].get(s,0):>2}" for s in sizes)
        msk_line  = "     мск:   " + " ".join(f"{s}:{it['size_msk'].get(s,0):>2}" for s in sizes)
        print(qty_line)
        print(sold_line)
        print(stk_line)
        print(msk_line)
        print()

    print(f"{'='*100}")
    print(f"ИТОГО: {len(items)} моделей, {total_pairs_all} пар")
    by_zone = {}
    for it in items:
        z = it['zone']
        by_zone[z] = by_zone.get(z, {'n': 0, 'p': 0})
        by_zone[z]['n'] += 1
        by_zone[z]['p'] += it['pairs']
    for z in ['critical', 'soon', 'nice']:
        if z in by_zone:
            print(f"  {z:10s}: {by_zone[z]['n']:3d} моделей, {by_zone[z]['p']:4d} пар")
    print(f"{'='*100}")

    # Upload
    if not args.dry_run:
        key = SUPABASE_SERVICE_KEY or SUPABASE_KEY
        resp = requests.post(
            f"{SUPABASE_URL}/rest/v1/orders",
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            },
            json={
                "id": ORDER_ID,
                "status": "draft",
                "items": items,
                "meta": {
                    "date": TODAY.isoformat(),
                    "season": SEASON_COEFF,
                    "order_mode": "sizes",
                    "transit_orders": TRANSIT_ORDERS,
                    "transit_pairs": sum(transit.values()),
                    "sales_window": f"{actual_days} дней ({start_date} — {last_sale_date})",
                    "actual_weeks": actual_weeks,
                },
            },
            timeout=30,
        )
        if resp.status_code not in (200, 201):
            print(f"Ошибка: {resp.status_code} {resp.text[:500]}")
            sys.exit(1)

        print(f"\nЗаказ создан: {ORDER_ID}")
        print(f"\nЗакупщик:")
        print(f"  {SITE_URL}/?id={ORDER_ID}&role=buyer")
        print(f"\nПоставщик:")
        print(f"  {SITE_URL}/?id={ORDER_ID}&role=supplier")
    else:
        print(f"\n[DRY-RUN] Заказ НЕ загружен.")


if __name__ == "__main__":
    main()
