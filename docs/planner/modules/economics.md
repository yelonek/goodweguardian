# economics

**Cashflow w jednej godzinie** (`net_kwh` jak w KPI):

| `net_kwh` | Wzór |
|-----------|------|
| > 0 (eksport netto) | `net_kwh × rce_pln_kwh` |
| < 0 (import netto) | `net_kwh × import_pln_per_kwh` |
| = 0 | `0` |

**Cel optimizera:** max **Σ** `cashflow_pln` po godzinach z cenami.

**Wejście:** `net_kwh`, obie stawki dla tej h.

**Norma:** [PLANNING_SYSTEM.md](../../../PLANNING_SYSTEM.md) §12 pkt 2.
