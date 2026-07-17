#!/usr/bin/env python3
"""
AUDIT BOT GRID - V4
Analyse complète du patrimoine vs stratégie Hold.
PnL Binance incrémental via curseur last_trade_id → ne relit jamais les vieux trades.
"""
import os
import sys
import json
from datetime import datetime
from binance.client import Client
from dotenv import load_dotenv

load_dotenv()

PAIRES = {
    "INJ":  {"symbol": "INJUSDC",  "asset": "INJ"},
    "EGLD": {"symbol": "EGLDUSDC", "asset": "EGLD"}
}

SNAPSHOT_FILE = "snapshot_t0.json"
PNL_CACHE     = "pnl_cache.json"   # curseur incrémental + cumuls
EXPORT_HTML   = True

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def get_snapshot() -> dict:
    with open(SNAPSHOT_FILE) as f:
        return json.load(f)

def load_bot_state(symbol: str) -> dict:
    sf = f"state_{symbol.lower()}.json"
    if os.path.exists(sf):
        try:
            with open(sf) as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def get_allocated_usdc() -> float:
    total = 0.0
    for cfg in PAIRES.values():
        total += load_bot_state(cfg["symbol"]).get("capital_usdc", 0.0)
    return total

def fmt_signed(val: float, dec: int = 2) -> str:
    return f"{'+' if val >= 0 else ''}{val:.{dec}f}"

def pct(val: float, ref: float) -> str:
    if ref == 0: return "n/a"
    return f"{fmt_signed(val / ref * 100, 2)}%"

# ─────────────────────────────────────────────
# PNL INCRÉMENTAL — CŒUR DU SYSTÈME
# ─────────────────────────────────────────────

def load_pnl_cache() -> dict:
    """
    Structure :
    {
      "INJUSDC":  { "last_trade_id": 123, "usdc_spent": 0.0,
                    "usdc_gained": 0.0,   "nb_trades": 0 },
      "EGLDUSDC": { ... }
    }
    """
    if os.path.exists(PNL_CACHE):
        try:
            with open(PNL_CACHE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_pnl_cache(cache: dict):
    with open(PNL_CACHE, "w") as f:
        json.dump(cache, f, indent=2)
    print(f"  💾 Cache PnL sauvegardé → {PNL_CACHE}")

def fetch_new_trades(client: Client, symbol: str,
                     cache: dict, t0_ms: int) -> dict:
    """
    Lit uniquement les trades nouveaux depuis le dernier curseur.
    - 1er lancement  : startTime = T0 (scan initial complet depuis T0)
    - Suivants       : fromId = last_trade_id + 1 (incrémental)
    Gère la pagination automatiquement si > 1000 trades nouveaux.
    """
    entry = cache.get(symbol, {
        "last_trade_id": None,
        "usdc_spent":    0.0,
        "usdc_gained":   0.0,
        "nb_trades":     0,
    })

    new_total = 0

    # Paramètre de départ selon présence du curseur
    if entry["last_trade_id"] is None:
        kwargs = {"symbol": symbol, "startTime": t0_ms, "limit": 1000}
        print(f"  🔍 [{symbol}] Premier scan depuis T0...")
    else:
        kwargs = {"symbol": symbol, "fromId": entry["last_trade_id"] + 1, "limit": 1000}

    while True:
        trades = client.get_my_trades(**kwargs)
        if not trades:
            break

        for t in trades:
            qty       = float(t["quoteQty"])
            comm_usdc = float(t["commission"]) if t["commissionAsset"] == "USDC" else 0.0
            if t["isBuyer"]:
                entry["usdc_spent"]  += qty + comm_usdc
            else:
                entry["usdc_gained"] += qty - comm_usdc

        entry["last_trade_id"] = trades[-1]["id"]
        new_total += len(trades)

        # Pagination : s'il y avait exactement 1000 résultats, il peut en exister d'autres
        if len(trades) < 1000:
            break
        kwargs = {"symbol": symbol, "fromId": entry["last_trade_id"] + 1, "limit": 1000}

    entry["nb_trades"] += new_total
    if new_total > 0:
        print(f"  ✅ [{symbol}] +{new_total} nouveaux trades intégrés "
              f"(total cumulé : {entry['nb_trades']})")
    else:
        print(f"  ✅ [{symbol}] Aucun nouveau trade depuis le dernier audit")

    return entry

# ─────────────────────────────────────────────
# ANALYSE PRINCIPALE
# ─────────────────────────────────────────────

def run_audit(client: Client, snapshot: dict) -> dict:
    # Timestamp T0 en millisecondes pour Binance API
    t0_ms = int(datetime.strptime(
        snapshot["date_reference"], "%Y-%m-%d"
    ).timestamp() * 1000)

    # Charger et mettre à jour le cache incrémental
    cache = load_pnl_cache()
    print("\n  📡 Mise à jour incrémentale des trades Binance...")
    for cfg in PAIRES.values():
        cache[cfg["symbol"]] = fetch_new_trades(client, cfg["symbol"], cache, t0_ms)
    save_pnl_cache(cache)

    results = {}
    valeur_crypto_actuelle = 0.0
    valeur_crypto_hold     = 0.0
    pnl_reel_total         = 0.0
    trades_total           = 0

    for name, cfg in PAIRES.items():
        # Solde et prix live Binance
        balance      = client.get_asset_balance(asset=cfg["asset"])
        solde_actuel = float(balance["free"]) + float(balance["locked"])
        prix         = float(client.get_ticker(symbol=cfg["symbol"])["lastPrice"])
        stock_t0     = snapshot[name]["stock"]

        val_actuelle = solde_actuel * prix
        val_hold     = stock_t0    * prix
        delta_tokens = solde_actuel - stock_t0

        # PnL réel depuis Binance (source de vérité)
        c           = cache[cfg["symbol"]]
        pnl_reel    = c["usdc_gained"] - c["usdc_spent"]
        nb_trades   = c["nb_trades"]

        # State du bot (grille)
        state        = load_bot_state(cfg["symbol"])
        grille_prete = state.get("grid_ready", False)
        sell_grid    = state.get("sell_grid",  [])
        buy_grid     = state.get("buy_grid",   [])

        valeur_crypto_actuelle += val_actuelle
        valeur_crypto_hold     += val_hold
        pnl_reel_total         += pnl_reel
        trades_total           += nb_trades

        results[name] = {
            "solde_actuel":  solde_actuel,
            "stock_t0":      stock_t0,
            "delta_tokens":  delta_tokens,
            "prix":          prix,
            "val_actuelle":  val_actuelle,
            "val_hold":      val_hold,
            "usdc_spent":    c["usdc_spent"],
            "usdc_gained":   c["usdc_gained"],
            "pnl_reel":      pnl_reel,
            "nb_trades":     nb_trades,
            "grille_prete":  grille_prete,
            "sell_grid":     sell_grid,
            "buy_grid":      buy_grid,
        }

    # Cash
    cash_actuel      = float(client.get_asset_balance(asset="USDC")["free"])
    cash_t0          = snapshot["CASH"]["usdc"]
    cash_alloue      = get_allocated_usdc()
    cash_pour_calcul = cash_alloue if cash_alloue > 0 else cash_actuel

    # Patrimoines
    patrimoine_actuel     = valeur_crypto_actuelle + cash_pour_calcul
    patrimoine_hold       = valeur_crypto_hold     + cash_t0
    alpha_brut            = patrimoine_actuel - patrimoine_hold
    alpha_latent          = valeur_crypto_actuelle - valeur_crypto_hold
    # Alpha sécurisé corrigé :
    #   pnl_reel_total     = cash encaissé - cash dépensé (négatif si le bot accumule)
    #   delta_tokens_value = valeur actuelle des tokens accumulés ou vendus
    # Les deux ensemble = vrai bilan : cash transformé en tokens valorisés au prix du moment
    delta_tokens_value    = sum(d['delta_tokens'] * d['prix'] for d in results.values())
    alpha_securise        = pnl_reel_total + delta_tokens_value
    ratio_cristallisation = (alpha_securise / alpha_brut * 100) if alpha_brut != 0 else 0.0

    return {
        "date_reference":         snapshot["date_reference"],
        "timestamp":              datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "paires":                 results,
        "cash_actuel":            cash_actuel,
        "cash_alloue":            cash_pour_calcul,
        "cash_t0":                cash_t0,
        "valeur_crypto_actuelle": valeur_crypto_actuelle,
        "valeur_crypto_hold":     valeur_crypto_hold,
        "patrimoine_actuel":      patrimoine_actuel,
        "patrimoine_hold":        patrimoine_hold,
        "alpha_brut":             alpha_brut,
        "alpha_latent":           alpha_latent,
        "alpha_securise":         alpha_securise,
        "ratio_cristallisation":  ratio_cristallisation,
        "pnl_reel_total":         pnl_reel_total,
        "delta_tokens_value":     delta_tokens_value,
        "trades_total":           trades_total,
    }

# ─────────────────────────────────────────────
# AFFICHAGE CONSOLE
# ─────────────────────────────────────────────

def print_report(r: dict):
    W   = 62
    SEP = "═" * W
    sep = "─" * W

    print(f"\n{SEP}")
    print(f"  📊 AUDIT PATRIMOINE GRID BOT — {r['timestamp']}")
    print(f"  📅 Référence T0 : {r['date_reference']}")
    print(SEP)

    for name, d in r["paires"].items():
        status    = "🟢" if d["grille_prete"] else "🔴"
        delta_ico = "▲" if d["delta_tokens"] >= 0 else "▼"
        pnl_ico   = "▲" if d["pnl_reel"] >= 0 else "▼"
        print(f"\n  {status} {name}/USDC  @  {d['prix']:.4f} $")
        print(sep)
        print(f"    Stock T0           : {d['stock_t0']:.4f} tokens")
        print(f"    Stock Actuel       : {d['solde_actuel']:.4f} tokens  "
              f"({delta_ico} {fmt_signed(d['delta_tokens'], 4)})")
        print(f"    Val. Actuelle      : {d['val_actuelle']:.2f} $")
        print(f"    Val. Hold Pure     : {d['val_hold']:.2f} $")
        print(sep)
        print(f"    USDC dépensé       : {d['usdc_spent']:.4f} $")
        print(f"    USDC encaissé      : {d['usdc_gained']:.4f} $")
        print(f"    PnL Réel (Binance) : {pnl_ico} {fmt_signed(d['pnl_reel'], 4)} $  ← source de vérité")
        print(f"    Trades totaux      : {d['nb_trades']}")
        print(f"    Grille BUY / SELL  : {len(d['buy_grid'])} / {len(d['sell_grid'])}")

    print(f"\n{SEP}")
    print(f"  💼 SYNTHÈSE PATRIMOINE")
    print(sep)
    print(f"    Cash USDC T0       : {r['cash_t0']:.2f} $")
    print(f"    Cash USDC Alloué   : {r['cash_alloue']:.2f} $")
    print(f"    Crypto (Actuel)    : {r['valeur_crypto_actuelle']:.2f} $")
    print(f"    Crypto (Hold)      : {r['valeur_crypto_hold']:.2f} $")
    print(sep)
    print(f"    Patrimoine Actuel  : {r['patrimoine_actuel']:.2f} $")
    print(f"    Patrimoine Hold    : {r['patrimoine_hold']:.2f} $")
    print(sep)
    print(f"    ALPHA BRUT         : {fmt_signed(r['alpha_brut'])} $  ({pct(r['alpha_brut'], r['patrimoine_hold'])})")
    print(sep)
    print(f"    Alpha latent       : {fmt_signed(r['alpha_latent'])} $  (prix-dépendant ⚠️)")
    print(f"    Alpha sécurisé     : {fmt_signed(r['alpha_securise'], 4)} $  (PnL cash + tokens valorisés)")
    print(f"      dont PnL cash    : {fmt_signed(r['pnl_reel_total'], 4)} $")
    print(f"      dont Δ tokens    : {fmt_signed(r['delta_tokens_value'], 4)} $")

    c     = r["ratio_cristallisation"]
    ico_c = "🔴" if c < 20 else ("🟡" if c < 50 else "🟢")
    print(f"    Cristallisation    : {ico_c} {c:.1f}%")
    print(sep)
    print(f"    PnL Réel Total     : {fmt_signed(r['pnl_reel_total'], 4)} $")
    print(f"    Trades Totaux      : {r['trades_total']}")
    print(SEP)

# ─────────────────────────────────────────────
# EXPORT HTML
# ─────────────────────────────────────────────

def export_html(r: dict, filename: str = "rapport_audit.html"):
    ab_col  = "#22c55e" if r["alpha_brut"]      >= 0 else "#ef4444"
    as_col  = "#22c55e" if r["alpha_securise"]  >= 0 else "#ef4444"
    c_pct   = min(100, max(0, r["ratio_cristallisation"]))
    c_col   = "#22c55e" if c_pct >= 50 else ("#eab308" if c_pct >= 20 else "#ef4444")

    paires_html = ""
    for name, d in r["paires"].items():
        sdot  = "#22c55e" if d["grille_prete"] else "#ef4444"
        dcol  = "#22c55e" if d["delta_tokens"] >= 0 else "#ef4444"
        pcol  = "#22c55e" if d["pnl_reel"]     >= 0 else "#ef4444"
        paires_html += f"""
        <div class="card">
          <div class="card-header">
            <span class="dot" style="background:{sdot}"></span>
            <span class="pair-name">{name}/USDC</span>
            <span class="price">@ {d['prix']:.4f} $</span>
          </div>
          <div class="grid2">
            <div class="stat"><div class="label">Stock T0</div>
              <div class="value">{d['stock_t0']:.4f}</div></div>
            <div class="stat"><div class="label">Stock Actuel</div>
              <div class="value">{d['solde_actuel']:.4f}
                <span style="color:{dcol};font-size:0.8em">({fmt_signed(d['delta_tokens'],4)})</span>
              </div></div>
            <div class="stat"><div class="label">Val. Actuelle</div>
              <div class="value">{d['val_actuelle']:.2f} $</div></div>
            <div class="stat"><div class="label">Val. Hold</div>
              <div class="value">{d['val_hold']:.2f} $</div></div>
            <div class="stat"><div class="label">USDC Dépensé</div>
              <div class="value">{d['usdc_spent']:.2f} $</div></div>
            <div class="stat"><div class="label">USDC Encaissé</div>
              <div class="value">{d['usdc_gained']:.2f} $</div></div>
            <div class="stat"><div class="label">PnL Réel Binance</div>
              <div class="value" style="color:{pcol}">{fmt_signed(d['pnl_reel'],4)} $</div></div>
            <div class="stat"><div class="label">Trades | BUY/SELL</div>
              <div class="value">{d['nb_trades']} | {len(d['buy_grid'])}/{len(d['sell_grid'])}</div></div>
          </div>
        </div>"""

    html = f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Audit Grid Bot — {r['timestamp']}</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@300;500;700&display=swap');
  :root {{
    --bg:#0b0f1a; --surface:#111827; --border:#1f2937;
    --text:#e2e8f0; --muted:#64748b; --accent:#38bdf8;
  }}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:var(--bg);color:var(--text);font-family:'IBM Plex Sans',sans-serif;padding:2rem;min-height:100vh}}
  h1{{font-size:1.5rem;font-weight:700;color:var(--accent);letter-spacing:.05em}}
  .subtitle{{color:var(--muted);font-size:.85rem;margin-top:.25rem;font-family:'IBM Plex Mono',monospace}}
  .header{{margin-bottom:2rem;border-bottom:1px solid var(--border);padding-bottom:1rem}}
  .cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:1.25rem;margin-bottom:1.5rem}}
  .card{{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:1.25rem}}
  .card-header{{display:flex;align-items:center;gap:.6rem;margin-bottom:1rem}}
  .dot{{width:10px;height:10px;border-radius:50%;flex-shrink:0}}
  .pair-name{{font-family:'IBM Plex Mono',monospace;font-weight:600;font-size:1.05rem}}
  .price{{margin-left:auto;color:var(--muted);font-family:'IBM Plex Mono',monospace;font-size:.85rem}}
  .grid2{{display:grid;grid-template-columns:1fr 1fr;gap:.75rem}}
  .stat{{background:var(--bg);border-radius:8px;padding:.6rem .8rem}}
  .label{{font-size:.72rem;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;margin-bottom:.2rem}}
  .value{{font-family:'IBM Plex Mono',monospace;font-size:.9rem;font-weight:600}}
  .summary{{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:1.5rem}}
  .summary h2{{font-size:.85rem;text-transform:uppercase;letter-spacing:.1em;color:var(--accent);margin-bottom:1rem}}
  .sg{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:1rem}}
  .bs{{text-align:center;padding:1rem;background:var(--bg);border-radius:8px}}
  .bl{{font-size:.72rem;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;margin-bottom:.4rem}}
  .bv{{font-family:'IBM Plex Mono',monospace;font-size:1.3rem;font-weight:700}}
  .ab{{margin-top:1rem;padding:1rem 1.25rem;border-radius:10px;border:1px solid var(--border);
       display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:.5rem}}
  .al{{font-size:.8rem;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}}
  .av{{font-family:'IBM Plex Mono',monospace;font-size:1.6rem;font-weight:700}}
  .ap{{font-family:'IBM Plex Mono',monospace;font-size:.95rem;opacity:.7}}
  footer{{margin-top:1.5rem;text-align:center;color:var(--muted);font-size:.75rem;font-family:'IBM Plex Mono',monospace}}
</style>
</head>
<body>
<div class="header">
  <h1>📊 AUDIT PATRIMOINE — GRID BOT</h1>
  <div class="subtitle">Généré le {r['timestamp']}  ·  Référence T0 : {r['date_reference']}</div>
</div>
<div class="cards">{paires_html}</div>
<div class="summary">
  <h2>💼 Synthèse Patrimoine</h2>
  <div class="sg">
    <div class="bs"><div class="bl">Cash T0</div><div class="bv">{r['cash_t0']:.2f} $</div></div>
    <div class="bs"><div class="bl">Cash Alloué</div><div class="bv">{r['cash_alloue']:.2f} $</div></div>
    <div class="bs"><div class="bl">Patrimoine Actuel</div><div class="bv">{r['patrimoine_actuel']:.2f} $</div></div>
    <div class="bs"><div class="bl">Patrimoine Hold</div><div class="bv">{r['patrimoine_hold']:.2f} $</div></div>
    <div class="bs"><div class="bl">PnL Réel Total</div>
      <div class="bv" style="color:{as_col}">{fmt_signed(r['pnl_reel_total'],4)} $</div></div>
    <div class="bs"><div class="bl">Trades Totaux</div><div class="bv">{r['trades_total']}</div></div>
  </div>
  <div class="ab">
    <div>
      <div class="al">Alpha Brut (vs Hold)</div>
      <div class="av" style="color:{ab_col}">{fmt_signed(r['alpha_brut'])} $</div>
      <div class="ap" style="color:{ab_col}">{pct(r['alpha_brut'], r['patrimoine_hold'])}</div>
    </div>
    <div style="text-align:right">
      <div class="al">Alpha Sécurisé (cash + tokens valorisés)</div>
      <div class="av" style="color:{as_col}">{fmt_signed(r['alpha_securise'],4)} $</div>
    </div>
  </div>
  <div class="ab" style="margin-top:.75rem;flex-direction:column;gap:.75rem">
    <div style="display:flex;justify-content:space-between;flex-wrap:wrap;gap:.5rem">
      <div>
        <div class="al">⚠️ Alpha Latent (prix-dépendant)</div>
        <div class="av" style="color:{ab_col};font-size:1.2rem">{fmt_signed(r['alpha_latent'])} $</div>
      </div>
    </div>
    <div>
      <div class="al">Taux de cristallisation</div>
      <div style="margin-top:.4rem;background:var(--bg);border-radius:6px;height:10px;overflow:hidden">
        <div style="height:100%;width:{c_pct:.1f}%;background:{c_col};transition:width .3s"></div>
      </div>
      <div class="ap" style="margin-top:.3rem">{r['ratio_cristallisation']:.1f}% sécurisé</div>
    </div>
  </div>
</div>
<footer>Grid Bot Audit v4 · PnL source Binance · Curseur incrémental last_trade_id</footer>
</body>
</html>"""

    with open(filename, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  📄 Rapport HTML → {filename}")

# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    try:
        client   = Client(os.getenv("BINANCE_API_KEY"), os.getenv("BINANCE_API_SECRET"))
        snapshot = get_snapshot()
        result   = run_audit(client, snapshot)
        print_report(result)
        if EXPORT_HTML:
            export_html(result)
    except FileNotFoundError as e:
        print(f"❌ Fichier manquant : {e}")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Erreur : {e}")
        raise
