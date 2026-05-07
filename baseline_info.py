"""Zwięzła specyfikacja baseline modeli (wersjonowana, do porównań i regresji)."""

from __future__ import annotations

import json
from pathlib import Path

BASELINE_VERSION = "2026-05-05"

_REFERENCE_METRICS = (
    Path(__file__).resolve().parent / "reference" / "load_forecast_backtest_baseline.json"
)


def baseline_spec() -> dict:
    """
    Specyfikacja + opcjonalnie zcommitowane metryki referencyjne z pliku JSON.
    """
    spec: dict = {
        "baseline_version": BASELINE_VERSION,
        "load_forecast": {
            "id": "load_hist_median_v1",
            "summary_pl": (
                "Prognoza zużycia: percentyle (p25/p50/p75) z próbek tej samej godziny "
                "z ostatnich N dni; dzień docelowy nie wchodzi do próbek. "
                "Preferencja próbek z tego samego typu dnia (weekday vs weekend); "
                "przy małej liczbie próbek — wszystkie godziny z lookback; przy braku — zero. "
                "Nowcast (opcjonalnie): bias mocy z ostatnich LOAD_NOWCAST_WINDOW_MIN min "
                "względem baseline p50 bieżącej godziny; korekta zanika po LOAD_NOWCAST_DECAY_HOURS."
            ),
            "actual_kwh_per_hour": (
                "Średnia arytmetyczna consumption_w [W] we wszystkich rekordach telemetrii "
                "w danej lokalnej godzinie, podzielona przez 1000 → przybliżone kWh/h."
            ),
            "api_forecast": "GET /api/load-forecast",
            "api_backtest": "GET /api/load-forecast/backtest",
            "cli_backtest": "uv run python load_forecast.py --lookback 28 [--max-days N]",
            "default_lookback_days": 28,
            "reference_metrics_repo_path": "reference/load_forecast_backtest_baseline.json",
        },
        "pv_forecast": {
            "id": "pv_solcast_proxy_hourly_v1",
            "summary_pl": (
                "Prognoza PV z proxy Solcast (/forecasts); sloty 30m; agregacja do godzin "
                "lokalnych (średnia moc kW); pasma p10/p50/p90 jako pv_kw_p10 / pv_kw / pv_kw_p90."
            ),
            "api": "GET /api/pv-forecast",
        },
        "pricing": {
            "id": "rce_g12_effective_import_v1",
            "summary_pl": (
                "RCE godzinowe PLN/kWh (proxy /api/rce lub fallback PSE); "
                "effective import = dystrybucja G12 wg strefy + energia (RCE lub stałe z .env)."
            ),
            "api": "GET /api/pricing/day",
        },
        "kpi_net_billing": {
            "id": "kpi_counter_hourly_net_v1",
            "summary_pl": (
                "Bilans godzinowy między pełnymi godzinami na licznikach E_imp/E_exp "
                "(pierwszy pomiar w H vs pierwszy w H+1). "
                "net_kWh = Δexp − Δimp. "
                "net > 0 → wpływ do depozytu: net × RCE godzinowe; "
                "net < 0 → rachunek: |net| × effective import."
            ),
            "api": "GET /api/kpi/today",
        },
        "how_to_update_reference_metrics": (
            "1) Uruchom: uv run python load_forecast.py --lookback 28 --max-days 30 "
            "2) Skopiuj pole 'results' i metadane do reference/load_forecast_backtest_baseline.json "
            "(git_commit_* z git rev-parse HEAD). 3) Commit. Porównuj kolejne modele tymi samymi parametrami."
        ),
    }
    if _REFERENCE_METRICS.is_file():
        try:
            spec["load_forecast"]["reference_metrics_committed"] = json.loads(
                _REFERENCE_METRICS.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            spec["load_forecast"]["reference_metrics_committed"] = None
            spec["load_forecast"]["reference_metrics_error"] = "nie można odczytać JSON"
    return spec
