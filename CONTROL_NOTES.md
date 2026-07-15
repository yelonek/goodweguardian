## Cel projektu (GoodWeGuardian)

### Skąd brać ustawienia (jedna hierarchia)

| Co chcesz zmienić | Gdzie | Uwaga |
|-------------------|--------|--------|
| **Włączyć / wyłączyć zapisy do inwertera** (`set_ecoslot`) | Albo **`.env`** → `GUARDIAN_CONTROL_ENABLED`, albo **plik** `state/guardian_control_override.json` z `{"control_enabled": true}` / `false` | **Jeśli plik override istnieje i ma `control_enabled` — zawsze on wygrywa** (czytany co cykl). Dashboard i API tylko **zapisują ten sam plik** — to nie jest trzeci przełącznik. Żeby znów obowiązywało tylko `.env`, **usuń** plik override (lub usuń z niego klucz i napraw JSON — prościej skasować plik). Zmiana `.env` wymaga **restartu** procesu. |
| **Pokrętła strojenia** (SOC defense, sufit mocy przy niskim SOC, soak, nowcast, korekta PV, ceny/taryfa, ekonomia planera) | **Tylko dashboard → Ustawienia / onboarding** (zapis do `state/settings.json`) | **Jedno źródło prawdy = `settings.json`.** `.env` NIE jest już warstwą strojenia. Pętla Guardiana czyta **na żywo** (bez restartu) — patrz sekcja niżej. |
| **Telemetria** | **`TELEMETRY_ENABLED` w `.env`** | **Restart**. |
| **Klucz do API dashboardu** | **`GUARDIAN_API_KEY` w `.env`** | Bez klucza endpointy kontroli/zapisu zwracają 401/503. |

W logu / telemetrii (przełączniki control/planner): `source=override` = decyzja z pliku JSON; `source=env` = brak (lub nieużywalny) override, używane `GUARDIAN_CONTROL_ENABLED`.

### System ustawień strojenia (`guardian_settings.py`)

Jeden typowany schemat (`GuardianSettings`, Pydantic) obejmuje wszystkie pokrętła strojenia.
**Jedno źródło prawdy to `state/settings.json`.** Wartość efektywną daje `get_settings()` —
dwie warstwy, od najniższej:

1. **default** — bezpieczna wartość ze schematu (start / fresh install),
2. **`state/settings.json` → `overrides`** — jedyne miejsce, gdzie trafiają zmiany z UI/onboardingu.

**Czysty podział:** każda zmienna jest ustawiana **albo** w `.env` (infrastruktura/sekrety),
**albo** w `settings.json` przez dashboard (strojenie) — nigdy w obu. `.env` nie wpływa na
pola tego schematu; strojenia nie ma w `.env` ani w `guardian_config`.

- **Hot-reload:** `get_settings()` przelicza wynik po zmianie mtime `settings.json`.
  Pętla Guardiana (`hourly_balance_run`) czyta ustawienia **co cykl** — zmiana z dashboardu
  działa bez restartu.
- **Planer** działa jako świeży proces (cron) — czyta bieżące `settings.json` przy każdym uruchomieniu.
  Podgląd prognoz/planu w dashboardzie odświeża część pokręteł strojenia dopiero po restarcie uvicorn,
  ale sterowanie na inwerterze (pętla) i autorytatywny plan (cron) są aktualne.
- **API:** `GET /api/settings` (publiczne — schemat + wartości + `sources` + `onboarding_completed`),
  `PUT /api/settings` (`{"overrides": {...}, "complete_onboarding": bool}`, wymaga `X-Guardian-Api-Key`),
  `DELETE /api/settings[?keys=a,b]` (reset override do wartości domyślnej). `PUT` z wartością `null`
  usuwa dany override. `source` pola to `override` (z `settings.json`) albo `default` (schemat).
- **Onboarding:** przy pierwszym starcie (brak/`onboarding_completed=false` w `settings.json`) dashboard
  pokazuje kreator z domyślnymi wartościami i opisami; zapis ustawia marker `onboarding_completed`.
- **Watchdog SOC** (rezerwa nocna, progi low/full) to teraz **6 pól tego schematu**; endpointy
  `/api/guardian/watchdog-soc` to cienki widok nad `settings.json` (`guardian_watchdog_override.py`).
- **Poza schematem (zostają w `.env`, wymaga restartu):** `INVERTER_IP`, `GUARDIAN_API_KEY`,
  `*_PROXY_BASE_URL`, `TESLA_WC_HOST`, `P_INVERTER`/`P_BATTERY`, ścieżki, `TELEMETRY_*`,
  przełączniki operacyjne `GUARDIAN_CONTROL_ENABLED` / `PLANNER_EXECUTION_ENABLED`. Szablon: `.env.example`.

---

Guardian to warstwa „supervisor / watchdog” nad inwerterem GoodWe:

- **Domyślnie**: pozwala GoodWe działać samodzielnie (guardian **nie interweniuje**).
- **Interwencja**: tylko gdy GoodWe „zawiedzie” względem celów bilansu i/lub ekonomii.
- **Powrót do normy**: po korekcie guardian **oddaje sterowanie** (slot balansujący Off/0%).


## Założenia i konwencje znaków

- **Energia w godzinie**: `remaining_kwh = ΔE_export − ΔE_import`
  - `remaining_kwh > 0`: w tej godzinie oddano więcej energii do sieci niż pobrano.
  - `remaining_kwh < 0`: w tej godzinie pobrano więcej energii z sieci niż oddano.
- **Moc sieci** (`grid_w` / `sieć`): dodatnie = eksport do sieci, ujemne = import z sieci.
- **Moc baterii** (`P_bat`): dodatnie = rozładowanie, ujemne = ładowanie (zgodnie z `pbattery1`).


## „Fakty GoodWe” (ważne nieliniowości / zachowanie EcoSlot)

Użytkownik obserwuje i traktuje jako stały fakt:

- **EcoSlot jest sterowaniem trybem, nie „czystym setpointem W”**.
- Przypadki graniczne:
  - **`CHARGE 1%`**: bateria ładuje się „jakby” **całym PV + ~70W**, czyli PV jest kierowane do baterii, a `1%` jest tylko „dodatkiem”.
  - **`DISCHARGE 1%`**: do sieci trafia „jakby” **PV + ~70W − zużycie domu**.
- Dodatkowe ograniczenie zaobserwowane w praktyce:
  - Przy **SOC ≥ 90%** maksymalne rzeczywiste ładowanie baterii bywa ograniczone do ok. **2.1 kW** (nawet przy wyższym `CHARGE%`).
- W praktyce łatwo o sytuacje, gdzie sterowanie małe w % powoduje duży efekt (bo PV „dokleja się” do trybu).

Wniosek: regulacja musi brać pod uwagę, że **zmiana znaku trybu (charge↔discharge) jest kosztowna** i może powodować duże skoki.


## Cele sterowania (priorytety)

- **Minimalizacja zbędnych cykli baterii**:
  - Unikać bezsensownego przełączania `ŁADUJ ↔ ROZŁADUJ` co minutę.
  - Unikać „ładowanie i rozładowywanie bez potrzeby”, bo to pogarsza efektywność.
- **Ekonomia taryf**:
  - Import w drogiej taryfie jest bardzo kosztowny (np. 1.10 PLN/kWh).
  - Eksport jest mniej opłacalny (np. dynamicznie ~0.40 PLN/kWh).
  - Z tego powodu preferowane jest bycie na **lekkim plusie eksportu** (mniejsza strata niż ryzyko zakupu).
- **Efektywność energetyczna**:
  - Preferowane jest ładowanie baterii **bezpośrednio z PV** (DC→DC), a nie przez sieć (AC→DC).
  - Sieć jest traktowana jako „bufor” w trakcie godziny; nie ma potrzeby panikować wcześnie.


## Polityka „watchdog” (docelowe zachowanie)

Zasada nadrzędna: **guardian nie steruje, jeśli nie musi**.

- **Early window**: w większości czasu godziny guardian milczy (sieć jako bufor).
- **Late window**: w końcówce godziny można domykać bilans.
- **Emergency**:
  - nie tylko na podstawie chwilowego importu,
  - ale też na podstawie tego, czy bilans energii jest już **„nie do odrobienia”** w samej końcówce (limit mocy baterii).

Guard kierunku (aby uniknąć „naprawy w złą stronę”):

- jeśli `remaining_kwh < 0` (energia netto import): watchdog **nie może** komenderować `CHARGE`
- jeśli `remaining_kwh > 0` (energia netto eksport): watchdog **nie może** komenderować `DISCHARGE`


## Typowe bolączki / objawy w logach

- **Nieliniowość PV + tryb**:
  - „Za duże wysterowanie” mimo małego celu (np. 1%).
  - Wynika z faktu, że PV „dokleja się” do charge/discharge.
- **Przestrzelenie przez czas**:
  - Slot ustawiony na kilka minut powoduje duże overshoot, bo PV/dom zmienia się szybko.
  - Preferowane krótkie okna ustawienia (np. 1–2 min) i korekta w kolejnych cyklach.
- **Mylące readback vs command**:
  - W jednej linii loga trzeba rozróżnić:
    - `ecoslot_read%` = stan odczytany przed zapisem,
    - `cmd=...` = polecenie, które guardian wysyła teraz.


## Telemetria i zdalne wyłączenie sterowania

Skrót hierarchii włączenia zapisów: tabela **„Skąd brać ustawienia”** na górze dokumentu.

- **Telemetria:** co cykl `hourly_balance_run` dopisuje linię JSON do `data/telemetry/telemetry_YYYY-MM-DD.jsonl` (data wg `TELEMETRY_TZ`, domyślnie `Europe/Warsaw`). Wyłączenie: `TELEMETRY_ENABLED=0` w `.env` (wymaga restartu procesu).
- **Sterowanie inwerterem:** domyślnie `GUARDIAN_CONTROL_ENABLED=1` w `.env`. Jeśli istnieje plik **`state/guardian_control_override.json`** z `{"control_enabled": true|false}`, ma pierwszeństwo (bez restartu po zapisie pliku). Ścieżkę można nadpisać: `GUARDIAN_CONTROL_OVERRIDE_PATH`.
- **Dashboard API:** przy ustawionym `GUARDIAN_API_KEY` w `.env` — `GET/PUT /api/guardian/control` z nagłówkiem `X-Guardian-Api-Key`. `PUT` **nadpisuje ten sam plik** co ręczna edycja `guardian_control_override.json`. Bez klucza API zwraca 503. Strona główna dashboardu ma prosty formularz (klucz w localStorage).
- **Docker:** zamontuj **ten sam** katalog `state/` do kontenera z pętlą guardiana i do kontenera z uvicorn, żeby `PUT` z dashboardu był widoczny w runnerze.
- **Powrót wyłącznie do .env:** usuń plik override. Dopóki plik istnieje, ma pierwszeństwo nad `.env` — ustawienie w dashboardzie „zgodne z .env” nadal trzyma override w pliku.

## Jak diagnozować zachowanie (praktycznie)

- Patrz na parę:
  - `balans_godz` (energia w godzinie) + `moc_bilans` (wymagana moc domknięcia),
  - `cmd=...` (co guardian kazał),
  - `ecoslot_read%` i `P_bat` w kolejnej minucie (czy GoodWe wykonał).
- Sprawdzaj, czy watchdog nie łamie „guard kierunku” (np. `remaining<0` i `cmd=On -%`).


## Obrona niskiego SOC (sufit mocy)

Przy niskim SOC (domyślnie **10%–20%**) wszystkie decyzje **DISCHARGE** Guardiana mają liniowy sufit mocy: **70 W przy 10%** → **1000 W przy 20%** (`soc_low_cap_*` w Ustawieniach). Clamp jest centralny (po Flappy / planie / deficit) — w tej strefie **nie** podbijamy do mocy domu; reszta idzie z sieci/PV. Uzasadnienie: duży prąd przy podłodze LFP obniża napięcie ogniwa; BMS wymusza doładowanie do ~15% (~0,5 kWh straty). Powyżej 20% SOC — bez tego limitu (tylko inwerter/bateria). Nie ma już osobnej strategii trybu (soak / load-cover / hold) przy niskim SOC.

