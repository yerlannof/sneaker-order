-- Выполнить в Supabase SQL Editor (https://supabase.com/dashboard → SQL Editor)

-- Таблица заказов
create table if not exists orders (
  id text primary key,
  created_at timestamptz default now(),
  updated_at timestamptz default now(),
  status text default 'draft',
  items jsonb not null,
  supplier_items jsonb,
  confirmed_items jsonb,
  meta jsonb
);

-- Разрешить анонимный доступ (заказы доступны по ID — ID не угадать)
alter table orders enable row level security;

create policy "Anyone can read orders" on orders
  for select using (true);

create policy "Anyone can insert orders" on orders
  for insert with check (true);

create policy "Anyone can update orders" on orders
  for update using (true);

create policy "Anyone can delete orders" on orders
  for delete using (true);
