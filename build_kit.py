#!/usr/bin/env python3
"""
Дашборд ПРОДАЖ одежды KIT (Китай) — kit.html для GitHub Pages.

KIT-линия = товары с суффиксом «KIT (» в имени (600xxx, но фильтр по ИМЕНИ —
диапазон 600xxx захватывает старую одежду GO/комбинезоны). Агрегация по артикулу
(= модель+цвет), размеры суммируются. Метрики как у обуви velocity, но для новинок
темп считается от окна ЖИЗНИ (дней с первой поставки), а не от календарных 30 дней.

Карточки с фото (из МС), вкладки по статусу, сортировка, поиск. По каждой модели:
темп/нед, WOS, продано (30д/всё), остатки М/ЦУМ/Аружан/склад, маржа, себес→розница.

    python sneaker-order/build_kit.py
"""
import base64
import json
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

import duckdb

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
from scripts.utils.inventory_cost import get_inventory_cost  # noqa: E402

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass
import requests  # noqa: E402

DB = ROOT / "data" / "pnlpower.duckdb"
OUT = HERE / "kit.html"
PHOTO_CACHE = HERE / ".photo_cache_kit.json"
BASE_URL = "https://api.moysklad.ru/api/remap/1.2"

# грубая сезонность (для WOS-проекции не критично, одежда осень-зима)
TODAY = date.today()


def latest(con, prefix):
    return con.execute(
        f"SELECT table_name FROM information_schema.tables WHERE table_name LIKE '{prefix}%' "
        f"ORDER BY table_name DESC LIMIT 1").fetchone()[0]


def load_cache():
    if PHOTO_CACHE.exists():
        return json.loads(PHOTO_CACHE.read_text())
    return {}


def save_cache(c):
    PHOTO_CACHE.write_text(json.dumps(c, ensure_ascii=False))


def fetch_photos(articles, cache):
    token = os.getenv("MOYSKLAD_TOKEN") or os.getenv("MS_TOKEN")
    if not token:
        print("   ⚠️ нет токена — фото пропущены"); return cache
    H = {"Authorization": f"Bearer {token}", "Accept-Encoding": "gzip"}
    todo = [a for a in articles if a not in cache]
    print(f"   Скачать фото: {len(todo)} (в кеше {len(cache)})")
    got = 0
    for i, art in enumerate(todo, 1):
        try:
            r = requests.get(f"{BASE_URL}/entity/product", headers=H,
                             params={"filter": f"article={art}", "limit": 5, "expand": "images"}, timeout=10)
            rows = r.json().get("rows", []) if r.status_code == 200 else []
            # берём первую карточку С фото
            img_href = None
            for p in rows:
                if p.get("images", {}).get("meta", {}).get("size", 0) > 0:
                    img_href = p["images"]["meta"]["href"]; break
            if not img_href:
                cache[art] = ""; continue
            r2 = requests.get(img_href, headers=H, timeout=10)
            imgs = r2.json().get("rows", []) if r2.status_code == 200 else []
            if not imgs:
                cache[art] = ""; continue
            mini = imgs[0].get("miniature", {}).get("href") or imgs[0].get("tiny", {}).get("href")
            if not mini:
                cache[art] = ""; continue
            r3 = requests.get(mini, headers=H, timeout=10)
            cache[art] = base64.b64encode(r3.content).decode("ascii") if r3.status_code == 200 else ""
            if cache[art]:
                got += 1
            if i % 15 == 0:
                print(f"      {i}/{len(todo)} …"); save_cache(cache)
        except Exception:
            cache[art] = ""
    save_cache(cache)
    print(f"   Скачано новых: {got}")
    return cache


CAT_ORDER = ["Флиска", "Ветровка", "Олимпийка", "Бомбер", "Куртка/Пуховик", "Спорткостюм",
             "Футболка", "Свитшот", "Худи", "Джинсы", "Джоггеры", "Лонгслив", "Другое"]


def category(name):
    n = name.lower()
    if "флис" in n or "кофта" in n: return "Флиска"
    if "ветровка" in n: return "Ветровка"
    if "олимпийка" in n: return "Олимпийка"
    if "бомбер" in n: return "Бомбер"
    if "пуховик" in n or "куртка" in n or "дутая" in n: return "Куртка/Пуховик"
    if "спорткостюм" in n or "костюм" in n: return "Спорткостюм"
    if "футболка" in n: return "Футболка"
    if "свитшот" in n: return "Свитшот"
    if "худи" in n or "балахон" in n: return "Худи"
    if "джинсы" in n: return "Джинсы"
    if "джоггер" in n or "джогер" in n: return "Джоггеры"
    if "лонг" in n: return "Лонгслив"
    return "Другое"


def build():
    con = duckdb.connect(str(DB), read_only=True)
    snap = latest(con, "inventory_snapshot_stores_")
    snap_date = snap.split("_")[-1]

    df = get_inventory_cost()
    cost = {str(r.article): float(r.unit_cost or 0) for r in df.itertuples()}

    # остатки по артикулу (KIT по имени), + базовое имя/цвет + размеры
    stock = con.execute(f"""
        SELECT article,
               ANY_VALUE(product_name) AS pn,
               SUM(moscow) m, SUM(tsum)+SUM(online) t, SUM(astana_aruzhan) a,
               SUM(main_warehouse)+SUM(baitursynova) wh, SUM(total_stock) tot
        FROM {snap}
        WHERE product_name LIKE '%KIT (%' AND total_stock > 0
        GROUP BY article
    """).fetchall()

    # продажи по артикулу
    sales = con.execute("""
        SELECT article,
               SUM(CASE WHEN sale_datetime >= (SELECT MAX(sale_datetime) FROM v_sales_canonical)-INTERVAL 30 DAY THEN quantity ELSE 0 END) s30,
               SUM(CASE WHEN sale_datetime >= (SELECT MAX(sale_datetime) FROM v_sales_canonical)-INTERVAL 7 DAY THEN quantity ELSE 0 END) s7,
               SUM(quantity) sall, SUM(revenue) rev,
               MIN(sale_datetime) first_sale, MAX(sale_datetime) last_sale
        FROM v_sales_canonical WHERE product_name LIKE '%KIT (%' AND price > 0
        GROUP BY article
    """).fetchall()
    sd = {str(a): dict(s30=int(s30 or 0), s7=int(s7 or 0), sall=int(sl or 0), rev=float(rev or 0),
                       first=fs, last=ls) for a, s30, s7, sl, rev, fs, ls in sales}

    # первая поставка (для окна жизни новинок)
    fsup = con.execute("""
        SELECT product_article art, MIN(DATE(supply_moment)) d
        FROM supply_positions WHERE product_article IS NOT NULL GROUP BY product_article
    """).fetchall()
    first_supply = {str(a): d for a, d in fsup}
    con.close()

    rows = []
    for art, pn, m, t, a, wh, tot in stock:
        art = str(art)
        m, t, a, wh, tot = [int(x or 0) for x in (m, t, a, wh, tot)]
        base = pn.rsplit(" (", 1)[0] if " (" in pn else pn
        color = ""
        mm = re.search(r"\(([^,]+),", pn)
        if mm:
            color = mm.group(1).strip()
        uc = cost.get(art, 0)
        s = sd.get(art, {})
        s30, s7, sall, rev = s.get("s30", 0), s.get("s7", 0), s.get("sall", 0), s.get("rev", 0.0)
        store_stock = m + t + a

        # окно жизни: с первой поставки (или первой продажи), но не старше 30д для темпа
        fs_supply = first_supply.get(art)
        life_start = fs_supply
        if life_start is None and s.get("first"):
            life_start = s["first"].date() if hasattr(s["first"], "date") else s["first"]
        days_live = (TODAY - life_start).days if life_start else 30
        days_live = max(days_live, 1)
        window = min(30, max(days_live, 7))
        # продажи в окне ~ sall если товар моложе окна, иначе s30
        sold_window = sall if days_live <= 30 else s30
        rate = round(sold_window / window * 7, 1)      # шт/нед
        wos = round(store_stock / rate, 1) if rate > 0 else None

        retail = round(rev / sall) if sall > 0 else 0
        # если не продавался — розница из карточки не в этом запросе; оценим по себесу×4 маркер
        margin = round((retail - uc) / retail * 100) if retail > 0 else None

        is_new = days_live <= 12
        if is_new and sall == 0:
            st = "new"
        elif sall == 0:
            st = "dead"
        elif rate <= 0:
            st = "dead"
        elif wos is not None and wos < 4:
            st = "hot"
        elif wos is not None and wos <= 10:
            st = "ok"
        elif wos is not None and wos <= 20:
            st = "slow"
        else:
            st = "over"

        rows.append(dict(a=art, base=base, color=color, cat=category(base),
                         m=m, t=t, ar=a, wh=wh, ss=store_stock, tot=tot,
                         s30=s30, s7=s7, sall=sall, rate=rate,
                         wos=wos if wos is not None else 999,
                         cost=int(uc), retail=retail, margin=margin,
                         days=days_live, st=st,
                         last=str(s["last"])[:10] if s.get("last") else None))

    # фото
    cache = load_cache()
    cache = fetch_photos([r["a"] for r in rows], cache)
    for r in rows:
        r["ph"] = cache.get(r["a"], "")

    rows.sort(key=lambda r: (-r["sall"], -(r["rate"])))
    return rows, snap_date


SMETA = {
    "hot":  ("🔥", "Продаётся", "#E8722B"),
    "ok":   ("🟢", "Норма",     "#2E9E5B"),
    "slow": ("🟡", "Медленно",  "#C98A16"),
    "over": ("🔴", "Стоит",     "#C0453A"),
    "dead": ("⚫", "Нет продаж", "#5B6470"),
    "new":  ("🆕", "Новинка",   "#2478B5"),
}


def render(rows, snap):
    withph = sum(1 for r in rows if r["ph"])
    tot_stock = sum(r["ss"] for r in rows)
    tot_sold = sum(r["sall"] for r in rows)
    frozen = sum(r["ss"] * r["cost"] for r in rows)
    cnt = {}
    for r in rows:
        cnt[r["st"]] = cnt.get(r["st"], 0) + 1
    maxsold = max([r["sall"] for r in rows] + [1])
    smeta = {k: dict(e=v[0], l=v[1], c=v[2]) for k, v in SMETA.items()}
    payload = json.dumps(dict(rows=rows, smeta=smeta, maxsold=maxsold), ensure_ascii=False)

    return f"""<!doctype html><html lang=ru><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<meta http-equiv="Cache-Control" content="no-store">
<title>Продажи одежды KIT</title>
<style>
:root{{--ground:#F6F5F3;--surface:#FFF;--ink:#191C1A;--muted:#6C716D;--line:#E6E5E1;
--accent:#7A4E2E;--good:#2E9E5B;--warn:#C98A16;--bad:#C0453A;--chip:#EEEDE9;
--shadow:0 1px 2px rgba(20,20,15,.05),0 5px 16px rgba(20,20,15,.06);}}
@media(prefers-color-scheme:dark){{:root{{--ground:#12110F;--surface:#1B1A17;--ink:#ECE9E3;--muted:#9A968E;
--line:#2A2823;--accent:#C89468;--good:#4CB878;--warn:#D9A93F;--bad:#E0685C;--chip:#252320;
--shadow:0 1px 2px rgba(0,0,0,.3),0 8px 22px rgba(0,0,0,.4);}}}}
:root[data-theme=light]{{--ground:#F6F5F3;--surface:#FFF;--ink:#191C1A;--muted:#6C716D;--line:#E6E5E1;--accent:#7A4E2E;--good:#2E9E5B;--warn:#C98A16;--bad:#C0453A;--chip:#EEEDE9;}}
:root[data-theme=dark]{{--ground:#12110F;--surface:#1B1A17;--ink:#ECE9E3;--muted:#9A968E;--line:#2A2823;--accent:#C89468;--good:#4CB878;--warn:#D9A93F;--bad:#E0685C;--chip:#252320;}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--ground);color:var(--ink);font-family:-apple-system,"SF Pro Text",system-ui,Roboto,sans-serif;-webkit-font-smoothing:antialiased;font-variant-numeric:tabular-nums;}}
.wrap{{max-width:1000px;margin:0 auto;padding:20px 12px 60px;}}
h1{{font-size:22px;font-weight:800;letter-spacing:-.02em;margin:0 0 3px;}}
.sub{{color:var(--muted);font-size:12.5px;margin:0 0 16px;}}
.kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:9px;margin-bottom:16px;}}
.kpi{{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:11px 13px;box-shadow:var(--shadow);}}
.kpi .l{{font-size:10.5px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);font-weight:700;}}
.kpi .v{{font-size:22px;font-weight:800;margin-top:2px;}}
.kpi .v small{{font-size:12px;font-weight:600;color:var(--muted);}}
.ctrl{{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-bottom:14px;position:sticky;top:0;background:var(--ground);padding:6px 0;z-index:5;}}
.tab{{border:1px solid var(--line);background:var(--surface);color:var(--ink);border-radius:999px;padding:6px 11px;font-size:12.5px;font-weight:600;cursor:pointer;}}
.tab.on{{background:var(--accent);color:#fff;border-color:var(--accent);}}
input,select{{border:1px solid var(--line);background:var(--surface);color:var(--ink);border-radius:9px;padding:7px 10px;font-size:13px;font-family:inherit;}}
input{{flex:1;min-width:110px;}}
.card{{display:grid;grid-template-columns:72px 1fr;gap:12px;background:var(--surface);border:1px solid var(--line);border-left:4px solid var(--sc,var(--line));border-radius:13px;padding:10px;margin-bottom:9px;box-shadow:var(--shadow);}}
.ph{{width:72px;height:96px;border-radius:9px;object-fit:cover;background:var(--chip);}}
.no{{width:72px;height:96px;border-radius:9px;background:var(--chip);display:flex;align-items:center;justify-content:center;font-size:26px;}}
.body{{min-width:0;}}
.top{{display:flex;justify-content:space-between;gap:8px;align-items:baseline;}}
.nm{{font-size:14px;font-weight:700;line-height:1.2;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}}
.sold{{text-align:right;white-space:nowrap;}}
.sold b{{font-size:19px;font-weight:800;}}.sold small{{font-size:10px;color:var(--muted);}}
.chips{{display:flex;flex-wrap:wrap;gap:5px;margin-top:5px;align-items:center;}}
.chip{{font-size:11px;font-weight:700;padding:2px 8px;border-radius:999px;color:#fff;}}
.bar{{height:6px;background:var(--chip);border-radius:3px;margin:8px 0 2px;overflow:hidden;}}
.bar i{{display:block;height:100%;background:var(--accent);}}
.line{{font-size:12.5px;color:var(--muted);margin-top:5px;line-height:1.4;}}
.line .lbl{{display:inline-block;min-width:74px;font-weight:700;font-size:11px;text-transform:uppercase;}}
.line b{{color:var(--ink);}}
.mrow{{font-size:11.5px;color:var(--muted);margin-top:6px;display:flex;flex-wrap:wrap;gap:4px 10px;}}
.mrow b{{color:var(--ink);}}
.note{{font-size:12px;color:var(--muted);margin:14px 2px 0;line-height:1.5;}}
</style></head><body><div class=wrap>
<h1>🧵 Продажи одежды KIT — как расходится</h1>
<p class=sub>Снапшот {snap} · KIT-линия (Китай) · каждый цвет-модель отдельно · темп = шт/нед от начала продаж · WOS = недель хватит</p>
<div class=kpis>
<div class=kpi><div class=l>Моделей</div><div class=v>{len(rows)}</div></div>
<div class=kpi><div class=l>В стоке</div><div class=v>{tot_stock}<small> шт</small></div></div>
<div class=kpi><div class=l>Продано всего</div><div class=v>{tot_sold}<small> шт</small></div></div>
<div class=kpi><div class=l>Заморожено</div><div class=v>{frozen/1e6:.1f}<small> М₸</small></div></div>
<div class=kpi><div class=l>🔥 Продаётся</div><div class=v>{cnt.get('hot',0)}</div></div>
<div class=kpi><div class=l>⚫🔴 Стоят</div><div class=v>{cnt.get('dead',0)+cnt.get('over',0)}</div></div>
</div>
<div class=ctrl>
<button class="tab on" data-f=all>Все</button>
<button class=tab data-f=hot>🔥 Продаётся</button>
<button class=tab data-f=ok>🟢 Норма</button>
<button class=tab data-f=slow>🟡 Медленно</button>
<button class=tab data-f=over>🔴 Стоит</button>
<button class=tab data-f=dead>⚫ Нет продаж</button>
<button class=tab data-f=new>🆕 Новинки</button>
<select id=sort><option value=sall>продано ↓</option><option value=rate>темп ↓</option><option value=ss>сток ↓</option><option value=wos>WOS ↓</option></select>
<input id=q placeholder="поиск модели / цвета / артикула…">
</div>
<div id=list></div>
<p class=note>Фото {withph}/{len(rows)}. Партия KIT свежая (первая 20.07, вторая 06.08) — темп считается от начала продаж каждой модели, а не за календарный месяц. «Стоит» = в стоке, но продаж почти нет.</p>
</div>
<script>
var D={payload};
var F='all',SB='sall',Q='';
var el=document.getElementById('list');
function draw(){{
 var r=D.rows.filter(function(x){{
  if(F!='all'&&x.st!=F)return false;
  if(Q){{if((x.base+' '+x.color+' '+x.a).toLowerCase().indexOf(Q)<0)return false;}}
  return true;}});
 r.sort(function(a,b){{return (b[SB]||0)-(a[SB]||0);}});
 var h='';
 r.forEach(function(x){{
  var sm=D.smeta[x.st];
  var wf=x.wos>=999?'∞':x.wos;
  var wc=x.wos>=999?'var(--bad)':(x.wos<4?'var(--warn)':(x.wos<=10?'var(--good)':(x.wos<=20?'var(--warn)':'var(--bad)')));
  var w=Math.min(100,Math.round(x.sall/D.maxsold*100));
  var ph=x.ph?'<img class=ph src="data:image/jpeg;base64,'+x.ph+'">':'<div class=no>👕</div>';
  var mrg=x.margin==null?'':'<span>маржа <b>'+x.margin+'%</b></span>';
  var last=x.last?'<span>последняя '+x.last.slice(5)+'</span>':'';
  h+='<div class=card style="--sc:'+sm.c+'">'+ph+'<div class=body>'
   +'<div class=top><div class=nm>'+x.base+' · '+x.color+'</div><div class=sold><b>'+x.sall+'</b> <small>продано</small></div></div>'
   +'<div class=chips><span class=chip style="background:'+sm.c+'">'+sm.e+' '+sm.l+'</span><span style="font-size:11px;color:var(--muted)">'+x.a+' · '+x.cat+'</span></div>'
   +'<div class=bar><i style="width:'+w+'%"></i></div>'
   +'<div class=line><span class=lbl>🛒 Продажи</span>всего <b>'+x.sall+' шт</b> · за 30д <b>'+x.s30+'</b> · темп <b>'+x.rate+'/нед</b></div>'
   +'<div class=line><span class=lbl>📦 Сток</span><b>'+x.ss+' шт</b>, хватит <b style="color:'+wc+'">'+wf+' нед</b> — М '+x.m+' · Ц+О '+x.t+' · А '+x.ar+(x.wh?' · скл '+x.wh:'')+'</div>'
   +'<div class=mrow><span>себес <b>'+x.cost.toLocaleString()+'₸</b></span>'+(x.retail?'<span>розница <b>'+x.retail.toLocaleString()+'₸</b></span>':'')+mrg+last+'</div>'
   +'</div></div>';
 }});
 el.innerHTML=h||'<p class=note>Ничего не найдено.</p>';
}}
document.querySelectorAll('.tab').forEach(function(t){{t.onclick=function(){{document.querySelectorAll('.tab').forEach(function(z){{z.classList.remove('on')}});t.classList.add('on');F=t.dataset.f;draw();}};}});
document.getElementById('sort').onchange=function(e){{SB=e.target.value;draw();}};
document.getElementById('q').oninput=function(e){{Q=e.target.value.toLowerCase();draw();}};
draw();
</script></body></html>"""


def main():
    rows, snap = build()
    html = render(rows, snap)
    OUT.write_text(html)
    print(f"\n✓ kit.html: {len(rows)} моделей, {len(html)/1e6:.1f} МБ")
    hot = sum(1 for r in rows if r["st"] == "hot")
    print(f"  Продано всего: {sum(r['sall'] for r in rows)} шт | в стоке {sum(r['ss'] for r in rows)} | 🔥 {hot}")


if __name__ == "__main__":
    main()
