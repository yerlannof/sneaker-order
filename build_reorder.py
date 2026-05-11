#!/usr/bin/env python3
"""
Per-size дозаказ — UI идентичен sneakers.html (привычный пользователю стиль).
Размеры с +/- кнопками заменяют кнопки скидок. Подсветка кратности 12.

Запуск: python3 sneaker-order/build_reorder.py
"""

import json
import re
import sys
from pathlib import Path
from datetime import date, timedelta

import duckdb

PROJECT_ROOT = Path(__file__).parent.parent
DB_PATH = PROJECT_ROOT / "data" / "pnlpower.duckdb"
OUT_HTML = Path(__file__).parent / "reorder.html"
PHOTO_CACHE = Path(__file__).parent / ".photo_cache_sneakers.json"

SEASON = {1: 0.59, 2: 0.79, 3: 1.35, 4: 1.26, 5: 1.00, 6: 0.99,
          7: 0.79, 8: 1.33, 9: 1.08, 10: 1.06, 11: 0.91, 12: 0.86}


def latest_snapshot(con):
    return con.execute("""SELECT table_name FROM information_schema.tables
        WHERE table_name LIKE 'inventory_snapshot_stores_%'
        ORDER BY table_name DESC LIMIT 1""").fetchone()[0]


def main():
    today = date.today()
    season_coef = SEASON[today.month]

    con = duckdb.connect(str(DB_PATH), read_only=True)
    snap = latest_snapshot(con)
    print(f"Снапшот: {snap}")

    # Snapshot 6 мес назад для истории
    target_180 = (today - timedelta(days=180)).strftime('%Y%m%d')
    snap_tabs = con.execute("""SELECT table_name FROM information_schema.tables
        WHERE table_name LIKE 'inventory_snapshot_stores_%' ORDER BY table_name""").fetchall()
    snap_6mo = min([(t[0][-8:], t[0]) for t in snap_tabs],
                   key=lambda x: abs(int(x[0]) - int(target_180)))
    snap_6mo_date = f"{snap_6mo[0][:4]}-{snap_6mo[0][4:6]}-{snap_6mo[0][6:]}"
    print(f"Снапшот 6 мес назад: {snap_6mo[1]}")

    # Main query
    print("Собираю данные...")
    base_rows = con.execute(f"""
WITH s35 AS (
    SELECT article,
        ANY_VALUE(REGEXP_REPLACE(product_name, ',\\s*\\d+(\\.\\d+)?$', '')) AS model,
        SUM(quantity) AS qty_35d,
        ROUND(SUM(quantity)/5.0 * {season_coef}, 1) AS adj_rate,
        ROUND(AVG(CASE WHEN price>0 THEN price END)) AS avg_price
    FROM retaildemand_positions
    WHERE document_moment >= CURRENT_DATE - INTERVAL 35 DAY AND price > 0
      AND TRY_CAST(article AS INTEGER) BETWEEN 200000 AND 209999
    GROUP BY article HAVING SUM(quantity) >= 5
),
sp AS (
    SELECT article,
        SUM(quantity) FILTER (WHERE document_moment >= CURRENT_DATE - INTERVAL 30 DAY AND price>0) AS s30,
        SUM(quantity) FILTER (WHERE document_moment >= CURRENT_DATE - INTERVAL 90 DAY AND price>0) AS s90,
        SUM(quantity) FILTER (WHERE document_moment >= CURRENT_DATE - INTERVAL 180 DAY AND price>0) AS s180,
        SUM(quantity) FILTER (WHERE document_moment >= CURRENT_DATE - INTERVAL 365 DAY AND price>0) AS s365,
        SUM(quantity) FILTER (WHERE price>0) AS sall,
        MAX(DATE(document_moment)) AS last_sale_d
    FROM retaildemand_positions GROUP BY article
),
fs AS (
    SELECT product_article AS article, MIN(DATE(supply_moment)) AS first_d, SUM(quantity) AS first_qty
    FROM supply_positions WHERE product_article IS NOT NULL GROUP BY product_article
),
st6 AS (
    SELECT article, SUM(total_stock) AS s6
    FROM {snap_6mo[1]} WHERE TRY_CAST(article AS INTEGER) BETWEEN 200000 AND 209999
    GROUP BY article
),
stk AS (
    SELECT article,
        SUM(total_stock) AS total, SUM(moscow) AS msk,
        SUM(tsum) AS ts, SUM(online) AS onl,
        SUM(astana_aruzhan) AS ar, SUM(main_warehouse) AS wh
    FROM {snap} WHERE TRY_CAST(article AS INTEGER) BETWEEN 200000 AND 209999
    GROUP BY article
),
buy AS (
    SELECT product_article AS article, LAST(price ORDER BY supply_moment) AS bp
    FROM supply_positions WHERE agent_name='Поставщик In' AND supply_moment>='2025-01-01'
    GROUP BY product_article
)
SELECT s35.article, s35.model, s35.qty_35d, s35.adj_rate, s35.avg_price,
    COALESCE(stk.total,0), COALESCE(stk.msk,0),
    COALESCE(stk.ts,0)+COALESCE(stk.onl,0) AS tsum_onl,
    COALESCE(stk.ar,0), COALESCE(stk.wh,0),
    CASE WHEN s35.adj_rate>0 THEN ROUND(COALESCE(stk.total,0)/s35.adj_rate,1) ELSE 999 END AS wos,
    COALESCE(buy.bp, 0) AS buy_price,
    COALESCE(sp.s30,0), COALESCE(sp.s90,0), COALESCE(sp.s180,0),
    COALESCE(sp.s365,0), COALESCE(sp.sall,0), sp.last_sale_d,
    fs.first_d, COALESCE(fs.first_qty, 0), COALESCE(st6.s6, 0)
FROM s35
LEFT JOIN stk ON s35.article=stk.article
LEFT JOIN buy ON s35.article=buy.article
LEFT JOIN sp ON s35.article=sp.article
LEFT JOIN fs ON s35.article=fs.article
LEFT JOIN st6 ON s35.article=st6.article
WHERE CASE WHEN s35.adj_rate>0 THEN COALESCE(stk.total,0)/s35.adj_rate ELSE 999 END < 10
ORDER BY s35.adj_rate DESC
""").fetchall()

    print(f"  Найдено: {len(base_rows)} моделей")

    # Размеры
    items = []
    for r in base_rows:
        (article, model, qty_35d, adj_rate, avg_price, total, msk, tsum_onl,
         ar, wh, wos, buy_price, s30, s90, s180, s365, sall, last_sale_d,
         first_d, first_qty, stock_6mo) = r

        # Stock by size (with breakdown by store)
        size_rows = con.execute(f"""
            SELECT REGEXP_EXTRACT(product_name, ',\\s*(\\d+\\.?\\d*)$', 1) AS sz,
                SUM(moscow) AS m, SUM(tsum) AS t, SUM(online) AS o,
                SUM(astana_aruzhan) AS a, SUM(main_warehouse) AS w,
                SUM(total_stock) AS tot
            FROM {snap}
            WHERE article = '{article}' AND total_stock > 0
            GROUP BY 1 ORDER BY 1
        """).fetchall()

        # Sold by size (60d)
        sold_rows = con.execute(f"""
            SELECT REGEXP_EXTRACT(product_name, ',\\s*(\\d+\\.?\\d*)$', 1) AS sz,
                CAST(SUM(quantity) AS INT)
            FROM retaildemand_positions
            WHERE article = '{article}' AND price>0
              AND document_moment >= CURRENT_DATE - INTERVAL 60 DAY
            GROUP BY 1
        """).fetchall()
        sold_map = {s: q for s, q in sold_rows if s}

        sizes = []
        for sr in size_rows:
            sz = sr[0]
            if not sz: continue
            sizes.append({
                'size': sz, 'stock': int(sr[6] or 0),
                'msk': int(sr[1] or 0), 'tsum': int((sr[2] or 0) + (sr[3] or 0)),
                'aruzhan': int(sr[4] or 0), 'warehouse': int(sr[5] or 0),
                'sold_60d': sold_map.get(sz, 0),
            })
        # Sizes without stock but with sales — добавить (нужно везти!)
        existing = {s['size'] for s in sizes}
        for sz, q in sold_map.items():
            if sz not in existing:
                sizes.append({'size': sz, 'stock': 0, 'msk': 0, 'tsum': 0,
                              'aruzhan': 0, 'warehouse': 0, 'sold_60d': q})
        try:
            sizes.sort(key=lambda x: float(x['size']))
        except (ValueError, TypeError):
            sizes.sort(key=lambda x: x['size'])

        size_nums = [float(s['size']) for s in sizes if s['size']]
        if size_nums:
            if max(size_nums) <= 40 and min(size_nums) >= 35: gender = 'Ж'
            elif min(size_nums) >= 40: gender = 'М'
            else: gender = 'У'
        else:
            gender = '?'

        target = max(0, round(float(adj_rate) * 8 - int(total)))
        days_no_sale = (today - last_sale_d).days if last_sale_d else None

        items.append({
            'article': str(article), 'model': model or '', 'gender': gender,
            'qty_35d': int(qty_35d), 'adj_rate': float(adj_rate),
            'avg_price': int(avg_price or 0), 'buy_price': int(buy_price or 0),
            'stock': {'total': int(total), 'msk': int(msk), 'tsum_online': int(tsum_onl),
                      'aruzhan': int(ar), 'warehouse': int(wh)},
            'wos': float(wos), 'sizes': sizes, 'target_total': target,
            'sales': {'s30': int(s30), 's90': int(s90), 's180': int(s180),
                      's365': int(s365), 'sall': int(sall)},
            'last_sale': last_sale_d.isoformat() if last_sale_d else None,
            'days_no_sale': days_no_sale,
            'history': {
                'first_supply_date': first_d.isoformat() if first_d else None,
                'first_supply_qty': int(first_qty),
                'snap_6mo_date': snap_6mo_date,
                'stock_6mo': int(stock_6mo),
            },
        })

    # Фото из кэша
    cache = {}
    if PHOTO_CACHE.exists():
        cache = json.loads(PHOTO_CACHE.read_text())
    for it in items:
        it['photo'] = cache.get(it['article'], '')
    print(f"  С фото: {sum(1 for i in items if i['photo'])}/{len(items)}")

    # Sort by WOS
    items.sort(key=lambda x: (x['wos'], -x['adj_rate']))

    con.close()

    # Сводка
    n_critical = sum(1 for i in items if i['wos'] < 3)
    n_soon = sum(1 for i in items if 3 <= i['wos'] < 6)
    n_nice = sum(1 for i in items if 6 <= i['wos'] < 10)
    total_target = sum(i['target_total'] for i in items)
    print(f"\n  Срочных (WOS<3): {n_critical}, скоро (3-6): {n_soon}, норм (6-10): {n_nice}")
    print(f"  Целевой объём: {total_target} пар\n")

    write_html(items)
    print(f"✓ HTML: {OUT_HTML}")


def write_html(items):
    items_json = json.dumps(items, ensure_ascii=False)
    html = TEMPLATE.replace('__ITEMS_JSON__', items_json)
    OUT_HTML.write_text(html, encoding='utf-8')


TEMPLATE = '''<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>Дозаказ кроссовок</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #f8f9fb; --card: #fff; --border: #e8ecf1;
  --text: #1a1d23; --text2: #6b7280; --text3: #9ca3af;
  --blue: #3b82f6; --blue-light: #eff6ff;
  --green: #10b981; --green-light: #ecfdf5;
  --red: #ef4444; --red-light: #fef2f2;
  --orange: #f59e0b; --orange-light: #fffbeb;
  --purple: #8b5cf6;
  --radius: 12px; --radius-sm: 8px;
}
* { margin:0; padding:0; box-sizing:border-box; }
html, body { overflow-x:hidden; }
body { font-family:'Inter',system-ui,sans-serif; background:var(--bg); color:var(--text);
  -webkit-font-smoothing:antialiased; padding-bottom:80px; }

.header { background:var(--card); border-bottom:1px solid var(--border); padding:16px 20px;
  position:sticky; top:0; z-index:100; }
.header h1 { font-size:20px; font-weight:800; letter-spacing:-0.5px; }
.header-meta { font-size:12px; color:var(--text3); margin-top:4px; }

.stats { display:grid; grid-template-columns:repeat(4,1fr); gap:8px; padding:12px 16px;
  position:sticky; top:62px; z-index:98; background:var(--bg); }
.stat { background:var(--card); border-radius:var(--radius-sm); padding:10px; text-align:center; border:1px solid var(--border); }
.stat-num { font-size:18px; font-weight:800; letter-spacing:-0.5px; }
.stat-label { font-size:9px; font-weight:600; color:var(--text3); text-transform:uppercase; letter-spacing:0.5px; margin-top:2px; }
.stat-num.green { color:var(--green); }
.stat-num.blue { color:var(--blue); }
.stat-num.orange { color:var(--orange); }

.filter-bar { padding:8px 16px; display:flex; gap:6px; overflow-x:auto;
  -webkit-overflow-scrolling:touch; scrollbar-width:none; background:var(--bg);
  position:sticky; top:170px; z-index:96; }
.filter-bar::-webkit-scrollbar { display:none; }
.filter-btn { padding:6px 12px; border-radius:20px; border:1px solid var(--border);
  background:var(--card); font-size:12px; font-weight:600; cursor:pointer; white-space:nowrap; font-family:inherit; }
.filter-btn.active { background:var(--text); color:#fff; border-color:var(--text); }

.items { padding:0 16px; }
.item { background:var(--card); border:1px solid var(--border); border-radius:var(--radius); margin-bottom:8px; overflow:hidden; }
.item-main { display:flex; gap:12px; padding:12px; }
.item-photo { width:80px; height:80px; border-radius:var(--radius-sm); object-fit:cover; background:var(--bg); flex-shrink:0; cursor:pointer; }
.item-photo-empty { width:80px; height:80px; border-radius:var(--radius-sm); background:var(--bg); flex-shrink:0; display:flex; flex-direction:column; align-items:center; justify-content:center; font-size:10px; color:var(--text3); text-align:center; }
.item-body { flex:1; min-width:0; }
.item-name { font-weight:600; font-size:13px; line-height:1.3; margin-bottom:2px; }
.item-article { font-size:11px; color:var(--text3); margin-bottom:4px; }
.brand-badge, .gender-badge, .wos-badge { padding:2px 7px; border-radius:6px; font-size:10px; font-weight:700; margin-right:4px; }
.gender-Ж { background:#fce7f3; color:#be185d; }
.gender-М { background:#dbeafe; color:#1d4ed8; }
.gender-У { background:#e0e7ff; color:#5b21b6; }
.wos-critical { background:var(--red-light); color:var(--red); }
.wos-soon { background:var(--orange-light); color:var(--orange); }
.wos-nice { background:var(--green-light); color:var(--green); }

.item-right { display:flex; flex-direction:column; align-items:flex-end; gap:4px; flex-shrink:0; }
.stock-total { font-size:18px; font-weight:800; color:var(--blue); }

.item-details { padding:0 12px 10px; }
.flow-box { display:grid; grid-template-columns:repeat(3,1fr); gap:6px; margin-bottom:8px;
  padding:8px; background:#fafbfc; border-radius:8px; border:1px solid var(--border); }
.flow-cell { text-align:center; }
.flow-label { font-size:9px; font-weight:600; color:var(--text3); text-transform:uppercase; }
.flow-val { font-size:18px; font-weight:800; margin:2px 0; }
.flow-sub { font-size:10px; color:var(--text3); }

.velocity-box { display:flex; gap:10px; padding:8px 10px; background:var(--bg);
  border-radius:6px; margin-bottom:8px; font-size:12px; align-items:center; flex-wrap:wrap; }
.velocity-box b { color:var(--text); }
.velocity-box.history { background:#f1f5f9; }

.stock-grid { display:grid; grid-template-columns:repeat(4,1fr); gap:4px; margin-bottom:8px; }
.stock-cell { text-align:center; padding:4px; background:var(--bg); border-radius:6px; }
.stock-cell-label { font-size:9px; font-weight:600; color:var(--text3); text-transform:uppercase; }
.stock-cell-val { font-size:14px; font-weight:700; }
.stock-cell-val.zero { color:var(--text3); }

.price-row { display:flex; gap:12px; align-items:center; flex-wrap:wrap; margin-bottom:8px; }
.price-item { font-size:12px; }
.price-item .label { color:var(--text3); font-size:10px; display:block; }
.price-item .val { font-weight:700; }

/* SIZE GRID — главное отличие от sneakers.html */
.size-grid { display:flex; gap:4px; overflow-x:auto; padding:4px 0; -webkit-overflow-scrolling:touch;
  scrollbar-width:none; margin-bottom:8px; }
.size-grid::-webkit-scrollbar { display:none; }
.size-cell { flex:0 0 auto; min-width:80px; padding:6px 4px; background:#fafafa;
  border:1px solid var(--border); border-radius:8px; text-align:center; }
.size-num { font-size:16px; font-weight:800; }
.size-info { font-size:9px; color:var(--text3); line-height:1.3; margin:2px 0; }
.size-info b { color:var(--text2); }
.size-row { display:flex; gap:3px; align-items:center; margin-top:4px; }
.sz-btn { width:24px; height:24px; border-radius:5px; border:1px solid var(--border);
  background:#fff; font-size:14px; font-weight:700; cursor:pointer;
  -webkit-tap-highlight-color:transparent; display:flex; align-items:center; justify-content:center; }
.sz-btn:active { transform:scale(0.9); }
.sz-btn.plus { color:var(--green); }
.sz-btn.minus { color:var(--red); }
.sz-qty { flex:1; text-align:center; font-size:15px; font-weight:800; min-width:24px; }
.sz-qty.zero { color:var(--text3); }
.sz-qty.has { color:var(--green); }

.total-bar { padding:8px 12px; background:var(--bg); border-radius:var(--radius-sm);
  display:flex; gap:12px; align-items:center; flex-wrap:wrap; margin-top:4px; font-size:12px; }
.total-bar.warn { background:#fffbeb; border:1px dashed #d97706; }
.total-bar.ok { background:#ecfdf5; border:1px dashed #6ee7b7; }
.total-amount { font-size:16px; font-weight:800; }
.total-amount.warn { color:var(--orange); }
.total-amount.ok { color:var(--green); }
.total-hint { color:var(--text2); font-size:11px; }

.bottom { position:fixed; bottom:0; left:0; right:0; background:var(--card);
  border-top:1px solid var(--border); padding:12px 16px; display:flex;
  justify-content:space-between; align-items:center; gap:8px; z-index:100;
  box-shadow:0 -4px 12px rgba(0,0,0,0.08); }
.bottom-info { font-size:12px; color:var(--text2); }
.bottom-info b { font-size:14px; color:var(--text); }

.btn { padding:8px 14px; border-radius:8px; border:1px solid var(--border); background:var(--card);
  font-size:13px; font-weight:600; cursor:pointer; font-family:inherit; }
.btn-green { background:var(--green); color:#fff; border-color:var(--green); }
.btn-outline { background:transparent; }

.toast { position:fixed; bottom:80px; left:50%; transform:translateX(-50%);
  background:var(--text); color:#fff; padding:10px 18px; border-radius:24px;
  font-size:13px; font-weight:600; opacity:0; transition:opacity 0.3s;
  pointer-events:none; z-index:200; }
.toast.show { opacity:1; }

.lightbox { display:none; position:fixed; inset:0; background:rgba(0,0,0,0.92); z-index:300;
  align-items:center; justify-content:center; padding:20px; }
.lightbox.active { display:flex; }
.lightbox img { max-width:100%; max-height:100%; }
.lb-close { position:absolute; top:16px; right:20px; color:#fff; font-size:32px; cursor:pointer; }
</style>
</head>
<body>

<div class="header">
  <h1>🛒 Дозаказ кроссовок</h1>
  <div class="header-meta" id="header-meta">Загрузка...</div>
</div>

<div class="stats">
  <div class="stat"><div class="stat-num" id="s-models">0</div><div class="stat-label">Моделей</div></div>
  <div class="stat"><div class="stat-num orange" id="s-target">0</div><div class="stat-label">Цель пар</div></div>
  <div class="stat"><div class="stat-num blue" id="s-pairs">0</div><div class="stat-label">В заказе</div></div>
  <div class="stat"><div class="stat-num green" id="s-sum">0</div><div class="stat-label">Сумма ₸</div></div>
</div>

<div class="filter-bar">
  <button class="filter-btn active" onclick="setFilter('all', event)">Все</button>
  <button class="filter-btn" onclick="setFilter('critical', event)">🔴 WOS&lt;3</button>
  <button class="filter-btn" onclick="setFilter('soon', event)">🟠 WOS 3-6</button>
  <button class="filter-btn" onclick="setFilter('nice', event)">🟢 WOS 6-10</button>
  <button class="filter-btn" onclick="setFilter('ordered', event)">📝 В заказе</button>
  <button class="filter-btn" onclick="setFilter('men', event)">М</button>
  <button class="filter-btn" onclick="setFilter('women', event)">Ж</button>
</div>

<div class="items" id="items"></div>

<div class="bottom">
  <div class="bottom-info">
    <b id="b-models">0</b> моделей | <b id="b-pairs">0</b> пар | <b id="b-sum">0</b>₸
  </div>
  <div style="display:flex; gap:6px;">
    <button class="btn btn-outline" onclick="resetAll()" title="Сбросить все">🔄</button>
    <button class="btn btn-outline" onclick="exportJSON()">JSON</button>
    <button class="btn btn-green" onclick="exportSupplier()">Поставщику</button>
  </div>
</div>

<div class="toast" id="toast"></div>
<div class="lightbox" id="lightbox" onclick="closeLightbox()">
  <div class="lb-close">×</div>
  <img id="lb-img" src="">
</div>

<script>
const ITEMS = __ITEMS_JSON__;
let order = {};  // {article: {size: qty}}
let filter = 'all';

try {
  const saved = localStorage.getItem('sneaker_reorder_v1');
  if (saved) order = JSON.parse(saved);
} catch(e) {}

function save() { try { localStorage.setItem('sneaker_reorder_v1', JSON.stringify(order)); } catch(e) {} }
function fmt(n) { return (n||0).toLocaleString('ru-RU'); }
function fmtK(n) { return n >= 1000000 ? (n/1000000).toFixed(1)+'M' : n >= 1000 ? Math.round(n/1000)+'K' : Math.round(n); }
function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2000);
}
function openLightbox(art) {
  const it = ITEMS.find(i => i.article === art);
  if (!it || !it.photo) return;
  document.getElementById('lb-img').src = 'data:image/jpeg;base64,' + it.photo;
  document.getElementById('lightbox').classList.add('active');
}
function closeLightbox() { document.getElementById('lightbox').classList.remove('active'); }

function totalFor(art) {
  if (!order[art]) return 0;
  return Object.values(order[art]).reduce((s,v) => s+(v||0), 0);
}
function hint12(total) {
  if (total === 0) return {cls:'', text:'Не заказано', amt_cls:''};
  const r = total % 12;
  if (r === 0) return {cls:'ok', text:'✓ кратно 12', amt_cls:'ok'};
  const add = 12 - r, sub = r;
  if (add <= sub) return {cls:'warn', text:'+'+add+' до '+Math.ceil(total/12)*12, amt_cls:'warn'};
  return {cls:'warn', text:'−'+sub+' до '+Math.floor(total/12)*12, amt_cls:'warn'};
}
function wosCls(w) { return w < 3 ? 'wos-critical' : w < 6 ? 'wos-soon' : 'wos-nice'; }
function wosLabel(w) {
  if (w < 3) return '🔴 ' + w.toFixed(1) + ' нед (критично)';
  if (w < 6) return '🟠 ' + w.toFixed(1) + ' нед (скоро)';
  return '🟢 ' + w.toFixed(1) + ' нед';
}

function adjSize(art, sz, delta) {
  if (!order[art]) order[art] = {};
  const cur = order[art][sz] || 0;
  const next = Math.max(0, cur + delta);
  if (next === 0) delete order[art][sz];
  else order[art][sz] = next;
  if (Object.keys(order[art]).length === 0) delete order[art];
  save();
  renderItem(art);
  updateBottom();
}

function setFilter(f, ev) {
  filter = f;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  ev.target.classList.add('active');
  renderAll();
}

function renderAll() {
  const filtered = ITEMS.filter(i => {
    if (filter === 'all') return true;
    if (filter === 'critical') return i.wos < 3;
    if (filter === 'soon') return i.wos >= 3 && i.wos < 6;
    if (filter === 'nice') return i.wos >= 6 && i.wos < 10;
    if (filter === 'ordered') return order[i.article] && Object.keys(order[i.article]).length > 0;
    if (filter === 'men') return i.gender === 'М';
    if (filter === 'women') return i.gender === 'Ж';
    return true;
  });
  document.getElementById('items').innerHTML = filtered.map(renderItemHTML).join('');
  document.getElementById('header-meta').textContent =
    filtered.length + ' моделей • Снапшот ' + (filtered[0]?.history?.snap_6mo_date || '') + ' • localStorage';
  updateStats(filtered);
}

function updateStats(filtered) {
  const target = filtered.reduce((s,i) => s+i.target_total, 0);
  let pairs = 0, sum = 0;
  for (const art in order) {
    const t = totalFor(art);
    pairs += t;
    const it = ITEMS.find(i => i.article === art);
    if (it) sum += t * it.buy_price;
  }
  document.getElementById('s-models').textContent = filtered.length;
  document.getElementById('s-target').textContent = target;
  document.getElementById('s-pairs').textContent = pairs;
  document.getElementById('s-sum').textContent = fmtK(sum);
}

function updateBottom() {
  let pairs = 0, sum = 0, mc = 0;
  for (const art in order) {
    const t = totalFor(art);
    if (t > 0) {
      mc++; pairs += t;
      const it = ITEMS.find(i => i.article === art);
      if (it) sum += t * it.buy_price;
    }
  }
  document.getElementById('b-models').textContent = mc;
  document.getElementById('b-pairs').textContent = pairs;
  document.getElementById('b-sum').textContent = fmtK(sum);
  document.getElementById('s-pairs').textContent = pairs;
  document.getElementById('s-sum').textContent = fmtK(sum);
}

function renderItemHTML(item) {
  const total = totalFor(item.article);
  const h = hint12(total);
  const photo = item.photo
    ? '<img class="item-photo" src="data:image/jpeg;base64,'+item.photo+'" onclick="openLightbox(\\''+item.article+'\\')">'
    : '<div class="item-photo-empty"><b>'+item.article+'</b><div>нет фото</div></div>';

  const sizesHtml = item.sizes.map(s => {
    const q = (order[item.article] && order[item.article][s.size]) || 0;
    return `<div class="size-cell">
      <div class="size-num">${s.size}</div>
      <div class="size-info">🏪 <b>${s.stock}</b> в сети<br>⚡ <b>${s.sold_60d}</b> за 60д</div>
      <div class="size-row">
        <button class="sz-btn minus" onclick="adjSize('${item.article}','${s.size}',-1)">−</button>
        <span class="sz-qty ${q===0?'zero':'has'}">${q}</span>
        <button class="sz-btn plus" onclick="adjSize('${item.article}','${s.size}',1)">+</button>
      </div>
    </div>`;
  }).join('');

  const hist = item.history || {};
  const histStr = hist.first_supply_date
    ? `<div class="velocity-box history">
        <span>📅 <b>${hist.first_supply_date.slice(0,7)}</b>: поставка ${hist.first_supply_qty} пар</span>
        ${hist.stock_6mo > 0 ? '<span style="color:var(--text3)">→ '+hist.snap_6mo_date.slice(0,7)+': <b>'+hist.stock_6mo+'</b></span>' : ''}
        <span style="color:var(--blue)">→ сейчас: <b>${item.stock.total}</b></span>
      </div>` : '';

  const s = item.sales;
  const sum_buy = total * item.buy_price;

  return `<div class="item" id="item-${item.article}">
    <div class="item-main">
      ${photo}
      <div class="item-body">
        <div class="item-name">${item.model}</div>
        <div class="item-article">
          <span class="gender-badge gender-${item.gender}">${item.gender}</span>
          <span style="color:var(--blue);cursor:pointer" onclick="navigator.clipboard.writeText('${item.article}').then(()=>toast('${item.article} скопирован'))">${item.article} 📋</span>
        </div>
        <div class="item-article" style="margin-top:4px"><span class="wos-badge ${wosCls(item.wos)}">${wosLabel(item.wos)}</span></div>
      </div>
      <div class="item-right">
        <div class="stock-total">${item.stock.total}</div>
        <div style="font-size:10px;color:var(--text3)">🎯 цель ${item.target_total}</div>
      </div>
    </div>
    <div class="item-details">
      <div class="flow-box">
        <div class="flow-cell">
          <div class="flow-label">Остаток</div>
          <div class="flow-val" style="color:var(--blue)">${item.stock.total}</div>
          <div class="flow-sub">${item.buy_price ? fmtK(item.stock.total*item.buy_price)+'₸ закуп' : ''}</div>
        </div>
        <div class="flow-cell">
          <div class="flow-label">Продано 90д</div>
          <div class="flow-val" style="color:var(--green)">${s.s90}</div>
          <div class="flow-sub">из них 30д: ${s.s30}</div>
        </div>
        <div class="flow-cell">
          <div class="flow-label">Посл. продажа</div>
          <div class="flow-val" style="font-size:14px">${item.last_sale ? item.last_sale.slice(5) : '—'}</div>
          <div class="flow-sub">${item.days_no_sale != null ? item.days_no_sale+' дн назад' : 'не было'}</div>
        </div>
      </div>
      ${histStr}
      <div class="velocity-box">
        <span>⚡ Продано:</span>
        <span><b>${s.s30}</b> <span style="font-size:10px;color:var(--text3)">30д</span></span>
        <span><b>${s.s90}</b> <span style="font-size:10px;color:var(--text3)">90д</span></span>
        <span><b>${s.s180}</b> <span style="font-size:10px;color:var(--text3)">180д</span></span>
        <span><b>${s.s365}</b> <span style="font-size:10px;color:var(--text3)">365д</span></span>
        <span><b>${s.sall}</b> <span style="font-size:10px;color:var(--text3)">всё</span></span>
        <span style="margin-left:auto;color:var(--text3)">📊 ${item.adj_rate}/нед</span>
      </div>
      <div class="stock-grid">
        <div class="stock-cell"><div class="stock-cell-label">Мск</div><div class="stock-cell-val ${item.stock.msk===0?'zero':''}">${item.stock.msk}</div></div>
        <div class="stock-cell"><div class="stock-cell-label">ЦУМ+Онл</div><div class="stock-cell-val ${item.stock.tsum_online===0?'zero':''}">${item.stock.tsum_online}</div></div>
        <div class="stock-cell"><div class="stock-cell-label">Аруж</div><div class="stock-cell-val ${item.stock.aruzhan===0?'zero':''}">${item.stock.aruzhan}</div></div>
        <div class="stock-cell"><div class="stock-cell-label">Склад</div><div class="stock-cell-val ${item.stock.warehouse===0?'zero':''}">${item.stock.warehouse}</div></div>
      </div>
      <div class="price-row">
        ${item.buy_price ? '<div class="price-item"><span class="label">Закуп</span> <span class="val">'+fmt(item.buy_price)+'₸</span></div>' : ''}
        <div class="price-item"><span class="label">РЦ</span> <span class="val">${fmt(item.avg_price)}₸</span></div>
        ${item.buy_price && item.avg_price ? '<div class="price-item"><span class="label">Маржа</span> <span class="val">'+Math.round((1-item.buy_price/item.avg_price)*100)+'%</span></div>' : ''}
        ${total > 0 ? '<div class="price-item"><span class="label">Сумма заказа</span> <span class="val">'+fmt(sum_buy)+'₸</span></div>' : ''}
      </div>
      <div class="size-grid">${sizesHtml}</div>
      <div class="total-bar ${h.cls}">
        <span>Всего к заказу: <span class="total-amount ${h.amt_cls}">${total}</span> пар</span>
        <span class="total-hint">${h.text}</span>
      </div>
    </div>
  </div>`;
}

function renderItem(art) {
  const el = document.getElementById('item-' + art);
  if (!el) return;
  const item = ITEMS.find(i => i.article === art);
  el.outerHTML = renderItemHTML(item);
}

function resetAll() {
  const n = Object.keys(order).length;
  if (n === 0) { toast('Нечего сбрасывать'); return; }
  if (!confirm('Сбросить весь заказ ('+n+' моделей)?')) return;
  order = {};
  save();
  renderAll();
  updateBottom();
  toast('Сброшено');
}

function exportJSON() {
  const items = [];
  for (const art in order) {
    const t = totalFor(art); if (t === 0) continue;
    const it = ITEMS.find(i => i.article === art);
    items.push({
      article: art, name: it?.model || '?', gender: it?.gender || '?',
      sizes: order[art], total_qty: t,
      buy_price: it?.buy_price || 0,
      sum: (it?.buy_price || 0) * t,
    });
  }
  if (items.length === 0) { toast('Заказ пустой'); return; }
  const payload = {
    generated_at: new Date().toISOString(), total_items: items.length,
    total_pairs: items.reduce((s,i)=>s+i.total_qty,0),
    total_sum: items.reduce((s,i)=>s+i.sum,0),
    items: items,
  };
  const blob = new Blob([JSON.stringify(payload, null, 2)], {type:'application/json'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'reorder_' + new Date().toISOString().slice(0,10) + '.json';
  a.click();
  toast('Экспорт: ' + items.length + ' моделей');
}

function exportSupplier() {
  let txt = '🛒 ЗАКАЗ КРОССОВОК\\n\\n';
  let tp = 0, ts = 0;
  for (const art in order) {
    const t = totalFor(art); if (t === 0) continue;
    const it = ITEMS.find(i => i.article === art); if (!it) continue;
    txt += '📦 ' + it.model + ' (' + art + ')\\n';
    const szs = Object.keys(order[art]).sort((a,b)=>parseFloat(a)-parseFloat(b));
    txt += '  ' + szs.map(s => s+'×'+order[art][s]).join(', ') + '\\n';
    txt += '  Всего: ' + t + ' пар × ' + fmt(it.buy_price) + '₸ = ' + fmt(t*it.buy_price) + '₸\\n\\n';
    tp += t; ts += t * it.buy_price;
  }
  txt += '\\n📊 ИТОГО: ' + tp + ' пар, ' + fmt(ts) + '₸';
  navigator.clipboard.writeText(txt).then(()=>toast('Скопировано — вставь в WhatsApp')).catch(()=>alert(txt));
}

renderAll();
updateBottom();
</script>
</body>
</html>'''


if __name__ == '__main__':
    main()
