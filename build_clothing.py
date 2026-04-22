#!/usr/bin/env python3
"""
Builder для clothing_v3.html — дашборд женской одежды с рекомендациями.

Логика: берёт все модели от 25 китайских поставщиков женской одежды,
считает остатки/продажи/скорость, даёт рекомендацию (дозаказ/держать/скидка).

Запуск:   python3 sneaker-order/build_clothing.py
Выход:    sneaker-order/clothing_data.json (для сверки)
          sneaker-order/clothing_v3.html   (для Pages)
"""
import duckdb
import json
import re
import copy
import sys
from datetime import date, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
DB_PATH = PROJECT_ROOT / 'data' / 'pnlpower.duckdb'
OLD_HTML = PROJECT_ROOT / 'sneaker-order' / 'clothing.html'
OUT_HTML = PROJECT_ROOT / 'sneaker-order' / 'clothing_v3.html'
OUT_JSON = PROJECT_ROOT / 'sneaker-order' / 'clothing_data.json'

SNAPSHOT_DATE = '20260421'

WOMEN_SUPPLIERS = [
    "Чаонань Фуши (Trendy Men's Fashion) (CNF)",
    'CNFTB', 'SC', 'DLJ', 'GYL', 'XSL (西思黎牛仔服饰 / Сисыли)',
    'JY', 'MYA', 'KS (K.S牛仔服饰)', 'DXJ (嘟小俊 / Ду Сяо Цзюнь)',
    'X5S (小5·SHOP)', 'MZ (美翥)', 'YYG (衣优谷 / Ийоугу)', 'DMK',
    'XIL', 'YQ', 'HH', 'DQJ (大琴家服饰)', 'MIL (MILDNESS)',
    'DJQ (丹佳琦服饰)', 'YF', 'KPK', 'YW',
    'ZZ (Неизвестный поставщик)', 'JL (金莱服饰 / Цзиньлай Фуши)',
]

# Правила рекомендаций — настраивается
MIN_DAYS_TO_JUDGE = 30
REORDER_ST = 60
REORDER_WOS_WEEKS = 8
HOLD_ST_MIN = 20
DISCOUNT_20_ST = 20
DISCOUNT_20_DAYS = 60
DISCOUNT_30_ST = 10
DISCOUNT_30_DAYS = 90
DEAD_STOCK_DAYS = 60


def extract_article(name: str) -> str | None:
    m = re.search(r'\[([^\]]+)\]', name)
    return m.group(1) if m else None


def classify_subfolder(name: str) -> str:
    lower = name.lower()
    if any(k in lower for k in ['футбол', 'худи', 'лонгслив', 'рубашк', 'топ ', 'майк', 'жилет', 'свитшот', 'толстовк']):
        return 'Верх'
    if any(k in lower for k in ['джинс', 'брюки', 'шорт']):
        return 'Низ'
    if 'куртк' in lower:
        return 'Верхняя одежда'
    if 'костюм' in lower:
        return 'Костюмы'
    return 'Прочее'


def recommend(item: dict) -> tuple[str, str]:
    """Возвращает (код рекомендации, причина по-русски)."""
    days = item['days_since_first']
    st = item['sell_through']
    wos = item['wos']
    sold_30 = item['sales']['last_30d']['qty']
    stock = item['stock']['total']
    accel = item['velocity']['acceleration']
    # Растёт если: либо стабильно (×1.2+ и ≥3 шт), либо очень резко (×1.5+ и ≥1 шт)
    growing = (accel >= 1.2 and sold_30 >= 3) or (accel >= 1.5 and sold_30 >= 1)

    if days < MIN_DAYS_TO_JUDGE:
        return ('WAIT', f'Пришло {days} дн назад — ждём ещё, рано судить')
    if stock == 0:
        return ('SOLD_OUT', f'Остаток закончился, продано {int(st)}% от завоза — дозаказать если хит')
    if st >= REORDER_ST and sold_30 > 0:
        return ('REORDER', f'Продано {st:.0f}% (хит), продаётся {sold_30} шт/мес — дозаказать срочно')
    if st >= HOLD_ST_MIN and wos < REORDER_WOS_WEEKS and sold_30 > 0:
        return ('REORDER', f'Продано {st:.0f}%, осталось на {wos:.0f} недель — дозаказать')
    if sold_30 == 0 and days > DEAD_STOCK_DAYS:
        return ('DEAD', f'Ноль продаж за посл. 30 дней ({days} дн на складе) — сильная скидка или списание')
    if st < DISCOUNT_30_ST and days > DISCOUNT_30_DAYS and not growing:
        return ('DISCOUNT_30', f'Только {st:.0f}% за {days} дн, ускорение слабое — скидка 30%')
    if st < DISCOUNT_20_ST and days > DISCOUNT_20_DAYS and not growing:
        return ('DISCOUNT_20', f'Продано {st:.0f}% за {days} дн, ускорение слабое — скидка 15-20%')
    if growing and st < HOLD_ST_MIN:
        return ('GROWING', f'Продано пока {st:.0f}%, НО ускорение ×{accel:.1f} ({sold_30}/мес) — весна разгонит, скидку не давать')
    return ('HOLD', f'Продано {st:.0f}%, стабильно идёт {sold_30} шт/мес — держим')


def fetch_photos_from_ms(model_names: list) -> dict:
    """Скачивает фото из МойСклад для списка имён моделей. Возвращает {base_model: base64_jpeg}."""
    import os
    from dotenv import load_dotenv
    load_dotenv()
    sys.path.insert(0, str(PROJECT_ROOT))
    from src.utils.http_retry import retry_get
    import base64

    base_url = "https://api.moysklad.ru/api/remap/1.2"
    token = os.getenv('MOYSKLAD_TOKEN')
    if not token:
        print("   ⚠️ MOYSKLAD_TOKEN не найден — пропускаю подтяжку фото")
        return {}
    headers = {"Authorization": f"Bearer {token}", "Accept-Encoding": "gzip"}

    result = {}
    for model in model_names:
        try:
            r = retry_get(f"{base_url}/entity/product", headers=headers,
                          params={'filter': f'name~{model}', 'limit': 5})
            for p in r.json().get('rows', [])[:3]:
                name = p.get('name', '')
                if not name.startswith(model):
                    continue
                img_meta = p.get('images', {}).get('meta', {})
                if not img_meta.get('href'):
                    break
                r2 = retry_get(img_meta['href'], headers=headers)
                imgs = r2.json().get('rows', [])
                if not imgs:
                    break
                mini = imgs[0].get('miniature', {}).get('href') or imgs[0].get('tiny', {}).get('href')
                if not mini:
                    break
                r3 = retry_get(mini, headers=headers)
                result[model] = base64.b64encode(r3.content).decode('ascii')
                print(f"   📸 Скачал фото: {model} ({len(r3.content)} байт)")
                break
        except Exception as e:
            print(f"   ⚠️ Не удалось скачать для {model}: {e}")
    return result


def load_old_photos() -> tuple[dict, dict]:
    """Извлекает фото из старого clothing.html по article и по base_model (split)."""
    if not OLD_HTML.exists():
        return {}, {}
    text = OLD_HTML.read_text()
    m = re.search(r'const ALL_ITEMS = (\[.*?\]);', text, re.DOTALL)
    if not m:
        return {}, {}
    items = json.loads(m.group(1))
    by_article = {}
    by_base = {}  # ключ — split_part(name, ' (', 1)
    for it in items:
        photo = it.get('photo', '')
        if not photo:
            continue
        # Артикул может быть в явном поле или внутри []
        art = it.get('article') or ''
        m_art = re.search(r'\[([^\]]+)\]', it.get('name', ''))
        if m_art:
            art = m_art.group(1)
        if art:
            by_article[art] = photo
        # Базовое имя — то что до первой ' ('
        base = it['name'].split(' (', 1)[0].strip()
        by_base[base] = photo
    return by_article, by_base


def main():
    today = date.today()
    d30 = (today - timedelta(days=30)).isoformat()
    print(f"Дата расчёта: {today}, окно скорости: {d30} → {today}")

    con = duckdb.connect(str(DB_PATH), read_only=True)
    snap = f'inventory_snapshot_stores_{SNAPSHOT_DATE}'
    prices = f'prices_snapshot_{SNAPSHOT_DATE}'

    print("\n1. Загружаю фото из старого файла...")
    photos_by_article, photos_by_name = load_old_photos()
    print(f"   фото по article: {len(photos_by_article)}")
    print(f"   фото по name:    {len(photos_by_name)}")

    print("\n2. Список базовых моделей от 25 поставщиков...")
    supplier_placeholders = ','.join(['?'] * len(WOMEN_SUPPLIERS))
    # any_value полного имени чтобы извлечь артикул из [XXX]
    q = f"""
        SELECT split_part(product_name, ' (', 1) AS base_model,
               ANY_VALUE(product_name) AS sample_name,
               ANY_VALUE(agent_name) AS supplier,
               MIN(DATE(supply_moment)) AS first_supply,
               MAX(DATE(supply_moment)) AS last_supply,
               SUM(quantity) AS supplied_qty,
               SUM(total) AS supplied_cost,
               ROUND(AVG(price)) AS unit_cost
        FROM supply_positions
        WHERE agent_name IN ({supplier_placeholders}) AND product_name LIKE '%(%'
        GROUP BY 1 ORDER BY 1
    """
    models = con.execute(q, WOMEN_SUPPLIERS).fetchall()
    print(f"   Найдено моделей: {len(models)}")

    results = []
    for (base_model, sample_name, supplier, first_supply, last_supply, sup_qty, sup_cost, unit_cost) in models:
        sup_qty = int(sup_qty or 0)
        sup_cost = float(sup_cost or 0)
        unit_cost = float(unit_cost or 0)

        # Остатки: ТОЧНОЕ совпадение базовой модели (split_part = base_model)
        stk = con.execute(f"""
            SELECT COALESCE(SUM(total_stock),0), COALESCE(SUM(moscow),0),
                   COALESCE(SUM(tsum + online),0), COALESCE(SUM(astana_aruzhan),0),
                   COALESCE(SUM(main_warehouse),0)
            FROM {snap}
            WHERE split_part(product_name, ' (', 1) = ?
        """, [base_model]).fetchone()
        total_s = max(int(stk[0]), 0)
        msk, tsum_on, aruz, wh = [max(int(x), 0) for x in stk[1:]]

        # Разбивка по цветам и размерам — ищем "(Цвет, Размер)" в конце полного имени
        variants_rows = con.execute(f"""
            SELECT product_name, SUM(total_stock) AS qty
            FROM {snap}
            WHERE split_part(product_name, ' (', 1) = ?
              AND total_stock > 0
            GROUP BY product_name
        """, [base_model]).fetchall()
        colors, sizes, matrix = {}, {}, {}
        for pname, qty in variants_rows:
            qty = int(qty)
            # Последняя скобка вида "(Белый, M)" или "(Чёрный, XL)"
            m2 = re.search(r'\(([^()]+),\s*([^()]+)\)\s*$', pname)
            if m2:
                color = m2.group(1).strip()
                size = m2.group(2).strip()
                colors[color] = colors.get(color, 0) + qty
                sizes[size] = sizes.get(size, 0) + qty
                matrix[f'{color}/{size}'] = qty

        # Продажи
        sl = con.execute("""
            SELECT
              SUM(quantity) FILTER (WHERE year=2026 AND month=1) AS jan_q,
              SUM(revenue)  FILTER (WHERE year=2026 AND month=1) AS jan_r,
              SUM(quantity) FILTER (WHERE year=2026 AND month=2) AS feb_q,
              SUM(revenue)  FILTER (WHERE year=2026 AND month=2) AS feb_r,
              SUM(quantity) FILTER (WHERE year=2026 AND month=3) AS mar_q,
              SUM(revenue)  FILTER (WHERE year=2026 AND month=3) AS mar_r,
              SUM(quantity) FILTER (WHERE year=2026 AND month=4) AS apr_q,
              SUM(revenue)  FILTER (WHERE year=2026 AND month=4) AS apr_r,
              SUM(quantity) FILTER (WHERE DATE(sale_datetime) >= ?::DATE) AS l30_q,
              SUM(revenue)  FILTER (WHERE DATE(sale_datetime) >= ?::DATE) AS l30_r,
              SUM(quantity) FILTER (WHERE DATE(sale_datetime) >= ?) AS all_q,
              SUM(revenue)  FILTER (WHERE DATE(sale_datetime) >= ?) AS all_r
            FROM sales WHERE split_part(product_name, ' (', 1) = ?
        """, [d30, d30, first_supply, first_supply, base_model]).fetchone()
        g = lambda x: float(x or 0)
        jan_q, jan_r, feb_q, feb_r, mar_q, mar_r, apr_q, apr_r, l30_q, l30_r, all_q, all_r = [g(x) for x in sl]

        # Retail price: из prices_snapshot → AVG реальной продажи
        rp = con.execute(f"""
            WITH p AS (SELECT AVG(sale_price) c FROM {prices}
                       WHERE split_part(name, ' (', 1) = ? AND sale_price > 0),
                 sa AS (SELECT AVG(price) c FROM sales
                       WHERE split_part(product_name, ' (', 1) = ? AND price > 0)
            SELECT COALESCE((SELECT c FROM p), (SELECT c FROM sa), 0)
        """, [base_model, base_model]).fetchone()
        retail = float(rp[0] or 0)

        # Дополнительные поля: последняя продажа, средняя реальная цена, возвраты
        extras = con.execute("""
            SELECT
              MAX(DATE(sale_datetime)) AS last_sale,
              ROUND(AVG(CASE WHEN price > 0 THEN price END)) AS avg_sale_price,
              MIN(CASE WHEN revenue > 0 THEN DATE(sale_datetime) END) AS first_sale
            FROM sales WHERE split_part(product_name, ' (', 1) = ?
        """, [base_model]).fetchone()
        last_sale = extras[0].isoformat() if extras[0] else None
        avg_sale_price = float(extras[1] or 0)
        first_sale = extras[2].isoformat() if extras[2] else None
        days_since_last_sale = (today - extras[0]).days if extras[0] else None

        # Возвраты: берем из supply_positions.total (если есть) или 0
        # (в sales нет return_quantity — оставляю 0, позже можно прицепить retailsalesreturn)
        returns_qty = 0

        days_since = (today - first_supply).days
        sell_through = (all_q / sup_qty * 100) if sup_qty else 0
        vel_recent = int(l30_q)
        vel_avg = (all_q / days_since * 30) if days_since else 0
        accel = (vel_recent / vel_avg) if vel_avg > 0 else 0
        daily = vel_recent / 30
        wos = (total_s / daily / 7) if daily > 0 else 999

        # Артикул извлекаем из полного имени (там может быть [XXX])
        article = extract_article(sample_name) or extract_article(base_model)
        # Прибыль: выручка − себес проданных
        gross_profit = all_r - (all_q * unit_cost) if unit_cost > 0 else 0
        # Маржа в %
        margin_pct = ((retail - unit_cost) / retail * 100) if retail > 0 else 0
        # Сортируем размеры правильно: XS < S < M < L < XL < 2XL < 3XL
        SIZE_ORDER = {'XS': 0, 'S': 1, 'M': 2, 'L': 3, 'XL': 4, '2XL': 5, '3XL': 6, 'XXL': 5, 'XXXL': 6}
        sizes_sorted = dict(sorted(sizes.items(), key=lambda x: (SIZE_ORDER.get(x[0], 99), x[0])))

        item = {
            'article': article,
            'name': base_model,
            'variants': {
                'colors': colors,
                'sizes': sizes_sorted,
                'matrix': matrix,
            },
            'last_sale': last_sale,
            'first_sale': first_sale,
            'days_since_last_sale': days_since_last_sale,
            'avg_sale_price': round(avg_sale_price) if avg_sale_price else 0,
            'returns_qty': returns_qty,
            'gross_profit': round(gross_profit),
            'margin_pct': round(margin_pct, 1),
            'supplier': supplier,
            'subfolder': classify_subfolder(base_model),
            'stock': {'total': total_s, 'moscow': msk, 'tsum_online': tsum_on, 'aruzhan': aruz, 'warehouse': wh},
            'sales': {
                'jan': {'qty': int(jan_q), 'rev': jan_r},
                'feb': {'qty': int(feb_q), 'rev': feb_r},
                'mar': {'qty': int(mar_q), 'rev': mar_r},
                'apr': {'qty': int(apr_q), 'rev': apr_r},
                'last_30d': {'qty': int(l30_q), 'rev': l30_r},
                'total_qty': int(all_q),
                'total_rev': all_r,
            },
            'supplied_qty': sup_qty,
            'supplied_cost': sup_cost,
            'cost': unit_cost,
            'retail': round(retail),
            'sell_through': round(sell_through, 1),
            'stock_value': round(total_s * retail),
            'stock_cost': round(total_s * unit_cost),
            'first_supply_date': first_supply.isoformat(),
            'last_supply_date': last_supply.isoformat(),
            'days_since_first': days_since,
            'velocity': {
                'recent_30d': vel_recent,
                'avg_30d': round(vel_avg, 1),
                'acceleration': round(min(accel, 99), 2),
            },
            'wos': round(min(wos, 999), 1),
        }
        rec, reason = recommend(item)
        item['recommendation'] = rec
        item['recommendation_reason'] = reason

        art = item['article']
        if art and art in photos_by_article:
            item['photo'] = photos_by_article[art]
        elif base_model in photos_by_name:  # теперь by_base под тем же названием
            item['photo'] = photos_by_name[base_model]
        else:
            item['photo'] = ''
        results.append(item)

    # Для моделей без фото — подтянуть из МС API
    no_photo_names = [r['name'] for r in results if not r['photo']]
    if no_photo_names:
        print(f"\n3. Моделей без фото: {len(no_photo_names)} — тяну из МС API...")
        fetched = fetch_photos_from_ms(no_photo_names)
        for r in results:
            if not r['photo'] and r['name'] in fetched:
                r['photo'] = fetched[r['name']]

    results.sort(key=lambda x: -x['stock_cost'])

    # Save JSON без фото для чтения
    clean = copy.deepcopy(results)
    for r in clean:
        r['photo'] = f'[{len(r["photo"])}b]' if r['photo'] else '(нет)'
    OUT_JSON.write_text(json.dumps(clean, ensure_ascii=False, indent=2))
    print(f"\n3. Сохранён JSON: {OUT_JSON}")

    # Сводка
    print("\n=== СВОДКА ===")
    rec_counts = {}
    for r in results:
        rec_counts[r['recommendation']] = rec_counts.get(r['recommendation'], 0) + 1
    for rec in ['REORDER', 'SOLD_OUT', 'HOLD', 'GROWING', 'WAIT', 'DISCOUNT_20', 'DISCOUNT_30', 'DEAD']:
        if rec in rec_counts:
            print(f"  {rec:12s} {rec_counts[rec]}")

    total_stock = sum(r['stock']['total'] for r in results)
    total_supplied = sum(r['supplied_qty'] for r in results)
    total_sold = sum(r['sales']['total_qty'] for r in results)
    total_rev = sum(r['sales']['total_rev'] for r in results)
    total_stock_cost = sum(r['stock_cost'] for r in results)
    total_stock_value = sum(r['stock_value'] for r in results)
    total_l30_q = sum(r['sales']['last_30d']['qty'] for r in results)
    total_l30_r = sum(r['sales']['last_30d']['rev'] for r in results)
    print(f"\n  Моделей:        {len(results)}")
    print(f"  Поставлено:     {total_supplied} шт")
    print(f"  Продано всего:  {total_sold} шт / {total_rev:,.0f} ₸")
    print(f"  Продано 30 дн:  {total_l30_q} шт / {total_l30_r:,.0f} ₸")
    print(f"  Остаток:        {total_stock} шт")
    print(f"  Остат. себес:   {total_stock_cost:,.0f} ₸")
    print(f"  Остат. в РЦ:    {total_stock_value:,.0f} ₸")
    return results


def main_wrapper():
    """Возвращает results для использования render_html."""
    return main()


REC_CONFIG = {
    'REORDER':     {'label': '🟢 ДОЗАКАЗАТЬ'},
    'SOLD_OUT':    {'label': '🟣 ЗАКОНЧИЛСЯ'},
    'GROWING':     {'label': '📈 РАСТЁТ'},
    'HOLD':        {'label': '🔵 ДЕРЖИМ'},
    'WAIT':        {'label': '⚪ ЖДЁМ'},
    'DISCOUNT_20': {'label': '🟠 СКИДКА 15-20%'},
    'DISCOUNT_30': {'label': '🔴 СКИДКА 30%'},
    'DEAD':        {'label': '🔴 СИЛЬНАЯ СКИДКА'},
}


def render_html(results: list):
    """Собирает clothing_v3.html на основе старого шаблона."""
    if not OLD_HTML.exists():
        print(f"⚠️ Нет старого {OLD_HTML}, генерация HTML пропущена")
        return

    old = OLD_HTML.read_text()

    # Новые данные
    items_json = json.dumps(results, ensure_ascii=False)

    # Статы
    total_stock = sum(r['stock']['total'] for r in results)
    total_sold = sum(r['sales']['total_qty'] for r in results)
    total_rev = sum(r['sales']['total_rev'] for r in results)
    total_supplied = sum(r['supplied_qty'] for r in results)
    total_sup_cost = sum(r['supplied_cost'] for r in results)
    total_stock_cost = sum(r['stock_cost'] for r in results)
    total_stock_value = sum(r['stock_value'] for r in results)

    def mk(n):
        if n >= 1_000_000:
            return f'{n/1_000_000:.1f}М'
        if n >= 1000:
            return f'{round(n/1000)}K'
        return str(round(n))

    # 1. Заменить title
    new_html = old.replace(
        '<title>Женская одежда — Dashboard</title>',
        '<title>Женская одежда v3 — Dashboard</title>'
    )

    # 2. Заменить header h1 + badge
    new_html = re.sub(
        r'<h1>Женская одежда</h1>\s*<span class="badge badge-info">\d+ моделей</span>',
        f'<h1>Женская одежда</h1>\n    <span class="badge badge-info">{len(results)} моделей</span>',
        new_html
    )

    # 3. Header meta (дата)
    today_s = date.today().strftime('%d.%m.%Y')
    new_html = re.sub(
        r'<div class="header-meta">Остатки:.*?</div>',
        f'<div class="header-meta">Остатки: {today_s} | Продажи: Янв–Апр 2026 (до {today_s})</div>',
        new_html
    )

    # 4. Заменить initial stats values (8 блоков)
    stats_replacements = [
        (r'<div class="stat-num" id="stat-items">\d+</div>',
         f'<div class="stat-num" id="stat-items">{len(results)}</div>'),
        (r'<div class="stat-num" id="stat-supplied"[^>]*>\d+</div>',
         f'<div class="stat-num" id="stat-supplied" style="color:var(--orange)">{total_supplied}</div>'),
        (r'<div class="stat-num" id="stat-supply-cost"[^>]*>[^<]+</div>',
         f'<div class="stat-num" id="stat-supply-cost" style="color:var(--orange)">{mk(total_sup_cost)}</div>'),
        (r'<div class="stat-num blue" id="stat-stock">\d+</div>',
         f'<div class="stat-num blue" id="stat-stock">{total_stock}</div>'),
        (r'<div class="stat-num" id="stat-cost">[^<]+</div>',
         f'<div class="stat-num" id="stat-cost">{mk(total_stock_cost)}</div>'),
        (r'<div class="stat-num green" id="stat-sold">\d+</div>',
         f'<div class="stat-num green" id="stat-sold">{total_sold}</div>'),
        (r'<div class="stat-num green" id="stat-revenue">[^<]+</div>',
         f'<div class="stat-num green" id="stat-revenue">{mk(total_rev)}</div>'),
        (r'<div class="stat-num purple" id="stat-retail">[^<]+</div>',
         f'<div class="stat-num purple" id="stat-retail">{mk(total_stock_value)}</div>'),
    ]
    for pattern, repl in stats_replacements:
        new_html = re.sub(pattern, repl, new_html)

    # 5. Заменить ALL_ITEMS массив
    new_html = re.sub(
        r'const ALL_ITEMS = \[.*?\];',
        f'const ALL_ITEMS = {items_json};',
        new_html, count=1, flags=re.DOTALL
    )

    # 6. Добавить CSS для рекомендаций перед </style>
    rec_css = '''
/* === Мобильная адаптация (основной use case) === */
@media(max-width:600px) {
  .header { padding:12px 16px; }
  .header h1 { font-size:17px; }
  .stats { grid-template-columns:repeat(3,1fr); gap:6px; padding:10px 12px; top:58px; }
  .stat { padding:8px 6px; }
  .stat-num { font-size:15px; }
  .stat-label { font-size:8px; }
  .controls { top:150px; }
  .filter-bar { top:200px; }
  .item { margin-bottom:10px; }
  .item-main { padding:10px; gap:10px; }
  .item-photo, .item-photo-empty { width:70px; height:70px; }
  .item-name { font-size:13px; }
  .disc-btn { padding:10px 14px; font-size:14px; min-width:50px; min-height:38px; }
  .disc-btns { gap:5px; }
  .bottom { padding:10px 14px; }
  .stock-grid { gap:3px; }
  .stock-cell-val { font-size:13px; }
  .stock-cell-label { font-size:8px; }
  .chip { padding:4px 9px; font-size:12px; }
  .disc-btn:active { background:#eff6ff; }
  .flow-val { font-size:16px; }
  .flow-label { font-size:9px; }
}

.rec-badge { padding:5px 10px; border-radius:6px; font-size:11px; font-weight:800; letter-spacing:0.3px; display:inline-block; margin-bottom:6px; }
.rec-REORDER     { background:#ecfdf5; color:#10b981; }
.rec-SOLD_OUT    { background:#f5f3ff; color:#8b5cf6; }
.rec-GROWING     { background:#ecfdf5; color:#10b981; }
.rec-HOLD        { background:#eff6ff; color:#3b82f6; }
.rec-WAIT        { background:#f3f4f6; color:#6b7280; }
.rec-DISCOUNT_20 { background:#fffbeb; color:#f59e0b; }
.rec-DISCOUNT_30 { background:#fef2f2; color:#ef4444; }
.rec-DEAD        { background:#fef2f2; color:#ef4444; }
.rec-reason      { font-size:12px; color:var(--text2); margin-bottom:8px; line-height:1.4; }
.flow-box        { display:grid; grid-template-columns:repeat(3,1fr); gap:6px; margin-bottom:8px; padding:8px; background:#fafbfc; border-radius:8px; border:1px solid var(--border); }
.flow-cell       { text-align:center; position:relative; }
.flow-label      { font-size:10px; font-weight:700; color:var(--text3); text-transform:uppercase; letter-spacing:0.5px; margin-bottom:2px; }
.flow-val        { font-size:17px; font-weight:800; color:var(--text); }
.flow-sub        { font-size:10px; color:var(--text3); margin-top:2px; }
.velocity-box    { display:flex; gap:10px; padding:8px 10px; background:var(--bg); border-radius:6px; margin-bottom:8px; font-size:12px; align-items:center; flex-wrap:wrap; }
.vel-num         { font-weight:800; color:var(--text); }
.vel-up          { color:#10b981; font-weight:700; }
.vel-down        { color:#ef4444; font-weight:700; }
.vel-flat        { color:var(--text2); font-weight:700; }
.wos-ok          { color:#10b981; font-weight:700; }
.wos-warn        { color:#f59e0b; font-weight:700; }
.wos-bad         { color:#ef4444; font-weight:700; }
.item-photo-empty svg { opacity:0.3; }
.item-photo-empty.wait { background:linear-gradient(135deg,#f0f9ff,#e0f2fe); }
.distribution-hint { font-size:12px; color:var(--orange); margin-top:6px; font-weight:600; padding:6px 10px; background:#fffbeb; border-radius:6px; border-left:3px solid var(--orange); }
.last-sale-info  { font-size:12px; color:var(--text2); margin-top:6px; }
.last-sale-info b { color:var(--text); }

/* Tooltips убраны — вместо них inline-пояснения. Атрибут data-tip остаётся
   в HTML для доступности, но визуально не показываем глючный tooltip. */

/* Легенда "как читать" в начале страницы */
.legend-toggle { padding:8px 14px; background:var(--blue-light); color:var(--blue); border:none; border-radius:8px; font-size:12px; font-weight:700; cursor:pointer; margin:4px 16px 0; font-family:inherit; width:calc(100% - 32px); text-align:left; -webkit-tap-highlight-color:transparent; }
.legend-toggle:active { transform:scale(0.99); }
.legend-body { margin:6px 16px 8px; padding:12px 14px; background:var(--card); border:1px solid var(--border); border-radius:8px; font-size:12px; line-height:1.6; color:var(--text2); display:none; }
.legend-body.open { display:block; }
.legend-body b { color:var(--text); font-weight:700; }
.legend-body .legend-row { margin-bottom:6px; padding-bottom:6px; border-bottom:1px solid var(--border); }
.legend-body .legend-row:last-child { margin-bottom:0; border-bottom:none; padding-bottom:0; }

/* Разбивка по цветам и размерам */
.variants-box { margin:8px 0; padding:8px 10px; background:#fafbfc; border-radius:6px; border:1px solid var(--border); }
.variants-title { font-size:10px; font-weight:700; color:var(--text3); text-transform:uppercase; letter-spacing:0.5px; margin-bottom:6px; }
.variants-chips { display:flex; gap:4px; flex-wrap:wrap; margin-bottom:6px; }
.chip { padding:3px 8px; border-radius:12px; font-size:11px; font-weight:600; background:#fff; border:1px solid var(--border); }
.chip b { font-weight:800; margin-left:3px; }
.chip.zero { background:#fef2f2; color:#ef4444; border-color:#fecaca; }
.chip.low  { background:#fffbeb; color:#f59e0b; border-color:#fde68a; }
.chip.ok   { background:#ecfdf5; color:#10b981; border-color:#a7f3d0; }
</style>'''
    new_html = new_html.replace('</style>', rec_css)

    # 6.5. Вставить легенду после stats
    legend_html = '''<button class="legend-toggle" onclick="this.nextElementSibling.classList.toggle('open'); this.textContent = this.nextElementSibling.classList.contains('open') ? '❓ Как читать — скрыть' : '❓ Как читать эти цифры'">❓ Как читать эти цифры</button>
<div class="legend-body">
  <div class="legend-row"><b>% справа вверху</b> — sell-through: сколько % от завоза уже продано. 60%+ = хит.</div>
  <div class="legend-row"><b>Большая цифра справа</b> — текущий остаток в штуках по всем складам.</div>
  <div class="legend-row"><b>⚡ Скорость</b> — сколько штук продано за последние 30 дней. <b>Ускорение ×N</b> — сравнение с средней скоростью: ×1.2+ значит весна/сезон разогнали продажи.</div>
  <div class="legend-row"><b>📦 Запас на X нед</b> — сколько недель хватит текущего остатка при нынешней скорости. <4 нед = срочно дозаказать, >10 = много.</div>
  <div class="legend-row"><b>🎨 Цвета и 📏 Размеры</b> — разбивка остатка. <span style="color:#ef4444">красный</span> = 0 (дыра в сетке), <span style="color:#f59e0b">жёлтый</span> = 1-2 шт (мало), <span style="color:#10b981">зелёный</span> = норма.</div>
  <div class="legend-row"><b>Рекомендация</b>: 🟢 ДОЗАКАЗАТЬ = хит, 📈 РАСТЁТ = продажи ускоряются (скидку не давать), 🔵 ДЕРЖИМ = норма, 🟠 СКИДКА = плохо идёт, 🔴 СИЛЬНАЯ СКИДКА = мёртвый товар.</div>
  <div class="legend-row"><b>🚛 Подсказка</b> появляется когда в магазине дисбаланс — например, в Москве 0 шт, но на складе лежит.</div>
</div>
'''
    new_html = new_html.replace(
        '<div class="controls">',
        legend_html + '\n<div class="controls">'
    )

    # 7. Добавить recommendation фильтры в filter-bar
    subfolders = sorted({it['subfolder'] for it in results})
    filter_buttons = ['<button class="filter-btn active" onclick="setFilter(\'all\')">Все</button>']
    # Рекомендации (с эмодзи)
    rec_emoji = {'REORDER': '🟢', 'SOLD_OUT': '🟣', 'GROWING': '📈', 'HOLD': '🔵', 'DISCOUNT_20': '🟠', 'DISCOUNT_30': '🔴', 'DEAD': '🔴', 'WAIT': '⚪'}
    rec_counts = {}
    for r in results:
        rc = r['recommendation']
        rec_counts[rc] = rec_counts.get(rc, 0) + 1
    for rc in ['REORDER', 'DEAD', 'DISCOUNT_20', 'GROWING', 'HOLD', 'SOLD_OUT', 'WAIT']:
        if rc in rec_counts:
            label = REC_CONFIG[rc]['label']
            filter_buttons.append(
                f'<button class="filter-btn" onclick="setFilter(\'rec:{rc}\')">{rec_emoji.get(rc,"")} {label} ({rec_counts[rc]})</button>'
            )
    # Категории
    for sf in subfolders:
        filter_buttons.append(f'<button class="filter-btn" onclick="setFilter(\'{sf}\')">{sf}</button>')

    new_filter_bar = '\n'.join(filter_buttons)
    new_html = re.sub(
        r'<div class="filter-bar" id="filterBar">.*?</div>',
        f'<div class="filter-bar" id="filterBar">\n{new_filter_bar}\n</div>',
        new_html, count=1, flags=re.DOTALL
    )

    # 8. Обновить applyFilters — поддержка фильтра rec:XXX
    new_html = new_html.replace(
        "if (currentFilter !== 'all' && item.subfolder !== currentFilter) return false;",
        """if (currentFilter !== 'all') {
      if (currentFilter.startsWith('rec:')) {
        if (item.recommendation !== currentFilter.slice(4)) return false;
      } else if (item.subfolder !== currentFilter) return false;
    }"""
    )

    # 9. Обновить setFilter — active class по onclick значению
    # (старый сравнивал по textContent — у нас теперь с эмодзи и числом не сработает)
    new_html = new_html.replace(
        """document.querySelectorAll('.filter-btn').forEach(b => {
    b.classList.toggle('active',
      (f === 'all' && b.textContent === 'Все') || b.textContent === f
    );
  });""",
        """document.querySelectorAll('.filter-btn').forEach(b => {
    const onclick = b.getAttribute('onclick') || '';
    const m = onclick.match(/setFilter\\('([^']+)'\\)/);
    b.classList.toggle('active', m && m[1] === f);
  });"""
    )

    # 10. Добавить сортировки по velocity/WOS
    new_html = new_html.replace(
        '<option value="cost">По себестоимости</option>',
        '<option value="cost">По себестоимости</option>\n    <option value="velocity">По скорости (30д)</option>\n    <option value="wos_asc">По WOS (срочно)</option>'
    )
    new_html = new_html.replace(
        "case 'cost': return (b.stock_cost || 0) - (a.stock_cost || 0);",
        """case 'cost': return (b.stock_cost || 0) - (a.stock_cost || 0);
      case 'velocity': return (b.velocity.recent_30d || 0) - (a.velocity.recent_30d || 0);
      case 'wos_asc': return (a.wos || 999) - (b.wos || 999);"""
    )

    # 11. Заменить renderItem целиком — новая версия с русскими лейблами и tooltips
    REC_LABELS_JS = json.dumps({k: v['label'] for k, v in REC_CONFIG.items()}, ensure_ascii=False)
    new_render = '''
function renderVariants(item) {
  const v = item.variants || {colors: {}, sizes: {}};
  const colors = v.colors || {};
  const sizes = v.sizes || {};
  const colorKeys = Object.keys(colors);
  const sizeKeys = Object.keys(sizes);
  if (!colorKeys.length && !sizeKeys.length) return '';

  const colorChips = Object.entries(colors)
    .sort((a,b) => b[1] - a[1])
    .map(([c, q]) => {
      const cls = q === 0 ? 'zero' : q <= 2 ? 'low' : 'ok';
      return `<span class="chip ${cls}" data-tip="${c}: ${q} шт в наличии">${c}<b>${q}</b></span>`;
    }).join('');

  // Размеры: показать все, даже нулевые (чтобы видно было дыры)
  const allSizesOrder = ['XS','S','M','L','XL','2XL','3XL','XXL','XXXL'];
  const numericSizes = sizeKeys.filter(s => /^\\d+/.test(s)).sort((a,b)=>parseFloat(a)-parseFloat(b));
  const orderedSizes = [...allSizesOrder.filter(s => s in sizes), ...numericSizes, ...sizeKeys.filter(s => !(allSizesOrder.includes(s) || /^\\d+/.test(s)))];
  const seen = new Set();
  const sizeChips = orderedSizes.filter(s => { if(seen.has(s)) return false; seen.add(s); return true; })
    .map(s => {
      const q = sizes[s];
      const cls = q === 0 ? 'zero' : q <= 2 ? 'low' : 'ok';
      return `<span class="chip ${cls}" data-tip="Размер ${s}: ${q} шт">${s}<b>${q}</b></span>`;
    }).join('');

  return `<div class="variants-box">
    ${colorKeys.length ? `<div class="variants-title">🎨 Цвета (${colorKeys.length})</div><div class="variants-chips">${colorChips}</div>` : ''}
    ${sizeKeys.length ? `<div class="variants-title" style="margin-top:${colorKeys.length?'6px':'0'}">📏 Размеры (${sizeKeys.length})</div><div class="variants-chips">${sizeChips}</div>` : ''}
  </div>`;
}

function renderItem(item, idx) {
  const itemKey = item.article || item.name;
  const disc = discounts[itemKey] || 0;
  const newPrice = disc > 0 ? Math.round(item.retail * (1 - disc/100)) : 0;
  const stBadge = stClass(item.sell_through);
  const REC_LABELS = ''' + REC_LABELS_JS + ''';

  // Фото или placeholder
  const photoHtml = item.photo
    ? `<img class="item-photo" src="data:image/jpeg;base64,${item.photo}" alt="${item.name}" onclick="openLightbox('${(item.article || item.name).replace(/'/g,"")}')">`
    : `<div class="item-photo-empty wait"><svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="2"/><path d="M21 15l-5-5L5 21"/></svg><div class="nf">нет фото</div></div>`;

  // Месяцы — только те что >0
  const monthsHtml = [
    item.sales.jan.qty > 0 ? `<span class="month-tag" data-tip="Январь 2026: ${item.sales.jan.qty} шт / ${fmt(Math.round(item.sales.jan.rev))} ₸">Янв: <b>${item.sales.jan.qty}</b></span>` : '',
    item.sales.feb.qty > 0 ? `<span class="month-tag" data-tip="Февраль 2026: ${item.sales.feb.qty} шт / ${fmt(Math.round(item.sales.feb.rev))} ₸">Фев: <b>${item.sales.feb.qty}</b></span>` : '',
    item.sales.mar.qty > 0 ? `<span class="month-tag" data-tip="Март 2026: ${item.sales.mar.qty} шт / ${fmt(Math.round(item.sales.mar.rev))} ₸">Мар: <b>${item.sales.mar.qty}</b></span>` : '',
    item.sales.apr.qty > 0 ? `<span class="month-tag" data-tip="Апрель 2026 (до сегодня): ${item.sales.apr.qty} шт / ${fmt(Math.round(item.sales.apr.rev))} ₸">Апр: <b>${item.sales.apr.qty}</b></span>` : '',
  ].filter(Boolean).join(' ');

  // Скорость + WOS — с понятными inline-метками
  const vel = item.velocity || {recent_30d: 0, avg_30d: 0, acceleration: 0};
  const accelClass = vel.acceleration >= 1.2 ? 'vel-up' : vel.acceleration <= 0.8 && vel.acceleration > 0 ? 'vel-down' : 'vel-flat';
  let accelTxt = '';
  if (vel.acceleration >= 1.2) accelTxt = `📈 ускорение ×${vel.acceleration.toFixed(2)}`;
  else if (vel.acceleration >= 0.8) accelTxt = `➡️ стабильно ×${vel.acceleration.toFixed(2)}`;
  else if (vel.acceleration > 0) accelTxt = `📉 замедляется ×${vel.acceleration.toFixed(2)}`;
  else accelTxt = '➖ нет данных';

  const wos = item.wos || 999;
  const wosClass = wos < 4 ? 'wos-bad' : wos < 10 ? 'wos-warn' : wos >= 99 ? '' : 'wos-ok';
  let wosLabel;
  if (wos >= 99) wosLabel = 'не продаётся';
  else if (wos < 4) wosLabel = `запас на ${wos.toFixed(0)} нед (мало)`;
  else if (wos < 10) wosLabel = `запас на ${wos.toFixed(0)} нед (норма)`;
  else wosLabel = `запас на ${wos.toFixed(0)} нед (много)`;

  const velHtml = `<div class="velocity-box">
    <span>⚡ <b>${vel.recent_30d}</b> шт/мес (посл 30 дн)</span>
    <span style="color:var(--text3)">среднее ${vel.avg_30d}</span>
    <span class="${accelClass}">${accelTxt}</span>
    <span style="margin-left:auto" class="${wosClass}">📦 ${wosLabel}</span>
  </div>`;

  // Рекомендация + причина
  const recLabel = REC_LABELS[item.recommendation] || item.recommendation;
  const recHtml = `<div><span class="rec-badge rec-${item.recommendation}" data-tip="Алгоритм: учитывает % продано, скорость за 30 дн, ускорение, остаток">${recLabel}</span>
    <div class="rec-reason">${item.recommendation_reason || ''}</div></div>`;

  // Блок "Завезено → Продано → Осталось"
  const flowHtml = `<div class="flow-box">
    <div class="flow-cell" data-tip="Сколько пришло всего (с первой поставки)">
      <div class="flow-label">Завезено</div>
      <div class="flow-val">${item.supplied_qty}</div>
      <div class="flow-sub">${item.first_supply_date || ''}</div>
    </div>
    <div class="flow-cell" data-tip="Сколько уже продано с первой поставки (без возвратов)">
      <div class="flow-label">Продано</div>
      <div class="flow-val" style="color:#10b981">${item.sales.total_qty}</div>
      <div class="flow-sub">${item.sell_through.toFixed(1)}% от завоза</div>
    </div>
    <div class="flow-cell" data-tip="Текущий остаток по всем складам">
      <div class="flow-label">Остаток</div>
      <div class="flow-val" style="color:#3b82f6">${item.stock.total}</div>
      <div class="flow-sub">${Math.max(0, item.supplied_qty - item.sales.total_qty - item.stock.total) > 0 ? 'потери: ' + (item.supplied_qty - item.sales.total_qty - item.stock.total) : ''}</div>
    </div>
  </div>`;

  // Разбивка по цветам и размерам (из снапшота)
  const variantsHtml = renderVariants(item);

  // Подсказка по распределению (если дисбаланс)
  let distHint = '';
  if (item.stock.total > 0) {
    if (item.stock.moscow === 0 && (item.stock.tsum_online + item.stock.aruzhan + item.stock.warehouse) >= 3) {
      distHint = '<div class="distribution-hint">🚛 В Москве 0 — двинь туда (там 53% продаж)</div>';
    } else if (item.stock.warehouse >= 5 && item.stock.moscow < 2) {
      distHint = '<div class="distribution-hint">🚛 На складе ' + item.stock.warehouse + ' шт — раскидать по магазинам</div>';
    }
  }

  // Последняя продажа
  let lastSaleInfo = '';
  if (item.last_sale) {
    lastSaleInfo = `<div class="last-sale-info" data-tip="Когда был последний чек с этим товаром">Последняя продажа: <b>${item.last_sale}</b> (${item.days_since_last_sale} дн назад)</div>`;
  } else if (item.sales.total_qty === 0) {
    lastSaleInfo = `<div class="last-sale-info" style="color:#ef4444">⚠️ Продаж не было ни разу за ${item.days_since_first} дней</div>`;
  }

  // Артикул: скрыть если null
  const articleLine = item.article
    ? `<div class="item-article" data-tip="Артикул товара в МойСкладе"><span style="color:var(--blue);cursor:pointer" onclick="navigator.clipboard.writeText('${item.article}').then(()=>toast('${item.article} скопирован'))">${item.article} 📋</span></div>`
    : `<div class="item-article" style="color:var(--text3)" data-tip="Артикул не извлечён из имени (буквенные коды СКxxx у одежды)">—</div>`;

  return `
  <div class="item" data-article="${item.article || item.name}">
    <div class="item-main">
      ${photoHtml}
      <div class="item-body">
        <div class="item-name">${item.name}</div>
        ${articleLine}
        ${item.subfolder ? `<span class="item-subfolder ${sfClass(item.subfolder)}" data-tip="Категория товара">${item.subfolder}</span>` : ''}
      </div>
      <div class="item-right">
        <span class="st-badge ${stBadge}" data-tip="Sell-through: ${item.sell_through.toFixed(1)}% — доля проданного от всего завоза">${item.sell_through > 0 ? item.sell_through.toFixed(1) + '%' : '—'}</span>
        <div class="stock-total" data-tip="Текущий остаток шт на всех складах">${item.stock.total}</div>
      </div>
    </div>
    <div class="item-details">
      ${recHtml}
      ${flowHtml}
      ${velHtml}
      <div class="stock-grid">
        <div class="stock-cell" data-tip="Остаток в магазине Москва (ТРЦ Москва, ~53% продаж)">
          <div class="stock-cell-label">Мск</div>
          <div class="stock-cell-val ${item.stock.moscow === 0 ? 'zero' : ''}">${item.stock.moscow}</div>
        </div>
        <div class="stock-cell" data-tip="ЦУМ + Online New (общий физический склад)">
          <div class="stock-cell-label">ЦУМ+Онл</div>
          <div class="stock-cell-val ${item.stock.tsum_online === 0 ? 'zero' : ''}">${item.stock.tsum_online}</div>
        </div>
        <div class="stock-cell" data-tip="Магазин в Астане (Аружан), ~11% продаж">
          <div class="stock-cell-label">Аружан</div>
          <div class="stock-cell-val ${item.stock.aruzhan === 0 ? 'zero' : ''}">${item.stock.aruzhan}</div>
        </div>
        <div class="stock-cell" data-tip="Основной склад — ещё не распределено по магазинам">
          <div class="stock-cell-label">Склад</div>
          <div class="stock-cell-val ${item.stock.warehouse === 0 ? 'zero' : ''}">${item.stock.warehouse}</div>
        </div>
      </div>
      ${variantsHtml}
      ${distHint}
      <div class="sales-row" style="margin-top:8px">
        <span class="sales-tag" data-tip="Всего продано шт за всё время"><b>${item.sales.total_qty}</b> шт продано</span>
        ${item.sales.total_rev > 0 ? `<span class="sales-tag" data-tip="Выручка от продаж этой модели"><b>${fmtK(item.sales.total_rev)}₸</b> выручка</span>` : ''}
        ${item.gross_profit > 0 ? `<span class="sales-tag" data-tip="Прибыль = выручка − себес × продано"><b>${fmtK(item.gross_profit)}₸</b> прибыль</span>` : ''}
        ${monthsHtml}
      </div>
      ${lastSaleInfo}
      <div class="price-row">
        <div class="price-item" data-tip="Себестоимость одной единицы (средняя цена закупки)"><span class="label">Себестоимость</span> <span class="val">${item.cost > 0 ? fmt(Math.round(item.cost)) + '₸' : '—'}</span></div>
        <div class="price-item" data-tip="Розничная цена из прайса МойСклад"><span class="label">Цена в магазине</span> <span class="val">${item.retail > 0 ? fmt(Math.round(item.retail)) + '₸' : '—'}</span></div>
        ${item.margin_pct > 0 ? `<div class="price-item" data-tip="Маржа = (РЦ − Себес) / РЦ"><span class="label">Маржа</span> <span class="val">${item.margin_pct}%</span></div>` : ''}
        ${item.avg_sale_price > 0 && Math.abs(item.avg_sale_price - item.retail) > 100 ? `<div class="price-item" data-tip="Средняя цена реальных продаж — отличается от РЦ, значит были скидки"><span class="label">Ср. цена продажи</span> <span class="val" style="color:#f59e0b">${fmt(item.avg_sale_price)}₸</span></div>` : ''}
        ${item.stock_value > 0 ? `<div class="price-item" data-tip="Стоимость остатка по рознице"><span class="label">Остаток в РЦ</span> <span class="val">${fmtK(item.stock_value)}₸</span></div>` : ''}
        ${item.stock_cost > 0 ? `<div class="price-item" data-tip="Сумма замороженного капитала (остаток × себес)"><span class="label">Заморожено</span> <span class="val">${fmtK(item.stock_cost)}₸</span></div>` : ''}
      </div>
      <div class="discount-row" style="flex-wrap:wrap" data-tip="Выбери скидку — сохранится локально и в Supabase при нажатии Сохранить">
        <label>Скидка</label>
        <div class="disc-btns">
          ${[0,5,10,15,20,25,30,40,50].map(d => `<button type="button" class="disc-btn ${disc==d && d>0?'active':''}" onclick="onDiscount('${(item.article || item.name).replace(/'/g,"").replace(/"/g,"")}',${d})">${d?d+'%':'✕'}</button>`).join('')}
        </div>
        ${newPrice > 0
          ? `<span class="new-price" data-tip="Новая цена после скидки">${fmt(newPrice)}₸</span>`
          : `<span class="new-price" style="color:var(--text3)">${item.retail > 0 ? fmt(Math.round(item.retail)) + '₸' : '—'}</span>`
        }
      </div>
    </div>
  </div>`;
}
'''
    # Заменить старую функцию renderItem целиком (lambda чтобы не ломать \d в JS regex)
    new_html = re.sub(
        r'function renderItem\(item, idx\) \{.*?\n\}\s*\n',
        lambda _: new_render + '\n',
        new_html, count=1, flags=re.DOTALL
    )

    # 12. Починить синтаксическую ошибку в старом коде (пропущен ?)
    new_html = new_html.replace(
        """const photoHtml = item.photo
    : `<div class="item-photo-empty"><div class="art">${item.article}</div><div class="nf">нет фото</div></div>`;""",
        """const photoHtml = item.photo
    ? `<img class="item-photo" src="data:image/jpeg;base64,${item.photo}" alt="${item.name}" onclick="openLightbox('${item.article || item.name.replace(/'/g, '')}')">`
    : `<div class="item-photo-empty"><div class="art">${(item.article || '—').substring(0,8)}</div><div class="nf">нет фото</div></div>`;"""
    )

    # 13. Исправить article в search/null handling — у нас многие article=null
    new_html = new_html.replace(
        "if (q && !item.name.toLowerCase().includes(q) && !item.article.toLowerCase().includes(q)) return false;",
        "if (q && !item.name.toLowerCase().includes(q) && !(item.article || '').toLowerCase().includes(q)) return false;"
    )

    # Также article копирование — защита от null
    new_html = new_html.replace(
        "navigator.clipboard.writeText('${item.article}')",
        "navigator.clipboard.writeText('${item.article || item.name}')"
    )

    # 14. Починить saveDiscounts, updateStats, exportCSV, onDiscount — использовать item.article || item.name везде
    # 14a. updateStats и exportCSV: discounts[i.article] → discounts[i.article || i.name]
    new_html = new_html.replace(
        "const disc = discounts[i.article] || 0;\n    const price = disc > 0 ? i.retail * (1 - disc/100) : i.retail;",
        "const disc = discounts[i.article || i.name] || 0;\n    const price = disc > 0 ? i.retail * (1 - disc/100) : i.retail;"
    )
    new_html = new_html.replace(
        "const disc = discounts[i.article] || 0;\n    const newP = disc > 0 ? Math.round(i.retail * (1 - disc/100)) : i.retail;",
        "const disc = discounts[i.article || i.name] || 0;\n    const newP = disc > 0 ? Math.round(i.retail * (1 - disc/100)) : i.retail;"
    )
    # 14b. saveDiscounts
    new_html = new_html.replace(
        "const discountItems = ALL_ITEMS.filter(i => discounts[i.article]).map(i => ({",
        "const discountItems = ALL_ITEMS.filter(i => discounts[i.article || i.name]).map(i => ({"
    )
    new_html = new_html.replace(
        "discount: discounts[i.article],",
        "discount: discounts[i.article || i.name],"
    )
    new_html = new_html.replace(
        "new_price: Math.round(i.retail * (1 - discounts[i.article]/100)),",
        "new_price: Math.round(i.retail * (1 - discounts[i.article || i.name]/100)),"
    )
    # 14c. onDiscount — в re-render найти item тоже по fallback, и обновлять новую кнопку
    new_html = new_html.replace(
        "const item = ALL_ITEMS.find(i => i.article === article);",
        "const item = ALL_ITEMS.find(i => (i.article || i.name) === article);"
    )
    # CSS активного состояния button
    new_html = new_html.replace(
        '.disc-btn.active { background:var(--red); color:#fff; border-color:var(--red); }',
        '.disc-btn.active { background:var(--red); color:#fff; border-color:var(--red); }\n.disc-btn { -webkit-appearance:none; appearance:none; }\n.disc-btn:focus { outline:none; }'
    )

    OUT_HTML.write_text(new_html)
    size_mb = OUT_HTML.stat().st_size / 1024 / 1024
    print(f"\n4. Сгенерирован HTML: {OUT_HTML} ({size_mb:.1f} MB)")


if __name__ == '__main__':
    results = main()
    render_html(results)
