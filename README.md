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
sudo apt install smartmontools mdadm python3-venv
```

(`mdadm` jest potrzebny tylko do sekcji RAID — jeśli hosta nie ma żadnej
macierzy, ta sekcja po prostu pokaże "brak wykrytych macierzy".)

## Instalacja (jedna komenda)

```bash
git clone https://github.com/<konto>/nas-monitor.git
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
python3 -m unittest tests.test_monitor -v
```

## Struktura projektu

```
nas_monitor/
  app.py            - Flask app (routes: / oraz /api/status)
  monitor.py         - cała logika: lsblk, smartctl, mdadm (czysty odczyt)
  templates/
    dashboard.html
  static/
    style.css
    dashboard.js      - odpytuje /api/status co 20s, bez frameworków
tests/
  test_monitor.py     - testy parsowania na przykładowych danych
nas-monitor.service   - jednostka systemd (uruchamia przez gunicorn)
```

## Plan / kolejne fazy (jeszcze nie zaimplementowane)

To narzędzie ma docelowo być uniwersalnym web UI do zarządzania
Debian + Samba, nie tylko monitoringiem. Ustalona kolejność:

1. **Monitoring dysków i RAID (odczyt)** — ✅ ta wersja.
2. **Zarządzanie Samba** — udziały, użytkownicy, edycja `smb.conf` +
   walidacja `testparm` przed reloadem.
3. **Zarządzanie RAID** — tworzenie/rozbudowa/usuwanie macierzy. Ustalono:
   operacje mają wykonywać się automatycznie po potwierdzeniu w UI (nie
   tylko generować komendę do ręcznego wklejenia). Wymaga dodatkowych
   zabezpieczeń przed budową: weryfikacja że dysk jest pusty/niezamontowany,
   wykrywanie istniejącego superbloku, wyraźne ostrzeżenie o nieodwracalności
   przed każdym potwierdzeniem.
