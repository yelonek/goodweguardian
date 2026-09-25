"""Historia dashboardu: ts z lokalnego czasu w linii logu, nie UTC prefiksu."""

from __future__ import annotations

from guardian_dashboard import _attach_slot4_followups, _parse_dashboard_line


def _dash(clock: str, body: str) -> str:
    return f"2026-09-25 {clock} [INFO] dashboard | {clock} | {body}"


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
    assert row.fields["slot4_write"] is None


def test_history_reads_explicit_slot4_write() -> None:
    line = (
        "2026-09-25 08:42:00 [INFO] dashboard | 08:42:00 | balans_godz=+2.050 kWh "
        "(Δexp−Δimp; Δimp=0.000 Δexp=2.050) | moc_bilans=+6.833 kW | "
        "sieć=+4.20 kW | PV=4.55 kW | dom=369 W | SOC=19% | P_bat=+74 W | "
        "do_końca=1080s | slot_bal=True inny_eco=False ecoslot_read%=1 | "
        "próg=0.30 kW | interwen=True | export_pv_surplus | cmd=On +1% 1080s | slot4=hold"
    )
    row = _parse_dashboard_line(line)
    assert row is not None
    assert row.fields["slot4_write"] == "hold"
    assert row.fields["reason"] == "export_pv_surplus"


def test_history_pairs_slot_hold_with_older_dashboard_line() -> None:
    lines = [
        _dash(
            "08:42:00",
            "balans_godz=+2.050 kWh (Δexp−Δimp; Δimp=0.000 Δexp=2.050) | "
            "moc_bilans=+6.833 kW | sieć=+4.20 kW | PV=4.55 kW | dom=369 W | "
            "SOC=19% | P_bat=+74 W | do_końca=1080s | slot_bal=True inny_eco=False "
            "ecoslot_read%=1 | próg=0.30 kW | interwen=True | export_pv_surplus | "
            "cmd=On +1% 1080s",
        ),
        "2026-09-25 08:42:00 [INFO] slot_hold slot=eco_mode_4 reason=export_pv_surplus power=+1 soc=100 end=08:59",
        _dash(
            "08:43:00",
            "balans_godz=+0.000 kWh (Δexp−Δimp; Δimp=0.000 Δexp=0.000) | "
            "moc_bilans=+0.000 kW | sieć=+0.00 kW | PV=0.00 kW | dom=400 W | "
            "SOC=19% | P_bat=+0 W | do_końca=1020s | slot_bal=False inny_eco=False "
            "ecoslot_read%=0 | próg=0.30 kW | interwen=False | within_band | "
            "cmd=Off +0% 0s",
        ),
        _dash(
            "08:44:00",
            "balans_godz=+0.000 kWh (Δexp−Δimp; Δimp=0.000 Δexp=0.000) | "
            "moc_bilans=+0.000 kW | sieć=+0.00 kW | PV=0.00 kW | dom=400 W | "
            "SOC=19% | P_bat=+0 W | do_końca=960s | slot_bal=True inny_eco=False "
            "ecoslot_read%=-1 | próg=0.30 kW | interwen=True | import_grid "
            "[control_off:override] | cmd=Off +0% 0s",
        ),
        _dash(
            "08:45:00",
            "balans_godz=+0.000 kWh (Δexp−Δimp; Δimp=0.000 Δexp=0.000) | "
            "moc_bilans=+0.000 kW | sieć=+0.00 kW | PV=0.00 kW | dom=400 W | "
            "SOC=19% | P_bat=+0 W | do_końca=900s | slot_bal=False inny_eco=False "
            "ecoslot_read%=-1 | próg=0.30 kW | interwen=True | import_grid | "
            "cmd=On -1% 900s",
        ),
        "2026-09-25 08:45:01 [ERROR] ecoslot write failed | slot=eco_mode_4 error=timeout",
        _dash(
            "08:46:00",
            "balans_godz=+0.000 kWh (Δexp−Δimp; Δimp=0.000 Δexp=0.000) | "
            "moc_bilans=+0.000 kW | sieć=+0.00 kW | PV=0.00 kW | dom=400 W | "
            "SOC=19% | P_bat=+0 W | do_końca=840s | slot_bal=True inny_eco=False "
            "ecoslot_read%=-1 | próg=0.30 kW | interwen=False | within_band | "
            "cmd=Off +0% 0s",
        ),
    ]
    rows = _attach_slot4_followups(lines)
    assert [r.fields["slot4_write"] for r in rows] == ["hold", "none", "none", "fail", "sent"]
