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

VERSION = "panel-v1"

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


def set_killswitch(value):
    return sb_patch("sts_control?id=eq.1", {
        "killswitch": bool(value),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })


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

COND_TYPES = {"ema_cross", "rsi", "price", "oi_change", "volume", "funding"}
OPS = {"<", ">", "<=", ">="}
TIMEFRAMES = {"5m", "15m", "30m", "1h", "4h", "1d"}
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

    tf = (d.get(f"{prefix}_timeframe") or "5m").lower()
    if tf not in TIMEFRAMES:
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

    tf = (d.get("timeframe") or "5m").lower()
    if tf not in TIMEFRAMES:
        err.append(f"periyot gecersiz (izinli: {', '.join(sorted(TIMEFRAMES))})")

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
    killswitch = bool(ctrl[0].get("killswitch")) if ctrl else False

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
        "settings": settings,
        "webhooks": webhooks,
        "webhook_enabled": bool(WEBHOOK_TOKEN),
        "webhook_token": WEBHOOK_TOKEN,   # hazir mesaj uretmek icin (panel auth'lu)
        "trades": trades,
        "events": events,
        "rules": rules,
        "server_time": datetime.now(timezone.utc).isoformat(),
    }


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

    yon = str(d.get("direction") or d.get("side") or "").strip().upper()
    if yon in ("SELL", "SHORT"):
        yon = "SHORT"
    elif yon in ("BUY", "LONG"):
        yon = "LONG"
    else:
        err.append("direction SHORT/LONG (veya BUY/SELL) olmali")

    clean = {"coin": coin, "direction": yon}

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
<meta name="theme-color" content="#F5F2EA">
<title>STS Panel</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Archivo:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#F5F2EA; --surface:#FCFAF6; --surface2:#EDE8DC; --border:#DCD5C6;
  --text:#1A1815; --text2:#6B655C; --text3:#9B948A;
  --green:#2D6A4F; --greenBg:#E2EDE6; --greenBd:#BBD4C4;
  --coral:#C9553B; --coralBg:#F7E4DE; --coralBd:#E8C0B4;
  --purple:#6B4E9B; --purpleBg:#EBE4F5;
  --amber:#8A6410; --amberBg:#F5EBD3;
  --shadow:0 1px 2px rgba(26,24,21,.05);
}
[data-theme="dark"]{
  --bg:#15140F; --surface:#1E1C16; --surface2:#272419; --border:#3B372C;
  --text:#F2EDE2; --text2:#A69F91; --text3:#746D60;
  --green:#74C494; --greenBg:#1A3226; --greenBd:#2A4C38;
  --coral:#E8836A; --coralBg:#3A211A; --coralBd:#5A342A;
  --purple:#B79BE8; --purpleBg:#2A2140;
  --amber:#DCA83A; --amberBg:#33280F;
  --shadow:none;
}
*{box-sizing:border-box;margin:0;padding:0}
html{-webkit-text-size-adjust:100%}
body{
  font-family:'Archivo',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  background:var(--bg); color:var(--text);
  padding:14px; max-width:1160px; margin:0 auto;
  transition:background .2s,color .2s;
}
.mono{font-family:'JetBrains Mono',ui-monospace,'SF Mono',Consolas,monospace}

/* ---------- header ---------- */
.hdr{display:flex;align-items:center;justify-content:space-between;gap:10px;
  padding:14px 18px;background:var(--surface);border:1px solid var(--border);
  border-radius:14px;margin-bottom:12px;flex-wrap:wrap;box-shadow:var(--shadow)}
.brand{font-size:22px;font-weight:800;letter-spacing:-.02em}
.hdr-l{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.hdr-r{display:flex;align-items:center;gap:8px}

.badge{font-size:10px;font-weight:700;padding:3px 9px;border-radius:20px;
  letter-spacing:.06em;text-transform:uppercase;border:1px solid transparent;white-space:nowrap}
.b-test{background:var(--amberBg);color:var(--amber);border-color:var(--amber)}
.b-live{background:var(--coralBg);color:var(--coral);border-color:var(--coral)}
.b-ok{background:var(--greenBg);color:var(--green);border-color:var(--greenBd)}
.b-off{background:var(--coralBg);color:var(--coral);border-color:var(--coralBd)}
.b-short{background:var(--coralBg);color:var(--coral)}
.b-long{background:var(--greenBg);color:var(--green)}
.b-sig{background:var(--surface2);color:var(--text2)}
.b-rule{background:var(--purpleBg);color:var(--purple)}

.btn{font-family:inherit;font-size:12px;font-weight:600;padding:8px 14px;
  border-radius:9px;border:1px solid var(--border);background:var(--surface);
  color:var(--text);cursor:pointer;transition:.15s;white-space:nowrap}
.btn:hover{background:var(--surface2)}
.btn-stop{border-color:var(--coralBd);background:var(--coralBg);color:var(--coral)}
.btn-go{border-color:var(--greenBd);background:var(--greenBg);color:var(--green)}
.icon-btn{width:36px;height:36px;padding:0;display:grid;place-items:center;font-size:15px}

/* ---------- tabs ---------- */
.tabs{display:flex;gap:4px;background:var(--surface2);padding:5px;
  border-radius:11px;margin-bottom:16px;overflow-x:auto;-webkit-overflow-scrolling:touch}
.tab{font-family:inherit;font-size:11px;font-weight:700;letter-spacing:.07em;
  text-transform:uppercase;padding:9px 16px;border-radius:7px;color:var(--text2);
  cursor:pointer;border:1px solid transparent;background:transparent;
  transition:.15s;white-space:nowrap}
.tab:hover{color:var(--text)}
.tab.on{background:var(--surface);color:var(--text);border-color:var(--border);box-shadow:var(--shadow)}

/* ---------- section labels ---------- */
.sect{font-size:10px;font-weight:700;color:var(--text3);letter-spacing:.1em;
  text-transform:uppercase;margin:0 0 9px 2px}

/* ---------- metrics ---------- */
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(158px,1fr));
  gap:10px;margin-bottom:18px}
.met{background:var(--surface);border:1px solid var(--border);border-radius:13px;
  padding:14px 16px;box-shadow:var(--shadow)}
.met .l{font-size:11px;font-weight:500;color:var(--text2);margin-bottom:5px;letter-spacing:.01em}
.met .v{font-size:26px;font-weight:800;letter-spacing:-.03em;line-height:1.05}
.met .s{font-size:11px;color:var(--text3);margin-top:4px}
.up{color:var(--green)}.dn{color:var(--coral)}.mut{color:var(--text2)}

/* ---------- cards ---------- */
.card{background:var(--surface);border:1px solid var(--border);border-radius:13px;
  padding:14px 16px;margin-bottom:12px;box-shadow:var(--shadow)}

/* ---------- positions ---------- */
.pos{display:flex;align-items:center;justify-content:space-between;gap:10px;
  padding:13px 15px;background:var(--surface);border:1px solid var(--border);
  border-radius:12px;margin-bottom:8px;flex-wrap:wrap;box-shadow:var(--shadow)}
.pos-l{min-width:0;flex:1}
.pos-nm{display:flex;align-items:center;gap:7px;margin-bottom:5px;flex-wrap:wrap}
.pos-nm b{font-size:16px;font-weight:800;letter-spacing:-.02em}
.pos-dt{font-size:11px;color:var(--text2)}
.pos-dt span{color:var(--text3)}
.pos-r{text-align:right}
.pnl{font-size:19px;font-weight:800;letter-spacing:-.02em;line-height:1.1}
.pnl-pct{font-size:12px;font-weight:600;margin-top:1px}

/* ---------- tables ---------- */
.tw{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{width:100%;border-collapse:collapse;font-size:12px}
th{text-align:left;font-size:10px;font-weight:700;color:var(--text3);
  letter-spacing:.07em;text-transform:uppercase;padding:8px 10px;
  border-bottom:1px solid var(--border);white-space:nowrap}
td{padding:10px;border-bottom:1px solid var(--border);color:var(--text);white-space:nowrap}
tbody tr:last-child td{border-bottom:none}
tbody tr:hover{background:var(--surface2)}
.empty{color:var(--text3);font-size:12px;padding:26px;text-align:center}

/* ---------- form ---------- */
label{font-size:11px;font-weight:500;color:var(--text2);display:block;margin-bottom:5px}
input,select{width:100%;font-family:inherit;font-size:13px;background:var(--bg);
  border:1px solid var(--border);border-radius:8px;color:var(--text);
  padding:9px 10px;transition:.15s}
input:focus,select:focus{outline:none;border-color:var(--text2)}
input:disabled{background:var(--surface2);color:var(--text3);cursor:not-allowed}
input::placeholder{color:var(--text3)}
select{cursor:pointer;-webkit-appearance:none;appearance:none;
  background-image:linear-gradient(45deg,transparent 50%,var(--text2) 50%),
  linear-gradient(135deg,var(--text2) 50%,transparent 50%);
  background-position:calc(100% - 15px) 50%,calc(100% - 10px) 50%;
  background-size:5px 5px,5px 5px;background-repeat:no-repeat;padding-right:28px}
.frow{display:grid;grid-template-columns:repeat(auto-fit,minmax(135px,1fr));
  gap:11px;margin-bottom:11px}
.crow{display:grid;grid-template-columns:150px 82px 62px 1fr 34px;gap:7px;
  align-items:center;margin-bottom:7px}
.crow.single{grid-template-columns:150px 62px 1fr 34px}
.cunit{position:relative;display:flex;align-items:center}
.cunit input{padding-right:30px}
.cunit .u{position:absolute;right:10px;font-size:11px;font-weight:600;
  color:var(--text3);pointer-events:none}
.xbtn{font-family:inherit;background:var(--coralBg);border:1px solid var(--coralBd);
  color:var(--coral);border-radius:8px;cursor:pointer;height:36px;font-size:14px;
  font-weight:700;display:grid;place-items:center}
.mini{font-family:inherit;background:transparent;border:1px solid var(--border);
  color:var(--text2);border-radius:7px;cursor:pointer;padding:5px 10px;
  font-size:11px;font-weight:600;margin-left:5px;transition:.15s}
.mini:hover{border-color:var(--text2);color:var(--text)}
.mini.del:hover{border-color:var(--coralBd);color:var(--coral);background:var(--coralBg)}
.divider{border-top:1px solid var(--border);padding-top:12px;margin-top:12px}
.cprow{display:flex;gap:8px;align-items:stretch;margin-top:4px}
.cprow input,.cprow textarea{flex:1;font-family:'JetBrains Mono',monospace;
  font-size:11px;line-height:1.5;resize:vertical}
.cprow .btn{white-space:nowrap;align-self:flex-start}
.dynbox{background:var(--bg);border:1px solid var(--border);border-radius:11px;
  padding:12px 14px;margin-bottom:10px}
.dynhead{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap}
.dynhint{font-size:11px;color:var(--text3)}
.chk{display:flex;align-items:center;gap:8px;cursor:pointer;margin:0}
.chk input{width:16px;height:16px;accent-color:var(--green);cursor:pointer}
.chk span{font-size:13px;font-weight:600;color:var(--text)}
.warnbox{background:var(--amberBg);border:1px solid var(--amber);border-radius:9px;
  padding:10px 12px;margin-top:10px;font-size:11px;color:var(--amber);line-height:1.5}
.errbox{display:none;background:var(--coralBg);border:1px solid var(--coralBd);
  border-radius:9px;padding:11px 13px;margin-top:12px;font-size:12px;color:var(--coral)}
#toast{position:fixed;bottom:18px;right:18px;background:var(--text);color:var(--bg);
  border-radius:10px;padding:12px 18px;font-size:12px;font-weight:600;
  display:none;z-index:99;box-shadow:0 4px 12px rgba(0,0,0,.15)}

@media(max-width:720px){
  body{padding:10px}
  .brand{font-size:19px}
  .met .v{font-size:22px}
  .crow,.crow.single{grid-template-columns:1fr 1fr;gap:6px}
  .crow>*:first-child{grid-column:1/-1}
  .crow .xbtn{grid-column:1/-1}
  th,td{padding:8px 7px;font-size:11px}
  .pnl{font-size:16px}
}
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
    <span id="ks-state" class="badge b-off" style="display:none">Kill-switch</span>
    <button class="btn icon-btn" onclick="toggleTheme()" id="theme-btn" title="Tema">&#9789;</button>
    <button id="ks-btn" class="btn btn-stop" onclick="toggleKs()">Durdur</button>
  </div>
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
          <option value="1h">1h</option><option value="4h">4h</option><option value="1d">1d</option>
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
          <span style="font-size:11px;color:var(--text3)">OI ve Hacim: son bar, onceki N barin ortalamasina gore karsilastirilir. Yonu operator belirler, eksi isareti gerekmez.</span>
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
              <option value="1h">1h</option><option value="4h">4h</option><option value="1d">1d</option>
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
              <option value="1h">1h</option><option value="4h">4h</option><option value="1d">1d</option>
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
              <option value="1h">1h</option><option value="4h">4h</option><option value="1d">1d</option>
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
              <option value="1h">1h</option><option value="4h">4h</option><option value="1d">1d</option>
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
              <option value="1h">1h</option><option value="4h">4h</option><option value="1d">1d</option>
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
              <option value="1h">1h</option><option value="4h">4h</option><option value="1d">1d</option>
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

      <label style="margin-top:12px">Alarm mesaji &#8212; SHORT</label>
      <div class="cprow">
        <textarea id="wh-msg-short" rows="2" readonly class="mono"></textarea>
        <button class="btn" onclick="kopyala('wh-msg-short',this)">Kopyala</button>
      </div>

      <label style="margin-top:10px">Alarm mesaji &#8212; LONG</label>
      <div class="cprow">
        <textarea id="wh-msg-long" rows="2" readonly class="mono"></textarea>
        <button class="btn" onclick="kopyala('wh-msg-long',this)">Kopyala</button>
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
  if(m) m.setAttribute('content', t==='dark' ? '#15140F' : '#F5F2EA');
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
function esc(s){var d=document.createElement('div');d.textContent=s==null?'':String(s);return d.innerHTML}
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

  var ks=state.killswitch;
  var kss=document.getElementById('ks-state');
  kss.style.display=ks?'':'none';
  kss.textContent='Kill-switch aktif';
  var kb=document.getElementById('ks-btn');
  kb.textContent=ks?'Devam et':'Durdur';
  kb.className=ks?'btn btn-go':'btn btn-stop';

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
  if(!pos.length){box.innerHTML='<div class="empty">Acik pozisyon yok</div>';}
  else{
    box.innerHTML=pos.map(function(p){
      var u=n(p.upnl), m=n(p.margin);
      var pct=(u!==null&&m)?u/m*100:null;
      return '<div class="pos"><div class="pos-l">'
        +'<div class="pos-nm"><b>'+esc(p.coin)+'</b>'
        +'<span class="badge '+(p.side==='LONG'?'b-long':'b-short')+'">'+esc(p.side||'')+'</span>'
        +'<span class="badge '+(p.source==='rule'?'b-rule':'b-sig')+'">'+(p.source==='rule'?'Kural':'Sinyal')+'</span></div>'
        +'<div class="pos-dt mono"><span>Giris</span> '+fmt(p.entry,6)
        +' &nbsp;<span>Mark</span> '+fmt(p.mark,6)
        +' &nbsp;<span>TP</span> '+fmt(p.tp,6)
        +' &nbsp;<span>SL</span> '+fmt(p.sl,6)
        +' &nbsp;<span>'+(p.leverage||'—')+'x</span>'
        +(m?' &nbsp;<span>'+usd(m,0)+'</span>':'')
        +'</div></div>'
        +'<div class="pos-r"><div class="pnl '+cls(u)+'">'+sgn(u)+'</div>'
        +(pct!==null?'<div class="pnl-pct '+cls(pct)+'">'+pctTxt(pct)+'</div>':'')
        +'</div></div>';
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
      +'<td class="mono">'+fmt(t.entry_price,6)+'</td>'
      +'<td class="mono">'+(t.closed_at?fmt(t.exit_price,6):'<span class="mut">Acik</span>')+'</td>'
      +'<td class="mono '+cls(p)+'"><b>'+(p===null?'—':sgn(p))+'</b>'
        +(pct!==null?'<br><span style="font-size:10px">'+pctTxt(pct)+'</span>':'')+'</td>'
      +'<td>'+esc(t.exit_reason||'—')+'</td>'
      +'<td class="mut mono">'+esc((t.opened_at||'').replace('T',' ').slice(5,16))+'</td></tr>';
  }).join(''):'<tr><td colspan="8" class="empty">Islem yok</td></tr>';

  var wb=document.querySelector('#webhooks tbody');
  var whs=state.webhooks||[];
  wb.innerHTML=whs.length?whs.map(function(w){
    var d;
    if(!w.executed){d='<span class="badge b-test">Bekliyor</span>';}
    else if((w.result||'').indexOf('OPENED')===0){d='<span class="badge b-ok">Acildi</span>';}
    else if((w.result||'').indexOf('SKIPPED')===0){d='<span class="badge b-sig">Atlandi</span>';}
    else {d='<span class="badge b-off">Hata</span>';}
    return '<tr><td class="mut">'+w.id+'</td>'
      +'<td class="mut mono">'+esc((w.created_at||'').replace('T',' ').slice(5,19))+'</td>'
      +'<td><b>'+esc(w.coin)+'</b></td>'
      +'<td><span class="badge '+(w.direction==='LONG'?'b-long':'b-short')+'">'+esc(w.direction)+'</span></td>'
      +'<td>'+d+'</td>'
      +'<td style="white-space:normal">'+esc(w.result||'—')+'</td></tr>';
  }).join(''):'<tr><td colspan="6" class="empty">Webhook yok</td></tr>';

  var eb=document.querySelector('#events tbody');
  var evs=state.events||[];
  eb.innerHTML=evs.length?evs.map(function(e){
    return '<tr><td class="mut mono">'+esc((e.ts||'').replace('T',' ').slice(5,19))+'</td>'
      +'<td>'+esc(e.kind)+'</td><td><b>'+esc(e.coin||'—')+'</b></td>'
      +'<td style="white-space:normal">'+esc(e.detail||'')+'</td></tr>';
  }).join(''):'<tr><td colspan="4" class="empty">Olay yok</td></tr>';

  var rb=document.querySelector('#rules tbody');
  var rules=state.rules||[];
  rb.innerHTML=rules.length?rules.map(function(r){
    var stt=r.active?'<span class="badge b-ok">Aktif</span>'
      :(r.triggered_at?'<span class="badge b-rule">Tetiklendi</span>':'<span class="badge b-off">Pasif</span>');
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
    var v=st[S_MAP[id]];
    el.value=(v===null||v===undefined)?'':v;
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

/* ---------- kosul tanimlari ---------- */
var CT={
  ema_cross:{ad:'EMA kesisimi', p1:'Hizli', p2:'Yavas',  u2:'',  d1:7,   d2:30,    op:'<'},
  rsi:      {ad:'RSI',          p1:null,    p2:'Esik',   u2:'',  d1:null,d2:70,    op:'>'},
  price:    {ad:'Fiyat',        p1:null,    p2:'Fiyat',  u2:'$', d1:null,d2:'',    op:'<'},
  oi_change:{ad:'OI degisimi',  p1:'Onceki bar', p2:'Fark', u2:'%', d1:3, d2:5,   op:'>'},
  volume:   {ad:'Hacim',        p1:'Onceki bar', p2:'Fark', u2:'%', d1:3, d2:5,   op:'>'},
  funding:  {ad:'Funding',      p1:null,    p2:'Oran',   u2:'%', d1:null,d2:-0.05, op:'<'}
};

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
  var cls=(mode==='AND')?'b-test':'b-rule';
  var ipucu=condText(r['dyn_'+which+'_conditions'], r['dyn_'+which+'_logic']);
  return '<br><span class="badge '+cls+'" title="'+esc(tf+' | '+ipucu)+'">'
    +(mode==='AND'?'+VE ':'+VEYA ')+esc(tf)+'</span>';
}

function lvl(type,val){
  if(val==null)return '—';
  return type==='pct'?(fmt(val,2)+'%'):('$'+fmt(val,8));
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
  var ops=['<','>','<=','>='].map(function(o){
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
  var p1=row.querySelector('.c-p1'), p2=row.querySelector('.c-p2');
  var w1=row.querySelector('.w1');
  var tek = def.p1===null;
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
  document.getElementById(on+'-tf').value=(cfg&&cfg.timeframe)||'5m';
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
function toggleKs(){
  var ks=state&&state.killswitch;
  var action=ks?'/api/resume':'/api/stop';
  var msg=ks?'Bot devam etsin mi?':'Yeni pozisyon acma DURDURULSUN mu?\\n(Acik pozisyonlar izlenmeye devam eder)';
  if(!confirm(msg))return;
  fetch(action,{method:'POST'}).then(function(){toast(ks?'Devam ediliyor':'Durduruldu');refresh();});
}

/* ---------- yenileme ---------- */
function refresh(){
  fetch('/api/state').then(function(r){return r.json()}).then(function(d){
    state=d;render();
  }).catch(function(){
    var h=document.getElementById('health');
    h.textContent='Panel hatasi';h.className='badge b-off';
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
