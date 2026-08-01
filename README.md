# nas-monitor

Uniwersalny web UI do zarządzania Debian + Samba. **Faza 1 (ta wersja): tylko
monitoring, wyłącznie odczyt** — stan dysków (S.M.A.R.T.) i macierzy RAID
(mdadm). Żadna operacja w tej wersji nic nie zmienia na dysku.

Działa na dowolnym hoście Debian z bezpośrednim dostępem do dysków (bare
metal, VM z passthrough) — mdadm i smartctl nie mają dostępu do surowych
urządzeń blokowych z poziomu unprivileged LXC, więc to narzędzie nie nadaje
się do kontenera bez takiego dostępu.

## Wymagania systemowe

```bash
sudo apt install smartmontools mdadm samba python3-venv
```

(`mdadm` jest potrzebny tylko do sekcji RAID - jeśli hosta nie ma żadnej
macierzy, ta sekcja po prostu pokaże "brak wykrytych macierzy". `samba`
daje `smbpasswd`/`pdbedit`, potrzebne do zarządzania użytkownikami SMB.)

## Instalacja (jedna komenda)

```bash
git clone https://github.com/TjomekPL/nas-monitor.git
cd nas-monitor
sudo ./install.sh
```

`install.sh` instaluje pakiety systemowe, tworzy virtualenv, kopiuje pliki
do `/opt/nas-monitor` i uruchamia usługę systemd. Dashboard będzie dostępny
pod `http://<adres-hosta>:8420`.

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

## Uwaga bezpieczeństwa

- Usługa działa jako **root** — to konieczne, żeby `smartctl` mógł odpytywać
  dyski bezpośrednio (ATA/NVMe passthrough wymaga uprawnień roota).
- **Nie ma jeszcze żadnej autoryzacji** — każdy w sieci LAN, kto zna adres i
  port, widzi dashboard. W tej fazie to tylko odczyt (brak ryzyka utraty
  danych), ale nie wystawiaj tego portu poza zaufaną sieć domową bez
  dodania auth (np. reverse proxy z basic auth) w kolejnej iteracji.

## Testy

Logika parsowania (`smartctl -j`, `/proc/mdstat`, `mdadm --detail --export`)
ma testy jednostkowe działające na przykładowych danych — nie wymagają
prawdziwego sprzętu ani zainstalowanego `smartctl`/`mdadm`:

```bash
python3 -m unittest discover -s tests -v
```

## Struktura projektu i architektura

Projekt jest pomyślany jako narzędzie uniwersalne (nie tylko SMB) - dlatego
kod jest podzielony na warstwę rdzenia (protokół-agnostyczną) i warstwy
protokołów, żeby dodanie kolejnego (np. NFS) kiedyś było "dopisz nowy plik",
a nie przepisywanie wszystkiego:

```
nas_monitor/
  system_tools.py   - wspólne: bezpieczne odpalanie poleceń, szukanie binarek
  monitor.py         - rdzeń: dyski, SMART, RAID (czysty odczyt)
  users.py           - rdzeń: użytkownicy i grupy systemowe (wykrywanie + tworzenie)
  smb.py             - backend SMB: hasła Samby, dowiązanie do kont systemowych
  smb_shares.py       - backend SMB: udziały (tworzenie/edycja/usuwanie pod /srv, testparm+rollback)
  app.py              - Flask app, wszystkie trasy
  templates/
    dashboard.html
  static/
    style.css
    dashboard.js      - odpytuje /api/status, /api/users, /api/shares co 20s, bez frameworków
tests/
  test_monitor.py     - testy dysków/SMART/RAID na przykładowych danych
  test_users.py        - testy kont/grup systemowych
  test_smb.py           - testy warstwy SMB (użytkownicy)
  test_smb_shares.py     - testy warstwy SMB (udziały) - w tym prawdziwe testy na tmpdir dla configparser
nas-monitor.service      - jednostka systemd (uruchamia przez gunicorn)
```

**Ważne rozróżnienie w `users.py`/`smb.py`**: konto systemowe (Linux, z
własnym shellem) i dostęp SMB (osobne hasło przez `smbpasswd`) to dwie
różne rzeczy, nawet dla tego samego użytkownika. Nowe konta domyślnie
dostają `nologin` (nie mogą się zalogować do systemu/SSH) - dostęp SMB jest
całkowicie niezależny od tego. Jeśli kiedyś dojdzie NFS, nie miałby w ogóle
pojęcia "hasło użytkownika" (NFS klasycznie działa przez UID/GID i adres
klienta), więc ta separacja jest zamierzona.

## Plan / kolejne fazy

1. **Monitoring dysków i RAID (odczyt)** - ✅ zrobione.
2. **Użytkownicy i grupy (pełny cykl: wykrywanie, tworzenie, edycja, usuwanie)** - ✅
   zrobione. Wykrywanie czyta rzeczywisty stan systemu (`pwd`/`grp`/`pdbedit -L`),
   nie osobną bazę. Tworzenie: jeden formularz zakłada konto systemowe (domyślnie
   `nologin`) + hasło SMB + grupy (nowe grupy tworzone automatycznie). Duże litery
   w nazwie (np. "Tomek") stają się kontem `tomek` + etykietą GECOS "Tomek".
   Edycja: grupy (pełna podmiana listy), hasło SMB, prawo logowania, etykieta -
   bez zmiany samej nazwy konta systemowego (zbyt ryzykowne; usuń+załóż od nowa
   zamiast tego). Usuwanie: pełne (konto + SMB, katalog domowy NIE usuwany
   domyślnie) albo tylko dostęp SMB (konto zostaje).
3. **Udziały Samby (pełny cykl)** - ✅ zrobione. Udziały tworzone przez to
   narzędzie zawsze lądują pod `/srv/<nazwa>` (jak w OMV) i są zapisywane w
   osobnym, w pełni zarządzanym pliku `/etc/samba/smb.conf.d/nas-monitor-shares.conf`,
   dołączanym do głównego `smb.conf` przez `include =` (dopisywane raz, przy
   pierwszym użyciu) - główny plik z Twoimi komentarzami nigdy nie jest
   nadpisywany w całości. Każdy zapis jest walidowany `testparm` na
   rzeczywistym, złączonym pliku przed zastosowaniem; błąd = automatyczny
   rollback do poprzedniej treści, nic nie zostaje zepsute. Wykrywanie
   pokazuje też udziały zdefiniowane ręcznie wprost w głównym `smb.conf`
   (oznaczone jako spoza tego narzędzia, tylko do podglądu - edycja/usuwanie
   działa wyłącznie na udziałach zarządzanych tutaj). Dostęp do udziału
   przypisuje się **per użytkownik** (nie przez ręczne wybieranie grupy) -
   pod spodem narzędzie samo zarządza dedykowaną grupą `<udział>_access`
   (tworzy ją, dopisuje/wypisuje wybranych userów przy edycji, kasuje przy
   usunięciu udziału) i ustawia ją jako właściciela folderu (setgid) oraz
   `force group` w smb.conf, więc zapis działa spójnie niezależnie od
   pozostałych grup łączącego się użytkownika.
4. **Zarządzanie RAID** - tworzenie/rozbudowa/usuwanie macierzy. Ustalono:
   operacje mają wykonywać się automatycznie po potwierdzeniu w UI (nie
   tylko generować komendę do ręcznego wklejenia). Wymaga dodatkowych
   zabezpieczeń przed budową: weryfikacja że dysk jest pusty/niezamontowany,
   wykrywanie istniejącego superbloku, wyraźne ostrzeżenie o nieodwracalności
   przed każdym potwierdzeniem.
