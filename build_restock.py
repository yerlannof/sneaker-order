#!/usr/bin/env python3
"""
Дашборд «ЧТО ДОКУПИТЬ» — вещи + аксессуары (НЕ обувь).

Рейтинг: проверенные ходовые × мало осталось.
  - только прибыльные модели (маржа ≥ 30%) — убыточный зимний сток (куртки в минус) исключён
  - есть реальный спрос (продано ≥ 4 с начала года, последняя продажа ≤ 60 дн назад)
  - дозаказ нужен (WOS < 10 или распродано в ноль)
  - score = недельная скорость / (WOS+1); распроданные — наверх

Группировка по МОДЕЛИ (у одежды/аксессуаров артикул в названии, не в поле):
  «Сумка хобо плетёная (YF001) (Чёрный, OneSize)» → «Сумка хобо плетёная»

Запуск: .venv/bin/python sneaker-order/build_restock.py [--skip-photos]
Выход:  restock.html + restock_lite.json + restock_photos.json
URL:    https://yerlannof.github.io/sneaker-order/restock.html
"""
import duckdb
import json
import re
import sys
import os
import base64
import math
from datetime import date, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
DB_PATH = PROJECT_ROOT / 'data' / 'pnlpower.duckdb'
OUT_HTML = Path(__file__).parent / 'restock.html'
OUT_LITE = Path(__file__).parent / 'restock_lite.json'
OUT_PHOTOS = Path(__file__).parent / 'restock_photos.json'
PHOTO_CACHE = Path(__file__).parent / '.photo_cache_restock.json'

SNAPSHOT_DATE = '20260624'
TARGET_WEEKS = 8           # на сколько недель хотим покрытие при дозаказе
TOP_N = 30
MARGIN_MIN = 30            # % — отсекаем убыточный/низкомаржинальный сток
SOLD_MIN = 4              # минимум продаж YTD чтобы считать «проверенным»
RECENT_DAYS = 60          # последняя продажа не позже — иначе сезонный мёртвый
WOS_INCLUDE = 10          # включаем в дозаказ если запас < 10 недель или 0

# Сезонность (из CLAUDE.md) — лёгкая поправка скорости на месяц заказа
SEASON = {1: 0.59, 2: 0.79, 3: 1.35, 4: 1.26, 5: 1.00, 6: 0.99,
          7: 0.79, 8: 1.33, 9: 1.08, 10: 1.06, 11: 0.91, 12: 0.86}

SIZE_RE = re.compile(r',\s*(one\s?size|onesize|XXXL|XXL|XL|XS|3XL|2XL|[SML]|\d+(?:\.\d+)?)\s*$', re.I)
EXCL = ('пакет', 'zip lock', 'сертификат', 'доставка', 'подарочн', 'лимонад', 'red bull', 'обувь ')
ACCESSORY_KW = ('носк', 'очки', 'шапк', 'кепк', 'бейсболк', 'сумка', 'рюкзак', 'бандана',
                'ремень', 'перчат', 'визор', 'панам', 'носoк', 'чулк', 'браслет', 'цепочк')


def base_name(name: str) -> str:
    """Имя модели без цвета/размера/артикула-в-скобках."""
    if not name:
        return ''
    n = name.strip()
    if ' (' in n:                       # «… (YF001) (Чёрный, OneSize)»
        return n.split(' (')[0].strip()
    prev = None                          # «Носки 2500, one size», «GO SEX Black NK, M»
    while prev != n:
        prev = n
        n = SIZE_RE.sub('', n).strip()
    return n


def is_excluded(n: str) -> bool:
    nl = (n or '').lower()
    return any(k in nl for k in EXCL)


def classify(n: str) -> str:
    nl = (n or '').lower()
    return 'Аксессуары' if any(k in nl for k in ACCESSORY_KW) else 'Одежда'


def load_cache() -> dict:
    if PHOTO_CACHE.exists():
        try:
            return json.loads(PHOTO_CACHE.read_text())
        except Exception:
            return {}
    return {}


def save_cache(c: dict):
    PHOTO_CACHE.write_text(json.dumps(c, ensure_ascii=False))


def fetch_photo(base: str, headers: dict, base_url: str) -> str:
    """Ищет товар по имени модели, берёт мини-фото. '' если нет."""
    import requests
    try:
        r = requests.get(f"{base_url}/entity/product", headers=headers,
                         params={'filter': f'name~{base}', 'limit': 1}, timeout=5)
        if r.status_code != 200:
            return ''
        rows = r.json().get('rows', [])
        if not rows:
            return ''
        meta = rows[0].get('images', {}).get('meta', {})
        if not meta.get('href'):
            return ''
        r2 = requests.get(meta['href'], headers=headers, timeout=5)
        imgs = r2.json().get('rows', []) if r2.status_code == 200 else []
        if not imgs:
            return ''
        mini = imgs[0].get('miniature', {}).get('href') or imgs[0].get('tiny', {}).get('href')
        if not mini:
            return ''
        r3 = requests.get(mini, headers=headers, timeout=5)
        return base64.b64encode(r3.content).decode('ascii') if r3.status_code == 200 else ''
    except Exception:
        return ''


def main():
    today = date.today()
    d30 = today - timedelta(days=30)
    d90 = today - timedelta(days=90)
    d180 = today - timedelta(days=180)
    season = SEASON.get(today.month, 1.0)
    print(f"Дата: {today} | сезонность мес={season}")

    con = duckdb.connect(str(DB_PATH), read_only=True)

    # 1. Продажи YTD (вещи + аксессуары, не обувь)
    rows = con.execute("""
        SELECT product_name, quantity, revenue, profit, cogs, DATE(sale_datetime) d
        FROM v_sales_canonical
        WHERE year = 2026
          -- COALESCE: у аксессуаров/одежды article пустой → TRY_CAST=NULL,
          -- а NULL NOT BETWEEN = не TRUE и молча отсекает их. -1 = не обувь.
          AND COALESCE(TRY_CAST(article AS INTEGER), -1) NOT BETWEEN 200000 AND 209999
    """).fetchall()

    from collections import defaultdict
    agg = defaultdict(lambda: {'sold': 0.0, 'rev': 0.0, 'prof': 0.0, 'cogs_sum': 0.0,
                               's30': 0.0, 's90': 0.0, 's180': 0.0,
                               'first': None, 'last': None, 'variants': set()})
    for name, q, rev, prof, cogs, d in rows:
        if is_excluded(name):
            continue
        b = base_name(name)
        a = agg[b]
        a['sold'] += q; a['rev'] += rev; a['prof'] += prof; a['cogs_sum'] += (cogs or 0)
        if d >= d30: a['s30'] += q
        if d >= d90: a['s90'] += q
        if d >= d180: a['s180'] += q
        a['first'] = d if not a['first'] else min(a['first'], d)
        a['last'] = d if not a['last'] else max(a['last'], d)
        a['variants'].add(name)

    # 2. Остатки по складам (по модели)
    snap = f'inventory_snapshot_stores_{SNAPSHOT_DATE}'
    srows = con.execute(f"""
        SELECT product_name, SUM(moscow) ms, SUM(tsum) ts, SUM(online) on_, SUM(astana_aruzhan) ar,
               SUM(main_warehouse) wh, SUM(total_stock) tot
        FROM {snap}
        WHERE COALESCE(TRY_CAST(article AS INTEGER), -1) NOT BETWEEN 200000 AND 209999
        GROUP BY product_name
    """).fetchall()
    stock = defaultdict(lambda: {'ms': 0, 'tsum_online': 0, 'aruzhan': 0, 'wh': 0, 'total': 0})
    for name, ms, ts, on_, ar, wh, tot in srows:
        if not name or is_excluded(name):
            continue
        b = base_name(name)
        s = stock[b]
        s['ms'] += int(ms or 0); s['tsum_online'] += int((ts or 0) + (on_ or 0))
        s['aruzhan'] += int(ar or 0); s['wh'] += int(wh or 0); s['total'] += int(tot or 0)

    # 2b. РЕАЛЬНЫЙ закуп из supply_positions по имени модели.
    # КРИТИЧНО: v_sales_canonical.cogs join'ит себес по article, а у одежды/аксессуаров
    # артикул ПУСТОЙ → не находит и подставляет заглушку ~1000₸ → маржа завышена (87-95%).
    # Реальный закуп есть в поставках по названию (куртка ~5449, не 1000). См. clothing_clearance_dashboard.
    sup = con.execute("""SELECT product_name, price, quantity FROM supply_positions
                         WHERE product_name IS NOT NULL""").fetchall()
    cost_acc = defaultdict(list)
    supplied_qty = defaultdict(int)   # сколько ВСЕГО закуплено (справочно — с возвратами/списаниями не бьётся идеально)
    for nm, pr, qty in sup:
        b = base_name(nm)
        if pr and pr > 0:
            cost_acc[b].append(float(pr))
        supplied_qty[b] += int(qty or 0)
    cost_by_base = {b: sum(v) / len(v) for b, v in cost_acc.items()}

    # 3. Сборка кандидатов на дозаказ
    items = []
    no_cost = 0
    for b, a in agg.items():
        if a['rev'] <= 0:
            continue
        unit_price = a['rev'] / a['sold'] if a['sold'] else 0
        # себес: реальный закуп из поставок (по имени); fallback — cogs из продаж
        real_cost = cost_by_base.get(b)
        if real_cost is None:
            real_cost = a['cogs_sum'] / a['sold'] if a['sold'] else 0
            no_cost += 1
        unit_cost = real_cost
        real_profit = a['rev'] - unit_cost * a['sold']
        margin = real_profit / a['rev'] * 100 if a['rev'] else 0
        a = {**a, 'prof': real_profit}  # дальше profit = реальный
        st = stock.get(b, {'total': 0, 'ms': 0, 'tsum_online': 0, 'aruzhan': 0, 'wh': 0})
        total_stock = st['total']
        # недельная скорость — устойчивая (макс из окон), c сезонной поправкой
        weekly = max(a['s90'] / 13, a['s30'] / 4.3, a['s180'] / 26)
        weekly_adj = weekly * season
        wos = total_stock / weekly if weekly > 0 else 999
        need = max(0, math.ceil(weekly_adj * TARGET_WEEKS) - total_stock)
        days_no_sale = (today - a['last']).days if a['last'] else 999

        # фильтр «надо докупить»: прибыльное + проверенное + живое + реально не хватает
        # (распродано в ноль ИЛИ запаса не хватает на TARGET_WEEKS)
        if not (margin >= MARGIN_MIN and a['sold'] >= SOLD_MIN
                and days_no_sale <= RECENT_DAYS
                and (total_stock == 0 or need > 0)):
            continue

        # score: ходовое × мало осталось; распродано (wos=0) — наверх
        score = weekly / (wos + 1) if weekly > 0 else 0
        if total_stock == 0:
            score = weekly * 2  # распродано = максимальный приоритет

        urgency = ('SOLDOUT' if total_stock == 0 else
                   'URGENT' if wos < 4 else
                   'SOON' if wos < 8 else 'OK')

        items.append({
            'name': b,
            'cat': classify(b),
            'sold_ytd': round(a['sold']),
            's30': round(a['s30']), 's90': round(a['s90']), 's180': round(a['s180']),
            'revenue': round(a['rev']), 'profit': round(a['prof']),
            'margin': round(margin),
            'unit_price': round(unit_price), 'unit_cost': round(unit_cost),
            'stock': {'total': total_stock, 'ms': st['ms'],
                      'tsum_online': st['tsum_online'], 'aruzhan': st['aruzhan'], 'wh': st['wh']},
            'weekly': round(weekly, 1), 'wos': round(wos, 1) if wos < 999 else 999,
            'reorder_qty': need, 'urgency': urgency,
            'supplied': supplied_qty.get(b, 0),   # закуплено всего (справочно ≈)
            'colors': len(a['variants']),
            'first_sale': a['first'].isoformat() if a['first'] else None,
            'last_sale': a['last'].isoformat() if a['last'] else None,
            'days_no_sale': days_no_sale,
            'score': round(score, 3),
        })

    items.sort(key=lambda x: -x['score'])
    items = items[:TOP_N]
    print(f"Кандидатов на докупку (топ {TOP_N}): {len(items)}")

    # 4. Фото — ищем по имени модели (только для топа)
    cache = load_cache()
    if '--skip-photos' not in sys.argv and not os.environ.get('SKIP_PHOTOS'):
        from dotenv import load_dotenv
        load_dotenv(PROJECT_ROOT / '.env')
        token = os.getenv('MOYSKLAD_TOKEN')
        if token:
            headers = {'Authorization': f'Bearer {token}', 'Accept-Encoding': 'gzip'}
            base_url = 'https://api.moysklad.ru/api/remap/1.2'
            todo = [it['name'] for it in items if it['name'] not in cache]
            print(f"   Скачать фото: {len(todo)} (в кеше {len(cache)})")
            for i, nm in enumerate(todo, 1):
                cache[nm] = fetch_photo(nm, headers, base_url)
                if i % 10 == 0:
                    save_cache(cache); print(f"      {i}/{len(todo)}", flush=True)
            save_cache(cache)
    for it in items:
        it['photo'] = cache.get(it['name'], '')
    print(f"   С фото: {sum(1 for it in items if it['photo'])}/{len(items)}")

    # сводка
    tot_profit = sum(it['profit'] for it in items)
    tot_reorder = sum(it['reorder_qty'] for it in items)
    soldout = sum(1 for it in items if it['urgency'] == 'SOLDOUT')
    print(f"\n=== СВОДКА: топ-{len(items)} к докупке ===")
    print(f"   Прибыль этих моделей YTD: {tot_profit:,.0f} ₸")
    print(f"   Распродано в ноль: {soldout} | Суммарно докупить: ~{tot_reorder} шт")
    for it in items[:12]:
        u = {'SOLDOUT': '⚪РАСПРОД', 'URGENT': '🔴СРОЧНО', 'SOON': '🟡СКОРО', 'OK': '🟢'}[it['urgency']]
        print(f"   {u} {it['name'][:34]:<35} прод {it['sold_ytd']:>4} ост {it['stock']['total']:>3} "
              f"WOS {it['wos'] if it['wos']<999 else '∞':>4} марж {it['margin']:>3}% докупить {it['reorder_qty']}")

    render_html(items)
    return items


def render_html(items: list):
    today_s = date.today().strftime('%d.%m.%Y')
    data_json = json.dumps(items, ensure_ascii=False)
    n_cloth = sum(1 for it in items if it['cat'] == 'Одежда')
    n_acc = sum(1 for it in items if it['cat'] == 'Аксессуары')
    n_soldout = sum(1 for it in items if it['urgency'] == 'SOLDOUT')
    n_urgent = sum(1 for it in items if it['urgency'] == 'URGENT')
    tot_profit = sum(it['profit'] for it in items)
    tot_reorder = sum(it['reorder_qty'] for it in items)

    html = '''<!DOCTYPE html>
<html lang="ru"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>Что докупить — вещи и аксессуары</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--border:#e5e7eb;--text:#1a1d23;--text2:#525866;--text3:#9aa1ad;--blue:#3b82f6}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--text);font-size:14px;-webkit-font-smoothing:antialiased}
.header{background:linear-gradient(135deg,#111827,#1f2937);color:#fff;padding:16px 18px;position:sticky;top:0;z-index:50}
.header h1{font-size:19px;font-weight:800;display:flex;align-items:center;gap:8px}
.header-meta{font-size:12px;color:#9ca3af;margin-top:3px}
.stats{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;padding:12px 14px;background:#0b1220}
.stat{background:#111a2e;border:1px solid #1f2a44;border-radius:10px;padding:9px 6px;text-align:center}
.stat-num{font-size:18px;font-weight:800;color:#fff}
.stat-num.red{color:#f87171}.stat-num.amber{color:#fbbf24}.stat-num.green{color:#34d399}.stat-num.blue{color:#60a5fa}
.stat-label{font-size:10px;color:#8b93a7;margin-top:2px;text-transform:uppercase;letter-spacing:.4px}
.controls{position:sticky;top:0;background:var(--bg);padding:10px 14px;border-bottom:1px solid var(--border);z-index:40}
.search{width:100%;padding:10px 12px;border:1px solid var(--border);border-radius:9px;font-size:14px;margin-bottom:8px}
.filters{display:flex;gap:6px;flex-wrap:wrap}
.fbtn{padding:6px 12px;border:1px solid var(--border);background:#fff;border-radius:20px;font-size:12px;font-weight:600;cursor:pointer;white-space:nowrap}
.fbtn.active{background:var(--text);color:#fff;border-color:var(--text)}
.sortbar{display:flex;gap:6px;align-items:center;margin-top:8px;font-size:12px;color:var(--text2)}
.sortbar select{padding:5px 8px;border:1px solid var(--border);border-radius:7px;font-size:12px}
.items{padding:12px 14px;display:flex;flex-direction:column;gap:10px;max-width:760px;margin:0 auto}
.card{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:12px;display:flex;gap:12px;box-shadow:0 1px 2px rgba(0,0,0,.04)}
.rank{position:absolute;margin-top:-4px;margin-left:-4px;background:var(--text);color:#fff;font-size:11px;font-weight:800;width:22px;height:22px;border-radius:50%;display:flex;align-items:center;justify-content:center}
.photo{width:82px;height:82px;border-radius:10px;object-fit:cover;background:#f1f3f5;flex-shrink:0}
.photo-empty{width:82px;height:82px;border-radius:10px;background:#f1f3f5;display:flex;align-items:center;justify-content:center;color:var(--text3);font-size:10px;text-align:center;flex-shrink:0}
.body{flex:1;min-width:0}
.name{font-weight:700;font-size:15px;line-height:1.25;margin-bottom:4px}
.badges{display:flex;gap:5px;flex-wrap:wrap;margin-bottom:8px;align-items:center}
.badge{padding:3px 8px;border-radius:6px;font-size:10px;font-weight:800;letter-spacing:.3px}
.b-SOLDOUT{background:#1f2937;color:#fff}.b-URGENT{background:#fef2f2;color:#dc2626}
.b-SOON{background:#fffbeb;color:#d97706}.b-OK{background:#ecfdf5;color:#059669}
.b-cat{background:#eef2ff;color:#4f46e5}
.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin-bottom:8px}
.m{background:#fafbfc;border:1px solid var(--border);border-radius:8px;padding:6px 4px;text-align:center}
.m-lab{font-size:9px;color:var(--text3);text-transform:uppercase;letter-spacing:.3px}
.m-val{font-size:15px;font-weight:800;margin-top:1px}
.m-sub{font-size:9px;color:var(--text3)}
.reorder{background:#eff6ff;border:1px dashed #60a5fa;border-radius:8px;padding:7px 10px;font-size:13px;font-weight:700;color:#1d4ed8;display:flex;justify-content:space-between;align-items:center}
.reorder.zero{background:#f9fafb;border-color:var(--border);color:var(--text3)}
.storeline{font-size:11px;color:var(--text2);margin-bottom:6px}
.storeline b{color:var(--text)}
.flowline,.priceline{font-size:12px;color:var(--text2);margin-bottom:6px;background:#fafbfc;border:1px solid var(--border);border-radius:7px;padding:5px 8px}
.flowline b,.priceline b{color:var(--text)}
.flowline .st{color:var(--text3);font-size:11px}
.mu{display:inline-block;background:#1e293b;color:#fff;font-weight:800;font-size:11px;padding:1px 7px;border-radius:10px;margin:0 3px}
.wos-bad{color:#dc2626}.wos-warn{color:#d97706}.wos-ok{color:#059669}
.empty{text-align:center;padding:40px;color:var(--text3)}
@media(max-width:600px){.stats{grid-template-columns:repeat(3,1fr)}.photo,.photo-empty{width:66px;height:66px}.m-val{font-size:14px}}
</style></head>
<body>
<div class="header">
  <h1>🛒 Что докупить <span style="font-size:12px;font-weight:600;color:#9ca3af">вещи + аксессуары</span></h1>
  <div class="header-meta">Данные ''' + today_s + ''' · с начала года · топ-''' + str(len(items)) + ''' проверенных ходовых, что заканчиваются · маржа ≥''' + str(MARGIN_MIN) + '''%</div>
</div>
<div class="stats">
  <div class="stat"><div class="stat-num">''' + str(len(items)) + '''</div><div class="stat-label">моделей</div></div>
  <div class="stat"><div class="stat-num red">''' + str(n_soldout) + '''</div><div class="stat-label">распродано</div></div>
  <div class="stat"><div class="stat-num amber">''' + str(n_urgent) + '''</div><div class="stat-label">срочно</div></div>
  <div class="stat"><div class="stat-num blue">~''' + str(tot_reorder) + '''</div><div class="stat-label">докупить шт</div></div>
  <div class="stat"><div class="stat-num green">''' + (f'{tot_profit/1e6:.1f}М' if tot_profit >= 1e6 else f'{round(tot_profit/1000)}K') + '''</div><div class="stat-label">приб YTD</div></div>
</div>
<div class="controls">
  <input class="search" id="search" placeholder="🔍 поиск по названию…" oninput="render()">
  <div class="filters" id="filters">
    <button class="fbtn active" data-f="all" onclick="setF(this)">Все (''' + str(len(items)) + ''')</button>
    <button class="fbtn" data-f="cat:Одежда" onclick="setF(this)">👕 Одежда (''' + str(n_cloth) + ''')</button>
    <button class="fbtn" data-f="cat:Аксессуары" onclick="setF(this)">🎒 Аксессуары (''' + str(n_acc) + ''')</button>
    <button class="fbtn" data-f="u:SOLDOUT" onclick="setF(this)">⚪ Распродано (''' + str(n_soldout) + ''')</button>
    <button class="fbtn" data-f="u:URGENT" onclick="setF(this)">🔴 Срочно (''' + str(n_urgent) + ''')</button>
  </div>
  <div class="sortbar">Сортировка:
    <select id="sort" onchange="render()">
      <option value="score">Приоритет докупки</option>
      <option value="sold_ytd">Продано (YTD)</option>
      <option value="s30">Скорость 30д</option>
      <option value="profit">Прибыль</option>
      <option value="wos">Меньше остатка (WOS)</option>
      <option value="margin">Маржа</option>
    </select>
  </div>
</div>
<div class="items" id="list"></div>
<script>
const DATA = ''' + data_json + ''';
let curF = 'all';
function setF(btn){document.querySelectorAll('.fbtn').forEach(b=>b.classList.remove('active'));btn.classList.add('active');curF=btn.dataset.f;render();}
function fmt(n){return (n||0).toLocaleString('ru-RU');}
function fmtK(n){return n>=1e6?(n/1e6).toFixed(1)+'М':n>=1000?Math.round(n/1000)+'K':String(Math.round(n));}
const U={SOLDOUT:['⚪ РАСПРОДАНО','b-SOLDOUT'],URGENT:['🔴 СРОЧНО','b-URGENT'],SOON:['🟡 СКОРО','b-SOON'],OK:['🟢 ЕСТЬ ЗАПАС','b-OK']};
function card(it,i){
  const [ul,uc]=U[it.urgency];
  const wosCls=it.wos<4?'wos-bad':it.wos<8?'wos-warn':'wos-ok';
  const wosTxt=it.stock.total===0?'0 (нет)':it.wos>=999?'∞':it.wos+' нед';
  const ph=it.photo?`<img class="photo" src="data:image/jpeg;base64,${it.photo}">`:`<div class="photo-empty">нет фото</div>`;
  const ro=it.reorder_qty>0?`<div class="reorder"><span>📦 Докупить</span><span>~${it.reorder_qty} шт <span style="font-weight:500;color:#60a5fa">(до ${'''+str(TARGET_WEEKS)+'''} нед)</span></span></div>`
    :`<div class="reorder zero"><span>📦 Дозаказ</span><span>запаса хватает</span></div>`;
  const st=it.stock;
  const storeParts=[];
  if(st.ms)storeParts.push(`Мск <b>${st.ms}</b>`);
  if(st.tsum_online)storeParts.push(`ЦУМ+Онл <b>${st.tsum_online}</b>`);
  if(st.aruzhan)storeParts.push(`Аружан <b>${st.aruzhan}</b>`);
  if(st.wh)storeParts.push(`склад <b>${st.wh}</b>`);
  const storeLine=storeParts.length?`<div class="storeline">📍 ${storeParts.join(' · ')}</div>`:`<div class="storeline" style="color:#dc2626">📍 нет на складах</div>`;
  const sellThrough=it.supplied>0?Math.min(100,Math.round(it.sold_ytd/it.supplied*100)):0;
  const flowLine=`<div class="flowline">📦 Закуплено <b>≈${it.supplied}</b> → продано <b>${it.sold_ytd}</b> → осталось <b style="${it.stock.total===0?'color:#dc2626':''}">${it.stock.total}</b>${it.supplied>0?` <span class="st">(продано ${sellThrough}%)</span>`:''}</div>`;
  const markup=it.unit_cost>0?(it.unit_price/it.unit_cost):0;
  const muTxt=markup>0?('×'+markup.toFixed(1).replace('.',',')):'—';
  const priceLine=`<div class="priceline">💰 Себес <b>${fmt(it.unit_cost)}₸</b> <span class="mu">${muTxt}</span> РЦ <b>${fmt(it.unit_price)}₸</b> · маржа <b style="color:#059669">${it.margin}%</b> = <b>${fmt(it.unit_price-it.unit_cost)}₸</b>/шт</div>`;
  return `<div style="position:relative"><span class="rank">${i+1}</span>
  <div class="card">${ph}
    <div class="body">
      <div class="name">${it.name}</div>
      <div class="badges"><span class="badge ${uc}">${ul}</span><span class="badge b-cat">${it.cat}</span>${it.colors>1?`<span style="font-size:11px;color:#9aa1ad">${it.colors} цв/вар</span>`:''}</div>
      ${storeLine}
      ${flowLine}
      ${priceLine}
      <div class="metrics">
        <div class="m"><div class="m-lab">Продано год</div><div class="m-val">${it.sold_ytd}</div><div class="m-sub">30д: ${it.s30}</div></div>
        <div class="m"><div class="m-lab">Остаток</div><div class="m-val" style="${it.stock.total===0?'color:#dc2626':''}">${it.stock.total}</div><div class="m-sub ${wosCls}">${wosTxt}</div></div>
        <div class="m"><div class="m-lab">Маржа</div><div class="m-val" style="color:#059669">${it.margin}%</div><div class="m-sub">${fmt(it.unit_cost)}→${fmt(it.unit_price)}₸</div></div>
        <div class="m"><div class="m-lab">Поймали маржи</div><div class="m-val">${fmtK(it.profit)}</div><div class="m-sub">за год ₸</div></div>
      </div>
      ${ro}
    </div></div></div>`;
}
function render(){
  const q=document.getElementById('search').value.toLowerCase();
  const sort=document.getElementById('sort').value;
  let arr=DATA.filter(it=>{
    if(curF.startsWith('cat:')&&it.cat!==curF.slice(4))return false;
    if(curF.startsWith('u:')&&it.urgency!==curF.slice(2))return false;
    if(q&&!it.name.toLowerCase().includes(q))return false;
    return true;
  });
  arr.sort((a,b)=> sort==='wos'? (a.wos-b.wos) : ((b[sort]||0)-(a[sort]||0)));
  const el=document.getElementById('list');
  el.innerHTML=arr.length?arr.map((it,i)=>card(it,i)).join(''):'<div class="empty">Ничего не найдено</div>';
}
render();
</script>
</body></html>'''

    OUT_HTML.write_text(html, encoding='utf-8')
    # отдельные json для верификации/повторного использования
    OUT_LITE.write_text(json.dumps([{k: v for k, v in it.items() if k != 'photo'} for it in items],
                                   ensure_ascii=False, indent=2))
    OUT_PHOTOS.write_text(json.dumps({it['name']: it['photo'] for it in items if it['photo']}, ensure_ascii=False))
    print(f"\n✓ HTML: {OUT_HTML} ({OUT_HTML.stat().st_size/1024:.0f} KB)")


if __name__ == '__main__':
    main()
