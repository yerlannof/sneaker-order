#!/usr/bin/env python3
"""
Генератор страницы для дозаказа — per-size формат.

Особенности:
- Per-size кнопки +/- для точного заказа размеров (не коробок)
- Видны остатки каждого размера в сети
- Видны продажи каждого размера за 60 дней
- Подсветка кратности 12 (12/24/36/48...)
- Подсказка сколько нужно добавить/убрать до 12-кратности
- Сохранение в localStorage (как и уценочный)
- Экспорт JSON для создания поступления в МС

Запуск:
    python3 sneaker-order/build_reorder.py
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
OUT_JSON = Path(__file__).parent / "reorder_data.json"
PHOTO_CACHE = Path(__file__).parent / ".photo_cache_sneakers.json"

# Сезонность (CLAUDE.md)
SEASON = {1: 0.59, 2: 0.79, 3: 1.35, 4: 1.26, 5: 1.00, 6: 0.99,
          7: 0.79, 8: 1.33, 9: 1.08, 10: 1.06, 11: 0.91, 12: 0.86}


def latest_snapshot(con):
    return con.execute("""SELECT table_name FROM information_schema.tables
        WHERE table_name LIKE 'inventory_snapshot_stores_%'
        ORDER BY table_name DESC LIMIT 1""").fetchone()[0]


def main():
    today = date.today()
    season_coef = SEASON[today.month]
    print(f"Дата: {today}, сезонный коэф: {season_coef}")

    con = duckdb.connect(str(DB_PATH), read_only=True)
    snap = latest_snapshot(con)
    print(f"Снапшот: {snap}\n")

    # 1. Базовый запрос — артикулы с продажами ≥ 5 за 35 дней, WOS < 10
    print("1. Отбираю модели в заказ...")
    base_rows = con.execute(f"""
WITH s35 AS (
    SELECT
        article,
        ANY_VALUE(REGEXP_REPLACE(product_name, ',\\s*\\d+(\\.\\d+)?$', '')) AS model,
        SUM(quantity) AS qty_35d,
        ROUND(SUM(quantity)/5.0 * {season_coef}, 1) AS adj_rate,
        ROUND(AVG(CASE WHEN price>0 THEN price END)) AS avg_price
    FROM retaildemand_positions
    WHERE document_moment >= CURRENT_DATE - INTERVAL 35 DAY
      AND price > 0
      AND TRY_CAST(article AS INTEGER) BETWEEN 200000 AND 209999
    GROUP BY article HAVING SUM(quantity) >= 5
),
s60 AS (
    SELECT article, SUM(quantity) AS qty_60d
    FROM retaildemand_positions
    WHERE document_moment >= CURRENT_DATE - INTERVAL 60 DAY AND price > 0
    GROUP BY article
),
stk AS (
    SELECT article,
        SUM(total_stock) AS total,
        SUM(moscow) AS msk, SUM(tsum) AS ts, SUM(online) AS onl,
        SUM(astana_aruzhan) AS ar, SUM(main_warehouse) AS wh
    FROM {snap}
    WHERE TRY_CAST(article AS INTEGER) BETWEEN 200000 AND 209999
    GROUP BY article
),
buy_p AS (
    SELECT product_article AS article,
        LAST(price ORDER BY supply_moment) AS buy_price,
        LAST(agent_name ORDER BY supply_moment) AS supplier
    FROM supply_positions
    WHERE agent_name='Поставщик In' AND supply_moment>='2025-01-01'
    GROUP BY product_article
)
SELECT s35.article, s35.model, s35.qty_35d, s35.adj_rate, s35.avg_price,
    COALESCE(s60.qty_60d, s35.qty_35d) AS qty_60d,
    COALESCE(stk.total,0) AS total,
    COALESCE(stk.msk,0) AS msk,
    COALESCE(stk.ts,0) AS ts,
    COALESCE(stk.onl,0) AS onl,
    COALESCE(stk.ar,0) AS ar,
    COALESCE(stk.wh,0) AS wh,
    CASE WHEN s35.adj_rate>0 THEN ROUND(COALESCE(stk.total,0)/s35.adj_rate,1) ELSE 999 END AS wos,
    COALESCE(buy_p.buy_price, 0) AS buy_price,
    buy_p.supplier
FROM s35
LEFT JOIN s60 ON s35.article = s60.article
LEFT JOIN stk ON s35.article = stk.article
LEFT JOIN buy_p ON s35.article = buy_p.article
WHERE CASE WHEN s35.adj_rate>0 THEN COALESCE(stk.total,0)/s35.adj_rate ELSE 999 END < 10
ORDER BY s35.adj_rate DESC
""").fetchall()
    print(f"   Найдено: {len(base_rows)} моделей")

    # 2. Размеры — для каждой модели остаток + продано
    print("2. Размеры по моделям...")
    items = []
    for r in base_rows:
        (article, model, qty_35d, adj_rate, avg_price, qty_60d,
         total, msk, ts, onl, ar, wh, wos, buy_price, supplier) = r

        # Размеры с остатком (с разделением по магазинам)
        size_stock = con.execute(f"""
            SELECT
                REGEXP_EXTRACT(product_name, ',\\s*(\\d+\\.?\\d*)$', 1) AS size,
                SUM(moscow) AS msk, SUM(tsum) AS ts, SUM(online) AS onl,
                SUM(astana_aruzhan) AS ar, SUM(main_warehouse) AS wh,
                SUM(total_stock) AS total
            FROM {snap}
            WHERE article = '{article}' AND total_stock > 0
            GROUP BY 1 ORDER BY 1
        """).fetchall()

        # Размеры с продажами (60 дней)
        size_sold = con.execute(f"""
            SELECT
                REGEXP_EXTRACT(product_name, ',\\s*(\\d+\\.?\\d*)$', 1) AS size,
                CAST(SUM(quantity) AS INT) AS qty
            FROM retaildemand_positions
            WHERE article = '{article}' AND price > 0
              AND document_moment >= CURRENT_DATE - INTERVAL 60 DAY
            GROUP BY 1
        """).fetchall()
        sold_by_size = {s: q for s, q in size_sold if s}

        sizes = []
        for s in size_stock:
            sz = s[0]
            if not sz: continue
            sizes.append({
                'size': sz,
                'stock_total': int(s[6] or 0),
                'msk': int(s[1] or 0),
                'tsum': int((s[2] or 0) + (s[3] or 0)),
                'aruzhan': int(s[4] or 0),
                'warehouse': int(s[5] or 0),
                'sold_60d': sold_by_size.get(sz, 0),
            })

        # Добавим размеры с продажами но без остатка (они нужны для заказа — точно идут!)
        existing_sizes = {s['size'] for s in sizes}
        for sz, q in sold_by_size.items():
            if sz not in existing_sizes:
                sizes.append({
                    'size': sz, 'stock_total': 0, 'msk': 0, 'tsum': 0,
                    'aruzhan': 0, 'warehouse': 0, 'sold_60d': q,
                })

        # Сортировка по размеру
        try:
            sizes.sort(key=lambda x: float(x['size']))
        except (ValueError, TypeError):
            sizes.sort(key=lambda x: x['size'])

        # Гендер по размерной сетке (мужская/женская/унисекс)
        size_nums = [float(s['size']) for s in sizes if s['size']]
        if size_nums:
            min_sz = min(size_nums); max_sz = max(size_nums)
            if max_sz <= 40 and min_sz >= 35: gender = 'Ж'
            elif min_sz >= 40 and max_sz <= 46: gender = 'М'
            else: gender = 'У'  # унисекс
        else:
            gender = '?'

        # Рекомендация: сколько нужно для покрытия 8 недель спроса
        target_total = max(0, round(float(adj_rate) * 8 - int(total)))

        items.append({
            'article': str(article),
            'model': model or '',
            'gender': gender,
            'qty_35d': int(qty_35d),
            'qty_60d': int(qty_60d),
            'adj_rate': float(adj_rate),
            'avg_price': int(avg_price or 0),
            'buy_price': int(buy_price or 0),
            'supplier': supplier or '?',
            'stock': {
                'total': int(total),
                'msk': int(msk),
                'tsum_online': int(ts) + int(onl),
                'aruzhan': int(ar),
                'warehouse': int(wh),
            },
            'wos': float(wos),
            'sizes': sizes,
            'target_total': target_total,  # рекомендованный заказ всего
        })

    # 3. Фото из кэша
    print("3. Фото из кэша...")
    cache = {}
    if PHOTO_CACHE.exists():
        cache = json.loads(PHOTO_CACHE.read_text())
    for it in items:
        it['photo'] = cache.get(it['article'], '')
    with_photo = sum(1 for it in items if it['photo'])
    print(f"   С фото: {with_photo}/{len(items)}")

    # Сортировка по WOS (срочнее → выше)
    items.sort(key=lambda x: (x['wos'], -x['adj_rate']))

    con.close()

    # Сохраняем JSON для встраивания в HTML
    OUT_JSON.write_text(json.dumps(items, ensure_ascii=False))
    print(f"\n   JSON: {OUT_JSON} ({OUT_JSON.stat().st_size/1024:.0f} KB)")

    # Сводка
    total_models = len(items)
    total_target = sum(i['target_total'] for i in items)
    print(f"\n=== СВОДКА ===")
    print(f"   Моделей: {total_models}")
    print(f"   Рекомендованный заказ всего (target): {total_target} пар")
    print(f"   Срочных (WOS<3): {sum(1 for i in items if i['wos']<3)}")
    print(f"   Скоро (WOS 3-6): {sum(1 for i in items if 3<=i['wos']<6)}")
    print(f"   Норм (WOS 6-10): {sum(1 for i in items if 6<=i['wos']<10)}")

    # HTML
    print("\n4. Генерирую HTML...")
    render_html(items)
    print(f"   HTML: {OUT_HTML}")


def render_html(items):
    items_json = json.dumps(items, ensure_ascii=False)

    html = '''<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Дозаказ кроссовок — Per-Size</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #f8f9fb; --card: #fff; --border: #e8ecf1;
  --text: #1a1d23; --text2: #6b7280; --text3: #9ca3af;
  --blue: #3b82f6; --green: #10b981; --red: #ef4444; --orange: #f59e0b;
}
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family:'Inter',system-ui,sans-serif; background:var(--bg); color:var(--text);
  padding-bottom:80px; -webkit-font-smoothing:antialiased; }
.header { background:var(--card); border-bottom:1px solid var(--border); padding:16px 20px;
  position:sticky; top:0; z-index:100; }
.header h1 { font-size:20px; font-weight:800; }
.header-meta { font-size:12px; color:var(--text3); margin-top:4px; }

.filters { padding:8px 16px; display:flex; gap:6px; overflow-x:auto; background:var(--bg);
  position:sticky; top:62px; z-index:90; }
.filter-btn { padding:6px 12px; border-radius:20px; border:1px solid var(--border);
  background:var(--card); font-size:12px; font-weight:600; cursor:pointer; white-space:nowrap;
  font-family:inherit; }
.filter-btn.active { background:var(--text); color:#fff; border-color:var(--text); }

.items { padding:0 12px; }
.item { background:var(--card); border:1px solid var(--border); border-radius:12px;
  margin-bottom:10px; overflow:hidden; }
.item-head { display:flex; gap:12px; padding:12px; }
.item-photo { width:80px; height:80px; border-radius:8px; object-fit:cover;
  flex-shrink:0; background:var(--bg); cursor:pointer; }
.item-photo-empty { width:80px; height:80px; border-radius:8px; background:var(--bg);
  display:flex; align-items:center; justify-content:center; flex-shrink:0; color:var(--text3);
  font-size:10px; text-align:center; }
.item-body { flex:1; min-width:0; }
.item-name { font-weight:700; font-size:14px; line-height:1.3; margin-bottom:4px; }
.item-art { font-size:11px; color:var(--text3); display:flex; gap:8px; flex-wrap:wrap; }
.gender-badge { padding:1px 6px; border-radius:4px; font-size:10px; font-weight:700; }
.gender-Ж { background:#fce7f3; color:#be185d; }
.gender-М { background:#dbeafe; color:#1d4ed8; }
.gender-У { background:#e0e7ff; color:#5b21b6; }
.wos-badge { padding:2px 7px; border-radius:6px; font-size:11px; font-weight:700; }
.wos-critical { background:#fee2e2; color:#dc2626; }
.wos-soon { background:#fed7aa; color:#c2410c; }
.wos-nice { background:#d1fae5; color:#059669; }

.item-stats { padding:0 12px 8px; display:flex; gap:8px; flex-wrap:wrap; font-size:11px;
  color:var(--text2); }
.stat-pill { padding:3px 8px; border-radius:6px; background:var(--bg); }
.stat-pill b { color:var(--text); }

.stocks-row { padding:8px 12px; display:grid; grid-template-columns:repeat(4,1fr); gap:4px;
  margin-bottom:4px; }
.stock-cell { text-align:center; padding:4px; background:var(--bg); border-radius:6px;
  font-size:11px; }
.stock-cell-label { color:var(--text3); font-weight:600; text-transform:uppercase; font-size:9px; }
.stock-cell-val { font-weight:700; font-size:13px; }
.stock-cell-val.zero { color:var(--text3); }

.sizes-grid { padding:6px 8px; display:grid; grid-template-columns:repeat(auto-fill,minmax(140px,1fr));
  gap:6px; }
.size-card { border:1px solid var(--border); border-radius:8px; padding:6px 8px; background:#fafafa; }
.size-row { display:flex; justify-content:space-between; align-items:center; }
.size-num { font-size:16px; font-weight:800; }
.size-info { font-size:10px; color:var(--text3); line-height:1.3; }
.size-info b { color:var(--text2); }
.size-buttons { display:flex; gap:4px; align-items:center; margin-top:4px; }
.sz-btn { width:28px; height:28px; border-radius:6px; border:1px solid var(--border);
  background:#fff; font-size:16px; font-weight:700; cursor:pointer; -webkit-tap-highlight-color:transparent; }
.sz-btn:active { transform:scale(0.9); }
.sz-btn.plus { color:var(--green); }
.sz-btn.minus { color:var(--red); }
.sz-qty { flex:1; text-align:center; font-size:16px; font-weight:800; min-width:30px; }
.sz-qty.zero { color:var(--text3); }

.total-bar { padding:8px 12px; background:#fffbeb; border-top:1px solid #fcd34d;
  display:flex; gap:12px; align-items:center; flex-wrap:wrap; font-size:12px; }
.total-bar.empty { background:var(--bg); border-top:1px solid var(--border); }
.total-bar.ok { background:#ecfdf5; border-top:1px solid #6ee7b7; }
.total-amount { font-size:16px; font-weight:800; color:var(--orange); }
.total-amount.ok { color:var(--green); }
.total-hint { color:var(--text2); font-size:11px; }

.bottom { position:fixed; bottom:0; left:0; right:0; background:var(--card);
  border-top:1px solid var(--border); padding:12px 16px;
  display:flex; gap:8px; justify-content:space-between; align-items:center;
  box-shadow:0 -4px 12px rgba(0,0,0,0.08); z-index:100; }
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
.lb-close { position:absolute; top:16px; right:20px; color:#fff; font-size:32px;
  cursor:pointer; }
</style>
</head>
<body>

<div class="header">
  <h1>🛒 Дозаказ кроссовок</h1>
  <div class="header-meta" id="header-meta">Загрузка...</div>
</div>

<div class="filters">
  <button class="filter-btn active" onclick="setFilter('all')">Все</button>
  <button class="filter-btn" onclick="setFilter('critical')">🔴 WOS&lt;3</button>
  <button class="filter-btn" onclick="setFilter('soon')">🟠 WOS 3-6</button>
  <button class="filter-btn" onclick="setFilter('nice')">🟢 WOS 6-10</button>
  <button class="filter-btn" onclick="setFilter('ordered')">📝 В заказе</button>
  <button class="filter-btn" onclick="setFilter('men')">М</button>
  <button class="filter-btn" onclick="setFilter('women')">Ж</button>
</div>

<div class="items" id="items"></div>

<div class="bottom">
  <div class="bottom-info">
    <span id="order-count">0</span> моделей |
    <b id="order-pairs">0</b> пар |
    <b id="order-sum">0</b>₸
  </div>
  <div style="display:flex; gap:6px;">
    <button class="btn btn-outline" onclick="resetAll()" title="Сбросить">🔄</button>
    <button class="btn btn-outline" onclick="exportJSON()">JSON</button>
    <button class="btn btn-green" onclick="exportSupplier()">Поставщику</button>
  </div>
</div>

<div class="toast" id="toast"></div>
<div class="lightbox" id="lightbox" onclick="closeLightbox()">
  <div class="lb-close">&times;</div>
  <img id="lb-img" src="">
</div>

<script>
const ITEMS = ''' + items_json + ''';
let order = {};  // {article: {size: qty}}
let filter = 'all';

// Load from localStorage
try {
  const saved = localStorage.getItem('reorder_v1');
  if (saved) order = JSON.parse(saved);
} catch(e) {}

function save() {
  try { localStorage.setItem('reorder_v1', JSON.stringify(order)); } catch(e) {}
}

function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2000);
}

function fmt(n) { return (n||0).toLocaleString('ru-RU'); }
function fmtK(n) { return n >= 1000 ? (n/1000).toFixed(0)+'K' : Math.round(n); }

function openLightbox(art) {
  const it = ITEMS.find(i => i.article === art);
  if (!it || !it.photo) return;
  document.getElementById('lb-img').src = 'data:image/jpeg;base64,' + it.photo;
  document.getElementById('lightbox').classList.add('active');
}
function closeLightbox() {
  document.getElementById('lightbox').classList.remove('active');
}

function wosClass(wos) {
  if (wos < 3) return 'wos-critical';
  if (wos < 6) return 'wos-soon';
  return 'wos-nice';
}

function wosLabel(wos) {
  if (wos < 3) return '🔴 ' + wos.toFixed(1) + ' нед (критично)';
  if (wos < 6) return '🟠 ' + wos.toFixed(1) + ' нед (скоро)';
  return '🟢 ' + wos.toFixed(1) + ' нед';
}

function totalForItem(article) {
  if (!order[article]) return 0;
  return Object.values(order[article]).reduce((s,v) => s+(v||0), 0);
}

function getRemainderHint(total) {
  if (total === 0) return {state: 'empty', text: 'Не заказано', need: 0};
  const r = total % 12;
  if (r === 0) return {state: 'ok', text: '✓ кратно 12', need: 0};
  const toAdd = 12 - r;
  const toRemove = r;
  if (toAdd <= toRemove) return {state: 'warn', text: '+' + toAdd + ' до '+Math.ceil(total/12)*12, need: toAdd};
  return {state: 'warn', text: '−' + toRemove + ' до ' + Math.floor(total/12)*12, need: -toRemove};
}

function changeSize(article, size, delta) {
  if (!order[article]) order[article] = {};
  const cur = order[article][size] || 0;
  const next = Math.max(0, cur + delta);
  if (next === 0) delete order[article][size];
  else order[article][size] = next;
  if (Object.keys(order[article]).length === 0) delete order[article];
  save();
  renderItem(article);  // ре-рендер только этой карточки
  updateBottom();
}

function setFilter(f) {
  filter = f;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  event.target.classList.add('active');
  renderAll();
}

function renderAll() {
  const container = document.getElementById('items');
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
  container.innerHTML = filtered.map(renderItemHTML).join('');
  document.getElementById('header-meta').textContent =
    filtered.length + ' моделей • Сохраняется автоматически в браузере';
}

function renderItemHTML(item) {
  const total = totalForItem(item.article);
  const hint = getRemainderHint(total);
  const photoHtml = item.photo
    ? '<img class="item-photo" src="data:image/jpeg;base64,' + item.photo +
      '" onclick="openLightbox(\\'' + item.article + '\\')">'
    : '<div class="item-photo-empty">' + item.article + '<br>нет фото</div>';

  const sizesHtml = item.sizes.map(s => {
    const qty = (order[item.article] && order[item.article][s.size]) || 0;
    return `<div class="size-card">
      <div class="size-row">
        <span class="size-num">${s.size}</span>
        <div class="size-info" style="text-align:right">
          <div>🏪 <b>${s.stock_total}</b> в сети</div>
          <div>⚡ <b>${s.sold_60d}</b> за 60д</div>
        </div>
      </div>
      <div class="size-buttons">
        <button class="sz-btn minus" onclick="changeSize('${item.article}','${s.size}',-1)">−</button>
        <span class="sz-qty ${qty===0?'zero':''}">${qty}</span>
        <button class="sz-btn plus" onclick="changeSize('${item.article}','${s.size}',1)">+</button>
      </div>
    </div>`;
  }).join('');

  const totalSum = total * item.buy_price;
  const totalBarClass = hint.state;

  return `<div class="item" id="item-${item.article}">
    <div class="item-head">
      ${photoHtml}
      <div class="item-body">
        <div class="item-name">${item.model}</div>
        <div class="item-art">
          <span style="color:var(--blue);cursor:pointer"
            onclick="navigator.clipboard.writeText('${item.article}').then(()=>toast('${item.article} скопирован'))">
            ${item.article} 📋
          </span>
          <span class="gender-badge gender-${item.gender}">${item.gender}</span>
          <span class="wos-badge ${wosClass(item.wos)}">${wosLabel(item.wos)}</span>
        </div>
      </div>
    </div>

    <div class="item-stats">
      <span class="stat-pill">⚡ <b>${item.qty_35d}</b> за 35д (${item.qty_60d} за 60д)</span>
      <span class="stat-pill">📊 <b>${item.adj_rate}</b>/нед</span>
      <span class="stat-pill">💵 РЦ <b>${fmt(item.avg_price)}</b>₸</span>
      ${item.buy_price > 0 ? '<span class="stat-pill">💸 Закуп <b>'+fmt(item.buy_price)+'</b>₸</span>' : ''}
      <span class="stat-pill">🎯 цель <b>${item.target_total}</b> пар</span>
    </div>

    <div class="stocks-row">
      <div class="stock-cell"><div class="stock-cell-label">Мск</div>
        <div class="stock-cell-val ${item.stock.msk===0?'zero':''}">${item.stock.msk}</div></div>
      <div class="stock-cell"><div class="stock-cell-label">ЦУМ+Онл</div>
        <div class="stock-cell-val ${item.stock.tsum_online===0?'zero':''}">${item.stock.tsum_online}</div></div>
      <div class="stock-cell"><div class="stock-cell-label">Аруж</div>
        <div class="stock-cell-val ${item.stock.aruzhan===0?'zero':''}">${item.stock.aruzhan}</div></div>
      <div class="stock-cell"><div class="stock-cell-label">Склад</div>
        <div class="stock-cell-val ${item.stock.warehouse===0?'zero':''}">${item.stock.warehouse}</div></div>
    </div>

    <div class="sizes-grid">${sizesHtml}</div>

    <div class="total-bar ${totalBarClass}">
      <span>Всего к заказу: <span class="total-amount ${hint.state==='ok'?'ok':''}">${total}</span> пар</span>
      <span class="total-hint">${hint.text}</span>
      ${totalSum > 0 ? '<span class="total-hint" style="margin-left:auto">≈ '+fmt(totalSum)+'₸</span>' : ''}
    </div>
  </div>`;
}

function renderItem(article) {
  const el = document.getElementById('item-' + article);
  if (!el) return;
  const item = ITEMS.find(i => i.article === article);
  if (!item) return;
  el.outerHTML = renderItemHTML(item);
}

function updateBottom() {
  let totalPairs = 0, totalSum = 0, modelsWithOrder = 0;
  for (const art in order) {
    const sum = Object.values(order[art]).reduce((s,v) => s+(v||0), 0);
    if (sum > 0) {
      modelsWithOrder++;
      totalPairs += sum;
      const item = ITEMS.find(i => i.article === art);
      if (item) totalSum += sum * item.buy_price;
    }
  }
  document.getElementById('order-count').textContent = modelsWithOrder;
  document.getElementById('order-pairs').textContent = totalPairs;
  document.getElementById('order-sum').textContent = fmtK(totalSum);
}

function resetAll() {
  if (Object.keys(order).length === 0) { toast('Нечего сбрасывать'); return; }
  if (!confirm('Сбросить весь заказ?')) return;
  order = {};
  save();
  renderAll();
  updateBottom();
  toast('Заказ сброшен');
}

function exportJSON() {
  const items = [];
  for (const art in order) {
    const total = Object.values(order[art]).reduce((s,v) => s+(v||0), 0);
    if (total === 0) continue;
    const item = ITEMS.find(i => i.article === art);
    items.push({
      article: art,
      name: item ? item.model : '?',
      gender: item ? item.gender : '?',
      sizes: order[art],
      total_qty: total,
      buy_price: item ? item.buy_price : 0,
      sum: item ? total * item.buy_price : 0,
    });
  }
  if (items.length === 0) { toast('Заказ пустой'); return; }
  const payload = {
    generated_at: new Date().toISOString(),
    total_items: items.length,
    total_pairs: items.reduce((s,i) => s + i.total_qty, 0),
    total_sum: items.reduce((s,i) => s + i.sum, 0),
    items: items
  };
  const blob = new Blob([JSON.stringify(payload, null, 2)], {type:'application/json'});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = 'reorder_' + new Date().toISOString().slice(0,10) + '.json';
  a.click();
  URL.revokeObjectURL(url);
  toast('Экспорт: ' + items.length + ' моделей');
}

function exportSupplier() {
  // Простой текст для WhatsApp
  let txt = '🛒 ЗАКАЗ КРОССОВОК\\n\\n';
  let totalP = 0, totalS = 0;
  for (const art in order) {
    const total = Object.values(order[art]).reduce((s,v) => s+(v||0), 0);
    if (total === 0) continue;
    const item = ITEMS.find(i => i.article === art);
    if (!item) continue;
    txt += `📦 ${item.model} (${art})\\n`;
    const sortedSizes = Object.keys(order[art]).sort((a,b) => parseFloat(a)-parseFloat(b));
    txt += '  Размеры: ' + sortedSizes.map(s => s+'×'+order[art][s]).join(', ') + '\\n';
    txt += `  Всего: ${total} пар × ${fmt(item.buy_price)}₸ = ${fmt(total*item.buy_price)}₸\\n\\n`;
    totalP += total;
    totalS += total * item.buy_price;
  }
  txt += `\\n📊 ИТОГО: ${totalP} пар, ${fmt(totalS)}₸`;

  navigator.clipboard.writeText(txt).then(() => {
    toast('Скопировано — вставь в WhatsApp');
  }).catch(() => {
    // Fallback — показать в alert
    alert(txt);
  });
}

renderAll();
updateBottom();
</script>
</body>
</html>'''

    OUT_HTML.write_text(html, encoding='utf-8')


if __name__ == '__main__':
    main()
