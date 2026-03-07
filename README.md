# Sneaker Order

Shared sneaker ordering system — buyer edits, supplier confirms availability and prices.

## Setup

### 1. Supabase (free)
1. Go to [supabase.com](https://supabase.com) → New Project
2. Open SQL Editor → paste and run `setup.sql`
3. Go to Settings → API → copy **Project URL** and **anon public key**

### 2. Configure
Create `.env` in this folder:
```
SUPABASE_URL=https://xxxxx.supabase.co
SUPABASE_KEY=eyJhbGci...
```

Also update `index.html` lines with `%%SUPABASE_URL%%` and `%%SUPABASE_KEY%%`.

### 3. Deploy
```bash
git add -A && git commit -m "Initial" && git push
```
Enable GitHub Pages: repo Settings → Pages → Source: main → / (root)

Site will be at: `https://yerlannof.github.io/sneaker-order/`

## Usage

```bash
# From pnlpower directory:
python ../sneaker-order/upload_order.py --min-sold 5

# Output:
# Закупщик: https://yerlannof.github.io/sneaker-order/?id=abc123&role=buyer
# Поставщик: https://yerlannof.github.io/sneaker-order/?id=abc123&role=supplier
```

Send the supplier link via WhatsApp. Supplier marks availability and prices. You confirm and export JSON.
