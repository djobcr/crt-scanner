"""
crt_scanner.py
==============
Scanner du *CRT 3 Candle Model* (Candle Range Theory) avec confluence
multi-timeframes Weekly + H4, et alertes Telegram.

Setup recherché, sur chaque timeframe, sur la dernière bougie clôturée :
    bougie 1 = avant-dernière bougie clôturée   (le "range")
    bougie 2 = dernière bougie clôturée          (la "manipulation")
  -> bougie 2 a SWEEP la bougie 1 (mèche au-delà d'un extrême de la bougie 1)
  -> PUIS bougie 2 a CLÔTURÉ à l'intérieur du range de la bougie 1.
Setup valide uniquement si réuni EN WEEKLY *ET* EN H4. À chaque scan, tous les
H4 clôturés de la semaine en cours sont balayés (aucun raté).

SOURCES DE DONNÉES (interchangeables) :
  - tradingview : via tvDatafeed -> MÊMES bougies que tes charts TradingView.
                  Idéal pour le CRT (forex, or, BTC...). Non officiel : peut casser.
  - ccxt        : exchanges crypto uniquement (BTC, ETH... en /USDT).
La détection CRT est indépendante de la source.

Installation :
    pip install pandas
    pip install tradingview-datafeed      # source tradingview (défaut)
    pip install ccxt                       # source ccxt (crypto)

Telegram : créer un bot via @BotFather, récupérer TOKEN + chat_id, puis
    export TELEGRAM_TOKEN="..."   ;   export TELEGRAM_CHAT_ID="..."

Exemples :
    # Forex majeures + or + BTC, bougies TradingView (broker OANDA par défaut)
    python crt_scanner.py
    python crt_scanner.py --telegram --watch 30
    python crt_scanner.py --symbols OANDA:EURUSD OANDA:XAUUSD BINANCE:BTCUSDT
    python crt_scanner.py --tv-exchange FOREXCOM           # autre broker
    python crt_scanner.py --tv-user MONID --tv-pass MONMDP # session loggée (plus de symboles)
    # Crypto pur via ccxt
    python crt_scanner.py --source ccxt --top 50 --telegram
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

import pandas as pd


# Windows : la console par défaut (cp1252) plante sur les emojis et le tiret '–'.
# On force stdout/stderr en UTF-8 pour que les print ne lèvent pas d'exception.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass


# Fuseau d'AFFICHAGE des alertes et du tableau (réglé dans main via --tz).
# Par défaut UTC ; le CLI le met sur Europe/Paris.
DISPLAY_TZ = timezone.utc


def fmt_time(ms: int, fmt: str = "%Y-%m-%d %H:%M %Z") -> str:
    """Formate un timestamp epoch (ms, UTC) dans le fuseau d'affichage courant."""
    return datetime.fromtimestamp(ms / 1000, tz=DISPLAY_TZ).strftime(fmt)


# ===========================================================================
# 1) Détection CRT — cœur, totalement indépendant de la source de données
# ===========================================================================
@dataclass
class CRT:
    direction: str   # 'bull' (sweep du bas) | 'bear' (sweep du haut) | 'both' (outside bar)
    c1_low: float
    c1_high: float
    c2_low: float
    c2_high: float
    c2_close: float
    span: int = 1    # nb de bougies entre la range et la manipulation (1 = 3 candle model)


def crt_at(highs, lows, closes, i: int, window: int = 1) -> CRT | None:
    """
    CRT où la bougie de MANIPULATION est à l'index i. On balaie les bougies 'range'
    candidates de i-1 à i-window (la plus proche d'abord) : dès qu'une bougie range r
    est telle que la bougie i SWEEP un de ses extrêmes ET CLÔTURE dans son range,
    on renvoie le CRT correspondant. window=1 = modèle strict d'origine (range = i-1).
    Bougies CLÔTURÉES.
    """
    if i < 1 or i >= len(closes):
        return None

    c2_high, c2_low, c2_close = highs[i], lows[i], closes[i]
    stop = max(-1, i - 1 - window)
    for r in range(i - 1, stop, -1):                 # range candle la plus proche d'abord
        c1_high, c1_low = highs[r], lows[r]
        swept_low = c2_low < c1_low
        swept_high = c2_high > c1_high
        closed_inside = c1_low <= c2_close <= c1_high
        if not closed_inside or not (swept_low or swept_high):
            continue
        if swept_low and not swept_high:
            direction = "bull"
        elif swept_high and not swept_low:
            direction = "bear"
        else:
            direction = "both"
        return CRT(direction, c1_low, c1_high, c2_low, c2_high, c2_close, span=i - r)
    return None


def detect_crt(highs, lows, closes, window: int = 1) -> CRT | None:
    """Raccourci : CRT sur la DERNIÈRE bougie clôturée (range cherché jusqu'à window avant)."""
    return crt_at(highs, lows, closes, len(closes) - 1, window)


def directions_aligned(a: CRT, b: CRT) -> bool:
    if "both" in (a.direction, b.direction):
        return True
    return a.direction == b.direction


# ===========================================================================
# 2) Sources de données interchangeables
# ===========================================================================
# Une source expose .ohlcv(symbol, timeframe, limit) -> (times_ms, highs, lows, closes)
# en ne renvoyant QUE des bougies clôturées (la bougie en formation est retirée),
# et .default_symbols() -> liste de symboles par défaut.
TF_MS = {"4h": 14_400_000, "1w": 604_800_000}
WEEK_MS = 604_800_000


def _drop_forming(rows, tf_ms: int, tz_offset_ms: int = 0):
    """rows = [(open_ms, high, low, close), ...] -> garde les bougies clôturées."""
    now = int(time.time() * 1000)
    # open ramené en UTC = open_ms - tz_offset_ms ; close si open_utc + durée <= now
    return [r for r in rows if (r[0] - tz_offset_ms) + tf_ms <= now]


class Source:
    name = "?"

    def ohlcv(self, symbol: str, timeframe: str, limit: int):
        raise NotImplementedError

    def default_symbols(self) -> list[str]:
        return []


class TradingViewSource(Source):
    """Bougies identiques aux charts TradingView, via tvDatafeed."""
    name = "tradingview"

    def __init__(self, username=None, password=None, tv_exchange="OANDA", source_tz=None):
        from tvDatafeed import TvDatafeed, Interval
        self._Interval = Interval
        self._TF = {"1w": Interval.in_weekly, "4h": Interval.in_4_hour}
        self.tv = TvDatafeed(username, password)
        self.tv_exchange = tv_exchange
        # tvDatafeed renvoie les timestamps dans le fuseau LOCAL de la machine.
        # On les interprète donc dans ce fuseau (ou source_tz si forcé) pour
        # retrouver le vrai UTC. Sur un VPS en UTC, le local EST déjà UTC : juste.
        self.src_tz = ZoneInfo(source_tz) if source_tz else datetime.now().astimezone().tzinfo

    def _split(self, symbol: str):
        # "OANDA:EURUSD" -> ("EURUSD", "OANDA") ; "EURUSD" -> ("EURUSD", défaut)
        if ":" in symbol:
            ex, sym = symbol.split(":", 1)
            return sym, ex
        return symbol, self.tv_exchange

    def ohlcv(self, symbol, timeframe, limit):
        sym, ex = self._split(symbol)
        df = self.tv.get_hist(sym, ex, interval=self._TF[timeframe], n_bars=limit + 2)
        if df is None or len(df) == 0:
            return [], [], [], []

        df = df.rename(columns={c: c.lower() for c in df.columns})
        rows = []
        for ts, row in df.iterrows():
            pts = pd.Timestamp(ts)
            if pts.tzinfo is None:
                pts = pts.tz_localize(self.src_tz)   # fuseau local de la machine -> vrai UTC
            open_ms = int(pts.timestamp() * 1000)
            rows.append((open_ms, float(row["high"]), float(row["low"]), float(row["close"])))

        rows = _drop_forming(rows, TF_MS[timeframe], 0)
        return ([r[0] for r in rows], [r[1] for r in rows],
                [r[2] for r in rows], [r[3] for r in rows])

    def default_symbols(self):
        majors = ["EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD"]
        return ([f"{self.tv_exchange}:{m}" for m in majors]
                + [f"{self.tv_exchange}:XAUUSD", "BINANCE:BTCUSDT"])


class CCXTSource(Source):
    """Exchanges crypto via ccxt (paires /USDT par défaut)."""
    name = "ccxt"

    def __init__(self, exchange_name="binance", quote="USDT", top=50):
        import ccxt
        if not hasattr(ccxt, exchange_name):
            raise SystemExit(f"Exchange ccxt inconnu : {exchange_name!r}")
        self.ex = getattr(ccxt, exchange_name)({"enableRateLimit": True})
        self.quote = quote
        self.top = top

    def ohlcv(self, symbol, timeframe, limit):
        raw = self.ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit + 2)
        rows = [(c[0], c[2], c[3], c[4]) for c in raw]
        rows = _drop_forming(rows, TF_MS[timeframe], 0)
        return ([r[0] for r in rows], [r[1] for r in rows],
                [r[2] for r in rows], [r[3] for r in rows])

    def default_symbols(self):
        self.ex.load_markets()
        tickers = self.ex.fetch_tickers()
        rows = [(s, t.get("quoteVolume") or 0)
                for s, t in tickers.items() if s.endswith(f"/{self.quote}")]
        rows.sort(key=lambda x: x[1], reverse=True)
        return [s for s, _ in rows[:self.top]]


# ===========================================================================
# 3) Étape 1 (weekly) — contexte de la semaine en cours
# ===========================================================================
def weekly_context(source: Source, symbol: str, window: int = 1):
    """
    Renvoie (crt_weekly, weekly_ts, week_start, week_end) si la dernière weekly
    clôturée forme un CRT (range cherché jusqu'à `window` weekly avant), sinon None.
    week_start = open de la semaine en cours. week_start et les open H4 viennent du
    même flux : la fenêtre est donc cohérente quel que soit le fuseau du fournisseur.
    """
    t, h, l, c = source.ohlcv(symbol, "1w", max(6, window + 2))
    if len(c) < 2:
        return None
    crt = detect_crt(h, l, c, window)
    if crt is None:
        return None
    weekly_ts = t[-1]
    week_start = weekly_ts + WEEK_MS
    return crt, weekly_ts, week_start, week_start + WEEK_MS


# ===========================================================================
# 4) Telegram (lib standard, aucune dépendance)
# ===========================================================================
def send_telegram(token: str, chat_id: str, text: str) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": "true",
    }).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=20) as r:
            return r.status == 200
    except Exception as exc:
        print(f"  [telegram] échec d'envoi : {exc}", file=sys.stderr)
        return False


_LABEL = {"bull": ("🟢", "LONG"), "bear": ("🔴", "SHORT"), "both": ("⚪", "OUTSIDE")}


_JOURS_FR = ["lun", "mar", "mer", "jeu", "ven", "sam", "dim"]


def format_alert(m: "Match") -> str:
    emoji, w_dir = _LABEL[m.weekly.direction]
    sym = m.symbol.split(":")[-1]          # BINANCE:BTCUSDT -> BTCUSDT
    dt = datetime.fromtimestamp(m.h4_ts / 1000, tz=DISPLAY_TZ)
    when = f"{_JOURS_FR[dt.weekday()]} {dt:%H:%M}"   # ex. "ven 18:00"
    # span = écart bougies range -> manipulation. 1 = 3 candle model classique,
    # sinon formation plus longue (range + intermédiaires + manip + expansion).
    n = m.h4.span + 2
    model = "3 candle model" if m.h4.span == 1 else f"{n} candle model (étendu)"
    return (
        f"{emoji} <b>{sym} · {w_dir}</b>\n"
        f"🕓 H4 · {when}\n"
        f"📐 {model}"
    )


# ===========================================================================
# 5) Dédup persistante — une alerte par setup (par jeu de bougies)
# ===========================================================================
def load_seen(path: str) -> dict:
    if not path or not os.path.exists(path):
        return {}
    try:
        seen = json.load(open(path))
    except Exception:
        return {}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    return {k: v for k, v in seen.items() if v >= cutoff}


def save_seen(path: str, seen: dict) -> None:
    if path:
        json.dump(seen, open(path, "w"))


def alert_key(m: "Match") -> str:
    return f"{m.symbol}|{m.weekly_ts}|{m.h4_ts}"


# ===========================================================================
# 6) Scanner
# ===========================================================================
@dataclass
class Match:
    symbol: str
    weekly: CRT
    h4: CRT
    aligned: bool
    weekly_ts: int
    h4_ts: int


def scan(source: Source, symbols: list[str], require_align: bool,
         latest_only: bool = False, window: int = 1) -> list[Match]:
    matches: list[Match] = []
    total = len(symbols)

    for idx, symbol in enumerate(symbols, 1):
        print(f"\r  {idx}/{total}  {symbol:<18}", end="", file=sys.stderr, flush=True)
        try:
            ctx = weekly_context(source, symbol, window)
            if ctx is None:
                continue
            weekly, weekly_ts, week_start, week_end = ctx

            hts, hh, hl, hc = source.ohlcv(symbol, "4h", 80)
            if len(hts) < 2:
                continue

            idxs = [i for i in range(1, len(hts)) if week_start <= hts[i] < week_end]
            if latest_only and idxs:
                idxs = [idxs[-1]]

            for i in idxs:
                h4 = crt_at(hh, hl, hc, i, window)
                if h4 is None:
                    continue
                aligned = directions_aligned(weekly, h4)
                if require_align and not aligned:
                    continue
                matches.append(Match(symbol, weekly, h4, aligned, weekly_ts, hts[i]))
        except Exception as exc:
            print(f"\n  [!] {symbol}: {exc}", file=sys.stderr)
            continue

    print("\r" + " " * 42 + "\r", end="", file=sys.stderr)
    return matches


def matches_to_df(matches: list[Match]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": m.symbol,
            "weekly": m.weekly.direction,
            "h4": m.h4.direction,
            "h4_time": fmt_time(m.h4_ts, "%m-%d %H:%M %Z"),
            "aligned": m.aligned,
            "w_range": f"{m.weekly.c1_low:g}–{m.weekly.c1_high:g}",
            "h4_range": f"{m.h4.c1_low:g}–{m.h4.c1_high:g}",
            "h4_close": f"{m.h4.c2_close:g}",
        }
        for m in matches
    )


def run_once(source, symbols, require_align, out=None, tg=None, seen_path=None,
             latest_only=False, window=1):
    stamp = datetime.now(tz=DISPLAY_TZ).strftime("%Y-%m-%d %H:%M %Z")
    win_txt = f" — fenêtre {window} bougie(s)" if window > 1 else ""
    print(f"\n=== Scan CRT W+H4 — {len(symbols)} symboles — source {source.name}{win_txt} — {stamp} ===")
    matches = scan(source, symbols, require_align, latest_only, window)

    if not matches:
        print("Aucun setup CRT W+H4 pour le moment.")
        return

    df = matches_to_df(matches)
    pd.set_option("display.max_rows", None)
    pd.set_option("display.width", 200)
    print(f"\n{len(df)} setup(s) valide(s) :\n")
    print(df.to_string(index=False))

    if out:
        df.to_csv(out, index=False)
        print(f"\n💾 exporté → {out}")

    if tg:
        sent = send_new_alerts(matches, tg, seen_path)
        print(f"📨 {sent} nouvelle(s) alerte(s) Telegram "
              f"({len(matches) - sent} déjà notifiée(s)).")


def send_new_alerts(matches: list["Match"], tg, seen_path) -> int:
    """Envoie sur Telegram les setups pas encore notifiés (dédup). Renvoie le nb envoyé."""
    token, chat_id = tg
    seen = load_seen(seen_path)
    now_iso = datetime.now(timezone.utc).isoformat()
    sent = 0
    for m in matches:
        key = alert_key(m)
        if key in seen:
            continue
        if send_telegram(token, chat_id, format_alert(m)):
            seen[key] = now_iso
            sent += 1
    save_seen(seen_path, seen)
    return sent


# ===========================================================================
# 6b) Dashboard web (visuel) — lib standard, aucune dépendance
# ===========================================================================
_DIR = {"bull": ("bull", "LONG"), "bear": ("bear", "SHORT"), "both": ("both", "OUTSIDE")}


_DASH_CSS = """
:root{--ground:#0b0e15;--surface:#131826;--surface-2:#1b2233;--line:#242d40;
--text:#e7eaf0;--muted:#8891a4;--accent:#e3b15c;--bull:#2ebd85;--bear:#e8585e;--both:#98a1b3}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--ground);color:var(--text);font-family:'Archivo',system-ui,-apple-system,Segoe UI,sans-serif;-webkit-font-smoothing:antialiased;padding:0 20px 48px;line-height:1.5}
.wrap{max-width:1180px;margin:0 auto}
.sr-only{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0,0,0,0)}
.topbar{position:sticky;top:0;z-index:5;display:flex;align-items:center;gap:14px;flex-wrap:wrap;padding:16px 0 15px;margin-bottom:22px;background:var(--ground);border-bottom:1px solid var(--line)}
.brand{display:flex;align-items:center;gap:11px}
.logo{width:36px;height:36px;border-radius:10px;background:linear-gradient(150deg,#e6bd6a,#c89a42);color:#1c1407;font-weight:800;font-size:13px;display:grid;place-items:center;letter-spacing:-.3px}
.name{font-weight:700;font-size:16px;letter-spacing:-.2px}
.sub{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.16em;margin-top:1px}
.live{margin-left:auto;display:flex;align-items:center;gap:8px;color:var(--muted);font-size:12.5px;font-family:'IBM Plex Mono',ui-monospace,monospace}
.pip{width:8px;height:8px;border-radius:50%;background:var(--bull);animation:pulse 2.4s infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(46,189,133,.5)}70%{box-shadow:0 0 0 7px rgba(46,189,133,0)}100%{box-shadow:0 0 0 0 rgba(46,189,133,0)}}
.summary{display:flex;align-items:flex-end;gap:26px;flex-wrap:wrap;margin-bottom:24px}
.bignum{font-size:52px;font-weight:800;letter-spacing:-2px;line-height:.85;font-variant-numeric:tabular-nums}
.bignum span{font-size:14px;font-weight:500;color:var(--muted);letter-spacing:0;margin-left:10px}
.tallies{display:flex;gap:8px;flex-wrap:wrap}
.tally{display:flex;align-items:center;gap:7px;background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:7px 12px;font-size:13px;color:var(--muted);font-family:'IBM Plex Mono',ui-monospace,monospace}
.tally b{color:var(--text);font-weight:600}
.sw{width:9px;height:9px;border-radius:3px}
.sw.bull{background:var(--bull)}.sw.bear{background:var(--bear)}.sw.both{background:var(--both)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px}
.card{position:relative;background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:15px 16px 7px;overflow:hidden;transition:transform .15s ease,border-color .15s ease}
.card:hover{transform:translateY(-2px);border-color:#34415c}
.card::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px}
.card.bull::before{background:var(--bull)}.card.bear::before{background:var(--bear)}.card.both::before{background:var(--both)}
.chead{display:flex;align-items:center;gap:10px;margin-bottom:3px;padding-left:7px}
.wsub{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:11px;color:var(--muted);padding-left:7px;margin-bottom:4px}
.wsub b{color:#c2c8d4;font-weight:500}
.pair{font-size:19px;font-weight:700;letter-spacing:-.3px}
.wpill{font-size:10px;font-weight:700;letter-spacing:.09em;padding:4px 9px;border-radius:20px;text-transform:uppercase}
.wpill.bull{background:rgba(46,189,133,.14);color:var(--bull)}
.wpill.bear{background:rgba(232,88,94,.14);color:var(--bear)}
.wpill.both{background:rgba(152,161,179,.14);color:var(--both)}
.count{margin-left:auto;font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:12px;color:var(--muted);background:var(--surface-2);border-radius:8px;padding:3px 9px}
.row{display:flex;align-items:center;gap:10px;padding:8px 7px;border-top:1px solid rgba(255,255,255,.045)}
.row.fresh{background:linear-gradient(90deg,rgba(227,177,92,.07),transparent)}
.tag{font-size:11px;font-weight:700;letter-spacing:.04em;width:64px;flex:none;display:flex;align-items:center;gap:7px}
.tag .dot{width:7px;height:7px;border-radius:50%;flex:none}
.tag.bull{color:var(--bull)}.tag.bull .dot{background:var(--bull)}
.tag.bear{color:var(--bear)}.tag.bear .dot{background:var(--bear)}
.tag.both{color:var(--both)}.tag.both .dot{background:var(--both)}
.when{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:13px;color:#ccd2dd;font-variant-numeric:tabular-nums}
.grp{display:flex;align-items:center;gap:8px;margin:0 7px;padding:11px 0 4px;border-top:1px solid rgba(255,255,255,.07)}
.grp.first{border-top:none;padding-top:4px}
.gchip{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:10.5px;font-weight:600;padding:2px 7px;border-radius:6px;background:var(--surface-2);color:var(--muted)}
.gchip.ext{background:rgba(227,177,92,.14);color:var(--accent)}
.glabel{font-size:10px;text-transform:uppercase;letter-spacing:.11em;color:var(--muted)}
.gcount{margin-left:auto;font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:11px;color:var(--muted)}
.grp + .row{border-top:none}
.empty{grid-column:1/-1;color:var(--muted);padding:60px;text-align:center;background:var(--surface);border:1px solid var(--line);border-radius:14px}
.foot{margin-top:26px;padding-top:16px;border-top:1px solid var(--line);color:#5d6678;font-size:12px;display:flex;gap:18px;flex-wrap:wrap;font-family:'IBM Plex Mono',ui-monospace,monospace}
@media (prefers-reduced-motion:reduce){.pip{animation:none}.card{transition:none}}
@media (max-width:520px){.bignum{font-size:42px}body{padding:0 14px 40px}}
"""


def render_dashboard(matches, stamp, window, source_name, refresh_s=60) -> str:
    by_sym: dict[str, list] = {}
    for m in matches:
        by_sym.setdefault(m.symbol, []).append(m)

    tally = {"bear": 0, "bull": 0, "both": 0}
    for m in matches:
        tally[m.h4.direction] = tally.get(m.h4.direction, 0) + 1

    cards = []
    for sym, ms in sorted(by_sym.items()):
        wcls, wlab = _DIR[ms[0].weekly.direction]
        latest_ts = max(m.h4_ts for m in ms)

        groups: dict[int, list] = {}                       # regroupe par modèle (span)
        for m in ms:
            groups.setdefault(m.h4.span, []).append(m)

        blocks = []
        for gi, span in enumerate(sorted(groups)):          # classiques (3C) d'abord
            n = span + 2
            ext = " ext" if span > 1 else ""
            first = " first" if gi == 0 else ""
            gms = sorted(groups[span], key=lambda x: x.h4_ts, reverse=True)
            rows = []
            for m in gms:
                dcls, dlab = _DIR[m.h4.direction]
                dt = datetime.fromtimestamp(m.h4_ts / 1000, tz=DISPLAY_TZ)
                when = f"{_JOURS_FR[dt.weekday()]} {dt:%d/%m · %H:%M}"
                fresh = " fresh" if m.h4_ts == latest_ts else ""
                rows.append(
                    f'<div class="row{fresh}">'
                    f'<span class="tag {dcls}"><span class="dot"></span>{dlab}</span>'
                    f'<span class="when">{html.escape(when)}</span></div>'
                )
            blocks.append(
                f'<div class="grp{first}"><span class="gchip{ext}">{n}C</span>'
                f'<span class="glabel">modèle {n} bougies</span>'
                f'<span class="gcount">{len(gms)}</span></div>{"".join(rows)}'
            )

        clean = html.escape(sym.split(":")[-1])
        wdt = datetime.fromtimestamp(ms[0].weekly_ts / 1000, tz=DISPLAY_TZ)
        cards.append(
            f'<div class="card {wcls}"><div class="chead">'
            f'<span class="pair">{clean}</span>'
            f'<span class="wpill {wcls}">Weekly {wlab}</span>'
            f'<span class="count">{len(ms)}</span></div>'
            f'<div class="wsub">manipulation weekly · <b>sem. du {wdt:%d/%m}</b></div>'
            f'{"".join(blocks)}</div>'
        )

    grid = "".join(cards) or '<div class="empty">Aucun setup CRT Weekly + H4 pour le moment.</div>'
    win_txt = f"fenêtre {window} bougies" if window > 1 else "modèle strict"

    def _tchip(cls, lab, n):
        return f'<span class="tally"><span class="sw {cls}"></span><b>{n}</b> {lab}</span>'

    tallies = (_tchip("bear", "short", tally.get("bear", 0))
               + _tchip("bull", "long", tally.get("bull", 0))
               + _tchip("both", "outside", tally.get("both", 0)))

    return (
        '<!doctype html><html lang="fr"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<meta http-equiv="refresh" content="{refresh_s}">'
        '<title>CRT Scanner · Weekly + H4</title>'
        '<link rel="preconnect" href="https://fonts.googleapis.com">'
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
        'family=Archivo:wght@500;600;700;800&family=IBM+Plex+Mono:wght@400;500;600&display=swap">'
        f'<style>{_DASH_CSS}</style></head><body><div class="wrap">'
        f'<h2 class="sr-only">CRT Scanner : {len(matches)} setups Weekly+H4 alignés sur {len(by_sym)} paires.</h2>'
        '<div class="topbar"><div class="brand"><div class="logo">CRT</div>'
        '<div><div class="name">Scanner</div><div class="sub">Weekly · H4 confluence</div></div></div>'
        f'<div class="live"><span class="pip"></span>maj {html.escape(stamp)}</div></div>'
        f'<div class="summary"><div class="bignum">{len(matches)}<span>setups alignés</span></div>'
        f'<div class="tallies">{tallies}</div></div>'
        f'<div class="grid">{grid}</div>'
        f'<div class="foot"><span>&#8635; auto {refresh_s}s</span><span>{html.escape(win_txt)}</span>'
        f'<span>source {html.escape(source_name)}</span><span>{len(by_sym)} paires</span></div>'
        '</div></body></html>'
    )


def serve_dashboard(source, symbols, require_align, window, port, tg,
                    seen_path, interval_min, latest_only=False):
    state = {"matches": [], "stamp": "scan en cours…", "error": None}

    def loop():
        while True:
            try:
                ms = scan(source, symbols, require_align, latest_only, window)
                state["matches"] = ms
                state["stamp"] = datetime.now(tz=DISPLAY_TZ).strftime("%Y-%m-%d %H:%M %Z")
                state["error"] = None
                if tg:
                    send_new_alerts(ms, tg, seen_path)
            except Exception as exc:                       # un scan raté ne tue pas le serveur
                state["error"] = str(exc)
                print(f"[serve] scan erreur : {exc}", file=sys.stderr)
            time.sleep(max(1, interval_min) * 60)

    threading.Thread(target=loop, daemon=True).start()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            page = render_dashboard(state["matches"], state["stamp"], window,
                                    source.name, refresh_s=60)
            body = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):                          # silence les logs HTTP
            pass

    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"📊 Dashboard en ligne : http://localhost:{port}  (Ctrl+C pour arrêter)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nArrêt.")


# ===========================================================================
# 7) CLI
# ===========================================================================
def resolve_telegram(args):
    token = args.telegram_token or os.environ.get("TELEGRAM_TOKEN")
    chat_id = args.telegram_chat or os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise SystemExit(
            "Telegram demandé mais identifiants manquants.\n"
            "Définis TELEGRAM_TOKEN et TELEGRAM_CHAT_ID, ou --telegram-token / --telegram-chat."
        )
    return token, chat_id


def build_source(args) -> Source:
    if args.source == "ccxt":
        return CCXTSource(args.exchange, args.quote, args.top)
    user = args.tv_user or os.environ.get("TV_USERNAME")
    pwd = args.tv_pass or os.environ.get("TV_PASSWORD")
    return TradingViewSource(user, pwd, args.tv_exchange, args.source_tz)


def main() -> None:
    p = argparse.ArgumentParser(
        description="Scanner CRT 3 Candle Model — Weekly + H4, alertes Telegram.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--source", choices=["tradingview", "ccxt"], default="tradingview",
                   help="source de données (défaut : tradingview)")
    p.add_argument("--symbols", nargs="+",
                   help="symboles à scanner. TradingView : 'OANDA:EURUSD'. ccxt : 'BTC/USDT'.")
    p.add_argument("--align", action="store_true", help="exige le même biais en Weekly et H4")
    p.add_argument("--latest-only", action="store_true",
                   help="ne teste que le dernier H4 clôturé (défaut : tous les H4 de la semaine)")
    p.add_argument("--crt-window", type=int, default=1, metavar="N",
                   help="nb de bougies en arrière où chercher la bougie 'range' pour le "
                        "sweep+reclaim (1 = modèle strict 2 bougies ; ex. 5 = multi-bougies)")
    p.add_argument("-o", "--out", help="exporte les setups en CSV")
    p.add_argument("--html", metavar="PATH",
                   help="génère un dashboard HTML (snapshot) puis quitte")
    p.add_argument("--serve", type=int, metavar="PORT", nargs="?", const=8000,
                   help="lance le dashboard web auto-actualisé sur le PORT (défaut 8000), "
                        "rescanne en boucle (voir --watch pour l'intervalle)")
    p.add_argument("--watch", type=int, metavar="MIN", help="relance le scan toutes les MIN minutes")
    # TradingView
    p.add_argument("--tv-exchange", default="OANDA",
                   help="broker TradingView par défaut pour les symboles sans préfixe (défaut : OANDA)")
    p.add_argument("--tv-user", help="identifiant TradingView (sinon TV_USERNAME)")
    p.add_argument("--tv-pass", help="mot de passe TradingView (sinon TV_PASSWORD)")
    p.add_argument("--source-tz", default=None,
                   help="fuseau dans lequel tvDatafeed renvoie ses timestamps "
                        "(défaut : fuseau local de la machine). Ex : 'UTC', 'Europe/Paris'.")
    p.add_argument("--tz", default="Europe/Paris",
                   help="fuseau d'affichage des heures dans les alertes et le tableau "
                        "(défaut : Europe/Paris). Ex : 'UTC'.")
    # ccxt
    p.add_argument("--exchange", default="binance", help="exchange ccxt (défaut : binance)")
    p.add_argument("--quote", default="USDT", help="devise de cotation ccxt (défaut : USDT)")
    p.add_argument("--top", type=int, default=50, help="nb de paires ccxt par volume (défaut : 50)")
    # Telegram
    p.add_argument("--telegram", action="store_true", help="active les alertes Telegram")
    p.add_argument("--telegram-token", help="token du bot (sinon TELEGRAM_TOKEN)")
    p.add_argument("--telegram-chat", help="chat id (sinon TELEGRAM_CHAT_ID)")
    p.add_argument("--seen-file", default=".crt_seen.json", help="fichier de dédup des alertes")
    p.add_argument("--test-telegram", action="store_true", help="envoie un message de test et quitte")
    args = p.parse_args()

    global DISPLAY_TZ
    DISPLAY_TZ = ZoneInfo(args.tz)

    if args.test_telegram:
        token, chat_id = resolve_telegram(args)
        ok = send_telegram(token, chat_id, "✅ Test CRT scanner — connexion Telegram OK.")
        print("Envoyé." if ok else "Échec — vérifie token / chat_id.")
        return

    tg = resolve_telegram(args) if args.telegram else None
    source = build_source(args)
    symbols = args.symbols or source.default_symbols()

    # Dashboard HTML (snapshot) : un scan, on écrit le fichier, alertes éventuelles, puis quitte.
    # Idéal serverless (GitHub Actions) : un seul passage fait dashboard + Telegram.
    if args.html:
        stamp = datetime.now(tz=DISPLAY_TZ).strftime("%Y-%m-%d %H:%M %Z")
        matches = scan(source, symbols, args.align, args.latest_only, args.crt_window)
        with open(args.html, "w", encoding="utf-8") as f:
            f.write(render_dashboard(matches, stamp, args.crt_window, source.name))
        msg = f"📊 dashboard écrit → {args.html}  ({len(matches)} setup(s))"
        if tg:
            sent = send_new_alerts(matches, tg, args.seen_file)
            msg += f" · 📨 {sent} nouvelle(s) alerte(s)"
        print(msg)
        return

    # Dashboard web auto-actualisé (tourne en continu, idéal 24/7).
    if args.serve:
        serve_dashboard(source, symbols, args.align, args.crt_window, args.serve, tg,
                        args.seen_file, args.watch or 30, args.latest_only)
        return

    if not args.watch:
        run_once(source, symbols, args.align, args.out, tg, args.seen_file,
                 args.latest_only, args.crt_window)
        return

    try:
        while True:
            run_once(source, symbols, args.align, args.out, tg, args.seen_file,
                     args.latest_only, args.crt_window)
            print(f"\n⏳ prochain scan dans {args.watch} min…")
            time.sleep(args.watch * 60)
    except KeyboardInterrupt:
        print("\nArrêt.")


if __name__ == "__main__":
    main()
