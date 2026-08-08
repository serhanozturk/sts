#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""STS panel dogrulama — tasarim degisikligi sonrasi kritik kontroller."""
import os
import re
import json
import subprocess
import tempfile

os.environ.setdefault("SUPABASE_URL", "https://m.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "k")
import panel as P                                    # noqa: E402

GECTI, FAIL = 0, []


def check(ad, kosul, detay=""):
    global GECTI
    if kosul:
        GECTI += 1
        print(f"  OK   {ad}")
    else:
        FAIL.append(ad)
        print(f"  FAIL {ad} {detay}")


h = P.HTML
css = re.search(r"<style>(.*?)</style>", h, re.S).group(1)
js = re.search(r"<script>(.*?)</script>", h, re.S).group(1)

print("=== CSS kapsama ===")
siniflar = set()
for m in re.findall(r'class="([^"]+)"', h):
    for s in m.split():
        if "+" not in s and "'" not in s:
            siniflar.add(s)
for m in re.findall(r"className\s*=\s*'([^']+)'", h):
    siniflar.update(m.split())
for m in re.findall(r"'(badge [a-z-]+)'", h):
    siniflar.update(m.split())
for m in re.findall(r"classList\.(?:add|remove|toggle)\('([^']+)'\)", h):
    siniflar.add(m)

JS_ONLY = {"c-type", "c-op", "c-p1", "c-p2", "w1"}
eksik = sorted(s for s in siniflar - JS_ONLY if ("." + s) not in css)
check("tum siniflar CSS'te tanimli", not eksik, f"eksik: {eksik}")

print("\n=== Tema degiskenleri ===")
DEG = ["--bg", "--surface", "--surface2", "--border", "--text", "--text2", "--text3",
       "--green", "--greenBg", "--greenBd", "--coral", "--coralBg", "--coralBd",
       "--purple", "--purpleBg", "--amber", "--amberBg", "--shadow"]
eks = [v for v in DEG if css.count(v + ":") < 2]
check("18 degisken iki temada tanimli", not eks, f"eksik: {eks}")
check("koyu tema blogu var", '[data-theme="dark"]' in css)
check("mobil media query var", "@media (max-width:720px)" in css)

print("\n=== JS bozulmamis ===")
check("kacirilmis tirnak korunmus", "dynToggle(\\'p" in js)
check("\\n kacisi korunmus", "kapatilsin mi?\\nBu islem" in js)
check("ham satir sonu yok", "mi?\n" not in js)
fn = set(re.findall(r"function\s+(\w+)\s*\(", js))
vr = set(re.findall(r"\bvar\s+(\w+)\s*=", js))
cak = sorted(fn & vr)
check("fonksiyon-var isim cakismasi yok", not cak, f"golgelenen: {cak}")

yol = os.path.join(tempfile.gettempdir(), "sts_js.js")
open(yol, "w", encoding="utf-8").write(js)
r = subprocess.run(["node", "--check", yol], capture_output=True, text=True, timeout=30)
check("node --check gecti", r.returncode == 0, (r.stderr or "")[:250])

print("\n=== Gizlenmesi gereken kutular ===")
check("errbox varsayilan gizli", re.search(r"\.errbox\{[^}]*display:none", css) is not None)
check("warnbox inline gizli", 'id="dtp-warn" class="warnbox" style="display:none"' in h
      or 'class="warnbox" style="display:none"' in h)
check("stopband inline gizli", 'id="stop-banner" class="stopband" style="display:none"' in h)
check("rule-form inline gizli", 'id="rule-form" style="display:none"' in h)
check("json-box inline gizli", 'id="json-box" style="display:none"' in h)
check("doner sadece animasyon", re.search(r"\.doner\{animation:", css) is not None)
check("doner boyut vermiyor", not re.search(r"\.doner\{[^}]*width:", css))

print("\n=== render() sahte DOM ile ===")
STUB = """
var _els={};
function _el(sel){ var _v=(sel==='.c-type')?'ema_cross':((sel==='.c-op')?'<':'');
 return {style:{},value:_v,dataset:{},textContent:'',innerHTML:'',checked:false,disabled:false,
  offsetTop:0,offsetParent:null,
  classList:{add:function(){},remove:function(){},toggle:function(){}},
  appendChild:function(){},remove:function(){},addEventListener:function(){},
  removeAttribute:function(){},setAttribute:function(){},select:function(){},
  setSelectionRange:function(){},scrollIntoView:function(){},
  querySelector:function(s){return _el(s)},querySelectorAll:function(){return []},
  closest:function(){return _el()},parentNode:{remove:function(){}}}; }
var document={getElementById:function(id){if(!_els[id])_els[id]=_el();return _els[id];},
 querySelector:function(){return _el()},querySelectorAll:function(){return []},
 createElement:function(){return _el()},body:_el(),addEventListener:function(){},
 documentElement:{setAttribute:function(){},getAttribute:function(){return 'light'}}};
var window={matchMedia:function(){return {matches:false}},isSecureContext:true,
 getSelection:function(){return {removeAllRanges:function(){}}},
 scrollTo:function(){},addEventListener:function(){}};
var localStorage={getItem:function(){return null},setItem:function(){}};
var location={origin:'https://test',href:'https://test'};
var navigator={};
function fetch(){return {then:function(){return {then:function(){return {catch:function(){}}}}}};}
function setInterval(){} function setTimeout(){} function confirm(){return false}
function alert(){}
"""
VERI = {
    "status": {"level": "RUN", "balance": 4355.0, "sig_count": 4, "sig_max": 6,
               "rule_count": 1, "rule_max": 10, "testnet": True,
               "positions": [{"trade_id": 6, "coin": "OGN", "side": "SHORT",
                              "source": "signal", "entry": 0.01665, "mark": 0.01613,
                              "tp": 0.01499, "sl": 0.01915, "upnl": 31.21,
                              "margin": 100, "leverage": 10, "contracts": 60024},
                             {"trade_id": 9, "coin": "BTCUSDT", "side": "SHORT",
                              "source": "rule", "entry": 64827.6, "mark": 64985.6,
                              "tp": 64179.3, "sl": 65050.0, "upnl": -24.11,
                              "margin": 100, "leverage": 99, "contracts": 0.1526}]},
    "status_age": 9, "level": "RUN", "emergency_pending": False, "killswitch": False,
    "webhook_enabled": True, "webhook_token": "tok",
    "settings": {"margin_usdt": 100, "leverage": 10, "margin_mode": "cross",
                 "tp_pct": 10, "sl_pct": 15, "max_positions": 6,
                 "max_rule_positions": 10, "min_balance": 1000, "rule_min_free": 100,
                 "dedup_days": 2, "poll_seconds": 20,
                 "signal_types": "PUMP_1H,PUMP_15M", "strength": "strong",
                 "wh_margin_usdt": 100, "wh_leverage": 10, "wh_tp_type": "pct",
                 "wh_tp_value": 10, "wh_sl_type": "pct", "wh_sl_value": 15,
                 "wh_dedup_sec": 60, "dyn_tp_active": True, "dyn_tp_timeframe": "5m",
                 "dyn_tp_mode": "OR", "dyn_tp_logic": "AND",
                 "dyn_tp_conditions": [{"type": "rsi", "op": ">", "p2": 70}],
                 "dyn_sl_active": False},
    "trades": [{"id": 6, "coin": "OGN", "side": "SHORT", "source": "signal",
                "entry_price": 0.01665, "exit_price": None, "pnl": None,
                "opened_at": "2026-08-08T10:00:00+00:00", "closed_at": None,
                "margin_usdt": 100, "tp_price": 0.01499, "sl_price": 0.01915,
                "req_result": None, "dyn_tp": None, "dyn_sl": None},
               {"id": 5, "coin": "TUT", "side": "SHORT", "source": "rule",
                "entry_price": 0.0688, "exit_price": 0.06924, "pnl": -6.39,
                "opened_at": "2026-08-08T15:00:00+00:00",
                "closed_at": "2026-08-08T15:19:00+00:00", "exit_reason": "SL_SOFT",
                "margin_usdt": 100, "tp_price": 0.06192, "sl_price": 0.069}],
    "events": [{"id": 1, "ts": "2026-08-08T15:19:00+00:00", "kind": "CLOSE",
                "coin": "TUT", "detail": "SHORT SL_SOFT"}],
    "webhooks": [{"id": 5, "created_at": "2026-08-08T15:15:00+00:00",
                  "coin": "BTCUSDT", "direction": "SHORT", "executed": True,
                  "result": "CLOSED", "payload": {"action": "close"}}],
    "rules": [{"id": 4, "coin": "TUT", "direction": "SHORT", "timeframe": "5m",
               "conditions": [{"type": "touch_ema", "op": "=", "p2": 7}],
               "logic": "AND", "tp_type": "pct", "tp_value": 10,
               "sl_type": "pct", "sl_value": 15, "margin_usdt": 100,
               "leverage": 10, "active": False,
               "triggered_at": "2026-08-08T15:00:00+00:00",
               "dyn_tp_active": True, "dyn_tp_mode": "OR", "dyn_tp_timeframe": "5m",
               "dyn_tp_conditions": [{"type": "rsi", "op": ">", "p2": 70}],
               "dyn_sl_active": False}],
}
kod = (STUB + "\n" + js + "\nstate = " + json.dumps(VERI) + ";\n"
       + "try{ render(); console.log('OK1'); }catch(e){ console.log('HATA1: '+e.message); }\n"
       + "try{ fillSettings(); console.log('OK2'); }catch(e){ console.log('HATA2: '+e.message); }\n"
       + "try{ uretJson(); console.log('OK3'); }catch(e){ console.log('HATA3: '+e.message); }\n")
yol2 = os.path.join(tempfile.gettempdir(), "sts_render.js")
open(yol2, "w", encoding="utf-8").write(kod)
r2 = subprocess.run(["node", yol2], capture_output=True, text=True, timeout=30)
cikti = (r2.stdout or "") + (r2.stderr or "")
check("render() hatasiz", "OK1" in cikti, cikti[:300])
check("fillSettings() hatasiz", "OK2" in cikti, cikti[:300])
check("uretJson() hatasiz", "OK3" in cikti, cikti[:300])

print("\n=== Backend saglam ===")
check("validate_rule calisiyor",
      P.validate_rule({"coin": "HEI", "direction": "SHORT", "timeframe": "5m",
                       "conditions": [{"type": "touch_ema", "op": "=", "p2": 7}],
                       "tp_type": "pct", "tp_value": 10,
                       "sl_type": "pct", "sl_value": 15})[1] == [])
check("validate_webhook kapatma",
      P.validate_webhook({"coin": "HEI", "action": "close"})[0].get("action") == "close")
check("LEVELS tanimli", P.LEVELS == ("RUN", "PAUSE", "STOP"))
check("touch tipleri tanimli",
      "touch_price" in P.COND_TYPES and "touch_ema" in P.COND_TYPES)

print("\n" + "=" * 46)
if FAIL:
    print(f"BASARISIZ: {len(FAIL)} -> {FAIL}")
    raise SystemExit(1)
print(f"TUM DOGRULAMALAR GECTI ({GECTI})")
