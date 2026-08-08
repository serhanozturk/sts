#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
STS PANEL v1
============
Salt-okuma web paneli + kill-switch.

- Executor'in Supabase'e yazdigi sts_status snapshot'ini okur (Binance key GORMEZ)
- bot_trades / sts_events / sts_rules tablolarini listeler
- Kill-switch: bot_stop.flag dosyasini olusturur/siler (executor ile ayni dizin)

Varsayilan 127.0.0.1:8080 - disariya ACIK DEGIL.
Erisim: SSH tuneli -> ssh -L 8080:localhost:8080 root@SUNUCU_IP
Cloudflare Tunnel kurulunca kalici erisim eklenecek (Asama 4).
"""

import os
import sys
import json
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

VERSION = "v3.0"
# ---------------------------------------------------------------------------
# SURUM GECMISI (her kod degisikliginde artir)
#   v3.0  yeni tasarim (borsa temasi, DM Sans + JetBrains Mono, iki tema),
#         3 seviyeli kill-switch arayuzu, webhook kapatma + mesaj olusturucu,
#         degme kosullari, olay/durum rozetlerine renk, periyot 1D/1W buyuk
#         harf, "koruma bot tarafinda" uyarisi, fiyat ondalik formati,
#         savePos yalnizca degisen alani gonderir
#   v2    Ayarlar sekmesi, JSON ile kural ekleme, dinamik TP/SL bloklari,
#         acik pozisyon yonetimi (Yonet), webhook karti, yenile butonu
#   v1    5 sekme, kural CRUD, kill-switch, gece/gunduz temasi
# ---------------------------------------------------------------------------

# ======================================================================
# .env
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

SUPABASE_URL = (os.environ.get("SUPABASE_URL", "") or "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
PANEL_BIND   = os.environ.get("PANEL_BIND", "127.0.0.1")
PANEL_PORT   = int(os.environ.get("PANEL_PORT", "8080"))
WEBHOOK_TOKEN = os.environ.get("WEBHOOK_TOKEN", "")
PANEL_USER   = os.environ.get("PANEL_USER", "")
PANEL_PASS   = os.environ.get("PANEL_PASS", "")
AUTH_ON      = bool(PANEL_USER and PANEL_PASS)


def log(msg, level="INFO"):
    ts = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {level:5s} {msg}", flush=True)


# ======================================================================
# SUPABASE (salt okuma)
# ======================================================================

def sb_get(path):
    if not (SUPABASE_URL and SUPABASE_KEY):
        return None
    url = f"{SUPABASE_URL}/rest/v1/{path.lstrip('/')}"
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else []
    except Exception as e:
        log(f"Supabase GET {path}: {e}", "ERROR")
        return None


def sb_patch(path, body):
    if not (SUPABASE_URL and SUPABASE_KEY):
        return False
    url = f"{SUPABASE_URL}/rest/v1/{path.lstrip('/')}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="PATCH", headers=headers)
    try:
        urllib.request.urlopen(req, timeout=10).read()
        return True
    except Exception as e:
        log(f"Supabase PATCH {path}: {e}", "ERROR")
        return False


LEVELS = ("RUN", "PAUSE", "STOP")


def set_level(level, note=None):
    """Kill-switch seviyesi. killswitch alani geriye uyum icin birlikte guncellenir."""
    level = (level or "").upper()
    if level not in LEVELS:
        return False
    return sb_patch("sts_control?id=eq.1", {
        "level": level,
        "killswitch": level != "RUN",
        "level_at": datetime.now(timezone.utc).isoformat(),
        "level_note": (note or "")[:200] or None,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })


def request_emergency():
    """Acil cikis: executor tum pozisyonlari kapatir, sonra STOP'a geker."""
    return sb_patch("sts_control?id=eq.1", {
        "req_emergency": True,
        "level_at": datetime.now(timezone.utc).isoformat(),
        "level_note": "acil cikis istendi",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })


def set_killswitch(value):
    """Geriye uyum: eski /api/stop, /api/resume uclari icin."""
    return set_level("PAUSE" if value else "RUN",
                     "eski uc uzerinden" if value else None)


def sb_post(path, body):
    if not (SUPABASE_URL and SUPABASE_KEY):
        return None
    url = f"{SUPABASE_URL}/rest/v1/{path.lstrip('/')}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else []
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")[:300]
        except Exception:
            pass
        log(f"Supabase POST {path}: HTTP {e.code} {detail}", "ERROR")
        return None
    except Exception as e:
        log(f"Supabase POST {path}: {e}", "ERROR")
        return None


def sb_delete(path):
    if not (SUPABASE_URL and SUPABASE_KEY):
        return False
    url = f"{SUPABASE_URL}/rest/v1/{path.lstrip('/')}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Prefer": "return=minimal",
    }
    req = urllib.request.Request(url, method="DELETE", headers=headers)
    try:
        urllib.request.urlopen(req, timeout=10).read()
        return True
    except Exception as e:
        log(f"Supabase DELETE {path}: {e}", "ERROR")
        return False


# ======================================================================
# KURAL DOGRULAMA
# ======================================================================

COND_TYPES = {"ema_cross", "rsi", "price", "oi_change", "volume", "funding",
              "touch_price", "touch_ema"}
OPS = {"<", ">", "<=", ">=", "="}
# Periyot: panel "1D" gonderir, eski kayitlarda "1d" olabilir -> kucuk harfle karsilastir
TIMEFRAMES = {"5m", "15m", "30m", "1h", "4h", "1d", "1w"}


def _tf_normal(tf):
    """Periyodu dogrula ve GORUNTU bicimine getir: dakika/saat kucuk, gun/hafta buyuk."""
    t = str(tf or "").strip().lower()
    if t not in TIMEFRAMES:
        return None
    return t.upper() if t in ("1d", "1w") else t
NEEDS_P1 = {"ema_cross", "oi_change", "volume"}   # rsi periyodu sabit 14


def _num(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace("%", "").replace("x", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def validate_conditions(raw_conds, etiket=""):
    """Kosul listesini dogrula. Doner: (temiz_liste, hata_listesi).
    Giris kurallari ve dinamik cikis ayni dogrulamayi kullanir."""
    err = []
    on = (etiket + " ") if etiket else ""

    if isinstance(raw_conds, str):
        try:
            raw_conds = json.loads(raw_conds)
        except Exception:
            return [], [on + "kosullar okunamadi"]
    if not isinstance(raw_conds, list) or not raw_conds:
        return [], [on + "en az bir kosul gerekli"]
    if len(raw_conds) > 8:
        err.append(on + "en fazla 8 kosul")

    conds = []
    for i, c in enumerate(raw_conds, 1):
        if not isinstance(c, dict):
            err.append(f"{on}kosul {i}: gecersiz")
            continue
        t = (c.get("type") or "").lower()
        op = (c.get("op") or "").strip()
        p1 = _num(c.get("p1"))
        p2 = _num(c.get("p2"))
        if t not in COND_TYPES:
            err.append(f"{on}kosul {i}: tip gecersiz")
            continue
        if op not in OPS:
            err.append(f"{on}kosul {i}: operator gecersiz")
            continue
        if p2 is None:
            err.append(f"{on}kosul {i}: deger bos")
            continue
        if t in NEEDS_P1:
            if p1 is None or p1 <= 0:
                err.append(f"{on}kosul {i}: periyot/bar sayisi pozitif olmali")
                continue
            if p1 > 500:
                err.append(f"{on}kosul {i}: periyot 500'den kucuk olmali")
                continue
        if t == "touch_ema" and (p2 <= 0 or p2 > 500):
            err.append(f"{on}kosul {i}: ema periyodu 1-500 arasi olmali")
            continue
        if t == "touch_price" and p2 <= 0:
            err.append(f"{on}kosul {i}: fiyat pozitif olmali")
            continue
        if t == "ema_cross" and (p2 <= 0 or p2 > 500):
            err.append(f"{on}kosul {i}: yavas periyot 1-500 arasi olmali")
            continue
        if t == "rsi" and not (0 <= p2 <= 100):
            err.append(f"{on}kosul {i}: rsi esigi 0-100 arasi olmali")
            continue
        # oi_change / volume: yon operatorde, deger pozitif saklanir
        if t in ("oi_change", "volume"):
            p2 = abs(p2)
            if p2 == 0:
                err.append(f"{on}kosul {i}: yuzde degeri 0 olamaz")
                continue
        conds.append({"type": t, "op": op,
                      "p1": p1 if t in NEEDS_P1 else None, "p2": p2})

    return conds, err


def validate_dynamic(d, prefix, etiket):
    """Dinamik TP/SL blogunu dogrula. Doner: (alan_dict, hata_listesi).
    Pasifse sadece active=false doner."""
    aktif = d.get(f"{prefix}_active")
    aktif = str(aktif).strip().lower() in ("1", "true", "yes", "on") if not isinstance(aktif, bool) else aktif
    if not aktif:
        return {f"{prefix}_active": False}, []

    err = []
    out = {f"{prefix}_active": True}

    tf = _tf_normal(d.get(f"{prefix}_timeframe") or "5m")
    if tf is None:
        err.append(f"{etiket}: periyot gecersiz")
    else:
        out[f"{prefix}_timeframe"] = tf

    logic = (d.get(f"{prefix}_logic") or "AND").upper()
    if logic == "-":
        logic = "AND"
    if logic not in ("AND", "OR"):
        err.append(f"{etiket}: mantik AND veya OR olmali")
    else:
        out[f"{prefix}_logic"] = logic

    mode = (d.get(f"{prefix}_mode") or "OR").upper()
    if mode not in ("AND", "OR"):
        err.append(f"{etiket}: hard/dinamik iliskisi AND veya OR olmali")
    else:
        out[f"{prefix}_mode"] = mode

    conds, cerr = validate_conditions(d.get(f"{prefix}_conditions"), etiket)
    if cerr:
        err += cerr
    else:
        out[f"{prefix}_conditions"] = conds

    if err:
        return None, err
    return out, []


def validate_rule(d):
    """Panelden gelen kurali dogrula. Doner: (temiz_dict | None, hata_listesi)."""
    err = []

    coin = (d.get("coin") or "").strip().upper()
    if not coin or len(coin) > 20:
        err.append("coin gecersiz")

    direction = (d.get("direction") or "SHORT").upper()
    if direction not in ("SHORT", "LONG"):
        err.append("yon SHORT veya LONG olmali")

    tf = _tf_normal(d.get("timeframe") or "5m")
    if tf is None:
        err.append("periyot gecersiz (izinli: 5m, 15m, 30m, 1h, 4h, 1D, 1W)")
        tf = "5m"

    logic = (d.get("logic") or "AND").upper()
    if logic == "-":
        logic = "AND"   # tek kural: mantik operatoru anlamsiz, AND ile ayni
    if logic not in ("AND", "OR"):
        err.append("mantik AND veya OR olmali")

    conds, cerr = validate_conditions(d.get("conditions"))
    err += cerr

    tp_type = (d.get("tp_type") or "pct").lower()
    sl_type = (d.get("sl_type") or "pct").lower()
    if tp_type not in ("pct", "price"):
        err.append("tp tipi pct veya price olmali")
    if sl_type not in ("pct", "price"):
        err.append("sl tipi pct veya price olmali")

    tp_value = _num(d.get("tp_value"))
    sl_value = _num(d.get("sl_value"))
    if tp_value is None or tp_value <= 0:
        err.append("tp degeri pozitif olmali")
    if sl_value is None or sl_value <= 0:
        err.append("sl degeri pozitif olmali")

    margin = _num(d.get("margin_usdt")) or 100.0
    if margin <= 0 or margin > 10000:
        err.append("teminat 0-10000 arasi olmali")
    lev = int(_num(d.get("leverage")) or 10)
    if lev < 1 or lev > 125:
        err.append("kaldirac 1-125 arasi olmali")

    # pct modunda mantik kontrolu (price modunda giris fiyati bilinmiyor,
    # executor acilista tekrar dogruluyor)
    if not err and tp_type == "pct" and tp_value >= 100:
        err.append("tp yuzdesi 100'den kucuk olmali")

    days = _num(d.get("expire_days"))
    expires_at = None
    if days and days > 0:
        expires_at = (datetime.now(timezone.utc) + timedelta(days=min(days, 365))).isoformat()

    # dinamik cikis bloklari (opsiyonel)
    dyn = {}
    for prefix, etiket in (("dyn_tp", "Dinamik TP"), ("dyn_sl", "Dinamik SL")):
        blok, derr = validate_dynamic(d, prefix, etiket)
        if derr:
            err += derr
        elif blok:
            dyn.update(blok)

    if err:
        return None, err

    return {
        "coin": coin, "direction": direction, "timeframe": tf,
        "conditions": conds, "logic": logic,
        **dyn,
        "tp_type": tp_type, "tp_value": tp_value,
        "sl_type": sl_type, "sl_value": sl_value,
        "margin_usdt": margin, "leverage": lev,
        "expires_at": expires_at,
        "active": bool(d.get("active", True)),
        "note": (d.get("note") or "")[:200] or None,
    }, []


def gather_state():
    status_rows = sb_get("sts_status?id=eq.1&limit=1")
    status, status_age = None, None
    if status_rows:
        row = status_rows[0]
        status = row.get("payload")
        try:
            upd = datetime.fromisoformat(row.get("updated_at", "").replace("Z", "+00:00"))
            status_age = (datetime.now(timezone.utc) - upd).total_seconds()
        except Exception:
            pass

    ctrl = sb_get("sts_control?id=eq.1&limit=1")
    c0 = ctrl[0] if ctrl else {}
    killswitch = bool(c0.get("killswitch"))
    level = (c0.get("level") or ("PAUSE" if killswitch else "RUN")).upper()
    if level not in LEVELS:
        level = "RUN"

    stg = sb_get("sts_settings?id=eq.1&limit=1")
    settings = stg[0] if stg else None

    webhooks = sb_get("sts_webhooks?order=id.desc&limit=30") or []

    trades = sb_get("bot_trades?order=id.desc&limit=100") or []
    events = sb_get("sts_events?order=id.desc&limit=100") or []
    rules  = sb_get("sts_rules?order=id.desc&limit=50") or []

    return {
        "status": status,
        "status_age": round(status_age) if status_age is not None else None,
        "killswitch": killswitch,
        "level": level,
        "emergency_pending": bool(c0.get("req_emergency")),
        "level_at": c0.get("level_at"),
        "settings": settings,
        "webhooks": webhooks,
        "webhook_enabled": bool(WEBHOOK_TOKEN),
        "panel_version": VERSION,
        "webhook_token": WEBHOOK_TOKEN,   # hazir mesaj uretmek icin (panel auth'lu)
        "trades": trades,
        "events": events,
        "rules": rules,
        "server_time": datetime.now(timezone.utc).isoformat(),
    }


# ======================================================================
# ACIK POZISYON YONETIMI
# ======================================================================

def validate_position_req(d):
    """Panelden gelen pozisyon guncelleme istegini dogrula.
    Doner: (alan_dict, hata_listesi)."""
    err = []
    out = {}

    if d.get("close"):
        out["req_close"] = True
        out["req_at"] = datetime.now(timezone.utc).isoformat()
        return out, []          # kapatma istegi tek basina yeter

    for anahtar, alan, ad in (("tp_price", "req_tp_price", "TP"),
                              ("sl_price", "req_sl_price", "SL")):
        if anahtar not in d or d.get(anahtar) in (None, ""):
            continue
        v = _num(d.get(anahtar))
        if v is None or v <= 0:
            err.append(f"{ad} fiyati pozitif olmali")
            continue
        out[alan] = v

    # dinamik cikis bloklari (dogrudan yazilir, executor okur)
    for prefix, etiket in (("dyn_tp", "Dinamik TP"), ("dyn_sl", "Dinamik SL")):
        if f"{prefix}_active" not in d:
            continue
        blok, derr = validate_dynamic(d, prefix, etiket)
        if derr:
            err += derr
            continue
        # bot_trades'te tek jsonb kolon olarak tutulur
        if blok.get(f"{prefix}_active"):
            out[prefix] = {
                "active": True,
                "timeframe": blok[f"{prefix}_timeframe"],
                "conditions": blok[f"{prefix}_conditions"],
                "logic": blok[f"{prefix}_logic"],
                "mode": blok[f"{prefix}_mode"],
            }
        else:
            out[prefix] = None

    if err:
        return None, err
    if not out:
        return None, ["degistirilecek alan yok"]
    out["req_at"] = datetime.now(timezone.utc).isoformat()
    return out, []


# ======================================================================
# WEBHOOK
# ======================================================================

WH_OPT_FIELDS = {
    "margin_usdt": (float, 1, 100000),
    "leverage":    (int, 1, 125),
    "tp_value":    (float, 0.00000001, 10000000),
    "sl_value":    (float, 0.00000001, 10000000),
}


def validate_webhook(d):
    """TradingView'den gelen payload'i dogrula.
    Doner: (temiz_dict | None, hata_listesi).
    Zorunlu: coin, direction. Digerleri opsiyonel (Ayarlar varsayilanlari kullanilir)."""
    err = []
    if not isinstance(d, dict):
        return None, ["payload nesne olmali"]

    coin = str(d.get("coin") or d.get("symbol") or "").strip().upper()
    coin = coin.replace("PERP", "").replace(".P", "").replace("/", "")
    if not coin or len(coin) > 20:
        err.append("coin gecersiz")

    eylem_ham = str(d.get("action") or "open").strip().lower()
    yon = str(d.get("direction") or d.get("side") or "").strip().upper()
    if yon in ("SELL", "SHORT"):
        yon = "SHORT"
    elif yon in ("BUY", "LONG"):
        yon = "LONG"
    elif eylem_ham == "close":
        yon = "SHORT"          # kapatmada yon kullanilmaz, kayit icin varsayilan
    else:
        err.append("direction SHORT/LONG (veya BUY/SELL) olmali")

    eylem = str(d.get("action") or "open").strip().lower()
    if eylem not in ("open", "close"):
        err.append("action open veya close olmali")

    clean = {"coin": coin, "direction": yon}
    if eylem == "close":
        clean["action"] = "close"

    # opsiyonel sayisal alanlar
    for key, (caster, lo, hi) in WH_OPT_FIELDS.items():
        if key not in d or d.get(key) in (None, ""):
            continue
        v = _num(d.get(key))
        if v is None:
            err.append(f"{key}: sayi olmali")
            continue
        if v < lo or v > hi:
            err.append(f"{key}: {lo} - {hi} arasi olmali")
            continue
        clean[key] = caster(v)

    for key in ("tp_type", "sl_type"):
        if key in d and d.get(key):
            t = str(d.get(key)).strip().lower()
            if t not in ("pct", "price"):
                err.append(f"{key} pct veya price olmali")
            else:
                clean[key] = t

    # tp/sl ciftleri: deger verildiyse tip de netlesmis olmali (varsayilan pct)
    for yan in ("tp", "sl"):
        if f"{yan}_value" in clean and f"{yan}_type" not in clean:
            clean[f"{yan}_type"] = "pct"

    if "note" in d and d.get("note"):
        clean["note"] = str(d["note"])[:200]

    if err:
        return None, err
    return clean, []


# ======================================================================
# AYAR DOGRULAMA
# ======================================================================

# alan: (tip, min, max)
SETTING_FIELDS = {
    "wh_margin_usdt":     (float, 1, 100000),
    "wh_leverage":        (int,   1, 125),
    "wh_tp_value":        (float, 0.00000001, 10000000),
    "wh_sl_value":        (float, 0.00000001, 10000000),
    "wh_dedup_sec":       (int,   0, 86400),
    "margin_usdt":        (float, 1, 100000),
    "leverage":           (int,   1, 125),
    "tp_pct":             (float, 0.1, 99),
    "sl_pct":             (float, 0.1, 500),
    "max_positions":      (int,   1, 50),
    "max_rule_positions": (int,   1, 50),
    "min_balance":        (float, 0, 1000000),
    "rule_min_free":      (float, 0, 1000000),
    "dedup_days":         (float, 0, 365),
    "poll_seconds":       (int,   5, 600),
}
SETTING_LABELS = {
    "wh_margin_usdt": "Webhook teminat", "wh_leverage": "Webhook kaldirac",
    "wh_tp_value": "Webhook TP", "wh_sl_value": "Webhook SL",
    "wh_dedup_sec": "Webhook tekrar korumasi",
    "margin_usdt": "Teminat", "leverage": "Kaldirac",
    "tp_pct": "Hard TP", "sl_pct": "Hard SL",
    "max_positions": "Max sinyal pozisyonu",
    "max_rule_positions": "Max kural pozisyonu",
    "min_balance": "Min bakiye", "rule_min_free": "Kural min serbest bakiye",
    "dedup_days": "Dedup suresi", "poll_seconds": "Tarama araligi",
}


def validate_settings(d):
    """Panelden gelen ayarlari dogrula. Doner: (temiz_dict | None, hata_listesi).
    NOT: testnet burada YOK - env'de kalir (guvenlik)."""
    err = []
    clean = {}

    for key, (caster, lo, hi) in SETTING_FIELDS.items():
        if key not in d:
            continue
        v = _num(d.get(key))
        ad = SETTING_LABELS.get(key, key)
        if v is None:
            err.append(f"{ad}: sayi olmali")
            continue
        if v < lo or v > hi:
            err.append(f"{ad}: {lo} - {hi} arasi olmali")
            continue
        clean[key] = caster(v)

    for key in ("wh_tp_type", "wh_sl_type"):
        if key in d:
            t = (d.get(key) or "").strip().lower()
            if t not in ("pct", "price"):
                err.append(f"{SETTING_LABELS.get(key, key)} pct veya price olmali")
            else:
                clean[key] = t

    if "margin_mode" in d:
        mm = (d.get("margin_mode") or "").strip().lower()
        if mm not in ("cross", "isolated"):
            err.append("Marj modu cross veya isolated olmali")
        else:
            clean["margin_mode"] = mm

    if "strength" in d:
        st = (d.get("strength") or "").strip()
        if not st or len(st) > 30:
            err.append("Strength degeri gecersiz")
        else:
            clean["strength"] = st

    if "signal_types" in d:
        raw = d.get("signal_types") or ""
        types = [s.strip().upper() for s in str(raw).split(",") if s.strip()]
        if not types:
            err.append("En az bir sinyal tipi gerekli")
        elif len(types) > 10:
            err.append("En fazla 10 sinyal tipi")
        elif any(len(t) > 30 for t in types):
            err.append("Sinyal tipi cok uzun")
        else:
            clean["signal_types"] = ",".join(types)

    # dinamik cikis bloklari (sinyal havuzu + webhook)
    for prefix, etiket in (("dyn_tp", "Dinamik TP"), ("dyn_sl", "Dinamik SL"),
                           ("wh_dyn_tp", "Webhook dinamik TP"),
                           ("wh_dyn_sl", "Webhook dinamik SL")):
        if f"{prefix}_active" not in d:
            continue
        blok, derr = validate_dynamic(d, prefix, etiket)
        if derr:
            err += derr
        elif blok:
            clean.update(blok)

    # tutarlilik: TP/SL mantigi
    if clean.get("tp_pct") is not None and clean["tp_pct"] >= 100:
        err.append("Hard TP %100'den kucuk olmali")

    if not clean and not err:
        err.append("Degistirilecek alan yok")

    if err:
        return None, err
    return clean, []


# ======================================================================
# HTML
# ======================================================================

HTML = """<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="theme-color" content="#ffffff">
<title>STS Panel</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Archivo:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
/* ============================================================
   TASARIM 2 — yuksek kontrastli, siyah/beyaz temelli borsa temasi
   Pill sekmeler, mint/kizil yon renkleri, sifira yakin golge.
   ============================================================ */

@import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;700&family=JetBrains+Mono:wght@400;500;700&display=swap');

/* --- 1. Degiskenler --- */
:root{
  --bg:#ffffff;
  --surface:#ffffff;
  --surface2:#f2f3f5;
  --border:#e3e5e9;
  --text:#000000;
  --text2:#5c6068;
  --text3:#93979f;

  --green:#00915f;   --greenBg:#e2f6ee;  --greenBd:#9fdcc4;
  --coral:#e5333f;   --coralBg:#fdeaeb;  --coralBd:#f5b0b5;
  --purple:#2a5cff;  --purpleBg:#e8eeff;
  --amber:#a86a00;   --amberBg:#fdf1da;
  --shadow:0 0 0 0 transparent;

  --mono:"JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  --sans:"DM Sans",-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  --r:4px;
}

[data-theme="dark"]{
  --bg:#000000;
  --surface:#0d0e11;
  --surface2:#17181c;
  --border:#23252b;
  --text:#ffffff;
  --text2:#9296a0;
  --text3:#5e626b;

  --green:#00d18f;   --greenBg:rgba(0,209,143,.14);  --greenBd:rgba(0,209,143,.34);
  --coral:#ff4d5e;   --coralBg:rgba(255,77,94,.14);  --coralBd:rgba(255,77,94,.34);
  --purple:#5b8cff;  --purpleBg:rgba(91,140,255,.16);
  --amber:#f0b03c;   --amberBg:rgba(240,176,60,.14);
  --shadow:0 0 0 0 transparent;
}

/* --- 2. Temel --- */
*,*::before,*::after{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{
  margin:0;background:var(--bg);color:var(--text);
  font:400 12px/1.4 var(--sans);letter-spacing:-.004em;
  -webkit-font-smoothing:antialiased;
}
h1,h2,h3,h4{margin:0;font-weight:700;letter-spacing:-.025em}
a{color:var(--purple);text-decoration:none}
a:hover{color:var(--purple);text-decoration:underline}
::selection{background:var(--purpleBg);color:var(--text)}

/* --- 3. Baslik --- */
.hdr{
  display:flex;align-items:center;justify-content:space-between;gap:12px;
  height:44px;padding:0 14px;
  background:var(--surface);border-bottom:1px solid var(--border);
  position:sticky;top:0;z-index:50;
}
.hdr-l,.hdr-r{display:flex;align-items:center;gap:7px;min-width:0}
/* seviye butonlarini yardimci ikonlardan AYIR - kazara Duraklat'a basilmasin */
#btn-pause{margin-left:22px;position:relative}
#btn-pause::before{content:"";position:absolute;left:-12px;top:4px;bottom:4px;
  width:1px;background:var(--border)}
.hdr-r{justify-content:flex-end}
.brand{
  font:800 14px/1 var(--sans);letter-spacing:-.03em;color:var(--text);
  white-space:nowrap;margin-right:6px;
}

.brand::before{
  content:"";flex:none;width:22px;height:22px;border-radius:6px;
  background-color:var(--text);
  background-image:
    linear-gradient(var(--green),var(--green)),
    linear-gradient(var(--green),var(--green)),
    linear-gradient(var(--coral),var(--coral));
  background-size:3px 6px,3px 10px,3px 14px;
  background-position:5px 12px,10px 8px,15px 4px;
  background-repeat:no-repeat;
}
.brand{display:inline-flex;align-items:center;gap:8px}

/* --- 4. Rozetler (pill) --- */
.badge{
  display:inline-flex;align-items:center;gap:5px;
  height:19px;padding:0 8px;border-radius:999px;
  font:600 10px/1 var(--sans);letter-spacing:.04em;text-transform:uppercase;
  background:var(--surface2);border:1px solid transparent;color:var(--text2);
  white-space:nowrap;vertical-align:middle;
}
.b-test{background:var(--amberBg);color:var(--amber)}
.b-live{background:var(--greenBg);color:var(--green)}
.b-live::before{content:"";width:5px;height:5px;border-radius:50%;background:var(--green)}
.b-ok{background:var(--greenBg);color:var(--green)}
.b-off{background:var(--surface2);color:var(--text3)}
.b-short{background:var(--coral);color:#fff}
.b-long{background:var(--green);color:#001a12}
[data-theme="dark"] .b-long{color:#00150e}
.b-sig{background:var(--purpleBg);color:var(--purple)}
.b-rule{background:var(--amberBg);color:var(--amber)}
.b-done{background:var(--purpleBg);color:var(--purple)}
.b-err{background:var(--coral);color:#fff}

/* --- 5. Butonlar --- */
.btn{
  display:inline-flex;align-items:center;justify-content:center;gap:6px;
  height:29px;padding:0 13px;border-radius:var(--r);
  border:1px solid var(--border);background:var(--surface2);color:var(--text);
  font:600 12px/1 var(--sans);cursor:pointer;
  transition:background .12s,color .12s,border-color .12s;
}
.btn:hover{background:var(--border)}
.btn:focus-visible,.icon-btn:focus-visible,.mini:focus-visible,
input:focus-visible,select:focus-visible,textarea:focus-visible{
  outline:2px solid var(--purple);outline-offset:1px;
}
.btn:disabled,.btn[disabled]{opacity:.4;cursor:not-allowed}
.btn-go{background:var(--green);border-color:var(--green);color:#00150e}
.btn-go:hover{filter:brightness(1.1);background:var(--green)}
.btn-stop{background:var(--coral);border-color:var(--coral);color:#fff}
.btn-stop:hover{filter:brightness(1.1);background:var(--coral)}

.icon-btn{
  display:inline-flex;align-items:center;justify-content:center;
  width:29px;height:29px;padding:0;border-radius:var(--r);
  border:1px solid transparent;background:transparent;color:var(--text2);
  font-size:13px;line-height:1;cursor:pointer;transition:background .12s,color .12s;
}
.icon-btn:hover{background:var(--surface2);color:var(--text)}

.mini{
  display:inline-flex;align-items:center;justify-content:center;gap:4px;
  height:20px;padding:0 8px;border-radius:999px;
  border:1px solid var(--border);background:transparent;color:var(--text2);
  font:600 10px/1 var(--sans);letter-spacing:.03em;text-transform:uppercase;cursor:pointer;
  transition:background .12s,color .12s,border-color .12s;
}
.mini:hover{background:var(--surface2);color:var(--text)}
.mini.del{color:var(--coral);border-color:var(--coralBd)}
.mini.del:hover{background:var(--coral);border-color:var(--coral);color:#fff}

.xbtn{
  display:inline-flex;align-items:center;justify-content:center;
  width:18px;height:18px;padding:0;border:0;border-radius:50%;
  background:transparent;color:var(--text3);font:400 14px/1 var(--sans);cursor:pointer;
}
.xbtn:hover{background:var(--coral);color:#fff}

/* --- 6. Sekmeler: pill grup --- */
.tabs{
  display:flex;align-items:center;gap:3px;
  padding:7px 12px;background:var(--surface);
  border-bottom:1px solid var(--border);
  overflow-x:auto;scrollbar-width:none;
}
.tabs::-webkit-scrollbar{display:none}
.tab{
  display:inline-flex;align-items:center;gap:6px;
  height:26px;padding:0 12px;border:0;border-radius:999px;
  background:transparent;color:var(--text2);
  font:600 12px/1 var(--sans);white-space:nowrap;cursor:pointer;
  transition:background .12s,color .12s;
}
.tab:hover{background:var(--surface2);color:var(--text)}
.tab.on{background:var(--text);color:var(--bg)}
.tab.on:hover{background:var(--text);color:var(--bg)}

/* --- 7. Metrikler --- */
.grid{
  display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
  gap:8px;
}
.met{
  display:flex;flex-direction:column;gap:4px;
  padding:10px 12px;background:var(--surface2);
  border:1px solid transparent;border-radius:var(--r);min-width:0;
}
.met .l{
  font:500 10.5px/1 var(--sans);letter-spacing:.02em;color:var(--text3);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}
.met .v{
  font:600 19px/1.1 var(--mono);letter-spacing:-.03em;color:var(--text);
  font-variant-numeric:tabular-nums;
}
.met .s{font:400 10.5px/1.2 var(--mono);color:var(--text2);font-variant-numeric:tabular-nums}
.met .v.up,.met .s.up{color:var(--green)}
.met .v.dn,.met .s.dn{color:var(--coral)}

/* --- 8. Pozisyon satiri --- */
.pos{
  display:flex;align-items:center;justify-content:space-between;gap:10px;
  padding:9px 12px;background:var(--surface);
  border:1px solid var(--border);border-radius:var(--r);
}
.pos + .pos{margin-top:-1px;border-radius:0}
.pos:hover{background:var(--surface2)}
.pos-l{display:flex;flex-direction:column;gap:3px;min-width:0}
.pos-nm{display:flex;align-items:center;gap:6px;font:700 13px/1.1 var(--sans);letter-spacing:-.02em}
.pos-dt{
  font:400 10.5px/1.2 var(--mono);color:var(--text3);
  font-variant-numeric:tabular-nums;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}
.pos-r{display:flex;flex-direction:column;align-items:flex-end;gap:2px;flex:none}
.pnl{font:600 15px/1.1 var(--mono);letter-spacing:-.02em;font-variant-numeric:tabular-nums;color:var(--text)}
.pnl-pct{font:500 11px/1.1 var(--mono);font-variant-numeric:tabular-nums;color:var(--text2)}
.pos .up,.pnl.up,.pnl-pct.up{color:var(--green)}
.pos .dn,.pnl.dn,.pnl-pct.dn{color:var(--coral)}

/* --- 9. Tablolar --- */
.tw{border:1px solid var(--border);border-radius:var(--r);background:var(--surface);overflow:auto}
.tw table{width:100%;border-collapse:collapse;font-size:11.5px}
.tw th{
  position:sticky;top:0;z-index:2;padding:8px 12px;text-align:left;white-space:nowrap;
  background:var(--surface);color:var(--text3);
  font:500 10.5px/1 var(--sans);letter-spacing:.02em;
  border-bottom:1px solid var(--border);
}
.tw td{
  padding:7px 12px;color:var(--text);white-space:nowrap;vertical-align:middle;
  border-bottom:1px solid var(--border);
}
.tw tbody tr:last-child td{border-bottom:0}
.tw tbody tr:nth-child(even) td{background:var(--surface2)}
.tw tbody tr:hover td{background:var(--purpleBg)}
.tw th:not(:first-child),.tw td:not(:first-child){text-align:right}
.tw th:first-child,.tw td:first-child{text-align:left;font-weight:600}
.tw td.mono,.tw td .mono{font-family:var(--mono);font-variant-numeric:tabular-nums}
.tw td.up,.tw td .up{color:var(--green)}
.tw td.dn,.tw td .dn{color:var(--coral)}
.tw td.mut,.tw td .mut{color:var(--text3)}
.empty{padding:30px 12px;text-align:center;color:var(--text3);font-size:11.5px;background:var(--surface)}

/* --- 10. Formlar --- */
label{
  display:block;margin-bottom:5px;
  font:500 10.5px/1 var(--sans);letter-spacing:.01em;color:var(--text3);
}
input,select,textarea{
  width:100%;height:30px;padding:0 9px;
  background:var(--surface2);color:var(--text);
  border:1px solid var(--border);border-radius:var(--r);
  font:500 12px/1 var(--mono);font-variant-numeric:tabular-nums;
  transition:border-color .12s,background .12s;
}
textarea{height:auto;min-height:66px;padding:8px 9px;line-height:1.5;resize:vertical}
select{
  appearance:none;padding-right:24px;cursor:pointer;
  background-image:linear-gradient(45deg,transparent 50%,currentColor 50%),
                   linear-gradient(135deg,currentColor 50%,transparent 50%);
  background-position:calc(100% - 13px) 13px,calc(100% - 9px) 13px;
  background-size:4px 4px,4px 4px;background-repeat:no-repeat;
}
input:hover,select:hover,textarea:hover{border-color:var(--border-strong,var(--text3))}
input:focus,select:focus,textarea:focus{border-color:var(--purple);background:var(--surface);outline:none}
input::placeholder,textarea::placeholder{color:var(--text3);font-family:var(--sans)}
input:disabled,select:disabled,textarea:disabled{opacity:.45;cursor:not-allowed}
input[type="checkbox"],input[type="radio"]{width:14px;height:14px;padding:0;accent-color:var(--purple);cursor:pointer;flex:none}

.frow{display:flex;flex-wrap:wrap;gap:9px;align-items:flex-end}
.frow > *{flex:1 1 150px;min-width:0}
.crow{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}
.crow.single{grid-template-columns:1fr}
.cunit{position:relative;display:flex;align-items:stretch;min-width:0}
.cunit input,.cunit select{padding-right:52px}
.cunit .u{
  position:absolute;right:1px;top:1px;bottom:1px;
  display:inline-flex;align-items:center;padding:0 9px;
  background:transparent;border-left:1px solid var(--border);
  border-radius:0 var(--r) var(--r) 0;
  font:600 10.5px/1 var(--mono);color:var(--text3);white-space:nowrap;pointer-events:none;
}
/* .divider panelde hem ayirici hem KAPSAYICI - height:1px icerigi tasirirdi */
.divider{border-top:1px solid var(--border);padding-top:12px;margin-top:12px}

/* --- 11. Kutular --- */
.errbox,.warnbox,.okbox{
  padding:9px 11px;border-radius:var(--r);border:0;border-left:2px solid;
  font-size:11.5px;line-height:1.5;
}
.errbox{background:var(--coralBg);border-color:var(--coral);color:var(--coral)}
.warnbox{background:var(--amberBg);border-color:var(--amber);color:var(--amber)}
.okbox{background:var(--greenBg);border-color:var(--green);color:var(--green)}

.stopband{
  display:flex;align-items:center;justify-content:space-between;gap:10px;
  padding:8px 12px;background:var(--coral);border-radius:var(--r);
  color:#fff;font:600 11.5px/1.3 var(--sans);
}
.stopband .mini,.stopband .mini.del{border-color:rgba(255,255,255,.5);color:#fff;background:transparent}
.stopband .mini:hover{background:rgba(255,255,255,.16);color:#fff}

.dynbox{background:var(--surface);border:1px solid var(--border);border-radius:var(--r);overflow:hidden}
.dynhead{
  display:flex;align-items:center;justify-content:space-between;gap:8px;
  padding:8px 12px;background:transparent;border-bottom:1px solid var(--border);
  font:700 11.5px/1 var(--sans);letter-spacing:-.01em;color:var(--text);
}
.dynhint{padding:8px 12px;font-size:10.5px;line-height:1.5;color:var(--text3);background:var(--surface2)}
.dynbox > .frow,.dynbox > .crow{padding:11px}

.chk{
  display:flex;align-items:center;gap:7px;
  padding:6px 10px;border:1px solid transparent;border-radius:999px;
  background:var(--surface2);cursor:pointer;font:500 11.5px/1 var(--sans);color:var(--text);
}
.chk:hover{background:var(--border)}
.chk label{margin:0;font:inherit;letter-spacing:0;color:inherit;cursor:pointer}

.pospanel{padding:11px;background:var(--surface2);border-radius:var(--r)}
.cprow{
  display:flex;align-items:center;justify-content:space-between;gap:8px;
  padding:5px 0;font-size:11.5px;color:var(--text2);
}
.cprow > :last-child{font-family:var(--mono);font-variant-numeric:tabular-nums;color:var(--text);font-weight:500}

/* --- 12. Yardimci --- */
.mono{font-family:var(--mono);font-variant-numeric:tabular-nums;letter-spacing:-.015em}
.up{color:var(--green)}
.dn{color:var(--coral)}
.mut{color:var(--text3)}
.sect{
  display:block;margin:16px 0 8px;
  font:700 13px/1 var(--sans);letter-spacing:-.025em;color:var(--text);
}
#toast{
  position:fixed;left:50%;bottom:20px;transform:translateX(-50%);z-index:200;
  max-width:min(92vw,420px);padding:10px 16px;
  background:var(--text);color:var(--bg);
  border:0;border-radius:999px;
  font:600 12px/1.4 var(--sans);text-align:center;
}
#toast.err{background:var(--coral);color:#fff}
#toast.ok{background:var(--green);color:#00150e}

*::-webkit-scrollbar{width:9px;height:9px}
*::-webkit-scrollbar-thumb{background:var(--border);border-radius:6px}
*::-webkit-scrollbar-thumb:hover{background:var(--text3)}
*::-webkit-scrollbar-track{background:transparent}

/* --- 13. Mobil --- */
@media (max-width:720px){
  .hdr{height:auto;flex-wrap:wrap;gap:6px;padding:8px 10px}
  .hdr-l,.hdr-r{flex:1 1 100%;flex-wrap:wrap}
  .hdr-r{justify-content:flex-start}

  .tabs{padding:6px 8px}
  .tab{height:30px}

  .grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:6px}
  .met{padding:9px 10px}
  .met .v{font-size:16px}

  .pos{flex-direction:column;align-items:stretch;gap:6px;border-radius:var(--r)}
  .pos + .pos{margin-top:6px;border-radius:var(--r)}
  .pos-r{flex-direction:row;gap:8px;align-items:baseline}

  .frow > *{flex:1 1 100%}
  .crow{grid-template-columns:1fr}

  .btn,.icon-btn{height:36px}
  .icon-btn{width:36px}
  .mini{height:26px;padding:0 11px}
  input,select{height:36px}

  .tw table{font-size:11px}
  .tw th,.tw td{padding:7px 9px}

  #toast{left:10px;right:10px;transform:none;max-width:none}
}

/* ============================================================
   UYUM KATMANI — mevcut panel yapisinin gerektirdigi eklemeler
   (tasarimin gorunumunu bozmaz, eksikleri tamamlar)
   ============================================================ */

/* Sekme panelleri: tasarimda govde padding'i yok, icerik kenara yapisirdi */
#p-durum,#p-islemler,#p-olaylar,#p-kurallar,#p-ayarlar{
  padding:14px;max-width:1280px;margin:0 auto;
}

/* .card — panelde yogun kullanilan kapsayici, tasarimda tanimli degildi */
.card{
  background:var(--surface);border:1px solid var(--border);
  border-radius:var(--r);padding:12px;margin-bottom:10px;
}
.card > .sect:first-child{margin-top:0}

/* Hata kutulari JS tarafindan display:block ile aciliyor -> varsayilan gizli */
.errbox{display:none}

/* .doner buton uzerine SINIF olarak ekleniyor (spinner elemani degil) */
.doner{animation:doner-spin .7s linear infinite}
@keyframes doner-spin{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion:reduce){.doner{animation-duration:2.4s}}

/* Kosul satiri: 4-5 kolonlu duzen (tip | p1 | operator | p2 | sil) */
.crow{grid-template-columns:150px 82px 62px 1fr 34px;align-items:center;gap:7px}
.crow.single{grid-template-columns:150px 62px 1fr 34px}
@media (max-width:720px){
  .crow,.crow.single{grid-template-columns:1fr 1fr;gap:6px}
  .crow > *:first-child{grid-column:1/-1}
  .crow .xbtn{grid-column:1/-1}
}

/* Kosul satirindaki birim etiketi p2 alanina yakin dursun */
.crow .cunit input{padding-right:30px}
.crow .cunit .u{border-left:0;padding:0 8px}

/* .u2 birim etiketi (JS ile doldurulur) */
.crow .u2{min-width:14px;text-align:right}

/* Dinamik blok govdesi: dogrudan cocuk olmayan frow'lar da nefes alsin */
.dynbox .frow,.dynbox .crow,.dynbox > div > .frow{padding-left:11px;padding-right:11px}
.dynbox .warnbox{margin:0 11px 11px}
.dynbox button{margin-left:11px;margin-bottom:11px}

/* Kopyala satiri: input + buton yan yana (tasarimda anahtar-deger satiriydi) */
.cprow{align-items:flex-start;padding:0;margin-top:4px}
.cprow input,.cprow textarea{flex:1 1 auto;font-family:var(--mono);font-size:11px}
.cprow .btn{flex:none;white-space:nowrap}
.cprow > :last-child{font-weight:600}

/* Pozisyon yonetim paneli acik pozisyon kartinin altinda tam genislik */
.pospanel{margin:-1px 0 8px;border:1px solid var(--border);border-top:0;
  border-radius:0 0 var(--r) var(--r);flex-basis:100%}

/* Xbtn kosul satirinda daha rahat tiklanabilir olsun */
.crow .xbtn{width:26px;height:26px;border:1px solid var(--coralBd);
  border-radius:var(--r);color:var(--coral);background:var(--coralBg)}
.crow .xbtn:hover{background:var(--coral);color:#fff}

/* ---- FORM DUZENI ----
   Tasarim .frow'u flex yapti; panelin formunda .frow bazen KAPSAYICI
   (icinde divider + baslik + baska satirlar) olarak kullaniliyor.
   Bu bloklarin satir olarak akmasi gerekiyor, yan yana dizilmemeli. */
.divider.frow{display:flex;flex-wrap:wrap}
.divider > .sect,.divider > p{flex-basis:100%;margin-top:0}

/* Kosullar bolumu: baslik satiri + kosul listesi alt alta */
#rule-form > .divider{display:block}
#rule-form > .divider > div:first-child{display:flex;align-items:center;
  justify-content:space-between;gap:10px;flex-wrap:wrap;margin-bottom:9px}

/* Dinamik cikis bolumu bloklari tam genislik */
.dynbox{flex-basis:100%;margin-bottom:10px}
.dynbox .frow{padding:0 11px}
.dynbox .frow:first-of-type{padding-top:11px}

/* Form icindeki bolum basliklari ustteki alandan ayrilsin */
#rule-form .sect{margin:0 0 4px}

/* Kaydet/Iptal satiri tam genislik, saga yatik */
#rule-form > div:last-child{flex-basis:100%}

/* JSON alani ve kayitli kurallar formun ALTINDA kalsin */
#p-kurallar > .card{margin-bottom:12px}

/* Kural tablosunda kosul metni tasmasin */
.tw td .mono{white-space:normal;word-break:break-word}

/* Olaylar/detay hucreleri uzun metinde sarsin */
.tw td[style*="white-space:normal"]{max-width:520px}

</style>
</head>
<body>

<div class="hdr">
  <div class="hdr-l">
    <span class="brand">STS</span>
    <span id="mode" class="badge b-test">—</span>
    <span id="health" class="badge b-off">Baglaniyor</span>
  </div>
  <div class="hdr-r">
    <span id="ver" class="badge b-off" title="Panel / Executor surumu">&mdash;</span>
    <span id="lvl-state" class="badge b-ok">&mdash;</span>
    <button class="btn icon-btn" onclick="elleYenile(this)" id="refresh-btn" title="Yenile">&#8635;</button>
    <button class="btn icon-btn" onclick="toggleTheme()" id="theme-btn" title="Tema">&#9789;</button>
    <button id="btn-pause" class="btn" onclick="seviyeAyarla('PAUSE')" title="Yeni pozisyon acmayi durdur, izlemeye devam et">Duraklat</button>
    <button id="btn-stop" class="btn btn-stop" onclick="seviyeAyarla('STOP')" title="Botu tamamen durdur">Bot dur</button>
    <button id="btn-emg" class="btn btn-stop" onclick="acilCikis()" title="Tum pozisyonlari kapat ve dur">Acil cikis</button>
  </div>
</div>

<div id="stop-banner" class="stopband" style="display:none">
  <b>BOT DURDURULDU</b> &mdash; Acik pozisyonlar IZLENMIYOR.
  Yumusak TP/SL, dinamik cikis, webhook ve kural motoru calismiyor.
  Hard TP/SL emirleri Binance'te duruyor (demo ortaminda guvenilmez).
</div>

<div class="tabs">
  <button class="tab on" onclick="show('durum',this)">Durum</button>
  <button class="tab" onclick="show('islemler',this)">Islemler</button>
  <button class="tab" onclick="show('olaylar',this)">Olaylar</button>
  <button class="tab" onclick="show('kurallar',this)">Kurallar</button>
  <button class="tab" onclick="show('ayarlar',this)">Ayarlar</button>
</div>

<div id="p-durum">
  <div class="grid">
    <div class="met"><div class="l">Bakiye</div><div class="v" id="m-bal">—</div><div class="s">USDT</div></div>
    <div class="met"><div class="l">Acik PnL</div><div class="v" id="m-upnl">—</div><div class="s" id="m-upnl-pct">Anlik</div></div>
    <div class="met"><div class="l">Sinyal havuzu</div><div class="v" id="m-sig">—</div><div class="s">Acik / max</div></div>
    <div class="met"><div class="l">Ozel havuz</div><div class="v" id="m-rule">—</div><div class="s">Acik / max</div></div>
  </div>
  <p class="sect">Acik pozisyonlar</p>
  <div id="positions"><div class="empty">Yukleniyor...</div></div>
</div>

<div id="p-islemler" style="display:none">
  <div class="grid">
    <div class="met"><div class="l">Kapanan islem</div><div class="v" id="t-count">—</div><div class="s">Toplam</div></div>
    <div class="met"><div class="l">Toplam PnL</div><div class="v" id="t-pnl">—</div><div class="s" id="t-pnl-pct">Teminata gore</div></div>
    <div class="met"><div class="l">Isabet orani</div><div class="v" id="t-win">—</div><div class="s" id="t-win-sub">Karli / toplam</div></div>
  </div>
  <p class="sect">Islem gecmisi</p>
  <div class="card"><div class="tw"><table id="trades">
    <thead><tr><th>Coin</th><th>Yon</th><th>Kaynak</th><th>Giris</th><th>Cikis</th><th>PnL</th><th>Sebep</th><th>Acilis</th></tr></thead>
    <tbody></tbody>
  </table></div></div>
</div>

<div id="p-olaylar" style="display:none">
  <p class="sect">Webhook kuyrugu</p>
  <div class="card"><div class="tw"><table id="webhooks">
    <thead><tr><th>#</th><th>Zaman</th><th>Coin</th><th>Yon</th><th>Durum</th><th>Sonuc</th></tr></thead>
    <tbody></tbody>
  </table></div></div>

  <p class="sect">Olay akisi</p>
  <div class="card"><div class="tw"><table id="events">
    <thead><tr><th>Zaman</th><th>Tip</th><th>Coin</th><th>Detay</th></tr></thead>
    <tbody></tbody>
  </table></div></div>
</div>

<div id="p-kurallar" style="display:none">
  <div class="card">
    <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:12px;flex-wrap:wrap">
      <p class="sect" style="margin:0" id="form-title">Yeni kural</p>
      <button class="btn btn-go" onclick="toggleForm()" id="form-toggle">+ Kural ekle</button>
    </div>

    <div id="rule-form" style="display:none">
      <div class="frow">
        <div><label>Coin</label><input id="f-coin" placeholder="HEI"></div>
        <div><label>Yon</label><select id="f-dir"><option value="SHORT">Short</option><option value="LONG">Long</option></select></div>
        <div><label>Periyot</label><select id="f-tf">
          <option value="5m">5m</option><option value="15m">15m</option><option value="30m">30m</option>
          <option value="1h">1h</option><option value="4h">4h</option><option value="1D">1D</option>
        </select></div>
      </div>

      <div class="divider">
        <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:9px;flex-wrap:wrap">
          <label style="margin:0">Kosullar</label>
          <div style="display:flex;align-items:center;gap:8px">
            <span style="font-size:11px;color:var(--text2)">Mantik</span>
            <select id="f-logic" style="width:110px;padding:6px 8px;font-size:12px">
              <option value="-">— (tek kural)</option>
              <option value="AND">Ve</option>
              <option value="OR">Veya</option>
            </select>
          </div>
        </div>
        <div id="conds"></div>
        <div style="display:flex;align-items:center;gap:10px;margin-top:8px;flex-wrap:wrap">
          <button class="btn" style="font-size:11px;padding:6px 12px" onclick="addCond('conds')">+ Kosul ekle</button>
          <span style="font-size:11px;color:var(--text3)">OI ve Hacim: son bar, onceki N barin ortalamasina gore; yonu operator belirler, eksi isareti gerekmez. &nbsp;|&nbsp; DEGDI kosullari: kapanan mumun yuksek-dusuk araligi hedefe dokunduysa tetiklenir (fiyat geri donse bile), operator kullanilmaz.</span>
        </div>
      </div>

      <div class="divider frow">
        <div><label>TP tipi</label><select id="f-tptype" onchange="syncLevelUnits()"><option value="pct">Yuzde</option><option value="price">Fiyat</option></select></div>
        <div><label>TP degeri</label><div class="cunit"><input id="f-tpval" placeholder="10"><span class="u" id="u-tp">%</span></div></div>
        <div><label>SL tipi</label><select id="f-sltype" onchange="syncLevelUnits()"><option value="pct">Yuzde</option><option value="price">Fiyat</option></select></div>
        <div><label>SL degeri</label><div class="cunit"><input id="f-slval" placeholder="15"><span class="u" id="u-sl">%</span></div></div>
      </div>

      <div class="frow">
        <div><label>Teminat</label><div class="cunit"><input id="f-margin" placeholder="100" style="padding-left:22px"><span class="u" style="left:10px;right:auto">$</span></div></div>
        <div><label>Kaldirac</label><div class="cunit"><input id="f-lev" placeholder="10"><span class="u">x</span></div></div>
        <div><label>Gecerlilik</label><div class="cunit"><input id="f-days" placeholder="3"><span class="u">gun</span></div></div>
        <div><label>Not</label><input id="f-note" placeholder="Opsiyonel"></div>
      </div>

      <div class="divider">
        <p class="sect" style="margin-bottom:2px">Dinamik cikis (opsiyonel)</p>
        <p style="font-size:11px;color:var(--text3);margin-bottom:10px">
          Hard TP/SL Binance'te emir olarak durur. Dinamik cikis ise kosul saglaninca
          botun pozisyonu kapatmasidir. Giris kurallariyla ayni kosul tipleri kullanilir.
        </p>
      <div class="dynbox">
        <div class="dynhead">
          <label class="chk"><input type="checkbox" id="dtp-active" onchange="dynToggle('dtp')"><span>Dinamik TP</span></label>
          <span class="dynhint">Kar tarafi cikis kosulu</span>
        </div>
        <div id="dtp-body" style="display:none">
          <div class="frow" style="margin-top:10px">
            <div><label>Periyot</label><select id="dtp-tf">
              <option value="5m">5m</option><option value="15m">15m</option><option value="30m">30m</option>
              <option value="1h">1h</option><option value="4h">4h</option><option value="1D">1D</option>
            </select></div>
            <div><label>Hard ile iliski</label><select id="dtp-mode" onchange="dynModeWarn('dtp')">
              <option value="OR">VEYA - hangisi once gelirse</option>
              <option value="AND">VE - ikisi birden gerekli</option>
            </select></div>
            <div><label>Kosul mantigi</label><select id="dtp-logic">
              <option value="-">&#8212; (tek kural)</option>
              <option value="AND">Ve</option>
              <option value="OR">Veya</option>
            </select></div>
          </div>
          <div id="dtp-conds"></div>
          <button class="btn" style="font-size:11px;padding:6px 12px;margin-top:6px" onclick="addCond('dtp-conds')">+ Kosul ekle</button>
          <div id="dtp-warn" class="warnbox" style="display:none">
            <b>VE modu uyarisi:</b> Bu modda hard seviye Binance'e emir olarak GONDERILMEZ,
            bot her iki kosulu birlikte izler. Bot durursa bu koruma da durur.
          </div>
        </div>
      </div>
      <div class="dynbox">
        <div class="dynhead">
          <label class="chk"><input type="checkbox" id="dsl-active" onchange="dynToggle('dsl')"><span>Dinamik SL</span></label>
          <span class="dynhint">Zarar tarafi cikis kosulu</span>
        </div>
        <div id="dsl-body" style="display:none">
          <div class="frow" style="margin-top:10px">
            <div><label>Periyot</label><select id="dsl-tf">
              <option value="5m">5m</option><option value="15m">15m</option><option value="30m">30m</option>
              <option value="1h">1h</option><option value="4h">4h</option><option value="1D">1D</option>
            </select></div>
            <div><label>Hard ile iliski</label><select id="dsl-mode" onchange="dynModeWarn('dsl')">
              <option value="OR">VEYA - hangisi once gelirse</option>
              <option value="AND">VE - ikisi birden gerekli</option>
            </select></div>
            <div><label>Kosul mantigi</label><select id="dsl-logic">
              <option value="-">&#8212; (tek kural)</option>
              <option value="AND">Ve</option>
              <option value="OR">Veya</option>
            </select></div>
          </div>
          <div id="dsl-conds"></div>
          <button class="btn" style="font-size:11px;padding:6px 12px;margin-top:6px" onclick="addCond('dsl-conds')">+ Kosul ekle</button>
          <div id="dsl-warn" class="warnbox" style="display:none">
            <b>VE modu uyarisi:</b> Bu modda hard seviye Binance'e emir olarak GONDERILMEZ,
            bot her iki kosulu birlikte izler. Bot durursa bu koruma da durur.
          </div>
        </div>
      </div>
      </div>

      <div id="f-errors" class="errbox"></div>

      <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:14px">
        <button class="btn" onclick="closeForm()">Iptal</button>
        <button class="btn btn-go" onclick="saveRule()" id="f-save">Kaydet</button>
      </div>
    </div>
  </div>

  <div class="card">
    <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:10px;flex-wrap:wrap">
      <p class="sect" style="margin:0">JSON ile kural ekle</p>
      <button class="btn" onclick="toggleJson()" id="json-toggle">Ac</button>
    </div>
    <div id="json-box" style="display:none">
      <p style="font-size:11px;color:var(--text3);margin-bottom:8px">
        Tek kural veya kural listesi yapistirabilirsin. Form ile ayni dogrulamadan gecer.
      </p>
      <textarea id="f-json" rows="9" placeholder='{"coin":"HEI","direction":"SHORT","timeframe":"5m","conditions":[{"type":"ema_cross","op":"<","p1":7,"p2":30}],"tp_type":"price","tp_value":0.189,"sl_type":"price","sl_value":0.235,"margin_usdt":100,"leverage":10,"expire_days":3}'
        style="width:100%;font-family:'JetBrains Mono',monospace;font-size:11px;line-height:1.5;resize:vertical"></textarea>
      <div id="json-errors" class="errbox"></div>
      <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;margin-top:10px;flex-wrap:wrap">
        <button class="btn" style="font-size:11px;padding:6px 12px" onclick="ornekJson()">Ornek doldur</button>
        <div style="display:flex;gap:8px">
          <button class="btn" onclick="document.getElementById('f-json').value=''">Temizle</button>
          <button class="btn btn-go" onclick="importJson()" id="json-save">Ekle</button>
        </div>
      </div>
    </div>
  </div>

  <p class="sect">Kayitli kurallar</p>
  <div class="card"><div class="tw"><table id="rules">
    <thead><tr><th>#</th><th>Coin</th><th>Yon</th><th>Periyot</th><th>Kosullar</th><th>TP</th><th>SL</th><th>Teminat</th><th>Durum</th><th></th></tr></thead>
    <tbody></tbody>
  </table></div></div>
</div>

<div id="p-ayarlar" style="display:none">
  <div class="card">
    <p class="sect">Sinyal havuzu stratejisi</p>
    <div class="frow">
      <div><label>Teminat</label><div class="cunit"><input id="s-margin" style="padding-left:22px"><span class="u" style="left:10px;right:auto">$</span></div></div>
      <div><label>Kaldirac</label><div class="cunit"><input id="s-lev"><span class="u">x</span></div></div>
      <div><label>Marj modu</label><select id="s-mm"><option value="cross">Cross</option><option value="isolated">Isolated</option></select></div>
    </div>
    <div class="frow">
      <div><label>Hard TP</label><div class="cunit"><input id="s-tp"><span class="u">%</span></div></div>
      <div><label>Hard SL</label><div class="cunit"><input id="s-sl"><span class="u">%</span></div></div>
      <div><label>Dedup suresi</label><div class="cunit"><input id="s-dedup"><span class="u">gun</span></div></div>
    </div>
    <div class="frow">
      <div><label>Sinyal tipleri</label><input id="s-types" placeholder="PUMP_1H,PUMP_15M"></div>
      <div><label>Strength</label><input id="s-strength" placeholder="strong"></div>
    </div>
  </div>

  <div class="card">
    <p class="sect">Havuz limitleri ve kasa</p>
    <div class="frow">
      <div><label>Max sinyal pozisyonu</label><input id="s-maxpos"></div>
      <div><label>Max kural pozisyonu</label><input id="s-maxrule"></div>
    </div>
    <div class="frow">
      <div><label>Min bakiye (sinyal)</label><div class="cunit"><input id="s-minbal" style="padding-left:22px"><span class="u" style="left:10px;right:auto">$</span></div></div>
      <div><label>Kural min serbest bakiye</label><div class="cunit"><input id="s-minfree" style="padding-left:22px"><span class="u" style="left:10px;right:auto">$</span></div></div>
      <div><label>Tarama araligi</label><div class="cunit"><input id="s-poll"><span class="u">sn</span></div></div>
    </div>
  </div>

  <div class="card">
    <p class="sect">Sinyal havuzu dinamik cikisi</p>
    <p style="font-size:11px;color:var(--text3);margin-bottom:10px">
      Screener sinyaliyle acilan pozisyonlar icin gecerlidir. Pozisyon acilirken
      bu yapilandirma dondurulur; sonradan degistirmen acik pozisyonlari etkilemez.
    </p>
      <div class="dynbox">
        <div class="dynhead">
          <label class="chk"><input type="checkbox" id="stp-active" onchange="dynToggle('stp')"><span>Dinamik TP</span></label>
          <span class="dynhint">Kar tarafi cikis kosulu</span>
        </div>
        <div id="stp-body" style="display:none">
          <div class="frow" style="margin-top:10px">
            <div><label>Periyot</label><select id="stp-tf">
              <option value="5m">5m</option><option value="15m">15m</option><option value="30m">30m</option>
              <option value="1h">1h</option><option value="4h">4h</option><option value="1D">1D</option>
            </select></div>
            <div><label>Hard ile iliski</label><select id="stp-mode" onchange="dynModeWarn('stp')">
              <option value="OR">VEYA - hangisi once gelirse</option>
              <option value="AND">VE - ikisi birden gerekli</option>
            </select></div>
            <div><label>Kosul mantigi</label><select id="stp-logic">
              <option value="-">&#8212; (tek kural)</option>
              <option value="AND">Ve</option>
              <option value="OR">Veya</option>
            </select></div>
          </div>
          <div id="stp-conds"></div>
          <button class="btn" style="font-size:11px;padding:6px 12px;margin-top:6px" onclick="addCond('stp-conds')">+ Kosul ekle</button>
          <div id="stp-warn" class="warnbox" style="display:none">
            <b>VE modu uyarisi:</b> Bu modda hard seviye Binance'e emir olarak GONDERILMEZ,
            bot her iki kosulu birlikte izler. Bot durursa bu koruma da durur.
          </div>
        </div>
      </div>
      <div class="dynbox">
        <div class="dynhead">
          <label class="chk"><input type="checkbox" id="ssl-active" onchange="dynToggle('ssl')"><span>Dinamik SL</span></label>
          <span class="dynhint">Zarar tarafi cikis kosulu</span>
        </div>
        <div id="ssl-body" style="display:none">
          <div class="frow" style="margin-top:10px">
            <div><label>Periyot</label><select id="ssl-tf">
              <option value="5m">5m</option><option value="15m">15m</option><option value="30m">30m</option>
              <option value="1h">1h</option><option value="4h">4h</option><option value="1D">1D</option>
            </select></div>
            <div><label>Hard ile iliski</label><select id="ssl-mode" onchange="dynModeWarn('ssl')">
              <option value="OR">VEYA - hangisi once gelirse</option>
              <option value="AND">VE - ikisi birden gerekli</option>
            </select></div>
            <div><label>Kosul mantigi</label><select id="ssl-logic">
              <option value="-">&#8212; (tek kural)</option>
              <option value="AND">Ve</option>
              <option value="OR">Veya</option>
            </select></div>
          </div>
          <div id="ssl-conds"></div>
          <button class="btn" style="font-size:11px;padding:6px 12px;margin-top:6px" onclick="addCond('ssl-conds')">+ Kosul ekle</button>
          <div id="ssl-warn" class="warnbox" style="display:none">
            <b>VE modu uyarisi:</b> Bu modda hard seviye Binance'e emir olarak GONDERILMEZ,
            bot her iki kosulu birlikte izler. Bot durursa bu koruma da durur.
          </div>
        </div>
      </div>
  </div>

  <div class="card">
    <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;margin-bottom:4px">
      <p class="sect" style="margin:0">TradingView webhook</p>
      <span id="wh-state" class="badge b-off">—</span>
    </div>
    <p style="font-size:11px;color:var(--text3);margin-bottom:10px">
      Alarm payload'inda belirtilmeyen alanlar buradaki varsayilanlardan alinir.
      Webhook pozisyonlari ozel havuza sayilir.
    </p>
    <div class="frow">
      <div><label>Teminat</label><div class="cunit"><input id="s-whmargin" style="padding-left:22px"><span class="u" style="left:10px;right:auto">$</span></div></div>
      <div><label>Kaldirac</label><div class="cunit"><input id="s-whlev"><span class="u">x</span></div></div>
      <div><label>Tekrar korumasi</label><div class="cunit"><input id="s-whdedup"><span class="u">sn</span></div></div>
    </div>
    <div class="frow">
      <div><label>TP tipi</label><select id="s-whtptype"><option value="pct">Yuzde</option><option value="price">Fiyat</option></select></div>
      <div><label>TP degeri</label><input id="s-whtpval"></div>
      <div><label>SL tipi</label><select id="s-whsltype"><option value="pct">Yuzde</option><option value="price">Fiyat</option></select></div>
      <div><label>SL degeri</label><input id="s-whslval"></div>
    </div>
      <div class="dynbox">
        <div class="dynhead">
          <label class="chk"><input type="checkbox" id="wtp-active" onchange="dynToggle('wtp')"><span>Dinamik TP</span></label>
          <span class="dynhint">Webhook pozisyonlari icin</span>
        </div>
        <div id="wtp-body" style="display:none">
          <div class="frow" style="margin-top:10px">
            <div><label>Periyot</label><select id="wtp-tf">
              <option value="5m">5m</option><option value="15m">15m</option><option value="30m">30m</option>
              <option value="1h">1h</option><option value="4h">4h</option><option value="1D">1D</option>
            </select></div>
            <div><label>Hard ile iliski</label><select id="wtp-mode" onchange="dynModeWarn('wtp')">
              <option value="OR">VEYA - hangisi once gelirse</option>
              <option value="AND">VE - ikisi birden gerekli</option>
            </select></div>
            <div><label>Kosul mantigi</label><select id="wtp-logic">
              <option value="-">&#8212; (tek kural)</option>
              <option value="AND">Ve</option>
              <option value="OR">Veya</option>
            </select></div>
          </div>
          <div id="wtp-conds"></div>
          <button class="btn" style="font-size:11px;padding:6px 12px;margin-top:6px" onclick="addCond('wtp-conds')">+ Kosul ekle</button>
          <div id="wtp-warn" class="warnbox" style="display:none">
            <b>VE modu uyarisi:</b> Bu modda hard seviye Binance'e emir olarak GONDERILMEZ,
            bot her iki kosulu birlikte izler. Bot durursa bu koruma da durur.
          </div>
        </div>
      </div>
      <div class="dynbox">
        <div class="dynhead">
          <label class="chk"><input type="checkbox" id="wsl-active" onchange="dynToggle('wsl')"><span>Dinamik SL</span></label>
          <span class="dynhint">Webhook pozisyonlari icin</span>
        </div>
        <div id="wsl-body" style="display:none">
          <div class="frow" style="margin-top:10px">
            <div><label>Periyot</label><select id="wsl-tf">
              <option value="5m">5m</option><option value="15m">15m</option><option value="30m">30m</option>
              <option value="1h">1h</option><option value="4h">4h</option><option value="1D">1D</option>
            </select></div>
            <div><label>Hard ile iliski</label><select id="wsl-mode" onchange="dynModeWarn('wsl')">
              <option value="OR">VEYA - hangisi once gelirse</option>
              <option value="AND">VE - ikisi birden gerekli</option>
            </select></div>
            <div><label>Kosul mantigi</label><select id="wsl-logic">
              <option value="-">&#8212; (tek kural)</option>
              <option value="AND">Ve</option>
              <option value="OR">Veya</option>
            </select></div>
          </div>
          <div id="wsl-conds"></div>
          <button class="btn" style="font-size:11px;padding:6px 12px;margin-top:6px" onclick="addCond('wsl-conds')">+ Kosul ekle</button>
          <div id="wsl-warn" class="warnbox" style="display:none">
            <b>VE modu uyarisi:</b> Bu modda hard seviye Binance'e emir olarak GONDERILMEZ,
            bot her iki kosulu birlikte izler. Bot durursa bu koruma da durur.
          </div>
        </div>
      </div>
    <div style="border-top:1px solid var(--border);padding-top:12px;margin-top:12px">
      <label>Webhook URL</label>
      <div class="cprow">
        <input id="wh-url" readonly>
        <button class="btn" onclick="kopyala('wh-url',this)">Kopyala</button>
      </div>

      <div style="border-top:1px solid var(--border);padding-top:12px;margin-top:14px">
        <p class="sect" style="margin-bottom:4px">Mesaj olusturucu</p>
        <p style="font-size:11px;color:var(--text3);margin-bottom:10px">
          Alarma ozel degerler gir, uretilen JSON'u TradingView Message kutusuna yapistir.
          Bos biraktigin alanlar yukaridaki varsayilanlardan alinir.
        </p>
        <div class="frow">
          <div><label>Islem</label><select id="g-action" onchange="uretJson()">
            <option value="open">Pozisyon ac</option>
            <option value="close">Pozisyonu kapat</option>
          </select></div>
          <div><label>Coin</label><input id="g-coin" placeholder="{{ticker}}" oninput="uretJson()"></div>
          <div id="g-dir-wrap"><label>Yon</label><select id="g-dir" onchange="uretJson()">
            <option value="SHORT">Short</option><option value="LONG">Long</option>
          </select></div>
        </div>
        <div class="frow" id="g-detay">
          <div><label>TP tipi</label><select id="g-tptype" onchange="uretJson()">
            <option value="">Varsayilan</option><option value="pct">Yuzde</option><option value="price">Fiyat</option>
          </select></div>
          <div><label>TP degeri</label><input id="g-tpval" placeholder="bos = varsayilan" oninput="uretJson()"></div>
          <div><label>SL tipi</label><select id="g-sltype" onchange="uretJson()">
            <option value="">Varsayilan</option><option value="pct">Yuzde</option><option value="price">Fiyat</option>
          </select></div>
          <div><label>SL degeri</label><input id="g-slval" placeholder="bos = varsayilan" oninput="uretJson()"></div>
        </div>
        <div class="frow" id="g-detay2">
          <div><label>Teminat</label><div class="cunit"><input id="g-margin" placeholder="bos = varsayilan" style="padding-left:22px" oninput="uretJson()"><span class="u" style="left:10px;right:auto">$</span></div></div>
          <div><label>Kaldirac</label><div class="cunit"><input id="g-lev" placeholder="bos = varsayilan" oninput="uretJson()"><span class="u">x</span></div></div>
          <div><label>Not</label><input id="g-note" placeholder="opsiyonel" oninput="uretJson()"></div>
        </div>
        <label style="margin-top:8px">Uretilen mesaj</label>
        <div class="cprow">
          <textarea id="g-out" rows="3" readonly class="mono"></textarea>
          <button class="btn btn-go" onclick="kopyala('g-out',this)">Kopyala</button>
        </div>
      </div>

      <label style="margin-top:14px">Hazir mesaj &#8212; SHORT</label>
      <div class="cprow">
        <textarea id="wh-msg-short" rows="2" readonly class="mono"></textarea>
        <button class="btn" onclick="kopyala('wh-msg-short',this)">Kopyala</button>
      </div>

      <label style="margin-top:10px">Hazir mesaj &#8212; LONG</label>
      <div class="cprow">
        <textarea id="wh-msg-long" rows="2" readonly class="mono"></textarea>
        <button class="btn" onclick="kopyala('wh-msg-long',this)">Kopyala</button>
      </div>

      <label style="margin-top:10px">Hazir mesaj &#8212; POZISYONU KAPAT</label>
      <div class="cprow">
        <textarea id="wh-msg-close" rows="2" readonly class="mono"></textarea>
        <button class="btn" onclick="kopyala('wh-msg-close',this)">Kopyala</button>
      </div>

      <p style="font-size:11px;color:var(--text3);margin-top:8px">
        TradingView alarm penceresi: <b>Notifications</b> sekmesinde Webhook URL alanina
        yukaridaki adresi, <b>Settings</b> sekmesindeki Message kutusuna ilgili JSON'u yapistir.
        <code>{{ticker}}</code> TradingView tarafindan grafik sembolu ile degistirilir;
        sabit bir coin istiyorsan onun yerine coin adini yaz.
      </p>
    </div>
  </div>

  <div id="s-errors" class="errbox"></div>

  <div style="display:flex;justify-content:space-between;align-items:center;gap:10px;margin:12px 0 4px;flex-wrap:wrap">
    <span style="font-size:11px;color:var(--text3)">
      Testnet / canli anahtari guvenlik nedeniyle panelde degil, Coolify env ayarindadir.
      Kaydedilen ayarlar executor tarafindan en fazla 30 saniye icinde uygulanir.
    </span>
    <div style="display:flex;gap:8px">
      <button class="btn" onclick="fillSettings()">Geri al</button>
      <button class="btn btn-go" onclick="saveSettings()" id="s-save">Kaydet</button>
    </div>
  </div>
</div>

<div id="toast"></div>

<script>
var state=null, editingId=null;

/* ---------- tema ---------- */
function applyTheme(t){
  document.documentElement.setAttribute('data-theme', t);
  document.getElementById('theme-btn').innerHTML = (t==='dark') ? '&#9788;' : '&#9789;';
  var m=document.querySelector('meta[name=theme-color]');
  if(m) m.setAttribute('content', t==='dark' ? '#000000' : '#ffffff');
  try{localStorage.setItem('sts-theme',t)}catch(e){}
}
function toggleTheme(){
  var cur=document.documentElement.getAttribute('data-theme')||'light';
  applyTheme(cur==='dark'?'light':'dark');
}
(function(){
  var saved=null;
  try{saved=localStorage.getItem('sts-theme')}catch(e){}
  if(!saved) saved = (window.matchMedia&&window.matchMedia('(prefers-color-scheme: dark)').matches)?'dark':'light';
  applyTheme(saved);
})();

/* ---------- yardimcilar ---------- */
function show(name, el){
  ['durum','islemler','olaylar','kurallar','ayarlar'].forEach(function(n){
    document.getElementById('p-'+n).style.display=(n===name)?'':'none';
  });
  document.querySelectorAll('.tab').forEach(function(t){t.classList.remove('on')});
  el.classList.add('on');
}
function n(v,d){
  if(v===null||v===undefined||v===''||isNaN(v))return null;
  return Number(v);
}
function fmt(v,d){
  var x=n(v); if(x===null)return '—';
  return x.toLocaleString('en-US',{minimumFractionDigits:d===undefined?2:d,
    maximumFractionDigits:d===undefined?2:d});
}
function usd(v,d){
  var x=n(v); if(x===null)return '—';
  return '$'+Math.abs(x).toLocaleString('en-US',{minimumFractionDigits:d===undefined?2:d,
    maximumFractionDigits:d===undefined?2:d});
}
function sgn(v,d){
  var x=n(v); if(x===null)return '—';
  return (x<0?'-':'+')+usd(x,d);
}
function pctTxt(v,d){
  var x=n(v); if(x===null)return '';
  return (x<0?'':'+')+fmt(x,d===undefined?2:d)+'%';
}
function cls(v){var x=n(v); return x===null?'mut':(x>0?'up':(x<0?'dn':'mut'))}
/* Supabase UTC saklar - tarayicinin yerel saatine (TR) cevir */
function trZaman(iso, tarihli){
  if(!iso)return '—';
  var d=new Date(iso);
  if(isNaN(d))return String(iso).replace('T',' ').slice(5,16);
  var g=String(d.getDate()).padStart(2,'0');
  var a=String(d.getMonth()+1).padStart(2,'0');
  var s=String(d.getHours()).padStart(2,'0');
  var dk=String(d.getMinutes()).padStart(2,'0');
  var sn=String(d.getSeconds()).padStart(2,'0');
  return tarihli ? (g+'-'+a+' '+s+':'+dk+':'+sn) : (g+'-'+a+' '+s+':'+dk);
}

function esc(s){var d=document.createElement('div');d.textContent=s==null?'':String(s);return d.innerHTML}
function uretJson(){
  var out=document.getElementById('g-out');
  if(!out)return;
  var tok=(state&&state.webhook_token)||'<WEBHOOK_TOKEN tanimli degil>';
  function v(id){ var el=document.getElementById(id); return el?el.value.trim():''; }

  var eylem=v('g-action')||'open';
  var kapat=(eylem==='close');

  // kapatmada yon ve TP/SL/teminat alanlari gereksiz - gizle
  var dw=document.getElementById('g-dir-wrap');
  if(dw)dw.style.display=kapat?'none':'';
  ['g-detay','g-detay2'].forEach(function(id){
    var el=document.getElementById(id);
    if(el)el.style.display=kapat?'none':'';
  });

  if(kapat){
    out.value=JSON.stringify({token:tok, coin:(v('g-coin')||'{{ticker}}'), action:'close'});
    return;
  }

  var o={token:tok, coin:(v('g-coin')||'{{ticker}}'), direction:v('g-dir')||'SHORT'};

  var tpt=v('g-tptype'), tpv=v('g-tpval');
  if(tpv!==''){ o.tp_type = tpt||'pct'; o.tp_value = Number(tpv); }
  else if(tpt!==''){ o.tp_type = tpt; }

  var slt=v('g-sltype'), slv=v('g-slval');
  if(slv!==''){ o.sl_type = slt||'pct'; o.sl_value = Number(slv); }
  else if(slt!==''){ o.sl_type = slt; }

  var m=v('g-margin'); if(m!=='') o.margin_usdt = Number(m);
  var l=v('g-lev');    if(l!=='') o.leverage    = Number(l);
  var nt=v('g-note');  if(nt!=='') o.note       = nt;

  // sayi alanlarinda hatali giris varsa uyar
  var hatali=[];
  ['tp_value','sl_value','margin_usdt','leverage'].forEach(function(k){
    if(k in o && (isNaN(o[k])||o[k]<=0)) hatali.push(k);
  });
  out.value = hatali.length
    ? 'Gecersiz deger: '+hatali.join(', ')+' (sadece sayi gir)'
    : JSON.stringify(o);
}

function kopyala(id,btn){
  var el=document.getElementById(id);
  var eski=btn.textContent;
  function tamam(){ btn.textContent='Kopyalandi'; setTimeout(function(){btn.textContent=eski},1500); }
  // HTTP'de navigator.clipboard olmayabilir - yedek yontem
  if(navigator.clipboard&&window.isSecureContext){
    navigator.clipboard.writeText(el.value).then(tamam).catch(function(){secKopyala(el,tamam)});
  }else{ secKopyala(el,tamam); }
}
function secKopyala(el,cb){
  el.removeAttribute('readonly');
  el.select(); el.setSelectionRange(0,99999);
  try{ document.execCommand('copy'); cb(); }
  catch(e){ toast('Kopyalanamadi - elle secip kopyala'); }
  el.setAttribute('readonly','');
  window.getSelection().removeAllRanges();
}

function toast(m){
  var t=document.getElementById('toast');
  t.textContent=m;t.style.display='block';
  setTimeout(function(){t.style.display='none'},2400);
}

/* ---------- render ---------- */
function render(){
  if(!state)return;
  var st=state.status||{};

  var mode=document.getElementById('mode');
  if(st.testnet===false){mode.textContent='CANLI';mode.className='badge b-live';}
  else{mode.textContent='TESTNET';mode.className='badge b-test';}

  var h=document.getElementById('health'), age=state.status_age;
  if(age!==null&&age<90){h.textContent='Executor aktif ('+age+'s)';h.className='badge b-ok';}
  else if(age!==null){h.textContent='Executor sessiz ('+age+'s)';h.className='badge b-off';}
  else{h.textContent='Status yok';h.className='badge b-off';}

  var pv=state.panel_version||'?', bv=(st.version||'?');
  var vr=document.getElementById('ver');
  if(pv===bv){
    vr.textContent=pv;
    vr.className='badge b-off';
    vr.title='Panel ve executor ayni surumde: '+pv;
  }else{
    vr.textContent='P '+pv+' / B '+bv;
    vr.className='badge b-test';
    vr.title='SURUM UYUSMAZLIGI - panel '+pv+', executor '+bv
      +'. Biri deploy edilmemis olabilir.';
  }

  var seviye=(state.level||'RUN').toUpperCase();
  var ls=document.getElementById('lvl-state');
  ls.textContent=(state.emergency_pending?'ACIL CIKIS ISLENIYOR':(LVL_AD[seviye]||seviye));
  ls.className='badge '+(state.emergency_pending?'b-live':(seviye==='RUN'?'b-ok':(seviye==='PAUSE'?'b-test':'b-off')));

  document.getElementById('stop-banner').style.display=(seviye==='STOP')?'block':'none';

  var bp=document.getElementById('btn-pause'), bs=document.getElementById('btn-stop');
  bp.textContent=(seviye==='PAUSE')?'Devam et':'Duraklat';
  bp.className=(seviye==='PAUSE')?'btn btn-go':'btn';
  bs.textContent=(seviye==='STOP')?'Devam et':'Bot dur';
  bs.className=(seviye==='STOP')?'btn btn-go':'btn btn-stop';

  document.getElementById('m-bal').textContent=usd(st.balance,0);
  document.getElementById('m-sig').textContent=(st.sig_count!=null?st.sig_count:'—')+' / '+(st.sig_max||'—');
  document.getElementById('m-rule').textContent=(st.rule_count!=null?st.rule_count:'—')+' / '+(st.rule_max||'—');

  var pos=st.positions||[], upnl=0, marginSum=0, hasU=false;
  pos.forEach(function(p){
    if(p.upnl!=null){upnl+=Number(p.upnl);hasU=true;}
    if(p.margin!=null)marginSum+=Number(p.margin);
  });
  var mu=document.getElementById('m-upnl');
  mu.textContent=hasU?sgn(upnl):'—';
  mu.className='v '+(hasU?cls(upnl):'mut');
  document.getElementById('m-upnl-pct').textContent=
    (hasU&&marginSum>0)?pctTxt(upnl/marginSum*100)+' teminata gore':'Anlik';

  var box=document.getElementById('positions');
  // Yonet paneli acikken listeyi yeniden cizme - kullanici duzenleme yapiyor
  if(acikPos){
    guncellePnl(pos);
  }
  else if(!pos.length){box.innerHTML='<div class="empty">Acik pozisyon yok</div>';}
  else{
    box.innerHTML=pos.map(function(p){
      var u=n(p.upnl), m=n(p.margin);
      var pct=(u!==null&&m)?u/m*100:null;
      var tid = p.trade_id||0;
      return '<div class="pos"><div class="pos-l">'
        +'<div class="pos-nm"><b>'+esc(p.coin)+'</b>'
        +'<span class="badge '+(p.side==='LONG'?'b-long':'b-short')+'">'+esc(p.side||'')+'</span>'
        +'<span class="badge '+(p.source==='rule'?'b-rule':'b-sig')+'">'+(p.source==='rule'?'Kural':'Sinyal')+'</span></div>'
        +'<div class="pos-dt mono"><span>Giris</span> '+fiyat(p.entry)
        +' &nbsp;<span>Mark</span> <b id="mark-'+tid+'" style="font-weight:400">'+fiyat(p.mark)+'</b>'
        +' &nbsp;<span>TP</span> '+fiyat(p.tp)+korumaRozet(p.tp_order,'TP')
        +' &nbsp;<span>SL</span> '+fiyat(p.sl)+korumaRozet(p.sl_order,'SL')
        +' &nbsp;<span>'+(p.leverage||'—')+'x</span>'
        +(m?' &nbsp;<span>'+usd(m,0)+'</span>':'')
        +'</div></div>'
        +'<div class="pos-r"><div id="pnl-'+tid+'" class="pnl '+cls(u)+'">'+sgn(u)+'</div>'
        +(pct!==null?'<div id="pnlp-'+tid+'" class="pnl-pct '+cls(pct)+'">'+pctTxt(pct)+'</div>':'')
        +(tid?'<button class="mini" style="margin:6px 0 0" onclick="openPos('+tid+')">Yonet</button>':'')
        +'</div></div>'
        +(tid?posPanel(p,tid):'');
    }).join('');
  }

  /* islemler */
  var closed=(state.trades||[]).filter(function(t){return t.closed_at});
  var pnlSum=0,marSum=0,win=0;
  closed.forEach(function(t){
    var p=n(t.pnl)||0; pnlSum+=p; if(p>0)win++;
    var m=n(t.margin_usdt); if(m)marSum+=m;
  });
  document.getElementById('t-count').textContent=closed.length;
  var tp=document.getElementById('t-pnl');
  tp.textContent=closed.length?sgn(pnlSum):'—';
  tp.className='v '+cls(closed.length?pnlSum:null);
  document.getElementById('t-pnl-pct').textContent=
    marSum>0?pctTxt(pnlSum/marSum*100)+' teminata gore':'Teminata gore';
  document.getElementById('t-win').textContent=closed.length?Math.round(win/closed.length*100)+'%':'—';
  document.getElementById('t-win-sub').textContent=closed.length?(win+' / '+closed.length+' karli'):'Karli / toplam';

  var tb=document.querySelector('#trades tbody');
  var trades=state.trades||[];
  tb.innerHTML=trades.length?trades.map(function(t){
    var p=n(t.pnl), m=n(t.margin_usdt);
    var pct=(p!==null&&m)?p/m*100:null;
    return '<tr><td><b>'+esc(t.coin)+'</b></td>'
      +'<td><span class="badge '+(t.side==='LONG'?'b-long':'b-short')+'">'+esc(t.side||'')+'</span></td>'
      +'<td><span class="badge '+(t.source==='rule'?'b-rule':'b-sig')+'">'+(t.source==='rule'?'Kural':'Sinyal')+'</span></td>'
      +'<td class="mono">'+fiyat(t.entry_price)+'</td>'
      +'<td class="mono">'+(t.closed_at?fiyat(t.exit_price):'<span class="mut">Acik</span>')+'</td>'
      +'<td class="mono '+cls(p)+'"><b>'+(p===null?'—':sgn(p))+'</b>'
        +(pct!==null?'<br><span style="font-size:10px">'+pctTxt(pct)+'</span>':'')+'</td>'
      +'<td>'+esc(t.exit_reason||'—')+'</td>'
      +'<td class="mut mono">'+esc(trZaman(t.opened_at))+'</td></tr>';
  }).join(''):'<tr><td colspan="8" class="empty">Islem yok</td></tr>';

  var wb=document.querySelector('#webhooks tbody');
  var whs=state.webhooks||[];
  wb.innerHTML=whs.length?whs.map(function(w){
    var d;
    var r=w.result||'';
    if(!w.executed){d='<span class="badge b-test">Bekliyor</span>';}
    else if(r.indexOf('OPENED')===0){d='<span class="badge b-ok">Acildi</span>';}
    else if(r.indexOf('CLOSED')===0){d='<span class="badge b-done">Kapatildi</span>';}
    else if(r.indexOf('SKIPPED')===0){d='<span class="badge b-sig">Atlandi</span>';}
    else {d='<span class="badge b-err">Hata</span>';}
    return '<tr><td class="mut">'+w.id+'</td>'
      +'<td class="mut mono">'+esc(trZaman(w.created_at,true))+'</td>'
      +'<td><b>'+esc(w.coin)+'</b></td>'
      +'<td><span class="badge '+(w.direction==='LONG'?'b-long':'b-short')+'">'+esc(w.direction)+'</span></td>'
      +'<td>'+d+'</td>'
      +'<td style="white-space:normal">'+esc(w.result||'—')+'</td></tr>';
  }).join(''):'<tr><td colspan="6" class="empty">Webhook yok</td></tr>';

  var eb=document.querySelector('#events tbody');
  var evs=state.events||[];
  eb.innerHTML=evs.length?evs.map(function(e){
    return '<tr><td class="mut mono">'+esc(trZaman(e.ts,true))+'</td>'
      +'<td>'+olayRozet(e.kind)+'</td><td><b>'+esc(e.coin||'—')+'</b></td>'
      +'<td style="white-space:normal">'+esc(e.detail||'')+'</td></tr>';
  }).join(''):'<tr><td colspan="4" class="empty">Olay yok</td></tr>';

  var rb=document.querySelector('#rules tbody');
  var rules=state.rules||[];
  rb.innerHTML=rules.length?rules.map(function(r){
    var stt=r.active?'<span class="badge b-ok">Aktif</span>'
      :(r.triggered_at?'<span class="badge b-done">Tetiklendi</span>':'<span class="badge b-off">Pasif</span>');
    return '<tr><td class="mut">'+r.id+'</td><td><b>'+esc(r.coin)+'</b></td>'
      +'<td><span class="badge '+(r.direction==='LONG'?'b-long':'b-short')+'">'+esc(r.direction)+'</span></td>'
      +'<td>'+esc(r.timeframe)+'</td>'
      +'<td class="mono" style="max-width:240px;overflow:hidden;text-overflow:ellipsis">'+esc(condText(r.conditions,r.logic))+'</td>'
      +'<td class="mono">'+lvl(r.tp_type,r.tp_value)+dynBadge(r,'tp')+'</td>'
      +'<td class="mono">'+lvl(r.sl_type,r.sl_value)+dynBadge(r,'sl')+'</td>'
      +'<td class="mono mut">'+usd(r.margin_usdt,0)+' '+(r.leverage||'—')+'x</td>'
      +'<td>'+stt+'</td>'
      +'<td style="text-align:right">'
      +'<button class="mini" onclick="editRule('+r.id+')">Duzenle</button>'
      +'<button class="mini" onclick="toggleRule('+r.id+','+(!r.active)+')">'+(r.active?'Pasif':'Aktif')+'</button>'
      +'<button class="mini del" onclick="delRule('+r.id+')">Sil</button>'
      +'</td></tr>';
  }).join(''):'<tr><td colspan="10" class="empty">Kural yok</td></tr>';

  if(!settingsDirty) fillSettings();
}

/* ---------- ayarlar ---------- */
var settingsDirty=false;
var S_MAP={'s-margin':'margin_usdt','s-lev':'leverage','s-mm':'margin_mode',
  's-tp':'tp_pct','s-sl':'sl_pct','s-dedup':'dedup_days','s-types':'signal_types',
  's-strength':'strength','s-maxpos':'max_positions','s-maxrule':'max_rule_positions',
  's-minbal':'min_balance','s-minfree':'rule_min_free','s-poll':'poll_seconds',
  's-whmargin':'wh_margin_usdt','s-whlev':'wh_leverage','s-whdedup':'wh_dedup_sec',
  's-whtptype':'wh_tp_type','s-whtpval':'wh_tp_value',
  's-whsltype':'wh_sl_type','s-whslval':'wh_sl_value'};

function fillSettings(){
  var st=state&&state.settings;
  if(!st)return;
  Object.keys(S_MAP).forEach(function(id){
    var el=document.getElementById(id);
    if(!el)return;
    var deger=st[S_MAP[id]];
    el.value=(deger===null||deger===undefined)?'':deger;
  });
  dynFill('stp',{active:st.dyn_tp_active,timeframe:st.dyn_tp_timeframe,
    mode:st.dyn_tp_mode,logic:st.dyn_tp_logic,conditions:st.dyn_tp_conditions});
  dynFill('ssl',{active:st.dyn_sl_active,timeframe:st.dyn_sl_timeframe,
    mode:st.dyn_sl_mode,logic:st.dyn_sl_logic,conditions:st.dyn_sl_conditions});
  dynFill('wtp',{active:st.wh_dyn_tp_active,timeframe:st.wh_dyn_tp_timeframe,
    mode:st.wh_dyn_tp_mode,logic:st.wh_dyn_tp_logic,conditions:st.wh_dyn_tp_conditions});
  dynFill('wsl',{active:st.wh_dyn_sl_active,timeframe:st.wh_dyn_sl_timeframe,
    mode:st.wh_dyn_sl_mode,logic:st.wh_dyn_sl_logic,conditions:st.wh_dyn_sl_conditions});
  var ws=document.getElementById('wh-state');
  if(state.webhook_enabled){ws.textContent='Aktif';ws.className='badge b-ok';}
  else{ws.textContent='Kapali - WEBHOOK_TOKEN yok';ws.className='badge b-off';}
  var tok=state.webhook_token||'<WEBHOOK_TOKEN tanimli degil>';
  document.getElementById('wh-url').value=location.origin+'/webhook';
  document.getElementById('wh-msg-short').value=
    '{"token":"'+tok+'","coin":"{{ticker}}","direction":"SHORT"}';
  document.getElementById('wh-msg-long').value=
    '{"token":"'+tok+'","coin":"{{ticker}}","direction":"LONG"}';
  document.getElementById('wh-msg-close').value=
    '{"token":"'+tok+'","coin":"{{ticker}}","action":"close"}';
  uretJson();
  settingsDirty=false;
  document.getElementById('s-errors').style.display='none';
}

function saveSettings(){
  var body=Object.assign({}, dynRead('stp','dyn_tp'), dynRead('ssl','dyn_sl'),
    dynRead('wtp','wh_dyn_tp'), dynRead('wsl','wh_dyn_sl'));
  Object.keys(S_MAP).forEach(function(id){
    var el=document.getElementById(id);
    if(el) body[S_MAP[id]]=el.value;
  });
  var btn=document.getElementById('s-save');
  btn.disabled=true;btn.textContent='Kaydediliyor...';
  fetch('/api/settings',{method:'PATCH',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body)})
    .then(function(r){return r.json().then(function(j){return {s:r.status,j:j}})})
    .then(function(res){
      btn.disabled=false;btn.textContent='Kaydet';
      if(res.s===200&&res.j.ok){
        settingsDirty=false;
        document.getElementById('s-errors').style.display='none';
        toast('Ayarlar kaydedildi');refresh();
      }else{
        var b=document.getElementById('s-errors');
        b.innerHTML=(res.j.errors||['Kaydedilemedi']).map(function(e){return '&bull; '+esc(e)}).join('<br>');
        b.style.display='block';
      }
    }).catch(function(){
      btn.disabled=false;btn.textContent='Kaydet';
      var b=document.getElementById('s-errors');
      b.innerHTML='&bull; Baglanti hatasi';b.style.display='block';
    });
}

/* ---------- JSON ile kural ekleme ---------- */
function toggleJson(){
  var b=document.getElementById('json-box'), t=document.getElementById('json-toggle');
  var acik=b.style.display!=='none';
  b.style.display=acik?'none':'';
  t.textContent=acik?'Ac':'Kapat';
}
function ornekJson(){
  document.getElementById('f-json').value=JSON.stringify({
    coin:'HEI', direction:'SHORT', timeframe:'5m', logic:'-',
    conditions:[{type:'ema_cross',op:'<',p1:7,p2:30}],
    tp_type:'price', tp_value:0.189, sl_type:'price', sl_value:0.235,
    margin_usdt:100, leverage:10, expire_days:3, note:'ornek'
  },null,2);
}
function importJson(){
  var raw=document.getElementById('f-json').value.trim();
  var box=document.getElementById('json-errors');
  box.style.display='none';
  if(!raw){box.innerHTML='&bull; JSON bos';box.style.display='block';return;}
  var data;
  try{ data=JSON.parse(raw); }
  catch(e){ box.innerHTML='&bull; JSON gecersiz: '+esc(e.message);box.style.display='block';return; }
  var btn=document.getElementById('json-save');
  btn.disabled=true;btn.textContent='Ekleniyor...';
  fetch('/api/rules/import',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(data)})
    .then(function(r){return r.json().then(function(j){return {s:r.status,j:j}})})
    .then(function(res){
      btn.disabled=false;btn.textContent='Ekle';
      if(res.s===200&&res.j.ok){
        toast(res.j.count+' kural eklendi');
        document.getElementById('f-json').value='';
        toggleJson();refresh();
      }else{
        box.innerHTML=(res.j.errors||['Eklenemedi']).map(function(e){return '&bull; '+esc(e)}).join('<br>');
        box.style.display='block';
      }
    }).catch(function(){
      btn.disabled=false;btn.textContent='Ekle';
      box.innerHTML='&bull; Baglanti hatasi';box.style.display='block';
    });
}

/* ---------- acik pozisyon yonetimi ---------- */
/* Borsada koruma emri yoksa uyar: o taraf yalnizca bot izlemesine bagli.
   Bot durursa (veya STOP seviyesinde) o koruma da durur. */
function korumaRozet(varMi,tur){
  if(varMi!==false)return '';
  return ' <span class="badge b-test" title="Borsada '+tur+' emri YOK - koruma yalnizca'
    +' bot tarafinda (yumusak '+tur+'). Bot durursa bu koruma da durur.">koruma bot</span>';
}

function posPanel(p,tid){
  return '<div id="pp-'+tid+'" class="pospanel" style="display:none">'
    +'<div class="frow">'
      +'<div><label>Hard TP</label><input id="pp-tp-'+tid+'" value="'+(p.tp==null?'':p.tp)+'"></div>'
      +'<div><label>Hard SL</label><input id="pp-sl-'+tid+'" value="'+(p.sl==null?'':p.sl)+'"></div>'
    +'</div>'
    +((p.tp_order===false||p.sl_order===false)
      ? '<div class="warnbox" style="display:block;margin-top:8px">'
        +'<b>Koruma bot tarafinda.</b> Borsada '
        +[(p.tp_order===false?'TP':null),(p.sl_order===false?'SL':null)].filter(Boolean).join(' ve ')
        +' emri yok; bu seviye yalnizca botun yumusak izlemesiyle korunuyor. '
        +'Bot durursa (veya Bot dur seviyesinde) koruma kalkar.</div>'
      : '')
    +'<div class="dynbox" style="margin-top:8px">'
      +'<div class="dynhead">'
        +'<label class="chk"><input type="checkbox" id="p'+tid+'tp-active" onchange="dynToggle(\\'p'+tid+'tp\\')"><span>Dinamik TP</span></label>'
        +'<span class="dynhint">Bu pozisyona ozel</span></div>'
      +'<div id="p'+tid+'tp-body" style="display:none">'+dynBody('p'+tid+'tp')+'</div>'
    +'</div>'
    +'<div class="dynbox">'
      +'<div class="dynhead">'
        +'<label class="chk"><input type="checkbox" id="p'+tid+'sl-active" onchange="dynToggle(\\'p'+tid+'sl\\')"><span>Dinamik SL</span></label>'
        +'<span class="dynhint">Bu pozisyona ozel</span></div>'
      +'<div id="p'+tid+'sl-body" style="display:none">'+dynBody('p'+tid+'sl')+'</div>'
    +'</div>'
    +'<div id="pp-son-'+tid+'"></div>'
    +'<div id="pp-err-'+tid+'" class="errbox"></div>'
    +'<div style="display:flex;justify-content:space-between;gap:8px;margin-top:10px;flex-wrap:wrap">'
      +'<button class="btn btn-stop" onclick="closePos('+tid+')">Pozisyonu kapat</button>'
      +'<div style="display:flex;gap:8px">'
        +'<button class="btn" onclick="hidePos('+tid+')">Kapat</button>'
        +'<button class="btn btn-go" onclick="savePos('+tid+')">Kaydet</button>'
      +'</div></div>'
    +'</div>';
}

function dynBody(on){
  return '<div class="frow" style="margin-top:10px">'
    +'<div><label>Periyot</label><select id="'+on+'-tf">'
      +'<option value="5m">5m</option><option value="15m">15m</option><option value="30m">30m</option>'
      +'<option value="1h">1h</option><option value="4h">4h</option><option value="1D">1D</option>'
    +'</select></div>'
    +'<div><label>Hard ile iliski</label><select id="'+on+'-mode" onchange="dynModeWarn(\\''+on+'\\')">'
      +'<option value="OR">VEYA - hangisi once gelirse</option>'
      +'<option value="AND">VE - ikisi birden gerekli</option></select></div>'
    +'<div><label>Kosul mantigi</label><select id="'+on+'-logic">'
      +'<option value="-">&#8212; (tek kural)</option>'
      +'<option value="AND">Ve</option><option value="OR">Veya</option></select></div>'
    +'</div>'
    +'<div id="'+on+'-conds"></div>'
    +'<button class="btn" style="font-size:11px;padding:6px 12px;margin-top:6px" '
      +'onclick="addCond(\\''+on+'-conds\\')">+ Kosul ekle</button>'
    +'<div id="'+on+'-warn" class="warnbox" style="display:none">'
      +'<b>VE modu uyarisi:</b> Bu modda hard seviye Binance\\'e emir olarak GONDERILMEZ, '
      +'bot her iki kosulu birlikte izler. Bot durursa bu koruma da durur.</div>';
}

var acikPos=null;

/* Yonet paneli acikken: kartlari yeniden kurmadan sadece PnL/mark tazele */
function guncellePnl(pos){
  pos.forEach(function(p){
    var tid=p.trade_id;
    if(!tid)return;
    var u=n(p.upnl), m=n(p.margin);
    var pct=(u!==null&&m)?u/m*100:null;
    var el=document.getElementById('pnl-'+tid);
    if(el){ el.textContent=sgn(u); el.className='pnl '+cls(u); }
    var el2=document.getElementById('pnlp-'+tid);
    if(el2&&pct!==null){ el2.textContent=pctTxt(pct); el2.className='pnl-pct '+cls(pct); }
    var el3=document.getElementById('mark-'+tid);
    if(el3){ el3.textContent=fiyat(p.mark); }
  });
}

function openPos(tid){
  if(acikPos&&acikPos!==tid)hidePos(acikPos);
  var el=document.getElementById('pp-'+tid);
  if(!el)return;
  if(el.style.display!=='none'){hidePos(tid);return;}
  var bekleyen=(state.trades||[]).filter(function(x){return x.id===tid})[0]||{};
  if(bekleyen.req_tp_price!=null||bekleyen.req_sl_price!=null||bekleyen.req_close){
    toast('Bu pozisyonda bekleyen bir istek var');
  }
  el.style.display='block';
  acikPos=tid;
  var t=(state.trades||[]).filter(function(x){return x.id===tid})[0]||{};
  // son istek sonucu (basarili/basarisiz) goster
  var sonEl=document.getElementById('pp-son-'+tid);
  if(sonEl){
    if(t.req_result){
      var kotu=/KORUMASIZ|degistirilemedi|basarisiz|HATA|mantiksiz/i.test(t.req_result);
      sonEl.innerHTML='<div class="'+(kotu?'warnbox':'okbox')+'" style="display:block">'
        +'<b>Son istek:</b> '+esc(t.req_result)
        +(t.req_at?' <span style="opacity:.7">('+esc(trZaman(t.req_at,true))+')</span>':'')
        +'</div>';
    } else { sonEl.innerHTML=''; }
  }
  LOGIC_OF['p'+tid+'tp-conds']='p'+tid+'tp-logic';
  LOGIC_OF['p'+tid+'sl-conds']='p'+tid+'sl-logic';
  dynFill('p'+tid+'tp', t.dyn_tp);
  dynFill('p'+tid+'sl', t.dyn_sl);
}
function hidePos(tid){
  var el=document.getElementById('pp-'+tid);
  if(el)el.style.display='none';
  if(acikPos===tid)acikPos=null;
}
function savePos(tid){
  var body=Object.assign({},
    dynRead('p'+tid+'tp','dyn_tp'), dynRead('p'+tid+'sl','dyn_sl'));
  // Hard TP/SL: SADECE degistiyse gonder. Aksi halde executor bosuna
  // Binance emirlerini iptal/yeniden kurmaya calisir (demo'da -4130 hatasi).
  var t=(state.trades||[]).filter(function(x){return x.id===tid})[0]||{};
  var yeniTp=document.getElementById('pp-tp-'+tid).value.trim();
  var yeniSl=document.getElementById('pp-sl-'+tid).value.trim();
  if(yeniTp!=='' && Number(yeniTp)!==Number(t.tp_price)) body.tp_price=yeniTp;
  if(yeniSl!=='' && Number(yeniSl)!==Number(t.sl_price)) body.sl_price=yeniSl;
  var mesaj = (body.tp_price!==undefined||body.sl_price!==undefined)
    ? 'Istek gonderildi - executor 20sn icinde uygular'
    : 'Dinamik cikis kaydedildi';
  gonderPos(tid, body, mesaj);
}
function closePos(tid){
  var t=(state.trades||[]).filter(function(x){return x.id===tid})[0]||{};
  if(!confirm((t.coin||'Pozisyon')+' kapatilsin mi?\\nBu islem geri alinamaz.'))return;
  gonderPos(tid, {close:true}, 'Kapatma istegi gonderildi');
}
function gonderPos(tid, body, mesaj){
  fetch('/api/positions/'+tid,{method:'PATCH',
    headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
    .then(function(r){return r.json().then(function(j){return {s:r.status,j:j}})})
    .then(function(res){
      var b=document.getElementById('pp-err-'+tid);
      if(res.s===200&&res.j.ok){ if(b)b.style.display='none'; toast(mesaj); hidePos(tid); refresh(); }
      else if(b){ b.innerHTML=(res.j.errors||['Gonderilemedi']).map(function(e){
        return '&bull; '+esc(e)}).join('<br>'); b.style.display='block'; }
    }).catch(function(){
      var b=document.getElementById('pp-err-'+tid);
      if(b){b.innerHTML='&bull; Baglanti hatasi';b.style.display='block';}
    });
}

/* ---------- kosul tanimlari ---------- */
var CT={
  ema_cross:{ad:'EMA kesisimi', p1:'Hizli', p2:'Yavas',  u2:'',  d1:7,   d2:30,    op:'<'},
  rsi:      {ad:'RSI',          p1:null,    p2:'Esik',   u2:'',  d1:null,d2:70,    op:'>'},
  price:    {ad:'Fiyat',        p1:null,    p2:'Fiyat',  u2:'$', d1:null,d2:'',    op:'<'},
  oi_change:{ad:'OI degisimi',  p1:'Onceki bar', p2:'Fark', u2:'%', d1:3, d2:5,   op:'>'},
  volume:   {ad:'Hacim',        p1:'Onceki bar', p2:'Fark', u2:'%', d1:3, d2:5,   op:'>'},
  funding:  {ad:'Funding',      p1:null,    p2:'Oran',   u2:'%', d1:null,d2:-0.05, op:'<'},
  touch_price:{ad:'Fiyat degdi',p1:null,    p2:'Fiyat',  u2:'$', d1:null,d2:'',    op:'=', deg:1},
  touch_ema:{ad:'EMA degdi',    p1:null,    p2:'Periyot',u2:'',  d1:null,d2:30,    op:'=', deg:1}
};

/* Olay tipine gore renkli rozet */
var OLAY_SINIF={
  OPEN:'b-ok', CLOSE:'b-done', RULE_TRIGGER:'b-rule', SIGNAL_SKIP:'b-sig',
  ERROR:'b-err', LEVEL:'b-test', EMERGENCY:'b-err', SETTINGS:'b-off',
  WEBHOOK_REJECT:'b-err', SIZE_CLIP:'b-test', LEVEL_CHANGE:'b-done',
  LEVEL_FAIL:'b-err', SL_SOFT:'b-err', TP_SOFT:'b-ok'
};
function olayRozet(k){
  var s=OLAY_SINIF[k]||'b-off';
  return '<span class="badge '+s+'">'+esc(k||'-')+'</span>';
}

function yonTxt(op){ return (op==='>'||op==='>=') ? 'uzeri' : 'alti'; }

function condText(conds,logic){
  if(typeof conds==='string'){try{conds=JSON.parse(conds)}catch(e){return String(conds)}}
  if(!conds||!conds.length)return '—';
  var txt=conds.map(function(c){
    var t=c.type;
    if(t==='ema_cross')return 'EMA'+c.p1+' '+c.op+' EMA'+c.p2;
    if(t==='rsi')return 'RSI '+c.op+' '+c.p2;
    if(t==='oi_change')return 'OI: '+c.p1+' bar ort. '+yonTxt(c.op)+' %'+Math.abs(c.p2);
    if(t==='volume')return 'Hacim: '+c.p1+' bar ort. '+yonTxt(c.op)+' %'+Math.abs(c.p2);
    if(t==='funding')return 'Funding '+c.op+' '+c.p2+'%';
    if(t==='touch_price')return 'Fiyat $'+c.p2+' seviyesine DEGDI';
    if(t==='touch_ema')return 'Fiyat EMA'+c.p2+' seviyesine DEGDI';
    if(t==='price')return 'Fiyat '+c.op+' $'+c.p2;
    return t+' '+c.op+' '+c.p2;
  });
  if(txt.length===1)return txt[0];
  return txt.join(logic==='OR'?'  VEYA  ':'  VE  ');
}
function dynBadge(r,which){
  if(!r['dyn_'+which+'_active'])return '';
  var mode=(r['dyn_'+which+'_mode']||'OR').toUpperCase();
  var tf=r['dyn_'+which+'_timeframe']||'';
  var rozet=(mode==='AND')?'b-test':'b-rule';
  var ipucu=condText(r['dyn_'+which+'_conditions'], r['dyn_'+which+'_logic']);
  return '<br><span class="badge '+rozet+'" title="'+esc(tf+' | '+ipucu)+'">'
    +(mode==='AND'?'+VE ':'+VEYA ')+esc(tf)+'</span>';
}

/* Fiyat buyuklugune gore ondalik: >=100 -> 2, >=1 -> 4, <1 -> 6 */
function fiyat(v){
  var x=n(v); if(x===null)return '—';
  var a=Math.abs(x);
  var d = a>=100 ? 2 : (a>=1 ? 4 : 6);
  return x.toLocaleString('en-US',{minimumFractionDigits:d,maximumFractionDigits:d});
}

function lvl(type,val){
  if(val==null)return '—';
  return type==='pct'?(fmt(val,2)+'%'):('$'+fiyat(val));
}
function syncLevelUnits(){
  document.getElementById('u-tp').textContent=
    document.getElementById('f-tptype').value==='pct'?'%':'$';
  document.getElementById('u-sl').textContent=
    document.getElementById('f-sltype').value==='pct'?'%':'$';
}

function condRow(c){
  var d=document.createElement('div');
  d.className='crow';
  var t=(c&&c.type)||'ema_cross';
  var opts=Object.keys(CT).map(function(k){
    return '<option value="'+k+'"'+(k===t?' selected':'')+'>'+CT[k].ad+'</option>';
  }).join('');
  var ops=['<','>','<=','>=','='].map(function(o){
    return '<option value="'+o+'"'+((c&&c.op===o)?' selected':'')+'>'+o+'</option>';
  }).join('');
  d.innerHTML='<select class="c-type" onchange="onTypeChange(this)">'+opts+'</select>'
    +'<div class="cunit w1"><input class="c-p1"></div>'
    +'<select class="c-op">'+ops+'</select>'
    +'<div class="cunit"><input class="c-p2"><span class="u u2"></span></div>'
    +'<button class="xbtn" onclick="rmCond(this)" title="Kosulu sil">&times;</button>';
  return d;
}
function fillRow(row,c,useDefaults){
  var t=row.querySelector('.c-type').value, def=CT[t];
  if(!def){ def=CT['ema_cross']; }        // bilinmeyen tip: cokmek yerine varsayilan
  var p1=row.querySelector('.c-p1'), p2=row.querySelector('.c-p2');
  var w1=row.querySelector('.w1');
  var tek = def.p1===null;
  // Degme kosullarinda operator anlamsiz: bar araligi hedefe dokundu mu
  var opSel=row.querySelector('.c-op');
  if(def.deg){ opSel.style.visibility='hidden'; opSel.value='='; }
  else { opSel.style.visibility=''; if(opSel.value==='=')opSel.value=def.op||'<'; }
  row.classList.toggle('single', tek);
  w1.style.display = tek?'none':'';
  row.querySelector('.u2').textContent=def.u2||'';
  p1.placeholder = tek?'':def.p1;
  p2.placeholder = def.p2;
  if(useDefaults){
    p1.value = tek?'':(def.d1===null?'':def.d1);
    p2.value = def.d2===null?'':def.d2;
    row.querySelector('.c-op').value=def.op;
  }else if(c){
    p1.value = (c.p1===null||c.p1===undefined)?'':c.p1;
    p2.value = (c.p2===null||c.p2===undefined)?'':c.p2;
  }
  if(tek)p1.value='';
}
function onTypeChange(sel){ fillRow(sel.parentNode,null,true); }
/* kosul kutulari: giris ('conds'), dinamik TP ('dtp-conds'), dinamik SL ('dsl-conds') */
var LOGIC_OF={'conds':'f-logic','dtp-conds':'dtp-logic','dsl-conds':'dsl-logic',
  'stp-conds':'stp-logic','ssl-conds':'ssl-logic',
  'wtp-conds':'wtp-logic','wsl-conds':'wsl-logic'};

function addCond(boxId,c){
  boxId=boxId||'conds';
  var box=document.getElementById(boxId);
  var row=condRow(c);
  row.dataset.box=boxId;
  box.appendChild(row);
  fillRow(row,c,!c);
  syncLogic(boxId);
}
function rmCond(btn){
  var row=btn.parentNode;
  var boxId=row.dataset.box||'conds';
  var rows=document.querySelectorAll('#'+boxId+' .crow');
  if(rows.length<=1){toast('En az bir kosul gerekli');return;}
  row.remove();
  syncLogic(boxId);
}
function syncLogic(boxId){
  boxId=boxId||'conds';
  var cnt=document.querySelectorAll('#'+boxId+' .crow').length;
  var sel=document.getElementById(LOGIC_OF[boxId]);
  if(!sel)return;
  if(cnt<=1){ sel.value='-'; sel.disabled=true; }
  else{ sel.disabled=false; if(sel.value==='-')sel.value='AND'; }
}
function readConds(boxId){
  var out=[];
  document.querySelectorAll('#'+boxId+' .crow').forEach(function(row){
    out.push({type:row.querySelector('.c-type').value,
              op:row.querySelector('.c-op').value,
              p1:row.querySelector('.c-p1').value,
              p2:row.querySelector('.c-p2').value});
  });
  return out;
}

/* dinamik blok yardimcilari: on = 'dtp' | 'dsl' */
function dynToggle(on){
  var acik=document.getElementById(on+'-active').checked;
  document.getElementById(on+'-body').style.display=acik?'':'none';
  dynModeWarn(on);
}
function dynModeWarn(on){
  var mode=document.getElementById(on+'-mode').value;
  var aktif=document.getElementById(on+'-active').checked;
  var w=document.getElementById(on+'-warn');
  var goster = aktif && mode==='AND';
  w.style.display = goster?'block':'none';
}
function dynFill(on,cfg){
  var act=document.getElementById(on+'-active');
  var boxId=on+'-conds';
  var box=document.getElementById(boxId);
  box.innerHTML='';
  var aktif=!!(cfg&&cfg.active);
  act.checked=aktif;
  var tfd=(cfg&&cfg.timeframe)||'5m';
  if(String(tfd).toLowerCase()==='1d')tfd='1D';
  if(String(tfd).toLowerCase()==='1w')tfd='1W';
  document.getElementById(on+'-tf').value=tfd;
  document.getElementById(on+'-mode').value=(cfg&&cfg.mode)||'OR';
  document.getElementById(on+'-logic').value=(cfg&&cfg.logic)||'AND';
  var cs=cfg&&cfg.conditions;
  if(typeof cs==='string'){try{cs=JSON.parse(cs)}catch(e){cs=null}}
  if(cs&&cs.length){ cs.forEach(function(c){addCond(boxId,c)}); }
  else { addCond(boxId); }
  document.getElementById(on+'-logic').value=(cs&&cs.length>1)?((cfg&&cfg.logic)||'AND'):'-';
  syncLogic(boxId);
  dynToggle(on);
}
function dynRead(on,prefix){
  var o={};
  var aktif=document.getElementById(on+'-active').checked;
  o[prefix+'_active']=aktif;
  if(aktif){
    o[prefix+'_timeframe']=document.getElementById(on+'-tf').value;
    o[prefix+'_mode']=document.getElementById(on+'-mode').value;
    o[prefix+'_logic']=document.getElementById(on+'-logic').value;
    o[prefix+'_conditions']=readConds(on+'-conds');
  }
  return o;
}

/* ---------- form ---------- */
function toggleForm(){
  var f=document.getElementById('rule-form');
  if(f.style.display==='none')openForm(null);else closeForm();
}
function openForm(r){
  editingId=r?r.id:null;
  document.getElementById('rule-form').style.display='';
  document.getElementById('form-toggle').textContent='Kapat';
  document.getElementById('form-title').textContent=r?('Kural duzenle #'+r.id):'Yeni kural';
  document.getElementById('f-errors').style.display='none';
  document.getElementById('conds').innerHTML='';
  if(r){
    document.getElementById('f-coin').value=r.coin||'';
    document.getElementById('f-dir').value=r.direction||'SHORT';
    document.getElementById('f-tf').value=r.timeframe||'5m';
    document.getElementById('f-tptype').value=r.tp_type||'pct';
    document.getElementById('f-tpval').value=r.tp_value==null?'':r.tp_value;
    document.getElementById('f-sltype').value=r.sl_type||'pct';
    document.getElementById('f-slval').value=r.sl_value==null?'':r.sl_value;
    document.getElementById('f-margin').value=r.margin_usdt==null?'':r.margin_usdt;
    document.getElementById('f-lev').value=r.leverage==null?'':r.leverage;
    document.getElementById('f-days').value='';
    document.getElementById('f-note').value=r.note||'';
    var cs=r.conditions;
    if(typeof cs==='string'){try{cs=JSON.parse(cs)}catch(e){cs=[]}}
    (cs||[]).forEach(function(c){addCond('conds',c)});
    if(!cs||!cs.length)addCond('conds');
    document.getElementById('f-logic').value=(cs&&cs.length>1)?(r.logic||'AND'):'-';
    dynFill('dtp',{active:r.dyn_tp_active,timeframe:r.dyn_tp_timeframe,
      mode:r.dyn_tp_mode,logic:r.dyn_tp_logic,conditions:r.dyn_tp_conditions});
    dynFill('dsl',{active:r.dyn_sl_active,timeframe:r.dyn_sl_timeframe,
      mode:r.dyn_sl_mode,logic:r.dyn_sl_logic,conditions:r.dyn_sl_conditions});
  }else{
    ['f-coin','f-note','f-days'].forEach(function(id){document.getElementById(id).value=''});
    document.getElementById('f-dir').value='SHORT';
    document.getElementById('f-tf').value='5m';
    document.getElementById('f-tptype').value='pct';
    document.getElementById('f-tpval').value='10';
    document.getElementById('f-sltype').value='pct';
    document.getElementById('f-slval').value='15';
    document.getElementById('f-margin').value='100';
    document.getElementById('f-lev').value='10';
    document.getElementById('f-days').value='3';
    addCond('conds');
    dynFill('dtp',null);
    dynFill('dsl',null);
  }
  syncLevelUnits();
  syncLogic('conds');
  document.getElementById('rule-form').scrollIntoView({behavior:'smooth',block:'nearest'});
}
function closeForm(){
  document.getElementById('rule-form').style.display='none';
  document.getElementById('form-toggle').textContent='+ Kural ekle';
  document.getElementById('form-title').textContent='Yeni kural';
  editingId=null;
}
function collectForm(){
  var conds=readConds('conds');
  var lg=document.getElementById('f-logic').value;
  return Object.assign({}, dynRead('dtp','dyn_tp'), dynRead('dsl','dyn_sl'), {
    coin:document.getElementById('f-coin').value,
    direction:document.getElementById('f-dir').value,
    timeframe:document.getElementById('f-tf').value,
    logic:lg,
    conditions:conds,
    tp_type:document.getElementById('f-tptype').value,
    tp_value:document.getElementById('f-tpval').value,
    sl_type:document.getElementById('f-sltype').value,
    sl_value:document.getElementById('f-slval').value,
    margin_usdt:document.getElementById('f-margin').value||100,
    leverage:document.getElementById('f-lev').value||10,
    expire_days:document.getElementById('f-days').value,
    note:document.getElementById('f-note').value,
    active:true
  });
}
function showErrors(list){
  var b=document.getElementById('f-errors');
  b.innerHTML=list.map(function(e){return '&bull; '+esc(e)}).join('<br>');
  b.style.display='block';
  b.scrollIntoView({behavior:'smooth',block:'nearest'});
}
function saveRule(){
  var data=collectForm();
  var url=editingId?('/api/rules/'+editingId):'/api/rules';
  var method=editingId?'PATCH':'POST';
  var btn=document.getElementById('f-save');
  btn.disabled=true;btn.textContent='Kaydediliyor...';
  fetch(url,{method:method,headers:{'Content-Type':'application/json'},body:JSON.stringify(data)})
    .then(function(r){return r.json().then(function(j){return {s:r.status,j:j}})})
    .then(function(res){
      btn.disabled=false;btn.textContent='Kaydet';
      if(res.s===200&&res.j.ok){toast(editingId?'Kural guncellendi':'Kural eklendi');closeForm();refresh();}
      else showErrors(res.j.errors||['Kaydedilemedi']);
    }).catch(function(){
      btn.disabled=false;btn.textContent='Kaydet';
      showErrors(['Baglanti hatasi']);
    });
}
function editRule(id){
  var r=(state.rules||[]).filter(function(x){return x.id===id})[0];
  if(r)openForm(r);
}
function toggleRule(id,active){
  fetch('/api/rules/'+id+'/toggle',{method:'PATCH',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({active:active})})
    .then(function(){toast(active?'Kural aktif':'Kural pasif');refresh();});
}
function delRule(id){
  if(!confirm('Kural #'+id+' silinsin mi?'))return;
  fetch('/api/rules/'+id,{method:'DELETE'}).then(function(){toast('Kural silindi');refresh();});
}

/* ---------- kill-switch ---------- */
var LVL_AD={RUN:'Calisiyor', PAUSE:'Duraklatildi', STOP:'DURDURULDU'};

function seviyeAyarla(hedef){
  var su=(state&&state.level)||'RUN';
  if(hedef===su){
    if(!confirm('Bot normal moda dondurulsun mu?'))return;
    hedef='RUN';
  }else if(hedef==='PAUSE'){
    if(!confirm('DURAKLAT\\n\\nYeni pozisyon acilmayacak.\\n'
      +'Acik pozisyonlar izlenmeye ve TP/SL ile kapanmaya DEVAM eder.\\n\\nOnayliyor musun?'))return;
  }else if(hedef==='STOP'){
    if(!confirm('BOT DUR\\n\\nHer sey durur: yumusak TP/SL, dinamik cikis, webhook, kural motoru.\\n'
      +'ACIK POZISYONLAR IZLENMEYECEK.\\n'
      +'Hard TP/SL emirleri borsada kalir (demo ortaminda guvenilmez).\\n\\nOnayliyor musun?'))return;
  }
  fetch('/api/level',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({level:hedef})})
    .then(function(r){return r.json()})
    .then(function(j){ toast(j.ok?('Seviye: '+(LVL_AD[hedef]||hedef)):'Degistirilemedi'); refresh(); })
    .catch(function(){ toast('Baglanti hatasi'); });
}

function acilCikis(){
  var pos=(state&&state.status&&state.status.positions)||[];
  var toplam=0;
  pos.forEach(function(p){ if(p.upnl!=null) toplam+=Number(p.upnl); });
  if(!pos.length){
    if(!confirm('Acik pozisyon yok.\\nYine de botu DURDURMAK istiyor musun?'))return;
    seviyeAyarla('STOP');
    return;
  }
  var liste=pos.map(function(p){return '  - '+p.coin+' '+p.side+' ('+sgn(p.upnl)+')'}).join('\\n');
  if(!confirm('ACIL CIKIS\\n\\n'+pos.length+' pozisyon MARKET emriyle kapatilacak:\\n'
    +liste+'\\n\\nToplam PnL: '+sgn(toplam)+'\\n\\nBu islem GERI ALINAMAZ. Devam?'))return;
  if(!confirm('SON ONAY\\n\\n'+pos.length+' pozisyon simdi kapatilacak ve bot durdurulacak.\\n'
    +'Emin misin?'))return;
  fetch('/api/emergency',{method:'POST'})
    .then(function(r){return r.json()})
    .then(function(j){ toast(j.ok?'Acil cikis gonderildi':'Gonderilemedi'); refresh(); })
    .catch(function(){ toast('Baglanti hatasi'); });
}

/* ---------- yenileme ---------- */
function elleYenile(btn){
  btn.disabled=true;
  btn.classList.add('doner');
  refresh(function(){
    setTimeout(function(){ btn.disabled=false; btn.classList.remove('doner'); }, 350);
  });
}

function refresh(bitince){
  fetch('/api/state',{cache:'no-store'}).then(function(r){return r.json()}).then(function(d){
    state=d;render();
    if(bitince)bitince();
  }).catch(function(){
    var h=document.getElementById('health');
    h.textContent='Panel hatasi';h.className='badge b-off';
    if(bitince)bitince();
  });
}
Object.keys(S_MAP).forEach(function(id){
  var el=document.getElementById(id);
  if(el)el.addEventListener('input',function(){settingsDirty=true;});
});
['stp','ssl','wtp','wsl'].forEach(function(on){
  ['-active','-tf','-mode','-logic'].forEach(function(sfx){
    var el=document.getElementById(on+sfx);
    if(el)el.addEventListener('change',function(){settingsDirty=true;});
  });
  var box=document.getElementById(on+'-conds');
  if(box)box.addEventListener('input',function(){settingsDirty=true;});
});

refresh();
setInterval(refresh,10000);
</script>
</body>
</html>
"""



# ======================================================================
# HTTP SERVER
# ======================================================================

class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json", extra=None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _authed(self):
        """Basic Auth - PANEL_USER/PANEL_PASS doluysa zorunlu."""
        if not AUTH_ON:
            return True
        hdr = self.headers.get("Authorization", "")
        if hdr.startswith("Basic "):
            import base64
            try:
                user, _, pw = base64.b64decode(hdr[6:]).decode("utf-8").partition(":")
                if user == PANEL_USER and pw == PANEL_PASS:
                    return True
            except Exception:
                pass
        self._send(401, json.dumps({"error": "auth"}), extra={
            "WWW-Authenticate": 'Basic realm="STS Panel"'})
        return False

    def do_GET(self):
        if not self._authed():
            return
        if self.path == "/" or self.path.startswith("/index"):
            self._send(200, HTML, "text/html")
        elif self.path == "/api/state":
            try:
                self._send(200, json.dumps(gather_state()))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
        elif self.path == "/health":
            self._send(200, json.dumps({"ok": True, "version": VERSION}))
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > 100000:
                return None
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return None

    @staticmethod
    def _rule_id(path, prefix):
        try:
            rid = int(path[len(prefix):].strip("/"))
            return rid if rid > 0 else None
        except Exception:
            return None

    def _webhook(self):
        """TradingView webhook ucu. Basic Auth MUAF (TV sifre gonderemez),
        token ile korunur. Kuyruga yazar; emri executor acar."""
        d = self._body()
        gelen_token = ""
        if isinstance(d, dict):
            gelen_token = str(d.get("token") or "")
        if not gelen_token:
            gelen_token = self.headers.get("X-Webhook-Token", "") or ""

        if not WEBHOOK_TOKEN:
            log("Webhook geldi ama WEBHOOK_TOKEN tanimli degil - reddedildi", "WARN")
            sb_post("sts_events", {"kind": "WEBHOOK_REJECT", "coin": None,
                                   "detail": "WEBHOOK_TOKEN tanimli degil"})
            self._send(503, json.dumps({"ok": False, "error": "webhook kapali"}))
            return

        if gelen_token != WEBHOOK_TOKEN:
            kaynak = self.headers.get("X-Forwarded-For") or self.client_address[0]
            log(f"Webhook token HATALI (kaynak {kaynak}) - reddedildi", "WARN")
            sb_post("sts_events", {"kind": "WEBHOOK_REJECT", "coin": None,
                                   "detail": f"hatali token, kaynak {kaynak}"})
            self._send(401, json.dumps({"ok": False, "error": "token"}))
            return

        if d is None:
            self._send(400, json.dumps({"ok": False, "errors": ["payload okunamadi"]}))
            return

        clean, errs = validate_webhook(d)
        if errs:
            log(f"Webhook gecersiz: {errs}", "WARN")
            sb_post("sts_events", {"kind": "WEBHOOK_REJECT",
                                   "coin": str(d.get("coin") or "")[:20] or None,
                                   "detail": "; ".join(errs)[:400]})
            self._send(400, json.dumps({"ok": False, "errors": errs}))
            return

        govde = {k: v for k, v in clean.items() if k not in ("coin", "direction")}
        res = sb_post("sts_webhooks", {
            "coin": clean["coin"], "direction": clean["direction"],
            "payload": govde or None,
        })
        if res is None:
            self._send(500, json.dumps({"ok": False, "error": "kuyruga yazilamadi"}))
            return
        wid = res[0].get("id") if res else None
        log(f"Webhook alindi: {clean['coin']} {clean['direction']} (kuyruk #{wid})")
        self._send(200, json.dumps({"ok": True, "id": wid,
                                    "coin": clean["coin"], "direction": clean["direction"]}))

    def do_POST(self):
        # webhook auth'tan MUAF - token kendi icinde
        if self.path == "/webhook":
            self._webhook()
            return
        if not self._authed():
            return
        if self.path == "/api/stop":
            ok = set_killswitch(True)
            log("Kill-switch AKTIF (panel)")
            self._send(200 if ok else 500, json.dumps({"ok": ok, "killswitch": True}))
        elif self.path == "/api/resume":
            ok = set_killswitch(False)
            log("Kill-switch KALDIRILDI (panel)")
            self._send(200 if ok else 500, json.dumps({"ok": ok, "killswitch": False}))
        elif self.path == "/api/level":
            d = self._body() or {}
            seviye = (d.get("level") or "").upper()
            if seviye not in LEVELS:
                self._send(400, json.dumps({"ok": False,
                           "errors": [f"seviye RUN/PAUSE/STOP olmali"]}))
                return
            ok = set_level(seviye, d.get("note"))
            log(f"Seviye istegi: {seviye}")
            self._send(200 if ok else 500, json.dumps({"ok": ok, "level": seviye}))
            return
        elif self.path == "/api/emergency":
            ok = request_emergency()
            log("ACIL CIKIS istegi gonderildi", "WARN")
            self._send(200 if ok else 500, json.dumps({"ok": ok}))
            return
        elif self.path == "/api/rules/import":
            # JSON yapistirma: tek kural veya kural listesi
            d = self._body()
            if d is None:
                self._send(400, json.dumps({"ok": False, "errors": ["JSON okunamadi"]}))
                return
            items = d if isinstance(d, list) else [d]
            if not items:
                self._send(400, json.dumps({"ok": False, "errors": ["Bos JSON"]}))
                return
            if len(items) > 20:
                self._send(400, json.dumps({"ok": False, "errors": ["En fazla 20 kural"]}))
                return
            temiz, hatalar = [], []
            for i, it in enumerate(items, 1):
                if not isinstance(it, dict):
                    hatalar.append(f"{i}. kayit nesne degil")
                    continue
                c, e = validate_rule(it)
                if e:
                    on = f"{i}. kural: " if len(items) > 1 else ""
                    hatalar += [on + x for x in e]
                else:
                    temiz.append(c)
            if hatalar:
                self._send(400, json.dumps({"ok": False, "errors": hatalar}))
                return
            res = sb_post("sts_rules", temiz)
            if res is None:
                self._send(500, json.dumps({"ok": False, "errors": ["kayit basarisiz"]}))
                return
            log(f"JSON ile {len(temiz)} kural eklendi")
            self._send(200, json.dumps({"ok": True, "count": len(temiz)}))
            return
        elif self.path == "/api/rules":
            d = self._body()
            if d is None:
                self._send(400, json.dumps({"ok": False, "errors": ["gecersiz istek"]}))
                return
            clean, errs = validate_rule(d)
            if errs:
                self._send(400, json.dumps({"ok": False, "errors": errs}))
                return
            res = sb_post("sts_rules", clean)
            if res is None:
                self._send(500, json.dumps({"ok": False, "errors": ["kayit basarisiz"]}))
                return
            log(f"Kural eklendi: {clean['coin']} {clean['direction']} {clean['timeframe']}")
            self._send(200, json.dumps({"ok": True, "rule": res[0] if res else None}))
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_PATCH(self):
        if not self._authed():
            return
        # /api/settings -> strateji ayarlari
        if self.path == "/api/settings":
            d = self._body()
            if d is None:
                self._send(400, json.dumps({"ok": False, "errors": ["gecersiz istek"]}))
                return
            clean, errs = validate_settings(d)
            if errs:
                self._send(400, json.dumps({"ok": False, "errors": errs}))
                return
            clean["updated_at"] = datetime.now(timezone.utc).isoformat()
            ok = sb_patch("sts_settings?id=eq.1", clean)
            if ok:
                log("Ayarlar guncellendi: " + ", ".join(f"{k}={v}" for k, v in clean.items()
                                                        if k != "updated_at"))
            self._send(200 if ok else 500, json.dumps({"ok": ok}))
            return
        # /api/positions/{trade_id} -> acik pozisyon yonetimi
        if self.path.startswith("/api/positions/"):
            tid = self._rule_id(self.path, "/api/positions/")
            if not tid:
                self._send(400, json.dumps({"ok": False, "errors": ["gecersiz id"]}))
                return
            d = self._body()
            if d is None:
                self._send(400, json.dumps({"ok": False, "errors": ["gecersiz istek"]}))
                return
            alanlar, errs = validate_position_req(d)
            if errs:
                self._send(400, json.dumps({"ok": False, "errors": errs}))
                return
            ok = sb_patch(f"bot_trades?id=eq.{tid}&closed_at=is.null", alanlar)
            if ok:
                log(f"Pozisyon #{tid} istegi: " +
                    ", ".join(k for k in alanlar if k != "req_at"))
            self._send(200 if ok else 500, json.dumps({"ok": ok}))
            return

        # /api/rules/{id}/toggle  -> aktif/pasif
        if self.path.startswith("/api/rules/") and self.path.endswith("/toggle"):
            rid = self._rule_id(self.path[:-len("/toggle")], "/api/rules/")
            if not rid:
                self._send(400, json.dumps({"ok": False, "errors": ["gecersiz id"]}))
                return
            d = self._body() or {}
            active = bool(d.get("active"))
            body = {"active": active}
            if active:
                body["triggered_at"] = None   # yeniden aktif edilirse tetik sifirlanir
            ok = sb_patch(f"sts_rules?id=eq.{rid}", body)
            log(f"Kural {rid} -> {'aktif' if active else 'pasif'}")
            self._send(200 if ok else 500, json.dumps({"ok": ok}))
            return
        # /api/rules/{id} -> tam guncelleme
        if self.path.startswith("/api/rules/"):
            rid = self._rule_id(self.path, "/api/rules/")
            if not rid:
                self._send(400, json.dumps({"ok": False, "errors": ["gecersiz id"]}))
                return
            d = self._body()
            if d is None:
                self._send(400, json.dumps({"ok": False, "errors": ["gecersiz istek"]}))
                return
            clean, errs = validate_rule(d)
            if errs:
                self._send(400, json.dumps({"ok": False, "errors": errs}))
                return
            ok = sb_patch(f"sts_rules?id=eq.{rid}", clean)
            log(f"Kural {rid} guncellendi: {clean['coin']} {clean['direction']}")
            self._send(200 if ok else 500, json.dumps({"ok": ok}))
            return
        self._send(404, json.dumps({"error": "not found"}))

    def do_DELETE(self):
        if not self._authed():
            return
        if self.path.startswith("/api/rules/"):
            rid = self._rule_id(self.path, "/api/rules/")
            if not rid:
                self._send(400, json.dumps({"ok": False, "errors": ["gecersiz id"]}))
                return
            ok = sb_delete(f"sts_rules?id=eq.{rid}")
            log(f"Kural {rid} silindi")
            self._send(200 if ok else 500, json.dumps({"ok": ok}))
            return
        self._send(404, json.dumps({"error": "not found"}))

    def log_message(self, fmt, *args):
        pass  # erisim loglarini sustur


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def main():
    if not (SUPABASE_URL and SUPABASE_KEY):
        sys.stderr.write("HATA: SUPABASE_URL / SUPABASE_KEY tanimli degil.\n")
        sys.exit(1)
    srv = ThreadingHTTPServer((PANEL_BIND, PANEL_PORT), Handler)
    log(f"STS PANEL {VERSION} dinliyor: http://{PANEL_BIND}:{PANEL_PORT} | "
        f"auth={'acik' if AUTH_ON else 'KAPALI'} | "
        f"webhook={'acik' if WEBHOOK_TOKEN else 'KAPALI (WEBHOOK_TOKEN yok)'}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    log("Panel durduruldu")


if __name__ == "__main__":
    main()
