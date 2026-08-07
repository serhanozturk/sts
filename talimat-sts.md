# TALIMAT — STS (Sinyal Trading Sistemi)

> **GUNCEL SURUM GITHUB'DADIR:** `github.com/serhanozturk/sts` → `talimat-sts.md`
> Bu dosya STS'in tek dogruluk kaynagidir. Kod degisikligi yapmadan once oku.
>
> **Calisma sirasi:** Once GitHub'daki `talimat-sts.md`'yi `curl` ile cek (project
> knowledge'daki kopya eski olabilir). Mimari veya kural degisikliginden sonra bu
> dosyayi guncelle ve **GitHub'a commit et** — kod ile talimat birlikte guncellenir.
>
> Diger projeler: talimat-engine.md, talimat-terminal.md, talimat-screener.md

---

## 1. STS NEDIR

Screener sinyallerine ve kendi kurallarina gore Binance USDT perpetual futures'ta
**otomatik pozisyon acan** bot + web paneli. Engine ve Screener'dan **tamamen ayri**
bir projedir; onlarin dosyalarina/sunucularina DOKUNULMAZ.

STS sadece Supabase uzerinden Screener ile konusur (`screener_signals` tablosunu OKUR).

---

## 2. MIMARI — IKI AYRI SERVIS

| Servis | Dosya | Coolify app | Port | Binance key |
|---|---|---|---|---|
| Executor | `bot/bot.py` | **stsexecutor** | yok | VAR (emirleri o acar) |
| Panel | `panel/panel.py` | **stspanel** | 8080 (host 8090) | YOK |

**Panel Binance key'i GORMEZ.** Panel istek yazar, executor uygular.
Bu ayrim bilincli bir guvenlik karari — bozma.

- Sunucu: Hetzner `168.119.178.59` (Ubuntu 24.04 + Coolify, `:8000`)
- Repo: `github.com/serhanozturk/sts` — klasor yapisi `bot/` ve `panel/`
- Her klasorde `Dockerfile` (uzantisiz; `Dockerfile.bot` gibi isimler Coolify'da patlar)
- Coolify ayari: Base Directory `/bot` veya `/panel`, Dockerfile Location `Dockerfile`

### DEPLOY KURALI (kritik)
**Panel ve executor AYRI redeploy edilir.** Kod verirken hangi dosyanin
hangi uygulamayi etkiledigini ve her ikisinin de gerekip gerekmedigini
ACIKCA yaz. Bir tarafi deploy edip digerini unutmak en sik yasanan hata.

---

## 3. SUPABASE TABLOLARI

| Tablo | Kim yazar | Ne icin |
|---|---|---|
| `screener_signals` | Screener | Sinyal kaynagi (STS sadece okur) |
| `bot_trades` | executor | Islem kaydi + acik pozisyon durumu + panel istekleri |
| `sts_rules` | panel | Ozel kurallar |
| `sts_events` | ikisi | Olay akisi (atlama, hata, tetiklenme) |
| `sts_status` | executor | Panel icin durum snapshot'i (id=1, tek satir) |
| `sts_control` | panel | Kill-switch + `last_signal_id` (id=1) |
| `sts_settings` | panel | Strateji ayarlari (id=1) |
| `sts_webhooks` | panel | TradingView webhook kuyrugu |

`SUPABASE_URL` **bare domain** olmali — `/rest/v1` EKLEME.
Key `service_role` olmali (RLS kapali).

---

## 4. SINYAL KAYNAKLARI VE HAVUZLAR

Dort kaynak, tek emir motoru:

| Kaynak | Havuz | Nasil |
|---|---|---|
| Screener `strong` PUMP | **Sinyal** (varsayilan max 4) | `screener_signals` polling |
| Panel kurallari | **Ozel** (max 10) | `sts_rules`, bar kapanisinda degerlendirilir |
| TradingView webhook | **Ozel** | `/webhook` → kuyruk → executor (5 sn) |
| (izole kod alani) | — | **IPTAL EDILDI**, yerine forma yeni kosul tipi eklenir |

Havuzlar birbirinden bagimsiz. Sinyal havuzu dolunca ozel havuz etkilenmez.

### Pozisyon acmadan once HER ZAMAN
1. O coinde acik pozisyon var mi (`fetch_positions`)
2. Havuz limiti
3. Kasa kontrolu (sinyal: `min_balance`; ozel: dinamik SL riski + `rule_min_free`)
4. Dedup (sinyal: `dedup_days`; webhook: `wh_dedup_sec`)
5. Precision + `minNotional` + **max miktar** (asilirsa kirpilir, `SIZE_CLIP` olayi yazilir)

---

## 5. STRATEJI (sinyal havuzu — backtest ile sabitlendi)

- Yon **SHORT**, teminat `$100`, kaldirac `10x`, **CROSS**, **One-Way mode**
- Hard TP `-%10`, hard SL `+%15`
- Dedup 2 gun, max 4 es zamanli
- Sinyal filtresi: `signal_type IN (PUMP_1H, PUMP_15M)` + `strength='strong'` + `notified=true`
- Yeni sinyal tespiti `id > last_signal_id` (monoton, restart'a dayanikli)

**Bu degerler artik Ayarlar sekmesinden degistirilebilir** (`sts_settings`).
Executor 30 sn'de bir okur; Supabase'e ulasilamazsa env varsayilanlarina duser.
`BOT_TESTNET` **panelde YOK**, sadece env'de (yanlislikla canliya gecmemek icin).

---

## 6. CIKIS KATMANLARI

Uc katman, hepsi bagimsiz:

**1. Hard TP/SL** — Binance'e `closePosition=true` emri olarak gonderilir.
Canlida birincil koruma. Bot cokse bile calisir.

**2. Yumusak TP/SL** (`TP_SOFT` / `SL_SOFT`) — bot her dongude mark fiyati
seviyeyle karsilastirir, gecilmisse market emriyle kapatir.
`BOT_SOFT_EXIT=false` ile kapatilir. **Demo'da tek gercek koruma budur.**

**3. Dinamik TP/SL** — kosul bazli cikis (EMA/RSI/fiyat/OI/hacim/funding).
Iki ayri blok, her biri kendi periyoduyla. Hard ile iliskisi:
- `VEYA` → hangisi once gelirse (hard emir Binance'te durur)
- `VE` → ikisi birden gerekli (**hard emir Binance'e GONDERILMEZ**, koruma bota bagli — panel uyari gosterir)

AND modundaki tarafta yumusak katman **atlanir** (seviyeye ulasmak tek basina yetmez).

Dinamik yapilandirma **pozisyon acilirken dondurulur** (`bot_trades.dyn_tp/dyn_sl`).
Kural sonradan silinse/degisse acik pozisyon giris anindaki kurallariyla yonetilir.

---

## 7. KOSUL MOTORU

Alti tip, giris kurallari ve dinamik cikis ayni motoru kullanir:

| Tip | Sol alan | Sag alan | Not |
|---|---|---|---|
| `ema_cross` | hizli periyot | yavas periyot | |
| `rsi` | — | esik | periyot sabit 14 |
| `price` | — | fiyat | |
| `oi_change` | onceki bar sayisi | yuzde | son bar vs N bar ortalamasi |
| `volume` | onceki bar sayisi | yuzde | son bar vs N bar ortalamasi |
| `funding` | — | yuzde | |

**OI ve hacimde eksi isareti KULLANILMAZ** — yonu operator belirler:
`>` 5 = ortalamadan %5 buyuk, `<` 7 = ortalamadan %7 kucuk. Deger hep pozitif saklanir.

Mantik: `VE` / `VEYA`, tek kosulda `—` (otomatik kilitlenir).
Veri toplama `build_ctx()` ile ortak; ayni coin+periyot icin mum basina tek API cagrisi.

---

## 8. TRADINGVIEW WEBHOOK

```
TradingView alarm → panel /webhook (token) → sts_webhooks → executor (5 sn) → pozisyon
```

- Uc **Basic Auth'tan MUAF** (TV sifre gonderemez), `WEBHOOK_TOKEN` ile korunur
- Token yoksa uc `503` doner (yanlislikla korumasiz kalmaz)
- Hatali token denemeleri kaynak IP ile `sts_events`'e yazilir
- Payload zorunlu: `coin`, `direction`. Digerleri Ayarlar'daki `wh_*` varsayilanlarindan gelir
- Payload'daki deger varsayilani **ezer** → her alarma ozel TP/SL/teminat mumkun
- Panelde **mesaj olusturucu** var: alanlari doldur, JSON'u kopyala

**TradingView HTTP'de sadece port 80'e izin verir** — `:8090` calismaz.
Su an sslip.io adresi kullaniliyor; domain alininca HTTPS'e gecilecek.

---

## 9. BILINEN KISIT — DEMO ORTAMI (kanitlandi)

`demo-fapi.binance.com` stop emirlerinde tutarsiz:

- Emri **kabul eder**, id verir, web arayuzunde **gosterir**
- Ama API'de **okunamaz** (`-2013`), **iptal edilemez**, **TETIKLENMEZ**
- Yeni emir kurmaya calisinca `-4130` "zaten var" der
- `closePosition` ve `reduceOnly` — ikisinde de ayni

**Sonuclari:**
- Demo'da hard TP/SL **islevsiz** → yumusak katman zorunlu
- Acik pozisyonun TP/SL seviyesini **sonradan degistirmek demo'da calismaz**
- Kapanislar `MANUEL/BILINMIYOR` gorunur (bot hangi emrin kapattigini sorgulayamaz)

Canlida bu emirler normal calisir. Kodda hata yok, ortam kisiti.

---

## 10. GELISTIRME KURALLARI

### Kod disiplini
- **Izin olmadan kod yazma.** Once tasarimi tartis, tek soru sor, onay gelince yaz.
- Dosya adlari degismez: `bot/bot.py`, `panel/panel.py`
- Sadece degisecek yere dokun — yan etki yok
- Panel: Python stdlib + gomulu HTML/CSS/JS (harici bagimlilik yok)
- Executor: sadece `ccxt` bagimliligi

### TEST — atlanmaz
1. `python3 -m py_compile bot.py panel.py`
2. **Gomulu JS'i RUNTIME degerden cikar ve `node --check` ile dogrula**
3. Birim testler: `test_bot_v2.py`, `test_panel.py`

### TUZAK 1 — JS kacis karakterleri (iki kez yasandi)
`HTML = """..."""` bir Python string'idir. Icine `\'` yazarsan Python ters bolueyi
**yer**, JS'e `'` gider ve string erken kapanir → tum panel coker.
- Kaynakta `\\'` yaz (JS'e `\'` gitsin) veya apostrofu hic kullanma
- Ayni sey `\n` icin gecerli: kaynakta `\\n` olmali
- **JS sozdizimini KAYNAK metinden degil, `P.HTML` runtime degerinden test et** —
  kaynaktan test edersen bu hatayi goremezsin

### TUZAK 2 — `cancel_all_orders` sembolsuz cagrilmamali
Sembol verilmezse **hesaptaki TUM emirler** iptal olur, diger pozisyonlar zarar gorur.
`cancel_all()` icinde koruma var (sembol bossa hicbir sey yapmaz) — kaldirma.

### TUZAK 3 — PostgREST tarih formati
URL'de `+00:00` iceren ISO tarih kullanma; `+` bosluga donusur, sorgu `400` verir
ve **sessizce bos sonuc** doner. `iso_url()` yardimcisini kullan (`Z` formati).

### TUZAK 4 — fail-open
Kontrol sorgusu basarisiz olursa **korumayi kapatma**. Dedup sorgusu hata verirse
sinyal ATLANIR, acilmaz. Ayni mantik tum guvenlik kontrolleri icin gecerli.

### Talimat dosyasi guncel tutulur
Mimari, tablo, strateji parametresi, cikis katmani veya yeni bir tuzak ortaya
ciktiginda **bu dosyayi da guncelle ve GitHub'a commit et**. Kod ile talimatin
ayrisma hakki yoktur. Yeni bir sohbete baslarken once GitHub'daki surumu cek.

### Emir degistirme
Binance ayni yonde ikinci `closePosition` emrini reddeder (`-4130`).
Seviye degistirirken: o **sembolun** emirleri topluca iptal edilir, TP ve SL
**birlikte** yeniden kurulur (atomik). Kurulamazsa Telegram'a ACIL uyarisi gider.

---

## 11. ENV DEGISKENLERI

**stsexecutor**
```
BINANCE_API_KEY, BINANCE_API_SECRET
BOT_TESTNET=true
BOT_TESTNET_URL=https://demo-fapi.binance.com
SUPABASE_URL, SUPABASE_KEY
TELEGRAM_TOKEN, TELEGRAM_CHAT_IDS
BOT_SOFT_EXIT=true          # yumusak TP/SL
BOT_WEBHOOK_POLL_SEC=5
BOT_SETTINGS_POLL_SEC=30
# strateji varsayilanlari (Supabase yoksa gecerli): BOT_MARGIN_USDT, BOT_LEVERAGE,
# BOT_TP_PCT, BOT_SL_PCT, BOT_MAX_POSITIONS, BOT_MIN_BALANCE, BOT_DEDUP_DAYS ...
```

**stspanel**
```
SUPABASE_URL, SUPABASE_KEY
PANEL_BIND=0.0.0.0
PANEL_PORT=8080
PANEL_USER, PANEL_PASS      # Basic Auth
WEBHOOK_TOKEN               # yoksa /webhook 503 doner
```

---

## 12. PANEL

Bes sekme: **Durum**, **Islemler**, **Olaylar**, **Kurallar**, **Ayarlar**

- Gece/gunduz temasi (tercih tarayicida saklanir), mobil uyumlu
- Ana basliklar BUYUK HARF, alt basliklar Title case
- Para alanlarinda `$`, PnL'de hem `$` hem `%`
- Saatler UTC'den yerel saate (TR) cevrilir
- Yenile butonu (basliktan elle tazeleme)
- **Yonet** butonu: acik pozisyonda hard TP/SL degistir, pozisyona ozel dinamik
  cikis tanimla, elle kapat. Panel acikken liste yeniden cizilmez (form korunur),
  sadece PnL/mark tazelenir.
- Kill-switch: `sts_control.killswitch` (container bagimsiz)

---

## 13. YAPILACAKLAR

1. Domain + HTTPS — panel ve webhook token'i su an sifresiz gidiyor
2. `service_role` key sifirlama (sohbette aciga cikti)
3. Binance API key IP whitelist — canliya gecmeden
4. 2-3 hafta testnet dogrulamasi
5. Canliya gecerken pozisyon boyutunu $20-30 ile baslat
6. Backtest motoru (CLI + yerel OHLCV onbellegi) — ertelendi, backtest ayri
   chat'te Supabase verisiyle yapiliyor

**Ertelenen/iptal:** izole Python kod alani (iptal — yerine forma yeni kosul tipi
eklenir), panel ici backtest bolumu (iptal — KKS ile sohbette yapiliyor)
