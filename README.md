# nas-monitor

> ⚠️ **Nie jest to gruntownie przetestowana aplikacja produkcyjna.** Projekt
> jest w ciągłej, aktywnej budowie - funkcje bywają dodawane i przerabiane
> z sesji na sesję, część ścieżek (zwłaszcza rzadziej używane poziomy RAID,
> nietypowe konfiguracje dysków) jest sprawdzona tylko testami jednostkowymi
> na przykładowych danych, nie na realnym sprzęcie w każdej możliwej
> kombinacji. Właściciel projektu nie jest programistą - cały kod piszę ja
> (Claude, asystent AI od Anthropicu); on odpowiada za zgłaszanie niedoróbek,
> testowanie na żywym sprzęcie i wymyślanie funkcjonalności. Rób kopie
> zapasowe ważnych danych przed testowaniem operacji dyskowych/RAID i nie
> traktuj tego jako gotowego, w pełni dojrzałego produktu.

Samodzielny web dashboard do zarządzania serwerem plików na Debianie - dyski,
macierze RAID (mdadm), użytkownicy i grupy systemowe, udziały Samby, klucze
SSH, sieć oraz aktualizacje - wszystko z jednego miejsca, bez konieczności
wchodzenia po SSH do codziennych zadań. Pomyślany jako lżejsza, bardziej
przejrzysta alternatywa dla OpenMediaVault: pełne CRUD dla każdego zasobu od
razu (nie tylko podgląd), jawne, świadome decyzje o uprawnieniach (domyślnie
brak dostępu, nie domyślnie otwarte, jak w OMV), i kod, który mówi wprost co
i dlaczego robi zamiast ukrywać logikę za warstwami abstrakcji.

Działa na dowolnym hoście Debian z bezpośrednim dostępem do dysków (bare
metal, VM z passthrough) - mdadm i smartctl nie mają dostępu do surowych
urządzeń blokowych z poziomu unprivileged LXC, więc to narzędzie nie nadaje
się do kontenera bez takiego dostępu.

## Funkcje

**Dyski i macierze RAID**
- Podgląd każdego dysku (S.M.A.R.T., temperatura, stan zdrowia) i każdej
  macierzy mdadm, z paskiem zajętości miejsca.
- Pełne zarządzanie dyskami: formatowanie, czyszczenie (wipe), montowanie,
  odmontowywanie - niezależnie od tego, czy dysk jest aktualnie w użyciu.
- Tworzenie macierzy RAID (0/1/4/5/6/10) z wybranych wolnych dysków -
  **oraz z innych, już istniejących wolnych macierzy** (zagnieżdżony RAID,
  np. RAID1 nad dwoma RAID0 - prawdziwy "RAID 1+0"). Macierz zagnieżdżona
  wewnątrz innej pokazuje się poprawnie wcięta pod swoją macierzą nadrzędną,
  nie jako osobny, mylący wpis.
- Odłączanie pojedynczego dysku od macierzy i naprawa (dodanie zastępczego
  dysku, automatyczny rebuild) - tylko dla poziomów, które faktycznie na to
  pozwalają (RAID1/4/5/6/10). RAID0 i linear nie mają redundancji, więc
  mdadm i tak nigdy by tego nie wykonał - te akcje są tam świadomie
  ukryte zamiast prowadzić donikąd.
- Pełne usuwanie macierzy (`mdadm --stop` + wyczyszczenie superbloków
  wszystkich dysków) - dyski wracają jako czyste, gotowe do ponownego użycia.
- Kolejność sekcji (karty macierzy / tabela zarządzania dyskami) można
  przeciągać względem siebie - zapamiętywane trwale.
- JBOD/linear świadomie nieoferowany do tworzenia (zero redundancji, myląco
  "częściowo bezpieczny" - realnie równie ryzykowny co RAID0). Wykrywanie
  istniejącego JBOD (np. odziedziczonego z innego systemu) działa normalnie.

**Użytkownicy i grupy**
- Pełne CRUD dla kont systemowych + dostępu SMB (osobne hasła, świadomie
  rozdzielone - konto systemowe i dostęp do udziałów sieciowych to dwie
  różne rzeczy). Nowe konta domyślnie `nologin` (bez logowania do systemu).
- Grupy ogólne (pełne CRUD) niezależne od automatycznie zarządzanych grup
  dostępu do udziałów (`<udział>_access`), które nigdy się tu nie pokazują.

**Udziały Samby**
- Pełne CRUD, osobny, w pełni zarządzany plik smb.conf (dołączany, nigdy
  nie nadpisujący istniejącej konfiguracji), walidowany `testparm` przed
  każdym zapisem z automatycznym rollbackiem przy błędzie.
- Dostęp per użytkownik **i** per grupa, trzy poziomy (brak / tylko odczyt /
  odczyt i zapis), wymuszany jednocześnie przez Sambę i ACL na dysku.

**Klucze SSH**
- Generowanie i bezhasłowe wdrażanie kluczy ed25519 z dedykowanego konta
  serwisowego (`nas-sync`) na zdalne maszyny - do automatyzacji/rsync,
  niezależnie od haseł SMB. Śledzenie, na których urządzeniach klucz jest
  aktualny.

**Sieć**
- Wykrywanie: hostname, backend zarządzający siecią, adresy/maski/bramy/DNS/
  MAC per interfejs, typ karty (WiFi/USB/wbudowana).
- Zmiana ustawień IP/bramy/DNS (NetworkManager) z automatycznym cofnięciem
  po 30s, jeśli zmiana nie zostanie potwierdzona - zabezpieczenie przed
  odcięciem się od własnego dashboardu.

**Aktualizacje**
- Aplikacja sama się aktualizuje z GitHuba (`git fetch` + `reset --hard`,
  potem `install.sh` w tle) - jeden przycisk w panelu konta.
- Aktualizacje systemowe (apt) - sprawdzanie i instalowanie (`apt-get
  upgrade`, nigdy `dist-upgrade` - nic nie usuwa) z osobnego przycisku obok,
  status widoczny też w pasku stanu. Sprawdzanie na stałym interwale (30
  min), nie przy każdym otwarciu panelu.

**Log operacji** - każda mutująca operacja (dyski, RAID, użytkownicy,
udziały, klucze SSH, sieć) zapisana z czasem, statusem i szczegółem,
filtrowanie po zakresie czasu, konfigurowalny limit przechowywania.

**Interfejs**
- PL/EN, przełącznik w nagłówku.
- Jasny/ciemny motyw (domyślnie ciemny przy pierwszym użyciu).
- Trzy poziomy powiększenia interfejsu (80% / 100% / 120%), zapisywane
  trwale po stronie serwera.
- Logowanie na poziomie aplikacji (nie systemowe/PAM), konfigurowalny czas
  trwania sesji, blokada po zbyt wielu nieudanych próbach.

## Wymagania systemowe

```bash
sudo apt install smartmontools mdadm samba python3-venv acl
```

(`mdadm` jest potrzebny tylko do sekcji RAID - jeśli hosta nie ma żadnej
macierzy, ta sekcja po prostu pokaże "brak wykrytych macierzy". `samba`
daje `smbpasswd`/`pdbedit`, potrzebne do zarządzania użytkownikami SMB.
`acl` daje `setfacl`, potrzebne do udziałów z dostępem grupowym.)

## Instalacja (jedna komenda)

```bash
git clone https://github.com/TjomekPL/nas-monitor.git
cd nas-monitor
sudo ./install.sh
```

`install.sh` instaluje pakiety systemowe (w tym nginx + fail2ban), tworzy
virtualenv, klonuje repo do `/opt/nas-monitor`, generuje self-signed
certyfikat TLS, pyta o dane konta administratora i uruchamia usługę systemd
za reverse proxy nginx. Dashboard będzie dostępny pod `https://<adres-hosta>`
(przeglądarka pokaże ostrzeżenie o niezaufanym certyfikacie przy pierwszym
wejściu - to oczekiwane, certyfikat jest self-signed). Jeśli konfiguracja
HTTPS się nie powiedzie z jakiegoś powodu, instalator automatycznie zostaje
przy zwykłym `http://<adres-hosta>:8420` zamiast przerywać całą instalację.

## Instalacja ręczna (bez install.sh)

```bash
sudo mkdir -p /opt/nas-monitor
sudo cp -r nas_monitor requirements.txt nas-monitor.service /opt/nas-monitor/
cd /opt/nas-monitor
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
sudo cp nas-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nas-monitor
```

## Uruchomienie ręczne (bez systemd, do szybkiego testu)

```bash
cd /opt/nas-monitor
./venv/bin/python -m nas_monitor.app
```

## Bezpieczeństwo

- Usługa działa jako **root** - to konieczne, żeby `smartctl`/`mdadm` mogły
  odpytywać i zmieniać stan dysków bezpośrednio (ATA/NVMe passthrough i
  operacje blokowe wymagają uprawnień roota).
- Logowanie na poziomie aplikacji (`nas_monitor/auth.py`) - osobne konto
  administratora, niezależne od kont systemowych/SSH. Hasło hashowane
  (PBKDF2), blokada po zbyt wielu nieudanych próbach, konfigurowalny czas
  trwania sesji. Wyłącznik awaryjny (`AUTH_ENABLED=0` w
  `nas-monitor.service`) wymaga dostępu SSH/roota - celowo, żeby web UI nie
  mógł sam siebie ominąć.
- Reset hasła administratora tylko przez SSH (`reset-admin-password.sh`),
  świadomie bez mechanizmu przez e-mail.
- HTTPS przez self-signed certyfikat + nginx, fail2ban na logowaniu (obok
  wbudowanej blokady na poziomie aplikacji).

## Testy

Logika parsowania i mutacji (dyski, RAID, użytkownicy, udziały, klucze SSH,
sieć, autoryzacja) ma pełne testy jednostkowe działające na przykładowych
danych - nie wymagają prawdziwego sprzętu ani zainstalowanego
`smartctl`/`mdadm`:

```bash
python3 -m pytest tests/ -v
```

## Wygląd

Wyśrodkowany układ (max 1080px), jasny/ciemny motyw z przełącznikiem w
nagłówku (domyślnie ciemny przy pierwszym użyciu, zapamiętywany w
`localStorage`). Trzy poziomy powiększenia interfejsu (80/100/120%,
konfigurowalne w panelu konta, zapisywane po stronie serwera). Żaden z
motywów nie jest czystą bielą/czernią - tokeny kolorów (tło, tekst, akcent,
kolory statusu) są w `static/style.css` na górze pliku. Kolor akcentu
(stalowy niebieski) celowo różni się od zielonego "ok", żeby przycisk akcji
nigdy nie mylił się ze statusem "wszystko dobrze".

## Struktura projektu

Kod jest podzielony na warstwę rdzenia (protokół-agnostyczną) i warstwy
protokołów/zasobów, żeby dodanie kolejnego (np. NFS) było "dopisz nowy
plik", a nie przepisywanie wszystkiego:

```
nas_monitor/
  system_tools.py    - wspólne: bezpieczne odpalanie poleceń, szukanie binarek
  errors.py           - wspólne: wyniki błędów jako kod+kontekst (nigdy gotowy tekst) - patrz i18n niżej
  state_store.py        - lokalny magazyn JSON na stan nie do wyczytania z systemu
  monitor.py         - rdzeń, czysty odczyt: dyski, SMART, RAID (w tym zdrowie, zagnieżdżenie macierzy)
  disk_mutate.py       - mutacje dysków: format/wipe/mount/unmount, lista zarządzalnych dysków i macierzy
  raid_mutate.py         - mutacje RAID: tworzenie (w tym zagnieżdżone), detach/repair, usuwanie macierzy
  disk_labels.py           - kosmetyczne etykiety dysków/macierzy, kluczowane po serialu
  layout.py                  - zapamiętana kolejność kart/sekcji w UI
  users.py           - rdzeń: użytkownicy i grupy systemowe
  smb.py             - backend SMB: hasła Samby, dowiązanie do kont systemowych
  smb_shares.py       - backend SMB: udziały (CRUD pod /srv, testparm+rollback, ACL grupowe)
  ssh_keys.py          - klucze SSH: generowanie + wysyłanie na zdalne urządzenie
  network.py             - wykrywanie sieci (odczyt)
  network_mutate.py        - mutacja sieci: walidacja, nmcli, snapshot+auto-cofnięcie 30s
  system_stats.py            - bieżące obciążenie CPU/RAM/dysk/sieć (pasek stanu)
  oplog.py                - log operacji
  auth.py                   - konto administratora (poziom aplikacji): hasło, sesje
  setup_admin.py              - skrypt CLI wywoływany przez install.sh, ustawia konto admina
  update_manager.py             - self-update aplikacji przez git
  system_update.py               - aktualizacje systemowe przez apt
  app.py              - Flask app, wszystkie trasy
  templates/
    dashboard.html
    login.html
  static/
    style.css
    dashboard.js       - odpytuje /api/status, /api/users, /api/shares co 20s, bez frameworków
    login.js
    i18n/
      index.js           - funkcja t(), wykrywanie/przełączanie języka
      pl.js, en.js         - słowniki tłumaczeń
tests/                  - pełne testy jednostkowe dla każdego modułu powyżej
nas-monitor.service     - jednostka systemd (uruchamia przez gunicorn)
```

**Ważne rozróżnienie w `users.py`/`smb.py`**: konto systemowe (Linux, z
własnym shellem) i dostęp SMB (osobne hasło przez `smbpasswd`) to dwie
różne rzeczy, nawet dla tego samego użytkownika. Nowe konta domyślnie
dostają `nologin` - dostęp SMB jest całkowicie niezależny od tego.

**Backend nigdy nie generuje tekstu dla użytkownika** (i18n): każda
mutująca funkcja zwraca `error_code`/`warning_code` + kontekst do
interpolacji zamiast gotowego zdania - cały tekst mieszka w
`nas_monitor/static/i18n/{pl,en}.js`. Dodanie kolejnego języka to jeden
nowy plik na wzór `pl.js`/`en.js`.
