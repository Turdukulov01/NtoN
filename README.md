# ChainLens — Blockchain Wallet Aggregator

Веб-сервис для агрегации данных по кошелькам Bitcoin, Ethereum и TRON.
Инкрементальная синхронизация, аналитика объёмов, экспорт CSV/Excel.

---

## Архитектура

```
UI (React SPA)
    │
FastAPI Gateway  ←── JWT auth / RBAC
    │
┌───┴──────────────────────────┐
│ Services                     │
│  Wallet │ Analytics │ Export │
└───┬──────────────────────────┘
    │
Redis Queue + Rate Limiter
    │
┌───┴─────────────────────────────────┐
│ Workers                             │
│  BTC (Blockstream) │ ETH (Etherscan)│
│  TRON (Tronscan)   │ Aggregator     │
└───┬─────────────────────────────────┘
    │
PostgreSQL ── Redis cache ── S3/MinIO
```

### Компоненты

| Компонент | Технология | Назначение |
|---|---|---|
| UI | Vanilla HTML/JS (или React) | Dashboard, таблицы, аналитика |
| API | FastAPI + asyncpg | REST endpoints, аутентификация |
| Workers | asyncio tasks | Синхронизация per-network |
| Queue | Redis | Rate limiting, очереди задач |
| DB | PostgreSQL 16 | Транзакции, агрегаты, sync state |
| Cache | Redis | Агрегаты, сессии |
| Scheduler | APScheduler | Daily sync 00:00 UTC |
| Monitoring | Prometheus + Grafana | Метрики, алерты |

---

## Стек

### MVP (быстрый запуск)
- Python 3.12 + FastAPI + asyncpg
- PostgreSQL 16
- Redis 7
- Blockstream API (BTC — бесплатно)
- Etherscan free tier (3 rps)
- Tronscan free tier
- nginx + Docker Compose

### Production (масштаб 5000+ кошельков)
- Те же компоненты + Celery workers (горизонтальное масштабирование)
- Alchemy / QuickNode вместо Etherscan (снимает лимит 3 rps)
- TimescaleDB или партиционированный PostgreSQL
- Redis Cluster
- Kubernetes + HPA

---

## Схема БД

```sql
networks        — bitcoin | ethereum | tron
users           — RBAC: admin | analyst | viewer
wallets         — адреса с network_id, label, tags
assets          — ERC-20/TRC-20 токены + native coins
transactions    — все транзакции (upsert, дедупликация)
sync_states     — last_synced_block / last_synced_tx (инкрементальный sync)
daily_snapshots — закрывающий баланс на конец дня
monthly_aggregates — предрассчитанные объёмы по месяцам
audit_logs      — кто что делал (экспорт, пересчёт, добавление кошелька)
```

### Ключевые индексы
```sql
transactions: (wallet_id, block_timestamp)  -- диапазонные запросы
transactions: (tx_hash, wallet_id, asset_id) UNIQUE  -- дедупликация
monthly_aggregates: (wallet_id, year, month) UNIQUE
sync_states: (wallet_id, asset_id) UNIQUE
```

---

## Формулы объёма (industry standard)

```
volume_in    = Σ amount  WHERE direction = incoming AND NOT is_error
volume_out   = Σ amount  WHERE direction = outgoing AND NOT is_error
volume_total = volume_in + volume_out     (gross turnover / оборот)
net_flow     = volume_in − volume_out     (+ = нетто получатель)
tx_count_in  = COUNT(direction = incoming)
tx_count_out = COUNT(direction = outgoing)
```

---

## Пайплайн синхронизации

```
1. Scheduler (00:00 UTC) → trigger sync_all_wallets()
2. Batch N wallets (semaphore = SYNC_BATCH_SIZE)
3. Per wallet:
   a. Load sync_state (last_synced_block, last_synced_tx)
   b. adapter.fetch_transactions(from_block=last_synced_block)
   c. For each tx:
      - get_or_create_asset()
      - upsert_transaction() -- ON CONFLICT DO NOTHING (дедупликация)
      - update sync_state cursor
   d. rebuild_monthly_aggregates() for affected months
4. Update sync_state.status = completed
```

---

## Стратегия работы с лимитами API

| Провайдер | Лимит | Решение |
|---|---|---|
| Etherscan | 3 rps (free) | Token-bucket limiter; для prod → Alchemy/QuickNode (10–100 rps) |
| Tronscan | 100 rps free / 2000 rps paid | AsyncRateLimiter, отдельная очередь |
| Blockstream (BTC) | ~10 rps | Мягкий лимит + retry с backoff |
| Alchemy (ETH) | 300+ rps (paid) | Рекомендуется для prod с 500+ ETH-кошельками |

**Распределение нагрузки:**
- Wallets обрабатываются батчами: `asyncio.Semaphore(SYNC_BATCH_SIZE)`
- Каждый адаптер имеет свой `AsyncRateLimiter` (token bucket)
- Exponential backoff на HTTP ошибки (429, 5xx): `2^attempt` секунд
- При 5000 кошельков × 3 rps Etherscan = ~1667 секунд на ETH (28 мин) → нормально для daily sync

---

## API endpoints

```
POST   /api/v1/auth/token                    # Login → JWT
POST   /api/v1/auth/register                 # Create user

GET    /api/v1/wallets/                      # List wallets (pagination, search, filter)
POST   /api/v1/wallets/                      # Add wallet + trigger sync
DELETE /api/v1/wallets/{id}                  # Deactivate

GET    /api/v1/wallets/{id}/transactions     # Transactions with filters
GET    /api/v1/wallets/{id}/monthly-volume   # Monthly analytics
GET    /api/v1/analytics/summary             # Portfolio-level summary

GET    /api/v1/export/export                 # CSV/Excel export
POST   /api/v1/sync/{wallet_id}              # Manual re-sync
POST   /api/v1/sync/all                      # Full sync all wallets
```

---

## Быстрый старт

```bash
# 1. Клонировать и настроить окружение
cp .env.example .env
# Вставить API ключи в .env

# 2. Запустить
docker compose up -d

# 3. Инициализировать БД
docker compose exec api alembic upgrade head

# 4. Создать admin пользователя
docker compose exec api python -c "
from app.core.auth import hash_password
from app.models import User, UserRoleEnum
# ... создать через API
"
# или через API:
curl -X POST http://localhost:8000/api/v1/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@company.com","password":"yourpassword","role":"admin"}'

# 5. Открыть UI
open http://localhost:3000

# 6. Открыть API docs
open http://localhost:8000/docs

# 7. Grafana (monitoring)
open http://localhost:3001   # admin / admin
```

---

## Альтернативные провайдеры (при достижении лимитов)

### Ethereum
| Провайдер | Лимит | Цена | Рекомендация |
|---|---|---|---|
| Etherscan | 3 rps | Free | MVP |
| Alchemy | 300 rps | ~$50/mo | Production |
| QuickNode | 1000+ rps | ~$100/mo | High load |
| Собственная нода (geth) | Unlimited | ~$300/mo VPS | 5000+ кошельков |

### Bitcoin
| Провайдер | Лимит | Цена |
|---|---|---|
| Blockstream | ~10 rps | Free |
| Blockchain.info | ~5 rps | Free |
| Mempool.space | ~10 rps | Free |
| Собственная нода | Unlimited | ~$200/mo |

### TRON
| Провайдер | Лимит | Цена |
|---|---|---|
| Tronscan (free) | 100 rps | Free |
| Tronscan (paid) | 2000 rps | ~$50/mo |
| TronGrid | 1000 rps | Free tier |

---

## Нефункциональные требования (SLA)

| Параметр | Цель |
|---|---|
| Uptime | 99.5% |
| API latency (p95) | < 500ms |
| Daily sync completion | < 4 hours для 5000 кошельков |
| Data freshness | max 25 часов (daily sync) |
| Export max rows | 500,000 строк |
| Backup | PostgreSQL PITR, ежедневные снапшоты |
| Retention | Транзакции с 2026 года (хранить бессрочно) |

---

## Мониторинг и алерты

- **Prometheus**: scrape FastAPI `/metrics` (prometheus-fastapi-instrumentator)
- **Grafana**: dashboard — wallet count, sync status, API rps, error rate
- **Алерты**:
  - Sync failure rate > 10%
  - API error rate > 5%
  - DB connections > 80%
  - Redis memory > 80%
  - Daily sync не завершился к 06:00 UTC

---

## План разработки (спринты)

### Sprint 1 (2 нед.) — MVP Backend
- [ ] FastAPI skeleton + auth + DB models
- [ ] ETH адаптер (Etherscan)
- [ ] Базовый sync pipeline (без rate limiter)
- [ ] API: /wallets, /transactions
- [ ] Docker Compose

### Sprint 2 (2 нед.) — Все сети + Rate limiter
- [ ] BTC адаптер (Blockstream)
- [ ] TRON адаптер (Tronscan)
- [ ] Token-bucket rate limiter
- [ ] Инкрементальный sync (sync_state)
- [ ] Дедупликация (ON CONFLICT)

### Sprint 3 (1 нед.) — Аналитика
- [ ] Monthly aggregates worker
- [ ] API: /monthly-volume, /summary
- [ ] APScheduler daily sync

### Sprint 4 (1 нед.) — Фронтенд
- [ ] Dashboard
- [ ] Wallet list + add/remove
- [ ] Transaction table с фильтрами
- [ ] Analytics + charts

### Sprint 5 (1 нед.) — Экспорт + Мониторинг
- [ ] CSV/Excel export
- [ ] Audit log
- [ ] Prometheus + Grafana
- [ ] Алерты

### Sprint 6 (1 нед.) — Hardening
- [ ] RBAC тесты
- [ ] Load testing (1500 кошельков)
- [ ] Документация API
- [ ] Бэкапы PostgreSQL

---

## Уточняющие вопросы

1. **Учёт self-transfer**: транзакции где from = to (один кошелёк переводит сам себе) — считать в in/out или отдельно?
2. **USD-оценка**: нужна ли стоимость в USD на момент транзакции (требует price oracle, например CoinGecko API)?
3. **Мультивалютный отчёт**: агрегировать все токены в USD-эквиваленте в одну строку или раздельно по каждому asset?
4. **ERC-20 scope**: все токены у кошелька или только whitelist (например, только USDT/USDC/DAI)?
5. **Права на экспорт**: только admin, или viewer тоже может экспортировать?
6. **Уведомления**: нужны ли email/Telegram алерты при аномальных транзакциях (крупный объём, новый адрес)?
7. **Историческая загрузка**: для новых кошельков — грузить историю с 2026 года или за всё время существования адреса?
