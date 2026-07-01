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

### Структура кода

```
app/
  api/                 # FastAPI app factory и HTTP routers
  application/         # use-cases: сбор отчета кошелька, трассировка графа
  domain/              # чистые бизнес-правила: risk engine, wallet analytics
  infrastructure/      # внешние blockchain clients и normalizers
  core/                # настройки, форматирование, периоды, DB/Redis interfaces
  adapters/            # долгоживущие network adapters для worker pipeline
  workers/             # sync/aggregation jobs
```

Корневой каталог содержит только конфигурацию, документацию и deployment-файлы.
Python-код приложения находится внутри `app/`; запуск API: `uvicorn app.api.app:app`.

### Санкционный screening

Санкционные списки хранятся как reference data, отдельно от `risk_model.yaml`.
`risk_model.yaml` задает правила и override policy, а сами списки импортируются в PostgreSQL.

Первый источник: UK Sanctions List XML.

```bash
alembic upgrade head
python scripts/import_uk_sanctions_xml.py --download
# или из локального файла
python scripts/import_uk_sanctions_xml.py --xml data/sanctions/uk_official/UK-Sanctions-List.xml
```

Основные таблицы:

```
sanctions_import_runs
sanctions_subjects
sanctions_names
sanctions_documents
sanctions_addresses
sanctions_matches
```

Screening API:

```
POST /api/sanctions/screen
```

Confirmed match по `Unique ID`, `OFSI Group ID`, документу или `name + secondary field`
дает `override_hit=true` и итоговый RED через risk engine. Совпадение только по имени
не дает override и уходит в manual review.

### Risk assessment pipeline

Risk scoring теперь запускается как единый pipeline:

```
wallet report
    ↓
DB address_risk_tags
    ↓
sanctions screening по client / UBO / director / related parties
    ↓
risk engine
    ↓
risk_assessments + risk_assessment_evidence
```

Persistent таблицы:

```
address_risk_tags           # наша адресная risk-разметка
address_risk_tag_events     # история create/update/deactivate по risk-разметке
kyt_provider_reports        # сохранённые KYT-проверки внешних провайдеров
kyt_exposures               # нормализованные exposure categories из provider reports
risk_assessments            # сохранённый результат скоринга
risk_assessment_evidence    # evidence: tags, sanctions hits, exposures
```

Основные endpoints:

```
GET  /api/wallet/validate-address
POST /api/risk/assess-wallet
POST /api/risk/address-tags
GET  /api/risk/address-tags
GET  /api/risk/address-tags/{tag_id}/events
GET  /api/risk/kyt-reports
GET  /api/risk/kyt-reports/{report_id}
DELETE /api/risk/kyt-reports/{report_id}
GET  /api/risk/assessments/{assessment_id}
```

Для внешних KYT-провайдеров настройте ключи в `.env` или секретах окружения:

```bash
RANEX_API_KEY=...
RANEX_BASE_URL=https://kyt-api.ranex.asia
SHARD_PUBLIC_APP_ID=...
SHARD_API_SECRET=...
SHARD_BASE_URL=https://shard.ru
KYT_PROVIDER_CACHE_TTL_SECONDS=86400
```

Shard Risk API использует HMAC: `X-Public-App-ID` + `X-Hash`, где hash считается от URI и тела запроса секретом `SHARD_API_SECRET`.

`/api/risk/assess-wallet` принимает старый формат запроса и дополнительно:

```json
{
  "participant_profile": {
    "jurisdiction": "Kyrgyzstan",
    "license_status": "registered",
    "ubo_transparency": "partial",
    "reputation": "neutral",
    "asset_type": "stablecoin_regulated",
    "sof_sow_status": "verified"
  },
  "transaction_profile": {
    "volume_profile": "within_profile",
    "geography_status": "regulated_countries",
    "counterparty_status": "verified",
    "dex_usage": "none",
    "liquidity_pool_status": "none"
  },
  "control_profile": {
    "aml_kyc_status": "full",
    "client_funds_segregation": "full",
    "regulatory_reporting": "regular",
    "request_response": "standard"
  },
  "screening_subjects": [
    {
      "checked_subject_type": "client",
      "name": "MOHAMMAD HASSAN AKHUND",
      "passport_numbers": ["P04581926"]
    }
  ],
  "persist_assessment": true
}
```

Эти поля соответствуют PDF-модели A/B/C/D. Если данных по клиенту или сервису нет,
поля лучше не передавать: модель считает их как `not_provided`, а не подставляет догадки
из адреса кошелька.

Если санкционный screening дал confirmed match, risk engine ставит `final_score=100`,
`risk_zone=RED`, `override_reasons=["sanctions_screening_confirmed"]`.
Если найден только слабый/name-only match, override не применяется, но evidence сохраняется.

Внешние KYT-данные по адресу передаются отдельно как `external_kyt` или подтягиваются
через provider adapter, например Ranex API или Shard Risk API. Это не confirmed sanctions match, а внешняя
адресная атрибуция/экспозиция, поэтому provider score используется как evidence, а итоговый
балл считает наша модель по нормализованным категориям.

```json
{
  "kyt_provider": "ranex",
  "external_kyt_required": true,
  "force_kyt_refresh": false
}
```

Для Shard используйте `"kyt_provider": "shard"`. По умолчанию для TRON выбирается
currency tag `trx-usdt`; при необходимости токен можно передать через `kyt_token`
или `wallet_profile.token`.

Порядок работы provider cache:

```
wallet address
    ↓
fresh kyt_provider_reports row exists?
    ↓ yes                         ↓ no / force refresh
use PostgreSQL KYT check           call provider API
    ↓                              ↓
risk engine                        save raw_response + normalized_payload + kyt_exposures
                                   ↓
                                risk engine
```

Raw provider response хранится в `kyt_provider_reports.raw_response` (`JSONB`), нормализованный
payload — в `kyt_provider_reports.normalized_payload`, а категории/проценты — в `kyt_exposures`.
Это кеш внешних данных, а не наш итоговый risk assessment.

В UI страница `KYT база` показывает сохранённые KYT-проверки внешних провайдеров, top exposures,
детальный provider payload и внутренние `address_risk_tags`. Обычные `wallet.tags`
остаются операционными метками списка кошельков и не влияют на scoring; на scoring
влияют только risk tags из `address_risk_tags`, YAML или явного request payload.

Также можно передать уже нормализованные KYT-exposures вручную:

```json
{
  "external_kyt": {
    "provider": "manual",
    "source": "External KYT",
    "score_policy": "evidence_only",
    "address": "TTrzEDXhgDD8f2jmV47NdUE4EQ8bHv8ip2",
    "network": "tron",
    "risk_score": 22.72,
    "exposures": [
      {"name": "Sanctions", "percent": 9.9, "amount_human": "31327.06 TRX"},
      {"name": "Bridge", "percent": 7.59, "amount_human": "24016.65 TRX"}
    ]
  }
}
```

Provider score можно использовать как minimum score floor только явно:

```json
{
  "external_kyt": {
    "score_policy": "minimum_score",
    "risk_score": 60,
    "exposures": []
  }
}
```

Важно: `Sanctions=9.9%` в KYT-ответе означает экспозицию кошелька к категории,
а не подтверждённого санкционного владельца. RED override остаётся только за confirmed
screening match по санкционной базе, прямую/близкую risk-разметку адреса или другой
override-индикатор из PDF-модели. Косвенная KYT exposure используется как evidence и
компонентный сигнал, но сама по себе не копирует score провайдера и не делает RED.

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
