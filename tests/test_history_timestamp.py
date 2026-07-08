"""Historia dashboardu: ts z lokalnego czasu w linii logu, nie UTC prefiksu."""

from __future__ import annotations

from guardian_dashboard import _parse_dashboard_line


def test_history_ts_uses_embedded_local_time_not_log_prefix_utc() -> None:
    line = (
        "2026-07-08 08:04:00 [INFO] dashboard | 10:04:00 | balans_godz=-0.030 kWh "
        "(Δexp−Δimp; Δimp=0.140 Δexp=0.110) | moc_bilans=-0.032 kW | "
        "sieć=-2.83 kW | PV=3.30 kW | dom=666 W | SOC=56% | P_bat=-5435 W | "
        "do_końca=3360s | slot_bal=True inny_eco=False ecoslot_read%=-60 | "
        "próg=0.30 kW | interwen=True | neutral_pv_soak | cmd=On -60% 3360s"
    )
    row = _parse_dashboard_line(line)
    assert row is not None
    assert row.ts is not None
    assert row.ts.hour == 10
    assert row.ts.minute == 4
    assert row.fields["ts"] == "2026-07-08 10:04:00"
