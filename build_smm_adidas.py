#!/usr/bin/env python3
"""
SMM-лист акции «Adidas −20%» — чистый список для постинга.
Только: фото, название, артикул, размеры по магазинам, старая→новая цена, −20%.
БЕЗ себеса/маржи.

Источник скидок: /tmp/apply_adidas.json (файл применения, что уже залит в МС).
Запуск: .venv/bin/python sneaker-order/build_smm_adidas.py [--input PATH] [--skip-photos]
Выход:  smm_adidas.html  (self-contained)
URL:    https://yerlannof.github.io/sneaker-order/smm_adidas.html
"""
import duckdb, json, re, sys, os, base64
from datetime import date
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).parent.parent
DB = ROOT / 'data' / 'pnlpower.duckdb'
SNAP = 'inventory_snapshot_stores_20260624'
PHOTO_CACHE = Path(__file__).parent / '.photo_cache_sneakers.json'
OUT_HTML = Path(__file__).parent / 'smm_adidas.html'
INPUT = '/tmp/apply_adidas.json'

SIZE_RE = re.compile(r',\s*(\d+(?:\.\d+)?)\s*$')


def load_cache():
    if PHOTO_CACHE.exists():
        try: return json.loads(PHOTO_CACHE.read_text())
        except Exception: return {}
    return {}


def fetch_photo(art, headers, base_url):
    import requests
    try:
        r = requests.get(f"{base_url}/entity/product", headers=headers,
                         params={'filter': f'article={art}', 'limit': 1}, timeout=5)
        rows = r.json().get('rows', []) if r.status_code == 200 else []
        if not rows: return ''
        meta = rows[0].get('images', {}).get('meta', {})
        if not meta.get('href'): return ''
        r2 = requests.get(meta['href'], headers=headers, timeout=5)
        imgs = r2.json().get('rows', []) if r2.status_code == 200 else []
        if not imgs: return ''
        mini = imgs[0].get('miniature', {}).get('href') or imgs[0].get('tiny', {}).get('href')
        if not mini: return ''
        r3 = requests.get(mini, headers=headers, timeout=5)
        return base64.b64encode(r3.content).decode('ascii') if r3.status_code == 200 else ''
    except Exception:
        return ''


def main():
    inp = INPUT
    if '--input' in sys.argv:
        inp = sys.argv[sys.argv.index('--input') + 1]
    deal = json.loads(Path(inp).read_text())['items']
    by_article = {str(it['article']): it for it in deal}
    arts = list(by_article)
    con = duckdb.connect(str(DB), read_only=True)

    # размеры по складам из снапшота
    ph = ','.join(['?'] * len(arts))
    rows = con.execute(f"""
        SELECT article, product_name, moscow, tsum, online, astana_aruzhan
        FROM {SNAP} WHERE article IN ({ph}) AND total_stock > 0
    """, arts).fetchall()
    sizes = defaultdict(lambda: {'Москва': defaultdict(int), 'ЦУМ+Онл': defaultdict(int), 'Аружан': defaultdict(int)})
    for art, name, ms, ts, on_, ar in rows:
        m = SIZE_RE.search(name or '')
        sz = m.group(1).split('.')[0] if m else '?'
        s = sizes[art]
        s['Москва'][sz] += int(ms or 0)
        s['ЦУМ+Онл'][sz] += int((ts or 0) + (on_ or 0))
        s['Аружан'][sz] += int(ar or 0)

    # фото
    cache = load_cache()
    if '--skip-photos' not in sys.argv:
        from dotenv import load_dotenv
        load_dotenv(ROOT / '.env')
        token = os.getenv('MOYSKLAD_TOKEN')
        if token:
            headers = {'Authorization': f'Bearer {token}', 'Accept-Encoding': 'gzip'}
            base_url = 'https://api.moysklad.ru/api/remap/1.2'
            todo = [a for a in arts if not cache.get(a)]
            print(f"   Догрузка фото: {len(todo)}")
            for a in todo:
                cache[a] = fetch_photo(a, headers, base_url)
            PHOTO_CACHE.write_text(json.dumps(cache, ensure_ascii=False))

    # сборка items
    items = []
    for art, it in by_article.items():
        sz = sizes.get(art, {})
        store_sizes = {}
        for store in ('Москва', 'ЦУМ+Онл', 'Аружан'):
            present = sorted([s for s, q in sz.get(store, {}).items() if q > 0], key=lambda x: int(x) if x.isdigit() else 99)
            if present:
                store_sizes[store] = present
        items.append({
            'name': it['name'], 'article': art,
            'old': round(it['current_retail']), 'new': round(it['new_price']),
            'stock': it.get('stock', 0), 'sizes': store_sizes,
            'photo': cache.get(art, ''),
        })
    items.sort(key=lambda x: -x['stock'])
    print(f"   Моделей: {len(items)} | с фото: {sum(1 for i in items if i['photo'])}/{len(items)}")
    render(items)
    return items


def render(items):
    today_s = date.today().strftime('%d.%m.%Y')
    data = json.dumps(items, ensure_ascii=False)
    total_pairs = sum(i['stock'] for i in items)
    html = '''<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Adidas −20% — для СММ</title><style>
:root{--bg:#f6f7f9;--card:#fff;--border:#e5e7eb;--text:#1a1d23;--text2:#525866;--text3:#9aa1ad}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--text);font-size:14px}
.header{background:#000;color:#fff;padding:18px;text-align:center}
.header h1{font-size:22px;font-weight:900;letter-spacing:1px}
.header .sub{color:#a3a3a3;font-size:12px;margin-top:4px}
.disc-big{display:inline-block;background:#dc2626;color:#fff;font-weight:900;font-size:15px;padding:3px 12px;border-radius:20px;margin-top:8px}
.items{padding:12px;display:flex;flex-direction:column;gap:10px;max-width:680px;margin:0 auto}
.card{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:12px;display:flex;gap:12px}
.photo{width:96px;height:96px;border-radius:10px;object-fit:cover;background:#f1f3f5;flex-shrink:0}
.photo-empty{width:96px;height:96px;border-radius:10px;background:#f1f3f5;display:flex;align-items:center;justify-content:center;color:var(--text3);font-size:10px;flex-shrink:0}
.body{flex:1;min-width:0}
.name{font-weight:800;font-size:16px;line-height:1.25}
.art{font-size:11px;color:var(--text3);margin:2px 0 8px}
.price{display:flex;align-items:baseline;gap:10px;margin-bottom:8px}
.old{text-decoration:line-through;color:var(--text3);font-size:15px}
.new{color:#dc2626;font-weight:900;font-size:22px}
.off{background:#dc2626;color:#fff;font-size:11px;font-weight:800;padding:2px 7px;border-radius:6px}
.store{font-size:12px;color:var(--text2);margin-bottom:3px}
.store b{color:var(--text)}
.sz{display:inline-block;background:#f1f5f9;border-radius:5px;padding:1px 6px;margin:1px 2px 1px 0;font-size:11px;font-weight:600}
.controls{padding:10px 12px;max-width:680px;margin:0 auto}
.search{width:100%;padding:10px 12px;border:1px solid var(--border);border-radius:9px;font-size:14px}
.foot{text-align:center;color:var(--text3);font-size:11px;padding:16px}
@media(max-width:600px){.photo,.photo-empty{width:80px;height:80px}.new{font-size:20px}}
</style></head><body>
<div class="header"><h1>ADIDAS</h1><div class="sub">Данные ''' + today_s + ''' · ''' + str(len(items)) + ''' моделей · ''' + str(total_pairs) + ''' пар</div><div class="disc-big">−20% на всё</div></div>
<div class="controls"><input class="search" id="q" placeholder="🔍 поиск по названию / артикулу…" oninput="render()"></div>
<div class="items" id="list"></div>
<div class="foot">Цены действуют, пока есть наличие · размеры уточняйте по магазину</div>
<script>
const DATA=''' + data + ''';
function fmt(n){return (n||0).toLocaleString('ru-RU');}
function card(it){
  const ph=it.photo?`<img class="photo" src="data:image/jpeg;base64,${it.photo}">`:`<div class="photo-empty">фото</div>`;
  let stores='';
  for(const st in it.sizes){stores+=`<div class="store">📍 <b>${st}</b>: ${it.sizes[st].map(s=>`<span class="sz">${s}</span>`).join('')}</div>`;}
  if(!stores)stores='<div class="store" style="color:#dc2626">нет в наличии</div>';
  return `<div class="card">${ph}<div class="body">
    <div class="name">${it.name}</div><div class="art">арт. ${it.article}</div>
    <div class="price"><span class="old">${fmt(it.old)}₸</span><span class="new">${fmt(it.new)}₸</span><span class="off">−20%</span></div>
    ${stores}</div></div>`;
}
function render(){
  const q=document.getElementById('q').value.toLowerCase();
  const arr=DATA.filter(it=>!q||it.name.toLowerCase().includes(q)||it.article.includes(q));
  document.getElementById('list').innerHTML=arr.map(card).join('');
}
render();
</script></body></html>'''
    OUT_HTML.write_text(html, encoding='utf-8')
    print(f"\n✓ SMM: {OUT_HTML} ({OUT_HTML.stat().st_size/1024:.0f} KB)")


if __name__ == '__main__':
    main()
