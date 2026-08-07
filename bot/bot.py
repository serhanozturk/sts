#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
STS EXECUTOR v2
===============
Iki sinyal kaynagi, tek emir motoru:

  HAVUZ 1 - SINYAL (max 4):
    Screener 'strong' PUMP sinyalleri (Supabase screener_signals)
    Sabit strateji: SHORT, $100 x10 CROSS, TP -%10, SL +%15, dedup 2 gun

  HAVUZ 2 - OZEL KURAL (max 10):
    sts_rules tablosundaki kurallar (panel/SQL'den girilir)
    Yon serbest (LONG/SHORT), TP/SL serbest (pct/price), teminat serbest
    Kosullar: ema_cross, rsi, price, oi_change, volume, funding
    Her mum kapanisinda degerlendirilir, tetiklenince tek seferlik calisir

GUVENLIK:
  - API key'ler ENV'de. Varsayilan TESTNET.
  - Kill-switch: bot_stop.flag varsa yeni pozisyon acilmaz
  - Ayni coinde acik pozisyon varken yeni islem ACILMAZ (one-way carpisma korumasi)
  - SL kurulamazsa pozisyon aninda kapatilir
  - Ozel havuzda dinamik kasa: tum SL riskleri dusuldukten sonra
    bakiye BOT_RULE_MIN_FREE altina inecekse pozisyon acilmaz
"""

import os
import sys
import json
import time
import signal as _signal
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

try:
    import ccxt
except ImportError:
    sys.stderr.write("HATA: ccxt kurulu degil.  pip install ccxt\n")
    sys.exit(1)

VERSION = "v2"

# ======================================================================
# .env DOSYASI (varsa yukle - harici kutuphane gerekmez)
# ======================================================================

def load_dotenv(path=".env"):
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except Exception as e:
        sys.stderr.write(f"UYARI: .env okunamadi: {e}\n")


load_dotenv(os.environ.get("BOT_ENV_FILE", ".env"))

# ======================================================================
# KONFIGURASYON
# ======================================================================

def _env(key, default=None, required=False):
    v = os.environ.get(key, default)
    if required and not v:
        sys.stderr.write(f"HATA: {key} env degiskeni tanimli degil.\n")
        sys.exit(1)
    return v


def _env_f(key, default):
    try:
        return float(os.environ.get(key, default))
    except (TypeError, ValueError):
        return float(default)


def _env_i(key, default):
    try:
        return int(os.environ.get(key, default))
    except (TypeError, ValueError):
        return int(default)


def _env_b(key, default="false"):
    return str(os.environ.get(key, default)).strip().lower() in ("1", "true", "yes", "on")


# --- Binance ---
BINANCE_KEY     = _env("BINANCE_API_KEY", required=True)
BINANCE_SECRET  = _env("BINANCE_API_SECRET", required=True)
TESTNET         = _env_b("BOT_TESTNET", "true")
TESTNET_URL     = (_env("BOT_TESTNET_URL", "https://demo-fapi.binance.com") or "").rstrip("/")

# --- Sinyal havuzu stratejisi (backtest ile sabitlendi) ---
# --- Strateji ayarlari: ONCE env varsayilanlari, SONRA Supabase sts_settings
#     ile ustune yazilir. Supabase'e ulasilamazsa env degerleri gecerli kalir.
CFG = {
    "margin_usdt":        _env_f("BOT_MARGIN_USDT", 100),
    "leverage":           _env_i("BOT_LEVERAGE", 10),
    "margin_mode":        _env("BOT_MARGIN_MODE", "cross"),
    "tp_pct":             _env_f("BOT_TP_PCT", 10.0),
    "sl_pct":             _env_f("BOT_SL_PCT", 15.0),
    "max_positions":      _env_i("BOT_MAX_POSITIONS", 4),
    "max_rule_positions": _env_i("BOT_MAX_RULE_POSITIONS", 10),
    "min_balance":        _env_f("BOT_MIN_BALANCE", 900),
    "rule_min_free":      _env_f("BOT_RULE_MIN_FREE", 100),
    "dedup_days":         _env_f("BOT_DEDUP_DAYS", 2),
    "signal_types":       [s.strip() for s in _env("BOT_SIGNAL_TYPES", "PUMP_1H,PUMP_15M").split(",") if s.strip()],
    "strength":           _env("BOT_STRENGTH", "strong"),
    "poll_seconds":       _env_i("BOT_POLL_SECONDS", 20),
}
SETTINGS_POLL_SEC = _env_i("BOT_SETTINGS_POLL_SEC", 30)
WEBHOOK_POLL_SEC  = _env_i("BOT_WEBHOOK_POLL_SEC", 5)   # kuyruk kontrol araligi

# Webhook varsayilanlari (payload'da belirtilmeyen alanlar icin).
# sts_settings'ten wh_* kolonlariyla ustune yazilir.
WH = {
    "margin_usdt": _env_f("BOT_WH_MARGIN_USDT", 100),
    "leverage":    _env_i("BOT_WH_LEVERAGE", 10),
    "tp_type":     _env("BOT_WH_TP_TYPE", "pct"),
    "tp_value":    _env_f("BOT_WH_TP_VALUE", 10),
    "sl_type":     _env("BOT_WH_SL_TYPE", "pct"),
    "sl_value":    _env_f("BOT_WH_SL_VALUE", 15),
    "dedup_sec":   _env_i("BOT_WH_DEDUP_SEC", 60),
}

# --- Ozel kural havuzu (panelden degistirilmeyen sabitler) ---
RULE_LIMIT      = _env_i("BOT_RULE_LIMIT", 50)          # aktif kural ust siniri
RULE_POLL_SEC   = _env_i("BOT_RULE_POLL_SEC", 30)       # kural listesi yenileme

# --- Supabase ---
SUPABASE_URL    = (_env("SUPABASE_URL", "") or "").rstrip("/")
SUPABASE_KEY    = _env("SUPABASE_KEY", "")
SUPABASE_ON     = bool(SUPABASE_URL and SUPABASE_KEY)

# --- Telegram ---
TG_TOKEN        = _env("TELEGRAM_TOKEN", "")
TG_CHAT_IDS     = [c.strip() for c in (_env("TELEGRAM_CHAT_IDS", "") or "").split(",") if c.strip()]
TG_ON           = bool(TG_TOKEN and TG_CHAT_IDS)

# --- Dosyalar ---
STOP_FLAG       = _env("BOT_STOP_FLAG", "bot_stop.flag")   # yerel acil yedek

TF_SECONDS = {"5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400}


# ======================================================================
# LOG / TELEGRAM
# ======================================================================

def log(msg, level="INFO"):
    ts = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {level:5s} {msg}", flush=True)


def tg_send(text):
    if not TG_ON:
        return
    for chat_id in TG_CHAT_IDS:
        try:
            url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
            body = json.dumps({
                "chat_id": chat_id, "text": text,
                "parse_mode": "HTML", "disable_web_page_preview": True,
            }).encode("utf-8")
            req = urllib.request.Request(url, data=body, method="POST",
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10).read()
        except Exception as e:
            log(f"Telegram gonderim hatasi ({chat_id}): {e}", "WARN")


# ======================================================================
# SUPABASE
# ======================================================================

def sb_request(method, path, body=None):
    if not SUPABASE_ON:
        return None
    url = f"{SUPABASE_URL}/rest/v1/{path.lstrip('/')}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else []
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")[:300]
        except Exception:
            pass
        log(f"Supabase {method} {path}: HTTP {e.code} {detail}", "ERROR")
        return None
    except Exception as e:
        log(f"Supabase {method} {path}: {e}", "ERROR")
        return None


def sb_log_event(kind, coin=None, detail=""):
    sb_request("POST", "sts_events", {"kind": kind, "coin": coin, "detail": str(detail)[:500]})


def sb_upsert_status(payload):
    """Tek satirlik durum snapshot'i (panel okur). id=1 uzerine upsert."""
    if not SUPABASE_ON:
        return
    url = f"{SUPABASE_URL}/rest/v1/sts_status?on_conflict=id"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    body = json.dumps({
        "id": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "payload": payload,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        urllib.request.urlopen(req, timeout=12).read()
    except Exception as e:
        log(f"Status yazilamadi: {e}", "WARN")


# --- sinyal havuzu ---

def sb_fetch_new_signals(last_id):
    types = ",".join(CFG["signal_types"])
    path = (f"screener_signals?id=gt.{last_id}"
            f"&signal_type=in.({types})"
            f"&strength=eq.{CFG['strength']}"
            f"&notified=is.true"
            f"&order=id.asc&limit=50")
    rows = sb_request("GET", path)
    return rows if rows else []


def sb_max_signal_id():
    rows = sb_request("GET", "screener_signals?select=id&order=id.desc&limit=1")
    if rows and len(rows) > 0:
        return int(rows[0].get("id", 0))
    return 0


def sb_recent_trade_coins(days):
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = sb_request("GET", f"bot_trades?select=coin&opened_at=gte.{since}&source=eq.signal")
    if not rows:
        return set()
    return {r.get("coin") for r in rows if r.get("coin")}


def sb_open_trades():
    rows = sb_request("GET", "bot_trades?closed_at=is.null&order=id.asc")
    return rows if rows else []


def sb_update_trade(trade_id, fields):
    return sb_request("PATCH", f"bot_trades?id=eq.{trade_id}", fields)


def sb_insert_trade(row):
    res = sb_request("POST", "bot_trades", row)
    if res and len(res) > 0:
        return res[0]
    return None


def sb_close_trade(trade_id, exit_price, pnl, reason):
    body = {
        "closed_at": datetime.now(timezone.utc).isoformat(),
        "exit_price": exit_price, "pnl": pnl, "exit_reason": reason,
    }
    return sb_request("PATCH", f"bot_trades?id=eq.{trade_id}", body)


# --- kural havuzu ---

def sb_pending_webhooks():
    rows = sb_request("GET", "sts_webhooks?executed=is.false&order=id.asc&limit=10")
    return rows if rows else []


def sb_mark_webhook(wid, result, trade_id=None):
    body = {"executed": True,
            "executed_at": datetime.now(timezone.utc).isoformat(),
            "result": str(result)[:200]}
    if trade_id:
        body["trade_id"] = trade_id
    return sb_request("PATCH", f"sts_webhooks?id=eq.{wid}", body)


def sb_recent_webhook_coins(seconds):
    """Son N saniyede ISLENMIS webhook coinleri (tekrar korumasi)."""
    if seconds <= 0:
        return set()
    since = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
    rows = sb_request(
        "GET", f"sts_webhooks?select=coin&executed=is.true&executed_at=gte.{since}"
               f"&result=like.OPENED*")
    if not rows:
        return set()
    return {r.get("coin") for r in rows if r.get("coin")}


def sb_active_rules():
    rows = sb_request("GET", f"sts_rules?active=is.true&order=id.asc&limit={RULE_LIMIT}")
    return rows if rows else []


def sb_deactivate_rule(rule_id, triggered=False):
    body = {"active": False}
    if triggered:
        body["triggered_at"] = datetime.now(timezone.utc).isoformat()
    return sb_request("PATCH", f"sts_rules?id=eq.{rule_id}", body)


def sb_get_control():
    rows = sb_request("GET", "sts_control?id=eq.1&limit=1")
    if rows and len(rows) > 0:
        return rows[0]
    return None


def sb_get_settings():
    """sts_settings tablosundan strateji ayarlarini oku (tek satir, id=1)."""
    rows = sb_request("GET", "sts_settings?id=eq.1&limit=1")
    if rows and len(rows) > 0:
        return rows[0]
    return None


SETTINGS_NUM = {
    "margin_usdt": float, "leverage": int, "tp_pct": float, "sl_pct": float,
    "max_positions": int, "max_rule_positions": int,
    "min_balance": float, "rule_min_free": float,
    "dedup_days": float, "poll_seconds": int,
}


def apply_settings(row):
    """Supabase satirini CFG'ye uygula. Gecersiz/eksik alan env degerini korur.
    Doner: degisen alanlarin listesi."""
    if not row:
        return []
    changed = []
    for key, caster in SETTINGS_NUM.items():
        if key not in row or row[key] is None:
            continue
        try:
            val = caster(row[key])
        except (TypeError, ValueError):
            continue
        if val <= 0:                      # sifir/negatif ayar kabul edilmez
            continue
        if CFG.get(key) != val:
            CFG[key] = val
            changed.append(key)

    mm = (row.get("margin_mode") or "").strip().lower()
    if mm in ("cross", "isolated") and CFG["margin_mode"] != mm:
        CFG["margin_mode"] = mm
        changed.append("margin_mode")

    st = (row.get("strength") or "").strip()
    if st and CFG["strength"] != st:
        CFG["strength"] = st
        changed.append("strength")

    # webhook varsayilanlari
    for key, caster in (("margin_usdt", float), ("leverage", int),
                        ("tp_value", float), ("sl_value", float),
                        ("dedup_sec", int)):
        val = row.get("wh_" + key)
        if val is None:
            continue
        try:
            v = caster(val)
        except (TypeError, ValueError):
            continue
        if v <= 0 and key != "dedup_sec":
            continue
        if v < 0:
            continue
        if WH.get(key) != v:
            WH[key] = v
            changed.append("wh_" + key)
    for key in ("tp_type", "sl_type"):
        t = (row.get("wh_" + key) or "").strip().lower()
        if t in ("pct", "price") and WH[key] != t:
            WH[key] = t
            changed.append("wh_" + key)

    raw = row.get("signal_types") or ""
    types = [s.strip().upper() for s in str(raw).split(",") if s.strip()]
    if types and CFG["signal_types"] != types:
        CFG["signal_types"] = types
        changed.append("signal_types")

    return changed


def sb_update_control(fields):
    fields["updated_at"] = datetime.now(timezone.utc).isoformat()
    return sb_request("PATCH", "sts_control?id=eq.1", fields)


# ======================================================================
# YEREL STATE (artik Supabase'de - sts_control; dosya kaldirildi)
# ======================================================================


# ======================================================================
# INDIKATORLER (stdlib)
# ======================================================================

def ema(values, period):
    """Klasik EMA: seed = SMA(period). values kronolojik (eski -> yeni)."""
    if period <= 0 or len(values) < period:
        return None
    seed = sum(values[:period]) / period
    k = 2.0 / (period + 1)
    e = seed
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def rsi(values, period=14):
    """Wilder RSI. values kronolojik."""
    if period <= 0 or len(values) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, period + 1):
        d = values[i] - values[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_g = sum(gains) / period
    avg_l = sum(losses) / period
    for i in range(period + 1, len(values)):
        d = values[i] - values[i - 1]
        avg_g = (avg_g * (period - 1) + max(d, 0.0)) / period
        avg_l = (avg_l * (period - 1) + max(-d, 0.0)) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100.0 - 100.0 / (1.0 + rs)


def compare(a, op, b):
    if a is None or b is None:
        return False
    if op == "<":
        return a < b
    if op == ">":
        return a > b
    if op == "<=":
        return a <= b
    if op == ">=":
        return a >= b
    return False


def num(v, default=None):
    """'-5%' / '3x' / '0.21' gibi girdileri sayiya cevir."""
    if v is None:
        return default
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace("%", "").replace("x", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return default


# ======================================================================
# BORSA
# ======================================================================

class Exchange:
    def __init__(self):
        self.ex = ccxt.binanceusdm({
            "apiKey": BINANCE_KEY,
            "secret": BINANCE_SECRET,
            "enableRateLimit": True,
            "options": {"defaultType": "future"},
        })
        if TESTNET:
            self.ex.set_sandbox_mode(True)
            self._apply_testnet_url()
        self.markets = self.ex.load_markets()
        self._configured = set()

    def _apply_testnet_url(self):
        if not TESTNET_URL:
            return
        api = self.ex.urls.get("api", {})
        for key, val in list(api.items()):
            if not isinstance(val, str) or not key.startswith("fapi"):
                continue
            tail = val.split("/fapi/", 1)[1] if "/fapi/" in val else "v1"
            api[key] = f"{TESTNET_URL}/fapi/{tail}"
        self.ex.urls["api"] = api
        log(f"Testnet endpoint: {TESTNET_URL}")

    # --- sembol donusumu: 'COTI' veya 'COTIUSDT' -> 'COTI/USDT:USDT' ---
    def unified(self, raw_symbol):
        raw = (raw_symbol or "").strip().upper()
        raw = raw.replace("/", "").replace(":USDT", "").replace(".P", "")
        if not raw:
            return None
        adaylar = [raw]
        if not raw.endswith("USDT"):
            adaylar.append(raw + "USDT")
        for aday in adaylar:
            for sym, m in self.markets.items():
                if (m.get("id") == aday and m.get("swap")
                        and m.get("quote") == "USDT"):
                    return sym
        return None

    def configure_symbol(self, symbol, leverage=None, margin_mode=None):
        lev = leverage or CFG["leverage"]
        mm = margin_mode or CFG["margin_mode"]
        tag = (symbol, lev, mm)
        if tag in self._configured:
            return
        try:
            self.ex.set_margin_mode(mm, symbol)
        except Exception as e:
            if "No need to change" not in str(e):
                log(f"{symbol} margin mode: {e}", "WARN")
        try:
            self.ex.set_leverage(lev, symbol)
        except Exception as e:
            log(f"{symbol} leverage: {e}", "WARN")
        self._configured.add(tag)

    def open_positions(self):
        out = {}
        try:
            positions = self.ex.fetch_positions()
        except Exception as e:
            log(f"fetch_positions hatasi: {e}", "ERROR")
            return None
        for p in positions:
            try:
                contracts = float(p.get("contracts") or 0)
            except (TypeError, ValueError):
                contracts = 0
            if abs(contracts) > 0:
                out[p.get("symbol")] = {
                    "contracts": abs(contracts),
                    "entryPrice": p.get("entryPrice"),
                    "side": p.get("side"),
                    "markPrice": p.get("markPrice"),
                    "unrealizedPnl": p.get("unrealizedPnl"),
                }
        return out

    def free_usdt(self):
        try:
            bal = self.ex.fetch_balance()
            usdt = bal.get("USDT", {})
            free = usdt.get("free")
            if free is None:
                free = usdt.get("total", 0)
            return float(free or 0)
        except Exception as e:
            log(f"fetch_balance hatasi: {e}", "ERROR")
            return None

    def last_price(self, symbol):
        try:
            return float(self.ex.fetch_ticker(symbol)["last"])
        except Exception as e:
            log(f"{symbol} ticker hatasi: {e}", "ERROR")
            return None

    def ohlcv(self, symbol, timeframe, limit):
        try:
            return self.ex.fetch_ohlcv(symbol, timeframe, limit=min(limit, 1500))
        except Exception as e:
            log(f"{symbol} {timeframe} kline hatasi: {e}", "ERROR")
            return None

    def oi_history(self, symbol, timeframe, limit):
        try:
            return self.ex.fetch_open_interest_history(symbol, timeframe, limit=min(limit, 500))
        except Exception as e:
            log(f"{symbol} OI gecmisi hatasi: {e}", "ERROR")
            return None

    def funding_rate(self, symbol):
        try:
            fr = self.ex.fetch_funding_rate(symbol)
            rate = fr.get("fundingRate")
            return float(rate) * 100.0 if rate is not None else None  # yuzde
        except Exception as e:
            log(f"{symbol} funding hatasi: {e}", "ERROR")
            return None

    def min_notional(self, symbol):
        try:
            limits = self.markets[symbol].get("limits", {})
            cost = limits.get("cost", {}) or {}
            return float(cost.get("min") or 0)
        except Exception:
            return 0

    def max_amount(self, symbol):
        """Emir basina maksimum miktar: LOT_SIZE ve MARKET_LOT_SIZE'in kucugu.
        Bilinmiyorsa None doner."""
        try:
            limits = self.markets[symbol].get("limits", {}) or {}
            adaylar = []
            for anahtar in ("amount", "market"):
                mx = (limits.get(anahtar) or {}).get("max")
                if mx:
                    adaylar.append(float(mx))
            return min(adaylar) if adaylar else None
        except Exception:
            return None

    # --- emirler ---
    def market_order(self, symbol, side, notional_usdt):
        """side: 'sell' (short ac) | 'buy' (long ac)."""
        price = self.last_price(symbol)
        if not price:
            return None
        raw_amount = notional_usdt / price
        amount = float(self.ex.amount_to_precision(symbol, raw_amount))
        if amount <= 0:
            log(f"{symbol} miktar 0 cikti (fiyat {price}) - atlandi", "WARN")
            return None

        # maksimum miktar siniri: asilirsa limite kirp (sinyali kacirmamak icin)
        kirpma = None
        mx = self.max_amount(symbol)
        if mx and amount > mx:
            yeni = float(self.ex.amount_to_precision(symbol, mx))
            if yeni <= 0:
                log(f"{symbol} max miktar limiti gecersiz ({mx}) - atlandi", "WARN")
                return None
            kirpma = {"istenen": amount, "uygulanan": yeni,
                      "istenen_usdt": notional_usdt, "uygulanan_usdt": yeni * price}
            log(f"{symbol} max miktar siniri: {amount} -> {yeni} "
                f"(${notional_usdt:.0f} -> ${yeni*price:.0f})", "WARN")
            amount = yeni

        min_cost = self.min_notional(symbol)
        if min_cost and amount * price < min_cost:
            log(f"{symbol} minNotional altinda ({amount*price:.2f} < {min_cost}) - atlandi", "WARN")
            return None
        order = self.ex.create_order(symbol, "market", side, amount)
        fill = self._fill_info(symbol, order, amount)
        if fill and kirpma:
            fill["kirpma"] = kirpma
        return fill

    def _fill_info(self, symbol, order, fallback_amount):
        avg = order.get("average") or order.get("price")
        filled = order.get("filled") or fallback_amount
        if not avg:
            try:
                o = self.ex.fetch_order(order["id"], symbol)
                avg = o.get("average") or o.get("price")
                filled = o.get("filled") or filled
            except Exception:
                pass
        if not avg:
            avg = self.last_price(symbol)
        return {"id": order.get("id"), "price": float(avg), "amount": float(filled)}

    def place_tp_sl(self, symbol, pos_side, tp_price, sl_price,
                    send_tp=True, send_sl=True):
        """pos_side: 'SHORT' | 'LONG'. Fiyatlar mutlak.
        closePosition=true -> pozisyon kapaninca karsi emir otomatik iptal.
        send_tp/send_sl=False: seviye hesaplanir ama Binance'e emir GONDERILMEZ
        (dinamik cikis AND modunda kullanilir; bot kendisi izler)."""
        close_side = "buy" if pos_side == "SHORT" else "sell"
        tp = float(self.ex.price_to_precision(symbol, tp_price))
        sl = float(self.ex.price_to_precision(symbol, sl_price))
        tp_id = sl_id = None
        if send_tp:
            try:
                o = self.ex.create_order(symbol, "TAKE_PROFIT_MARKET", close_side, None, None,
                                         {"stopPrice": tp, "closePosition": True})
                tp_id = o.get("id")
            except Exception as e:
                log(f"{symbol} TP emri BASARISIZ: {e}", "ERROR")
        if send_sl:
            try:
                o = self.ex.create_order(symbol, "STOP_MARKET", close_side, None, None,
                                         {"stopPrice": sl, "closePosition": True})
                sl_id = o.get("id")
            except Exception as e:
                log(f"{symbol} SL emri BASARISIZ: {e}", "ERROR")
        return {"tp_price": tp, "sl_price": sl, "tp_id": tp_id, "sl_id": sl_id}

    def close_market(self, symbol, pos_side, amount):
        close_side = "buy" if pos_side == "SHORT" else "sell"
        try:
            return self.ex.create_order(symbol, "market", close_side, amount,
                                        None, {"reduceOnly": True})
        except Exception as e:
            log(f"{symbol} acil kapatma hatasi: {e}", "ERROR")
            return None

    def open_orders(self, symbol):
        """Semboldeki acik emirler. Hata olursa None."""
        try:
            return self.ex.fetch_open_orders(symbol)
        except Exception as e:
            log(f"{symbol} acik emirler okunamadi: {e}", "WARN")
            return None

    @staticmethod
    def _order_kind(o):
        """Emri TP / SL olarak siniflandir."""
        tip = (o.get("type") or "").upper()
        bilgi = o.get("info") or {}
        ham = (bilgi.get("type") or bilgi.get("origType") or "").upper()
        metin = tip + " " + ham
        if "TAKE_PROFIT" in metin:
            return "TP"
        if "STOP" in metin:
            return "SL"
        return None

    def protective_orders(self, symbol):
        """Semboldeki koruma emirlerini tipe gore grupla: {'TP': [...], 'SL': [...]}
        Hata olursa None."""
        emirler = self.open_orders(symbol)
        if emirler is None:
            return None
        out = {"TP": [], "SL": []}
        for o in emirler:
            k = self._order_kind(o)
            if k:
                out[k].append(o)
        return out

    def cancel_protective(self, symbol, kind):
        """Belirtilen tipteki TUM koruma emirlerini iptal et ve dogrula.
        Doner: (basarili_mi, mesaj)."""
        gruplar = self.protective_orders(symbol)
        if gruplar is None:
            return False, "acik emirler okunamadi"
        hedef = gruplar.get(kind, [])
        if not hedef:
            return True, "iptal edilecek emir yok"
        for o in hedef:
            self.cancel_order(o.get("id"), symbol)
        time.sleep(0.4)                       # borsa tarafinda islenmesini bekle
        kontrol = self.protective_orders(symbol)
        if kontrol is None:
            return False, "iptal dogrulanamadi"
        kalan = len(kontrol.get(kind, []))
        if kalan:
            return False, f"{kalan} adet {kind} emri iptal edilemedi"
        return True, f"{len(hedef)} adet {kind} emri iptal edildi"

    def cancel_order(self, order_id, symbol):
        if not order_id:
            return True
        try:
            self.ex.cancel_order(str(order_id), symbol)
            return True
        except Exception as e:
            # zaten dolmus/iptal edilmis olabilir - engelleyici degil
            log(f"{symbol} emir iptali ({order_id}): {e}", "WARN")
            return False

    def place_single(self, symbol, pos_side, kind, stop_price):
        """Tek koruma emri gonder. kind: 'TP' | 'SL'.
        Doner: (id, kesin_fiyat, hata_mesaji)."""
        close_side = "buy" if pos_side == "SHORT" else "sell"
        tip = "TAKE_PROFIT_MARKET" if kind == "TP" else "STOP_MARKET"
        fiyat = float(self.ex.price_to_precision(symbol, stop_price))
        try:
            o = self.ex.create_order(symbol, tip, close_side, None, None,
                                     {"stopPrice": fiyat, "closePosition": True})
            return o.get("id"), fiyat, None
        except Exception as e:
            mesaj = str(e)
            log(f"{symbol} {kind} emri kurulamadi: {mesaj}", "ERROR")
            return None, fiyat, mesaj

    def cancel_all(self, symbol):
        try:
            self.ex.cancel_all_orders(symbol)
        except Exception as e:
            log(f"{symbol} emir iptali: {e}", "WARN")


# ======================================================================
# DINAMIK CIKIS YAPILANDIRMASI
# ======================================================================

def extract_dynamic(src_row, prefix):
    """sts_rules veya sts_settings satirindan dinamik cikis blogunu cikar.
    prefix: 'dyn_tp' | 'dyn_sl'. Pasif/eksikse None doner."""
    if not src_row:
        return None
    if not src_row.get(f"{prefix}_active"):
        return None
    conds = src_row.get(f"{prefix}_conditions")
    if isinstance(conds, str):
        try:
            conds = json.loads(conds)
        except Exception:
            conds = None
    if not conds:
        return None
    mode = (src_row.get(f"{prefix}_mode") or "OR").upper()
    if mode not in ("OR", "AND"):
        mode = "OR"
    tf = src_row.get(f"{prefix}_timeframe") or "5m"
    if tf not in TF_SECONDS:
        tf = "5m"
    logic = (src_row.get(f"{prefix}_logic") or "AND").upper()
    if logic not in ("AND", "OR"):
        logic = "AND"
    return {"active": True, "timeframe": tf, "conditions": conds,
            "logic": logic, "mode": mode}


# ======================================================================
# KURAL DEGERLENDIRME
# ======================================================================

def required_bars(conditions):
    """Kosullarin ihtiyac duydugu mum sayisi."""
    need = 60
    for c in conditions:
        t = c.get("type")
        p1 = int(num(c.get("p1"), 0) or 0)
        if t == "ema_cross":
            p2 = int(num(c.get("p2"), 0) or 0)
            need = max(need, max(p1, p2) * 5)
        elif t == "rsi":
            need = max(need, (p1 if p1 > 0 else 14) * 5)
        elif t == "volume":
            need = max(need, p1 + 3)
    return min(need, 1500)


def eval_conditions(rule, ctx):
    """Kosullari degerlendir. Doner: (sonuc_bool, aciklama_listesi)."""
    conds = rule.get("conditions") or []
    if isinstance(conds, str):
        try:
            conds = json.loads(conds)
        except Exception:
            return False, ["conditions JSON okunamadi"]
    logic = (rule.get("logic") or "AND").upper()
    results, notes = [], []

    for c in conds:
        t = (c.get("type") or "").lower()
        op = c.get("op") or "<"
        p1 = num(c.get("p1"))
        p2 = num(c.get("p2"))
        ok, note = False, f"{t}?"

        if t == "ema_cross":
            a = ema(ctx["closes"], int(p1 or 0))
            b = ema(ctx["closes"], int(p2 or 0))
            ok = compare(a, op, b)
            if a is not None and b is not None:
                note = f"ema{int(p1)}={a:.6g} {op} ema{int(p2)}={b:.6g} -> {ok}"
            else:
                note = f"ema hesaplanamadi (mum yetersiz)"
        elif t == "rsi":
            # periyot sabit 14 (panelde tek deger alinir: esik)
            per = int(p1) if p1 else 14
            r = rsi(ctx["closes"], per)
            ok = compare(r, op, p2)
            note = f"rsi{per}={r:.2f} {op} {p2} -> {ok}" if r is not None else "rsi hesaplanamadi"
        elif t == "price":
            lp = ctx.get("last_close")
            ok = compare(lp, op, p2)
            note = f"fiyat={lp} {op} {p2} -> {ok}"
        elif t == "oi_change":
            chg = ctx.get("oi_change_pct")
            # deger her zaman pozitif girilir, yonu operator belirler:
            #   ">" 5 -> ortalamadan %5 BUYUK   |   "<" 7 -> ortalamadan %7 KUCUK
            esik = abs(p2) if p2 is not None else None
            if esik is not None and op in ("<", "<="):
                esik = -esik
            ok = compare(chg, op, esik)
            yon = "buyuk" if op in (">", ">=") else "kucuk"
            note = (f"oi son {int(p1 or 3)} bar ort. gore="
                    f"{chg if chg is None else round(chg,2)}% "
                    f"(esik %{p2} {yon}) -> {ok}")
        elif t == "volume":
            chg = ctx.get("vol_change_pct")
            esik = abs(p2) if p2 is not None else None
            if esik is not None and op in ("<", "<="):
                esik = -esik
            ok = compare(chg, op, esik)
            yon = "buyuk" if op in (">", ">=") else "kucuk"
            note = (f"hacim son {int(p1 or 3)} bar ort. gore="
                    f"{chg if chg is None else round(chg,2)}% "
                    f"(esik %{p2} {yon}) -> {ok}")
        elif t == "funding":
            f = ctx.get("funding_pct")
            ok = compare(f, op, p2)
            note = f"funding={f if f is None else round(f,4)}% {op} {p2}% -> {ok}"
        else:
            note = f"bilinmeyen kosul tipi: {t}"

        results.append(ok)
        notes.append(note)

    if not results:
        return False, ["kosul yok"]
    final = all(results) if logic != "OR" else any(results)
    return final, notes


# ======================================================================
# BOT
# ======================================================================

class Bot:
    def __init__(self):
        self.ex = Exchange()
        self.running = True
        self.killswitch = False
        self.last_signal_id = None
        self.open_trades = {}        # unified_symbol -> trade row
        self.rules = []              # aktif kurallar (cache)
        self.rules_loaded_at = 0
        self.settings_loaded_at = 0
        self.settings_warned = False
        self.settings_row = None     # sinyal havuzu dinamik cikis yapilandirmasi
        self.webhook_checked_at = 0
        self.rule_last_bar = {}      # rule_id -> son degerlendirilen bar id
        self.dyn_last_bar = {}       # (trade_id, dyn_tp|dyn_sl) -> son bar id
        self.kline_cache = {}        # (sym, tf) -> (bar_id, ohlcv)
        self._restore_open_trades()

    # ------------------------------------------------------------------
    def _restore_open_trades(self):
        for row in sb_open_trades():
            sym = self.ex.unified(row.get("coin", ""))
            if sym:
                self.open_trades[sym] = row
        if self.open_trades:
            log(f"Restart: {len(self.open_trades)} acik islem geri yuklendi")

    def refresh_control(self):
        """Kill-switch + last_signal_id Supabase'den. Dosya flag'i yerel yedek."""
        ctrl = sb_get_control()
        ks_db = bool(ctrl.get("killswitch")) if ctrl else False
        self.killswitch = ks_db or os.path.exists(STOP_FLAG)
        if ctrl and self.last_signal_id is None and ctrl.get("last_signal_id") is not None:
            self.last_signal_id = int(ctrl["last_signal_id"])
        return ctrl

    def refresh_settings(self, force=False):
        """Strateji ayarlarini Supabase'den oku (SETTINGS_POLL_SEC araligiyla).
        Ulasilamazsa mevcut CFG (env varsayilanlari) korunur."""
        nowt = time.time()
        if not force and nowt - self.settings_loaded_at < SETTINGS_POLL_SEC:
            return
        self.settings_loaded_at = nowt
        row = sb_get_settings()
        if row is None:
            if not self.settings_warned:
                log("Ayarlar okunamadi - env varsayilanlari kullaniliyor", "WARN")
                self.settings_warned = True
            return
        self.settings_warned = False
        self.settings_row = row
        changed = apply_settings(row)
        if changed:
            ozet = ", ".join(f"{k}={CFG[k]}" for k in changed)
            log(f"Ayarlar guncellendi: {ozet}")
            sb_log_event("SETTINGS", None, ozet)

    def init_last_id(self):
        self.refresh_control()
        if self.last_signal_id is None:
            max_id = sb_max_signal_id()
            self.last_signal_id = max_id
            sb_update_control({"last_signal_id": max_id})
            log(f"Ilk acilis: son sinyal id={max_id} baz alindi (gecmis islenmeyecek)")
        else:
            log(f"Devam: son islenen sinyal id={self.last_signal_id}")

    def stopped(self):
        return self.killswitch

    def count_pools(self, live):
        """Canli pozisyonlari kaynaklarina gore say.
        Kayitsiz pozisyon = sinyal sayilir (muhafazakar)."""
        sig = rule = 0
        for sym in live:
            t = self.open_trades.get(sym)
            if t and t.get("source") == "rule":
                rule += 1
            else:
                sig += 1
        return sig, rule

    def sl_risk_total(self):
        """Acik kayitli islemlerin toplam SL riski (USDT)."""
        total = 0.0
        for t in self.open_trades.values():
            try:
                entry = float(t.get("entry_price") or 0)
                slp = float(t.get("sl_price") or 0)
                amt = float(t.get("amount") or 0)
                if entry and slp and amt:
                    total += abs(entry - slp) * amt
                else:
                    total += 150.0
            except (TypeError, ValueError):
                total += 150.0
        return total

    # ------------------------------------------------------------------
    def run(self):
        self.refresh_settings(force=True)   # log satiri guncel ayarlari gostersin
        mode = "TESTNET" if TESTNET else "CANLI"
        log(f"STS EXECUTOR {VERSION} basladi | mod={mode} | "
            f"sinyal: ${CFG['margin_usdt']} x{CFG['leverage']} {CFG['margin_mode']} "
            f"TP-{CFG['tp_pct']}% SL+{CFG['sl_pct']}% max={CFG['max_positions']} | "
            f"kural havuzu max={CFG['max_rule_positions']}")
        tg_send(f"<b>STS basladi</b> ({mode})\n"
                f"Sinyal: ${CFG['margin_usdt']} x{CFG['leverage']} | "
                f"TP -{CFG['tp_pct']}% SL +{CFG['sl_pct']}% | max {CFG['max_positions']}\n"
                f"Kural havuzu: max {CFG['max_rule_positions']}")
        self.init_last_id()

        while self.running:
            try:
                self.refresh_control()
                self.refresh_settings()
                live = self.check_closed_positions()
                self.write_status(live)
                self.process_position_requests(live)
                self.monitor_dynamic_exits(live)
                self.refresh_rules()
                if self.stopped():
                    log("KILL-SWITCH aktif - yeni pozisyon acilmiyor", "WARN")
                else:
                    self.process_signals()
                    self.process_rules()
                    self.process_webhooks()
            except Exception as e:
                log(f"Dongu hatasi: {e}", "ERROR")
                sb_log_event("ERROR", None, f"dongu: {e}")
            for _ in range(int(CFG["poll_seconds"])):
                if not self.running:
                    break
                time.sleep(1)

        log("Bot durduruldu")
        tg_send("<b>STS durduruldu</b>")

    # ------------------------------------------------------------------
    # KAPANAN POZISYONLAR
    # ------------------------------------------------------------------
    def check_closed_positions(self):
        live = self.ex.open_positions()
        if live is None:
            return None
        for sym in list(self.open_trades.keys()):
            if sym in live:
                continue
            trade = self.open_trades.pop(sym)
            self._record_exit(sym, trade)
        return live

    def write_status(self, live):
        """Panel icin durum snapshot'i - executor key sahibi, panel sadece okur."""
        if live is None:
            return
        balance = self.ex.free_usdt()
        sig_count, rule_count = self.count_pools(live)
        positions = []
        for sym, p in live.items():
            t = self.open_trades.get(sym) or {}
            # koruma emirleri BORSADA gercekten duruyor mu (kayda guvenme)
            gruplar = self.ex.protective_orders(sym)
            tp_var = None if gruplar is None else len(gruplar.get("TP", [])) > 0
            sl_var = None if gruplar is None else len(gruplar.get("SL", [])) > 0
            positions.append({
                "symbol": sym,
                "trade_id": t.get("id"),
                "coin": t.get("coin") or sym.split("/")[0],
                "side": (p.get("side") or t.get("side") or "").upper(),
                "source": t.get("source") or "signal",
                "contracts": p.get("contracts"),
                "entry": p.get("entryPrice"),
                "mark": p.get("markPrice"),
                "upnl": p.get("unrealizedPnl"),
                "tp": t.get("tp_price"),
                "sl": t.get("sl_price"),
                "tp_order_var": tp_var,
                "sl_order_var": sl_var,
                "leverage": t.get("leverage"),
                "margin": t.get("margin_usdt"),
            })
        sb_upsert_status({
            "version": VERSION,
            "testnet": TESTNET,
            "killswitch": self.stopped(),
            "balance": balance,
            "sig_count": sig_count, "sig_max": CFG["max_positions"],
            "rule_count": rule_count, "rule_max": CFG["max_rule_positions"],
            "positions": positions,
        })

    def _record_exit(self, symbol, trade):
        reason = "UNKNOWN"
        exit_price = None
        for oid, tag in ((trade.get("tp_order_id"), "TP"),
                         (trade.get("sl_order_id"), "SL")):
            if not oid:
                continue
            try:
                o = self.ex.ex.fetch_order(oid, symbol)
                if o.get("status") == "closed" and (o.get("filled") or 0) > 0:
                    reason = tag
                    exit_price = o.get("average") or o.get("price")
                    break
            except Exception:
                continue
        if exit_price is None:
            exit_price = self.ex.last_price(symbol)
            if reason == "UNKNOWN":
                reason = "MANUEL/BILINMIYOR"

        entry = float(trade.get("entry_price") or 0)
        amount = float(trade.get("amount") or 0)
        side = (trade.get("side") or "SHORT").upper()
        pnl = None
        if entry and exit_price and amount:
            if side == "SHORT":
                pnl = round((entry - float(exit_price)) * amount, 4)
            else:
                pnl = round((float(exit_price) - entry) * amount, 4)

        self.ex.cancel_all(symbol)

        if trade.get("id"):
            sb_close_trade(trade["id"], exit_price, pnl, reason)

        coin = trade.get("coin", symbol)
        pnl_txt = f"{pnl:+.2f} USDT" if pnl is not None else "?"
        log(f"KAPANDI {coin} {side} | {reason} | cikis {exit_price} | PnL {pnl_txt}")
        sb_log_event("CLOSE", coin, f"{side} {reason} cikis={exit_price} pnl={pnl}")
        tg_send(f"<b>KAPANDI</b> {coin} {side}\nSebep: {reason}\nCikis: {exit_price}\nPnL: <b>{pnl_txt}</b>")

    def build_ctx(self, symbol, tf, conds, bar_id):
        """Kosullarin ihtiyac duydugu gosterge verisini topla (lazy + onbellekli).
        Giris kurallari ve dinamik cikis ayni fonksiyonu kullanir."""
        ctx = {}
        cond_types = {(c.get("type") or "").lower() for c in conds}

        need_kline = cond_types & {"ema_cross", "rsi", "price", "volume"}
        if need_kline:
            bars = required_bars(conds)
            cache_key = (symbol, tf)
            cached = self.kline_cache.get(cache_key)
            if cached and cached[0] == bar_id:
                ohlcv = cached[1]
            else:
                ohlcv = self.ex.ohlcv(symbol, tf, bars + 2)
                if ohlcv:
                    self.kline_cache[cache_key] = (bar_id, ohlcv)
            if not ohlcv or len(ohlcv) < 3:
                return None
            closed = ohlcv[:-1]                # son eleman canli mum
            ctx["closes"] = [c[4] for c in closed]
            ctx["last_close"] = closed[-1][4]
            vols = [c[5] for c in closed]
            for c in conds:
                if (c.get("type") or "").lower() == "volume":
                    n = int(num(c.get("p1"), 3) or 3)
                    if len(vols) > n:
                        prev = vols[-(n + 1):-1]
                        avg = sum(prev) / len(prev)
                        if avg > 0:
                            ctx["vol_change_pct"] = (vols[-1] - avg) / avg * 100.0
                    break

        if "oi_change" in cond_types:
            for c in conds:
                if (c.get("type") or "").lower() == "oi_change":
                    n = int(num(c.get("p1"), 3) or 3)
                    hist = self.ex.oi_history(symbol, tf, n + 2)
                    if hist and len(hist) >= n + 1:
                        vals = [h.get("openInterestValue") or h.get("openInterestAmount")
                                for h in hist]
                        vals = [float(v) for v in vals if v is not None]
                        if len(vals) >= n + 1:
                            prev = vals[-(n + 1):-1]
                            avg = sum(prev) / len(prev)
                            if avg > 0:
                                ctx["oi_change_pct"] = (vals[-1] - avg) / avg * 100.0
                    break

        if "funding" in cond_types:
            ctx["funding_pct"] = self.ex.funding_rate(symbol)

        return ctx

    # ------------------------------------------------------------------
    # ACIK POZISYON YONETIMI (panelden gelen istekler)
    # ------------------------------------------------------------------
    def refresh_open_trades(self, live):
        """Acik kayitlari Supabase'den tazele: panel dinamik cikis veya
        istek alanlarini degistirmis olabilir."""
        rows = sb_open_trades()
        if rows is None:
            return []
        guncel = []
        for row in rows:
            sym = row.get("symbol") or self.ex.unified(row.get("coin", ""))
            if not sym:
                continue
            eski = self.open_trades.get(sym)
            if eski:
                # bellekteki kaydi tazele (id, order id'leri korunur)
                eski.update({k: row.get(k) for k in
                             ("dyn_tp", "dyn_sl", "tp_price", "sl_price",
                              "tp_order_id", "sl_order_id",
                              "req_tp_price", "req_sl_price", "req_close")})
                guncel.append((sym, eski))
            elif live is not None and sym in live:
                self.open_trades[sym] = row
                guncel.append((sym, row))
        return guncel

    def process_position_requests(self, live):
        """Panelden gelen hard TP/SL degisikligi ve kapatma isteklerini uygula."""
        if live is None:
            return
        for sym, trade in self.refresh_open_trades(live):
            if sym not in live:
                continue
            try:
                if trade.get("req_close"):
                    self._req_close(sym, trade, live[sym])
                    continue
                if trade.get("req_tp_price") is not None:
                    self._req_level(sym, trade, "TP", float(trade["req_tp_price"]))
                if trade.get("req_sl_price") is not None:
                    self._req_level(sym, trade, "SL", float(trade["req_sl_price"]))
            except Exception as e:
                coin = trade.get("coin", sym)
                log(f"{coin} pozisyon istegi hatasi: {e}", "ERROR")
                if trade.get("id"):
                    sb_update_trade(trade["id"], {
                        "req_tp_price": None, "req_sl_price": None,
                        "req_close": False, "req_result": f"HATA: {e}"[:200]})

    def _req_level(self, symbol, trade, kind, yeni_fiyat):
        """Hard TP veya SL seviyesini degistir: eski emri iptal et, yenisini kur."""
        coin = trade.get("coin", symbol)
        side = (trade.get("side") or "SHORT").upper()
        entry = float(trade.get("entry_price") or 0)

        # mantik kontrolu
        if entry:
            if kind == "TP":
                mantikli = yeni_fiyat < entry if side == "SHORT" else yeni_fiyat > entry
            else:
                mantikli = yeni_fiyat > entry if side == "SHORT" else yeni_fiyat < entry
            if not mantikli:
                mesaj = f"{kind} seviyesi girise gore mantiksiz (giris {entry})"
                log(f"{coin} {mesaj}", "WARN")
                sb_update_trade(trade["id"], {f"req_{kind.lower()}_price": None,
                                              "req_result": mesaj[:200]})
                return

        alan = "tp_order_id" if kind == "TP" else "sl_order_id"
        eski_id = trade.get(alan)

        # AND modunda hard emir Binance'te YOK - sadece seviye guncellenir
        dyn = trade.get("dyn_tp" if kind == "TP" else "dyn_sl")
        if isinstance(dyn, str):
            try:
                dyn = json.loads(dyn)
            except Exception:
                dyn = None
        and_modu = bool(dyn and dyn.get("active") and
                        (dyn.get("mode") or "OR").upper() == "AND")

        alan_fiyat_eski = trade.get("tp_price" if kind == "TP" else "sl_price")

        yeni_id = None
        if and_modu:
            kesin = float(self.ex.ex.price_to_precision(symbol, yeni_fiyat))
        else:
            # Binance ayni yonde ikinci closePosition emrini REDDEDER (-4130).
            # Kayittaki id yanlis olabilir; borsadan okuyup tipe gore iptal et.
            iptal_ok, iptal_msg = self.ex.cancel_protective(symbol, kind)
            if not iptal_ok:
                mesaj = f"{kind} degistirilemedi: {iptal_msg}"[:200]
                log(f"{coin} {mesaj} - mevcut emir korunuyor", "WARN")
                sb_update_trade(trade["id"], {
                    f"req_{kind.lower()}_price": None,
                    "req_at": datetime.now(timezone.utc).isoformat(),
                    "req_result": mesaj})
                sb_log_event("LEVEL_FAIL", coin, mesaj)
                tg_send(f"<b>{kind} DEGISTIRILEMEDI</b> {coin}\n{iptal_msg}\n"
                        f"Mevcut emir korundu, pozisyon guvende.")
                return
            yeni_id, kesin, hata = self.ex.place_single(symbol, side, kind, yeni_fiyat)
            if yeni_id is None:
                geri_id = None
                if alan_fiyat_eski:
                    geri_id, _, _ = self.ex.place_single(symbol, side, kind,
                                                         float(alan_fiyat_eski))
                if geri_id:
                    mesaj = f"{kind} degistirilemedi: {hata}"[:200]
                    log(f"{coin} {kind} degistirilemedi, eski seviye geri kuruldu "
                        f"({alan_fiyat_eski})", "WARN")
                    sb_update_trade(trade["id"], {
                        f"req_{kind.lower()}_price": None,
                        (("tp_order_id") if kind == "TP" else "sl_order_id"): str(geri_id),
                        "req_at": datetime.now(timezone.utc).isoformat(),
                        "req_result": mesaj})
                    sb_log_event("LEVEL_FAIL", coin, mesaj)
                    tg_send(f"<b>{kind} DEGISTIRILEMEDI</b> {coin}\n{hata}\n"
                            f"Eski seviye ({alan_fiyat_eski}) geri kuruldu.")
                else:
                    mesaj = f"{kind} KORUMASIZ: {hata}"[:200]
                    log(f"{coin} {kind} emri yok - POZISYON KORUMASIZ!", "ERROR")
                    sb_update_trade(trade["id"], {
                        f"req_{kind.lower()}_price": None,
                        (("tp_order_id") if kind == "TP" else "sl_order_id"): None,
                        "req_at": datetime.now(timezone.utc).isoformat(),
                        "req_result": mesaj})
                    sb_log_event("ERROR", coin, mesaj)
                    tg_send(f"<b>ACIL - {coin}</b>\n{kind} emri kurulamadi ve eski emir "
                            f"geri getirilemedi. Pozisyon {kind} korumasi OLMADAN acik.\n"
                            f"Hata: {hata}\nElle mudahale et.")
                return

        alan_fiyat = "tp_price" if kind == "TP" else "sl_price"
        sb_update_trade(trade["id"], {
            alan_fiyat: kesin, alan: str(yeni_id) if yeni_id else None,
            f"req_{kind.lower()}_price": None,
            "req_at": datetime.now(timezone.utc).isoformat(),
            "req_result": f"{kind} -> {kesin}",
        })
        trade[alan_fiyat] = kesin
        trade[alan] = str(yeni_id) if yeni_id else None

        log(f"{coin} {kind} guncellendi -> {kesin}" + (" (AND modu, emir yok)" if and_modu else ""))
        sb_log_event("LEVEL_CHANGE", coin, f"{kind} -> {kesin}")
        tg_send(f"<b>{kind} DEGISTI</b> {coin}\nYeni seviye: {kesin}")

    def _req_close(self, symbol, trade, pos):
        """Panelden elle kapatma istegi."""
        coin = trade.get("coin", symbol)
        side = (trade.get("side") or "SHORT").upper()
        amount = float(pos.get("contracts") or trade.get("amount") or 0)
        if amount <= 0:
            sb_update_trade(trade["id"], {"req_close": False,
                                          "req_result": "miktar okunamadi"})
            return

        self.ex.cancel_all(symbol)
        res = self.ex.close_market(symbol, side, amount)
        if not res:
            sb_update_trade(trade["id"], {"req_close": False,
                                          "req_result": "kapatma emri basarisiz"})
            tg_send(f"<b>UYARI</b> {coin}\nElle kapatma basarisiz oldu.")
            return

        cikis = None
        try:
            cikis = res.get("average") or res.get("price")
        except Exception:
            pass
        if not cikis:
            cikis = self.ex.last_price(symbol)

        entry = float(trade.get("entry_price") or 0)
        pnl = None
        if entry and cikis:
            pnl = round((entry - float(cikis)) * amount, 4) if side == "SHORT" \
                else round((float(cikis) - entry) * amount, 4)

        sb_close_trade(trade["id"], cikis, pnl, "MANUEL_PANEL")
        sb_update_trade(trade["id"], {"req_close": False, "req_result": "kapatildi"})
        self.open_trades.pop(symbol, None)

        pnl_txt = f"{pnl:+.2f} USDT" if pnl is not None else "?"
        log(f"PANELDEN KAPATILDI {coin} {side} | cikis {cikis} | PnL {pnl_txt}")
        sb_log_event("CLOSE", coin, f"{side} MANUEL_PANEL cikis={cikis} pnl={pnl}")
        tg_send(f"<b>PANELDEN KAPATILDI</b> {coin} {side}\nCikis: {cikis}\nPnL: <b>{pnl_txt}</b>")

    # ------------------------------------------------------------------
    # DINAMIK CIKIS
    # ------------------------------------------------------------------
    def monitor_dynamic_exits(self, live):
        """Acik pozisyonlarda dinamik TP/SL kosullarini bar kapanisinda kontrol et."""
        if live is None or not self.open_trades:
            return
        for sym in list(self.open_trades.keys()):
            if sym not in live:
                continue
            trade = self.open_trades[sym]
            for tag in ("dyn_tp", "dyn_sl"):
                cfg = trade.get(tag)
                if isinstance(cfg, str):
                    try:
                        cfg = json.loads(cfg)
                    except Exception:
                        cfg = None
                if not cfg or not cfg.get("active"):
                    continue
                try:
                    if self._check_dynamic(sym, trade, live[sym], tag, cfg):
                        break          # pozisyon kapandi, digerine bakma
                except Exception as e:
                    log(f"{trade.get('coin', sym)} {tag} degerlendirme hatasi: {e}", "ERROR")

    def _check_dynamic(self, symbol, trade, pos, tag, cfg):
        """Tek bir dinamik blogu degerlendir. Kapatildiysa True doner."""
        tf = cfg.get("timeframe") or "5m"
        tf_sec = TF_SECONDS.get(tf)
        if not tf_sec:
            return False

        conds = cfg.get("conditions") or []
        if isinstance(conds, str):
            try:
                conds = json.loads(conds)
            except Exception:
                return False
        if not conds:
            return False

        # bar takibi: her kapanmis mum icin bir kez
        bar_id = int(time.time() // tf_sec)
        anahtar = (trade.get("id") or symbol, tag)
        if self.dyn_last_bar.get(anahtar) == bar_id:
            return False
        if time.time() - bar_id * tf_sec < 5:
            return False
        self.dyn_last_bar[anahtar] = bar_id

        ctx = self.build_ctx(symbol, tf, conds, bar_id)
        if ctx is None:
            return False

        ok, notes = eval_conditions({"conditions": conds,
                                     "logic": cfg.get("logic") or "AND"}, ctx)
        if not ok:
            return False

        # AND modu: hard seviyeye de ulasilmis olmali
        mode = (cfg.get("mode") or "OR").upper()
        if mode == "AND":
            hard = trade.get("tp_price") if tag == "dyn_tp" else trade.get("sl_price")
            mark = ctx.get("last_close") or self.ex.last_price(symbol)
            if not hard or not mark:
                return False
            side = (trade.get("side") or "SHORT").upper()
            if tag == "dyn_tp":
                ulasti = mark <= float(hard) if side == "SHORT" else mark >= float(hard)
            else:
                ulasti = mark >= float(hard) if side == "SHORT" else mark <= float(hard)
            if not ulasti:
                return False
            notes.append(f"AND: hard seviye {hard} ulasildi (fiyat {mark})")

        coin = trade.get("coin", symbol)
        sebep = "DYN_TP" if tag == "dyn_tp" else "DYN_SL"
        aciklama = " | ".join(notes)
        log(f"{sebep} TETIKLENDI {coin}: {aciklama}")
        sb_log_event(sebep, coin, aciklama)

        side = (trade.get("side") or "SHORT").upper()
        amount = float(pos.get("contracts") or trade.get("amount") or 0)
        if amount <= 0:
            return False

        self.ex.cancel_all(symbol)          # bekleyen hard emirleri temizle
        res = self.ex.close_market(symbol, side, amount)
        if not res:
            log(f"{coin} {sebep} kapatma emri BASARISIZ", "ERROR")
            sb_log_event("ERROR", coin, f"{sebep} kapatma basarisiz")
            return False

        cikis = None
        try:
            cikis = res.get("average") or res.get("price")
        except Exception:
            pass
        if not cikis:
            cikis = self.ex.last_price(symbol)

        entry = float(trade.get("entry_price") or 0)
        pnl = None
        if entry and cikis:
            pnl = round((entry - float(cikis)) * amount, 4) if side == "SHORT" \
                else round((float(cikis) - entry) * amount, 4)

        if trade.get("id"):
            sb_close_trade(trade["id"], cikis, pnl, sebep)
        self.open_trades.pop(symbol, None)

        pnl_txt = f"{pnl:+.2f} USDT" if pnl is not None else "?"
        tg_send(f"<b>{sebep}</b> {coin} {side}\nCikis: {cikis}\nPnL: <b>{pnl_txt}</b>\n{aciklama}")
        return True

    # ------------------------------------------------------------------
    # HAVUZ 1 - SINYAL
    # ------------------------------------------------------------------
    def process_signals(self):
        last_id = int(self.last_signal_id or 0)
        signals = sb_fetch_new_signals(last_id)
        if not signals:
            return
        for sig in signals:
            sid = int(sig.get("id", 0))
            coin = (sig.get("symbol") or sig.get("coin") or "").strip().upper()
            if coin:
                try:
                    self.handle_signal(sig, coin)
                except Exception as e:
                    log(f"{coin} sinyal islenirken hata: {e}", "ERROR")
                    sb_log_event("ERROR", coin, f"sinyal: {e}")
                    tg_send(f"<b>HATA</b> {coin}\n{e}")
            if sid > int(self.last_signal_id or 0):
                self.last_signal_id = sid
                sb_update_control({"last_signal_id": sid})

    def handle_signal(self, sig, coin):
        stype = sig.get("signal_type", "?")
        log(f"Yeni sinyal: {coin} ({stype}) id={sig.get('id')}")

        symbol = self.ex.unified(coin)
        if not symbol:
            log(f"{coin} Binance futures'ta bulunamadi - atlandi", "WARN")
            sb_log_event("SIGNAL_SKIP", coin, "coin binance futures'ta yok")
            return

        live = self.ex.open_positions()
        if live is None:
            sb_log_event("SIGNAL_SKIP", coin, "pozisyonlar okunamadi")
            return
        if symbol in live or symbol in self.open_trades:
            log(f"{coin} zaten acik pozisyonda - atlandi")
            sb_log_event("SIGNAL_SKIP", coin, "coinde acik pozisyon var")
            return

        sig_count, _ = self.count_pools(live)
        if sig_count >= CFG["max_positions"]:
            log(f"{coin} atlandi - sinyal havuzu dolu ({CFG['max_positions']})", "WARN")
            sb_log_event("SIGNAL_SKIP", coin, f"sinyal havuzu dolu ({sig_count}/{CFG['max_positions']})")
            tg_send(f"<b>ATLANDI</b> {coin} ({stype})\nSebep: sinyal havuzu dolu")
            return

        balance = self.ex.free_usdt()
        if balance is None:
            sb_log_event("SIGNAL_SKIP", coin, "bakiye okunamadi")
            return
        if balance < CFG["min_balance"]:
            log(f"{coin} atlandi - bakiye ${balance:.2f} < ${CFG['min_balance']}", "WARN")
            sb_log_event("SIGNAL_SKIP", coin, f"bakiye yetersiz ({balance:.0f}/{CFG['min_balance']})")
            tg_send(f"<b>ATLANDI</b> {coin} ({stype})\nSebep: bakiye ${balance:.2f}")
            return

        recent = sb_recent_trade_coins(CFG["dedup_days"])
        if coin in recent:
            log(f"{coin} atlandi - son {CFG['dedup_days']} gun icinde islem gordu")
            sb_log_event("SIGNAL_SKIP", coin, f"dedup ({CFG['dedup_days']} gun)")
            return

        self.open_position(
            coin=coin, symbol=symbol, side="SHORT",
            margin=CFG["margin_usdt"], leverage=CFG["leverage"],
            tp_price=None, sl_price=None,
            tp_pct=CFG["tp_pct"], sl_pct=CFG["sl_pct"],
            source="signal", rule_id=None,
            signal_id=sig.get("id"), signal_type=stype,
            dyn_tp=extract_dynamic(self.settings_row, "dyn_tp"),
            dyn_sl=extract_dynamic(self.settings_row, "dyn_sl"),
        )

    # ------------------------------------------------------------------
    # HAVUZ 2 - OZEL KURALLAR
    # ------------------------------------------------------------------
    def refresh_rules(self):
        nowt = time.time()
        if nowt - self.rules_loaded_at < RULE_POLL_SEC:
            return
        rows = sb_active_rules()
        self.rules = rows
        self.rules_loaded_at = nowt

    def process_rules(self):
        if not self.rules:
            return
        now_utc = datetime.now(timezone.utc)

        for rule in self.rules:
            rid = rule.get("id")
            coin = (rule.get("coin") or "").strip().upper()
            tf = rule.get("timeframe") or "5m"
            tf_sec = TF_SECONDS.get(tf)
            if not tf_sec:
                continue

            # gecerlilik
            exp = rule.get("expires_at")
            if exp:
                try:
                    exp_dt = datetime.fromisoformat(exp.replace("Z", "+00:00"))
                    if now_utc >= exp_dt:
                        sb_deactivate_rule(rid)
                        sb_log_event("RULE_EXPIRE", coin, f"kural {rid} suresi doldu")
                        log(f"Kural {rid} ({coin}) suresi doldu - pasife alindi")
                        continue
                except Exception:
                    pass

            # bar takibi: yeni kapanmis mum var mi
            bar_id = int(time.time() // tf_sec)
            if self.rule_last_bar.get(rid) == bar_id:
                continue
            if time.time() - bar_id * tf_sec < 5:
                continue  # mum kapanisindan hemen sonra 5sn bekle
            self.rule_last_bar[rid] = bar_id

            try:
                self.evaluate_rule(rule, coin, tf, tf_sec, bar_id)
            except Exception as e:
                log(f"Kural {rid} ({coin}) degerlendirme hatasi: {e}", "ERROR")
                sb_log_event("ERROR", coin, f"kural {rid}: {e}")

    def evaluate_rule(self, rule, coin, tf, tf_sec, bar_id):
        rid = rule.get("id")
        symbol = self.ex.unified(coin)
        if not symbol:
            sb_deactivate_rule(rid)
            sb_log_event("RULE_EXPIRE", coin, f"kural {rid}: coin binance'ta yok - pasife alindi")
            log(f"Kural {rid}: {coin} binance'ta yok - pasife alindi", "WARN")
            return

        conds = rule.get("conditions") or []
        if isinstance(conds, str):
            try:
                conds = json.loads(conds)
            except Exception:
                conds = []
        # --- veri topla (giris ve dinamik cikis ayni yardimciyi kullanir) ---
        ctx = self.build_ctx(symbol, tf, conds, bar_id)
        if ctx is None:
            return

        # --- degerlendir ---
        ok, notes = eval_conditions(rule, ctx)
        if not ok:
            return

        log(f"KURAL TETIKLENDI {rid} ({coin} {rule.get('direction')}): " + " | ".join(notes))
        sb_log_event("RULE_TRIGGER", coin, f"kural {rid}: " + " | ".join(notes))

        # tetiklendi -> tek seferlik: hemen pasife al (cifte tetik onlenir)
        sb_deactivate_rule(rid, triggered=True)
        self.rules = [r for r in self.rules if r.get("id") != rid]

        self.open_rule_position(rule, coin, symbol)

    def open_rule_position(self, rule, coin, symbol):
        rid = rule.get("id")
        direction = (rule.get("direction") or "SHORT").upper()

        live = self.ex.open_positions()
        if live is None:
            sb_log_event("SIGNAL_SKIP", coin, f"kural {rid}: pozisyonlar okunamadi")
            return
        if symbol in live or symbol in self.open_trades:
            log(f"Kural {rid}: {coin} zaten acik pozisyonda - islem acilmadi", "WARN")
            sb_log_event("SIGNAL_SKIP", coin, f"kural {rid}: coinde acik pozisyon var")
            tg_send(f"<b>KURAL ATLANDI</b> {coin}\nCoinde zaten acik pozisyon var")
            return

        _, rule_count = self.count_pools(live)
        if rule_count >= CFG["max_rule_positions"]:
            log(f"Kural {rid}: ozel havuz dolu ({CFG['max_rule_positions']})", "WARN")
            sb_log_event("SIGNAL_SKIP", coin, f"kural {rid}: ozel havuz dolu")
            tg_send(f"<b>KURAL ATLANDI</b> {coin}\nOzel havuz dolu ({rule_count}/{CFG['max_rule_positions']})")
            return

        margin = float(num(rule.get("margin_usdt"), 100) or 100)
        lev = int(num(rule.get("leverage"), CFG["leverage"]) or CFG["leverage"])
        notional = margin * lev

        # dinamik kasa: mevcut SL riskleri + bu pozisyonun SL riski
        balance = self.ex.free_usdt()
        if balance is None:
            sb_log_event("SIGNAL_SKIP", coin, f"kural {rid}: bakiye okunamadi")
            return
        est_price = self.ex.last_price(symbol)
        new_risk = 150.0
        if est_price:
            slp = self._resolve_level(rule.get("sl_type"), rule.get("sl_value"),
                                      est_price, direction, is_tp=False)
            if slp:
                new_risk = abs(est_price - slp) / est_price * notional
        total_risk = self.sl_risk_total() + new_risk
        if balance - total_risk < CFG["rule_min_free"]:
            log(f"Kural {rid}: kasa yetersiz (bakiye {balance:.0f}, risk {total_risk:.0f})", "WARN")
            sb_log_event("SIGNAL_SKIP", coin,
                         f"kural {rid}: kasa yetersiz (bakiye={balance:.0f} risk={total_risk:.0f})")
            tg_send(f"<b>KURAL ATLANDI</b> {coin}\nKasa yetersiz: bakiye ${balance:.0f}, toplam SL riski ${total_risk:.0f}")
            return

        self.open_position(
            coin=coin, symbol=symbol, side=direction,
            margin=margin, leverage=lev,
            tp_price=self._val_if(rule, "tp", "price"),
            sl_price=self._val_if(rule, "sl", "price"),
            tp_pct=self._val_if(rule, "tp", "pct"),
            sl_pct=self._val_if(rule, "sl", "pct"),
            source="rule", rule_id=rid,
            signal_id=None, signal_type=f"RULE_{rid}",
            dyn_tp=extract_dynamic(rule, "dyn_tp"),
            dyn_sl=extract_dynamic(rule, "dyn_sl"),
        )

    @staticmethod
    def _val_if(rule, which, want_type):
        t = (rule.get(f"{which}_type") or "pct").lower()
        if t == want_type:
            return num(rule.get(f"{which}_value"))
        return None

    @staticmethod
    def _resolve_level(ltype, lvalue, entry, side, is_tp):
        """pct/price -> mutlak fiyat."""
        v = num(lvalue)
        if v is None:
            return None
        if (ltype or "pct").lower() == "price":
            return v
        pct = abs(v) / 100.0
        if side == "SHORT":
            return entry * (1 - pct) if is_tp else entry * (1 + pct)
        return entry * (1 + pct) if is_tp else entry * (1 - pct)

    # ------------------------------------------------------------------
    # HAVUZ 2b - TRADINGVIEW WEBHOOK
    # ------------------------------------------------------------------
    def process_webhooks(self):
        """Kuyruktaki webhook sinyallerini isle. Ozel havuza sayilir."""
        nowt = time.time()
        if nowt - self.webhook_checked_at < WEBHOOK_POLL_SEC:
            return
        self.webhook_checked_at = nowt

        bekleyen = sb_pending_webhooks()
        if not bekleyen:
            return

        for wh in bekleyen:
            wid = wh.get("id")
            coin = (wh.get("coin") or "").strip().upper()
            yon = (wh.get("direction") or "SHORT").strip().upper()
            payload = wh.get("payload") or {}
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except Exception:
                    payload = {}
            try:
                sonuc = self.handle_webhook(wid, coin, yon, payload)
            except Exception as e:
                log(f"Webhook #{wid} ({coin}) hata: {e}", "ERROR")
                sb_log_event("ERROR", coin, f"webhook #{wid}: {e}")
                sonuc = f"ERROR:{e}"
            sb_mark_webhook(wid, sonuc)

    def handle_webhook(self, wid, coin, yon, payload):
        """Tek webhook sinyalini degerlendir. Doner: sonuc metni."""
        log(f"Webhook #{wid}: {coin} {yon}")

        if yon not in ("SHORT", "LONG"):
            return "SKIPPED:gecersiz yon"

        symbol = self.ex.unified(coin)
        if not symbol:
            sb_log_event("SIGNAL_SKIP", coin, f"webhook #{wid}: coin binance futures'ta yok")
            return "SKIPPED:coin yok"

        if self.stopped():
            sb_log_event("SIGNAL_SKIP", coin, f"webhook #{wid}: kill-switch aktif")
            return "SKIPPED:kill-switch"

        live = self.ex.open_positions()
        if live is None:
            return "SKIPPED:pozisyonlar okunamadi"
        if symbol in live or symbol in self.open_trades:
            sb_log_event("SIGNAL_SKIP", coin, f"webhook #{wid}: coinde acik pozisyon var")
            return "SKIPPED:coinde acik pozisyon"

        _, rule_count = self.count_pools(live)
        if rule_count >= CFG["max_rule_positions"]:
            sb_log_event("SIGNAL_SKIP", coin,
                         f"webhook #{wid}: ozel havuz dolu ({rule_count}/{CFG['max_rule_positions']})")
            tg_send(f"<b>WEBHOOK ATLANDI</b> {coin}\nOzel havuz dolu "
                    f"({rule_count}/{CFG['max_rule_positions']})")
            return "SKIPPED:havuz dolu"

        # kisa sureli tekrar korumasi
        dedup = int(WH.get("dedup_sec") or 0)
        if dedup > 0 and coin in sb_recent_webhook_coins(dedup):
            sb_log_event("SIGNAL_SKIP", coin, f"webhook #{wid}: son {dedup} sn icinde islendi")
            return f"SKIPPED:tekrar ({dedup}sn)"

        # parametreler: payload > Ayarlar varsayilanlari
        margin = float(num(payload.get("margin_usdt"), WH["margin_usdt"]) or WH["margin_usdt"])
        lev = int(num(payload.get("leverage"), WH["leverage"]) or WH["leverage"])
        tp_type = (payload.get("tp_type") or WH["tp_type"]).lower()
        sl_type = (payload.get("sl_type") or WH["sl_type"]).lower()
        tp_val = num(payload.get("tp_value"), WH["tp_value"])
        sl_val = num(payload.get("sl_value"), WH["sl_value"])
        notional = margin * lev

        # kasa kontrolu (kural havuzuyla ayni mantik)
        balance = self.ex.free_usdt()
        if balance is None:
            return "SKIPPED:bakiye okunamadi"
        est_price = self.ex.last_price(symbol)
        new_risk = 150.0
        if est_price:
            slp = self._resolve_level(sl_type, sl_val, est_price, yon, is_tp=False)
            if slp:
                new_risk = abs(est_price - slp) / est_price * notional
        total_risk = self.sl_risk_total() + new_risk
        if balance - total_risk < CFG["rule_min_free"]:
            sb_log_event("SIGNAL_SKIP", coin,
                         f"webhook #{wid}: kasa yetersiz (bakiye={balance:.0f} risk={total_risk:.0f})")
            tg_send(f"<b>WEBHOOK ATLANDI</b> {coin}\nKasa yetersiz: "
                    f"bakiye ${balance:.0f}, toplam SL riski ${total_risk:.0f}")
            return "SKIPPED:kasa yetersiz"

        acilan = self.open_position(
            coin=coin, symbol=symbol, side=yon,
            margin=margin, leverage=lev,
            tp_price=tp_val if tp_type == "price" else None,
            sl_price=sl_val if sl_type == "price" else None,
            tp_pct=tp_val if tp_type == "pct" else None,
            sl_pct=sl_val if sl_type == "pct" else None,
            source="rule", rule_id=None,
            signal_id=None, signal_type=f"WEBHOOK_{wid}",
            dyn_tp=extract_dynamic(self.settings_row, "wh_dyn_tp"),
            dyn_sl=extract_dynamic(self.settings_row, "wh_dyn_sl"),
        )
        return "OPENED" if acilan else "ERROR:pozisyon acilamadi"

    # ------------------------------------------------------------------
    # ORTAK POZISYON ACMA
    # ------------------------------------------------------------------
    def open_position(self, coin, symbol, side, margin, leverage,
                      tp_price, sl_price, tp_pct, sl_pct,
                      source, rule_id, signal_id, signal_type,
                      dyn_tp=None, dyn_sl=None):
        self.ex.configure_symbol(symbol, leverage=leverage)
        notional = margin * leverage
        order_side = "sell" if side == "SHORT" else "buy"

        fill = self.ex.market_order(symbol, order_side, notional)
        if not fill:
            log(f"{coin} emir acilamadi", "ERROR")
            sb_log_event("ERROR", coin, f"{source}: emir acilamadi")
            return False

        entry = fill["price"]
        amount = fill["amount"]
        gercek_notional = entry * amount
        log(f"ACILDI {coin} {side} | giris {entry} | miktar {amount} | "
            f"${gercek_notional:.0f} | kaynak={source}")

        kirpma = fill.get("kirpma")
        if kirpma:
            mesaj = (f"borsa max miktar siniri nedeniyle pozisyon kucultuldu: "
                     f"${kirpma['istenen_usdt']:.0f} -> ${kirpma['uygulanan_usdt']:.0f}")
            sb_log_event("SIZE_CLIP", coin, mesaj)
            tg_send(f"<b>POZISYON KUCULTULDU</b> {coin}\n{mesaj}")

        # TP/SL mutlak seviyeleri
        tp = tp_price if tp_price else self._resolve_level("pct", tp_pct, entry, side, is_tp=True)
        sl = sl_price if sl_price else self._resolve_level("pct", sl_pct, entry, side, is_tp=False)

        # sanity: SHORT -> tp < entry < sl ; LONG -> sl < entry < tp
        sane = (tp and sl and
                ((side == "SHORT" and tp < entry < sl) or
                 (side == "LONG" and sl < entry < tp)))
        if not sane:
            log(f"{coin} TP/SL mantiksiz (giris={entry} tp={tp} sl={sl}) - POZISYON KAPATILIYOR", "ERROR")
            self.ex.close_market(symbol, side, amount)
            sb_log_event("ERROR", coin, f"tp/sl mantiksiz: giris={entry} tp={tp} sl={sl}")
            tg_send(f"<b>ACIL KAPATMA</b> {coin}\nTP/SL seviyeleri girise gore mantiksiz.")
            return False

        # AND modu: hard emir Binance'e gonderilmez, bot ikisini birlikte izler
        tp_and = bool(dyn_tp and (dyn_tp.get("mode") or "OR").upper() == "AND")
        sl_and = bool(dyn_sl and (dyn_sl.get("mode") or "OR").upper() == "AND")
        prot = self.ex.place_tp_sl(symbol, side, tp, sl,
                                   send_tp=not tp_and, send_sl=not sl_and)

        if not sl_and and not prot["sl_id"]:
            log(f"{coin} SL kurulamadi - pozisyon KORUMASIZ, kapatiliyor", "ERROR")
            self.ex.cancel_all(symbol)
            self.ex.close_market(symbol, side, amount)
            sb_log_event("ERROR", coin, "SL kurulamadi, acil kapatildi")
            tg_send(f"<b>ACIL KAPATMA</b> {coin}\nSL emri kurulamadi.")
            return False

        if sl_and:
            log(f"{coin} SL emri Binance'e GONDERILMEDI (dinamik SL AND modu) - "
                f"koruma bota bagli", "WARN")
            sb_log_event("SL_SOFT", coin, "dinamik SL AND modu: hard SL emri yok, koruma bota bagli")
            tg_send(f"<b>DIKKAT</b> {coin}\nDinamik SL AND modunda - Binance'te SL emri yok, "
                    f"koruma bot calistigi surece gecerli.")

        row = {
            "coin": coin, "symbol": symbol,
            "signal_id": signal_id, "signal_type": signal_type,
            "side": side, "source": source, "rule_id": rule_id,
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "entry_price": entry, "amount": amount,
            "margin_usdt": margin, "leverage": leverage,
            "tp_price": prot["tp_price"], "sl_price": prot["sl_price"],
            "tp_order_id": str(prot["tp_id"]) if prot["tp_id"] else None,
            "sl_order_id": str(prot["sl_id"]) if prot["sl_id"] else None,
            "dyn_tp": dyn_tp, "dyn_sl": dyn_sl,   # acilista donduruldu
            "testnet": TESTNET,
        }
        saved = sb_insert_trade(row)
        if saved:
            row["id"] = saved.get("id")
        self.open_trades[symbol] = row

        mode = "TESTNET" if TESTNET else "CANLI"
        sb_log_event("OPEN", coin, f"{side} {source} giris={entry} tp={prot['tp_price']} sl={prot['sl_price']}")
        tg_send(f"<b>{side} ACILDI</b> {coin} ({mode})\n"
                f"Kaynak: {source}{f' (kural {rule_id})' if rule_id else ''}\n"
                f"Giris: {entry}\nMiktar: {amount}\n"
                f"TP: {prot['tp_price']}\nSL: {prot['sl_price']}")
        return True


# ======================================================================
# MAIN
# ======================================================================

_bot = None


def _shutdown(signum, frame):
    log("Kapatma sinyali alindi, dongu bitiriliyor...")
    if _bot:
        _bot.running = False


def main():
    global _bot
    if not SUPABASE_ON:
        sys.stderr.write("HATA: SUPABASE_URL / SUPABASE_KEY tanimli degil.\n")
        sys.exit(1)
    _signal.signal(_signal.SIGINT, _shutdown)
    _signal.signal(_signal.SIGTERM, _shutdown)
    _bot = Bot()
    _bot.run()


if __name__ == "__main__":
    main()
