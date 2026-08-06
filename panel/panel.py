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
from datetime import datetime, timezone
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
@media(max-width:600px){td:nth-child(n+6),th:nth-child(n+6){display:none}}
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
  <div class="card"><div style="overflow-x:auto"><table id="rules">
    <thead><tr><th>id</th><th>coin</th><th>yon</th><th>tf</th><th>kosullar</th><th>tp</th><th>sl</th><th>durum</th></tr></thead>
    <tbody></tbody>
  </table></div></div>
  <p class="age">kural ekleme/duzenleme Asama 3'te panele gelecek - simdilik SQL ile</p>
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
    var conds = r.conditions;
    if (typeof conds!=='string') conds=JSON.stringify(conds);
    var st_= r.active?'<span class="badge b-ok">aktif</span>'
           : (r.triggered_at?'<span class="badge b-rule">tetiklendi</span>':'<span class="badge b-off">pasif</span>');
    return '<tr><td>'+r.id+'</td><td>'+esc(r.coin)+'</td>'
      +'<td><span class="badge '+(r.direction==='LONG'?'b-long':'b-short')+'">'+esc(r.direction)+'</span></td>'
      +'<td>'+esc(r.timeframe)+'</td>'
      +'<td style="max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+esc(conds)+'</td>'
      +'<td>'+esc(r.tp_type)+' '+fmt(r.tp_value,6)+'</td>'
      +'<td>'+esc(r.sl_type)+' '+fmt(r.sl_value,6)+'</td>'
      +'<td>'+st_+'</td></tr>';
  }).join('');
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
        else:
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
