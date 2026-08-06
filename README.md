# STS - Sinyal Trading Sistemi

Iki servis, tek repo:

| Dosya | Servis | Gorev |
|---|---|---|
| bot.py | sts-executor | Sinyal + kural motoru, Binance emirleri (key sahibi) |
| panel.py | sts-panel | Web arayuzu, kill-switch (key gormez) |

## Coolify kurulumu (iki application, ayni repo)

### 1) sts-executor
- Build: Dockerfile, path: `Dockerfile.bot`
- Port: yok (arka plan servisi) - "Ports Exposes" bos birak
- Env: BINANCE_API_KEY, BINANCE_API_SECRET, BOT_TESTNET=true,
  SUPABASE_URL, SUPABASE_KEY, TELEGRAM_TOKEN, TELEGRAM_CHAT_IDS
  (strateji degerleri varsayilanlarda - istege bagli override)

### 2) sts-panel
- Build: Dockerfile, path: `Dockerfile.panel`
- Port: 8080
- Env: SUPABASE_URL, SUPABASE_KEY,
  PANEL_BIND=0.0.0.0, PANEL_PORT=8080,
  PANEL_USER=..., PANEL_PASS=...   (Basic Auth - PUBLIC ERISIMDE ZORUNLU)

## Supabase tablolari
Sirayla calistir: bot_trades.sql, sts_asama1.sql, sts_asama2.sql, sts_asama2b.sql

## Kill-switch
Panel butonu -> sts_control.killswitch (Supabase).
Acil yerel yedek: executor calisirken bot_stop.flag dosyasi da calisir.
