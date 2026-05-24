#!/usr/bin/env python3
"""
Builder для clothing_clearance.html — дашборд УЦЕНКИ женской одежды.

Та же схема что у обуви (build_sneakers.py), но под одежду:
- Универс артикулов = папка МойСклад "Женская одежда" (authoritative)
- Карточка = 1 артикул, внутри МАТРИЦА цвет×размер (а не просто размеры)
- Скидка ставится на ВЕСЬ артикул (все цвета+размеры) — как обувь
- Продажи из retaildemand_positions по НАЗВАНИЮ модели (у одежды артикул в этих
  таблицах пустой — матчим по split_part(name,' ('))
- Себес из supply_positions по названию модели
- Категории health: DEAD / COOLING / SLOW / OK / HOT / NEW + UNPROFITABLE
- Учитывает ТЕКУЩУЮ скидку в МС (Цена продажи новая < Цена продажи)
- exportJSON в формате clearance_decisions для update_ms_prices.py

Запуск: python3 sneaker-order/build_clothing_clearance.py [--skip-photos]
"""
import duckdb
import json
import re
import sys
import os
import base64
import copy
import requests
from datetime import date, timedelta
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / '.env')
except Exception:
    pass

PROJECT_ROOT = Path(__file__).parent.parent
DB_PATH = PROJECT_ROOT / 'data' / 'pnlpower.duckdb'
TEMPLATE_HTML = PROJECT_ROOT / 'sneaker-order' / 'clothing.html'  # используем как шаблон
OUT_HTML = PROJECT_ROOT / 'sneaker-order' / 'clothing_clearance.html'
OUT_JSON = PROJECT_ROOT / 'sneaker-order' / 'clothing_clearance_data.json'
PHOTO_CACHE = PROJECT_ROOT / 'sneaker-order' / '.photo_cache_clothing.json'
LITE_NAME = 'clothing_clearance_lite.json'
PHOTOS_NAME = 'clothing_clearance_photos.json'
LS_KEY = 'clothing_clearance_discounts'      # отдельный localStorage-ключ!
SUPABASE_ORDER_ID = 'CLOTHING-CLEAR-001'

MOYSKLAD_TOKEN = os.getenv('MOYSKLAD_TOKEN')
MS_HEADERS = {'Authorization': f'Bearer {MOYSKLAD_TOKEN}', 'Accept': 'application/json;charset=utf-8'}

SNAPSHOT_DATE = '20260523'
PRICES_DATE = '20260522'

TODAY = date.today()

SIZE_ORDER = {'XS': 0, 'S': 1, 'M': 2, 'L': 3, 'XL': 4, 'XXL': 5, '2XL': 5,
              'XXXL': 6, '3XL': 6, '4XL': 7, 'OneSize': 8, 'One Size': 8}


def classify_subfolder(name: str) -> str:
    """Грубая категория одежды для фильтра."""
    n = (name or '').lower()
    if any(k in n for k in ['футбол', 'худи', 'лонгслив', 'рубашк', 'топ ', 'майк', 'жилет', 'свитшот', 'толстовк', 'боди', 'шакет', 'бомбер', 'жакет']):
        return 'Верх'
    if any(k in n for k in ['джинс', 'брюки', 'шорт', 'кюлот', 'юбк', 'легинс']):
        return 'Низ'
    if 'костюм' in n:
        return 'Костюмы'
    if any(k in n for k in ['платье', 'сарафан']):
        return 'Платья'
    return 'Прочее'


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


def classify_clothing(stock, s30, s30_60, s90, sold_all, first_sale, last_sale,
                      cost, retail, cur_disc):
    """Категоризация уценки одежды.

    Возвращает (category, health, suggested_discount, reason).
    category — бакет для фильтра/CSS (UNPROFITABLE/DEAD/SLOW/NEW/HOT/NORMAL).
    health   — детальный статус (DEAD/COOLING/SLOW/OK/HOT/NEW).
    suggested_discount — рекомендуемая скидка (с учётом текущей в МС).
    """
    age = (TODAY - first_sale).days if first_sale else 999
    last_days = (TODAY - last_sale).days if last_sale else 999
    st = sold_all / (sold_all + stock) * 100 if (sold_all + stock) > 0 else 0

    # Убыточные (РЦ ≤ себес×1.1)
    if cost > 0 and retail > 0 and retail < cost * 1.1:
        if s30 > 0:
            return ('UNPROFITABLE', 'UNPROFITABLE', 0,
                    f'РЦ {retail:,.0f} ≤ себес {cost:,.0f}, но продаётся ({s30}/30д) → ПОДНЯТЬ цену до {cost*1.3:,.0f}')
        return ('UNPROFITABLE', 'UNPROFITABLE', 0,
                f'РЦ {retail:,.0f} ≤ себес {cost:,.0f} и не идёт — поднять цену, скидка бессмысленна')

    # Слишком новый
    if first_sale and age < 30:
        return ('NEW', 'NEW', 0, f'В продаже {age} дн — рано судить')

    # Базовая категория
    if s90 == 0 or last_days > 80:
        cat, health = 'DEAD', 'DEAD'
        disc = 50 if age > 365 else 40
        reason = (f'Ни одной продажи' if last_days >= 999 else f'{last_days} дн без продаж') + \
                 f', {stock} шт лежат — глубокая уценка'
    elif s30 == 0:
        cat, health, disc = 'SLOW', 'COOLING', 25
        reason = f'Продавалось, встало ({last_days} дн назад), 90д={s90} — остыло'
    elif s30 < 0.4 * max(s30_60, 1) and stock >= 5:
        cat, health, disc = 'SLOW', 'COOLING', 25
        reason = f'Скорость упала ({s30_60}→{s30}/мес) при остатке {stock} — остывает'
    else:
        wos_m = stock / max(s30, 0.5)  # месяцев запаса
        if wos_m > 5 and st < 50:
            cat, health, disc = 'SLOW', 'SLOW', 15
            reason = f'{s30}/мес × {stock} шт = ~{wos_m:.0f} мес запаса — мягко ускорить'
        elif st > 40 and s30 >= s30_60:
            cat, health, disc = 'HOT', 'HOT', 0
            reason = f'Хит: {s30}/мес, ST {st:.0f}% — не трогать'
        else:
            cat, health, disc = 'NORMAL', 'OK', 0
            reason = f'Норма: {s30}/мес, {stock} шт'

    # Усилитель: вообще без продаж 60д и есть запас
    if (s30 + s30_60) == 0 and stock >= 5 and 0 < disc < 50:
        disc = min(50, disc + (10 if disc >= 40 else 15))

    # Защита маржи: финальная цена ≥ себес × 1.05
    if retail > 0 and cost > 0 and disc > 0:
        min_p = cost * 1.05
        if retail * (1 - disc / 100) < min_p:
            disc = min(disc, max(0, int(100 * (1 - min_p / retail))))

    # Учёт ТЕКУЩЕЙ скидки в МС: если уже не меньше рекомендуемой — углублять не надо
    if cur_disc > 0 and disc <= cur_disc:
        if health in ('DEAD', 'COOLING', 'SLOW') and s30 == 0 and disc > cur_disc:
            pass  # оставляем углубление
        else:
            disc = 0
            reason += f' | уже −{cur_disc}% в МС' + (' и шевелится' if s30 > 0 else '')
    elif cur_disc > 0 and disc > cur_disc:
        reason += f' | сейчас −{cur_disc}%, мало → поднять до −{disc}%'

    return (cat, health, disc, reason)


def load_photo_cache() -> dict:
    if PHOTO_CACHE.exists():
        return json.loads(PHOTO_CACHE.read_text())
    return {}


def save_photo_cache(cache: dict):
    PHOTO_CACHE.write_text(json.dumps(cache))


def fetch_ms_women_products() -> list:
    """Тянет все товары из папки 'Женская одежда' (с salePrices и images)."""
    if not MOYSKLAD_TOKEN:
        print("   ⚠️ Нет токена МС")
        return []
    prods, offset = [], 0
    while True:
        url = (f'https://api.moysklad.ru/api/remap/1.2/entity/product'
               f'?filter=pathName~Женская одежда&limit=100&offset={offset}')
        r = requests.get(url, headers=MS_HEADERS, timeout=30)
        r.raise_for_status()
        d = r.json()
        prods += d['rows']
        if len(prods) >= d['meta']['size']:
            break
        offset += 100
    return prods


def fetch_photos_for_products(products: list, cached: dict) -> dict:
    """Качает мини-фото для товаров (по article), используя их images.meta.href."""
    todo = [p for p in products if p.get('article') and p['article'] not in cached]
    print(f"   Скачать новых фото: {len(todo)} (в кеше: {len(cached)})")
    new_count = skip_count = 0
    for i, p in enumerate(todo, 1):
        art = p['article']
        try:
            img_meta = p.get('images', {}).get('meta', {})
            if not img_meta.get('size'):
                cached[art] = ''; continue
            r2 = requests.get(img_meta['href'], headers=MS_HEADERS, timeout=5)
            imgs = r2.json().get('rows', []) if r2.status_code == 200 else []
            if not imgs:
                cached[art] = ''; continue
            mini = imgs[0].get('miniature', {}).get('href') or imgs[0].get('tiny', {}).get('href')
            if not mini:
                cached[art] = ''; continue
            r3 = requests.get(mini, headers=MS_HEADERS, timeout=5)
            if r3.status_code != 200:
                cached[art] = ''; skip_count += 1; continue
            cached[art] = base64.b64encode(r3.content).decode('ascii')
            new_count += 1
            if (new_count + skip_count) % 20 == 0:
                print(f"      {i}/{len(todo)}: {new_count} скачано, {skip_count} пропущено", flush=True)
                save_photo_cache(cached)
        except Exception:
            cached[art] = ''; skip_count += 1
    save_photo_cache(cached)
    print(f"   Скачано: {new_count}, пропущено: {skip_count}")
    return cached


def main():
    today = TODAY
    print(f"Дата: {today}")

    # 1. Универс артикулов из папки МС "Женская одежда"
    print("\n1. Тяну женскую одежду из МойСклад (папка)...")
    products = fetch_ms_women_products()
    print(f"   Товаров в папке: {len(products)}")
    # article -> {name, reg, new, cur_disc}
    ms_by_article = {}
    for p in products:
        art = p.get('article')
        if not art:
            continue
        pr = {sp.get('priceType', {}).get('name'): sp.get('value', 0) / 100 for sp in p.get('salePrices', [])}
        reg = pr.get('Цена продажи', 0)
        new = pr.get('Цена продажи новая', 0)
        cur_disc = round(100 * (1 - new / reg)) if (reg and new and new < reg) else 0
        ms_by_article[art] = {'name': p.get('name', ''), 'reg': reg, 'new': new, 'cur_disc': cur_disc}
    articles = sorted(ms_by_article.keys())
    if not articles:
        print("   ⚠️ Нет артикулов — выходим"); return []
    art_in = "','".join(a.replace("'", "''") for a in articles)

    con = duckdb.connect(str(DB_PATH), read_only=True)
    snap = f'inventory_snapshot_stores_{SNAPSHOT_DATE}'

    # 2. Остатки + матрица цвет×размер из снапшота (по артикулу)
    print("\n2. Остатки и матрица цвет×размер...")
    snap_rows = con.execute(f"""
        SELECT article, product_name,
               SUM(total_stock) qty, SUM(moscow) m, SUM(tsum)+SUM(online) tsum_on,
               SUM(astana_aruzhan) ar, SUM(main_warehouse) wh
        FROM {snap}
        WHERE article IN ('{art_in}') AND total_stock > 0
        GROUP BY article, product_name
    """).fetchall()

    by_art = {}  # article -> aggregate
    for art, pname, qty, m, tsum_on, ar, wh in snap_rows:
        qty = int(qty or 0)
        d = by_art.setdefault(art, {
            'total': 0, 'moscow': 0, 'tsum_online': 0, 'aruzhan': 0, 'warehouse': 0,
            'colors': {}, 'sizes': {}, 'matrix': {}, 'base_counts': {}})
        d['total'] += qty
        d['moscow'] += int(m or 0); d['tsum_online'] += int(tsum_on or 0)
        d['aruzhan'] += int(ar or 0); d['warehouse'] += int(wh or 0)
        base = (pname or '').split(' (')[0]
        d['base_counts'][base] = d['base_counts'].get(base, 0) + qty
        # Парсим "(Цвет, Размер)" в конце имени
        m2 = re.search(r'\(([^()]+),\s*([^()]+)\)\s*$', pname or '')
        if m2:
            color, size = m2.group(1).strip(), m2.group(2).strip()
            d['colors'][color] = d['colors'].get(color, 0) + qty
            d['sizes'][size] = d['sizes'].get(size, 0) + qty
            d['matrix'][f'{color}/{size}'] = d['matrix'].get(f'{color}/{size}', 0) + qty
        else:
            # fallback: "..., Размер" или просто без вариантов
            m3 = re.search(r',\s*([^,()]+)\s*$', pname or '')
            sz = m3.group(1).strip() if m3 else 'OneSize'
            d['sizes'][sz] = d['sizes'].get(sz, 0) + qty

    # primary base_model по каждому артикулу (макс остаток) — для матча продаж/себеса
    primary_base = {art: max(d['base_counts'], key=d['base_counts'].get)
                    for art, d in by_art.items() if d['base_counts']}
    bases = sorted(set(primary_base.values()))
    base_in = "','".join(b.replace("'", "''") for b in bases)

    # 3. Себес по базовой модели из supply_positions
    print("\n3. Себестоимость по поставкам...")
    cost_by_base = {}
    for base, unit in con.execute(f"""
        SELECT split_part(product_name,' (',1) base, SUM(total)/NULLIF(SUM(quantity),0) unit
        FROM supply_positions
        WHERE applicable=true AND split_part(product_name,' (',1) IN ('{base_in}')
        GROUP BY 1
    """).fetchall():
        cost_by_base[base] = float(unit or 0)

    # 4. Продажи по базовой модели из retaildemand_positions (точный источник)
    print("\n4. Продажи из retaildemand_positions...")
    d30 = (today - timedelta(days=30)).isoformat()
    d60 = (today - timedelta(days=60)).isoformat()
    d90 = (today - timedelta(days=90)).isoformat()
    d180 = (today - timedelta(days=180)).isoformat()
    d365 = (today - timedelta(days=365)).isoformat()
    sales_by_base = {}
    for row in con.execute(f"""
        SELECT split_part(product_name,' (',1) base,
          SUM(quantity) FILTER (WHERE document_moment >= '{d30}') s30,
          SUM(quantity) FILTER (WHERE document_moment >= '{d60}' AND document_moment < '{d30}') s30_60,
          SUM(quantity) FILTER (WHERE document_moment >= '{d90}') s90,
          SUM(quantity) FILTER (WHERE document_moment >= '{d180}') s180,
          SUM(quantity) FILTER (WHERE document_moment >= '{d365}') s365,
          SUM(quantity) sall,
          MAX(DATE(document_moment)) last_sale, MIN(DATE(document_moment)) first_sale
        FROM retaildemand_positions
        WHERE price > 0 AND split_part(product_name,' (',1) IN ('{base_in}')
        GROUP BY 1
    """).fetchall():
        base, s30, s30_60, s90, s180, s365, sall, last_sale, first_sale = row
        sales_by_base[base] = dict(
            s30=int(s30 or 0), s30_60=int(s30_60 or 0), s90=int(s90 or 0),
            s180=int(s180 or 0), s365=int(s365 or 0), sall=int(sall or 0),
            last_sale=last_sale, first_sale=first_sale)

    # 5. Сборка items
    print("\n5. Сборка items...")
    items = []
    for art in articles:
        d = by_art.get(art)
        if not d or d['total'] <= 0:
            continue
        ms = ms_by_article[art]
        base = primary_base.get(art, ms['name'])
        sl = sales_by_base.get(base, {})
        s30 = sl.get('s30', 0); s30_60 = sl.get('s30_60', 0); s90 = sl.get('s90', 0)
        s180 = sl.get('s180', 0); s365 = sl.get('s365', 0); sall = sl.get('sall', 0)
        last_sale = sl.get('last_sale'); first_sale = sl.get('first_sale')
        cost = float(cost_by_base.get(base, 0) or 0)
        # orig = изначальная "Цена продажи" (зачёркнутая); cur = текущая (со скидкой если есть) = БАЗА скидки
        orig_price = float(ms['reg'] or 0)
        new_ms = float(ms['new'] or 0)
        retail = new_ms if (orig_price > 0 and 0 < new_ms < orig_price) else orig_price
        cur_disc = ms['cur_disc']
        total = d['total']

        days_no_sale = (today - last_sale).days if last_sale else 999
        daily = s30 / 30
        wos = (total / daily / 7) if daily > 0 else 999
        st = (sall / (total + sall) * 100) if (total + sall) > 0 else 0

        cat, health, sug_disc, reason = classify_clothing(
            total, s30, s30_60, s90, sall, first_sale, last_sale, cost, retail, cur_disc)

        # сортировка размеров
        sizes_sorted = dict(sorted(d['sizes'].items(),
                                   key=lambda x: (SIZE_ORDER.get(x[0], 50), x[0])))

        items.append({
            'article': art,
            'name': ms['name'] or base,
            'brand': classify_subfolder(ms['name'] or base),   # для фильтра
            'subfolder': classify_subfolder(ms['name'] or base),
            'stock': {'total': total, 'moscow': d['moscow'],
                      'tsum_online': d['tsum_online'], 'aruzhan': d['aruzhan'], 'warehouse': d['warehouse']},
            'sizes': sizes_sorted,
            'variants': {'colors': d['colors'], 'sizes': sizes_sorted, 'matrix': d['matrix']},
            'cost': cost, 'retail': retail,
            'orig_price': orig_price,
            'cur_disc': cur_disc,
            'margin_pct': round((retail - cost) / retail * 100, 1) if retail > 0 else 0,
            'sales': {'s30': s30, 's30_60': s30_60, 's90': s90, 's180': s180, 's365': s365,
                      'sall': sall, 'rev_30d': s30 * retail, 'rev_90d': s90 * retail},
            'last_sale': last_sale.isoformat() if last_sale else None,
            'days_no_sale': days_no_sale,
            'days_since_supply': None,
            'sell_through': round(st, 1),
            'wos': round(wos, 1) if wos < 999 else 999,
            'velocity': {'recent_30d': s30, 'avg_30d': round(s90 / 3, 1) if s90 else 0,
                         'acceleration': round(s30 / (s90 / 3), 2) if s90 > 0 else 0},
            'frozen_cost': int(total * cost),
            'frozen_retail': int(total * retail),
            'category': cat,
            'health_v2': health,
            'suggested_discount': sug_disc,
            'reason': reason,
        })

    # Сортировка: убыточные → мёртвые → медленные → новые → хиты → норма
    cat_order = {'UNPROFITABLE': 0, 'DEAD': 1, 'SLOW': 2, 'NEW': 3, 'HOT': 4, 'NORMAL': 5}
    items.sort(key=lambda x: (cat_order.get(x['category'], 99), -x['frozen_cost']))

    # 6. Фото
    print("\n6. Фото...")
    cache = load_photo_cache()
    skip_download = '--skip-photos' in sys.argv or os.environ.get('SKIP_PHOTOS')
    if skip_download:
        print("   --skip-photos: качаю только из кеша")
    else:
        cache = fetch_photos_for_products(products, cache)
    for it in items:
        it['photo'] = cache.get(it['article'], '')
    has_photo = sum(1 for it in items if it['photo'])
    print(f"   С фото: {has_photo}/{len(items)}")

    # Сводка
    cat_count, cat_frozen = {}, {}
    for it in items:
        c = it['category']
        cat_count[c] = cat_count.get(c, 0) + 1
        cat_frozen[c] = cat_frozen.get(c, 0) + it['frozen_cost']
    cat_names = {'UNPROFITABLE': '⚠️ Убыточные', 'DEAD': '🔴 Мёртвые',
                 'SLOW': '🟠 Медленные/Остывшие', 'NEW': '⚪ Новые',
                 'HOT': '🟢 Хиты', 'NORMAL': '🔵 Норма'}
    print("\n=== СВОДКА ===")
    for c in ['UNPROFITABLE', 'DEAD', 'SLOW', 'NEW', 'HOT', 'NORMAL']:
        if c in cat_count:
            print(f"  {cat_names[c]:<26} {cat_count[c]:>4} моделей  {cat_frozen[c]:>12,.0f} ₸")
    already = sum(1 for it in items if it['cur_disc'] > 0)
    to_apply = sum(1 for it in items if it['suggested_discount'] > 0)
    print(f"  Уже со скидкой в МС: {already} | Рекомендовано к уценке: {to_apply}")

    # JSON для верификации (без photo)
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
  const healthBadge = item.health_v2 ? `<span class="health-badge">${item.health_v2}</span>` : '';
  const curDiscBadge = (item.cur_disc > 0) ? `<span class="curdisc-badge">в МС уже −${item.cur_disc}%</span>` : '';
  const catHtml = `<div><span class="cat-badge cat-${item.category}">${catLabel}</span>${healthBadge}${curDiscBadge}
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
      ${(item.suggested_discount > 0 && disc === 0) ? `
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
    'UNPROFITABLE': {'label': '⚠️ УБЫТОЧНЫЕ', 'color': '#dc2626', 'bg': '#fef2f2'},
    'DEAD':         {'label': '🔴 МЁРТВЫЕ',    'color': '#ef4444', 'bg': '#fef2f2'},
    'SLOW':         {'label': '🟠 МЕДЛЕННЫЕ',  'color': '#f59e0b', 'bg': '#fffbeb'},
    'NEW':          {'label': '⚪ НОВЫЕ',       'color': '#6b7280', 'bg': '#f3f4f6'},
    'HOT':          {'label': '🟢 ХИТЫ',        'color': '#10b981', 'bg': '#ecfdf5'},
    'NORMAL':       {'label': '🔵 НОРМА',       'color': '#3b82f6', 'bg': '#eff6ff'},
    'INTENTIONAL':  {'label': '⚫ NB/Asics/Puma','color': '#525252', 'bg': '#f5f5f4'},
}


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
        # сохраняем уже собранные variants (матрица цвет×размер), не затираем!
        adapted = {**it,
                   'variants': it.get('variants') or {'colors': {}, 'sizes': it.get('sizes', {}), 'matrix': {}},
                   'subfolder': it.get('subfolder') or it.get('brand', 'Прочее')}
        adapted_items.append(adapted)

    new_html = TEMPLATE_HTML.read_text()

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

    # Отдельный localStorage-ключ (иначе скидки смешаются с обувью на том же origin)
    new_html = new_html.replace("'clothing_discounts'", f"'{LS_KEY}'")
    # Title — гибко
    new_html = re.sub(r'<title>[^<]+</title>',
                      '<title>Уценка одежды — Dashboard</title>', new_html)
    # H1 + badge
    new_html = re.sub(r'<h1>[^<]+</h1>\s*<span class="badge badge-info">[^<]+</span>',
                      f'<h1>Уценка одежды</h1>\n    <span class="badge badge-info">{total_models} моделей</span>',
                      new_html)
    # Header meta
    today_s = date.today().strftime('%d.%m.%Y')
    new_html = re.sub(r'<div class="header-meta">Остатки:[^<]*</div>',
                      f'<div class="header-meta">Остатки: {today_s} | Женская одежда</div>',
                      new_html)
    # Stats
    cat_count = {}
    for it in items: cat_count[it['category']] = cat_count.get(it['category'], 0) + 1
    pct = lambda c: f'{cat_count.get(c, 0)}'

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

    lite_file = PROJECT_ROOT / 'sneaker-order' / LITE_NAME
    photos_file = PROJECT_ROOT / 'sneaker-order' / PHOTOS_NAME
    lite_file.write_text(json.dumps(items_lite, ensure_ascii=False))
    photos_file.write_text(json.dumps(photos_only, ensure_ascii=False))
    print(f"   ✓ Lite (без фото): {lite_file.stat().st_size/1024:.0f} KB")
    print(f"   ✓ Фото: {photos_file.stat().st_size/1024/1024:.1f} MB")

    # В HTML вставляем 2-stage loader: сначала данные → рендер, потом фото → дорендерим
    new_data_block = '''// === DATA ===
let ALL_ITEMS = [];

async function loadData() {
  const loader = document.getElementById('loader');
  if (loader) loader.textContent = '⏳ Загружаю данные…';
  const r = await fetch('LITE_FILE_PLACEHOLDER');
  ALL_ITEMS = await r.json();
  if (loader) loader.textContent = '⏳ Рендерим карточки…';
  applyFilters();
  if (loader) loader.style.display = 'none';

  // Фото догружаем в фоне (не блокирует UI)
  setTimeout(async () => {
    try {
      const r2 = await fetch('PHOTOS_FILE_PLACEHOLDER');
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
    new_data_block = (new_data_block
                      .replace('LITE_FILE_PLACEHOLDER', LITE_NAME)
                      .replace('PHOTOS_FILE_PLACEHOLDER', PHOTOS_NAME))
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
.cat-UNPROFITABLE { background:#fef2f2; color:#dc2626; }
.cat-DEAD         { background:#fef2f2; color:#ef4444; }
.cat-SLOW         { background:#fffbeb; color:#f59e0b; }
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
.cat-COOLING      { background:#fffbeb; color:#f59e0b; }
.health-badge     { display:inline-block; margin-left:6px; padding:3px 8px; border-radius:6px; font-size:10px; font-weight:800; background:#1f2937; color:#fff; letter-spacing:0.3px; }
.curdisc-badge    { display:inline-block; margin-left:6px; padding:3px 8px; border-radius:6px; font-size:10px; font-weight:700; background:#fef3c7; color:#92400e; border:1px solid #fcd34d; }
.matrix-wrap      { overflow-x:auto; margin-bottom:8px; border:1px solid var(--border); border-radius:8px; }
table.matrix      { border-collapse:collapse; width:100%; font-size:12px; }
table.matrix th, table.matrix td { padding:5px 7px; text-align:center; border-bottom:1px solid var(--border); border-right:1px solid var(--border); }
table.matrix thead th { background:#f3f4f6; font-weight:800; font-size:11px; position:sticky; top:0; }
table.matrix td.mx-c { text-align:left; font-weight:700; background:#fafbfc; white-space:nowrap; max-width:130px; overflow:hidden; text-overflow:ellipsis; }
table.matrix th.mx-c { text-align:left; }
table.matrix td.mx0  { color:#cbd5e1; }
table.matrix td.mx1  { background:#fffbeb; color:#f59e0b; font-weight:700; }
table.matrix td.mxok { background:#ecfdf5; color:#10b981; font-weight:700; }
table.matrix td.mx-sum { font-weight:800; background:#eff6ff; color:#3b82f6; }
table.matrix tr.mx-foot td { border-bottom:none; background:#eff6ff; font-weight:800; }
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
    cat_order = ['UNPROFITABLE', 'DEAD', 'SLOW', 'NEW', 'HOT', 'NORMAL', 'INTENTIONAL']
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

const SIZE_ORD = {'XS':0,'S':1,'M':2,'L':3,'XL':4,'XXL':5,'2XL':5,'XXXL':6,'3XL':6,'4XL':7,'OneSize':8,'One Size':8};
function sizeKey(s){ return (s in SIZE_ORD) ? SIZE_ORD[s] : (parseFloat(s) || 50); }

function renderSizes(item) {
  const v = item.variants || {};
  const colors = v.colors || {};
  const sizes = v.sizes || item.sizes || {};
  const matrix = v.matrix || {};
  const sizeKeys = Object.keys(sizes).sort((a,b)=>sizeKey(a)-sizeKey(b));
  if (!sizeKeys.length) return '';
  const colorKeys = Object.keys(colors);

  // Если есть расцветки и матрица — рисуем таблицу цвет×размер
  if (colorKeys.length && Object.keys(matrix).length) {
    colorKeys.sort((a,b)=>(colors[b]||0)-(colors[a]||0));
    let head = '<th class="mx-c">Цвет / Разм</th>' + sizeKeys.map(s=>`<th>${s}</th>`).join('') + '<th>Σ</th>';
    let body = colorKeys.map(c=>{
      let row = sizeKeys.map(s=>{
        const q = matrix[c+'/'+s] || 0;
        const cls = q===0 ? 'mx0' : q<=1 ? 'mx1' : 'mxok';
        return `<td class="${cls}">${q||'·'}</td>`;
      }).join('');
      return `<tr><td class="mx-c">${c}</td>${row}<td class="mx-sum">${colors[c]}</td></tr>`;
    }).join('');
    let foot = '<td class="mx-c"></td>' + sizeKeys.map(s=>`<td class="mx-sum">${sizes[s]||0}</td>`).join('') + `<td class="mx-sum">${item.stock.total}</td>`;
    return `<div class="matrix-wrap"><table class="matrix"><thead><tr>${head}</tr></thead><tbody>${body}<tr class="mx-foot">${foot}</tr></tbody></table></div>`;
  }
  // Иначе — чипсы размеров
  const chips = sizeKeys.map(sz => {
    const q = sizes[sz];
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
  const healthBadge = item.health_v2 ? `<span class="health-badge">${item.health_v2}</span>` : '';
  const curDiscBadge = (item.cur_disc > 0) ? `<span class="curdisc-badge">в МС уже −${item.cur_disc}%</span>` : '';
  const catHtml = `<div><span class="cat-badge cat-${item.category}">${catLabel}</span>${healthBadge}${curDiscBadge}
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
    new_html = new_html.replace("'CLOTHING-001'", f"'{SUPABASE_ORDER_ID}'")

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
  try { localStorage.setItem(''' + f"'{LS_KEY}'" + ''', JSON.stringify(discounts)); } catch(e) {}
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
