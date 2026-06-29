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
    done: bool = False   # target (bord opposé du range) déjà atteinte -> setup consommé


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
        # Sweep évalué sur TOUTE la formation (range exclue -> manipulation incluse) :
        # si les deux extrêmes ont été pris pendant la formation -> double purge -> 'both'.
        seg_high = max(highs[r + 1:i + 1])
        seg_low = min(lows[r + 1:i + 1])
        swept_low = seg_low < c1_low
        swept_high = seg_high > c1_high
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


def crt_all(highs, lows, closes, i: int, window: int = 1) -> list[CRT]:
    """
    TOUS les CRT où la bougie de manipulation est à l'index i : un CRT par bougie
    'range' valide parmi i-1 .. i-window. Chaque distance de range = un modèle
    distinct (span -> 3,4,5… bougies), possiblement de sens différent. Renvoyés
    du modèle le plus court (range proche) au plus long.
    """
    out: list[CRT] = []
    if i < 1 or i >= len(closes):
        return out
    c2_high, c2_low, c2_close = highs[i], lows[i], closes[i]
    stop = max(-1, i - 1 - window)
    for r in range(i - 1, stop, -1):
        c1_high, c1_low = highs[r], lows[r]
        # Sweep évalué sur TOUTE la formation (range exclue -> manipulation incluse) :
        # si les deux extrêmes ont été pris pendant la formation -> double purge -> 'both'.
        seg_high = max(highs[r + 1:i + 1])
        seg_low = min(lows[r + 1:i + 1])
        swept_low = seg_low < c1_low
        swept_high = seg_high > c1_high
        if not (c1_low <= c2_close <= c1_high) or not (swept_low or swept_high):
            continue
        if swept_low and not swept_high:
            d = "bull"
        elif swept_high and not swept_low:
            d = "bear"
        else:
            d = "both"
        out.append(CRT(d, c1_low, c1_high, c2_low, c2_high, c2_close, span=i - r))
    return out


def dir_aligned(a: str, b: str) -> bool:
    """Deux sens sont compatibles si identiques, ou si l'un est 'both' (outside)."""
    return a == b or "both" in (a, b)


def directions_aligned(a: CRT, b: CRT) -> bool:
    return dir_aligned(a.direction, b.direction)


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
    Renvoie (modeles_weekly, weekly_ts, week_start, week_end) ou None.
    modeles_weekly = liste de TOUS les CRT weekly formés par la dernière weekly
    clôturée (un par distance de range -> modèles 3,4,5… bougies), chacun avec son
    propre sens (ils peuvent se contredire). week_start = open de la semaine en cours.
    """
    t, h, l, c = source.ohlcv(symbol, "1w", max(6, window + 2))
    if len(c) < 2:
        return None
    models = crt_all(h, l, c, len(c) - 1, window)
    if not models:
        return None
    weekly_ts = t[-1]
    week_start = weekly_ts + WEEK_MS
    return models, weekly_ts, week_start, week_start + WEEK_MS


# ===========================================================================
# 4) Telegram (lib standard, aucune dépendance)
# ===========================================================================
def tv_url(symbol: str) -> str:
    """Lien direct vers le graphique TradingView du symbole (ex. OANDA:XAUUSD)."""
    return "https://www.tradingview.com/chart/?symbol=" + urllib.parse.quote(symbol, safe="")


def send_telegram(token: str, chat_id: str, text: str, buttons=None) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": "true",
    }
    if buttons:                                   # buttons = [[{"text":..,"url":..}], ...]
        payload["reply_markup"] = json.dumps({"inline_keyboard": buttons})
    data = urllib.parse.urlencode(payload).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=20) as r:
            return r.status == 200
    except Exception as exc:
        print(f"  [telegram] échec d'envoi : {exc}", file=sys.stderr)
        return False


_LABEL = {"bull": ("🟢", "LONG"), "bear": ("🔴", "SHORT"), "both": ("⚪", "OUTSIDE")}


_JOURS_FR = ["lun", "mar", "mer", "jeu", "ven", "sam", "dim"]


def _hit_line(h: "H4Hit", confirmed: list) -> str:
    dt = datetime.fromtimestamp(h.ts / 1000, tz=DISPLAY_TZ)
    when = f"{_JOURS_FR[dt.weekday()]} {dt:%H:%M}"
    sizes = ", ".join(f"{w.span + 2}C" for w in sorted(confirmed, key=lambda c: c.span))
    return f"  • H4 {when} · {h.span + 2}C  <i>(Weekly {sizes})</i>"


def _pair_buttons(symbols):
    """Un bouton URL TradingView par symbole (1 par ligne)."""
    seen, out = set(), []
    for s in symbols:
        if s in seen:
            continue
        seen.add(s)
        out.append([{"text": f"📈 {s.split(':')[-1]}", "url": tv_url(s)}])
    return out


def format_digest(new_items: list) -> tuple:
    """new_items = [(symbol, H4Hit, confirmed)]. Renvoie (texte, boutons) d'un seul message."""
    groups: dict = {}
    order = []
    for symbol, h, confirmed in new_items:
        k = (symbol, h.direction)
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append((h, confirmed))

    n = len(new_items)
    head = f"🔔 <b>{n} nouveau{'x' if n > 1 else ''} setup{'s' if n > 1 else ''} CRT</b>"
    blocks = []
    for symbol, d in order:
        emoji, dlab = _LABEL[d]
        sym = symbol.split(":")[-1]
        lines = "\n".join(_hit_line(h, c) for h, c in
                          sorted(groups[(symbol, d)], key=lambda x: x[0].ts, reverse=True))
        blocks.append(f"{emoji} <b>{sym} · {dlab}</b>\n{lines}")
    text = head + "\n\n" + "\n\n".join(blocks)
    return text, _pair_buttons([s for s, _ in order])


def build_recap(results: list, stamp: str) -> tuple:
    """Récap quotidien : tous les modèles weekly actifs et le nb de confluences H4."""
    head = f"📊 <b>Récap CRT du jour</b>\n<i>{html.escape(stamp)}</i>"
    blocks, syms = [], []
    for res in sorted(results, key=lambda r: r.symbol):
        parts = []
        for w in sorted(res.weekly, key=lambda c: c.span):
            if w.done:                          # target weekly atteinte -> on ignore
                continue
            emoji, dlab = _LABEL[w.direction]
            nh = sum(1 for hh in res.h4s if dir_aligned(w.direction, hh.direction) and not hh.done)
            parts.append(f"{emoji} W{w.span + 2}C {dlab} ({nh} H4)")
        if not parts:
            continue
        blocks.append(f"<b>{res.symbol.split(':')[-1]}</b> · " + " | ".join(parts))
        syms.append(res.symbol)
    text = head + "\n\n" + ("\n".join(blocks) if blocks else "Aucun setup actif aujourd'hui.")
    return text, _pair_buttons(syms)


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


def alert_key(symbol: str, weekly_ts: int, h: "H4Hit") -> str:
    return f"{symbol}|{weekly_ts}|{h.ts}|{h.direction}|{h.span}"


# ===========================================================================
# 6) Scanner
# ===========================================================================
@dataclass
class H4Hit:
    ts: int
    span: int
    direction: str
    target: float = 0.0   # bord opposé du range H4 (cible du trade)
    done: bool = False    # cible déjà touchée par une bougie postérieure


@dataclass
class SymResult:
    symbol: str
    weekly_ts: int
    weekly: list      # list[CRT] : tous les modèles weekly (3,4,5… bougies), sens variés
    h4s: list         # list[H4Hit] : tous les CRT H4 de la semaine en cours


def _in_skip(hour: int, skip) -> bool:
    """True si `hour` (0-23) est dans la plage [start, end) à exclure. Gère minuit (22->7)."""
    if not skip:
        return False
    s, e = skip
    if s == e:
        return False
    return s <= hour < e if s < e else (hour >= s or hour < e)


def scan(source: Source, symbols: list[str], require_align: bool = True,
         latest_only: bool = False, window: int = 1, skip=None) -> list[SymResult]:
    results: list[SymResult] = []
    total = len(symbols)

    for idx, symbol in enumerate(symbols, 1):
        print(f"\r  {idx}/{total}  {symbol:<18}", end="", file=sys.stderr, flush=True)
        try:
            ctx = weekly_context(source, symbol, window)
            if ctx is None:
                continue
            models, weekly_ts, week_start, week_end = ctx
            models = [m for m in models if m.direction != "both"]   # outside weekly = hors plan
            if not models:
                continue

            hts, hh, hl, hc = source.ohlcv(symbol, "4h", 80)
            n = len(hts)

            # Extrêmes de la semaine en cours (TOUTES les bougies, même session exclue) :
            # sert à savoir si la TARGET WEEKLY (bord opposé du range weekly) a été atteinte.
            wk = [i for i in range(n) if week_start <= hts[i] < week_end]
            week_high = max((hh[i] for i in wk), default=None)
            week_low = min((hl[i] for i in wk), default=None)
            for w in models:
                if w.direction == "bull" and week_high is not None and week_high >= w.c1_high:
                    w.done = True
                elif w.direction == "bear" and week_low is not None and week_low <= w.c1_low:
                    w.done = True

            idxs = [i for i in range(1, n) if week_start <= hts[i] < week_end]
            if skip:                                 # retire les H4 de la session exclue
                idxs = [i for i in idxs
                        if not _in_skip(datetime.fromtimestamp(hts[i] / 1000, tz=DISPLAY_TZ).hour, skip)]
            if latest_only and idxs:
                idxs = [idxs[-1]]

            h4s: list[H4Hit] = []
            seen_pair = set()                       # 1 ligne par (bougie H4, sens) : modèle le + court
            for i in idxs:
                for crt in crt_all(hh, hl, hc, i, window):
                    if crt.direction == "both":     # OUTSIDE H4 exclus (sweep des 2 côtés)
                        continue
                    key = (hts[i], crt.direction)
                    if key in seen_pair:
                        continue
                    seen_pair.add(key)
                    # NB : pas de "target H4". La seule cible = le bord opposé du CRT weekly
                    # (géré au niveau du modèle weekly via w.done). Le bord du range H4 n'est
                    # pas un objectif dans la stratégie.
                    h4s.append(H4Hit(hts[i], crt.span, crt.direction))

            results.append(SymResult(symbol, weekly_ts, models, h4s))
        except Exception as exc:
            print(f"\n  [!] {symbol}: {exc}", file=sys.stderr)
            continue

    print("\r" + " " * 42 + "\r", end="", file=sys.stderr)
    return results


def weekly_blocks(res: SymResult):
    """[(modele_weekly, [H4Hit du même sens, récents d'abord]), ...] trié par taille de modèle."""
    blocks = []
    for w in sorted(res.weekly, key=lambda c: c.span):
        hits = sorted([h for h in res.h4s if dir_aligned(w.direction, h.direction)],
                      key=lambda h: h.ts, reverse=True)
        blocks.append((w, hits))
    return blocks


def iter_alerts(results: list[SymResult]):
    """Génère (symbol, weekly_ts, H4Hit, [modèles weekly confirmés]) pour chaque manip H4 alignée."""
    for res in results:
        for h in res.h4s:
            confirmed = [w for w in res.weekly if dir_aligned(w.direction, h.direction)]
            if confirmed:
                yield res.symbol, res.weekly_ts, h, confirmed


def results_to_df(results: list[SymResult]) -> pd.DataFrame:
    rows = []
    for res in results:
        sym = res.symbol.split(":")[-1]
        for w, hits in weekly_blocks(res):
            for h in hits:
                rows.append({
                    "symbol": sym,
                    "weekly": f"{w.span + 2}C {w.direction}",
                    "h4": f"{h.span + 2}C {h.direction}",
                    "h4_time": fmt_time(h.ts, "%m-%d %H:%M %Z"),
                })
    return pd.DataFrame(rows)


def run_once(source, symbols, require_align, out=None, tg=None, seen_path=None,
             latest_only=False, window=1, skip=None):
    stamp = datetime.now(tz=DISPLAY_TZ).strftime("%Y-%m-%d %H:%M %Z")
    win_txt = f" — fenêtre {window} bougie(s)" if window > 1 else ""
    print(f"\n=== Scan CRT Weekly+H4 — {len(symbols)} symboles — source {source.name}{win_txt} — {stamp} ===")
    results = scan(source, symbols, require_align, latest_only, window, skip)

    df = results_to_df(results)
    if df.empty:
        print("Aucune confluence CRT Weekly+H4 pour le moment.")
        return

    pd.set_option("display.max_rows", None)
    pd.set_option("display.width", 200)
    print(f"\n{len(df)} confluence(s) (modèle weekly × manip H4) :\n")
    print(df.to_string(index=False))

    if out:
        df.to_csv(out, index=False)
        print(f"\n💾 exporté → {out}")

    if tg:
        sent, total = send_new_alerts(results, tg, seen_path)
        print(f"📨 {sent} nouvelle(s) alerte(s) Telegram ({total - sent} déjà notifiée(s)).")


def send_new_alerts(results: list["SymResult"], tg, seen_path):
    """Envoie UN message digest groupant les nouvelles manips H4 (dédup). Renvoie (envoyés, total)."""
    token, chat_id = tg
    seen = load_seen(seen_path)
    now_iso = datetime.now(timezone.utc).isoformat()
    new_items, new_keys, total = [], [], 0
    for symbol, weekly_ts, h, confirmed in iter_alerts(results):
        live = [w for w in confirmed if not w.done]
        if h.done or not live:           # target atteinte (H4 ou tous les weekly) -> pas d'alerte
            continue
        total += 1
        key = alert_key(symbol, weekly_ts, h)
        if key in seen:
            continue
        new_items.append((symbol, h, live))
        new_keys.append(key)

    if new_items:
        text, buttons = format_digest(new_items)
        if not send_telegram(token, chat_id, text, buttons):
            return 0, total                       # échec : on ne marque rien comme vu
        for key in new_keys:
            seen[key] = now_iso
    save_seen(seen_path, seen)
    return len(new_items), total


def maybe_send_recap(results, tg, state_path, recap_hour, stamp) -> bool:
    """Envoie le récap quotidien une fois par jour, à partir de recap_hour (fuseau --tz)."""
    if recap_hour is None or not tg:
        return False
    now = datetime.now(tz=DISPLAY_TZ)
    if now.hour < recap_hour:
        return False
    state = load_json(state_path)
    today = now.strftime("%Y-%m-%d")
    if state.get("last_recap") == today:
        return False
    text, buttons = build_recap(results, stamp)
    if send_telegram(tg[0], tg[1], text, buttons):
        state["last_recap"] = today
        save_json(state_path, state)
        return True
    return False


# --- Persistance JSON générique + historique des setups ---------------------
def load_json(path: str) -> dict:
    if not path or not os.path.exists(path):
        return {}
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return {}


def save_json(path: str, obj) -> None:
    if path:
        json.dump(obj, open(path, "w", encoding="utf-8"))


def update_history(hist: dict, results: list, now_ms: int):
    """Ajoute les confluences inédites à l'historique (clé unique), purge au-delà de 30 j."""
    new = 0
    for symbol, weekly_ts, h, confirmed in iter_alerts(results):
        key = f"{symbol}|{weekly_ts}|{h.ts}|{h.direction}|{h.span}"
        if key not in hist:
            hist[key] = {
                "symbol": symbol, "h4_ts": h.ts, "dir": h.direction, "span": h.span,
                "sizes": sorted(w.span + 2 for w in confirmed), "first_seen": now_ms,
            }
            new += 1
    cutoff = now_ms - 30 * 86400 * 1000
    hist = {k: v for k, v in hist.items() if v.get("h4_ts", 0) >= cutoff}
    return hist, new


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
.card{position:relative;background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:15px 16px 9px;transition:transform .15s ease,border-color .15s ease}
.card:hover{transform:translateY(-2px);border-color:#34415c}
.chead{display:flex;align-items:center;gap:10px;margin-bottom:3px;padding-left:2px}
.wsub{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:11px;color:var(--muted);padding-left:2px;margin-bottom:10px}
.wsub b{color:#c2c8d4;font-weight:500}
.pair{font-size:19px;font-weight:700;letter-spacing:-.3px}
.wpill{font-size:10px;font-weight:700;letter-spacing:.09em;padding:4px 9px;border-radius:20px;text-transform:uppercase}
.wpill.bull{background:rgba(46,189,133,.14);color:var(--bull)}
.wpill.bear{background:rgba(232,88,94,.14);color:var(--bear)}
.wpill.both{background:rgba(152,161,179,.14);color:var(--both)}
.count{margin-left:auto;font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:12px;color:var(--muted);background:var(--surface-2);border-radius:8px;padding:3px 9px}
.wmodel{margin-bottom:10px;border-left:3px solid var(--line);padding:1px 0 1px 10px}
.wmodel.bull{border-left-color:var(--bull)}
.wmodel.bear{border-left-color:var(--bear)}
.wmodel.both{border-left-color:var(--both)}
.wmhead{display:flex;align-items:center;gap:8px;padding:2px 2px 5px}
.gchip{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:10.5px;font-weight:600;padding:2px 7px;border-radius:6px;background:var(--surface-2);color:var(--muted)}
.gchip.ext{background:rgba(227,177,92,.14);color:var(--accent)}
.glabel{font-size:10px;text-transform:uppercase;letter-spacing:.11em;color:var(--muted)}
.wmdir{display:flex;align-items:center;gap:5px;font-size:10.5px;font-weight:700;letter-spacing:.05em}
.wmdir .dot{width:7px;height:7px;border-radius:50%}
.wmdir.bull{color:var(--bull)}.wmdir.bull .dot{background:var(--bull)}
.wmdir.bear{color:var(--bear)}.wmdir.bear .dot{background:var(--bear)}
.wmdir.both{color:var(--both)}.wmdir.both .dot{background:var(--both)}
.gcount{margin-left:auto;font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:11px;color:var(--muted)}
.row{display:flex;align-items:center;gap:10px;padding:6px 2px}
.row.fresh{background:linear-gradient(90deg,rgba(227,177,92,.09),transparent);border-radius:6px}
.tag{font-size:11px;font-weight:700;letter-spacing:.04em;width:60px;flex:none;display:flex;align-items:center;gap:7px}
.tag .dot{width:7px;height:7px;border-radius:50%;flex:none}
.tag.bull{color:var(--bull)}.tag.bull .dot{background:var(--bull)}
.tag.bear{color:var(--bear)}.tag.bear .dot{background:var(--bear)}
.tag.both{color:var(--both)}.tag.both .dot{background:var(--both)}
.when{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:13px;color:#ccd2dd;font-variant-numeric:tabular-nums}
.hsize{margin-left:auto;font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:11px;color:var(--muted);background:var(--surface-2);padding:1px 7px;border-radius:6px}
.hsize.ext{background:rgba(227,177,92,.13);color:var(--accent)}
.noh4{padding:2px 2px 6px;color:#5d6678;font-size:12px;font-style:italic}
.wmodel.done{opacity:.42}
.row.done{opacity:.5}
.row.done .when{text-decoration:line-through}
.tgt{margin-left:6px;font-size:11px;flex:none}
.donetag{margin-left:8px;font-size:9px;font-weight:700;letter-spacing:.04em;color:var(--accent);background:rgba(227,177,92,.14);padding:2px 6px;border-radius:5px;white-space:nowrap}
.empty{grid-column:1/-1;color:var(--muted);padding:60px;text-align:center;background:var(--surface);border:1px solid var(--line);border-radius:14px}
.foot{margin-top:26px;padding-top:16px;border-top:1px solid var(--line);color:#5d6678;font-size:12px;display:flex;gap:18px;flex-wrap:wrap;font-family:'IBM Plex Mono',ui-monospace,monospace}
.hdot{width:8px;height:8px;border-radius:50%;background:var(--both);flex:none}
.live.ok .hdot{background:var(--bull)}
.live.warn .hdot{background:var(--accent)}
.live.bad .hdot{background:var(--bear)}
.live.bad{color:var(--bear)}
.nav{display:flex;gap:7px;margin-left:14px}
.navlink{font-size:12px;color:var(--muted);text-decoration:none;border:1px solid var(--line);padding:5px 11px;border-radius:9px}
.navlink:hover{color:var(--text);border-color:#34415c}
.navlink.active{color:var(--accent);border-color:rgba(227,177,92,.4)}
a.pair{text-decoration:none;color:var(--text)}
a.pair:hover{color:var(--accent)}
a:focus-visible,.navlink:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.hday{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:11px;text-transform:uppercase;letter-spacing:.12em;color:var(--muted);margin:20px 2px 6px;padding-top:14px;border-top:1px solid var(--line)}
.hday:first-of-type{border-top:none;padding-top:0;margin-top:4px}
.hrow{display:flex;align-items:center;gap:10px;padding:7px 2px;border-bottom:1px solid rgba(255,255,255,.04)}
.hsym{font-weight:700;font-size:13px;width:80px;flex:none;text-decoration:none;color:var(--text)}
.hsym:hover{color:var(--accent)}
.htime{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:13px;color:#ccd2dd;font-variant-numeric:tabular-nums}
.hwk{margin-left:auto;font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:11px;color:var(--muted)}
@media (prefers-reduced-motion:reduce){.pip{animation:none}.card{transition:none}}
@media (max-width:520px){.bignum{font-size:42px}body{padding:0 14px 40px}}
"""


def render_dashboard(results, stamp, window, source_name, refresh_s=60, skip=None, gen_ms=0) -> str:
    results = sorted(results, key=lambda r: r.symbol)

    wt = {"bear": 0, "bull": 0, "both": 0}                  # modèles weekly par sens
    for res in results:
        for w in res.weekly:
            wt[w.direction] = wt.get(w.direction, 0) + 1

    cards = []
    for res in results:
        latest_ts = max((h.ts for h in res.h4s), default=0)
        blocks = []
        for w in sorted(res.weekly, key=lambda c: c.span):
            wcls, wlab = _DIR[w.direction]
            wn = w.span + 2
            wext = " ext" if w.span > 1 else ""
            wdone = " done" if w.done else ""
            hits = sorted([h for h in res.h4s if dir_aligned(w.direction, h.direction)],
                          key=lambda h: h.ts, reverse=True)
            rows = []
            for h in hits:
                dcls, dlab = _DIR[h.direction]
                dt = datetime.fromtimestamp(h.ts / 1000, tz=DISPLAY_TZ)
                when = f"{_JOURS_FR[dt.weekday()]} {dt:%d/%m · %H:%M}"
                rdone = h.done or w.done
                fresh = " fresh" if (h.ts == latest_ts and not rdone) else ""
                hext = " ext" if h.span > 1 else ""
                mark = '<span class="tgt" title="target atteinte">🎯</span>' if rdone else ""
                rows.append(
                    f'<div class="row{fresh}{" done" if rdone else ""}">'
                    f'<span class="tag {dcls}"><span class="dot"></span>{dlab}</span>'
                    f'<span class="when">{html.escape(when)}</span>'
                    f'<span class="hsize{hext}">{h.span + 2}C</span>{mark}</div>'
                )
            body = "".join(rows) or '<div class="noh4">aucune confluence H4 pour l\'instant</div>'
            done_tag = '<span class="donetag">🎯 target atteinte</span>' if w.done else ""
            blocks.append(
                f'<div class="wmodel {wcls}{wdone}"><div class="wmhead">'
                f'<span class="gchip{wext}">{wn}C</span>'
                f'<span class="glabel">weekly {wn} bougies</span>'
                f'<span class="wmdir {wcls}"><span class="dot"></span>{wlab}</span>'
                f'<span class="gcount">{len(hits)}</span>{done_tag}</div>{body}</div>'
            )

        clean = html.escape(res.symbol.split(":")[-1])
        wdt = datetime.fromtimestamp(res.weekly_ts / 1000, tz=DISPLAY_TZ)
        cards.append(
            f'<div class="card"><div class="chead">'
            f'<a class="pair" href="{tv_url(res.symbol)}" target="_blank" rel="noopener">{clean} ↗</a>'
            f'<span class="count">{len(res.weekly)} modèles</span></div>'
            f'<div class="wsub">manipulation weekly · <b>sem. du {wdt:%d/%m}</b></div>'
            f'{"".join(blocks)}</div>'
        )

    grid = "".join(cards) or '<div class="empty">Aucun CRT weekly pour le moment.</div>'
    win_txt = f"fenêtre {window} bougies" if window > 1 else "modèle strict"
    skip_txt = f"<span>session {skip[0]:02d}h–{skip[1]:02d}h exclue</span>" if skip else ""
    npairs = len(results)
    nmodels = sum(len(r.weekly) for r in results)

    def _tchip(cls, lab, n):
        return f'<span class="tally"><span class="sw {cls}"></span><b>{n}</b> {lab}</span>'

    tallies = (_tchip("bear", "short", wt.get("bear", 0))
               + _tchip("bull", "long", wt.get("bull", 0))
               + _tchip("both", "outside", wt.get("both", 0)))

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
        f'<h2 class="sr-only">CRT Scanner : {nmodels} modèles weekly sur {npairs} paires.</h2>'
        '<div class="topbar"><div class="brand"><div class="logo">CRT</div>'
        '<div><div class="name">Scanner</div><div class="sub">Weekly · H4 confluence</div></div></div>'
        '<div class="nav"><a class="navlink active" href="index.html">Dashboard</a>'
        '<a class="navlink" href="history.html">Historique</a></div>'
        f'<div class="live" id="live"><span class="hdot"></span><span id="health">maj {html.escape(stamp)}</span></div></div>'
        f'<div class="summary"><div class="bignum">{npairs}<span>paires actives</span></div>'
        f'<div class="tallies">{tallies}</div></div>'
        f'<div class="grid">{grid}</div>'
        f'<div class="foot"><span>&#8635; auto {refresh_s}s</span><span>{html.escape(win_txt)}</span>'
        f'{skip_txt}<span>{nmodels} modèles weekly</span><span>source {html.escape(source_name)}</span></div>'
        '</div>'
        f'<script>(function(){{var g={gen_ms};var L=document.getElementById("live"),H=document.getElementById("health");'
        'function u(){var m=Math.floor((Date.now()-g)/60000);'
        'H.textContent=m<1?"à l\\u2019instant":"il y a "+m+" min";'
        'L.className="live "+(m<35?"ok":(m<70?"warn":"bad"));}'
        'if(g>0){u();setInterval(u,30000);}else{L.className="live ok";}})();</script>'
        '</body></html>'
    )


def render_history(hist: dict, stamp, source_name, refresh_s=300) -> str:
    recs = sorted(hist.values(), key=lambda r: r["h4_ts"], reverse=True)
    days: dict = {}
    for r in recs:
        dt = datetime.fromtimestamp(r["h4_ts"] / 1000, tz=DISPLAY_TZ)
        days.setdefault(dt.strftime("%Y-%m-%d"), []).append((r, dt))

    sections = []
    for dkey in sorted(days, reverse=True):
        ddt = days[dkey][0][1]
        label = f"{_JOURS_FR[ddt.weekday()]} {ddt:%d/%m/%Y}"
        rows = []
        for r, dt in days[dkey]:
            dcls, dlab = _DIR[r["dir"]]
            sym = html.escape(r["symbol"].split(":")[-1])
            hn = r["span"] + 2
            hext = " ext" if r["span"] > 1 else ""
            wsz = ", ".join(f"{n}C" for n in r["sizes"])
            rows.append(
                f'<div class="hrow"><span class="tag {dcls}"><span class="dot"></span>{dlab}</span>'
                f'<a class="hsym" href="{tv_url(r["symbol"])}" target="_blank" rel="noopener">{sym}</a>'
                f'<span class="htime">{dt:%H:%M}</span>'
                f'<span class="hsize{hext}">{hn}C</span>'
                f'<span class="hwk">Weekly {wsz}</span></div>'
            )
        sections.append(f'<div class="hday">{label} · {len(days[dkey])}</div>{"".join(rows)}')

    body = "".join(sections) or '<div class="empty">Historique vide pour le moment.</div>'
    return (
        '<!doctype html><html lang="fr"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<meta http-equiv="refresh" content="{refresh_s}">'
        '<title>CRT Scanner · Historique</title>'
        '<link rel="preconnect" href="https://fonts.googleapis.com">'
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
        'family=Archivo:wght@500;600;700;800&family=IBM+Plex+Mono:wght@400;500;600&display=swap">'
        f'<style>{_DASH_CSS}</style></head><body><div class="wrap">'
        f'<h2 class="sr-only">Historique CRT : {len(recs)} setups passés.</h2>'
        '<div class="topbar"><div class="brand"><div class="logo">CRT</div>'
        '<div><div class="name">Scanner</div><div class="sub">Historique des setups</div></div></div>'
        '<div class="nav"><a class="navlink" href="index.html">Dashboard</a>'
        '<a class="navlink active" href="history.html">Historique</a></div>'
        f'<div class="live"><span class="hdot"></span>maj {html.escape(stamp)}</div></div>'
        f'<div class="summary"><div class="bignum">{len(recs)}<span>setups archivés (30 j)</span></div></div>'
        f'<div>{body}</div>'
        f'<div class="foot"><span>{len(days)} jours</span><span>source {html.escape(source_name)}</span></div>'
        '</div></body></html>'
    )


def serve_dashboard(source, symbols, require_align, window, port, tg,
                    seen_path, interval_min, latest_only=False, skip=None,
                    history_file=None, state_file=None, recap_hour=None):
    state = {"matches": [], "stamp": "scan en cours…", "hist": {}, "gen_ms": 0}

    def loop():
        while True:
            try:
                ms = scan(source, symbols, require_align, latest_only, window, skip)
                now_ms = int(time.time() * 1000)
                state["matches"] = ms
                state["gen_ms"] = now_ms
                state["stamp"] = datetime.now(tz=DISPLAY_TZ).strftime("%Y-%m-%d %H:%M %Z")
                hist, _ = update_history(load_json(history_file), ms, now_ms)
                save_json(history_file, hist)
                state["hist"] = hist
                if tg:
                    send_new_alerts(ms, tg, seen_path)
                    maybe_send_recap(ms, tg, state_file, recap_hour, state["stamp"])
            except Exception as exc:                       # un scan raté ne tue pas le serveur
                print(f"[serve] scan erreur : {exc}", file=sys.stderr)
            time.sleep(max(1, interval_min) * 60)

    threading.Thread(target=loop, daemon=True).start()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?")[0]
            if path.startswith("/history"):
                page = render_history(state["hist"], state["stamp"], source.name, refresh_s=60)
            else:
                page = render_dashboard(state["matches"], state["stamp"], window,
                                        source.name, refresh_s=60, skip=skip, gen_ms=state["gen_ms"])
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
    p.add_argument("--skip-session", type=int, nargs=2, metavar=("DEBUT", "FIN"), default=None,
                   help="exclut les bougies H4 ouvrant dans [DEBUT,FIN) (heures, fuseau --tz). "
                        "Gère le passage de minuit. Ex : --skip-session 22 7 (session asiatique).")
    p.add_argument("--history-file", default=".crt_history.json",
                   help="fichier d'historique des setups (pour la page Historique)")
    p.add_argument("--state-file", default=".crt_state.json",
                   help="fichier d'état (date du dernier récap quotidien…)")
    p.add_argument("--daily-recap", type=int, metavar="HEURE", default=None,
                   help="envoie un récap Telegram 1x/jour à partir de cette heure (fuseau --tz). Ex : --daily-recap 8")
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
    skip = tuple(args.skip_session) if args.skip_session else None

    if args.html:
        now_ms = int(time.time() * 1000)
        stamp = datetime.now(tz=DISPLAY_TZ).strftime("%Y-%m-%d %H:%M %Z")
        results = scan(source, symbols, args.align, args.latest_only, args.crt_window, skip)
        with open(args.html, "w", encoding="utf-8") as f:
            f.write(render_dashboard(results, stamp, args.crt_window, source.name,
                                     skip=skip, gen_ms=now_ms))
        # Historique : page history.html à côté du dashboard
        hist, n_new = update_history(load_json(args.history_file), results, now_ms)
        save_json(args.history_file, hist)
        hist_path = os.path.join(os.path.dirname(args.html) or ".", "history.html")
        with open(hist_path, "w", encoding="utf-8") as f:
            f.write(render_history(hist, stamp, source.name))
        nmodels = sum(len(r.weekly) for r in results)
        msg = (f"📊 dashboard + historique écrits ({len(results)} paires · {nmodels} modèles · "
               f"{n_new} nouveau(x) en historique)")
        if tg:
            sent, _ = send_new_alerts(results, tg, args.seen_file)
            if maybe_send_recap(results, tg, args.state_file, args.daily_recap, stamp):
                msg += " · 🗞️ récap envoyé"
            msg += f" · 📨 {sent} alerte(s)"
        print(msg)
        return

    # Dashboard web auto-actualisé (tourne en continu, idéal 24/7).
    if args.serve:
        serve_dashboard(source, symbols, args.align, args.crt_window, args.serve, tg,
                        args.seen_file, args.watch or 30, args.latest_only, skip,
                        args.history_file, args.state_file, args.daily_recap)
        return

    if not args.watch:
        run_once(source, symbols, args.align, args.out, tg, args.seen_file,
                 args.latest_only, args.crt_window, skip)
        return

    try:
        while True:
            run_once(source, symbols, args.align, args.out, tg, args.seen_file,
                     args.latest_only, args.crt_window, skip)
            print(f"\n⏳ prochain scan dans {args.watch} min…")
            time.sleep(args.watch * 60)
    except KeyboardInterrupt:
        print("\nArrêt.")


if __name__ == "__main__":
    main()
