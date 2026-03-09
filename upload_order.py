#!/usr/bin/env python3
"""
Генерация заказа кроссовок и загрузка в Supabase.

Использование:
    # Из папки pnlpower:
    python ../sneaker-order/upload_order.py --min-sold 5

    # Без фото (быстро, для теста):
    python ../sneaker-order/upload_order.py --min-sold 5 --no-photos

Выведет две ссылки:
    - Закупщик: https://yerlannof.github.io/sneaker-order/?id=xxx&role=buyer
    - Поставщик: https://yerlannof.github.io/sneaker-order/?id=xxx&role=supplier
"""

import argparse
import base64
import json
import os
import sys
import uuid
import requests
from datetime import date, timedelta
from io import BytesIO
from pathlib import Path

try:
    from PIL import Image as PILImage
except ImportError:
    PILImage = None

# Paths — run from pnlpower directory
PNLPOWER_DIR = Path(__file__).parent.parent / "pnlpower"
if not PNLPOWER_DIR.exists():
    PNLPOWER_DIR = Path.cwd()

DB_PATH = PNLPOWER_DIR / "data" / "pnlpower.duckdb"
ENV_PATH = PNLPOWER_DIR / ".env"

# Supabase config — from env or .env
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

SITE_URL = "https://yerlannof.github.io/sneaker-order"

SEASONALITY = {
    1: 0.59, 2: 0.79, 3: 1.35, 4: 1.26, 5: 1.00, 6: 0.99,
    7: 0.79, 8: 1.33, 9: 1.08, 10: 1.06, 11: 0.91, 12: 0.86,
}

EXCLUDE_PATTERNS = [
    '%Пакет%', '%Носки%', '%Футболк%', '%Штан%', '%Куртк%', '%Худи%',
    '%Шорт%', '%Рюкзак%', '%Сумк%', '%Шапк%', '%АКЦИЯ 1=2%', '%Обувь%',
    '%Очки%', '%Ремень%', '%Кепк%', '%Брюки%', '%Джоггер%', '%Доставка%',
    '%Zip Lock%', '%one size%',
]


def load_env():
    global SUPABASE_URL, SUPABASE_KEY, SUPABASE_SERVICE_KEY
    # Try sneaker-order/.env first, then pnlpower/.env
    for env_path in [Path(__file__).parent / ".env", ENV_PATH]:
        if env_path.exists():
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("SUPABASE_URL="):
                        SUPABASE_URL = SUPABASE_URL or line.split("=", 1)[1].strip().strip('"')
                    elif line.startswith("SUPABASE_KEY="):
                        SUPABASE_KEY = SUPABASE_KEY or line.split("=", 1)[1].strip().strip('"')
                    elif line.startswith("SUPABASE_SERVICE_KEY="):
                        SUPABASE_SERVICE_KEY = SUPABASE_SERVICE_KEY or line.split("=", 1)[1].strip().strip('"')


def get_moysklad_token():
    token = os.environ.get("MOYSKLAD_TOKEN") or os.environ.get("MS_TOKEN")
    if token:
        return token
    if ENV_PATH.exists():
        with open(ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if line.startswith("MOYSKLAD_TOKEN=") or line.startswith("MS_TOKEN="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def fetch_image_bytes(article, token):
    """Скачать фото товара из МойСклад. Возвращает JPEG bytes."""
    headers = {"Authorization": f"Bearer {token}", "Accept-Encoding": "gzip"}
    try:
        r = requests.get(
            f"https://api.moysklad.ru/api/remap/1.2/entity/product?limit=1&filter=article={article}",
            headers=headers, timeout=10)
        if not r.ok or not r.json().get("rows"):
            return None
        images_meta = r.json()["rows"][0].get("images", {}).get("meta", {})
        if not images_meta.get("href") or images_meta.get("size", 0) == 0:
            return None
        img_r = requests.get(images_meta["href"], headers=headers, timeout=10)
        if not img_r.ok:
            return None
        img_rows = img_r.json().get("rows", [])
        if not img_rows:
            return None
        download_url = img_rows[0].get("meta", {}).get("downloadHref")
        if not download_url:
            return None
        img_data = requests.get(download_url, headers=headers, timeout=15)
        if not img_data.ok:
            return None
        # Resize to 800px max side — good quality for lightbox, reasonable size
        if PILImage:
            img = PILImage.open(BytesIO(img_data.content))
            img.thumbnail((800, 800))
            buf = BytesIO()
            img.save(buf, "JPEG", quality=92)
            return buf.getvalue()
        return img_data.content
    except Exception:
        return None


def calc_boxes(sizes_sold, sizes_stock, weekly_rate, season_coeff, weeks):
    all_sizes = set()
    for s in list(sizes_sold.keys()) + list(sizes_stock.keys()):
        try:
            all_sizes.add(int(float(s)))
        except (ValueError, TypeError):
            pass
    has_women = bool(all_sizes & {36, 37})
    has_men = bool(all_sizes & {43, 44})
    target = int(round(weekly_rate * season_coeff * weeks))
    current = sum(sizes_stock.get(str(s), 0) for s in all_sizes)
    need = max(0, target - current)
    if need == 0:
        return 0, 0
    if has_women and has_men:
        tb = max(1, round(need / 6))
        return tb // 2, tb - tb // 2
    elif has_women:
        return max(1, round(need / 6)), 0
    else:
        return 0, max(1, round(need / 6))


def generate_order(weeks=8, min_sold=3, with_photos=True):
    import duckdb
    con = duckdb.connect(str(DB_PATH), read_only=True)
    today = date.today()

    snap = con.execute("""
        SELECT table_name FROM information_schema.tables
        WHERE table_name LIKE 'inventory_snapshot_stores_%'
        ORDER BY table_name DESC LIMIT 1
    """).fetchone()
    if not snap:
        print("Нет снапшотов!")
        sys.exit(1)
    snap = snap[0]

    sc = SEASONALITY.get(today.month, 1.0)
    start = today - timedelta(days=35)
    sw = 5.0
    excl = " AND ".join(f"product_name NOT LIKE '{p}'" for p in EXCLUDE_PATTERNS)

    # FIX: JOIN по article — товары переименовываются в МойСклад!
    rows = con.execute(f"""
    WITH sa AS (
        SELECT
            article,
            LAST(REGEXP_REPLACE(product_name, ',\\s*\\d+(\\.\\d+)?$', '') ORDER BY sale_datetime) as model,
            CAST(SUM(quantity) AS INT) as qty_sold,
            ROUND(SUM(quantity)/{sw}, 1) as weekly_rate,
            ROUND(SUM(quantity)/{sw}*{sc}, 1) as adj_rate,
            ROUND(AVG(CASE WHEN price>0 THEN price END)) as avg_price,
            ROUND(SUM(profit)*100.0/NULLIF(SUM(revenue),0),1) as margin_pct,
            ROUND(AVG(CASE WHEN price>0 THEN cogs END)) as avg_cogs
        FROM sales
        WHERE sale_datetime>='{start}' AND price>0 AND {excl}
          AND article IS NOT NULL AND article != ''
        GROUP BY 1 HAVING SUM(quantity)>={min_sold}
    ),
    st AS (
        SELECT
            article,
            LAST(REGEXP_REPLACE(product_name, ',\\s*\\d+(\\.\\d+)?$', '') ORDER BY product_name) as model,
            CAST(SUM(moscow) AS INT) as moscow,
            CAST(SUM(tsum+online) AS INT) as tsum_online,
            CAST(SUM(astana_aruzhan) AS INT) as aruzhan,
            CAST(SUM(main_warehouse) AS INT) as warehouse,
            CAST(SUM(moscow+tsum+online+astana_aruzhan+main_warehouse) AS INT) as total
        FROM {snap}
        WHERE article IS NOT NULL AND article != ''
        GROUP BY 1
    )
    SELECT COALESCE(st.model, sa.model) as model, sa.article, sa.qty_sold, sa.weekly_rate, sa.adj_rate,
        COALESCE(st.total,0), COALESCE(st.moscow,0), COALESCE(st.tsum_online,0),
        COALESCE(st.aruzhan,0), COALESCE(st.warehouse,0),
        CASE WHEN sa.adj_rate>0 THEN ROUND(COALESCE(st.total,0)/sa.adj_rate,1) ELSE 999 END,
        sa.margin_pct, sa.avg_price, sa.avg_cogs
    FROM sa LEFT JOIN st ON sa.article=st.article
    WHERE CASE WHEN sa.adj_rate>0 THEN COALESCE(st.total,0)/sa.adj_rate ELSE 999 END < 10
    ORDER BY sa.adj_rate DESC
    """).fetchall()

    # Articles — search ALL suppliers first, then prefer "Поставщик In" if available
    articles = {}
    # 1) All suppliers (fallback)
    for ar in con.execute("""
        SELECT REGEXP_REPLACE(product_name, ',\\s*\\d+(\\.\\d+)?$', '') as model,
               LAST(product_article ORDER BY supply_moment) as article
        FROM supply_positions
        WHERE supply_moment>='2025-06-01' AND product_article IS NOT NULL AND product_article != ''
        GROUP BY 1
    """).fetchall():
        if ar[1]:
            articles[ar[0]] = ar[1]
    # 2) Also check sales table for articles not found in supplies
    for ar in con.execute("""
        SELECT REGEXP_REPLACE(product_name, ',\\s*\\d+(\\.\\d+)?$', '') as model,
               LAST(article ORDER BY sale_datetime) as article
        FROM sales
        WHERE sale_datetime>='2025-10-01' AND article IS NOT NULL AND article != ''
        GROUP BY 1
    """).fetchall():
        if ar[1] and ar[0] not in articles:
            articles[ar[0]] = ar[1]
    # 3) Override with "Поставщик In" articles (preferred, most recent)
    for ar in con.execute("""
        SELECT REGEXP_REPLACE(product_name, ',\\s*\\d+(\\.\\d+)?$', '') as model,
               LAST(product_article ORDER BY supply_moment) as article
        FROM supply_positions
        WHERE agent_name='Поставщик In' AND supply_moment>='2025-10-01'
              AND product_article IS NOT NULL AND product_article != ''
        GROUP BY 1
    """).fetchall():
        if ar[1]:
            articles[ar[0]] = ar[1]

    # Buy prices (from "Поставщик In" only — that's the actual buy price)
    buy_prices = {}
    for bp in con.execute("""
        SELECT product_article, LAST(price ORDER BY supply_moment) as last_price
        FROM supply_positions
        WHERE agent_name='Поставщик In' AND supply_moment>='2025-06-01'
        GROUP BY product_article
    """).fetchall():
        buy_prices[bp[0]] = float(bp[1]) if bp[1] else 0

    # Sizes — JOIN по article
    size_data = {}
    for r in rows:
        art = r[1].replace("'", "''") if r[1] else ''
        sold = con.execute(f"""
            SELECT REGEXP_EXTRACT(product_name, ',\\s*(\\d+\\.?\\d*)$', 1), CAST(SUM(quantity) AS INT)
            FROM sales WHERE sale_datetime>='{start}' AND price>0
              AND article='{art}'
            GROUP BY 1
        """).fetchall()
        stk = con.execute(f"""
            SELECT REGEXP_EXTRACT(product_name, ',\\s*(\\d+\\.?\\d*)$', 1),
                   CAST(SUM(moscow+tsum+online+astana_aruzhan+main_warehouse) AS INT)
            FROM {snap}
            WHERE article='{art}'
            GROUP BY 1
        """).fetchall()
        size_data[r[0]] = ({s[0]: s[1] for s in sold}, {s[0]: s[1] for s in stk})
    con.close()

    # Photos → upload to Supabase Storage (800px, good quality)
    token = get_moysklad_token() if with_photos else None
    storage_key = SUPABASE_SERVICE_KEY or SUPABASE_KEY
    photos = {}  # article → public URL
    if with_photos and token:
        # Only upload photos for models in the order (not all 1000+ articles)
        order_models = set(r[0] for r in rows if r[10] < 10)
        unique_articles = set(articles[m] for m in order_models if m in articles and articles[m])
        print(f"Загрузка {len(unique_articles)} фото...")
        uploaded = 0
        cached = 0
        for i, art in enumerate(unique_articles):
            pub_url = f"{SUPABASE_URL}/storage/v1/object/public/photos/{art}.jpg"
            # Check if already in storage
            try:
                check = requests.head(pub_url, timeout=10)
                if check.status_code == 200:
                    photos[art] = pub_url
                    cached += 1
                    continue
            except Exception:
                pass
            if True:
                img_bytes = fetch_image_bytes(art, token)
                if img_bytes:
                    up = requests.post(
                        f"{SUPABASE_URL}/storage/v1/object/photos/{art}.jpg",
                        headers={
                            "Authorization": f"Bearer {storage_key}",
                            "Content-Type": "image/jpeg",
                            "x-upsert": "true",
                        },
                        data=img_bytes,
                        timeout=15,
                    )
                    if up.status_code in (200, 201):
                        photos[art] = pub_url
                        uploaded += 1
            if (i + 1) % 10 == 0:
                print(f"  {i+1}/{len(unique_articles)}...")
        print(f"  Загружено: {uploaded} новых, {cached} из кэша")

    # Build items
    # Индексы: 0=model, 1=article, 2=qty_sold, 3=weekly_rate, 4=adj_rate,
    #   5=total, 6=moscow, 7=tsum_online, 8=aruzhan, 9=warehouse, 10=wos,
    #   11=margin, 12=avg_price, 13=avg_cogs
    items = []
    for r in rows:
        if r[10] >= 10:
            continue
        model = r[0]
        article = r[1] or articles.get(model, "")
        ss, sk = size_data.get(model, ({}, {}))
        wb, mb = calc_boxes(ss, sk, float(r[3]), sc, weeks)
        zone = "critical" if r[10] < 3 else ("soon" if r[10] < 6 else "nice")
        bp = buy_prices.get(article, 0)
        items.append({
            "model": model,
            "article": article,
            "photo_url": photos.get(article, ""),
            "women_boxes": wb,
            "men_boxes": mb,
            "pairs": wb * 6 + mb * 6,
            "zone": zone,
            "sold": r[2],
            "weekly_rate": float(r[3]),
            "adj_rate": float(r[4]),
            "stock": r[5],
            "wos": float(r[10]),
            "margin": float(r[11]) if r[11] else 0,
            "price": float(r[12]) if r[12] else 0,
            "cogs": float(r[13]) if r[13] else 0,
            "buy_price": bp,
            "moscow": r[6],
            "tsum_online": r[7],
            "aruzhan": r[8],
            "warehouse": r[9],
        })

    # Merge duplicates by article (e.g. Rose Whisper Z / Rose Gold Z / Rose Gold In = same shoe)
    merged = {}
    for it in items:
        art = it["article"]
        if not art or art not in merged:
            merged[art or it["model"]] = it
        else:
            m = merged[art]
            m["sold"] += it["sold"]
            m["stock"] += it["stock"]
            m["moscow"] += it["moscow"]
            m["tsum_online"] += it["tsum_online"]
            m["aruzhan"] += it["aruzhan"]
            m["warehouse"] += it["warehouse"]
            m["weekly_rate"] = round(m["weekly_rate"] + it["weekly_rate"], 1)
            m["adj_rate"] = round(m["adj_rate"] + it["adj_rate"], 1)
            # Recalc WOS and boxes with merged data
            m["wos"] = round(m["stock"] / m["adj_rate"], 1) if m["adj_rate"] > 0 else 999
            m["zone"] = "critical" if m["wos"] < 3 else ("soon" if m["wos"] < 6 else "nice")
            # Keep better margin, lower cogs
            if it["margin"] > m["margin"]:
                m["margin"] = it["margin"]
            # Recalc boxes with merged rates
            all_ss, all_sk = {}, {}
            for src_model in [m["model"], it["model"]]:
                ss, sk = size_data.get(src_model, ({}, {}))
                for s, v in ss.items():
                    all_ss[s] = all_ss.get(s, 0) + v
                for s, v in sk.items():
                    all_sk[s] = all_sk.get(s, 0) + v
            wb, mb = calc_boxes(all_ss, all_sk, m["weekly_rate"], sc, weeks)
            m["women_boxes"] = wb
            m["men_boxes"] = mb
            m["pairs"] = wb * 6 + mb * 6
            # Use shorter name
            if len(it["model"]) < len(m["model"]):
                m["model"] = it["model"]
    items = list(merged.values())

    return items, {"date": today.strftime("%d.%m.%Y"), "season": sc, "weeks": weeks, "snap": snap}


def get_next_order_number():
    """Get next sequential order number (ЗК-001, ЗК-002, ...)."""
    try:
        resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/orders?select=id&id=like.ЗК-*&order=id.desc&limit=1",
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
            },
            timeout=10,
        )
        if resp.ok:
            rows = resp.json()
            if rows:
                last = rows[0]["id"]  # e.g. "ЗК-005"
                num = int(last.split("-")[1])
                return f"ЗК-{num + 1:03d}"
    except Exception:
        pass
    return "ЗК-001"


def upload_to_supabase(items, meta):
    order_id = get_next_order_number()

    resp = requests.post(
        f"{SUPABASE_URL}/rest/v1/orders",
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        },
        json={
            "id": order_id,
            "status": "draft",
            "items": items,
            "meta": meta,
        },
        timeout=30,
    )

    if resp.status_code not in (200, 201):
        print(f"Ошибка загрузки: {resp.status_code}")
        print(resp.text[:500])
        sys.exit(1)

    return order_id


def main():
    parser = argparse.ArgumentParser(description="Генерация и загрузка заказа кроссовок")
    parser.add_argument("--weeks", type=int, default=8)
    parser.add_argument("--min-sold", type=int, default=3)
    parser.add_argument("--no-photos", action="store_true")
    args = parser.parse_args()

    load_env()

    if not SUPABASE_URL or not SUPABASE_KEY:
        print("Нужны SUPABASE_URL и SUPABASE_KEY")
        print("Добавь в .env файл или в переменные окружения")
        sys.exit(1)

    print("Генерация заказа...")
    items, meta = generate_order(weeks=args.weeks, min_sold=args.min_sold, with_photos=not args.no_photos)

    total_w = sum(i['women_boxes'] for i in items)
    total_m = sum(i['men_boxes'] for i in items)
    print(f"\n{len(items)} моделей, {total_w} жен + {total_m} муж = {total_w*6 + total_m*6} пар")

    print("\nЗагрузка в Supabase...")
    order_id = upload_to_supabase(items, meta)

    print(f"\n{'='*60}")
    print(f"Заказ создан: {order_id}")
    print(f"{'='*60}")
    print(f"\nЗакупщик:")
    print(f"  {SITE_URL}/?id={order_id}&role=buyer")
    print(f"\nПоставщик (кинуть в WhatsApp):")
    print(f"  {SITE_URL}/?id={order_id}&role=supplier")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
