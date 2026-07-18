"""
tests/test_stop_loss_rn025.py

Test de régression structurel pour RN-025 (mise en conformité du
moteur de risque avec RN-024).

bot_gateio.py est un script exécutable au niveau module (appels
réseau, lecture de sys.argv) — non importable proprement dans des
tests. Ce fichier vérifie donc statiquement, sur le code source, que
le mécanisme retiré ne peut pas être silencieusement réintroduit, et
que le mécanisme qui doit rester actif est toujours présent.

Portée strictement respectée :
  - Ne réévalue aucun calcul (pnl_pct, drawdown, etc.) : ces calculs
    restent dans bot_gateio.py, hors de portée de ces tests.
  - Vérifie uniquement des propriétés structurelles du code source :
    absence du STOP_LOSS PnL supprimé, présence du STOP_LOSS drawdown
    conservé, cohérence avec le principe du préambule de RN-025 (les
    mécanismes de protection ne consultent que le domaine economic).
"""

from pathlib import Path

BOT_GATEIO_SOURCE = Path(__file__).parent.parent / "bot_gateio.py"


def _read_source() -> str:
    return BOT_GATEIO_SOURCE.read_text(encoding="utf-8")


class TestPnlStopLossIsRemoved:
    def test_global_stop_loss_pnl_constant_is_gone(self):
        source = _read_source()
        assert "GLOBAL_STOP_LOSS_PNL =" not in source

    def test_pnl_stop_loss_log_message_is_gone(self):
        source = _read_source()
        assert "STOP-LOSS (PnL total)" not in source

    def test_no_conditional_break_compares_pnl_pct_to_a_threshold(self):
        # Garde-fou large : aucune condition de la forme
        # "pnl_pct < ..." suivie d'un arrêt (break) ne doit subsister.
        # On vérifie l'absence du motif exact qui existait avant RN-025.
        source = _read_source()
        assert "if pnl_pct < " not in source


class TestDrawdownStopLossStillPresent:
    def test_global_stop_loss_dd_constant_is_present(self):
        source = _read_source()
        assert "GLOBAL_STOP_LOSS_DD = 0.25" in source

    def test_drawdown_stop_loss_log_message_is_present(self):
        source = _read_source()
        assert "STOP-LOSS (drawdown)" in source

    def test_drawdown_condition_is_present(self):
        source = _read_source()
        assert "if drawdown_dd >= GLOBAL_STOP_LOSS_DD:" in source


class TestPnlPctIsReportingOnly:
    def test_pnl_pct_is_still_computed_for_reporting(self):
        # RN-025 : pnl_pct reste calcule et journalise a titre de
        # reporting, il n'est simplement plus utilise pour declencher
        # un arret.
        source = _read_source()
        assert 'pnl_pct = capital_view["pnl_pct"]' in source

    def test_pnl_pct_is_still_passed_to_the_stop_loss_journal_entry(self):
        # Le journal continue de recevoir pnl_pct (a titre informatif)
        # pour l'unique cause de stop-loss restante (drawdown).
        source = _read_source()
        assert "pnl_pct=pnl_pct," in source
