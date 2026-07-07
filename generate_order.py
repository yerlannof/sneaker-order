#!/usr/bin/env python3
"""
КАНОНИЧЕСКИЙ генератор заказа кроссовок (v1, июль 2026).

Заменяет upload_order.py / build_reorder.py / reorder_analysis.py — единая
формула, исправляющая находки аудита 07.07.2026 (см. память reorder-audit-jul2026):

1. Сезонность считается от ОКНА ПРОДАЖ (прибытие + weeks), а не от текущего месяца.
   Наблюдаемый темп деасезонализируется по коэффициентам окна наблюдения.
2. Лид-тайм поставки (--lead-weeks, default 3) заложен в потребность.
3. Транзит — только заказы из Supabase не старше 8 недель (фантомные не считаются).
4. Скидки из prices_snapshot: >=50% — ликвидация, в заказ НЕ идёт (отчёт отдельно);
   10-49% — маркер в имени модели, Алуа видит что скорость искусственная.
5. Блок «распроданных хитов»: продано >=15 за 180д, сток <=3, текущих продаж нет —
   раньше были невидимы фильтру.
6. Возвраты: темп умножается на факт. коэффициент net/gross из
   sales_by_employee_correct (~0.95).
7. Размеры: целевой запас по глобальным весам пола МИНУС текущий сток размера
   (выбитые размеры получают долю, затаренные — 0). 36-й не заказываем, если есть в стоке.
8. Капы по темпу (54/42/30/24 пар) + «пробник» 12 пар для непроверенных моделей.

Использование (из папки pnlpower):
    python3 sneaker-order/generate_order.py --dry-run          # посчитать, показать, JSON в файл
    python3 sneaker-order/generate_order.py                    # создать заказ в Supabase + фото
    python3 sneaker-order/generate_order.py --weeks 8 --lead-weeks 3 --no-photos
"""

import argparse
import json
import os
import sys
from datetime import date, timedelta
from io import BytesIO
from pathlib import Path

import requests

try:
    from PIL import Image as PILImage
except ImportError:
    PILImage = None

PNLPOWER_DIR = Path(__file__).parent.parent / "pnlpower"
if not PNLPOWER_DIR.exists():
    PNLPOWER_DIR = Path.cwd()
DB_PATH = PNLPOWER_DIR / "data" / "pnlpower.duckdb"
ENV_PATHS = [Path(__file__).parent / ".env", PNLPOWER_DIR / ".env"]

SITE_URL = "https://yerlannof.github.io/sneaker-order"

SEASON = {1: 0.59, 2: 0.79, 3: 1.35, 4: 1.26, 5: 1.00, 6: 0.99,
          7: 0.79, 8: 1.33, 9: 1.08, 10: 1.06, 11: 0.91, 12: 0.86}

# Глобальные веса размеров (6 мес реальных данных, size_ordering_strategy.md)
W_WEIGHTS = {'36': 0.12, '37': 0.15, '38': 0.27, '39': 0.22, '40': 0.18, '41': 0.06}
M_WEIGHTS = {'40': 0.07, '41': 0.16, '42': 0.24, '43': 0.23, '44': 0.18, '45': 0.10}

DISCOUNT_EXCLUDE = 50   # скидка >= X% = ликвидация, в заказ не включаем
DISCOUNT_FLAG = 10      # скидка >= X% = маркер в имени
REVIVAL_MIN_180D = 15   # порог «распроданного хита»
TRANSIT_MAX_AGE_WEEKS = 8

env_cache = {}


def env(key):
    if not env_cache:
        for p in ENV_PATHS:
            if p.exists():
                for line in p.read_text().splitlines():
                    line = line.strip()
                    if '=' in line and not line.startswith('#'):
                        k, v = line.split('=', 1)
                        env_cache.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return os.environ.get(key) or env_cache.get(key, '')


# ---------------------------------------------------------------- сезонность

def mean_coef(start, days):
    """Средний сезонный коэффициент за days дней от start."""
    return sum(SEASON[(start + timedelta(days=i)).month] for i in range(days)) / days


def coef_weeks(start, weeks):
    """Сумма «коэффициенто-недель» за weeks недель от start.
    base_rate * coef_weeks = ожидаемые продажи за период."""
    days = int(round(weeks * 7))
    return sum(SEASON[(start + timedelta(days=i)).month] for i in range(days)) / 7.0


# ---------------------------------------------------------------- транзит

def fetch_transit(max_age_weeks=TRANSIT_MAX_AGE_WEEKS):
    """Заказы в пути: только sent/supplier_done НЕ СТАРШЕ max_age_weeks.
    Возвращает ({article: pairs}, {article: [{order_id, pairs, size_qty}]}, [order_ids])."""
    url, key = env('SUPABASE_URL'), env('SUPABASE_KEY')
    if not url or not key:
        return {}, {}, []
    cutoff = (date.today() - timedelta(weeks=max_age_weeks)).isoformat()
    try:
        resp = requests.get(
            f"{url}/rest/v1/orders?select=id,status,created_at,items,confirmed_items,supplier_items"
            f"&status=in.(sent,supplier_done)&created_at=gte.{cutoff}",
            headers={"apikey": key, "Authorization": f"Bearer {key}"}, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        print(f"⚠️  Транзит из Supabase не загружен: {e}")
        return {}, {}, []
    transit, detail, ids = {}, {}, []
    for o in resp.json():
        ids.append(o['id'])
        items = o.get('confirmed_items') or o.get('supplier_items') or o.get('items') or []
        for it in items:
            art, pairs = it.get('article', ''), 0
            if it.get('size_qty'):
                pairs = sum(v or 0 for v in it['size_qty'].values())
            else:
                pairs = it.get('pairs', 0)
            if art and pairs > 0:
                transit[art] = transit.get(art, 0) + pairs
                detail.setdefault(art, []).append(
                    {'order_id': o['id'], 'pairs': pairs, 'size_qty': it.get('size_qty')})
    return transit, detail, ids


# ---------------------------------------------------------------- данные

def load_data(con, weeks, lead_weeks, min_sold35):
    today = date.today()
    arrival = today + timedelta(weeks=lead_weeks)

    snap = con.execute("""SELECT table_name FROM information_schema.tables
        WHERE table_name LIKE 'inventory_snapshot_stores_%'
        ORDER BY table_name DESC LIMIT 1""").fetchone()[0]
    price_snap = con.execute("""SELECT table_name FROM information_schema.tables
        WHERE table_name LIKE 'prices_snapshot_%'
        ORDER BY table_name DESC LIMIT 1""").fetchone()[0]

    # Коэффициент возвратов net/gross за 2 последних полных месяца
    r = con.execute("""
        SELECT SUM(net_revenue)/NULLIF(SUM(sales_sum),0) FROM sales_by_employee_correct
        WHERE (year, month) IN (
            SELECT DISTINCT year, month FROM sales_by_employee_correct
            ORDER BY year DESC, month DESC LIMIT 3 OFFSET 1)
    """).fetchone()[0]
    returns_coef = round(float(r), 3) if r else 0.95

    obs35 = mean_coef(today - timedelta(days=35), 35)
    obs90 = mean_coef(today - timedelta(days=90), 90)
    obs180 = mean_coef(today - timedelta(days=180), 180)
    lead_cw = coef_weeks(today, lead_weeks)
    cover_cw = coef_weeks(arrival, weeks)
    cover_avg = cover_cw / weeks

    rows = con.execute(f"""
    WITH sales AS (
        SELECT article,
            ANY_VALUE(REGEXP_REPLACE(product_name, ',\\s*\\d+(\\.\\d+)?$', '')) AS model,
            SUM(quantity) FILTER (WHERE document_moment >= CURRENT_DATE - INTERVAL 35 DAY)  AS q35,
            SUM(quantity) FILTER (WHERE document_moment >= CURRENT_DATE - INTERVAL 90 DAY)  AS q90,
            SUM(quantity) FILTER (WHERE document_moment >= CURRENT_DATE - INTERVAL 180 DAY) AS q180,
            SUM(quantity) AS sall,
            SUM(revenue) FILTER (WHERE document_moment >= CURRENT_DATE - INTERVAL 35 DAY)
                / NULLIF(SUM(quantity) FILTER (WHERE document_moment >= CURRENT_DATE - INTERVAL 35 DAY), 0)
                AS realized_35,
            SUM(revenue) FILTER (WHERE document_moment >= CURRENT_DATE - INTERVAL 90 DAY)
                / NULLIF(SUM(quantity) FILTER (WHERE document_moment >= CURRENT_DATE - INTERVAL 90 DAY), 0)
                AS realized_90,
            SUM(revenue) FILTER (WHERE document_moment >= CURRENT_DATE - INTERVAL 180 DAY)
                / NULLIF(SUM(quantity) FILTER (WHERE document_moment >= CURRENT_DATE - INTERVAL 180 DAY), 0)
                AS realized_180,
            MAX(DATE(document_moment)) AS last_sale,
            MIN(DATE(document_moment)) AS first_sale
        FROM retaildemand_positions
        WHERE price > 0 AND TRY_CAST(article AS INTEGER) BETWEEN 200000 AND 209999
          AND product_name NOT LIKE '%АКЦИЯ 1=2%'
        GROUP BY article
    ),
    w1 AS (
        SELECT p.article, SUM(p.quantity) AS w1_qty
        FROM retaildemand_positions p
        JOIN sales s ON s.article = p.article
        WHERE p.price > 0 AND DATE(p.document_moment) < s.first_sale + INTERVAL 7 DAY
        GROUP BY p.article
    ),
    stk AS (
        SELECT article,
            SUM(moscow) AS msk, SUM(tsum + online) AS tsum_onl,
            SUM(astana_aruzhan) AS aru, SUM(main_warehouse) AS wh,
            SUM(moscow + tsum + online + astana_aruzhan + main_warehouse) AS active
        FROM {snap}
        WHERE TRY_CAST(article AS INTEGER) BETWEEN 200000 AND 209999
        GROUP BY article
    ),
    buy_in AS (
        SELECT product_article AS article, LAST(price ORDER BY supply_moment) AS bp
        FROM supply_positions
        WHERE agent_name = 'Поставщик In' AND applicable AND supply_moment >= '2025-01-01'
        GROUP BY 1
    ),
    buy_any AS (
        SELECT product_article AS article, LAST(price ORDER BY supply_moment) AS bp
        FROM supply_positions
        WHERE applicable AND price > 0 AND supply_moment >= '2025-01-01'
        GROUP BY 1
    ),
    price_now AS (
        SELECT article, MAX(sale_price) AS sale_price, MAX(new_price) AS new_price
        FROM {price_snap} GROUP BY article
    )
    SELECT s.article, s.model, COALESCE(s.q35,0), COALESCE(s.q90,0), COALESCE(s.q180,0),
        s.sall, s.realized_35, s.realized_90, s.realized_180,
        s.last_sale, s.first_sale, COALESCE(w1.w1_qty,0),
        COALESCE(stk.msk,0), COALESCE(stk.tsum_onl,0), COALESCE(stk.aru,0),
        COALESCE(stk.wh,0), COALESCE(stk.active,0),
        COALESCE(buy_in.bp, buy_any.bp, 0),
        pn.sale_price, pn.new_price
    FROM sales s
    LEFT JOIN w1 USING (article)
    LEFT JOIN stk USING (article)
    LEFT JOIN buy_in USING (article)
    LEFT JOIN buy_any USING (article)
    LEFT JOIN price_now pn USING (article)
    """).fetchall()

    meta = dict(snap=snap, price_snap=price_snap, returns_coef=returns_coef,
                obs35=obs35, obs90=obs90, obs180=obs180,
                lead_cw=lead_cw, cover_cw=cover_cw, cover_avg=cover_avg,
                arrival=arrival, today=today, weeks=weeks, lead_weeks=lead_weeks,
                min_sold35=min_sold35)
    return rows, meta


def size_details(con, snap, article):
    """Остатки по размерам (по складам), продажи по размерам за 60д,
    и ИЗВЕСТНАЯ СЕТКА модели (все размеры из поставок + всех продаж + стока) —
    чтобы не заказывать размеры, которых у модели не существует в МойСклад."""
    art = str(article).replace("'", "''")
    stk = con.execute(f"""
        SELECT REGEXP_EXTRACT(product_name, ',\\s*(\\d+\\.?\\d*)$', 1) AS sz,
            CAST(SUM(moscow) AS INT), CAST(SUM(tsum + online) AS INT),
            CAST(SUM(astana_aruzhan) AS INT), CAST(SUM(main_warehouse) AS INT)
        FROM {snap} WHERE article = '{art}'
        GROUP BY 1""").fetchall()
    sold = con.execute(f"""
        SELECT REGEXP_EXTRACT(product_name, ',\\s*(\\d+\\.?\\d*)$', 1) AS sz,
            CAST(SUM(quantity) FILTER (WHERE document_moment >= CURRENT_DATE - INTERVAL 60 DAY) AS INT),
            COUNT(*)
        FROM retaildemand_positions
        WHERE article = '{art}' AND price > 0
        GROUP BY 1""").fetchall()
    supplied = con.execute(f"""
        SELECT DISTINCT REGEXP_EXTRACT(product_name, ',\\s*(\\d+\\.?\\d*)$', 1) AS sz
        FROM supply_positions WHERE product_article = '{art}'""").fetchall()

    def norm(s):
        try:
            f = float(s)
            return str(int(f)) if f == int(f) else str(f)
        except (TypeError, ValueError):
            return None

    size_msk, size_tsum, size_aru, size_wh, size_stock = {}, {}, {}, {}, {}
    known = set()
    for sz, m, t, a, w in stk:
        sz = norm(sz)
        if not sz:
            continue
        known.add(sz)
        size_msk[sz] = size_msk.get(sz, 0) + m
        size_tsum[sz] = size_tsum.get(sz, 0) + t
        size_aru[sz] = size_aru.get(sz, 0) + a
        size_wh[sz] = size_wh.get(sz, 0) + w
        size_stock[sz] = size_stock.get(sz, 0) + m + t + a + w
    size_sold = {}
    for sz, q60, _n in sold:
        sz = norm(sz)
        if sz:
            known.add(sz)
            size_sold[sz] = size_sold.get(sz, 0) + (q60 or 0)
    for (sz,) in supplied:
        sz = norm(sz)
        if sz:
            known.add(sz)
    return size_stock, size_sold, size_msk, size_tsum, size_aru, size_wh, known


def detect_gender(known_sizes):
    nums = []
    for s in known_sizes:
        try:
            nums.append(float(s))
        except ValueError:
            pass
    if not nums:
        return 'У'
    if max(nums) <= 40.5:
        return 'Ж'
    if min(nums) >= 40:
        return 'М'
    return 'У'


def gender_weights(gender):
    if gender == 'Ж':
        return dict(W_WEIGHTS)
    if gender == 'М':
        return dict(M_WEIGHTS)
    # Унисекс: женские×0.45 (36-39) + мужские×0.55 (40-45)
    w = {sz: v * 0.45 for sz, v in W_WEIGHTS.items() if sz in ('36', '37', '38', '39')}
    for sz, v in M_WEIGHTS.items():
        w[sz] = w.get(sz, 0) + v * 0.55
    total = sum(w.values())
    return {sz: v / total for sz, v in w.items()}


def distribute_sizes(target_inventory, order_total, size_stock, transit_pairs, gender,
                     known_sizes=None):
    """Раскладка заказа по размерам.
    target_inventory — сколько пар ВСЕГО должно быть (до вычета стока);
    order_total — сколько заказываем (после вычета);
    Дефицит размера = target_inventory*вес − сток размера. Заказ пропорционален дефициту.
    known_sizes — реальная сетка модели в МС: не заказываем несуществующие размеры."""
    weights = gender_weights(gender)
    if known_sizes:
        limited = {sz: w for sz, w in weights.items() if sz in known_sizes}
        if limited:  # перенормировать веса на реальную сетку модели
            total_w = sum(limited.values())
            weights = {sz: w / total_w for sz, w in limited.items()}
    # транзит распределяем по весам (детальнее не знаем)
    deficits = {}
    for sz, wt in weights.items():
        have = size_stock.get(sz, 0) + transit_pairs * wt
        deficits[sz] = max(0.0, target_inventory * wt - have)
    # Правило: 36-й не заказываем, если есть хоть 1 в стоке (залёживается)
    if size_stock.get('36', 0) >= 1:
        deficits['36'] = 0.0
    total_def = sum(deficits.values())
    if total_def <= 0:
        return {}
    scale = order_total / total_def
    size_qty = {sz: int(round(d * scale)) for sz, d in deficits.items()}
    # добить/срезать разницу от округления на размере с макс. дефицитом
    diff = order_total - sum(size_qty.values())
    if diff != 0:
        pivot = max(deficits, key=deficits.get)
        size_qty[pivot] = max(0, size_qty.get(pivot, 0) + diff)
    return {sz: q for sz, q in size_qty.items() if q > 0}


def cap_for_rate(adj_rate):
    """Максимум пар на модель по темпу (sneaker_order_workflow.md)."""
    if adj_rate > 3:
        return 54
    if adj_rate >= 1.5:
        return 42
    if adj_rate >= 0.5:
        return 30
    return 24


def round6(n):
    """К ближайшему кратному 6, минимум 6."""
    if n <= 0:
        return 0
    return max(6, int(round(n / 6.0)) * 6)


# ---------------------------------------------------------------- сборка

def build_items(con, rows, meta, transit, transit_detail):
    today = meta['today']
    items, liquidation, skipped_ok = [], [], 0

    for r in rows:
        (article, model, q35, q90, q180, sall, realized_35, realized_90, realized_180,
         last_sale, first_sale, w1, msk, tsum_onl, aru, wh, active,
         buy_price, sale_price, new_price) = r
        article = str(article)
        q35, q90, q180 = int(q35), int(q90), int(q180)
        active = int(active)
        in_transit = transit.get(article, 0)

        # --- скидка сейчас
        discount_pct = 0
        if sale_price and new_price and 0 < new_price < sale_price:
            discount_pct = round(100 * (float(sale_price) - float(new_price)) / float(sale_price))

        # --- темп: обычный / остывающий / распроданный хит
        revival = False
        if q35 >= meta['min_sold35']:
            obs_rate, obs_coef, sold_disp, period = q35 / 5.0, meta['obs35'], q35, '35д'
            realized_win = realized_35
        elif q90 >= 9:
            obs_rate, obs_coef, sold_disp, period = q90 / (90 / 7.0), meta['obs90'], q90, '90д'
            realized_win = realized_90
        elif q180 >= REVIVAL_MIN_180D and active <= 3:
            obs_rate, obs_coef, sold_disp, period = q180 / (180 / 7.0), meta['obs180'], q180, '180д'
            realized_win = realized_180
            revival = True
        else:
            continue

        # --- ИСТОРИЧЕСКАЯ уценка: по какой цене реально продавалось в окне подсчёта.
        # Ловит модели, слитые на ликвидации, у которых скидку в МС уже вернули
        # (кейс Travis Scott Jumpman Jack: 43 «продажи» по 3,4-4,5К при базе 12,9К).
        hist_discount_pct = 0
        if sale_price and realized_win and float(sale_price) > 0:
            hist_discount_pct = max(0, round(
                100 * (float(sale_price) - float(realized_win)) / float(sale_price)))
        eff_discount = max(discount_pct, hist_discount_pct)

        base_rate = obs_rate / obs_coef * meta['returns_coef']  # чистый «майский» темп
        adj_rate = base_rate * meta['cover_avg']                # ожидаемый темп в окне продаж

        # --- потребность с лид-таймом и сезонностью окна продаж
        target_inventory = base_rate * (meta['lead_cw'] + meta['cover_cw'])
        order_raw = target_inventory - active - in_transit
        if order_raw < 4:
            skipped_ok += 1
            continue

        # --- ликвидация: не заказываем то, что продавали/продаём с большой скидкой
        if eff_discount >= DISCOUNT_EXCLUDE:
            liquidation.append(dict(article=article, model=model, sold=sold_disp,
                                    period=period, stock=active, discount=eff_discount,
                                    kind='сейчас' if discount_pct >= DISCOUNT_EXCLUDE else 'история'))
            continue

        # --- капы
        cap = cap_for_rate(adj_rate)
        if revival:
            cap = min(cap, 24)          # риск: модель могла «умереть» — пробуем скромно
        if eff_discount >= DISCOUNT_FLAG:
            cap = min(cap, 18)          # спрос по полной цене не доказан — скромнее
        if int(w1) <= 2 and q180 < 15:
            cap = min(cap, 12)          # непроверенная слабая модель — пробник
        order_total = round6(min(order_raw, cap))
        if order_total == 0:
            skipped_ok += 1
            continue

        # --- размеры
        size_stock, size_sold, size_msk, size_tsum, size_aru, size_wh, known = \
            size_details(con, meta['snap'], article)
        gender = detect_gender(known)
        size_qty = distribute_sizes(target_inventory, order_total, size_stock,
                                    in_transit, gender, known_sizes=known)
        if not size_qty:
            skipped_ok += 1
            continue
        pairs = sum(size_qty.values())

        # --- цены/маржа
        buy_price = float(buy_price or 0)
        shelf_price = float(new_price) if (new_price and float(new_price) > 0) else float(sale_price or 0)
        realized = float(realized_win) if realized_win else shelf_price
        margin = round((realized - buy_price) / realized * 100, 1) if realized > 0 and buy_price > 0 else 0

        wos = round(active / adj_rate, 1) if adj_rate > 0 else 999
        zone = 'critical' if wos < 3 else ('soon' if wos < 6 else 'nice')

        # --- маркеры для Алуа прямо в имени
        display = model or article
        if revival:
            display = f"🔥 {display} — БЫЛ РАСПРОДАН ({q180} за 180д)"
        if discount_pct >= DISCOUNT_FLAG:
            display = f"⚠️ {display} — СКИДКА −{discount_pct}% (скорость искусственная!)"
        elif hist_discount_pct >= DISCOUNT_FLAG:
            display = f"⚠️ {display} — ПРОДАВАЛСЯ со скидкой ~−{hist_discount_pct}%"

        items.append({
            'article': article,
            'model': display,
            'photo_url': '',
            'order_mode': 'sizes',
            'size_qty': size_qty,
            'size_sold': size_sold,
            'size_stock': size_stock,
            'size_msk': size_msk,
            'size_tsum': size_tsum,
            'size_aru': size_aru,
            'size_wh': size_wh,
            'pairs': pairs,
            'zone': zone,
            'sold': sold_disp,
            'sold_period': period,
            'weekly_rate': round(obs_rate, 1),
            'adj_rate': round(adj_rate, 1),
            'stock': active,
            'in_transit': in_transit,
            'transit_detail': transit_detail.get(article, []),
            'wos': wos,
            'w1': int(w1),
            'discount_pct': discount_pct,
            'hist_discount_pct': hist_discount_pct,
            'margin': margin,
            'price': round(shelf_price),
            'realized_price': round(realized),
            'cogs': round(buy_price),
            'buy_price': round(buy_price),
            'moscow': int(msk),
            'tsum_online': int(tsum_onl),
            'aruzhan': int(aru),
            'warehouse': int(wh),
        })

    items.sort(key=lambda x: (x['wos'], -x['adj_rate']))
    return items, liquidation, skipped_ok


# ---------------------------------------------------------------- фото

def fetch_image_bytes(article, token):
    headers = {"Authorization": f"Bearer {token}", "Accept-Encoding": "gzip"}
    try:
        r = requests.get(
            f"https://api.moysklad.ru/api/remap/1.2/entity/product?limit=1&filter=article={article}",
            headers=headers, timeout=10)
        if not r.ok or not r.json().get("rows"):
            return None
        im = r.json()["rows"][0].get("images", {}).get("meta", {})
        if not im.get("href") or im.get("size", 0) == 0:
            return None
        ir = requests.get(im["href"], headers=headers, timeout=10)
        rows = ir.json().get("rows", []) if ir.ok else []
        dl = rows[0].get("meta", {}).get("downloadHref") if rows else None
        if not dl:
            return None
        img = requests.get(dl, headers=headers, timeout=15)
        if not img.ok:
            return None
        if PILImage:
            i = PILImage.open(BytesIO(img.content))
            i.thumbnail((800, 800))
            buf = BytesIO()
            i.convert('RGB').save(buf, "JPEG", quality=92)
            return buf.getvalue()
        return img.content
    except Exception:
        return None


def attach_photos(items):
    url = env('SUPABASE_URL')
    storage_key = env('SUPABASE_SERVICE_KEY') or env('SUPABASE_KEY')
    token = env('MOYSKLAD_TOKEN') or env('MS_TOKEN')
    cached = uploaded = missing = 0
    for i, it in enumerate(items):
        art = it['article']
        pub = f"{url}/storage/v1/object/public/photos/{art}.jpg"
        try:
            if requests.head(pub, timeout=10).status_code == 200:
                it['photo_url'] = pub
                cached += 1
                continue
        except Exception:
            pass
        img = fetch_image_bytes(art, token) if token else None
        if img:
            up = requests.post(
                f"{url}/storage/v1/object/photos/{art}.jpg",
                headers={"Authorization": f"Bearer {storage_key}",
                         "Content-Type": "image/jpeg", "x-upsert": "true"},
                data=img, timeout=20)
            if up.status_code in (200, 201):
                it['photo_url'] = pub
                uploaded += 1
                continue
        missing += 1
        if (i + 1) % 10 == 0:
            print(f"  фото {i+1}/{len(items)}...")
    print(f"  Фото: {cached} из кэша, {uploaded} загружено, {missing} нет")


# ---------------------------------------------------------------- Supabase

def next_order_id():
    url, key = env('SUPABASE_URL'), env('SUPABASE_KEY')
    try:
        r = requests.get(
            f"{url}/rest/v1/orders?select=id&id=like.ЗК-*&order=id.desc&limit=1",
            headers={"apikey": key, "Authorization": f"Bearer {key}"}, timeout=10)
        rows = r.json() if r.ok else []
        if rows:
            return f"ЗК-{int(rows[0]['id'].split('-')[1]) + 1:03d}"
    except Exception:
        pass
    return "ЗК-016"


def upload(items, meta_out):
    url = env('SUPABASE_URL')
    key = env('SUPABASE_SERVICE_KEY') or env('SUPABASE_KEY')
    oid = next_order_id()
    r = requests.post(
        f"{url}/rest/v1/orders",
        headers={"apikey": key, "Authorization": f"Bearer {key}",
                 "Content-Type": "application/json", "Prefer": "return=minimal"},
        json={"id": oid, "status": "draft", "items": items, "meta": meta_out},
        timeout=30)
    if r.status_code not in (200, 201):
        print(f"❌ Ошибка загрузки в Supabase: {r.status_code}\n{r.text[:400]}")
        sys.exit(1)
    return oid


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Канонический генератор заказа кроссовок")
    ap.add_argument("--weeks", type=int, default=8, help="покрытие после прибытия, недель")
    ap.add_argument("--lead-weeks", type=float, default=3, help="лид-тайм поставки, недель")
    ap.add_argument("--min-sold35", type=int, default=5)
    ap.add_argument("--dry-run", action="store_true", help="не создавать заказ, JSON в файл")
    ap.add_argument("--no-photos", action="store_true")
    args = ap.parse_args()

    import duckdb
    con = duckdb.connect(str(DB_PATH), read_only=True)

    rows, meta = load_data(con, args.weeks, args.lead_weeks, args.min_sold35)
    print(f"Снапшот: {meta['snap']} | Цены: {meta['price_snap']}")
    print(f"Сегодня {meta['today']} (сезон {SEASON[meta['today'].month]}), "
          f"прибытие ~{meta['arrival']} | окно продаж {args.weeks} нед, "
          f"средний коэфф. окна {meta['cover_avg']:.2f}")
    print(f"Деасезонализация наблюдения: 35д={meta['obs35']:.2f}, 90д={meta['obs90']:.2f} | "
          f"возвраты: net/gross={meta['returns_coef']}")

    transit, transit_detail, transit_ids = fetch_transit()
    print(f"Транзит (заказы < {TRANSIT_MAX_AGE_WEEKS} нед): "
          f"{transit_ids or 'нет'} — {sum(transit.values())} пар")

    items, liquidation, skipped = build_items(con, rows, meta, transit, transit_detail)
    con.close()

    total_pairs = sum(i['pairs'] for i in items)
    total_sum = sum(i['pairs'] * i['buy_price'] for i in items)
    total_profit = sum(i['pairs'] * (i['realized_price'] - i['buy_price'])
                       for i in items if i['buy_price'] > 0)
    n_rev = sum(1 for i in items if i['model'].startswith('🔥'))
    n_disc = sum(1 for i in items if i['discount_pct'] >= DISCOUNT_FLAG)

    print(f"\n{'='*64}")
    print(f"Моделей: {len(items)} (из них 🔥 распроданных хитов: {n_rev}, "
          f"⚠️ на скидке: {n_disc}) | пропущено (хватает): {skipped}")
    print(f"Пар: {total_pairs} | Сумма закупа: {total_sum:,.0f} ₸ | "
          f"Прогноз валовой прибыли: {total_profit:,.0f} ₸")
    if liquidation:
        print(f"\n🚫 НЕ включены (ликвидация, скидка >= {DISCOUNT_EXCLUDE}%):")
        for l in liquidation:
            print(f"   {l['article']} {l['model'][:45]:45} −{l['discount']}% ({l['kind']}), "
                  f"продано {l['sold']}/{l['period']}, сток {l['stock']}")

    meta_out = {
        "date": meta['today'].strftime("%d.%m.%Y"),
        "generator": "generate_order.py v1 (аудит 07.07.2026)",
        "snap": meta['snap'],
        "weeks": args.weeks,
        "lead_weeks": args.lead_weeks,
        "arrival_date": meta['arrival'].isoformat(),
        "season": round(meta['cover_avg'], 2),
        "season_note": f"коэфф. окна продаж {meta['arrival']}+{args.weeks}нед = {meta['cover_avg']:.2f} "
                       f"(НЕ текущий месяц {SEASON[meta['today'].month]})",
        "returns_coef": meta['returns_coef'],
        "order_mode": "sizes",
        "transit_orders": transit_ids,
        "transit_pairs": sum(transit.values()),
        "excluded_liquidation": liquidation,
    }

    if args.dry_run:
        out = Path(__file__).parent / f"order_dryrun_{meta['today'].isoformat()}.json"
        out.write_text(json.dumps({"items": items, "meta": meta_out},
                                  ensure_ascii=False, indent=1))
        print(f"\n[dry-run] JSON: {out}")
        print("\nТоп-15 по срочности:")
        for it in items[:15]:
            print(f"  WOS={it['wos']:5} {it['article']} {it['model'][:52]:52} "
                  f"заказ {it['pairs']:3} пар (сток {it['stock']}, темп {it['adj_rate']}/нед)")
        return

    if not args.no_photos:
        print("\nФото...")
        attach_photos(items)

    oid = upload(items, meta_out)
    print(f"\n{'='*64}\nЗаказ создан: {oid}")
    print(f"Закупщик (Алуа):  {SITE_URL}/?id={oid}&role=buyer")
    print(f"Поставщик:        {SITE_URL}/?id={oid}&role=supplier")


if __name__ == '__main__':
    main()
