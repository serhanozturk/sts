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
NEEDS_P1 = {"ema_cross", "rsi", "oi_change", "volume"}


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
    if logic not in ("AND", "OR"):
        err.append("mantik AND veya OR olmali")

    raw_conds = d.get("conditions") or []
    if isinstance(raw_conds, str):
        try:
            raw_conds = json.loads(raw_conds)
        except Exception:
            err.append("kosullar okunamadi")
            raw_conds = []
    if not isinstance(raw_conds, list) or not raw_conds:
        err.append("en az bir kosul gerekli")
        raw_conds = []
    if len(raw_conds) > 8:
        err.append("en fazla 8 kosul")

    conds = []
    for i, c in enumerate(raw_conds, 1):
        t = (c.get("type") or "").lower()
        op = (c.get("op") or "").strip()
        p1 = _num(c.get("p1"))
        p2 = _num(c.get("p2"))
        if t not in COND_TYPES:
            err.append(f"kosul {i}: tip gecersiz")
            continue
        if op not in OPS:
            err.append(f"kosul {i}: operator gecersiz")
            continue
        if p2 is None:
            err.append(f"kosul {i}: deger bos")
            continue
        if t in NEEDS_P1:
            if p1 is None or p1 <= 0:
                err.append(f"kosul {i}: periyot/mum sayisi pozitif olmali")
                continue
            if p1 > 500:
                err.append(f"kosul {i}: periyot 500'den kucuk olmali")
                continue
        if t == "ema_cross" and (p2 is None or p2 <= 0 or p2 > 500):
            err.append(f"kosul {i}: yavas periyot 1-500 arasi olmali")
            continue
        if t == "rsi" and not (0 <= p2 <= 100):
            err.append(f"kosul {i}: rsi esigi 0-100 arasi olmali")
            continue
        conds.append({"type": t, "op": op,
                      "p1": p1 if t in NEEDS_P1 else None, "p2": p2})

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

    if err:
        return None, err

    return {
        "coin": coin, "direction": direction, "timeframe": tf,
        "conditions": conds, "logic": logic,
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

    trades = sb_get("bot_trades?order=id.desc&limit=100") or []
    events = sb_get("sts_events?order=id.desc&limit=100") or []
    rules  = sb_get("sts_rules?order=id.desc&limit=50") or []

    return {
        "status": status,
        "status_age": round(status_age) if status_age is not None else None,
        "killswitch": killswitch,
        "trades": trades,
        "events": events,
        "rules": rules,
        "server_time": datetime.now(timezone.utc).isoformat(),
    }


# ======================================================================
# HTML
# ======================================================================

HTML = """<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>STS Panel</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#0f0f11;color:#e2e0d8;padding:14px;max-width:1100px;margin:0 auto}
.hdr{display:flex;align-items:center;justify-content:space-between;padding:12px 16px;background:#1a1a1f;border:1px solid #2e2e35;border-radius:12px;margin-bottom:10px;flex-wrap:wrap;gap:8px}
.hdr h1{font-size:16px;font-weight:600}
.badge{font-size:11px;padding:2px 8px;border-radius:4px;font-weight:600}
.b-test{background:#2a2010;color:#ef9f27}
.b-live{background:#2a1010;color:#e24b4a}
.b-ok{background:#12210f;color:#7cb342}
.b-off{background:#2a1010;color:#e24b4a}
.b-short{background:#2a1010;color:#e24b4a}
.b-long{background:#12210f;color:#7cb342}
.b-sig{background:#141417;color:#888780;border:1px solid #2e2e35}
.b-rule{background:#1e1530;color:#a98fe0}
.btn{padding:7px 14px;font-size:12px;font-weight:600;border-radius:8px;border:1px solid;cursor:pointer;background:transparent}
.btn-stop{border-color:#4a1b1b;background:#1e1010;color:#e24b4a}
.btn-go{border-color:#1a3d1e;background:#101a12;color:#7cb342}
.tabs{display:flex;gap:4px;background:#141417;padding:4px;border-radius:8px;margin-bottom:12px;flex-wrap:wrap}
.tab{padding:7px 16px;font-size:13px;border-radius:6px;color:#888780;cursor:pointer;border:none;background:transparent}
.tab.on{background:#1a1a1f;color:#e2e0d8;font-weight:600;border:1px solid #3a3a42}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px;margin-bottom:14px}
.met{background:#141417;border-radius:8px;padding:10px 12px}
.met .l{font-size:11px;color:#5f5e5a;margin-bottom:3px}
.met .v{font-size:20px;font-weight:600}
.met .s{font-size:11px;color:#5f5e5a;margin-top:2px}
.up{color:#7cb342}.dn{color:#e24b4a}.mut{color:#888780}
.card{background:#1a1a1f;border:1px solid #2e2e35;border-radius:12px;padding:12px 14px;margin-bottom:10px}
.sect{font-size:11px;font-weight:600;color:#5f5e5a;letter-spacing:.06em;text-transform:uppercase;margin:0 0 8px}
table{width:100%;border-collapse:collapse;font-size:12px}
th{text-align:left;color:#5f5e5a;font-weight:600;padding:6px 8px;border-bottom:1px solid #2e2e35;font-size:11px}
td{padding:7px 8px;border-bottom:1px solid #222228;color:#c2c0b6}
tr:last-child td{border-bottom:none}
.pos{display:flex;align-items:center;justify-content:space-between;padding:10px 12px;background:#141417;border-radius:8px;margin-bottom:6px;flex-wrap:wrap;gap:6px}
.pos .nm{font-size:14px;font-weight:600}
.pos .dt{font-size:11px;color:#888780}
.pnl{font-size:14px;font-weight:600}
.empty{color:#5f5e5a;font-size:12px;padding:14px;text-align:center}
.age{font-size:11px;color:#5f5e5a}
#toast{position:fixed;bottom:16px;right:16px;background:#1a1a1f;border:1px solid #3a3a42;border-radius:8px;padding:10px 16px;font-size:12px;display:none}
label{font-size:11px;color:#5f5e5a;display:block;margin-bottom:3px}
input,select{width:100%;background:#141417;border:1px solid #2e2e35;border-radius:6px;color:#e2e0d8;padding:6px 8px;font-size:12px;font-family:inherit}
input:focus,select:focus{outline:none;border-color:#5f5e5a}
.frow{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px;margin-bottom:8px}
.crow{display:grid;grid-template-columns:130px 70px 55px 90px 30px;gap:6px;align-items:center;margin-bottom:6px}
.xbtn{background:#1e1010;border:1px solid #4a1b1b;color:#e24b4a;border-radius:6px;cursor:pointer;padding:6px 0;font-size:12px}
.mini{background:transparent;border:1px solid #2e2e35;color:#888780;border-radius:5px;cursor:pointer;padding:3px 8px;font-size:11px;margin-left:4px}
.mini:hover{border-color:#5f5e5a;color:#c2c0b6}
.mini.del:hover{border-color:#4a1b1b;color:#e24b4a}
@media(max-width:600px){td:nth-child(n+6),th:nth-child(n+6){display:none}.crow{grid-template-columns:1fr 1fr;grid-auto-rows:auto}}
</style>
</head>
<body>

<div class="hdr">
  <div style="display:flex;align-items:center;gap:10px">
    <h1>STS</h1>
    <span id="mode" class="badge b-test">-</span>
    <span id="health" class="badge b-off">baglaniyor</span>
  </div>
  <div style="display:flex;align-items:center;gap:10px">
    <span id="ks-state" class="age"></span>
    <button id="ks-btn" class="btn btn-stop" onclick="toggleKs()">durdur</button>
  </div>
</div>

<div class="tabs">
  <button class="tab on" onclick="show('durum',this)">durum</button>
  <button class="tab" onclick="show('islemler',this)">islemler</button>
  <button class="tab" onclick="show('olaylar',this)">olaylar</button>
  <button class="tab" onclick="show('kurallar',this)">kurallar</button>
</div>

<div id="p-durum">
  <div class="grid">
    <div class="met"><div class="l">bakiye</div><div class="v" id="m-bal">-</div><div class="s">USDT</div></div>
    <div class="met"><div class="l">acik PnL</div><div class="v" id="m-upnl">-</div><div class="s">anlik</div></div>
    <div class="met"><div class="l">sinyal havuzu</div><div class="v" id="m-sig">-</div><div class="s">acik / max</div></div>
    <div class="met"><div class="l">ozel havuz</div><div class="v" id="m-rule">-</div><div class="s">acik / max</div></div>
  </div>
  <p class="sect">acik pozisyonlar</p>
  <div id="positions"><div class="empty">yukleniyor...</div></div>
</div>

<div id="p-islemler" style="display:none">
  <div class="grid">
    <div class="met"><div class="l">kapanan islem</div><div class="v" id="t-count">-</div></div>
    <div class="met"><div class="l">toplam PnL</div><div class="v" id="t-pnl">-</div><div class="s">USDT</div></div>
    <div class="met"><div class="l">isabet</div><div class="v" id="t-win">-</div></div>
  </div>
  <div class="card"><div style="overflow-x:auto"><table id="trades">
    <thead><tr><th>coin</th><th>yon</th><th>kaynak</th><th>giris</th><th>cikis</th><th>PnL</th><th>sebep</th><th>acilis</th></tr></thead>
    <tbody></tbody>
  </table></div></div>
</div>

<div id="p-olaylar" style="display:none">
  <div class="card"><div style="overflow-x:auto"><table id="events">
    <thead><tr><th>zaman</th><th>tip</th><th>coin</th><th>detay</th></tr></thead>
    <tbody></tbody>
  </table></div></div>
</div>

<div id="p-kurallar" style="display:none">
  <div class="card">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">
      <p class="sect" style="margin:0" id="form-title">yeni kural</p>
      <button class="btn btn-go" onclick="toggleForm()" id="form-toggle">+ kural ekle</button>
    </div>

    <div id="rule-form" style="display:none">
      <div class="frow">
        <div><label>coin</label><input id="f-coin" placeholder="HEI"></div>
        <div><label>yon</label><select id="f-dir"><option value="SHORT">short</option><option value="LONG">long</option></select></div>
        <div><label>periyot</label><select id="f-tf">
          <option value="5m">5m</option><option value="15m">15m</option><option value="30m">30m</option>
          <option value="1h">1h</option><option value="4h">4h</option><option value="1d">1d</option>
        </select></div>
        <div><label>mantik</label><select id="f-logic"><option value="AND">ve</option><option value="OR">veya</option></select></div>
      </div>

      <div style="border-top:1px solid #2e2e35;padding-top:10px;margin-top:10px">
        <label style="display:block;margin-bottom:6px">kosullar</label>
        <div id="conds"></div>
        <button class="btn" style="border-color:#3a3a42;color:#888780;font-size:11px;padding:4px 10px;margin-top:6px" onclick="addCond()">+ kosul ekle</button>
      </div>

      <div class="frow" style="border-top:1px solid #2e2e35;padding-top:10px;margin-top:10px">
        <div><label>tp tipi</label><select id="f-tptype"><option value="pct">yuzde</option><option value="price">fiyat</option></select></div>
        <div><label>tp degeri</label><input id="f-tpval" placeholder="10"></div>
        <div><label>sl tipi</label><select id="f-sltype"><option value="pct">yuzde</option><option value="price">fiyat</option></select></div>
        <div><label>sl degeri</label><input id="f-slval" placeholder="15"></div>
      </div>

      <div class="frow">
        <div><label>teminat (USDT)</label><input id="f-margin" placeholder="100"></div>
        <div><label>kaldirac</label><input id="f-lev" placeholder="10"></div>
        <div><label>gecerlilik (gun)</label><input id="f-days" placeholder="3"></div>
        <div><label>not</label><input id="f-note" placeholder="opsiyonel"></div>
      </div>

      <div id="f-errors" style="display:none;background:#1e1010;border:1px solid #4a1b1b;border-radius:6px;padding:8px 10px;margin-top:10px;font-size:12px;color:#e24b4a"></div>

      <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:12px">
        <button class="btn" style="border-color:#3a3a42;color:#888780" onclick="closeForm()">iptal</button>
        <button class="btn btn-go" onclick="saveRule()" id="f-save">kaydet</button>
      </div>
    </div>
  </div>

  <div class="card"><div style="overflow-x:auto"><table id="rules">
    <thead><tr><th>id</th><th>coin</th><th>yon</th><th>tf</th><th>kosullar</th><th>tp</th><th>sl</th><th>tem/kald</th><th>durum</th><th></th></tr></thead>
    <tbody></tbody>
  </table></div></div>
</div>

<div id="toast"></div>

<script>
var state = null;

function show(name, el) {
  ['durum','islemler','olaylar','kurallar'].forEach(function(n){
    document.getElementById('p-'+n).style.display = (n===name)?'':'none';
  });
  document.querySelectorAll('.tab').forEach(function(t){t.classList.remove('on')});
  el.classList.add('on');
}

function fmt(n, d) {
  if (n===null||n===undefined||isNaN(n)) return '-';
  return Number(n).toLocaleString('en-US',{maximumFractionDigits:d===undefined?2:d});
}

function esc(s){var d=document.createElement('div');d.textContent=s==null?'':String(s);return d.innerHTML}

function toast(msg){
  var t=document.getElementById('toast');
  t.textContent=msg; t.style.display='block';
  setTimeout(function(){t.style.display='none'},2500);
}

function render() {
  if (!state) return;
  var st = state.status || {};

  var mode=document.getElementById('mode');
  if (st.testnet===false){mode.textContent='CANLI';mode.className='badge b-live';}
  else {mode.textContent='testnet';mode.className='badge b-test';}

  var h=document.getElementById('health');
  var age=state.status_age;
  if (age!==null && age<90){h.textContent='executor aktif ('+age+'s)';h.className='badge b-ok';}
  else if (age!==null){h.textContent='executor SESSIZ ('+age+'s)';h.className='badge b-off';}
  else {h.textContent='status yok';h.className='badge b-off';}

  var ks=state.killswitch;
  document.getElementById('ks-state').textContent = ks?'KILL-SWITCH AKTIF':'';
  var kb=document.getElementById('ks-btn');
  kb.textContent = ks?'devam et':'durdur';
  kb.className = ks?'btn btn-go':'btn btn-stop';

  document.getElementById('m-bal').textContent = fmt(st.balance,0);
  document.getElementById('m-sig').textContent = (st.sig_count!=null?st.sig_count:'-')+' / '+(st.sig_max||'-');
  document.getElementById('m-rule').textContent = (st.rule_count!=null?st.rule_count:'-')+' / '+(st.rule_max||'-');

  var pos = st.positions||[];
  var upnl = 0, hasU=false;
  pos.forEach(function(p){ if(p.upnl!=null){upnl+=Number(p.upnl);hasU=true;} });
  var mu=document.getElementById('m-upnl');
  mu.textContent = hasU?fmt(upnl):'-';
  mu.className = 'v '+(upnl>0?'up':(upnl<0?'dn':'mut'));

  var box=document.getElementById('positions');
  if (!pos.length){box.innerHTML='<div class="empty">acik pozisyon yok</div>';}
  else {
    box.innerHTML = pos.map(function(p){
      var u=Number(p.upnl||0);
      return '<div class="pos">'
        +'<div><span class="nm">'+esc(p.coin)+'</span> '
        +'<span class="badge '+(p.side==='LONG'?'b-long':'b-short')+'">'+esc(p.side)+'</span> '
        +'<span class="badge '+(p.source==='rule'?'b-rule':'b-sig')+'">'+(p.source==='rule'?'kural':'sinyal')+'</span>'
        +'<div class="dt">giris '+fmt(p.entry,6)+' | mark '+fmt(p.mark,6)+' | tp '+fmt(p.tp,6)+' | sl '+fmt(p.sl,6)+' | '+(p.leverage||'-')+'x</div></div>'
        +'<div class="pnl '+(u>0?'up':(u<0?'dn':'mut'))+'">'+fmt(u)+'</div>'
        +'</div>';
    }).join('');
  }

  var closed=(state.trades||[]).filter(function(t){return t.closed_at});
  var pnlSum=0,win=0;
  closed.forEach(function(t){var p=Number(t.pnl||0);pnlSum+=p;if(p>0)win++;});
  document.getElementById('t-count').textContent=closed.length;
  var tp=document.getElementById('t-pnl');
  tp.textContent=fmt(pnlSum); tp.className='v '+(pnlSum>0?'up':(pnlSum<0?'dn':'mut'));
  document.getElementById('t-win').textContent = closed.length?Math.round(win/closed.length*100)+'%':'-';

  var tb=document.querySelector('#trades tbody');
  tb.innerHTML=(state.trades||[]).map(function(t){
    var p=t.pnl==null?null:Number(t.pnl);
    return '<tr><td>'+esc(t.coin)+'</td>'
      +'<td><span class="badge '+(t.side==='LONG'?'b-long':'b-short')+'">'+esc(t.side||'')+'</span></td>'
      +'<td>'+esc(t.source||'signal')+'</td>'
      +'<td>'+fmt(t.entry_price,6)+'</td>'
      +'<td>'+(t.closed_at?fmt(t.exit_price,6):'<span class="mut">acik</span>')+'</td>'
      +'<td class="'+(p>0?'up':(p<0?'dn':''))+'">'+(p==null?'-':fmt(p))+'</td>'
      +'<td>'+esc(t.exit_reason||'-')+'</td>'
      +'<td class="mut">'+esc((t.opened_at||'').replace('T',' ').slice(5,16))+'</td></tr>';
  }).join('');

  var eb=document.querySelector('#events tbody');
  eb.innerHTML=(state.events||[]).map(function(e){
    return '<tr><td class="mut">'+esc((e.ts||'').replace('T',' ').slice(5,19))+'</td>'
      +'<td>'+esc(e.kind)+'</td><td>'+esc(e.coin||'-')+'</td><td>'+esc(e.detail||'')+'</td></tr>';
  }).join('');

  var rb=document.querySelector('#rules tbody');
  rb.innerHTML=(state.rules||[]).map(function(r){
    var st_= r.active?'<span class="badge b-ok">aktif</span>'
           : (r.triggered_at?'<span class="badge b-rule">tetiklendi</span>':'<span class="badge b-off">pasif</span>');
    return '<tr><td>'+r.id+'</td><td>'+esc(r.coin)+'</td>'
      +'<td><span class="badge '+(r.direction==='LONG'?'b-long':'b-short')+'">'+esc(r.direction)+'</span></td>'
      +'<td>'+esc(r.timeframe)+'</td>'
      +'<td style="max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+esc(condText(r.conditions,r.logic))+'</td>'
      +'<td>'+fmtLevel(r.tp_type,r.tp_value)+'</td>'
      +'<td>'+fmtLevel(r.sl_type,r.sl_value)+'</td>'
      +'<td class="mut">'+fmt(r.margin_usdt,0)+'$ '+(r.leverage||'-')+'x</td>'
      +'<td>'+st_+'</td>'
      +'<td style="white-space:nowrap">'
      +'<button class="mini" onclick="editRule('+r.id+')">duzenle</button>'
      +'<button class="mini" onclick="toggleRule('+r.id+','+(!r.active)+')">'+(r.active?'pasif':'aktif')+'</button>'
      +'<button class="mini del" onclick="delRule('+r.id+')">sil</button>'
      +'</td></tr>';
  }).join('');
}

// ---------- kural formu ----------
var COND_LABELS={ema_cross:'ema kesisimi',rsi:'rsi',price:'fiyat',oi_change:'oi degisimi',volume:'hacim',funding:'funding'};
var NEEDS_P1=['ema_cross','rsi','oi_change','volume'];
var P1_HINT={ema_cross:'hizli',rsi:'periyot',oi_change:'mum',volume:'ort.mum'};
var P2_HINT={ema_cross:'yavas',rsi:'esik',price:'fiyat',oi_change:'%',volume:'x',funding:'%'};
var editingId=null;

function condText(conds,logic){
  if(typeof conds==='string'){try{conds=JSON.parse(conds)}catch(e){return String(conds)}}
  if(!conds||!conds.length)return '-';
  return conds.map(function(c){
    var t=COND_LABELS[c.type]||c.type;
    if(c.type==='ema_cross')return 'ema'+c.p1+' '+c.op+' ema'+c.p2;
    if(c.type==='rsi')return 'rsi'+c.p1+' '+c.op+' '+c.p2;
    if(c.type==='oi_change')return 'oi('+c.p1+' mum) '+c.op+' '+c.p2+'%';
    if(c.type==='volume')return 'hacim '+c.op+' '+c.p2+'x';
    return t+' '+c.op+' '+c.p2;
  }).join(logic==='OR'?'  VEYA  ':'  VE  ');
}

function fmtLevel(type,val){
  if(val==null)return '-';
  return type==='pct'?(fmt(val,2)+'%'):fmt(val,8);
}

function condRow(c){
  c=c||{type:'ema_cross',op:'<',p1:7,p2:30};
  var d=document.createElement('div');
  d.className='crow';
  var opts=Object.keys(COND_LABELS).map(function(k){
    return '<option value="'+k+'"'+(k===c.type?' selected':'')+'>'+COND_LABELS[k]+'</option>';
  }).join('');
  var ops=['<','>','<=','>='].map(function(o){
    return '<option value="'+o+'"'+(o===c.op?' selected':'')+'>'+o+'</option>';
  }).join('');
  d.innerHTML='<select class="c-type" onchange="syncCond(this)">'+opts+'</select>'
    +'<input class="c-p1" value="'+(c.p1==null?'':c.p1)+'">'
    +'<select class="c-op">'+ops+'</select>'
    +'<input class="c-p2" value="'+(c.p2==null?'':c.p2)+'">'
    +'<button class="xbtn" onclick="this.parentNode.remove()">x</button>';
  return d;
}

function syncCond(sel){
  var row=sel.parentNode, t=sel.value;
  var p1=row.querySelector('.c-p1'), p2=row.querySelector('.c-p2');
  var need=NEEDS_P1.indexOf(t)>=0;
  p1.disabled=!need;
  p1.placeholder=need?(P1_HINT[t]||''):'-';
  if(!need)p1.value='';
  p2.placeholder=P2_HINT[t]||'';
}

function addCond(c){
  var box=document.getElementById('conds');
  var row=condRow(c);
  box.appendChild(row);
  syncCond(row.querySelector('.c-type'));
}

function toggleForm(){
  var f=document.getElementById('rule-form');
  if(f.style.display==='none'){openForm(null)}else{closeForm()}
}

function openForm(r){
  editingId = r?r.id:null;
  document.getElementById('rule-form').style.display='';
  document.getElementById('form-toggle').textContent='kapat';
  document.getElementById('form-title').textContent = r?('kural duzenle #'+r.id):'yeni kural';
  document.getElementById('f-errors').style.display='none';
  var conds=document.getElementById('conds');
  conds.innerHTML='';
  if(r){
    document.getElementById('f-coin').value=r.coin||'';
    document.getElementById('f-dir').value=r.direction||'SHORT';
    document.getElementById('f-tf').value=r.timeframe||'5m';
    document.getElementById('f-logic').value=r.logic||'AND';
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
    (cs||[]).forEach(function(c){addCond(c)});
    if(!cs||!cs.length)addCond();
  }else{
    ['f-coin','f-tpval','f-slval','f-margin','f-lev','f-days','f-note'].forEach(function(id){
      document.getElementById(id).value='';
    });
    document.getElementById('f-dir').value='SHORT';
    document.getElementById('f-tf').value='5m';
    document.getElementById('f-logic').value='AND';
    document.getElementById('f-tptype').value='pct';
    document.getElementById('f-sltype').value='pct';
    addCond();
  }
  document.getElementById('rule-form').scrollIntoView({behavior:'smooth',block:'nearest'});
}

function closeForm(){
  document.getElementById('rule-form').style.display='none';
  document.getElementById('form-toggle').textContent='+ kural ekle';
  document.getElementById('form-title').textContent='yeni kural';
  editingId=null;
}

function collectForm(){
  var conds=[];
  document.querySelectorAll('#conds .crow').forEach(function(row){
    conds.push({
      type: row.querySelector('.c-type').value,
      op:   row.querySelector('.c-op').value,
      p1:   row.querySelector('.c-p1').value,
      p2:   row.querySelector('.c-p2').value
    });
  });
  return {
    coin: document.getElementById('f-coin').value,
    direction: document.getElementById('f-dir').value,
    timeframe: document.getElementById('f-tf').value,
    logic: document.getElementById('f-logic').value,
    conditions: conds,
    tp_type: document.getElementById('f-tptype').value,
    tp_value: document.getElementById('f-tpval').value,
    sl_type: document.getElementById('f-sltype').value,
    sl_value: document.getElementById('f-slval').value,
    margin_usdt: document.getElementById('f-margin').value||100,
    leverage: document.getElementById('f-lev').value||10,
    expire_days: document.getElementById('f-days').value,
    note: document.getElementById('f-note').value,
    active: true
  };
}

function showErrors(list){
  var box=document.getElementById('f-errors');
  box.innerHTML=list.map(function(e){return '&bull; '+esc(e)}).join('<br>');
  box.style.display='';
}

function saveRule(){
  var data=collectForm();
  var url=editingId?('/api/rules/'+editingId):'/api/rules';
  var method=editingId?'PATCH':'POST';
  var btn=document.getElementById('f-save');
  btn.disabled=true; btn.textContent='kaydediliyor...';
  fetch(url,{method:method,headers:{'Content-Type':'application/json'},body:JSON.stringify(data)})
    .then(function(r){return r.json().then(function(j){return {s:r.status,j:j}})})
    .then(function(res){
      btn.disabled=false; btn.textContent='kaydet';
      if(res.s===200&&res.j.ok){toast(editingId?'kural guncellendi':'kural eklendi');closeForm();refresh();}
      else{showErrors(res.j.errors||['kaydedilemedi']);}
    }).catch(function(){
      btn.disabled=false; btn.textContent='kaydet';
      showErrors(['baglanti hatasi']);
    });
}

function editRule(id){
  var r=(state.rules||[]).filter(function(x){return x.id===id})[0];
  if(r)openForm(r);
}

function toggleRule(id,active){
  fetch('/api/rules/'+id+'/toggle',{method:'PATCH',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({active:active})})
    .then(function(){toast(active?'kural aktif':'kural pasif');refresh();});
}

function delRule(id){
  if(!confirm('Kural #'+id+' silinsin mi?'))return;
  fetch('/api/rules/'+id,{method:'DELETE'}).then(function(){toast('kural silindi');refresh();});
}

function refresh(){
  fetch('/api/state').then(function(r){return r.json()}).then(function(d){
    state=d; render();
  }).catch(function(e){
    document.getElementById('health').textContent='panel hatasi';
  });
}

function toggleKs(){
  var ks=state&&state.killswitch;
  var action=ks?'/api/resume':'/api/stop';
  var msg=ks?'Bot devam etsin mi?':'Yeni pozisyon acma DURDURULSUN mu? (acik pozisyonlar izlenmeye devam eder)';
  if(!confirm(msg))return;
  fetch(action,{method:'POST'}).then(function(){toast(ks?'devam ediliyor':'durduruldu');refresh();});
}

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

    def do_POST(self):
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
    log(f"STS PANEL {VERSION} dinliyor: http://{PANEL_BIND}:{PANEL_PORT} | auth={'acik' if AUTH_ON else 'KAPALI'}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    log("Panel durduruldu")


if __name__ == "__main__":
    main()
