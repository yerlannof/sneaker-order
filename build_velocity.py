#!/usr/bin/env python3
"""
Дашборд СКОРОСТИ ПРОДАЖ обуви (velocity.html) — «ситуация на сегодня» для Алуа.

Переиспользует данные build_sneakers (фото из кэша + все метрики: WOS, ⭐скор,
💰GMROI, 🔍W1 vs сейчас, 📅дата распродажи). НЕ считает заново — единый источник.

Карточки с фото, сортировка по темпу/WOS/стоку/скору, вкладки по статусу скорости:
  🔥 горит (дозаказ) / 🟢 норма / 🟡 замедляется / 🔴 затарен / ⚫ мёртвый / 🆕 новинка

    python sneaker-order/build_velocity.py            # собрать velocity.html
"""
import os
import re
import sys
import json
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
os.environ.setdefault("SKIP_PHOTOS", "1")   # фото из кэша, не тянуть заново

import build_sneakers  # noqa: E402

OUT = HERE / "velocity.html"


def vstatus(it):
    s30 = it["sales"]["s30"]
    wf = it.get("wos_future", 999) or 999
    if it.get("category") == "NEW":
        return "new"
    if s30 == 0:
        return "dead"
    if wf <= 2.5:
        return "hot"
    if wf <= 8:
        return "ok"
    if wf <= 16:
        return "slow"
    return "over"


SMETA = {
    "hot":  ("🔥", "Горит — дозаказ", "#E8722B"),
    "ok":   ("🟢", "Норма",          "#2E9E5B"),
    "slow": ("🟡", "Замедляется",     "#C98A16"),
    "over": ("🔴", "Затарен",        "#C0453A"),
    "dead": ("⚫", "Мёртвый",        "#5B6470"),
    "new":  ("🆕", "Новинка",        "#2478B5"),
}
INTENTIONAL_BRANDS = {"Puma", "New Balance", "Asics"}


def main():
    items = build_sneakers.main()

    lite = []
    for it in items:
        s30 = it["sales"]["s30"]
        s90 = it["sales"]["s90"]
        rate = round(s30 * 7 / 30.0, 1)
        st = vstatus(it)
        stock = it["stock"]
        name = re.sub(r",\s*\d+(?:\.\d+)?\s*$", "", it["name"]).strip()  # срезать ", 36"
        lite.append(dict(
            a=it["article"], n=name, br=it.get("brand", ""),
            ph=it.get("photo", ""),
            m=stock["moscow"], t=stock["tsum_online"], ar=stock["aruzhan"],
            wh=stock["warehouse"], tot=stock["total"],
            s30=s30, s90=s90, rate=rate,
            wos=round(it.get("wos", 999) or 999, 1),
            wosf=round(it.get("wos_future", 999) or 999, 1),
            gm=it.get("gmroi", 0), sc=it.get("score", 0),
            mrg=round(it.get("margin_pct", 0)),
            w1=it.get("w1", 0), vs=it.get("vs_start_pct"),
            sell=it.get("sellout_date"), disc=it.get("cur_disc", 0),
            retail=int(it.get("retail", 0)), st=st,
            intn=it.get("brand") in INTENTIONAL_BRANDS,
        ))

    # сеть-метрики (без намеренного неликвида)
    act = [r for r in lite if not r["intn"]]
    tot_rate = sum(r["rate"] for r in act)
    tot_stock = sum(r["tot"] for r in act)
    net_wos = round(tot_stock / tot_rate, 1) if tot_rate else 0
    cnt = {}
    for r in act:
        cnt[r["st"]] = cnt.get(r["st"], 0) + 1
    maxrate = max([r["rate"] for r in lite] + [1])
    snap = build_sneakers.SNAPSHOT_DATE or "сегодня"

    html = render(lite, dict(tot_rate=tot_rate, tot_stock=tot_stock, net_wos=net_wos,
                             cnt=cnt, maxrate=maxrate), snap)
    OUT.write_text(html)
    mb = len(html) / 1e6
    print(f"\n✓ velocity.html: {len(lite)} моделей, {mb:.1f} МБ")
    print(f"  Сеть: {tot_rate:.0f} пар/нед | сток {tot_stock} | WOS {net_wos}")
    print(f"  Статусы: {cnt}")


def render(rows, s, snap):
    smeta = {k: dict(e=v[0], l=v[1], c=v[2]) for k, v in SMETA.items()}
    payload = json.dumps(dict(rows=rows, smeta=smeta, maxrate=s["maxrate"]), ensure_ascii=False)
    wos_cls = "bad" if s["net_wos"] > 14 else ("warn" if s["net_wos"] > 10 else "good")
    c = s["cnt"]
    return f"""<!doctype html><html lang=ru><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<meta http-equiv="Cache-Control" content="no-store">
<title>Скорость продаж обуви</title>
<style>
:root{{--ground:#F7F7F5;--surface:#FFF;--ink:#18201C;--muted:#6B716D;--line:#E7E7E3;
--accent:#0F766E;--good:#2E9E5B;--warn:#C98A16;--bad:#C0453A;--chip:#EFEFEC;
--shadow:0 1px 2px rgba(20,25,20,.05),0 5px 16px rgba(20,25,20,.06);}}
@media(prefers-color-scheme:dark){{:root{{--ground:#101311;--surface:#191D1A;--ink:#EAEEE9;--muted:#98A09A;
--line:#272C28;--accent:#3FBFB0;--good:#4CB878;--warn:#D9A93F;--bad:#E0685C;--chip:#232823;
--shadow:0 1px 2px rgba(0,0,0,.3),0 8px 22px rgba(0,0,0,.4);}}}}
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
.kpi.good .v{{color:var(--good)}}.kpi.warn .v{{color:var(--warn)}}.kpi.bad .v{{color:var(--bad)}}
.ctrl{{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-bottom:14px;position:sticky;top:0;background:var(--ground);padding:6px 0;z-index:5;}}
.tab{{border:1px solid var(--line);background:var(--surface);color:var(--ink);border-radius:999px;padding:6px 11px;font-size:12.5px;font-weight:600;cursor:pointer;}}
.tab.on{{background:var(--accent);color:#fff;border-color:var(--accent);}}
input,select{{border:1px solid var(--line);background:var(--surface);color:var(--ink);border-radius:9px;padding:7px 10px;font-size:13px;font-family:inherit;}}
input{{flex:1;min-width:110px;}}
.card{{display:grid;grid-template-columns:70px 1fr;gap:12px;background:var(--surface);border:1px solid var(--line);border-left:4px solid var(--sc,var(--line));border-radius:13px;padding:10px;margin-bottom:9px;box-shadow:var(--shadow);}}
.ph{{width:70px;height:70px;border-radius:9px;object-fit:cover;background:var(--chip);}}
.no{{width:70px;height:70px;border-radius:9px;background:var(--chip);display:flex;align-items:center;justify-content:center;font-size:22px;}}
.body{{min-width:0;}}
.top{{display:flex;justify-content:space-between;gap:8px;align-items:baseline;}}
.nm{{font-size:14px;font-weight:700;line-height:1.2;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}}
.rate{{text-align:right;white-space:nowrap;}}
.rate b{{font-size:19px;font-weight:800;}}.rate small{{font-size:10px;color:var(--muted);}}
.chips{{display:flex;flex-wrap:wrap;gap:5px;margin-top:5px;align-items:center;}}
.chip{{font-size:11px;font-weight:700;padding:2px 8px;border-radius:999px;color:#fff;}}
.bar{{height:6px;background:var(--chip);border-radius:3px;margin:7px 0 2px;overflow:hidden;}}
.bar i{{display:block;height:100%;background:var(--accent);}}
.line{{font-size:12.5px;color:var(--muted);margin-top:5px;line-height:1.4;}}
.line .lbl{{display:inline-block;min-width:96px;font-weight:700;font-size:11px;text-transform:uppercase;letter-spacing:.02em;}}
.line b{{color:var(--ink);font-weight:700;}}
.sold .lbl{{color:var(--accent);}}
.sold b{{font-size:13.5px;}}
.mrow{{font-size:11.5px;color:var(--muted);margin-top:6px;display:flex;flex-wrap:wrap;gap:4px 10px;}}
.mrow b{{color:var(--ink);}}
.note{{font-size:12px;color:var(--muted);margin:14px 2px 0;line-height:1.5;}}
</style></head><body><div class=wrap>
<h1>⚡ Скорость продаж обуви — ситуация на сегодня</h1>
<p class=sub>Снапшот {snap} · темп = пар/нед (30д) · WOS буд. = недель хватит с учётом сезона · ⭐ скор 0-100 · 💰 GMROI ₸/₸·мес · 🔍 темп сейчас vs старт(W1)</p>
<div class=kpis>
<div class=kpi><div class=l>Темп сети</div><div class=v>{s['tot_rate']:.0f}<small> /нед</small></div></div>
<div class=kpi><div class=l>Живой сток</div><div class=v>{s['tot_stock']:,}<small> пар</small></div></div>
<div class="kpi {wos_cls}"><div class=l>Сеть WOS</div><div class=v>{s['net_wos']:.0f}<small> нед</small></div></div>
<div class="kpi warn"><div class=l>🔥 Горят</div><div class=v>{c.get('hot',0)}</div></div>
<div class="kpi bad"><div class=l>🔴+⚫ Затар/мёртв</div><div class=v>{c.get('over',0)+c.get('dead',0)}</div></div>
<div class=kpi><div class=l>🆕 Новинки</div><div class=v>{c.get('new',0)}</div></div>
</div>
<div class=ctrl>
<button class="tab on" data-f=all>Все</button>
<button class=tab data-f=hot>🔥 Горят</button>
<button class=tab data-f=ok>🟢 Норма</button>
<button class=tab data-f=slow>🟡 Замедл.</button>
<button class=tab data-f=over>🔴 Затарены</button>
<button class=tab data-f=dead>⚫ Мёртвые</button>
<button class=tab data-f=new>🆕 Новинки</button>
<select id=sort><option value=rate>темп ↓</option><option value=wosf>WOS ↓</option><option value=tot>сток ↓</option><option value=sc>⭐ скор ↓</option><option value=gm>GMROI ↓</option></select>
<input id=q placeholder="поиск модели / артикула…">
</div>
<div id=list></div>
<p class=note>Puma / New Balance / Asics в метрики сети не входят (намеренно на складе). ⭐ скор — сводная сила модели (темп+тренд+маржа+свежесть). 🔍 vs старт: &gt;100% ускорилась, &lt;100% остыла.</p>
</div>
<script>
var D={payload};
var F='all',SB='rate',Q='';
var el=document.getElementById('list');
function draw(){{
 var r=D.rows.filter(function(x){{
  if(F!='all'&&x.st!=F)return false;
  if(Q){{if((x.n+' '+x.a).toLowerCase().indexOf(Q)<0)return false;}}
  return true;}});
 r.sort(function(a,b){{return (b[SB]||0)-(a[SB]||0);}});
 var h='';
 r.forEach(function(x){{
  var sm=D.smeta[x.st];
  var wf=x.wosf>=999?'∞':x.wosf;
  var wc=x.wosf>=999?'var(--bad)':(x.wosf<2.5?'var(--warn)':(x.wosf<=8?'var(--good)':(x.wosf<=16?'var(--warn)':'var(--bad)')));
  var w=Math.min(100,Math.round(x.rate/D.maxrate*100));
  var ph=x.ph?'<img class=ph src="data:image/jpeg;base64,'+x.ph+'">':'<div class=no>👟</div>';
  var disc=x.disc>0?'<span class=chip style="background:var(--bad)">−'+x.disc+'%</span>':'';
  var vs=x.vs==null?'':'<span>vs старт <b>'+x.vs+'%</b></span>';
  var sell=x.sell?'<span>📅 распродажа '+x.sell.slice(5)+'</span>':'';
  h+='<div class=card style="--sc:'+sm.c+'">'+ph+'<div class=body>'
   +'<div class=top><div class=nm>'+x.n+'</div><div class=rate><b>'+x.rate+'</b> <small>пар/нед</small></div></div>'
   +'<div class=chips><span class=chip style="background:'+sm.c+'">'+sm.e+' '+sm.l+'</span>'+disc+'<span style="font-size:11px;color:var(--muted)">'+x.a+' · '+x.br+'</span></div>'
   +'<div class=bar><i style="width:'+w+'%"></i></div>'
   +'<div class="line sold"><span class=lbl>🛒 Продано</span>за 30 дней <b>'+x.s30+' пар</b> · за 90 дней <b>'+x.s90+'</b> · темп <b>'+x.rate+'/нед</b></div>'
   +'<div class=line><span class=lbl>📦 Запас</span><b>'+x.tot+' пар</b>, хватит на <b style="color:'+wc+'">'+wf+' нед</b> — М '+x.m+' · Ц+О '+x.t+' · А '+x.ar+(x.wh?' · скл '+x.wh:'')+'</div>'
   +'<div class=mrow><span>⭐ скор <b>'+x.sc+'</b></span><span>маржа <b>'+x.mrg+'%</b></span><span>GMROI <b>'+x.gm+'</b></span>'+vs+sell+'</div>'
   +'</div></div>';
 }});
 el.innerHTML=h||'<p class=note>Ничего не найдено.</p>';
}}
document.querySelectorAll('.tab').forEach(function(t){{t.onclick=function(){{document.querySelectorAll('.tab').forEach(function(z){{z.classList.remove('on')}});t.classList.add('on');F=t.dataset.f;draw();}};}});
document.getElementById('sort').onchange=function(e){{SB=e.target.value;draw();}};
document.getElementById('q').oninput=function(e){{Q=e.target.value.toLowerCase();draw();}};
draw();
</script></body></html>"""


if __name__ == "__main__":
    main()
