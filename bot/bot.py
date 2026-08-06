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
MARGIN_USDT     = _env_f("BOT_MARGIN_USDT", 100)
LEVERAGE        = _env_i("BOT_LEVERAGE", 10)
MARGIN_MODE     = _env("BOT_MARGIN_MODE", "cross")
TP_PCT          = _env_f("BOT_TP_PCT", 10.0)
SL_PCT          = _env_f("BOT_SL_PCT", 15.0)
MAX_POSITIONS   = _env_i("BOT_MAX_POSITIONS", 4)        # sinyal havuzu
MIN_BALANCE     = _env_f("BOT_MIN_BALANCE", 900)
DEDUP_DAYS      = _env_f("BOT_DEDUP_DAYS", 2)
POLL_SECONDS    = _env_i("BOT_POLL_SECONDS", 20)
SIGNAL_TYPES    = [s.strip() for s in _env("BOT_SIGNAL_TYPES", "PUMP_1H,PUMP_15M").split(",") if s.strip()]
STRENGTH        = _env("BOT_STRENGTH", "strong")

# --- Ozel kural havuzu ---
MAX_RULE_POS    = _env_i("BOT_MAX_RULE_POSITIONS", 10)
RULE_MIN_FREE   = _env_f("BOT_RULE_MIN_FREE", 100)      # SL riskleri sonrasi min bakiye
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

POSITION_USDT   = MARGIN_USDT * LEVERAGE

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
    types = ",".join(SIGNAL_TYPES)
    path = (f"screener_signals?id=gt.{last_id}"
            f"&signal_type=in.({types})"
            f"&strength=eq.{STRENGTH}"
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
        lev = leverage or LEVERAGE
        mm = margin_mode or MARGIN_MODE
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
        min_cost = self.min_notional(symbol)
        if min_cost and amount * price < min_cost:
            log(f"{symbol} minNotional altinda ({amount*price:.2f} < {min_cost}) - atlandi", "WARN")
            return None
        order = self.ex.create_order(symbol, "market", side, amount)
        return self._fill_info(symbol, order, amount)

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

    def place_tp_sl(self, symbol, pos_side, tp_price, sl_price):
        """pos_side: 'SHORT' | 'LONG'. Fiyatlar mutlak.
        closePosition=true -> pozisyon kapaninca karsi emir otomatik iptal."""
        close_side = "buy" if pos_side == "SHORT" else "sell"
        tp = float(self.ex.price_to_precision(symbol, tp_price))
        sl = float(self.ex.price_to_precision(symbol, sl_price))
        tp_id = sl_id = None
        try:
            o = self.ex.create_order(symbol, "TAKE_PROFIT_MARKET", close_side, None, None,
                                     {"stopPrice": tp, "closePosition": True})
            tp_id = o.get("id")
        except Exception as e:
            log(f"{symbol} TP emri BASARISIZ: {e}", "ERROR")
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

    def cancel_all(self, symbol):
        try:
            self.ex.cancel_all_orders(symbol)
        except Exception as e:
            log(f"{symbol} emir iptali: {e}", "WARN")


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
            ok = compare(chg, op, p2)
            note = (f"oi son {int(p1 or 3)} bar ort. gore="
                    f"{chg if chg is None else round(chg,2)}% {op} {p2}% -> {ok}")
        elif t == "volume":
            chg = ctx.get("vol_change_pct")
            ok = compare(chg, op, p2)
            note = (f"hacim son {int(p1 or 3)} bar ort. gore="
                    f"{chg if chg is None else round(chg,2)}% {op} {p2}% -> {ok}")
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
        self.rule_last_bar = {}      # rule_id -> son degerlendirilen bar id
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
        mode = "TESTNET" if TESTNET else "CANLI"
        log(f"STS EXECUTOR {VERSION} basladi | mod={mode} | "
            f"sinyal: ${MARGIN_USDT} x{LEVERAGE} {MARGIN_MODE} TP-{TP_PCT}% SL+{SL_PCT}% max={MAX_POSITIONS} | "
            f"kural havuzu max={MAX_RULE_POS}")
        tg_send(f"<b>STS basladi</b> ({mode})\n"
                f"Sinyal: ${MARGIN_USDT} x{LEVERAGE} | TP -{TP_PCT}% SL +{SL_PCT}% | max {MAX_POSITIONS}\n"
                f"Kural havuzu: max {MAX_RULE_POS}")
        self.init_last_id()

        while self.running:
            try:
                self.refresh_control()
                live = self.check_closed_positions()
                self.write_status(live)
                self.refresh_rules()
                if self.stopped():
                    log("KILL-SWITCH aktif - yeni pozisyon acilmiyor", "WARN")
                else:
                    self.process_signals()
                    self.process_rules()
            except Exception as e:
                log(f"Dongu hatasi: {e}", "ERROR")
                sb_log_event("ERROR", None, f"dongu: {e}")
            for _ in range(POLL_SECONDS):
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
            positions.append({
                "symbol": sym,
                "coin": t.get("coin") or sym.split("/")[0],
                "side": (p.get("side") or t.get("side") or "").upper(),
                "source": t.get("source") or "signal",
                "contracts": p.get("contracts"),
                "entry": p.get("entryPrice"),
                "mark": p.get("markPrice"),
                "upnl": p.get("unrealizedPnl"),
                "tp": t.get("tp_price"),
                "sl": t.get("sl_price"),
                "leverage": t.get("leverage"),
                "margin": t.get("margin_usdt"),
            })
        sb_upsert_status({
            "version": VERSION,
            "testnet": TESTNET,
            "killswitch": self.stopped(),
            "balance": balance,
            "sig_count": sig_count, "sig_max": MAX_POSITIONS,
            "rule_count": rule_count, "rule_max": MAX_RULE_POS,
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
        if sig_count >= MAX_POSITIONS:
            log(f"{coin} atlandi - sinyal havuzu dolu ({MAX_POSITIONS})", "WARN")
            sb_log_event("SIGNAL_SKIP", coin, f"sinyal havuzu dolu ({sig_count}/{MAX_POSITIONS})")
            tg_send(f"<b>ATLANDI</b> {coin} ({stype})\nSebep: sinyal havuzu dolu")
            return

        balance = self.ex.free_usdt()
        if balance is None:
            sb_log_event("SIGNAL_SKIP", coin, "bakiye okunamadi")
            return
        if balance < MIN_BALANCE:
            log(f"{coin} atlandi - bakiye ${balance:.2f} < ${MIN_BALANCE}", "WARN")
            sb_log_event("SIGNAL_SKIP", coin, f"bakiye yetersiz ({balance:.0f}/{MIN_BALANCE})")
            tg_send(f"<b>ATLANDI</b> {coin} ({stype})\nSebep: bakiye ${balance:.2f}")
            return

        recent = sb_recent_trade_coins(DEDUP_DAYS)
        if coin in recent:
            log(f"{coin} atlandi - son {DEDUP_DAYS} gun icinde islem gordu")
            sb_log_event("SIGNAL_SKIP", coin, f"dedup ({DEDUP_DAYS} gun)")
            return

        self.open_position(
            coin=coin, symbol=symbol, side="SHORT",
            margin=MARGIN_USDT, leverage=LEVERAGE,
            tp_price=None, sl_price=None,
            tp_pct=TP_PCT, sl_pct=SL_PCT,
            source="signal", rule_id=None,
            signal_id=sig.get("id"), signal_type=stype,
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
        cond_types = {(c.get("type") or "").lower() for c in conds}

        # --- veri topla (lazy) ---
        ctx = {}
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
                return
            closed = ohlcv[:-1]  # son eleman canli mum
            ctx["closes"] = [c[4] for c in closed]
            ctx["last_close"] = closed[-1][4]
            vols = [c[5] for c in closed]
            for c in conds:
                if (c.get("type") or "").lower() == "volume":
                    n = int(num(c.get("p1"), 3) or 3)
                    if len(vols) > n:
                        prev = vols[-(n + 1):-1]          # son bar haric N bar
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
                        vals = [h.get("openInterestValue") or h.get("openInterestAmount") for h in hist]
                        vals = [float(v) for v in vals if v is not None]
                        if len(vals) >= n + 1:
                            prev = vals[-(n + 1):-1]      # son bar haric N bar
                            avg = sum(prev) / len(prev)
                            if avg > 0:
                                ctx["oi_change_pct"] = (vals[-1] - avg) / avg * 100.0
                    break

        if "funding" in cond_types:
            ctx["funding_pct"] = self.ex.funding_rate(symbol)

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
        if rule_count >= MAX_RULE_POS:
            log(f"Kural {rid}: ozel havuz dolu ({MAX_RULE_POS})", "WARN")
            sb_log_event("SIGNAL_SKIP", coin, f"kural {rid}: ozel havuz dolu")
            tg_send(f"<b>KURAL ATLANDI</b> {coin}\nOzel havuz dolu ({rule_count}/{MAX_RULE_POS})")
            return

        margin = float(num(rule.get("margin_usdt"), 100) or 100)
        lev = int(num(rule.get("leverage"), LEVERAGE) or LEVERAGE)
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
        if balance - total_risk < RULE_MIN_FREE:
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
    # ORTAK POZISYON ACMA
    # ------------------------------------------------------------------
    def open_position(self, coin, symbol, side, margin, leverage,
                      tp_price, sl_price, tp_pct, sl_pct,
                      source, rule_id, signal_id, signal_type):
        self.ex.configure_symbol(symbol, leverage=leverage)
        notional = margin * leverage
        order_side = "sell" if side == "SHORT" else "buy"

        fill = self.ex.market_order(symbol, order_side, notional)
        if not fill:
            log(f"{coin} emir acilamadi", "ERROR")
            sb_log_event("ERROR", coin, f"{source}: emir acilamadi")
            return

        entry = fill["price"]
        amount = fill["amount"]
        log(f"ACILDI {coin} {side} | giris {entry} | miktar {amount} | ${notional} | kaynak={source}")

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
            return

        prot = self.ex.place_tp_sl(symbol, side, tp, sl)

        if not prot["sl_id"]:
            log(f"{coin} SL kurulamadi - pozisyon KORUMASIZ, kapatiliyor", "ERROR")
            self.ex.cancel_all(symbol)
            self.ex.close_market(symbol, side, amount)
            sb_log_event("ERROR", coin, "SL kurulamadi, acil kapatildi")
            tg_send(f"<b>ACIL KAPATMA</b> {coin}\nSL emri kurulamadi.")
            return

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
