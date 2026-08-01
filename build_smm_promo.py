#!/usr/bin/env python3
"""
СММ-лист состояния скидок перед 1+1=3 (smm_promo.html).
Две вкладки:
  🏷 НА СКИДКЕ — товары, у которых скидка ЕСТЬ (постить как акцию)
  ❌ СКИДКУ УБРАЛИ — 42 модели, у которых скидку сняли 01.08 (полная цена, НЕ постить как скидку)
Чистый лист для СММ: фото, артикул, размеры по складам, цена, % — без себеса/маржи.
"""
import duckdb, json, os
from pathlib import Path
ROOT = Path(__file__).parent.parent; SO = ROOT/'sneaker-order'; DB = ROOT/'data'/'pnlpower.duckdb'
def latest(con,p): return con.execute(f"SELECT MAX(table_name) FROM information_schema.tables WHERE table_name LIKE '{p}%'").fetchone()[0]

# 42 убранных
removed = set()
apf = Path('/private/tmp/claude-501/-Users-yerlankulumgariyev-Documents-pnlpower/01b8b74f-f9cc-4a73-ac38-e31e4990c229/scratchpad/undiscount_apply.json')
if apf.exists():
    removed = {p['article'] for p in json.loads(apf.read_text())}

con = duckdb.connect(str(DB), read_only=True)
snap = latest(con,'inventory_snapshot_stores_2026'); pr = latest(con,'prices_snapshot_2026')
rows = con.execute(f'''
  WITH p AS (SELECT article, MAX(sale_price) sp, MAX(NULLIF(new_price,0)) np FROM {pr} GROUP BY article),
  st AS (SELECT article, ANY_VALUE(product_name) nm,
         CAST(SUM(moscow) AS INT) m, CAST(SUM(tsum+online) AS INT) t,
         CAST(SUM(astana_aruzhan) AS INT) a, CAST(SUM(total_stock) AS INT) q,
         LIST(struct_pack(name:=product_name, mo:=moscow, tc:=tsum+online, ar:=astana_aruzhan)) rowsz
         FROM {snap} GROUP BY article)
  SELECT st.article, st.nm, CAST(p.sp AS DOUBLE), CAST(p.np AS DOUBLE), st.m, st.t, st.a, st.q, st.rowsz
  FROM p JOIN st USING(article)
  WHERE p.np IS NOT NULL AND p.np < p.sp AND st.q>0
''').fetchall()
con.close()

import re
ph = {}
for f in ['.photo_cache_sneakers.json','.photo_cache_clothing.json','.photo_cache_restock.json']:
    pf = SO/f
    if pf.exists():
        try: ph.update(json.loads(pf.read_text()))
        except Exception: pass

def sizes_str(rowsz, key):
    d = {}
    for r in rowsz:
        n = r['name']; qv = int(r[key] or 0)
        if qv>0:
            m = re.search(r',\s*([0-9.]+)\s*$', n)
            if m: d[m.group(1)] = d.get(m.group(1),0)+qv
    return ' '.join(f"{k.rstrip('.0') or k}×{v}" for k,v in sorted(d.items(), key=lambda x: float(x[0])))

on_sale, removed_list = [], []
for art,nm,sp,np,m,t,a,q,rowsz in rows:
    it = dict(article=art, name=re.sub(r',\s*[0-9.]+$','',nm), orig=round(sp), price=round(np),
              disc=round(100*(1-np/sp)) if sp else 0, m=m, t=t, a=a, q=q,
              szM=sizes_str(rowsz,'mo'), szC=sizes_str(rowsz,'tc'), szA=sizes_str(rowsz,'ar'))
    on_sale.append(it)

# 42 убранных — берём из apply-файла (полная цена)
if apf.exists():
    from collections import defaultdict
    byart = defaultdict(list)
    for p in json.loads(apf.read_text()): byart[p['article']].append(p)
    con = duckdb.connect(str(DB), read_only=True)
    for art, ps in byart.items():
        r = con.execute(f"SELECT ANY_VALUE(product_name), CAST(SUM(moscow) AS INT), CAST(SUM(tsum+online) AS INT), CAST(SUM(astana_aruzhan) AS INT), CAST(SUM(total_stock) AS INT) FROM {snap} WHERE article=?", [art]).fetchone()
        if not r or not r[4]: continue
        removed_list.append(dict(article=art, name=re.sub(r',\s*[0-9.]+$','',r[0]),
                                 price=round(ps[0]['orig']/100), m=r[1], t=r[2], a=r[3], q=r[4]))
    con.close()

on_sale = [i for i in on_sale if i['article'] not in removed]  # на скидке = без убранных
on_sale.sort(key=lambda x: -x['disc'])
removed_list.sort(key=lambda x: -x['q'])
photos = {i['article']: ph[i['article']] for i in on_sale+removed_list if ph.get(i['article'])}
(SO/'smm_promo_lite.json').write_text(json.dumps({'on_sale':on_sale,'removed':removed_list}, ensure_ascii=False))
(SO/'smm_promo_photos.json').write_text(json.dumps(photos, ensure_ascii=False))
print(f'на скидке: {len(on_sale)} | скидку убрали: {len(removed_list)} | фото: {len(photos)}')
