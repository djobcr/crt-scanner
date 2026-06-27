# CRT Scanner - Weekly + H4 (Candle Range Theory)

Scanner du **CRT 3 Candle Model** avec confluence multi-timeframes **Weekly + H4**
et alertes **Telegram**. Conçu pour les majeures du forex, l'or (XAU) et BTC, en
lisant les **mêmes bougies que les charts TradingView**.

---

## 1. Le setup recherché

Sur un timeframe donné, on évalue les deux dernières bougies **clôturées** :

- **Bougie 1** = avant-dernière bougie clôturée (le "range").
- **Bougie 2** = dernière bougie clôturée (la "manipulation").

Le CRT est validé quand la **bougie 2** :

1. a **sweep** la bougie 1 : sa mèche dépasse un extrême de la bougie 1
   (`low2 < low1` = sweep du bas, biais **haussier** ; `high2 > high1` = sweep du
   haut, biais **baissier**),
2. **PUIS clôture à l'intérieur du range** de la bougie 1 (`low1 <= close2 <= high1`).

Cas particulier : si la bougie 2 balaie les deux côtés et clôture dans le range
(outside bar), le biais est marqué `both` et reste compatible avec les deux sens.

### Confluence Weekly + H4

Un setup n'est valide que si la condition est réunie **en Weekly ET en H4**.

- Étape 1 : la dernière weekly clôturée forme un CRT (le biais de la semaine).
- Étape 2 : un H4 de la **semaine en cours** forme un CRT.

Le scanner balaie **tous les H4 clôturés de la semaine en cours** à chaque
exécution : tu es alerté de chaque CRT H4 de la semaine, pas seulement du dernier,
et rien n'est raté même après une coupure du script.

---

## 2. Architecture

- **Détection CRT** (`crt_at`, `detect_crt`) : pur calcul sur des listes
  OHLC, totalement indépendant de la source de données.
- **Sources de données interchangeables** (classe `Source`) :
  - `tradingview` (défaut) : via `tvDatafeed`, bougies identiques aux charts
    TradingView. Idéal pour le CRT. Non officiel, peut casser.
  - `ccxt` : exchanges crypto uniquement (BTC, ETH... en /USDT).
- **Dédup persistante** (`.crt_seen.json`) : chaque alerte est identifiée par
  `symbole + bougie weekly + bougie H4`. Tu reçois chaque setup une seule fois ;
  une nouvelle bougie qui reforme un CRT génère une nouvelle alerte. Purge auto
  au-delà de 30 jours.
- **Alertes Telegram** : via la lib standard de Python (aucune dépendance).

---

## 3. Installation

```bash
pip install -r requirements.txt
```

(`requirements.txt` installe `pandas` et `tradingview-datafeed` ; décommenter
`ccxt` pour la source crypto.)

---

## 4. Utilisation

```bash
# Majeures forex + XAUUSD + BTC, bougies TradingView (broker OANDA par défaut)
python crt_scanner.py

# Avec alertes Telegram, en boucle toutes les 30 min
python crt_scanner.py --telegram --watch 30

# Liste de symboles explicite (notation BROKER:SYMBOLE)
python crt_scanner.py --symbols OANDA:EURUSD OANDA:GBPUSD OANDA:XAUUSD BINANCE:BTCUSDT

# Autre broker par défaut pour les symboles sans préfixe
python crt_scanner.py --tv-exchange FOREXCOM

# Session TradingView loggée (débloque plus de symboles)
python crt_scanner.py --tv-user MONID --tv-pass MONMDP

# Exiger le même biais en Weekly et H4
python crt_scanner.py --align

# Crypto pur via ccxt
python crt_scanner.py --source ccxt --top 50 --telegram
```

Options principales : `--source`, `--symbols`, `--align`, `--latest-only`,
`--watch MIN`, `--out fichier.csv`, `--tv-exchange`, `--tv-user/--tv-pass`,
`--tz-offset-hours`, `--telegram`, `--test-telegram`.

---

## 5. Telegram (une seule fois)

1. Créer un bot avec **@BotFather**, récupérer le **TOKEN**.
2. Envoyer un message au bot, puis ouvrir
   `https://api.telegram.org/bot<TOKEN>/getUpdates` pour lire le **chat_id**.
3. Exporter les identifiants :
   ```bash
   export TELEGRAM_TOKEN="123456:ABC..."
   export TELEGRAM_CHAT_ID="123456789"
   python crt_scanner.py --test-telegram   # vérifie la connexion
   ```

---

## 6. Déploiement

- **Local** : pour tester et recevoir les premières alertes (tourne tant que la
  machine est allumée).
- **VPS + systemd** (recommandé pour TradingView) : `deploy/crt-scanner.service`.
  tvDatafeed se logge une seule fois depuis une IP stable, ce qui évite les
  blocages. Toutes les commandes d'install sont en commentaire dans le fichier.
- **GitHub Actions** : `deploy/crt.yml` (à placer dans `.github/workflows/`).
  Gratuit et sans serveur, MAIS peu fiable avec tvDatafeed (IP de datacenter
  changeante + re-login -> risque de captcha). Voir l'avertissement dans le
  fichier. Pour du serverless fiable, il faudrait la source Twelve Data.

---

## 7. Points d'attention

- **Le broker compte.** Le CRT dépend du plus-haut/plus-bas exact de la bougie
  précédente, qui varie selon le flux. Utilise le broker depuis lequel tu trades
  réellement (`--tv-exchange` ou préfixe `BROKER:`) pour que les sweeps collent à
  tes charts.
- **tvDatafeed sans login** limite certains symboles, et les données peuvent être
  différées. Ajoute `--tv-user/--tv-pass` (ou `TV_USERNAME`/`TV_PASSWORD`) si
  besoin.
- **Fuseau horaire** : les timestamps tvDatafeed sont supposés UTC. La fenêtre
  "semaine en cours" reste correcte quoi qu'il arrive (weekly et H4 viennent du
  même flux). Si l'heure affichée dans les alertes est décalée, corrige avec
  `--tz-offset-hours`.
- **tvDatafeed est non officiel** : si ça casse, l'adaptateur Twelve Data (API
  officielle) se branche au même endroit (classe `Source`) sans toucher à la
  logique CRT.

---

## 8. Décisions de conception (récap)

- **Pourquoi pas le screener TradingView officiel ?** Son endpoint filtre
  l'état courant d'un symbole (RSI maintenant, prix vs EMA...). Il n'expose pas
  les OHLC de la bougie précédente par timeframe, donc il ne peut pas exprimer
  "la bougie 2 a sweep la bougie 1 puis clôturé dans son range" sur deux
  timeframes. On récupère donc les OHLC et on calcule le CRT nous-mêmes.
- **Pourquoi pas ccxt pour le forex ?** ccxt ne couvre que les exchanges crypto.
  EUR/USD, XAU/USD, etc. n'y existent pas. D'où la source TradingView.
- **Pourquoi tvDatafeed plutôt qu'une API officielle ?** Pour un CRT
  discrétionnaire, on veut que le scanner voie exactement les mêmes bougies que
  les charts tradés. tvDatafeed lit le flux TradingView. Compromis : non officiel,
  fragile. Twelve Data reste l'alternative officielle si fiabilité 24/7 prioritaire.
- **Pourquoi balayer toute la semaine en H4ŧ?** Une version qui ne regarde que le
  dernier H4 clôturé raterait les CRT H4 passés dans la semaine si le script
  n'est pas lancé au bon moment. Le balayage complet + la dédup garantissent
  "tous les CRT H4 de la semaine, chacun une fois".

---

## 9. Contenu du dossier

```
screener-tradingview/
├── README.md                 # ce fichier
├── crt_scanner.py            # le scanner
├── requirements.txt          # dépendances
└── deploy/
    ├── crt-scanner.service   # service systemd pour VPS (recommandé)
    └── crt.yml               # workflow GitHub Actions (-> .github/workflows/)
```
