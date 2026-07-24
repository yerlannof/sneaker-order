# sneaker-order — заказы кроссовок + публичные дашборды (GitHub Pages)

Отдельный git-репо (отдаёт статику на https://yerlannof.github.io/sneaker-order/).
Основная аналитика/база — в соседнем репо `pnlpower` (см. его `scripts/CANON.md`).

## Канон-карта
| Что | Скрипт | Выход |
|---|---|---|
| Заказ кроссовок (КАНОН) | `generate_order.py` | заказ в Supabase (ссылки закупщик/поставщик) |
| Сейл-дашборд обуви | `build_sneakers.py` | sneakers.html + index.html |
| Уценка одежды | `build_clothing_clearance.py` | clothing_clearance.html |
| Уценка Adidas (Аружан) | `build_adidas_clearance.py` | adidas_clearance.html |
| SMM-посты скидок | `build_smm.py`, `build_smm_adidas.py` | smm*.html |
| «Что докупить» (НЕ обувь) | `build_restock.py` | restock.html |
| Ребаланс (пишется из pnlpower) | — | rebalance.html, kpi.html, stats.html |

Запуск любого билдера: `../.venv/bin/python <скрипт>` (из этой папки) — база лежит в `../data/`.
`archive/` — легаси, НЕ ЗАПУСКАТЬ (`upload_order.py` породил фантомный транзит ЗК-015).
`.photo_cache_*.json` — локальные кэши фото (не в git); `*_photos.json` — публикуемые данные страниц.
