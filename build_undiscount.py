#!/usr/bin/env python3
"""
Дашборд СНЯТИЯ СКИДОК перед акцией 1+1=3 (undiscount.html).

Показывает все товары, у которых СЕЙЧАС стоит скидка (new_price < sale_price),
по приоритету «убрать скидку», чтобы под акцией 1+1=3 не уйти в минус
(акция дарит самый дешёвый из 3 → магазин съедает его полную себестоимость).

Приоритет:
  🔴 Критично — скидочная цена <= себес x1.1 (под акцией = убыток)
  🟠 Важно    — глубокая скидка (>=40%) и тонкая маржа (<30%)
  🟡 Логично  — хиты (>=4/мес): и так продаются, скидка дарит маржу
  ⚪ По желанию— лёгкая скидка

Карточка: фото, ~~изначальная~~ -> скидочная, себес, маржа, приоритет,
кнопка «❌ Убрать скидку» (вернуть изначальную) / «оставить».
Решения -> Supabase order UNDISCOUNT-001 (item.remove_discount = true/false).

    .venv/bin/python sneaker-order/build_undiscount.py [--skip-photos]
"""
import duckdb, json, os, re, sys, base64
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from scripts.utils.inventory_cost import get_inventory_cost

ROOT = Path(__file__).parent.parent
DB = ROOT / 'data' / 'pnlpower.duckdb'
SO = ROOT / 'sneaker-order'

def latest(con, pfx):
    return con.execute(f"SELECT MAX(table_name) FROM information_schema.tables WHERE table_name LIKE '{pfx}%'").fetchone()[0]

def main():
    con = duckdb.connect(str(DB), read_only=True)
    snap = latest(con, 'inventory_snapshot_stores_2026'); pr = latest(con, 'prices_snapshot_2026')
    cost = {r['article']: float(r['unit_cost'] or 0) for _,r in get_inventory_cost(con=con).iterrows()}
    rows = con.execute(f'''
      WITH p AS (SELECT article, MAX(sale_price) sp, MAX(NULLIF(new_price,0)) np FROM {pr} GROUP BY article),
      st AS (SELECT article, ANY_VALUE(product_name) nm,
             CAST(SUM(total_stock) AS INT) q, CAST(SUM(moscow) AS INT) m,
             CAST(SUM(tsum+online) AS INT) t, CAST(SUM(astana_aruzhan) AS INT) a,
             CAST(SUM(main_warehouse) AS INT) w FROM {snap} GROUP BY article),
      s AS (SELECT article, CAST(SUM(quantity) AS INT) s30 FROM retaildemand_positions
            WHERE price>0 AND document_moment>=CURRENT_DATE-INTERVAL 30 DAY GROUP BY article)
      SELECT st.article, st.nm, CAST(p.sp AS DOUBLE), CAST(p.np AS DOUBLE),
             st.q, st.m, st.t, st.a, st.w, COALESCE(s.s30,0)
      FROM p JOIN st USING(article) LEFT JOIN s USING(article)
      WHERE p.np IS NOT NULL AND p.np < p.sp AND st.q>0
    ''').fetchall()
    con.close()

    ph = {}
    for f in ['.photo_cache_sneakers.json','.photo_cache_clothing.json','.photo_cache_restock.json']:
        pf = SO / f
        if pf.exists():
            try: ph.update(json.loads(pf.read_text()))
            except Exception: pass

    SHLAK = re.compile(r'пакет|сертификат|подарочн|доставк|package|коробк', re.I)
    items = []
    for art,nm,sp,np,q,m,t,a,w,s30 in rows:
        if SHLAK.search(str(nm)) or np < 100:   # шлак/сертификаты/копеечные заглушки
            continue
        c = cost.get(art,0) or 0
        disc = round(100*(1-np/sp)) if sp else 0
        marg = round((np-c)/np*100) if np>0 and c>0 else 999
        # приоритет
        if c>0 and np <= c*1.1: tier, tname, prio = 'CRIT','🔴 Критично', 0
        elif disc>=40 and marg<30: tier, tname, prio = 'IMP','🟠 Важно', 1
        elif s30>=4: tier, tname, prio = 'HOT','🟡 Логично (хит)', 2
        else: tier, tname, prio = 'OPT','⚪ По желанию', 3
        # потеря маржи под акцией если оставить скидку (грубо: подаренная себес-разница на скидочной единице)
        loss = round((c - np)) if np < c else 0
        base = re.sub(r',\s*[0-9.]+$','', nm or '')  # без размера — для фото по имени
        items.append(dict(article=art, name=nm, base=base, orig=round(sp), disc_price=round(np),
                          disc=disc, cost=round(c), margin=marg, stock=q,
                          m=m, t=t, a=a, w=w, s30=s30, tier=tier, tname=tname, prio=prio,
                          loss_per_unit=loss,
                          recommend_remove = tier in ('CRIT','IMP','HOT')))
    # сортировка: приоритет, внутри — по «убыточности» (маржа asc), потом сток desc
    items.sort(key=lambda x: (x['prio'], x['margin'], -x['stock']))

    # фото по артикулу (кэши по артикулу)
    photos = {it['article']: ph[it['article']] for it in items if ph.get(it['article'])}
    (SO/'undiscount_lite.json').write_text(json.dumps(items, ensure_ascii=False))
    (SO/'undiscount_photos.json').write_text(json.dumps(photos, ensure_ascii=False))

    from collections import Counter
    tc = Counter(it['tier'] for it in items)
    print(f'товаров на скидке: {len(items)} | фото: {len(photos)}')
    print('  CRIT %d | IMP %d | HOT %d | OPT %d' % (tc['CRIT'], tc['IMP'], tc['HOT'], tc['OPT']))
    return items

if __name__ == '__main__':
    main()
