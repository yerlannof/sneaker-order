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

# Сезонность, веса размеров, mean_coef/coef_weeks/gender_weights — ЕДИНЫЙ модуль (04.09.2026):
sys.path.insert(0, str(PNLPOWER_DIR))
from scripts.utils.metrics import (SEASON, W_WEIGHTS, M_WEIGHTS,  # noqa: E402
                                   mean_coef, coef_weeks, gender_weights,
                                   paid_sales_sql, unit_flow_sql, forecast_unit_flow,
                                   business_today, completed_sales_window)

from scripts.utils.order_state import fetch_order_state, known_transit_sizes, arrival_date
from scripts.utils.stock_position import prepare_stock_position
from scripts.utils.order_receipts import net_orders, ensure_order_line_ids
from scripts.utils.order_observation import returns_observation
from scripts.utils.purchase_estimate import purchase_estimates, unknown_estimate, order_budget, require_priced_order

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

# mean_coef / coef_weeks — из scripts.utils.metrics


# ---------------------------------------------------------------- теги моделей

# Конец сезона для летних тегов (Алматы): лёгкая сетка умирает к середине сентября,
# сланцы — к концу августа. Окно продаж заказа обрезается этой датой.
SUMMER_END = date(2026, 9, 15)
SLIDES_END = date(2026, 8, 31)


def fetch_model_tags():
    """Теги из Supabase model_tags → (сезон, пол, назначение) по артикулам.
    Ручное знание Алуа/Ерлана — ГЛАВНЕЕ эвристик генератора."""
    url, key = env('SUPABASE_URL'), env('SUPABASE_KEY')
    if not url or not key:
        raise RuntimeError('Теги недоступны: нет SUPABASE_URL/KEY. Заказ остановлен.')
    try:
        r = requests.get(f"{url}/rest/v1/model_tags?select=article,season,gender,purpose&limit=10000",
                         headers={"apikey": key, "Authorization": f"Bearer {key}"}, timeout=15)
        r.raise_for_status()
        rows = r.json()
        if not isinstance(rows, list):
            raise ValueError('Неверный ответ model_tags')
        return ({str(t['article']): t['season'] for t in rows if t.get('season')},
                {str(t['article']): t['gender'] for t in rows if t.get('gender')},
                {str(t['article']): t['purpose'] for t in rows if t.get('purpose')})
    except Exception as e:
        raise RuntimeError('Ручные теги не загружены. Заказ остановлен, чтобы не потерять решения владельцев.') from e


# ---------------------------------------------------------------- транзит

def fetch_transit(max_age_weeks=TRANSIT_MAX_AGE_WEEKS):
    """One strict source for ordinary and seasonal replenishment."""
    return fetch_order_state(env('SUPABASE_URL'), env('SUPABASE_KEY'), requests.get, max_age_weeks, resolve_receipts=net_orders)


# ---------------------------------------------------------------- данные

def sales_manifest(con, end):
    """Make the observation interval explicit; this is not a completeness certificate."""
    last = con.execute('SELECT MAX(document_moment) FROM retaildemand_positions').fetchone()[0]
    return {'timezone': 'Asia/Almaty', 'end_exclusive': end.isoformat(),
            'through': (end-timedelta(days=1)).isoformat(),
            'source': 'retaildemand_positions', 'source_max_moment': str(last) if last else None,
            'completeness': 'not_certified_by_max_date',
            'windows': {str(n): {'from': (end-timedelta(days=n)).isoformat(),
                                'through': (end-timedelta(days=1)).isoformat(), 'days': n}
                        for n in (35, 60, 90, 180)},
            'stock_and_prices': 'current_sources_not_historical_reconstruction'}


def load_data(con, weeks, lead_weeks, min_sold35, future_promo="continue", stock_manifest=None, sales_through=None):
    today = business_today()
    _, sales_end = completed_sales_window(35, sales_through)
    arrival = today + timedelta(weeks=lead_weeks)

    # Production CLI always supplies a verified free-stock/staging manifest.
    # Direct analytical callers may explicitly compare the persisted historical snapshot.
    snap = stock_manifest['table'] if stock_manifest else con.execute("""SELECT table_name FROM information_schema.tables
        WHERE table_name LIKE 'inventory_snapshot_stores_%'
        ORDER BY table_name DESC LIMIT 1""").fetchone()[0]
    price_snap = con.execute("""SELECT table_name FROM information_schema.tables
        WHERE table_name LIKE 'prices_snapshot_%'
        ORDER BY table_name DESC LIMIT 1""").fetchone()[0]

    # Three calendar months completed before the selected sales observation cutoff.
    returns_manifest = returns_observation(con, sales_end)
    returns_coef = returns_manifest["coefficient"]

    obs35 = mean_coef(sales_end - timedelta(days=35), 35)
    obs90 = mean_coef(sales_end - timedelta(days=90), 90)
    obs180 = mean_coef(sales_end - timedelta(days=180), 180)
    lead_cw = coef_weeks(today, lead_weeks)
    cover_cw = coef_weeks(arrival, weeks)
    cover_avg = cover_cw / weeks

    rows = con.execute(f"""
    WITH sales AS (
        SELECT article,
            ANY_VALUE(REGEXP_REPLACE(product_name, ',\\s*\\d+(\\.\\d+)?$', '')) AS model,
            SUM(quantity) FILTER (WHERE document_moment >= DATE '{sales_end.isoformat()}' - INTERVAL 35 DAY)  AS q35,
            SUM(quantity) FILTER (WHERE document_moment >= DATE '{sales_end.isoformat()}' - INTERVAL 90 DAY)  AS q90,
            SUM(quantity) FILTER (WHERE document_moment >= DATE '{sales_end.isoformat()}' - INTERVAL 180 DAY) AS q180,
            SUM(quantity) AS sall,
            SUM(revenue) FILTER (WHERE document_moment >= DATE '{sales_end.isoformat()}' - INTERVAL 35 DAY)
                / NULLIF(SUM(quantity) FILTER (WHERE document_moment >= DATE '{sales_end.isoformat()}' - INTERVAL 35 DAY), 0)
                AS realized_35,
            SUM(revenue) FILTER (WHERE document_moment >= DATE '{sales_end.isoformat()}' - INTERVAL 90 DAY)
                / NULLIF(SUM(quantity) FILTER (WHERE document_moment >= DATE '{sales_end.isoformat()}' - INTERVAL 90 DAY), 0)
                AS realized_90,
            SUM(revenue) FILTER (WHERE document_moment >= DATE '{sales_end.isoformat()}' - INTERVAL 180 DAY)
                / NULLIF(SUM(quantity) FILTER (WHERE document_moment >= DATE '{sales_end.isoformat()}' - INTERVAL 180 DAY), 0)
                AS realized_180,
            MAX(DATE(document_moment)) AS last_sale,
            MIN(DATE(document_moment)) AS first_sale
        FROM retaildemand_positions
        WHERE {paid_sales_sql()} AND document_moment < DATE '{sales_end.isoformat()}'
          AND TRY_CAST(article AS INTEGER) BETWEEN 200000 AND 209999
          AND product_name NOT LIKE '%АКЦИЯ 1=2%'
        GROUP BY article
    ),
    w1 AS (
        SELECT p.article, SUM(p.quantity) AS w1_qty
        FROM retaildemand_positions p
        JOIN sales s ON s.article = p.article
        WHERE {paid_sales_sql("p")} AND p.document_moment < DATE '{sales_end.isoformat()}'
          AND p.product_name NOT LIKE '%АКЦИЯ 1=2%' AND DATE(p.document_moment) < s.first_sale + INTERVAL 7 DAY
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
    price_now AS (
        SELECT article, MAX(sale_price) AS sale_price, MAX(new_price) AS new_price
        FROM {price_snap} GROUP BY article
    )
    SELECT s.article, s.model, COALESCE(s.q35,0), COALESCE(s.q90,0), COALESCE(s.q180,0),
        s.sall, s.realized_35, s.realized_90, s.realized_180,
        s.last_sale, s.first_sale, COALESCE(w1.w1_qty,0),
        COALESCE(stk.msk,0), COALESCE(stk.tsum_onl,0), COALESCE(stk.aru,0),
        COALESCE(stk.wh,0), COALESCE(stk.active,0),
        NULL AS purchase_price,
        pn.sale_price, pn.new_price
    FROM sales s
    LEFT JOIN w1 USING (article)
    LEFT JOIN stk USING (article)
    LEFT JOIN price_now pn USING (article)
    """).fetchall()

    estimates = purchase_estimates(con, today)
    rows = [(*r[:17], estimates.get(str(r[0]), {}).get("amount"), *r[18:]) for r in rows]
    unit_flow = {}
    for days in (35, 90, 180):
        flows = con.execute(f"""SELECT article, {unit_flow_sql()}
            FROM retaildemand_positions
            WHERE document_moment < DATE '{sales_end.isoformat()}'
              AND document_moment >= DATE '{sales_end.isoformat()}' - INTERVAL {days} DAY
              AND TRY_CAST(article AS INTEGER) BETWEEN 200000 AND 209999
              AND product_name NOT LIKE '%АКЦИЯ 1=2%'
            GROUP BY article""").fetchall()
        for art, paid, free, issued in flows:
            unit_flow.setdefault(str(art), {})[f'{days}д'] = dict(
                paid_units=float(paid), free_units=float(free), issued_units=float(issued))
    forecast_unit_flow(0, 0, returns_coef, future_promo)  # validate scenario

    meta = dict(snap=snap, price_snap=price_snap, returns_coef=returns_coef,
                returns_manifest=returns_manifest,
                purchase_estimates=estimates, sales_manifest=sales_manifest(con, sales_end), sales_through=sales_end-timedelta(days=1),
                obs35=obs35, obs90=obs90, obs180=obs180,
                lead_cw=lead_cw, cover_cw=cover_cw, cover_avg=cover_avg,
                arrival=arrival, today=today, weeks=weeks, lead_weeks=lead_weeks,
                min_sold35=min_sold35, unit_flow=unit_flow, future_promo=future_promo, stock_manifest=stock_manifest)
    return rows, meta


def size_details(con, snap, article, sales_through=None):
    """Остатки по размерам (по складам), продажи по размерам за 60д,
    и ИЗВЕСТНАЯ СЕТКА модели (все размеры из поставок + всех продаж + стока) —
    чтобы не заказывать размеры, которых у модели не существует в МойСклад."""
    _, sales_end = completed_sales_window(60, sales_through)
    art = str(article).replace("'", "''")
    stk = con.execute(f"""
        SELECT REGEXP_EXTRACT(product_name, ',\\s*(\\d+\\.?\\d*)$', 1) AS sz,
            CAST(SUM(moscow) AS INT), CAST(SUM(tsum + online) AS INT),
            CAST(SUM(astana_aruzhan) AS INT), CAST(SUM(main_warehouse) AS INT)
        FROM {snap} WHERE article = '{art}'
        GROUP BY 1""").fetchall()
    sold = con.execute(f"""
        SELECT REGEXP_EXTRACT(product_name, ',\\s*(\\d+\\.?\\d*)$', 1) AS sz,
            CAST(SUM(quantity) FILTER (WHERE document_moment >= DATE '{sales_end.isoformat()}' - INTERVAL 60 DAY) AS INT),
            COUNT(*)
        FROM retaildemand_positions
        WHERE article = '{art}' AND {paid_sales_sql()}
          AND document_moment < DATE '{sales_end.isoformat()}'
          AND product_name NOT LIKE '%АКЦИЯ 1=2%'
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


# gender_weights — из scripts.utils.metrics (нормировка к 1.0)


def stock_credit(wos_weeks):
    """Какую долю стока размера ЗАЧИТЫВАТЬ при расчёте дефицита (память size_ordering_strategy, п.4;
    реализовано 04.09.2026 — до этого вычиталось 100% всегда). Быстрая модель съест сток до прихода
    заказа, поэтому её сток почти не покрывает будущий спрос."""
    if wos_weeks < 2:
        return 0.0
    if wos_weeks < 5:
        return 0.3
    if wos_weeks < 10:
        return 0.6
    return 0.9


def distribute_sizes(target_inventory, order_total, size_stock, transit_pairs, gender,
                     known_sizes=None, wos_weeks=None, transit_size_qty=None):
    """Раскладка заказа по размерам.
    target_inventory — сколько пар ВСЕГО должно быть (до вычета стока);
    order_total — сколько заказываем (после вычета);
    Дефицит размера = target_inventory*вес − сток размера × stock_credit(wos). Заказ пропорционален дефициту.
    known_sizes — реальная сетка модели в МС: не заказываем несуществующие размеры.
    wos_weeks — недели запаса модели (None = зачитывать сток полностью, старое поведение)."""
    weights = gender_weights(gender)
    credit = 1.0 if wos_weeks is None else stock_credit(wos_weeks)
    if known_sizes:
        limited = {sz: w for sz, w in weights.items() if sz in known_sizes}
        if not limited:
            raise ValueError(f'Нет весов для реальной сетки {sorted(known_sizes)}. Нужна ручная раскладка; вымышленные размеры не заказываем.')
        total_w = sum(limited.values())
        weights = {sz: w / total_w for sz, w in limited.items()}
    transit_size_qty = transit_size_qty or {}
    unknown_transit = transit_pairs - sum(transit_size_qty.values())
    if unknown_transit < 0:
        raise ValueError('Размерный транзит превышает общий')
    # Exact size quantities take priority; only unspecified pairs use weights.
    deficits = {}
    for sz, wt in weights.items():
        have = size_stock.get(sz, 0) * credit + transit_size_qty.get(sz, 0) + unknown_transit * wt
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

def build_items(con, rows, meta, transit, transit_detail,
                tag_seasons=None, tag_genders=None, tag_purposes=None):
    today = meta['today']
    tag_seasons = tag_seasons or {}
    tag_genders = tag_genders or {}
    tag_purposes = tag_purposes or {}
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
        flow = meta.get('unit_flow', {}).get(article, {}).get(period, {})
        free = flow.get('free_units', 0)
        effective_issued, paid_share = forecast_unit_flow(
            sold_disp, free, meta['returns_coef'], meta.get('future_promo', 'continue'))
        period_weeks = {'35д': 5.0, '90д': 90 / 7.0, '180д': 180 / 7.0}[period]
        depletion_base = effective_issued / period_weeks / obs_coef
        depletion_rate = depletion_base * meta['cover_avg']


        # --- ТЕГ СЕЗОННОСТИ (ручное знание): летним обрезаем окно продаж.
        # Лето+Спорт: после сезона спрос НЕ умирает (зал зимой) — хвост окна ×0.4.
        season_tag = tag_seasons.get(article, '')
        purpose_tag = tag_purposes.get(article, '')
        cover_cw_item = meta['cover_cw']
        season_note = ''
        if season_tag in ('summer', 'slides'):
            end = SLIDES_END if season_tag == 'slides' else SUMMER_END
            weeks_in = max(0.0, min(meta['weeks'], (end - meta['arrival']).days / 7))
            weeks_after = meta['weeks'] - weeks_in
            in_season_cw = coef_weeks(meta['arrival'], weeks_in)
            if purpose_tag == 'sport' and weeks_after > 0:
                after_cw = coef_weeks(end, weeks_after) * 0.4
                cover_cw_item = in_season_cw + after_cw
                season_note = f" ☀️🏃 сезон до {end.strftime('%d.%m')}, но спорт — зимой зал (хвост ×0.4)"
            else:
                cover_cw_item = in_season_cw
                season_note = f" ☀️ сезон до {end.strftime('%d.%m')} — заказ урезан"

        # --- потребность с лид-таймом и сезонностью окна продаж
        target_inventory = depletion_base * (meta['lead_cw'] + cover_cw_item)
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
        if season_tag == 'slides':
            cap = min(cap, 18)          # сланцам сезон вот-вот конец
        elif season_tag == 'summer':
            cap = min(cap, 24)          # летним хвост после сезона не нужен
        if int(w1) <= 2 and q180 < 15:
            cap = min(cap, 12)          # непроверенная слабая модель — пробник
        order_total = round6(min(order_raw, cap))
        if order_total == 0:
            skipped_ok += 1
            continue

        # --- размеры (пол из ТЕГА главнее эвристики по сетке)
        size_stock, size_sold, size_msk, size_tsum, size_aru, size_wh, known = \
            size_details(con, meta['snap'], article, meta['sales_through'])
        gender = {'men': 'М', 'women': 'Ж', 'unisex': 'У'}.get(
            tag_genders.get(article, ''), None) or detect_gender(known)
        wos_now = (active / depletion_rate) if depletion_rate > 0 else 999.0
        size_qty = distribute_sizes(target_inventory, order_total, size_stock,
                                    in_transit, gender, known_sizes=known, wos_weeks=wos_now,
                                    transit_size_qty=known_transit_sizes(transit_detail.get(article, [])))
        if not size_qty:
            skipped_ok += 1
            continue
        pairs = sum(size_qty.values())

        # --- цены/маржа
        buy_price = float(buy_price) if buy_price is not None and buy_price > 0 else None
        shelf_price = float(new_price) if (new_price and float(new_price) > 0) else float(sale_price or 0)
        realized = float(realized_win) if realized_win else shelf_price
        margin = round((realized - buy_price) / realized * 100, 1) if realized > 0 and buy_price is not None else None

        wos = round(active / depletion_rate, 1) if depletion_rate > 0 else 999
        zone = 'critical' if wos < 3 else ('soon' if wos < 6 else 'nice')

        # --- маркеры для Алуа прямо в имени
        display = model or article
        if revival:
            display = f"🔥 {display} — БЫЛ РАСПРОДАН ({q180} за 180д)"
        if discount_pct >= DISCOUNT_FLAG:
            display = f"⚠️ {display} — СКИДКА −{discount_pct}% (скорость искусственная!)"
        elif hist_discount_pct >= DISCOUNT_FLAG:
            display = f"⚠️ {display} — ПРОДАВАЛСЯ со скидкой ~−{hist_discount_pct}%"
        if season_note:
            display = f"{display}{season_note}"

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
            'stock_weekly_rate': round(depletion_rate, 3),
            'paid_units': sold_disp, 'free_units': free,
            'issued_units': flow.get('issued_units', sold_disp + free),
            'forecast_paid_share': paid_share,
            'future_promo': meta.get('future_promo', 'continue'),
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
            'cogs': round(buy_price, 2) if buy_price is not None else None,
            'buy_price': round(buy_price, 2) if buy_price is not None else None,
            'purchase_estimate': meta['purchase_estimates'].get(article, unknown_estimate(today)),
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


def attach_photos(items, refresh=False):
    """refresh=True — перезалить фото из МС поверх лежащих в Storage.

    ⚠️ Без этого фото живёт в Storage вечно: студийные снимки, загруженные в МС
    поверх старых, в заказ не попадали (ловили 23.08.2026 — в ЗК-017 34 модели
    тянулись из старого кэша). Имя файла то же (артикул.jpg), поэтому HEAD 200
    ничего не говорит о свежести — надо смотреть updated у картинки в МС.
    """
    url = env('SUPABASE_URL')
    storage_key = env('SUPABASE_SERVICE_KEY') or env('SUPABASE_KEY')
    token = env('MOYSKLAD_TOKEN') or env('MS_TOKEN')
    cached = uploaded = missing = 0
    for i, it in enumerate(items):
        art = it['article']
        pub = f"{url}/storage/v1/object/public/photos/{art}.jpg"
        try:
            if not refresh and requests.head(pub, timeout=10).status_code == 200:
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
        r.raise_for_status()
        rows = r.json()
        if not isinstance(rows, list):
            raise ValueError('Неверный ответ model_tags')
        if rows:
            return f"ЗК-{int(rows[0]['id'].split('-')[1]) + 1:03d}"
    except Exception:
        pass
    return "ЗК-016"


def upload(items, meta_out, oid=None):
    require_priced_order(items)
    url = env('SUPABASE_URL')
    key = env('SUPABASE_SERVICE_KEY') or env('SUPABASE_KEY')
    oid = oid or next_order_id()
    items = ensure_order_line_ids(oid, items)
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


# ---------------------------------------------------------------- осень

def fetch_zk_incoming(max_age_weeks=8):
    """Confirmed/sent inbound only. Missing source is an error in both paths."""
    return fetch_transit(max_age_weeks)[0]


def generate_autumn(con, args):
    """ОСЕННИЙ план-заказ: модели с сильной осенью-2025, тихие сейчас,
    НЕ покрытые активным заказом. Дата прибытия задаётся параметром;
    прошлое прибытие запрещено. Живые модели сюда не входят —
    они пополняются обычным циклом generate_order."""
    today = business_today()
    _, sales_end = completed_sales_window(90, getattr(args, "sales_through", None))
    arrival = arrival_date(args.arrival_date, today, args.lead_weeks)
    autumn_weeks = 13                      # 13 недель от выбранного прибытия
    yoy = args.yoy                          # поправка на моду год-к-году
    aut_coef_25 = mean_coef(date(2025, 9, 1), 91)
    cover_cw = coef_weeks(arrival, autumn_weeks)
    obs90 = mean_coef(sales_end - timedelta(days=90), 90)

    # Production CLI always supplies a verified free-stock/staging manifest.
    # Direct analytical callers may explicitly compare the persisted historical snapshot.
    snap = args.stock_manifest['table'] if args.stock_manifest else con.execute("""SELECT table_name FROM information_schema.tables
        WHERE table_name LIKE 'inventory_snapshot_stores_%'
        ORDER BY table_name DESC LIMIT 1""").fetchone()[0]
    price_snap = con.execute("""SELECT table_name FROM information_schema.tables
        WHERE table_name LIKE 'prices_snapshot_%'
        ORDER BY table_name DESC LIMIT 1""").fetchone()[0]
    returns_manifest = returns_observation(con, sales_end)
    returns_coef = returns_manifest["coefficient"]

    print(f"Возвраты: {returns_manifest['quality']}; месяцы {[p['month'] for p in returns_manifest['months']]}")
    incoming, incoming_detail, incoming_ids = fetch_transit()
    print(f"Снапшот: {snap} | Цены: {price_snap} | Прибытие к: {arrival}")
    print(f"Коэфф. окна {arrival}+{autumn_weeks} недель: {cover_cw/autumn_weeks:.2f} | YoY-поправка: {yoy} | "
          f"возвраты: {returns_coef} | едет из ЗК: {len(incoming)} артикулов")

    rows = con.execute(f"""
    WITH aut AS (
        SELECT article,
            ANY_VALUE(REGEXP_REPLACE(product_name, ',\\s*\\d+(\\.\\d+)?$', '')) AS model,
            SUM(quantity) AS q_aut,
            SUM(revenue)/NULLIF(SUM(quantity),0) AS realized_aut
        FROM retaildemand_positions
        WHERE {paid_sales_sql()} AND document_moment < DATE '{sales_end.isoformat()}'
          AND TRY_CAST(article AS INTEGER) BETWEEN 200000 AND 209999
          AND product_name NOT LIKE '%АКЦИЯ 1=2%'
          AND document_moment >= DATE '2025-09-01' AND document_moment < DATE '2025-12-01'
        GROUP BY article HAVING SUM(quantity) >= {args.min_autumn}
    ),
    cur AS (
        SELECT article,
            SUM(quantity) FILTER (WHERE document_moment >= DATE '{sales_end.isoformat()}' - INTERVAL 35 DAY) AS q35,
            SUM(quantity) FILTER (WHERE document_moment >= DATE '{sales_end.isoformat()}' - INTERVAL 90 DAY) AS q90
        FROM retaildemand_positions
        WHERE {paid_sales_sql()} AND document_moment < DATE '{sales_end.isoformat()}'
          AND product_name NOT LIKE '%АКЦИЯ 1=2%' GROUP BY article
    ),
    stk AS (
        SELECT article,
            SUM(moscow) AS msk, SUM(tsum + online) AS tsum_onl,
            SUM(astana_aruzhan) AS aru, SUM(main_warehouse) AS wh,
            SUM(moscow + tsum + online + astana_aruzhan + main_warehouse) AS active
        FROM {snap} GROUP BY article
    ),
    pn AS (SELECT article, MAX(sale_price) sp, MAX(new_price) np FROM {price_snap} GROUP BY article)
    SELECT a.article, a.model, a.q_aut, a.realized_aut,
        COALESCE(cur.q35,0), COALESCE(cur.q90,0),
        COALESCE(stk.msk,0), COALESCE(stk.tsum_onl,0), COALESCE(stk.aru,0),
        COALESCE(stk.wh,0), COALESCE(stk.active,0),
        NULL AS purchase_price, pn.sp, pn.np
    FROM aut a
    LEFT JOIN cur USING (article)
    LEFT JOIN stk USING (article)
    LEFT JOIN pn USING (article)
    """).fetchall()

    estimates = purchase_estimates(con, today)
    rows = [(*r[:11], estimates.get(str(r[0]), {}).get("amount"), *r[12:]) for r in rows]
    flows = {}
    for label, predicate in [('autumn', "document_moment >= DATE '2025-09-01' AND document_moment < DATE '2025-12-01'"),
                             ('current', f"document_moment >= DATE '{sales_end.isoformat()}' - INTERVAL 90 DAY AND document_moment < DATE '{sales_end.isoformat()}'")]:
        flows[label] = {str(a): (float(p), float(f), float(i)) for a,p,f,i in con.execute(
            f"SELECT article, {unit_flow_sql()} FROM retaildemand_positions WHERE {predicate} "
            "AND TRY_CAST(article AS INTEGER) BETWEEN 200000 AND 209999 "
            "AND product_name NOT LIKE '%АКЦИЯ 1=2%' GROUP BY article").fetchall()}
    future_promo = args.future_promo
    items, excluded = [], []
    for row in rows:
        (article, model, q_aut, realized_aut, q35, q90,
         msk, tsum_onl, aru, wh, active, buy_price, sale_price, new_price) = row
        article = str(article)
        q_aut, q35, q90, active = int(q_aut), int(q35), int(q90), int(active)

        # живые модели покрываются обычным циклом (и ЗК-016)
        if q35 >= 5:
            continue

        # осенняя цена: не считаем спросом то, что слили на ликвидации осенью
        aut_disc = 0
        if sale_price and realized_aut and float(sale_price) > 0:
            aut_disc = max(0, round(100 * (float(sale_price) - float(realized_aut)) / float(sale_price)))
        cur_disc = 0
        if sale_price and new_price and 0 < new_price < sale_price:
            cur_disc = round(100 * (float(sale_price) - float(new_price)) / float(sale_price))
        if max(aut_disc, cur_disc) >= DISCOUNT_EXCLUDE:
            excluded.append(dict(article=article, model=model, q_aut=q_aut,
                                 discount=max(aut_disc, cur_disc)))
            continue

        # темп осени-2025, очищенный от сезона, возвратов, с поправкой на моду
        base_aut = q_aut / autumn_weeks / aut_coef_25 * returns_coef * yoy
        free_aut = flows['autumn'].get(article, (0, 0, 0))[1]
        issued_aut, paid_share = forecast_unit_flow(q_aut, free_aut, returns_coef, future_promo)
        depletion_aut = issued_aut / autumn_weeks / aut_coef_25 * yoy
        target_inventory = depletion_aut * cover_cw

        # сколько запаса останется к выбранной дате прибытия
        cur_issued, _ = forecast_unit_flow(q90, flows['current'].get(article, (0, 0, 0))[1], returns_coef, future_promo)
        cur_base = (cur_issued / (90 / 7.0)) / obs90
        depletion = cur_base * coef_weeks(today, (arrival - today).days / 7)
        stock_sep = max(0, active - round(depletion))
        coming = incoming.get(article, 0)

        order_raw = target_inventory - stock_sep - coming
        if order_raw < 6:
            continue

        cap = 36 if q_aut >= 40 else 24
        if aut_disc >= DISCOUNT_FLAG:
            cap = min(cap, 12)
        order_total = round6(min(order_raw, cap))
        if order_total == 0:
            continue

        size_stock, size_sold, size_msk, size_tsum, size_aru, size_wh, known = \
            size_details(con, snap, article, sales_end-timedelta(days=1))
        gender = detect_gender(known)
        size_qty = distribute_sizes(target_inventory, order_total, size_stock, coming, gender,
                                    known_sizes=known, transit_size_qty=known_transit_sizes(incoming_detail.get(article, [])))
        if not size_qty:
            continue
        pairs = sum(size_qty.values())

        buy_price = float(buy_price) if buy_price is not None and buy_price > 0 else None
        shelf = float(new_price) if (new_price and float(new_price) > 0) else float(sale_price or 0)
        realized = float(realized_aut) if realized_aut else shelf
        margin = round((realized - buy_price) / realized * 100, 1) if realized > 0 and buy_price is not None else None

        display = f"🍂 {model or article} — ОСЕНЬЮ-25: {q_aut} шт"
        if aut_disc >= DISCOUNT_FLAG:
            display += f" (⚠️ продавался со скидкой ~−{aut_disc}%)"

        items.append({
            'article': article, 'model': display, 'photo_url': '',
            'order_mode': 'sizes', 'size_qty': size_qty, 'size_sold': size_sold,
            'size_stock': size_stock, 'size_msk': size_msk, 'size_tsum': size_tsum,
            'size_aru': size_aru, 'size_wh': size_wh,
            'pairs': pairs, 'zone': 'critical' if active == 0 else 'soon',
            'sold': q_aut, 'sold_period': 'осень25',
            'paid_units': q_aut, 'free_units': free_aut, 'issued_units': q_aut + free_aut,
            'forecast_paid_share': paid_share, 'future_promo': future_promo,
            'weekly_rate': round(q_aut / autumn_weeks, 1),
            'adj_rate': round(base_aut * cover_cw / autumn_weeks, 1),
            'stock': active, 'in_transit': coming, 'transit_detail': incoming_detail.get(article, []),
            'wos': round(stock_sep / (base_aut * cover_cw / autumn_weeks), 1) if base_aut > 0 else 0,
            'w1': 0, 'discount_pct': cur_disc, 'hist_discount_pct': aut_disc,
            'margin': margin, 'price': round(shelf), 'realized_price': round(realized),
            'cogs': round(buy_price, 2) if buy_price is not None else None, 'buy_price': round(buy_price, 2) if buy_price is not None else None,
            'purchase_estimate': estimates.get(article, unknown_estimate(today)),
            'moscow': int(msk), 'tsum_online': int(tsum_onl),
            'aruzhan': int(aru), 'warehouse': int(wh),
        })

    items.sort(key=lambda x: -x['sold'])
    total_pairs = sum(i['pairs'] for i in items)
    budget = order_budget(items)
    total_sum = budget['total_purchase_cost']
    cost_label = f'{total_sum:,.2f} ₸' if total_sum is not None else f"неизвестно (оценено {budget['known_purchase_cost']:,.2f} ₸, без цены {budget['unpriced_pairs']} пар)"
    print(f"\n{'='*64}")
    print(f"🍂 ОСЕННИЙ ПЛАН: {len(items)} моделей, {total_pairs} пар, {cost_label} оценка закупа")
    print(f"Исключено (осенью продавались на ликвидации ≥{DISCOUNT_EXCLUDE}%): {len(excluded)}")
    for e in excluded[:10]:
        print(f"   {e['article']} {e['model'][:45]:45} −{e['discount']}%, осень {e['q_aut']} шт")

    meta_out = {
        "date": today.strftime("%d.%m.%Y"),
        "generator": "generate_order.py --autumn v1",
        "snap": snap, "order_mode": "sizes", "stock_manifest": args.stock_manifest,
        "sales_manifest": sales_manifest(con, sales_end),
        "arrival_target": arrival.isoformat(),
        "send_to_supplier": (arrival - timedelta(weeks=args.lead_weeks)).isoformat(),
        "season_note": f"окно {arrival}+{autumn_weeks} недель, коэфф {cover_cw/autumn_weeks:.2f}, YoY {yoy}",
        "budget": budget,
        "returns_coef": returns_coef,
        "returns_manifest": returns_manifest,
        "excluded_liquidation": excluded,
        "transit_orders": incoming_ids, "transit_pairs": sum(incoming.values()),
        "future_promo": future_promo,
    }

    if args.dry_run:
        out = args.output_dir / f"autumn_dryrun_{today.isoformat()}.json"
        out.write_text(json.dumps({"items": items, "meta": meta_out}, ensure_ascii=False, indent=1))
        print(f"\n[dry-run] JSON: {out}\n\nТоп-20 осенних:")
        for it in items[:20]:
            print(f"  осень25={it['sold']:3} {it['article']} {it['model'][:52]:52} "
                  f"заказ {it['pairs']:3} пар (сток {it['stock']}, едет {it['in_transit']})")
        return

    require_priced_order(items)
    if not args.no_photos:
        print("\nФото...")
        attach_photos(items, refresh=args.refresh_photos)
    oid = upload(items, meta_out, oid=args.order_id or "ОСЕНЬ-2026")
    print(f"\n{'='*64}\nОсенний план создан: {oid}")
    print(f"Просмотр/правки:  {SITE_URL}/?id={oid}&role=buyer")
    print(f"Поставщику (в августе): {SITE_URL}/?id={oid}&role=supplier")


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Канонический генератор заказа кроссовок")
    ap.add_argument("--future-promo", choices=("continue", "stop"), default="continue",
                    help="расход запаса: акция продолжается (default) или подарки прекращаются; платный спрос фиксирован")
    ap.add_argument("--weeks", type=int, default=8, help="покрытие после прибытия, недель")
    ap.add_argument("--lead-weeks", type=float, default=3, help="лид-тайм поставки, недель")
    ap.add_argument("--sales-through", type=date.fromisoformat,
                    help="последний полный день продаж YYYY-MM-DD; default вчера по Алматы; историческое окно только dry-run")
    ap.add_argument("--min-sold35", type=int, default=5)
    ap.add_argument("--stock-source", type=Path, help="сохранённый проверяемый источник; по умолчанию свежие GET остатков и черновиков")
    ap.add_argument("--output-dir", type=Path, default=PNLPOWER_DIR/"data/order_previews", help="приватная папка результатов dry-run")
    ap.add_argument("--dry-run", action="store_true", help="не создавать заказ, JSON в файл")
    ap.add_argument("--no-photos", action="store_true")
    ap.add_argument("--refresh-photos", action="store_true",
                    help="перезалить фото из МС поверх Storage (после обновления снимков в МС)")
    ap.add_argument("--order-id", default=None,
                    help="ID заказа в Supabase (по умолчанию ЗК-NNN, для --autumn «ОСЕНЬ-2026»). Нужен, когда ID занят прошлым планом")
    ap.add_argument("--arrival-date", help="дата прибытия осеннего плана YYYY-MM-DD; default сегодня + lead-weeks")
    ap.add_argument("--autumn", action="store_true",
                    help="осенний план-заказ (сен-ноя): хиты осени-2025, тихие сейчас")
    ap.add_argument("--yoy", type=float, default=0.7,
                    help="поправка год-к-году для осеннего темпа (мода выдыхается)")
    ap.add_argument("--min-autumn", type=int, default=12,
                    help="мин. продаж за осень-2025 для осеннего плана")
    args = ap.parse_args()
    try:
        _, sales_end = completed_sales_window(35, args.sales_through)
    except ValueError as exc:
        ap.error(str(exc))
    if not args.dry_run and sales_end != business_today():
        ap.error("Историческое окно продаж допускается только с --dry-run")

    import duckdb
    con = duckdb.connect(str(DB_PATH), read_only=True)

    source=json.loads(args.stock_source.read_text()) if args.stock_source else None
    args.stock_manifest=prepare_stock_position(con, source, strict=not args.dry_run)
    if not args.dry_run and not args.stock_manifest['release_ready']:
        raise RuntimeError('В остатках есть исключения; разрешён только помеченный dry-run')
    args.output_dir.mkdir(parents=True,exist_ok=True)
    if args.autumn:
        generate_autumn(con, args)
        con.close()
        return

    rows, meta = load_data(con, args.weeks, args.lead_weeks, args.min_sold35, args.future_promo, args.stock_manifest, args.sales_through)
    print(f"Снапшот: {meta['snap']} | Цены: {meta['price_snap']}")

    print(f"Продажи: по {meta['sales_through']} включительно; сегодняшние чеки исключены")
    print(f"Доступность: {meta['stock_manifest']['totals']}; SHA {meta['stock_manifest']['source_sha256'][:16]}")
    print(f"Сегодня {meta['today']} (сезон {SEASON[meta['today'].month]}), "
          f"прибытие ~{meta['arrival']} | окно продаж {args.weeks} нед, "
          f"средний коэфф. окна {meta['cover_avg']:.2f}")
    print(f"Деасезонализация наблюдения: 35д={meta['obs35']:.2f}, 90д={meta['obs90']:.2f} | "
          f"возвраты: net/gross={meta['returns_coef']}")

    print(f"Возвраты: {meta['returns_manifest']['quality']}; месяцы {[p['month'] for p in meta['returns_manifest']['months']]}")
    transit, transit_detail, transit_ids = fetch_transit()
    print(f"Транзит (заказы < {TRANSIT_MAX_AGE_WEEKS} нед): "
          f"{transit_ids or 'нет'} — {sum(transit.values())} пар")

    tag_seasons, tag_genders, tag_purposes = fetch_model_tags()
    print(f"Теги моделей: сезон {len(tag_seasons)}, пол {len(tag_genders)}, "
          f"назначение {len(tag_purposes)} (лето/сланцы → окно урезано, "
          f"лето+спорт → хвост ×0.4, пол → размерная сетка)")

    items, liquidation, skipped = build_items(con, rows, meta, transit, transit_detail,
                                              tag_seasons, tag_genders, tag_purposes)
    con.close()

    total_pairs = sum(i['pairs'] for i in items)
    budget = order_budget(items)
    total_sum = budget['total_purchase_cost']
    cost_label = f'{total_sum:,.2f} ₸' if total_sum is not None else f"неизвестно (оценено {budget['known_purchase_cost']:,.2f} ₸, без цены {budget['unpriced_pairs']} пар)"
    total_profit = budget['forecast_gross_profit']
    profit_label = f'{total_profit:,.2f} ₸' if total_profit is not None else 'неизвестно'
    n_rev = sum(1 for i in items if i['model'].startswith('🔥'))
    n_disc = sum(1 for i in items if i['discount_pct'] >= DISCOUNT_FLAG)

    print(f"\n{'='*64}")
    print(f"Моделей: {len(items)} (из них 🔥 распроданных хитов: {n_rev}, "
          f"⚠️ на скидке: {n_disc}) | пропущено (хватает): {skipped}")
    print(f"Пар: {total_pairs} | Оценка закупа: {cost_label} | "
          f"Прогноз валовой прибыли: {profit_label}")
    if liquidation:
        print(f"\n🚫 НЕ включены (ликвидация, скидка >= {DISCOUNT_EXCLUDE}%):")
        for l in liquidation:
            print(f"   {l['article']} {l['model'][:45]:45} −{l['discount']}% ({l['kind']}), "
                  f"продано {l['sold']}/{l['period']}, сток {l['stock']}")

    meta_out = {
        "date": meta['today'].strftime("%d.%m.%Y"),
        "generator": "generate_order.py v1 (аудит 07.07.2026)",
        "snap": meta['snap'],
        "stock_manifest": meta["stock_manifest"],
        "sales_manifest": meta["sales_manifest"],
        "weeks": args.weeks,
        "future_promo": args.future_promo,
        "forecast_assumption": "paid demand unchanged; gifts use observed selected-window flow",
        "lead_weeks": args.lead_weeks,
        "arrival_date": meta['arrival'].isoformat(),
        "season": round(meta['cover_avg'], 2),
        "season_note": f"коэфф. окна продаж {meta['arrival']}+{args.weeks}нед = {meta['cover_avg']:.2f} "
                       f"(НЕ текущий месяц {SEASON[meta['today'].month]})",
        "budget": budget,
        "returns_coef": meta['returns_coef'],
        "returns_manifest": meta['returns_manifest'],
        "order_mode": "sizes",
        "transit_orders": transit_ids,
        "transit_pairs": sum(transit.values()),
        "excluded_liquidation": liquidation,
    }

    if args.dry_run:
        out = args.output_dir / f"order_dryrun_{meta['today'].isoformat()}.json"
        out.write_text(json.dumps({"items": items, "meta": meta_out},
                                  ensure_ascii=False, indent=1))
        print(f"\n[dry-run] JSON: {out}")
        print("\nТоп-15 по срочности:")
        for it in items[:15]:
            print(f"  WOS={it['wos']:5} {it['article']} {it['model'][:52]:52} "
                  f"заказ {it['pairs']:3} пар (сток {it['stock']}, темп {it['adj_rate']}/нед)")
        return

    require_priced_order(items)
    if not args.no_photos:
        print("\nФото...")
        attach_photos(items, refresh=args.refresh_photos)

    oid = upload(items, meta_out)
    print(f"\n{'='*64}\nЗаказ создан: {oid}")
    print(f"Закупщик (Алуа):  {SITE_URL}/?id={oid}&role=buyer")
    print(f"Поставщик:        {SITE_URL}/?id={oid}&role=supplier")


if __name__ == '__main__':
    main()
