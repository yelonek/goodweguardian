## Cel projektu (GoodWeGuardian)

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


## Jak diagnozować zachowanie (praktycznie)

- Patrz na parę:
  - `balans_godz` (energia w godzinie) + `moc_bilans` (wymagana moc domknięcia),
  - `cmd=...` (co guardian kazał),
  - `ecoslot_read%` i `P_bat` w kolejnej minucie (czy GoodWe wykonał).
- Sprawdzaj, czy watchdog nie łamie „guard kierunku” (np. `remaining<0` i `cmd=On -%`).

