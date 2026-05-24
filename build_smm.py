#!/usr/bin/env python3
"""
Builder для SMM-листа уценки — ЧИСТЫЙ список для СММ.
Только: фото, название, артикул, размеры/варианты по магазинам, старая→новая цена, % скидки.
БЕЗ себеса/маржи/прибыли/рекомендаций.

Обувь:   python3 sneaker-order/build_smm.py
Одежда:  python3 sneaker-order/build_smm.py --clothing
"""
import os, re, sys, json, requests, duckdb
from datetime import date
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / '.env')
SO = ROOT / 'sneaker-order'
DB = ROOT / 'data' / 'pnlpower.duckdb'
SNAP = 'inventory_snapshot_stores_20260523'
KEY = os.getenv('SUPABASE_KEY'); URL = os.getenv('SUPABASE_URL')
SIZE_ORD = {'XS': 0, 'S': 1, 'M': 2, 'L': 3, 'XL': 4, 'XXL': 5, '2XL': 5, 'XXXL': 6, '3XL': 6, '4XL': 7}

CONFIGS = {
    'shoe': dict(order='SNEAKERS-001', data='sneakers_data.json', photos='sneakers_photos.json',
                 out='smm', title='🏷 Уценка обувь — для СММ', cloth=False),
    'cloth': dict(order='CLOTHING-CLEAR-001', data='clothing_clearance_data.json', photos='clothing_clearance_photos.json',
                  out='smm_clothing', title='🏷 Уценка одежда — для СММ', cloth=True),
}


def sizes_by_store(articles, cloth=False):
    """Для каждого артикула: {склад: {вариант: кол-во}}. cloth → 'Цвет Размер', иначе номер размера."""
    con = duckdb.connect(str(DB), read_only=True)
    ph = "','".join(a.replace("'", "''") for a in articles)
    rows = con.execute(f"""
        SELECT article, product_name,
               SUM(moscow) m, SUM(tsum)+SUM(online) t,
               SUM(astana_aruzhan) a, SUM(main_warehouse) w
        FROM {SNAP} WHERE article IN ('{ph}') AND total_stock > 0
        GROUP BY article, product_name
    """).fetchall()
    con.close()

    def label_and_key(pname):
        if cloth:
            m = re.search(r'\(([^()]+),\s*([^()]+)\)\s*$', pname or '')
            if m:
                color, size = m.group(1).strip(), m.group(2).strip()
                return f'{color} {size}', (SIZE_ORD.get(size, 50), color)
        m = re.search(r',\s*([0-9]+(?:\.[0-9]+)?)\s*$', pname or '')
        if m:
            return m.group(1), (float(m.group(1)), '')
        return '?', (99, '')

    res = {}
    for art, pname, m, t, a, w in rows:
        lbl, key = label_and_key(pname)
        d = res.setdefault(art, {'m': {}, 't': {}, 'a': {}, 'w': {}})
        d.setdefault('_k', {})[lbl] = key
        for k2, qty in (('m', m), ('t', t), ('a', a), ('w', w)):
            q = int(qty or 0)
            if q > 0:
                d[k2][lbl] = d[k2].get(lbl, 0) + q
    out = {}
    for art, d in res.items():
        keys = d.pop('_k', {})
        out[art] = {st: dict(sorted(v.items(), key=lambda x: keys.get(x[0], (99, '')))) for st, v in d.items()}
    return out


def main(cfg):
    H = {'apikey': KEY, 'Authorization': f'Bearer {KEY}'}
    resp = requests.get(f"{URL}/rest/v1/orders?id=eq.{cfg['order']}&select=items", headers=H, timeout=20).json()
    if not resp or not resp[0].get('items'):
        print(f"⚠️ В Supabase нет скидок ({cfg['order']}) — нечего показывать"); return
    saved = resp[0]['items']
    disc = {str(it['article']): int(it.get('discount', 0)) for it in saved if it.get('discount')}

    data = {it['article']: it for it in json.load(open(SO / cfg['data']))}
    photos = {}
    pf = SO / cfg['photos']
    if pf.exists():
        photos = json.load(open(pf))

    ss = sizes_by_store(list(disc.keys()), cloth=cfg['cloth'])

    items = []
    for a, d in disc.items():
        it = data.get(a)
        if not it:
            continue
        orig = round(it['orig_price']); cur = round(it['retail'])
        new = round(cur * (1 - d / 100))
        total_off = round(100 * (1 - new / orig)) if orig > 0 else 0
        s = it['stock']
        items.append({
            'article': a, 'name': it['name'], 'brand': it.get('brand', ''),
            'old': orig, 'new': new, 'off': total_off,
            'stock': {'m': s['moscow'], 't': s['tsum_online'], 'a': s['aruzhan'], 'w': s['warehouse'], 'total': s['total']},
            'ss': ss.get(a, {'m': {}, 't': {}, 'a': {}, 'w': {}}),
        })
    items.sort(key=lambda x: -x['off'])

    lite = SO / f"{cfg['out']}_lite.json"
    ph = SO / f"{cfg['out']}_photos.json"
    lite.write_text(json.dumps(items, ensure_ascii=False))
    ph.write_text(json.dumps({x['article']: photos.get(x['article'], '') for x in items if photos.get(x['article'])}, ensure_ascii=False))

    total_pairs = sum(x['stock']['total'] for x in items)
    html = (HTML_TEMPLATE
            .replace('__TITLE__', cfg['title'])
            .replace('__LITE__', lite.name).replace('__PHOTOS__', ph.name)
            .replace('__COUNT__', str(len(items))).replace('__PAIRS__', f'{total_pairs:,}')
            .replace('__DATE__', date.today().strftime('%d.%m.%Y')))
    (SO / f"{cfg['out']}.html").write_text(html)
    print(f"✓ {cfg['out']}.html — моделей: {len(items)}, штук: {total_pairs}")


HTML_TEMPLATE = r'''<!DOCTYPE html>
<html lang="ru"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#f4f6f9;--card:#fff;--text:#111827;--text2:#6b7280;--text3:#9ca3af;--red:#e11d48;--border:#e5e7eb;}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent;}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--text);padding-bottom:40px;}
.header{background:#111827;color:#fff;padding:16px;position:sticky;top:0;z-index:10;}
.header h1{font-size:18px;font-weight:800;}
.header .sub{font-size:12px;color:#9ca3af;margin-top:3px;}
.bar{padding:10px 12px;background:#fff;border-bottom:1px solid var(--border);position:sticky;top:62px;z-index:9;display:flex;gap:6px;flex-wrap:wrap;align-items:center;}
.bar input{flex:1;min-width:140px;padding:9px 12px;border:1px solid var(--border);border-radius:10px;font-size:14px;}
.fbtn{padding:7px 12px;border:1px solid var(--border);background:#fff;border-radius:20px;font-size:13px;font-weight:600;cursor:pointer;white-space:nowrap;}
.fbtn.active{background:#111827;color:#fff;border-color:#111827;}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px;padding:12px;}
.card{background:var(--card);border-radius:14px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.08);display:flex;flex-direction:column;}
.photo{width:100%;aspect-ratio:1/1;object-fit:cover;background:#f3f4f6;}
.photo-empty{width:100%;aspect-ratio:1/1;display:flex;align-items:center;justify-content:center;background:#f3f4f6;color:#cbd5e1;font-size:13px;}
.body{padding:12px;display:flex;flex-direction:column;gap:8px;flex:1;}
.name{font-size:14px;font-weight:700;line-height:1.3;}
.art{font-size:12px;color:var(--text2);cursor:pointer;}
.brand{display:inline-block;background:#111827;color:#fff;font-size:10px;font-weight:700;padding:2px 7px;border-radius:8px;margin-right:5px;}
.prices{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap;}
.old{font-size:15px;color:var(--text3);text-decoration:line-through;}
.new{font-size:24px;font-weight:900;color:var(--red);}
.off{background:var(--red);color:#fff;font-size:13px;font-weight:800;padding:3px 9px;border-radius:8px;}
.stores{display:flex;flex-direction:column;gap:6px;margin-top:2px;border-top:1px solid var(--border);padding-top:8px;}
.srow{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap;font-size:12px;}
.slbl{color:var(--text2);white-space:nowrap;min-width:70px;}
.slbl b{color:var(--text);font-weight:800;}
.szs{display:flex;gap:4px;flex-wrap:wrap;}
.sz{background:#eef2ff;color:#3730a3;border-radius:6px;padding:2px 7px;font-size:12px;font-weight:600;}
.sz i{font-style:normal;color:#6366f1;font-weight:800;}
#loader{text-align:center;padding:40px;color:var(--text2);}
</style></head>
<body>
<div class="header"><h1>__TITLE__</h1><div class="sub">__COUNT__ моделей · __PAIRS__ шт · обновлено __DATE__</div></div>
<div class="bar">
  <input id="q" placeholder="Поиск по названию / артикулу…" oninput="render()">
  <button class="fbtn active" data-f="all" onclick="setF(this)">Все</button>
  <button class="fbtn" data-f="50" onclick="setF(this)">−50%+</button>
  <button class="fbtn" data-f="30" onclick="setF(this)">−30%+</button>
  <button class="fbtn" data-f="m" onclick="setF(this)">Москва</button>
  <button class="fbtn" data-f="t" onclick="setF(this)">ЦУМ+Онл</button>
  <button class="fbtn" data-f="a" onclick="setF(this)">Аружан</button>
</div>
<div id="loader">⏳ Загружаю…</div>
<div class="grid" id="grid"></div>
<script>
let ITEMS=[],PH={},F='all';
function fmt(n){return n.toLocaleString('ru-RU');}
function setF(b){document.querySelectorAll('.fbtn').forEach(x=>x.classList.remove('active'));b.classList.add('active');F=b.dataset.f;render();}
function render(){
  const q=(document.getElementById('q').value||'').toLowerCase();
  let list=ITEMS.filter(i=>{
    if(F==='50'&&i.off<50)return false;
    if(F==='30'&&i.off<30)return false;
    if(F==='m'&&!i.stock.m)return false;
    if(F==='t'&&!i.stock.t)return false;
    if(F==='a'&&!i.stock.a)return false;
    if(q&&!i.name.toLowerCase().includes(q)&&!i.article.toLowerCase().includes(q))return false;
    return true;
  });
  const srow=(lbl,tot,sizes)=>{
    if(!tot)return '';
    const sz=Object.entries(sizes||{}).map(([s,qq])=>`<span class="sz">${s}<i>×${qq}</i></span>`).join('');
    return `<div class="srow"><span class="slbl">${lbl} <b>${tot}</b></span><span class="szs">${sz}</span></div>`;
  };
  document.getElementById('grid').innerHTML=list.map(i=>{
    const ph=PH[i.article];
    const photo=ph?`<img class="photo" src="data:image/jpeg;base64,${ph}" loading="lazy">`:`<div class="photo-empty">нет фото</div>`;
    const ss=i.ss||{m:{},t:{},a:{},w:{}};
    const stores=srow('Москва',i.stock.m,ss.m)+srow('ЦУМ+Онл',i.stock.t,ss.t)+srow('Аружан',i.stock.a,ss.a)+srow('Склад',i.stock.w,ss.w);
    return `<div class="card">${photo}<div class="body">
      <div class="name">${i.name}</div>
      <div class="art" onclick="navigator.clipboard&&navigator.clipboard.writeText('${i.article}')"><span class="brand">${i.brand}</span>${i.article} 📋</div>
      <div class="prices"><span class="old">${fmt(i.old)}₸</span><span class="new">${fmt(i.new)}₸</span><span class="off">−${i.off}%</span></div>
      <div class="stores">${stores||'<span class="slbl">нет на точках</span>'}</div>
    </div></div>`;
  }).join('')||'<div id="loader">Ничего не найдено</div>';
}
async function load(){
  ITEMS=await (await fetch('__LITE__')).json();
  document.getElementById('loader').style.display='none';
  render();
  try{PH=await (await fetch('__PHOTOS__')).json();render();}catch(e){}
}
load();
</script></body></html>'''

if __name__ == '__main__':
    cfg = CONFIGS['cloth'] if '--clothing' in sys.argv else CONFIGS['shoe']
    main(cfg)
