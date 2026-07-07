#!/usr/bin/env python3
"""
Builder для sneakers.html — дашборд всех кроссовок с категоризацией для скидок.

Включает все модели обуви (артикулы 200000-209999):
- UNPROFITABLE (убыточные — продаём дешевле или равно себес)
- DEAD (мёртвые — >90 дней без продаж)
- SLOW (медленные — 60-90 дней, ≤3 шт/мес)
- INTENTIONAL (намеренный неликвид — NB/Asics/Puma)
- HOT (хиты — быстро продаются, нужно дозаказать)
- NORMAL (норма)

Запуск: python3 sneaker-order/build_sneakers.py
"""
import duckdb
import json
import re
import sys
import os
import base64
import copy
from datetime import date, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
DB_PATH = PROJECT_ROOT / 'data' / 'pnlpower.duckdb'
TEMPLATE_HTML = PROJECT_ROOT / 'sneaker-order' / 'clothing.html'  # используем как шаблон
OUT_HTML = PROJECT_ROOT / 'sneaker-order' / 'sneakers.html'
OUT_JSON = PROJECT_ROOT / 'sneaker-order' / 'sneakers_data.json'
PHOTO_CACHE = PROJECT_ROOT / 'sneaker-order' / '.photo_cache_sneakers.json'

SNAPSHOT_DATE = None  # None = взять свежайший (динамически в main)
PRICES_DATE = None    # None = взять свежайший


def latest_table_date(con, prefix):
    row = con.execute(f"""SELECT table_name FROM information_schema.tables
        WHERE table_name LIKE '{prefix}%' ORDER BY table_name DESC LIMIT 1""").fetchone()
    return row[0][-8:]


def is_summer_item(name: str) -> bool:
    """Летний товар — сейлить агрессивнее к концу сезона."""
    nm = (name or '').lower()
    return any(w in nm for w in ('crocs', 'слайд', 'slide', 'adilette', 'сланц',
                                 'сандал', 'sandal', 'mule', 'клоги', 'clog'))


def is_slide_item(name: str) -> bool:
    """Сланцы/шлёпанцы/Birkenstock — отдельная сейл-группа (решение Ерлана 07.07.2026:
    ВСЕ сланцы на скидку −20% заранее, сгруппировать вместе)."""
    nm = (name or '').lower()
    return any(w in nm for w in ('сланц', 'слайд', 'slide', 'шлёпанц', 'шлепанц',
                                 'adilette', 'birkenstock', 'вьетнам', 'flip flop'))


def fetch_reorder_articles() -> set:
    """Артикулы из последнего заказа ЗК-xxx (draft/sent) в Supabase —
    их НЕ уценивать: мы их только что дозаказали."""
    try:
        import requests
        from dotenv import load_dotenv
        load_dotenv(PROJECT_ROOT / '.env')
        url = os.getenv('SUPABASE_URL'); key = os.getenv('SUPABASE_KEY')
        if not url or not key:
            return set()
        r = requests.get(
            f"{url}/rest/v1/orders?select=id,items&id=like.ЗК-*"
            f"&status=in.(draft,sent,supplier_done)&order=id.desc&limit=2",
            headers={"apikey": key, "Authorization": f"Bearer {key}"}, timeout=15)
        arts = set()
        for o in (r.json() if r.ok else []):
            for it in (o.get('items') or []):
                if it.get('article'):
                    arts.add(str(it['article']))
        return arts
    except Exception:
        return set()


def detect_brand(name: str) -> str:
    nm = (name or '').lower()
    if 'jordan' in nm: return 'Jordan'
    if 'yeezy' in nm: return 'Yeezy'
    if 'air max' in nm or 'air force' in nm or 'dunk' in nm or 'nike' in nm or 'noсta' in nm: return 'Nike'
    if 'adidas' in nm or 'samba' in nm or 'spezial' in nm or 'gazelle' in nm or 'superstar' in nm: return 'Adidas'
    if 'new balance' in nm or 'nb ' in nm: return 'New Balance'
    if 'asics' in nm: return 'Asics'
    if 'puma' in nm: return 'Puma'
    if 'converse' in nm: return 'Converse'
    if 'reebok' in nm: return 'Reebok'
    if 'salomon' in nm: return 'Salomon'
    if 'vans' in nm: return 'Vans'
    if 'ugg' in nm: return 'UGG'
    if 'crocs' in nm: return 'Crocs'
    if 'balenciaga' in nm: return 'Balenciaga'
    if 'mizuno' in nm: return 'Mizuno'
    if 'saucony' in nm: return 'Saucony'
    if 'on cloud' in nm or 'cloudtilt' in nm: return 'On'
    if 'onitsuka' in nm: return 'Onitsuka'
    if 'travis scott' in nm or 'sb dunk' in nm: return 'Nike'
    return 'Прочее'


def is_intentional_dead(name: str) -> bool:
    """NB/Asics/Puma — намеренно убранные, не считать неликвидом."""
    nm = (name or '').lower()
    return 'new balance' in nm or 'asics' in nm or 'puma' in nm or ' nb ' in f' {nm} '


def categorize(item: dict) -> tuple[str, str]:
    """Базовая категория + сезонные/заказные оверрайды (июль 2026):
    1. 📦 REORDERED — артикул в свежем заказе ЗК-xxx: мы его дозаказали, уценять нельзя
       (кроме UNPROFITABLE — утечку денег показываем всегда).
    2. 🍂 HOLD_AUTUMN — осенью 2025 продавался сильно, сейчас затих: сезонная модель,
       через 6-8 недель начнёт продаваться сама — НЕ уценять летом.
    3. ☀️ SUMMER_CLEAR — летний товар (Crocs/слайды/сандалии) в сейл-категории:
       сезон уходит, уценять СЕЙЧАС и агрессивнее, к сентябрю станет мёртвым грузом.
    """
    cat, reason = _base_categorize(item)
    tag = item.get('season_tag') or ''   # ручной тег из model_tags (Supabase) — ГЛАВНЕЕ эвристик

    def zk_warn():
        w = ''
        if item.get('in_reorder'):
            w = ' ⚠️ ЕСТЬ В ЗАКАЗЕ ЗК-016 — если сейлим, убери её из заказа крестиком!'
        if item['cost'] > 0 and item['retail'] > 0 and item['retail'] < item['cost'] * 1.1:
            w += ' ⚠️ РЦ уже у себеса — скидка уведёт в минус.'
        return w

    # Сланцы — тег или имя. ВСЕ в одну сейл-группу с −20%
    if tag == 'slides' or (not tag and is_slide_item(item['name'])):
        return ('SLIDES', f'🩴 Сланцы — сейл −20%, лето уходит.{zk_warn()}')

    # Тег ЛЕТО — сейлить сейчас (сезон уходит), даже если модель ещё продаётся
    if tag == 'summer' and cat != 'UNPROFITABLE':
        return ('SUMMER_CLEAR', f'☀️ ЛЕТНЯЯ (тег) — сезон уходит, уценять сейчас.{zk_warn()} (Было бы: {reason})')

    if cat != 'UNPROFITABLE' and item.get('in_reorder'):
        return ('REORDERED', f'📦 В свежем заказе ЗК — дозаказан, НЕ уценять. (Было бы: {reason})')

    # Тег ВСЕСЕЗОН — сезонные оверрайды не применяются (AF1 White и т.п.)
    if tag == 'allseason':
        return (cat, f'💎 Всесезонная (тег). {reason}')

    if cat in ('DEAD', 'SLOW', 'COOLING_SALE'):
        # Тег осень/зима/деми — сезон впереди, не уценять летом
        if tag in ('autumn', 'winter', 'demi'):
            tl = {'autumn': '🍂 Осень-Зима', 'winter': '❄️ Зима', 'demi': '🌗 Демисезон'}[tag]
            return ('HOLD_AUTUMN', f'{tl} (тег) — сезон впереди, ЖДАТЬ, не уценять. (Было бы: {reason})')
        autumn_q = item.get('autumn_q', 0)
        s30 = item['sales']['s30']
        # осенью продавался заметно (>=12 за 3 мес) и заметно бодрее чем сейчас
        if autumn_q >= 12 and s30 < (autumn_q / 3) * 0.6:
            return ('HOLD_AUTUMN',
                    f'🍂 Осень-2025: {autumn_q} шт (~{autumn_q/3:.0f}/мес), сейчас {s30}/мес — '
                    f'сезонная, ЖДАТЬ сентября, не уценять. (Было бы: {reason})')
        if is_summer_item(item['name']):
            return ('SUMMER_CLEAR',
                    f'☀️ ЛЕТНИЙ товар, сезон уходит — уценять сейчас, +10-15% к обычной скидке. {reason}')

    return (cat, reason)


def _base_categorize(item: dict) -> tuple[str, str]:
    """Возвращает (код, причина по-русски)."""
    cost = item['cost']; retail = item['retail']
    s30 = item['sales']['s30']; s90 = item['sales']['s90']; total = item['stock']['total']
    days_no_sale = item['days_no_sale']
    days_since_supply = item['days_since_supply']

    # Намеренно убранные NB/Asics/Puma
    if is_intentional_dead(item['name']):
        return ('INTENTIONAL', 'NB/Asics/Puma — намеренный неликвид, не трогать')

    # Убыточные (РЦ < себес или маржа < 10%)
    # ВАЖНО: рекомендация зависит от того идёт ли товар:
    # - есть продажи → поднять цену (есть спрос, нечего терять маржу)
    # - не идёт → сейлить и списать (себес уже потерян, важнее вернуть кэш)
    if cost > 0 and retail > 0 and retail < cost * 1.1:
        loss = cost - retail
        if s30 > 0:
            # Есть продажи — поднять цену чтобы зафиксировать маржу
            return ('UNPROFITABLE', f'РЦ {retail:,.0f} ≤ себес {cost:,.0f} (теряем {loss:,.0f}/пара). Продано 30д={s30} → ПОДНЯТЬ цену до {cost*1.3:,.0f}')
        elif days_no_sale and days_no_sale > 30:
            # Нет продаж и убыточный — лучше сейл (себес уже потерян)
            new_p = int(retail * 0.7)
            return ('UNPROFITABLE', f'РЦ {retail:,.0f} < себес {cost:,.0f}, НЕ продаётся {days_no_sale}д. Сейлить -30% (→ {new_p:,.0f}₸) и списать остатки')
        else:
            return ('UNPROFITABLE', f'РЦ {retail:,.0f} ≤ себес {cost:,.0f} (теряем {loss:,.0f}/пара). Поднять цену или ждать ещё 30д')

    # Слишком новый — не судить
    if days_since_supply < 30 and total >= 3 and s30 < 3:
        return ('NEW', f'Пришло {days_since_supply} дн назад, рано судить')

    # Мёртвые — давно без продаж
    if days_no_sale > 90 and total >= 3 and s30 == 0:
        return ('DEAD', f'{days_no_sale} дн без продаж, {total} пар лежат — скидка 40-50% или списание')

    # ОСТЫВАЮТ — был приличный темп за 90д, но последний месяц просел >40%.
    # ВЫШЕ SLOW/HOT: остывание — про ТРЕНД (падает), а не абсолютный уровень.
    # Падающий хит надо ловить тут, а не прятать в «Хиты». Развилка (лови РАНО):
    #   сетка выбита (ходовой размер продавался, но кончился) → ДОЗАКАЗ, не скидка
    #   сетка полная (ходовые на месте, но не берут)          → мягкая СКИДКА
    if s90 >= 9 and total >= 3 and s30 < s90 / 3 * 0.6:
        core = {'37', '38', '39', '41', '42', '43'}
        in_sizes = {str(k).split('.')[0] for k, v in (item.get('sizes') or {}).items() if v > 0}
        sold_sizes = set(item.get('sizes_sold') or [])
        missing = (core & sold_sizes) - (core & in_sizes)
        avg = round(s90 / 3, 1)
        # Общий запас в неделях по ИСТОРИЧЕСКОМУ темпу (до остывания).
        wos_total = total / (s90 / 13) if s90 else 999
        # ДОЗАКАЗ — только если выбит ходовой И модель НЕ затоварена (запас ≤12 нед).
        # Иначе (затоварена ИЛИ сетка полная) → скидка: дозаказ к избытку = мёртвый сток.
        if missing and wos_total <= 12:
            # Пограничные: 30д=0 (совсем встал — спрос мог упасть, не только размер)
            # ИЛИ запас уже средний (>6 нед) → НЕ закупать вслепую, проверить руками.
            if s30 == 0 or wos_total > 6:
                return ('COOLING_REORDER',
                        f'⚠️ ПРОВЕРИТЬ РУКАМИ: выбиты {sorted(missing)}, НО {"30д=0 (спрос мог упасть)" if s30==0 else f"запас уже {wos_total:.0f} нед"}. Завозить минимум или пропустить')
            return ('COOLING_REORDER',
                    f'🔥 ГОРИТ ({avg}→{s30}/мес), запас {wos_total:.0f} нед. Ходовые {sorted(missing)} ВЫБИТЫ — ДОЗАКАЗАТЬ (товар хотят, нет размера)')
        else:
            months = int(total / max(s30, 0.5))
            why = (f'выбит {sorted(missing)}, но затоварено ({wos_total:.0f} нед) — дозаказ не нужен'
                   if missing else 'сетка полная')
            disc = 20 if (s30 < avg * 0.25 and months > 10) else (15 if (s30 < avg * 0.5 or months > 6) else 10)
            return ('COOLING_SALE',
                    f'Сдаёт обороты ({avg}→{s30}/мес), {why}, ~{months} мес запаса → мягкая скидка −{disc}%')

    # Медленные — лежат и слабо продаются
    if days_since_supply > 60 and s30 <= 3 and total >= 5:
        return ('SLOW', f'{s30} шт/мес × {total} пар = >{int(total/max(s30,1))} мес запаса — скидка 20-30%')

    # Хиты — быстро продаются
    if s30 >= 8:
        return ('HOT', f'Скорость {s30} шт/мес — пополнить если есть остаток')

    # Норма
    return ('NORMAL', f'Стабильно: {s30} шт/мес, {total} пар на складе')


def load_photo_cache() -> dict:
    if PHOTO_CACHE.exists():
        return json.loads(PHOTO_CACHE.read_text())
    return {}


def save_photo_cache(cache: dict):
    PHOTO_CACHE.write_text(json.dumps(cache))


def fetch_photos_from_ms(article_to_name: dict, cached: dict) -> dict:
    """Скачивает мини-фото из МойСклад с timeout=4 секунд (пропускает проблемные)."""
    import requests
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / '.env')

    base_url = "https://api.moysklad.ru/api/remap/1.2"
    token = os.getenv('MOYSKLAD_TOKEN')
    if not token:
        print("   ⚠️ Нет токена МС — пропускаю фото")
        return cached
    headers = {"Authorization": f"Bearer {token}", "Accept-Encoding": "gzip"}

    todo = [(art, name) for art, name in article_to_name.items() if art not in cached]
    print(f"   Скачать новых фото: {len(todo)} (уже в кеше: {len(cached)})")
    new_count = 0; skip_count = 0
    for i, (art, name) in enumerate(todo, 1):
        try:
            # Поиск товара (короткий timeout)
            r = requests.get(f"{base_url}/entity/product", headers=headers,
                             params={'filter': f'article={art}', 'limit': 1}, timeout=10)
            if r.status_code != 200:
                cached[art] = ''
                continue
            rows = r.json().get('rows', [])
            if not rows:
                cached[art] = ''
                continue
            img_meta = rows[0].get('images', {}).get('meta', {})
            if not img_meta.get('href'):
                cached[art] = ''
                continue
            r2 = requests.get(img_meta['href'], headers=headers, timeout=10)
            if r2.status_code != 200:
                cached[art] = ''
                continue
            imgs = r2.json().get('rows', [])
            if not imgs:
                cached[art] = ''
                continue
            mini = imgs[0].get('miniature', {}).get('href') or imgs[0].get('tiny', {}).get('href')
            if not mini:
                cached[art] = ''
                continue
            r3 = requests.get(mini, headers=headers, timeout=10)
            if r3.status_code != 200:
                cached[art] = ''
                skip_count += 1
                continue
            cached[art] = base64.b64encode(r3.content).decode('ascii')
            new_count += 1
            if (new_count + skip_count) % 20 == 0:
                print(f"      {i}/{len(todo)}: {new_count} скачано, {skip_count} пропущено", flush=True)
                save_photo_cache(cached)
        except Exception:
            cached[art] = ''
            skip_count += 1
    save_photo_cache(cached)
    print(f"   Скачано: {new_count}, пропущено (timeout/нет фото): {skip_count}")
    return cached


def main():
    today = date.today()
    d30 = (today - timedelta(days=30)).isoformat()
    print(f"Дата: {today}, окно скорости: {d30} → {today}")

    con = duckdb.connect(str(DB_PATH), read_only=True)
    snap_date = SNAPSHOT_DATE or latest_table_date(con, 'inventory_snapshot_stores_')
    prices_date = PRICES_DATE or latest_table_date(con, 'prices_snapshot_')
    snap = f'inventory_snapshot_stores_{snap_date}'
    prices = f'prices_snapshot_{prices_date}'
    print(f"Снапшот: {snap} | Цены: {prices}")
    reorder_arts = fetch_reorder_articles()
    print(f"В свежем заказе (не уценять): {len(reorder_arts)} артикулов")
    season_tags = get_model_tags()

    print("\n1. Запрос кроссовок (200000-209999) с остатком...")
    rows = con.execute(f"""
        WITH stock AS (
          SELECT article, ANY_VALUE(product_name) AS pname, SUM(total_stock) AS total,
                 SUM(moscow) AS msk, SUM(tsum) AS tsum, SUM(online) AS online,
                 SUM(astana_aruzhan) AS aruz, SUM(main_warehouse) AS wh
          FROM {snap}
          WHERE TRY_CAST(article AS INTEGER) BETWEEN 200000 AND 209999 AND total_stock > 0
          GROUP BY article
        ),
        prices AS (SELECT article, MAX(buy_price) c, MAX(sale_price) r, MAX(new_price) np FROM {prices} GROUP BY article),
        -- Продажи через retaildemand_positions (правильный источник — позиции каждого чека).
        -- Старая sales таблица (через /report/profit/byvariant) имеет дыры за янв-апр 2024
        -- и недостоверна по конкретным артикулам.
        s30 AS (SELECT article, SUM(quantity) q FROM retaildemand_positions WHERE document_moment >= ?::DATE AND price > 0 GROUP BY article),
        s90 AS (SELECT article, SUM(quantity) q FROM retaildemand_positions WHERE document_moment >= ?::DATE AND price > 0 GROUP BY article),
        s180 AS (SELECT article, SUM(quantity) q FROM retaildemand_positions WHERE document_moment >= ?::DATE AND price > 0 GROUP BY article),
        s365 AS (SELECT article, SUM(quantity) q FROM retaildemand_positions WHERE document_moment >= ?::DATE AND price > 0 GROUP BY article),
        sall AS (SELECT article, SUM(quantity) q FROM retaildemand_positions WHERE price > 0 GROUP BY article),
        -- Осень прошлого года (сен-ноя 2025): сезонные модели, которые нельзя уценять летом
        autumn AS (SELECT article, SUM(quantity) q FROM retaildemand_positions
                   WHERE document_moment >= DATE '2025-09-01' AND document_moment < DATE '2025-12-01'
                     AND price > 0 GROUP BY article),
        last_sale AS (SELECT article, MAX(DATE(document_moment)) d FROM retaildemand_positions WHERE price > 0 GROUP BY article),
        last_supply AS (SELECT product_article AS article, MAX(DATE(supply_moment)) d FROM supply_positions WHERE product_article IS NOT NULL GROUP BY product_article),
        first_supply AS (SELECT product_article AS article, MIN(DATE(supply_moment)) d, SUM(quantity) qty
                         FROM supply_positions WHERE product_article IS NOT NULL GROUP BY product_article),
        sup_cost AS (SELECT product_article AS article, ROUND(AVG(price)) c FROM supply_positions WHERE price > 0 GROUP BY product_article)
        SELECT s.article, s.pname, s.total, s.msk, s.tsum, s.online, s.aruz, s.wh,
               -- NULLIF: у товаров In buy_price в prices = 0 (не NULL), без NULLIF
               -- COALESCE брал бы 0 вместо цены из поставок → себес занижался на ~27М.
               COALESCE(NULLIF(p.c, 0), sc.c, 0) AS cost,
               COALESCE(p.r, 0) AS retail,
               COALESCE(p.np, 0) AS new_price,
               COALESCE(s30.q, 0) AS s30, COALESCE(s90.q, 0) AS s90,
               COALESCE(s180.q, 0) AS s180, COALESCE(s365.q, 0) AS s365, COALESCE(sall.q, 0) AS sall,
               last_sale.d AS last_sale_d, last_supply.d AS last_supply_d,
               first_supply.d AS first_supply_d, first_supply.qty AS first_supply_qty,
               COALESCE(autumn.q, 0) AS autumn_q
        FROM stock s
        LEFT JOIN prices p USING(article)
        LEFT JOIN autumn USING(article)
        LEFT JOIN sup_cost sc USING(article)
        LEFT JOIN s30 USING(article)
        LEFT JOIN s90 USING(article)
        LEFT JOIN s180 USING(article)
        LEFT JOIN s365 USING(article)
        LEFT JOIN sall USING(article)
        LEFT JOIN last_sale USING(article)
        LEFT JOIN last_supply USING(article)
        LEFT JOIN first_supply USING(article)
    """, [d30,
          (today - timedelta(days=90)).isoformat(),
          (today - timedelta(days=180)).isoformat(),
          (today - timedelta(days=365)).isoformat()]).fetchall()
    print(f"   Найдено моделей: {len(rows)}")

    # Размеры — отдельный запрос (variants) — берём из снапшота через product_name
    print("\n2. Размеры по каждой модели...")
    articles = [r[0] for r in rows]
    if articles:
        ph = ','.join(['?'] * len(articles))
        size_rows = con.execute(f"""
            SELECT article, product_name, SUM(total_stock) AS qty
            FROM {snap}
            WHERE article IN ({ph}) AND total_stock > 0
            GROUP BY article, product_name
        """, articles).fetchall()
        sizes_by_article = {}
        for art, pname, qty in size_rows:
            # размер — последняя цифра/часть после ", "
            m = re.search(r',\s*(\d+(?:\.\d+)?)\s*$', pname or '')
            if m:
                size = m.group(1)
                sizes_by_article.setdefault(art, {})
                sizes_by_article[art][size] = sizes_by_article[art].get(size, 0) + int(qty)

        # Проданные размеры за 90д (для развилки «Остывают»: какой ходовой размер
        # продавался, но выбит из наличия → дозаказ, а не скидка). Размер из имени.
        sold_size_rows = con.execute(f"""
            SELECT article, product_name, SUM(quantity) AS q
            FROM retaildemand_positions
            WHERE article IN ({ph}) AND price > 0 AND document_moment >= DATE '2026-03-07'
            GROUP BY article, product_name
        """, articles).fetchall()
        sold_sizes_by_article = {}
        for art, pname, qty in sold_size_rows:
            m = re.search(r',\s*(\d+(?:\.\d+)?)\s*$', pname or '')
            if m and int(qty or 0) > 0:
                size = m.group(1).split('.')[0]
                sold_sizes_by_article.setdefault(art, set()).add(size)

    # Snapshot ~6 месяцев назад (180 дней) — ищем ближайшую существующую таблицу
    target_180 = (today - timedelta(days=180)).strftime('%Y%m%d')
    snap_tables = con.execute("""
        SELECT table_name FROM information_schema.tables
        WHERE table_name LIKE 'inventory_snapshot_stores_%'
        ORDER BY table_name
    """).fetchall()
    snap_dates = [(t[0][-8:], t[0]) for t in snap_tables]
    # Ближайший к target_180 (но не позже сегодня)
    snap_6mo = min(snap_dates, key=lambda x: abs(int(x[0]) - int(target_180)))
    print(f"\n3a. Снапшот ~6 мес назад: {snap_6mo[1]} (цель: {target_180})")
    stock_6mo = {}
    if articles:
        ph = ','.join(['?'] * len(articles))
        r6mo = con.execute(f"""
            SELECT article, SUM(total_stock) FROM {snap_6mo[1]}
            WHERE article IN ({ph}) GROUP BY article
        """, articles).fetchall()
        stock_6mo = {a: int(t or 0) for a, t in r6mo}
    snap_6mo_iso = f"{snap_6mo[0][:4]}-{snap_6mo[0][4:6]}-{snap_6mo[0][6:]}"

    print("\n3b. Сборка items...")
    items = []
    for r in rows:
        (art, pname, total, msk, tsum, online, aruz, wh,
         cost, retail, new_price, s30, s90, s180, s365, sall,
         last_sale_d, last_supply_d, first_supply_d, first_supply_qty, autumn_q) = r
        cost = float(cost or 0); retail = float(retail or 0)
        new_price = float(new_price or 0)
        # orig = изначальная "Цена продажи" (зачёркнутая на стикере)
        # cur  = ТЕКУЩАЯ цена продажи (со скидкой если есть) — БАЗА для новой скидки
        orig_price = retail
        cur_price = new_price if (orig_price > 0 and 0 < new_price < orig_price) else orig_price
        cur_disc = round(100 * (1 - cur_price / orig_price)) if orig_price > 0 and cur_price < orig_price else 0
        retail = cur_price  # дальше retail = текущая цена (от неё считаем новую скидку)
        s30 = int(s30 or 0); s90 = int(s90 or 0)
        s180 = int(s180 or 0); s365 = int(s365 or 0); sall = int(sall or 0)
        total = int(total or 0)
        msk, tsum, online, aruz, wh = [int(x or 0) for x in (msk, tsum, online, aruz, wh)]
        first_supply_qty = int(first_supply_qty or 0)
        stock_at_6mo = stock_6mo.get(art, 0)

        days_no_sale = (today - last_sale_d).days if last_sale_d else 999
        days_since_supply = (today - last_supply_d).days if last_supply_d else 999
        days_in_stock = (today - first_supply_d).days if first_supply_d else 999

        # Скорость и WOS
        daily = s30 / 30
        wos = (total / daily / 7) if daily > 0 else 999
        # Sell-through за всё время с поставки — сложно (нет supplied_qty без полного агрегата) — упростим:
        # используем 90д продажи / (текущий остаток + продажи 90д)
        sold_90 = s90
        st = (sold_90 / (total + sold_90) * 100) if (total + sold_90) > 0 else 0

        # Категоризация
        item = {
            'article': str(art) if art else '',
            'name': pname or '',
            'brand': detect_brand(pname),
            'stock': {'total': total, 'moscow': msk,
                      'tsum_online': tsum + online, 'aruzhan': aruz, 'warehouse': wh},
            'sizes': sizes_by_article.get(art, {}),
            'sizes_sold': sorted(sold_sizes_by_article.get(art, set())),
            'cost': cost, 'retail': retail,
            'orig_price': orig_price,
            'cur_disc': cur_disc,
            'margin_pct': round((retail - cost) / retail * 100, 1) if retail > 0 else 0,
            'sales': {'s30': s30, 's90': s90, 's180': s180, 's365': s365, 'sall': sall,
                      'rev_30d': s30 * retail,
                      'rev_90d': s90 * retail},
            'history': {
                'first_supply_date': first_supply_d.isoformat() if first_supply_d else None,
                'first_supply_qty': first_supply_qty,
                'snap_6mo_date': snap_6mo_iso,
                'stock_6mo': stock_at_6mo,
            },
            'last_sale': last_sale_d.isoformat() if last_sale_d else None,
            'days_no_sale': days_no_sale if days_no_sale < 999 else None,
            'days_since_supply': days_since_supply if days_since_supply < 999 else None,
            'days_in_stock': days_in_stock if days_in_stock < 999 else None,
            'sell_through': round(st, 1),
            'wos': round(wos, 1) if wos < 999 else 999,
            'velocity': {'recent_30d': s30, 'avg_30d': round(s90/3, 1) if s90 else 0,
                        'acceleration': round(s30 / (s90/3), 2) if s90 > 0 else 0},
            'frozen_cost': int(total * cost),
            'frozen_retail': int(total * retail),
        }
        # categorize нужны calc'd поля
        item['days_no_sale'] = days_no_sale
        item['days_since_supply'] = days_since_supply
        item['autumn_q'] = int(autumn_q or 0)
        item['in_reorder'] = item['article'] in reorder_arts
        item['season_tag'] = season_tags.get(item['article'], '')
        cat, reason = categorize(item)
        item['category'] = cat
        item['reason'] = reason
        items.append(item)

    # Подмешиваем suggested_discount из стратегического аналитического слоя (если есть)
    # Это даёт пользователю стартовое значение скидки которое он может скорректировать
    layer_csv = PROJECT_ROOT / 'reports' / 'strategic' / f'assortment_layer_{snap_date}.csv'
    suggested_by_article = {}
    health_by_article = {}
    if layer_csv.exists():
        import csv
        with open(layer_csv) as f:
            for row in csv.DictReader(f):
                art = (row.get('article') or '').strip()
                if not art: continue
                # article в CSV может быть float (типа "200001.0")
                art_clean = art.split('.')[0] if '.' in art else art
                try:
                    sd = int(float(row.get('suggested_discount') or 0))
                except (ValueError, TypeError):
                    sd = 0
                suggested_by_article[art_clean] = sd
                health_by_article[art_clean] = row.get('health', '')
        print(f"   Подмешали suggested_discount из {layer_csv.name}: {len(suggested_by_article)} артикулов")

    for it in items:
        art = it.get('article', '')
        it['suggested_discount'] = suggested_by_article.get(art, 0)
        it['health_v2'] = health_by_article.get(art, '')
        # Категория решает рекомендацию (иначе противоречие: «ДОЗАКАЗ» + кнопка скидки):
        cat = it.get('category', '')
        if cat == 'COOLING_REORDER':
            # выбит ходовой размер → НЕ скидка, а дозаказ. Убираем рекомендацию скидки.
            it['suggested_discount'] = 0
            it['reorder_hint'] = it.get('reason', '')
        elif cat == 'COOLING_SALE':
            # мягкая скидка из reason («−N%»)
            m = re.search(r'−(\d+)%', it.get('reason', ''))
            if m:
                it['suggested_discount'] = int(m.group(1))
        elif cat in ('REORDERED', 'HOLD_AUTUMN'):
            # дозаказан / ждёт осени → скидку не предлагать
            it['suggested_discount'] = 0
        elif cat == 'SUMMER_CLEAR':
            # летний уходящий — агрессивнее обычного
            it['suggested_discount'] = max(it.get('suggested_discount', 0) or 0, 30)
        elif cat == 'SLIDES':
            # решение Ерлана: все сланцы −20% заранее
            it['suggested_discount'] = 20

    # Сортируем: убыточные → мёртвые → медленные → новые → хиты → норма → намеренные
    cat_order = {'SLIDES': 0, 'UNPROFITABLE': 1, 'SUMMER_CLEAR': 2, 'DEAD': 3,
                 'COOLING_REORDER': 4, 'COOLING_SALE': 5, 'SLOW': 6, 'HOLD_AUTUMN': 7,
                 'REORDERED': 8, 'NEW': 9, 'HOT': 10, 'NORMAL': 11, 'INTENTIONAL': 12}
    items.sort(key=lambda x: (cat_order.get(x['category'], 99), -x['frozen_cost']))

    print("\n4. Фото...")
    cache = load_photo_cache()
    skip_download = '--skip-photos' in sys.argv or os.environ.get('SKIP_PHOTOS')
    article_to_name = {it['article']: it['name'] for it in items if it['article']}
    if not skip_download:
        # Пустые записи кеша ('') = прошлые неудачи (timeout/404) — ПРОБУЕМ СНОВА.
        # Раньше они помечались пустыми навсегда → 172 модели остались без фото.
        retry = [a for a in article_to_name if cache.get(a, None) == '']
        if retry:
            print(f"   Повторная попытка для {len(retry)} пустых записей кеша")
            for a in retry:
                del cache[a]
        cache = fetch_photos_from_ms(article_to_name, cache)
    for it in items:
        it['photo'] = cache.get(it['article'], '')
    has_photo = sum(1 for it in items if it['photo'])
    print(f"   С фото: {has_photo}/{len(items)}")

    # Сводка
    cat_count = {}
    cat_frozen = {}
    for it in items:
        c = it['category']
        cat_count[c] = cat_count.get(c, 0) + 1
        cat_frozen[c] = cat_frozen.get(c, 0) + it['frozen_cost']

    print("\n=== СВОДКА ===")
    cat_names = {'SLIDES': '🩴 Сланцы/Birkenstock — сейл −20% (решение 07.07)',
                 'UNPROFITABLE': '⚠️ Убыточные (РЦ ≤ себес)',
                 'SUMMER_CLEAR': '☀️ Летние — сейлить СЕЙЧАС (сезон уходит)',
                 'DEAD': '🔴 Мёртвые (90+ дн без продаж)',
                 'COOLING_REORDER': '🔵 Остывают → ДОЗАКАЗ (выбит ходовой размер)',
                 'COOLING_SALE': '🟡 Остывают → СКИДКА (сетка полная, спрос упал)',
                 'SLOW': '🟠 Медленные (60+ дн, мало продаж)',
                 'HOLD_AUTUMN': '🍂 Ждут осени (сезонные — НЕ уценять)',
                 'REORDERED': '📦 В заказе ЗК (дозаказаны — НЕ уценять)',
                 'NEW': '⚪ Новые (рано судить)',
                 'HOT': '🟢 Хиты (быстро продаются)',
                 'NORMAL': '🔵 Норма',
                 'INTENTIONAL': '⚫ Намеренный неликвид (NB/Asics/Puma)'}
    for c in ['SLIDES', 'UNPROFITABLE', 'SUMMER_CLEAR', 'DEAD', 'COOLING_REORDER', 'COOLING_SALE', 'SLOW', 'HOLD_AUTUMN', 'REORDERED', 'NEW', 'HOT', 'NORMAL', 'INTENTIONAL']:
        if c in cat_count:
            print(f"  {cat_names[c]:<48} {cat_count[c]:>4} моделей  {cat_frozen[c]:>14,.0f} ₸")

    # Сохраняем JSON для верификации (без photo)
    clean = copy.deepcopy(items)
    for r in clean:
        r['photo'] = f'[{len(r["photo"])}b]' if r['photo'] else '(нет)'
    OUT_JSON.write_text(json.dumps(clean, ensure_ascii=False, indent=2))
    print(f"\n✓ JSON: {OUT_JSON}")

    return items


def new_render_function() -> str:
    """Возвращает JS-код renderItem функции для кроссовок."""
    cat_labels = json.dumps({k: v['label'] for k, v in CAT_CONFIG.items()}, ensure_ascii=False)
    return r'''
function renderItem(item, idx) {
  const itemKey = item.article || item.name;
  const disc = discounts[itemKey] || 0;
  const newPrice = disc > 0 ? Math.round(item.retail * (1 - disc/100)) : 0;
  const stBadge = stClass(item.sell_through);
  const CAT_LABELS = ''' + cat_labels + r''';

  const photoHtml = item.photo
    ? `<img class="item-photo" src="data:image/jpeg;base64,${item.photo}" alt="${item.name}" onclick="openLightbox('${itemKey.replace(/'/g,"")}')">`
    : `<div class="item-photo-empty"><div class="art">${(item.article||'—').substring(0,8)}</div><div class="nf">нет фото</div></div>`;

  const catLabel = CAT_LABELS[item.category] || item.category;
  const curDiscBadge = (item.cur_disc > 0) ? `<span class="curdisc-badge">в МС уже −${item.cur_disc}%</span>` : '';
  const catHtml = `<div><span class="cat-badge cat-${item.category}">${catLabel}</span>${curDiscBadge}
    <div class="cat-reason">${item.reason || ''}</div></div>`;

  let unprofitWarn = '';
  if (item.category === 'UNPROFITABLE' && item.cost > item.retail) {
    const loss = Math.round(item.cost - item.retail);
    unprofitWarn = `<div class="unprofit-warn">⚠️ Каждая продажа = убыток ${fmt(loss)}₸/пара. Поднять цену хотя бы до ${fmt(Math.round(item.cost * 1.3))}₸</div>`;
  }

  const s30 = (item.sales && item.sales.s30) || 0;
  const s90 = (item.sales && item.sales.s90) || 0;
  const s180 = (item.sales && item.sales.s180) || 0;
  const s365 = (item.sales && item.sales.s365) || 0;
  const sall = (item.sales && item.sales.sall) || 0;
  const wos = item.wos || 999;
  const wosClass = wos < 4 ? 'wos-bad' : wos < 12 ? 'wos-warn' : wos >= 99 ? '' : 'wos-ok';
  const wosLabel = wos >= 99 ? 'не продаётся' : wos < 4 ? `${wos.toFixed(0)} нед (мало)` : wos < 12 ? `${wos.toFixed(0)} нед` : `${wos.toFixed(0)} нед (много)`;
  const velHtml = `<div class="velocity-box">
    <span>⚡ Продано:</span>
    <span><b>${s30}</b> <span style="color:var(--text3);font-size:11px">30д</span></span>
    <span><b>${s90}</b> <span style="color:var(--text3);font-size:11px">90д</span></span>
    <span><b>${s180}</b> <span style="color:var(--text3);font-size:11px">180д</span></span>
    <span><b>${s365}</b> <span style="color:var(--text3);font-size:11px">365д</span></span>
    <span><b>${sall}</b> <span style="color:var(--text3);font-size:11px">всё</span></span>
    <span style="margin-left:auto" class="${wosClass}">📦 ${wosLabel}</span>
  </div>`;

  // История прихода: первая поставка → 6 мес назад → сейчас
  const h = item.history || {};
  let historyHtml = '';
  if (h.first_supply_date || h.stock_6mo > 0) {
    const parts = [];
    if (h.first_supply_date) {
      const fd = h.first_supply_date.slice(0, 7);  // YYYY-MM
      parts.push(`<span>📅 <b>${fd}</b>: поставка ${h.first_supply_qty || '?'} пар</span>`);
    }
    if (h.snap_6mo_date) {
      const snapDate = h.snap_6mo_date.slice(0, 7);
      parts.push(`<span style="color:var(--text3)">→ ${snapDate}: <b>${h.stock_6mo}</b></span>`);
    }
    parts.push(`<span style="color:var(--blue)">→ сейчас: <b>${item.stock.total}</b></span>`);
    historyHtml = `<div class="velocity-box" style="background:#f1f5f9">${parts.join(' ')}</div>`;
  }

  const flowHtml = `<div class="flow-box">
    <div class="flow-cell">
      <div class="flow-label">Остаток</div>
      <div class="flow-val" style="color:#3b82f6">${item.stock.total}</div>
      <div class="flow-sub">${item.frozen_cost > 0 ? fmtK(item.frozen_cost) + '₸ себес' : ''}</div>
    </div>
    <div class="flow-cell">
      <div class="flow-label">Продано 90д</div>
      <div class="flow-val" style="color:#10b981">${s90}</div>
      <div class="flow-sub">из них 30д: ${s30}</div>
    </div>
    <div class="flow-cell">
      <div class="flow-label">Посл. продажа</div>
      <div class="flow-val" style="font-size:14px">${item.last_sale ? item.last_sale.slice(5) : '—'}</div>
      <div class="flow-sub">${item.days_no_sale != null ? item.days_no_sale + ' дн назад' : 'не было'}</div>
    </div>
  </div>`;

  return `
  <div class="item" data-article="${itemKey}">
    <div class="item-main">
      ${photoHtml}
      <div class="item-body">
        <div class="item-name">${item.name}</div>
        <div class="item-article"><span class="brand-badge">${item.brand}</span><span style="color:var(--blue);cursor:pointer" onclick="navigator.clipboard.writeText('${itemKey}').then(()=>toast('${itemKey} скопирован'))">${item.article} 📋</span></div>
      </div>
      <div class="item-right">
        <span class="st-badge ${stBadge}">${item.sell_through > 0 ? item.sell_through.toFixed(0) + '%' : '—'}</span>
        <div class="stock-total">${item.stock.total}</div>
      </div>
    </div>
    <div class="item-details">
      ${catHtml}
      ${unprofitWarn}
      ${flowHtml}
      ${historyHtml}
      ${velHtml}
      <div class="stock-grid">
        <div class="stock-cell"><div class="stock-cell-label">Мск</div><div class="stock-cell-val ${item.stock.moscow === 0 ? 'zero' : ''}">${item.stock.moscow}</div></div>
        <div class="stock-cell"><div class="stock-cell-label">ЦУМ+Онл</div><div class="stock-cell-val ${item.stock.tsum_online === 0 ? 'zero' : ''}">${item.stock.tsum_online}</div></div>
        <div class="stock-cell"><div class="stock-cell-label">Аружан</div><div class="stock-cell-val ${item.stock.aruzhan === 0 ? 'zero' : ''}">${item.stock.aruzhan}</div></div>
        <div class="stock-cell"><div class="stock-cell-label">Склад</div><div class="stock-cell-val ${item.stock.warehouse === 0 ? 'zero' : ''}">${item.stock.warehouse}</div></div>
      </div>
      ${renderSizes(item)}
      <div class="price-row">
        <div class="price-item"><span class="label">Себестоимость</span> <span class="val">${item.cost > 0 ? fmt(Math.round(item.cost)) + '₸' : '—'}</span></div>
        ${item.cur_disc > 0
          ? `<div class="price-item"><span class="label">Изначальная</span> <span class="val" style="text-decoration:line-through;color:var(--text3)">${fmt(Math.round(item.orig_price))}₸</span></div>
             <div class="price-item"><span class="label">Сейчас (−${item.cur_disc}%)</span> <span class="val" style="color:#f59e0b">${fmt(Math.round(item.retail))}₸</span></div>`
          : `<div class="price-item"><span class="label">Цена в магазине</span> <span class="val">${item.retail > 0 ? fmt(Math.round(item.retail)) + '₸' : '—'}</span></div>`}
        ${item.margin_pct > 0 ? `<div class="price-item"><span class="label">Маржа</span> <span class="val">${item.margin_pct}%</span></div>` : ''}
        ${item.frozen_retail > 0 ? `<div class="price-item"><span class="label">Заморожено в РЦ</span> <span class="val">${fmtK(item.frozen_retail)}₸</span></div>` : ''}
      </div>
      <div class="discount-row" style="flex-wrap:wrap">
        <label>Скидка от текущей</label>
        <div class="disc-btns">
          ${[0,5,10,15,20,25,30,40,50].map(d => `<button type="button" class="disc-btn ${disc==d && d>0?'active':''}" onclick="onDiscount('${itemKey.replace(/'/g,"").replace(/"/g,"")}',${d})">${d?d+'%':'✕'}</button>`).join('')}
        </div>
        ${newPrice > 0
          ? `<span class="new-price">${fmt(newPrice)}₸</span>`
          : `<span class="new-price" style="color:var(--text3)">${item.retail > 0 ? fmt(Math.round(item.retail)) + '₸' : '—'}</span>`}
      </div>
      <div class="discount-row season-row" style="margin-top:6px;background:#f8fafc">
        <label>Сезон</label>
        <div class="disc-btns">
          ${SEASON_TAGS.map(([v,l]) => `<button type="button" class="disc-btn season-btn ${(seasonTags[itemKey]||item.season_tag||'')===v?'active':''}" onclick="setSeasonTag('${itemKey.replace(/'/g,"").replace(/"/g,"")}','${v}')">${l}</button>`).join('')}
        </div>
      </div>
      ${item.category === 'COOLING_REORDER' ? `
      <div class="discount-row" style="background:#f0f9ff;border:1px dashed #0ea5e9;margin-top:6px">
        <label style="color:#075985">🔵 ДОЗАКАЗ</label>
        <span style="font-size:12px;color:#075985">${item.reorder_hint || 'Выбит ходовой размер — дозаказать, НЕ сейлить'}</span>
      </div>` :
      (item.suggested_discount > 0 && disc === 0) ? `
      <div class="discount-row" style="background:#fef3c7;border:1px dashed #d97706;margin-top:6px">
        <label style="color:#92400e">💡 Реком</label>
        <button type="button" class="disc-btn active" style="background:#d97706;color:#fff;border-color:#d97706" onclick="onDiscount('${itemKey.replace(/'/g,"").replace(/"/g,"")}',${item.suggested_discount})">Применить ${item.suggested_discount}%</button>
        <span style="font-size:11px;color:#92400e">→ ${fmt(Math.round(item.retail * (1 - item.suggested_discount/100)))}₸ (${item.health_v2 || ''})</span>
      </div>` : ''}
    </div>
  </div>`;
}
'''


CAT_CONFIG = {
    'SLIDES':       {'label': '🩴 СЛАНЦЫ −20%', 'color': '#0891b2', 'bg': '#ecfeff'},
    'UNPROFITABLE': {'label': '⚠️ УБЫТОЧНЫЕ', 'color': '#dc2626', 'bg': '#fef2f2'},
    'SUMMER_CLEAR': {'label': '☀️ ЛЕТНИЕ — СЕЙЛИТЬ', 'color': '#ea580c', 'bg': '#fff7ed'},
    'DEAD':         {'label': '🔴 МЁРТВЫЕ',    'color': '#ef4444', 'bg': '#fef2f2'},
    'COOLING_REORDER': {'label': '🔵 ОСТЫВАЮТ → ДОЗАКАЗ', 'color': '#0ea5e9', 'bg': '#f0f9ff'},
    'COOLING_SALE':    {'label': '🟡 ОСТЫВАЮТ → СКИДКА',  'color': '#eab308', 'bg': '#fefce8'},
    'SLOW':         {'label': '🟠 МЕДЛЕННЫЕ',  'color': '#f59e0b', 'bg': '#fffbeb'},
    'HOLD_AUTUMN':  {'label': '🍂 ЖДЁТ ОСЕНИ', 'color': '#92400e', 'bg': '#fef3c7'},
    'REORDERED':    {'label': '📦 В ЗАКАЗЕ ЗК', 'color': '#7c3aed', 'bg': '#f5f3ff'},
    'NEW':          {'label': '⚪ НОВЫЕ',       'color': '#6b7280', 'bg': '#f3f4f6'},
    'HOT':          {'label': '🟢 ХИТЫ',        'color': '#10b981', 'bg': '#ecfdf5'},
    'NORMAL':       {'label': '🔵 НОРМА',       'color': '#3b82f6', 'bg': '#eff6ff'},
    'INTENTIONAL':  {'label': '⚫ NB/Asics/Puma','color': '#525252', 'bg': '#f5f5f4'},
}


def get_model_tags() -> dict:
    """Теги сезонности из Supabase model_tags → {article: season}.
    Compound-система: Алуа/Ерлан проставляют теги в дашборде, они копятся."""
    try:
        import requests
        from dotenv import load_dotenv
        load_dotenv(PROJECT_ROOT / '.env')
        url = os.getenv('SUPABASE_URL'); key = os.getenv('SUPABASE_KEY')
        if not url or not key:
            return {}
        r = requests.get(f"{url}/rest/v1/model_tags?select=article,season&limit=10000",
                         headers={'apikey': key, 'Authorization': f'Bearer {key}'}, timeout=15)
        tags = {str(t['article']): t['season'] for t in (r.json() if r.ok else [])}
        print(f"   Тегов сезонности из Supabase: {len(tags)}")
        return tags
    except Exception as e:
        print(f"   ⚠️ Теги не загружены: {e}")
        return {}


def get_preset_discounts() -> dict:
    """Тянет сохранённые скидки из Supabase (order SNEAKERS-001) → {article: discount}.
    Нужно чтобы восстановить уже выставленные скидки в дашборде, даже если
    localStorage браузера пуст."""
    try:
        import requests
        from dotenv import load_dotenv
        load_dotenv(PROJECT_ROOT / '.env')
        url = os.getenv('SUPABASE_URL'); key = os.getenv('SUPABASE_KEY')
        if not url or not key:
            return {}
        r = requests.get(f"{url}/rest/v1/orders?id=eq.SNEAKERS-001&select=items",
                         headers={'apikey': key, 'Authorization': f'Bearer {key}'}, timeout=15)
        rows = r.json()
        if not rows or not rows[0].get('items'):
            return {}
        preset = {}
        for it in rows[0]['items']:
            a = str(it.get('article', ''))
            d = int(it.get('discount', 0) or 0)
            if a and d > 0:
                preset[a] = d
        print(f"   Preset скидок из Supabase: {len(preset)}")
        return preset
    except Exception as e:
        print(f"   ⚠️ Не удалось получить preset: {e}")
        return {}


def render_html(items: list):
    """Собирает sneakers.html на основе clothing.html template (надёжно через маркеры)."""
    if not TEMPLATE_HTML.exists():
        print(f"⚠️ Нет {TEMPLATE_HTML}")
        return

    # Адаптируем формат items под шаблон одежды:
    # одежда: item.variants = {colors:{}, sizes:{}}
    # кроссовки: item.sizes напрямую → положу в variants
    adapted_items = []
    for it in items:
        adapted = {**it,
                   'variants': {'colors': {}, 'sizes': it.get('sizes', {}), 'matrix': {}},
                   'subfolder': it.get('brand', 'Прочее')}  # filter по бренду через subfolder
        adapted_items.append(adapted)

    new_html = TEMPLATE_HTML.read_text()

    # Восстановление ранее выставленных скидок: вшиваем preset из Supabase.
    # При загрузке подставляем для артикулов, которых нет в localStorage.
    preset = get_preset_discounts()
    preset_js = json.dumps(preset, ensure_ascii=False)
    new_html = new_html.replace(
        """// Load saved discounts from localStorage
try {
  const saved = localStorage.getItem('clothing_discounts');
  if (saved) discounts = JSON.parse(saved);
} catch(e) {}""",
        """// Load saved discounts from localStorage
try {
  const saved = localStorage.getItem('clothing_discounts');
  if (saved) discounts = JSON.parse(saved);
} catch(e) {}
// Восстановление ранее выставленных скидок (из Supabase, вшито при сборке)
const PRESET_DISCOUNTS = """ + preset_js + """;
let _restored = 0;
for (const a in PRESET_DISCOUNTS) {
  if (!discounts[a]) { discounts[a] = PRESET_DISCOUNTS[a]; _restored++; }
}
if (_restored > 0) {
  try { localStorage.setItem('clothing_discounts', JSON.stringify(discounts)); } catch(e) {}
}""")

    # Подсчёты для шапки
    total_models = len(items)
    total_stock = sum(it['stock']['total'] for it in items)
    total_cost = sum(it['frozen_cost'] for it in items)
    total_retail = sum(it['frozen_retail'] for it in items)
    total_s30 = sum(it['sales']['s30'] for it in items)

    def mk(n):
        if n >= 1_000_000: return f'{n/1_000_000:.1f}М'
        if n >= 1000: return f'{round(n/1000)}K'
        return str(round(n))

    # Title — гибко
    new_html = re.sub(r'<title>[^<]+</title>',
                      '<title>Кроссовки — Dashboard</title>', new_html)
    # H1 + badge
    new_html = re.sub(r'<h1>[^<]+</h1>\s*<span class="badge badge-info">[^<]+</span>',
                      f'<h1>Кроссовки</h1>\n    <span class="badge badge-info">{total_models} моделей</span>',
                      new_html)
    # Header meta
    today_s = date.today().strftime('%d.%m.%Y')
    new_html = re.sub(r'<div class="header-meta">Остатки:[^<]*</div>',
                      f'<div class="header-meta">Остатки: {today_s} | Все 200000-209999</div>',
                      new_html)
    # Stats
    cat_count = {}
    for it in items: cat_count[it['category']] = cat_count.get(it['category'], 0) + 1
    pct = lambda c: cat_count.get(c, 0)   # ЧИСЛО (не строка — иначе склейка 56+27='5627')
    cooling_total = pct("COOLING_REORDER") + pct("COOLING_SALE")

    stats_replacements = [
        (r'<div class="stat-num" id="stat-items">\d+</div>',
         f'<div class="stat-num" id="stat-items">{total_models}</div>'),
        (r'<div class="stat-num" id="stat-supplied"[^>]*>[^<]+</div>',
         f'<div class="stat-num" id="stat-supplied" style="color:#dc2626">{pct("UNPROFITABLE")+pct("DEAD")}</div>'),
        (r'<div class="stat-num" id="stat-supply-cost"[^>]*>[^<]+</div>',
         f'<div class="stat-num" id="stat-supply-cost" style="color:#f59e0b">{pct("SLOW")}</div>'),
        (r'<div class="stat-num blue" id="stat-stock">\d+</div>',
         f'<div class="stat-num blue" id="stat-stock">{total_stock}</div>'),
        (r'<div class="stat-num" id="stat-cost">[^<]+</div>',
         f'<div class="stat-num" id="stat-cost">{mk(total_cost)}</div>'),
        (r'<div class="stat-num green" id="stat-sold">\d+</div>',
         f'<div class="stat-num green" id="stat-sold">{total_s30}</div>'),
        (r'<div class="stat-num green" id="stat-revenue">[^<]+</div>',
         f'<div class="stat-num green" id="stat-revenue">{mk(total_s30 * (total_retail/total_stock if total_stock else 0))}</div>'),
        (r'<div class="stat-num purple" id="stat-retail">[^<]+</div>',
         f'<div class="stat-num purple" id="stat-retail">{mk(total_retail)}</div>'),
    ]
    for pattern, repl in stats_replacements:
        new_html = re.sub(pattern, repl, new_html)

    # Stat-labels — поменяем 2 на смысл
    new_html = new_html.replace('<div class="stat-label">Завезли шт</div>',
                                '<div class="stat-label">Убыт+Мёртв</div>', 1)
    new_html = new_html.replace('<div class="stat-label">Завоз себес</div>',
                                '<div class="stat-label">Медленные</div>', 1)
    new_html = new_html.replace('<div class="stat-label">Продано</div>',
                                '<div class="stat-label">Продано 30д</div>', 1)
    new_html = new_html.replace('<div class="stat-label">На полке РЦ</div>',
                                '<div class="stat-label">Стоимость в РЦ</div>', 1)

    # ALL_ITEMS — разделяем на 2 файла: данные (быстрый) + фото (медленный)
    items_lite = []
    photos_only = {}
    for it in adapted_items:
        photo = it.pop('photo', '')
        items_lite.append(it)
        if photo:
            photos_only[it['article'] or it['name']] = photo

    lite_file = PROJECT_ROOT / 'sneaker-order' / 'sneakers_lite.json'
    photos_file = PROJECT_ROOT / 'sneaker-order' / 'sneakers_photos.json'
    lite_file.write_text(json.dumps(items_lite, ensure_ascii=False))
    photos_file.write_text(json.dumps(photos_only, ensure_ascii=False))
    print(f"   ✓ Lite (без фото): {lite_file.stat().st_size/1024:.0f} KB")
    print(f"   ✓ Фото: {photos_file.stat().st_size/1024/1024:.1f} MB")

    # В HTML вставляем 2-stage loader: сначала данные → рендер, потом фото → дорендерим
    new_data_block = '''// === DATA ===
let ALL_ITEMS = [];

// Теги сезонности (compound-система): {article: season}. Хранятся в Supabase model_tags.
let seasonTags = {};
const SEASON_TAGS = [['slides','🩴'],['summer','☀️ Лето'],['demi','🌗 Деми'],
                     ['autumn','🍂 Осень'],['winter','❄️ Мех'],['allseason','💎 Всесез']];

async function setSeasonTag(art, season) {
  const cur = seasonTags[art] || '';
  try {
    if (cur === season) {
      // повторный тап = снять тег
      const r = await fetch(SUPABASE_URL + '/rest/v1/model_tags?article=eq.' + encodeURIComponent(art),
        {method: 'DELETE', headers: SB_HEADERS});
      if (!r.ok) throw new Error(r.status);
      delete seasonTags[art];
      toast('Тег снят: ' + art);
    } else {
      const r = await fetch(SUPABASE_URL + '/rest/v1/model_tags', {
        method: 'POST',
        headers: {...SB_HEADERS, 'Prefer': 'resolution=merge-duplicates,return=minimal'},
        body: JSON.stringify({article: art, season: season, updated_at: new Date().toISOString()})
      });
      if (!r.ok) throw new Error(r.status);
      seasonTags[art] = season;
      toast('✓ ' + art + ': тег сохранён');
    }
    applyFilters();
  } catch(e) {
    toast('⚠️ Не сохранилось (' + e.message + ') — проверь интернет');
  }
}

async function loadSeasonTags() {
  try {
    const r = await fetch(SUPABASE_URL + '/rest/v1/model_tags?select=article,season&limit=10000',
      {headers: SB_HEADERS});
    if (r.ok) {
      (await r.json()).forEach(t => seasonTags[t.article] = t.season);
      applyFilters();
    }
  } catch(e) {}
}

async function loadData() {
  const loader = document.getElementById('loader');
  if (loader) loader.textContent = '⏳ Загружаю данные…';
  const r = await fetch('sneakers_lite.json');
  ALL_ITEMS = await r.json();
  if (loader) loader.textContent = '⏳ Рендерим карточки…';
  applyFilters();
  if (loader) loader.style.display = 'none';
  loadSeasonTags();  // теги сезонности из облака (не блокирует)

  // СИНХРОНИЗАЦИЯ между устройствами: подтянуть актуальные скидки из Supabase.
  // Без этого правки Алуа с телефона не видны на компе (localStorage у каждого свой).
  try {
    const sr = await fetch(SUPABASE_URL + '/rest/v1/orders?id=eq.SNEAKERS-001&select=items', {headers: SB_HEADERS});
    if (sr.ok) {
      const rows = await sr.json();
      const cloud = (rows[0] && rows[0].items) || [];
      let synced = 0;
      cloud.forEach(it => { if (it.article && it.discount > 0) { discounts[it.article] = it.discount; synced++; } });
      if (synced > 0) {
        try { localStorage.setItem('clothing_discounts', JSON.stringify(discounts)); } catch(e) {}
        applyFilters();
        if (typeof toast === 'function') toast('☁️ Подгружено из облака: ' + synced + ' скидок');
      }
    }
  } catch(e) {}

  // Фото догружаем в фоне (не блокирует UI)
  setTimeout(async () => {
    try {
      const r2 = await fetch('sneakers_photos.json');
      const photos = await r2.json();
      ALL_ITEMS.forEach(it => {
        const k = it.article || it.name;
        if (photos[k]) it.photo = photos[k];
      });
      applyFilters();
    } catch(e) { console.error('Photos load failed:', e); }
  }, 100);
}

'''
    new_html = re.sub(r'// === DATA ===.*?(?=// === STATE ===)',
                      lambda _: new_data_block,
                      new_html, count=1, flags=re.DOTALL)
    new_html = new_html.replace('// === INIT ===\napplyFilters();',
                                '// === INIT ===\nloadData();')

    # Лоадер сверху и заменим легенду на нативный <details>
    loader_html = '<div id="loader" style="text-align:center; padding:20px; color:#3b82f6; font-weight:600;">⏳ Загружаю…</div>\n'
    new_html = re.sub(r'<div class="items" id="itemsList"></div>',
                      f'{loader_html}<div class="items" id="itemsList"></div>',
                      new_html, count=1)

    # Не вставляем дополнительный legend — оставим из шаблона
    legend_html = ''
    # Финальная чистка дубликатов через построчную обработку
    lines = new_html.split('\n')
    cleaned = []
    seen_legend_block = False
    in_legend = False
    seen_options = set()
    seen_legend_rows = set()
    for ln in lines:
        # Дедуп option в select
        opt = re.search(r'<option value="(\w+)"[^>]*>([^<]+)</option>', ln)
        if opt:
            key = (opt.group(1), opt.group(2))
            if key in seen_options:
                continue  # пропускаем дубликат
            seen_options.add(key)
        # Дедуп <div class="legend-row">...</div> по полному тексту
        if 'class="legend-row"' in ln:
            content = re.sub(r'\s+', ' ', ln.strip())
            if content in seen_legend_rows:
                continue
            seen_legend_rows.add(content)
        cleaned.append(ln)
    new_html = '\n'.join(cleaned)
    # legend оставляем как есть из шаблона, ничего не вставляем

    # Удаляем дубликаты legend block (если они есть)
    legend_pattern = r'<button class="legend-toggle"[^>]*>[^<]*</button>\s*<div class="legend-body">.*?</div>\s*\n*'
    matches = list(re.finditer(legend_pattern, new_html, flags=re.DOTALL))
    if len(matches) > 1:
        # Оставим только первый, удалим остальные
        for m in matches[1:][::-1]:  # reverse чтобы offsets оставались валидными
            new_html = new_html[:m.start()] + new_html[m.end():]

    # CSS — добавим badges для категорий + лёгкая адаптация
    extra_css = '''
.cat-badge { padding:5px 10px; border-radius:6px; font-size:11px; font-weight:800; letter-spacing:0.3px; display:inline-block; margin-bottom:6px; }
.cat-SLIDES       { background:#ecfeff; color:#0891b2; }
.cat-UNPROFITABLE { background:#fef2f2; color:#dc2626; }
.cat-SUMMER_CLEAR { background:#fff7ed; color:#ea580c; }
.cat-DEAD         { background:#fef2f2; color:#ef4444; }
.cat-SLOW         { background:#fffbeb; color:#f59e0b; }
.cat-HOLD_AUTUMN  { background:#fef3c7; color:#92400e; }
.cat-REORDERED    { background:#f5f3ff; color:#7c3aed; }
.cat-NEW          { background:#f3f4f6; color:#6b7280; }
.cat-HOT          { background:#ecfdf5; color:#10b981; }
.cat-NORMAL       { background:#eff6ff; color:#3b82f6; }
.cat-INTENTIONAL  { background:#f5f5f4; color:#525252; }
.cat-reason       { font-size:12px; color:var(--text2); margin-bottom:8px; line-height:1.4; }
.brand-badge      { padding:2px 8px; border-radius:10px; font-size:10px; font-weight:700; background:#1a1d23; color:#fff; display:inline-block; margin-right:4px; }
.flow-box         { display:grid; grid-template-columns:repeat(3,1fr); gap:6px; margin-bottom:8px; padding:8px; background:#fafbfc; border-radius:8px; border:1px solid var(--border); }
.flow-cell        { text-align:center; }
.flow-label       { font-size:10px; font-weight:700; color:var(--text3); text-transform:uppercase; letter-spacing:0.5px; margin-bottom:2px; }
.flow-val         { font-size:17px; font-weight:800; color:var(--text); }
.flow-sub         { font-size:10px; color:var(--text3); margin-top:2px; }
.size-chips       { display:flex; gap:5px; flex-wrap:wrap; padding:8px 10px; background:#fafbfc; border-radius:6px; border:1px solid var(--border); margin-bottom:8px; }
.chip             { padding:4px 10px; border-radius:12px; font-size:12px; font-weight:600; background:#fff; border:1px solid var(--border); }
.chip b           { font-weight:800; margin-left:3px; }
.chip.zero        { background:#fef2f2; color:#ef4444; border-color:#fecaca; }
.chip.low         { background:#fffbeb; color:#f59e0b; border-color:#fde68a; }
.chip.ok          { background:#ecfdf5; color:#10b981; border-color:#a7f3d0; }
.last-sale-info   { font-size:12px; color:var(--text2); margin-top:6px; }
.last-sale-info b { color:var(--text); }
.unprofit-warn    { background:#fef2f2; padding:8px 10px; border-radius:6px; border-left:3px solid #dc2626; font-size:12px; font-weight:600; color:#dc2626; margin-bottom:8px; }
.curdisc-badge    { display:inline-block; margin-left:6px; padding:3px 8px; border-radius:6px; font-size:10px; font-weight:700; background:#fef3c7; color:#92400e; border:1px solid #fcd34d; }
.season-btn       { font-size:11px; padding:6px 8px; }
.season-btn.active { background:#0891b2; border-color:#0891b2; color:#fff; }
.season-row label { color:#0891b2; font-weight:700; }
@media(max-width:600px) {
  .stats { grid-template-columns:repeat(3,1fr); gap:6px; padding:10px 12px; }
  .item-photo, .item-photo-empty { width:70px; height:70px; }
  .disc-btn { padding:10px 14px; min-width:50px; min-height:38px; font-size:14px; }
  .flow-val { font-size:16px; }
}
'''
    new_html = new_html.replace('</style>', extra_css + '\n</style>')

    # Filter-bar — заменим на категории
    cat_emoji_count = []
    cat_order = ['SLIDES', 'UNPROFITABLE', 'SUMMER_CLEAR', 'DEAD', 'COOLING_REORDER',
                 'COOLING_SALE', 'SLOW', 'HOLD_AUTUMN', 'REORDERED', 'NEW', 'HOT',
                 'NORMAL', 'INTENTIONAL']
    cat_emoji_count.append('<button class="filter-btn active" onclick="setFilter(\'all\')">Все</button>')
    for c in cat_order:
        if c in cat_count:
            label = CAT_CONFIG[c]['label']
            cat_emoji_count.append(f'<button class="filter-btn" onclick="setFilter(\'cat:{c}\')">{label} ({cat_count[c]})</button>')
    # Бренды — топ-7 по количеству
    brand_count = {}
    for it in items: brand_count[it['brand']] = brand_count.get(it['brand'], 0) + 1
    top_brands = sorted(brand_count, key=lambda x: -brand_count[x])[:7]
    for b in top_brands:
        cat_emoji_count.append(f'<button class="filter-btn" onclick="setFilter(\'brand:{b}\')">{b} ({brand_count[b]})</button>')

    new_html = re.sub(r'<div class="filter-bar" id="filterBar">.*?</div>',
                      f'<div class="filter-bar" id="filterBar">\n{chr(10).join(cat_emoji_count)}\n</div>',
                      new_html, count=1, flags=re.DOTALL)

    # === Заменяем целиком блок FILTERS & SORT === ... === RENDER ===
    new_filter_block = '''// === FILTERS & SORT ===
function setFilter(f) {
  currentFilter = f;
  document.querySelectorAll('.filter-btn').forEach(b => {
    const onclick = b.getAttribute('onclick') || '';
    const m = onclick.match(/setFilter\\('([^']+)'\\)/);
    b.classList.toggle('active', m && m[1] === f);
  });
  applyFilters();
}

function applyFilters() {
  const q = document.getElementById('search').value.toLowerCase();
  const sort = document.getElementById('sortSelect').value;

  let filtered = ALL_ITEMS.filter(item => {
    if (currentFilter !== 'all') {
      if (currentFilter.startsWith('cat:')) {
        if (item.category !== currentFilter.slice(4)) return false;
      } else if (currentFilter.startsWith('brand:')) {
        if (item.brand !== currentFilter.slice(6)) return false;
      } else if (item.subfolder !== currentFilter) return false;
    }
    if (q && !item.name.toLowerCase().includes(q) && !(item.article || '').toLowerCase().includes(q)) return false;
    return true;
  });

  filtered.sort((a, b) => {
    switch(sort) {
      case 'stock_value': return (b.frozen_retail || 0) - (a.frozen_retail || 0);
      case 'sell_through': return (b.sell_through || 0) - (a.sell_through || 0);
      case 'stock': return (b.stock.total || 0) - (a.stock.total || 0);
      case 'name': return a.name.localeCompare(b.name);
      case 'cost': return (b.frozen_cost || 0) - (a.frozen_cost || 0);
      case 'frozen_retail': return (b.frozen_retail || 0) - (a.frozen_retail || 0);
      case 'velocity': return ((b.sales && b.sales.s30) || 0) - ((a.sales && a.sales.s30) || 0);
      default: return 0;
    }
  });

  renderItems(filtered);
  updateStats(filtered);
}

function updateStats(items) {
  const totalStock = items.reduce((s,i) => s + i.stock.total, 0);
  const totalS30 = items.reduce((s,i) => s + ((i.sales && i.sales.s30) || 0), 0);
  const totalCost = items.reduce((s,i) => s + (i.frozen_cost || 0), 0);
  const totalRetail = items.reduce((s,i) => {
    const disc = discounts[i.article || i.name] || 0;
    const price = disc > 0 ? i.retail * (1 - disc/100) : i.retail;
    return s + i.stock.total * price;
  }, 0);
  const avgPrice = totalStock ? totalRetail / totalStock : 0;
  const totalRev = totalS30 * avgPrice;

  document.getElementById('stat-items').textContent = items.length;
  document.getElementById('stat-stock').textContent = fmt(totalStock);
  document.getElementById('stat-sold').textContent = fmt(totalS30);
  document.getElementById('stat-cost').textContent = fmt(Math.round(totalCost));
  document.getElementById('stat-retail').textContent = fmt(Math.round(totalRetail));
  document.getElementById('stat-revenue').textContent = fmt(Math.round(totalRev));
  document.getElementById('bottom-count').textContent = totalStock + ' шт';
  document.getElementById('bottom-value').textContent = fmt(Math.round(totalRetail)) + ' ₸';
}

'''
    new_html = re.sub(r'// === FILTERS & SORT ===.*?(?=// === RENDER ===)',
                      lambda _: new_filter_block,
                      new_html, count=1, flags=re.DOTALL)
    # setFilter уже в новом filter_block выше

    # Sort options
    new_html = new_html.replace(
        '<option value="cost">По себестоимости</option>',
        '<option value="cost">По заморож. себес</option>\n    <option value="frozen_retail">По стоимости в РЦ</option>\n    <option value="velocity">По скорости 30д</option>')
    new_html = new_html.replace(
        "case 'cost': return (b.stock_cost || 0) - (a.stock_cost || 0);",
        """case 'cost': return (b.frozen_cost || 0) - (a.frozen_cost || 0);
      case 'frozen_retail': return (b.frozen_retail || 0) - (a.frozen_retail || 0);
      case 'velocity': return (b.sales.s30 || 0) - (a.sales.s30 || 0);""")
    # Sort default
    new_html = new_html.replace('<option value="stock_value">По стоимости</option>',
                                '<option value="stock_value">По заморож. (РЦ)</option>')

    # === ВАЖНО ===
    # Удаляем СТАРЫЕ функции renderItem/renderVariants (от clothing template)
    # перед тем как вставить НОВЫЕ — иначе будут дубликаты которые ломают логику
    # Удаляем весь блок RENDER целиком — потом вставим свой
    render_block_re = r'// === RENDER ===.*?(?=// === DISCOUNT ===)'

    CAT_LABELS_JS = json.dumps({k: v['label'] for k, v in CAT_CONFIG.items()}, ensure_ascii=False)
    new_render_block = '''// === RENDER ===
function renderItems(items) {
  const container = document.getElementById('itemsList');
  container.innerHTML = items.map((item, idx) => renderItem(item, idx)).join('');
}

function renderSizes(item) {
  const s = (item.variants && item.variants.sizes) || item.sizes || {};
  const keys = Object.keys(s);
  if (!keys.length) return '';
  const sorted = keys.slice().sort((a,b) => parseFloat(a) - parseFloat(b));
  const chips = sorted.map(sz => {
    const q = s[sz];
    const cls = q === 0 ? 'zero' : q <= 1 ? 'low' : 'ok';
    return `<span class="chip ${cls}">${sz}<b>${q}</b></span>`;
  }).join('');
  return `<div class="size-chips">${chips}</div>`;
}

''' + new_render_function() + '''

'''
    new_html = re.sub(render_block_re,
                      lambda _: new_render_block,
                      new_html, count=1, flags=re.DOTALL)

    _DEAD_CODE_FROM_OLD_VERSION = '''DEAD
function renderItem(item, idx) {
  const itemKey = item.article || item.name;
  const disc = discounts[itemKey] || 0;
  const newPrice = disc > 0 ? Math.round(item.retail * (1 - disc/100)) : 0;
  const stBadge = stClass(item.sell_through);
  const CAT_LABELS = ''' + CAT_LABELS_JS + ''';

  const photoHtml = item.photo
    ? `<img class="item-photo" src="data:image/jpeg;base64,${item.photo}" alt="${item.name}" onclick="openLightbox('${itemKey.replace(/'/g,"")}')">`
    : `<div class="item-photo-empty"><div class="art">${(item.article||'—').substring(0,8)}</div><div class="nf">нет фото</div></div>`;

  const catLabel = CAT_LABELS[item.category] || item.category;
  const curDiscBadge = (item.cur_disc > 0) ? `<span class="curdisc-badge">в МС уже −${item.cur_disc}%</span>` : '';
  const catHtml = `<div><span class="cat-badge cat-${item.category}">${catLabel}</span>${curDiscBadge}
    <div class="cat-reason">${item.reason || ''}</div></div>`;

  // Особое предупреждение для убыточных
  let unprofitWarn = '';
  if (item.category === 'UNPROFITABLE' && item.cost > item.retail) {
    const loss = Math.round(item.cost - item.retail);
    unprofitWarn = `<div class="unprofit-warn">⚠️ Каждая продажа = убыток ${fmt(loss)}₸/пара. Поднять цену хотя бы до ${fmt(Math.round(item.cost * 1.3))}₸</div>`;
  }

  // Скорость и WOS
  const s30 = item.sales.s30 || 0;
  const wos = item.wos || 999;
  const wosClass = wos < 4 ? 'st-bad' : wos < 12 ? 'st-medium' : wos >= 99 ? 'st-none' : 'st-good';
  const wosLabel = wos >= 99 ? 'не продаётся' : wos < 4 ? `${wos.toFixed(0)} нед (мало)` : wos < 12 ? `${wos.toFixed(0)} нед` : `${wos.toFixed(0)} нед (много)`;
  const velHtml = `<div class="velocity-box" style="display:flex; gap:10px; padding:8px 10px; background:var(--bg); border-radius:6px; margin-bottom:8px; font-size:12px; align-items:center; flex-wrap:wrap;">
    <span>⚡ <b>${s30}</b> шт/мес (посл 30д)</span>
    <span style="color:var(--text3)">за 90д: ${item.sales.s90 || 0}</span>
    <span style="margin-left:auto" class="${wosClass}" style="padding:2px 8px; border-radius:6px;">📦 ${wosLabel}</span>
  </div>`;

  // Завезли/Продано/Остаток
  const flowHtml = `<div class="flow-box">
    <div class="flow-cell">
      <div class="flow-label">Остаток</div>
      <div class="flow-val" style="color:#3b82f6">${item.stock.total}</div>
      <div class="flow-sub">${item.frozen_cost > 0 ? fmtK(item.frozen_cost) + '₸ себес' : ''}</div>
    </div>
    <div class="flow-cell">
      <div class="flow-label">Продано 90д</div>
      <div class="flow-val" style="color:#10b981">${item.sales.s90 || 0}</div>
      <div class="flow-sub">из них 30д: ${s30}</div>
    </div>
    <div class="flow-cell">
      <div class="flow-label">Посл. продажа</div>
      <div class="flow-val" style="font-size:14px">${item.last_sale ? item.last_sale.slice(5) : '—'}</div>
      <div class="flow-sub">${item.days_no_sale != null ? item.days_no_sale + ' дн назад' : 'не было'}</div>
    </div>
  </div>`;

  return `
  <div class="item" data-article="${itemKey}">
    <div class="item-main">
      ${photoHtml}
      <div class="item-body">
        <div class="item-name">${item.name}</div>
        <div class="item-article"><span class="brand-badge">${item.brand}</span><span style="color:var(--blue);cursor:pointer" onclick="navigator.clipboard.writeText('${itemKey}').then(()=>toast('${itemKey} скопирован'))">${item.article} 📋</span></div>
      </div>
      <div class="item-right">
        <span class="st-badge ${stBadge}">${item.sell_through > 0 ? item.sell_through.toFixed(0) + '%' : '—'}</span>
        <div class="stock-total">${item.stock.total}</div>
      </div>
    </div>
    <div class="item-details">
      ${catHtml}
      ${unprofitWarn}
      ${flowHtml}
      ${historyHtml}
      ${velHtml}
      <div class="stock-grid">
        <div class="stock-cell"><div class="stock-cell-label">Мск</div><div class="stock-cell-val ${item.stock.moscow === 0 ? 'zero' : ''}">${item.stock.moscow}</div></div>
        <div class="stock-cell"><div class="stock-cell-label">ЦУМ+Онл</div><div class="stock-cell-val ${item.stock.tsum_online === 0 ? 'zero' : ''}">${item.stock.tsum_online}</div></div>
        <div class="stock-cell"><div class="stock-cell-label">Аружан</div><div class="stock-cell-val ${item.stock.aruzhan === 0 ? 'zero' : ''}">${item.stock.aruzhan}</div></div>
        <div class="stock-cell"><div class="stock-cell-label">Склад</div><div class="stock-cell-val ${item.stock.warehouse === 0 ? 'zero' : ''}">${item.stock.warehouse}</div></div>
      </div>
      ${renderSizes(item)}
      <div class="price-row">
        <div class="price-item"><span class="label">Себестоимость</span> <span class="val">${item.cost > 0 ? fmt(Math.round(item.cost)) + '₸' : '—'}</span></div>
        ${item.cur_disc > 0
          ? `<div class="price-item"><span class="label">Изначальная</span> <span class="val" style="text-decoration:line-through;color:var(--text3)">${fmt(Math.round(item.orig_price))}₸</span></div>
             <div class="price-item"><span class="label">Сейчас (−${item.cur_disc}%)</span> <span class="val" style="color:#f59e0b">${fmt(Math.round(item.retail))}₸</span></div>`
          : `<div class="price-item"><span class="label">Цена в магазине</span> <span class="val">${item.retail > 0 ? fmt(Math.round(item.retail)) + '₸' : '—'}</span></div>`}
        ${item.margin_pct > 0 ? `<div class="price-item"><span class="label">Маржа</span> <span class="val">${item.margin_pct}%</span></div>` : ''}
        ${item.frozen_retail > 0 ? `<div class="price-item"><span class="label">Заморожено в РЦ</span> <span class="val">${fmtK(item.frozen_retail)}₸</span></div>` : ''}
      </div>
      <div class="discount-row" style="flex-wrap:wrap">
        <label>Скидка от текущей</label>
        <div class="disc-btns">
          ${[0,5,10,15,20,25,30,40,50].map(d => `<button type="button" class="disc-btn ${disc==d && d>0?'active':''}" onclick="onDiscount('${itemKey.replace(/'/g,"").replace(/"/g,"")}',${d})">${d?d+'%':'✕'}</button>`).join('')}
        </div>
        ${newPrice > 0
          ? `<span class="new-price">${fmt(newPrice)}₸</span>`
          : `<span class="new-price" style="color:var(--text3)">${item.retail > 0 ? fmt(Math.round(item.retail)) + '₸' : '—'}</span>`}
      </div>
    </div>
  </div>`;
}
'''  # конец _DEAD_CODE_FROM_OLD_VERSION

    # Discount fallback в saveDiscounts/updateStats/exportCSV
    new_html = new_html.replace(
        "const item = ALL_ITEMS.find(i => i.article === article);",
        "const item = ALL_ITEMS.find(i => (i.article || i.name) === article);")
    new_html = new_html.replace(
        "if (q && !item.name.toLowerCase().includes(q) && !item.article.toLowerCase().includes(q)) return false;",
        "if (q && !item.name.toLowerCase().includes(q) && !(item.article || '').toLowerCase().includes(q)) return false;")
    # discount key fallback в renderItem не нужен — мы уже сделали itemKey
    # но в saveDiscounts/updateStats/exportCSV заменим
    new_html = new_html.replace(
        "discounts[i.article]",
        "discounts[i.article || i.name]"
    )
    # Supabase orderId — отдельный для кроссовок чтобы не пересекалось с одеждой
    new_html = new_html.replace("'CLOTHING-001'", "'SNEAKERS-001'")

    # === ДОБАВИТЬ JSON-экспорт, сброс и прогресс для воркфлоу /clearance ===
    # 1. Кнопки JSON и Reset в footer + прогресс-индикатор
    new_html = new_html.replace(
        '''  <div style="display:flex; gap:6px;">
    <button class="btn btn-outline" onclick="exportCSV()">CSV</button>
    <button class="btn btn-green" id="saveBtn" onclick="saveDiscounts()" disabled>Сохранить</button>
  </div>''',
        '''  <div style="display:flex; gap:6px;">
    <button class="btn btn-outline" onclick="resetAllDiscounts()" title="Сбросить все скидки">🔄</button>
    <button class="btn btn-outline" onclick="exportJSON()" title="Экспорт JSON для применения цен в МойСкладе">JSON</button>
    <button class="btn btn-outline" onclick="exportCSV()">CSV</button>
    <button class="btn btn-green" id="saveBtn" onclick="saveDiscounts()" disabled>Сохранить</button>
  </div>'''
    )
    new_html = new_html.replace(
        '''    <span id="bottom-count">1883 шт</span> |
    <b id="bottom-value">28,693,700 ₸</b>
  </div>''',
        '''    <span id="bottom-count">1883 шт</span> |
    <b id="bottom-value">28,693,700 ₸</b>
    <span id="discount-progress" style="margin-left:8px;color:#d97706;font-weight:700;display:none">📝 <span id="dp-count">0</span> со скидкой</span>
  </div>'''
    )

    # 2. Добавить функции exportJSON, resetAllDiscounts + прогресс в updateStats
    new_html = new_html.replace(
        '''  document.getElementById('bottom-count').textContent = totalStock + ' шт';
  document.getElementById('bottom-value').textContent = fmt(Math.round(totalRetail)) + ' ₸';
}''',
        '''  document.getElementById('bottom-count').textContent = totalStock + ' шт';
  document.getElementById('bottom-value').textContent = fmt(Math.round(totalRetail)) + ' ₸';

  // Прогресс — сколько товаров со скидкой
  const discCount = Object.keys(discounts).filter(k => discounts[k] > 0).length;
  const dpEl = document.getElementById('discount-progress');
  const dpCount = document.getElementById('dp-count');
  if (dpEl && dpCount) {
    if (discCount > 0) {
      dpEl.style.display = 'inline';
      dpCount.textContent = discCount;
    } else {
      dpEl.style.display = 'none';
    }
  }
}

// === EXPORT JSON для update_ms_prices.py ===
function exportJSON() {
  const items = ALL_ITEMS.filter(i => discounts[i.article || i.name] > 0).map(i => ({
    article: i.article,
    name: i.name,
    current_retail: i.orig_price || i.retail,   // изначальная = зачёркнутая на стикере
    cur_price: i.retail,                          // текущая цена (база скидки)
    discount: discounts[i.article || i.name],
    new_price: Math.round(i.retail * (1 - discounts[i.article || i.name]/100)),
    stock: i.stock.total,
    cost: i.cost,
    health: i.health_v2 || i.category,
  }));
  if (items.length === 0) { toast('Нет скидок для экспорта'); return; }
  const payload = { generated_at: new Date().toISOString(), total_items: items.length, items: items };
  const blob = new Blob([JSON.stringify(payload, null, 2)], {type: 'application/json'});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = 'clearance_decisions_' + new Date().toISOString().slice(0,10) + '.json';
  a.click();
  URL.revokeObjectURL(url);
  toast('Экспорт: ' + items.length + ' позиций');
}

// === RESET ALL DISCOUNTS ===
function resetAllDiscounts() {
  const n = Object.keys(discounts).filter(k => discounts[k] > 0).length;
  if (n === 0) { toast('Нет скидок для сброса'); return; }
  if (!confirm('Сбросить ВСЕ скидки (' + n + ' позиций)? Это нельзя отменить.')) return;
  discounts = {};
  try { localStorage.setItem('clothing_discounts', JSON.stringify(discounts)); } catch(e) {}
  hasChanges = true;
  applyFilters();
  toast('Сброшено: ' + n + ' скидок');
}'''
    )

    OUT_HTML.write_text(new_html)
    size_mb = OUT_HTML.stat().st_size / 1024 / 1024
    print(f"\n✓ HTML: {OUT_HTML} ({size_mb:.1f} MB)")


if __name__ == '__main__':
    items = main()
    render_html(items)
