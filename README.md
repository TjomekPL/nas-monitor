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

## Wygląd

Wyśrodkowany układ (max 1080px), jasny/ciemny motyw z przełącznikiem w
nagłówku (zapamiętywany w `localStorage`, domyślnie idzie za preferencją
systemu). Żaden z motywów nie jest czystą bielą/czernią - tokeny kolorów
(tło, tekst, akcent, kolory statusu) są w `static/style.css` na górze
pliku. Kolor akcentu (stalowy niebieski) celowo różni się od zielonego
"ok", żeby przycisk akcji nigdy nie mylił się ze statusem "wszystko
dobrze". Sprawdzone realnie (Playwright + Chromium w sandboxie): kontrast
tekstu spełnia WCAG AA w obu motywach, wyśrodkowanie liczone z rzeczywistej
geometrii strony, nie tylko wizualnie.

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
  ssh_keys.py          - klucze SSH per użytkownik: generowanie + wysyłanie na zdalne urządzenie
  state_store.py        - mały lokalny magazyn JSON na stan, którego nie da się wyczytać z systemu (śledzenie wdrożeń kluczy, docelowo log)
  network.py             - wykrywanie sieci: hostname, backend (NM/networkd/ifupdown), interfejsy, DNS - na razie tylko odczyt
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
  test_ssh_keys.py        - testy kluczy SSH
  test_network.py         - testy wykrywania sieci
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
   działa wyłącznie na udziałach zarządzanych tutaj).

   **Dostęp per użytkownik, trzy poziomy** (jak w OMV): każdy użytkownik ma
   dla danego udziału `Brak dostępu` / `Tylko odczyt` / `Odczyt i zapis`.
   Pod spodem: udział jest zawsze `read only = no`, a użytkownicy z
   poziomem "odczyt" trafiają do `read list` (Samba wymusza im tryb
   tylko-do-odczytu niezależnie od domyślnego trybu udziału). Dostęp do
   udziału w ogóle (czy ktoś może się nawet połączyć) idzie przez
   dedykowaną, samodzielnie zarządzaną grupę `<udział>_access` (tworzona
   automatycznie, userzy dopisywani/wypisywani przy edycji, kasowana przy
   usunięciu udziału) - właściciel folderu (setgid) i `force group` w
   smb.conf, więc zapis działa spójnie niezależnie od pozostałych grup
   łączącego się użytkownika.

   **Bezpieczeństwo grup, wynikłe z realnego incydentu**: udział założony
   starą wersją (pojedyncza grupa z listy, sprzed modelu per-użytkownik)
   mógł wskazywać na DOWOLNĄ istniejącą grupę - łącznie z prywatną grupą
   czyjegoś konta. Naprawione trzema niezależnymi zabezpieczeniami:
   (1) edycja takiego udziału automatycznie migruje go na własną,
   dedykowaną grupę, nigdy nie modyfikując "obcej" grupy; (2) usuwanie
   udziału kasuje grupę tylko wtedy, gdy jej nazwa odpowiada własnej
   konwencji narzędzia (`<udział>_access`); (3) grupy dostępowe udziałów
   są ukryte z ogólnej checklisty edycji użytkownika w sekcji Użytkownicy,
   żeby przypadkowa edycja kogoś nie odebrała mu po cichu dostępu do
   udziału.

   **Ważne**: tworzenie/zmiana hasła SMB (`smbpasswd -a`) nie synchronizuje
   prawdziwego hasła logowania systemowego mimo `unix password sync = yes`
   w domyślnym `smb.conf` - sprawdzone bezpośrednio (hash w `/etc/shadow`
   nie zmienia się). To odrębne dane, tak jak zaprojektowano.
4. **Zarządzanie RAID** - tworzenie/rozbudowa/usuwanie macierzy. Ustalono:
   operacje mają wykonywać się automatycznie po potwierdzeniu w UI (nie
   tylko generować komendę do ręcznego wklejenia). Wymaga dodatkowych
   zabezpieczeń przed budową: weryfikacja że dysk jest pusty/niezamontowany,
   wykrywanie istniejącego superbloku, wyraźne ostrzeżenie o nieodwracalności
   przed każdym potwierdzeniem. **Odłożone na razie** - do czasu, aż będzie
   dostępna maszyna z wolnymi dyskami do testowania na żywo.
5. **Certyfikaty (klucze SSH) dla użytkowników** - ✅ zrobione. Chodziło o
   klucze SSH (nie certyfikaty X.509) - do logowania/rsync na inne maszyny
   bez hasła, niezależnie od hasła SMB (to jest dokładnie ten problem
   "dane Samby ≠ dane SSH" z sekcji architektury wyżej). Generowanie pary
   ed25519 w `~/.ssh` konta (tylko dla kont z włączonym logowaniem/SSH),
   i "wysyłanie" klucza publicznego na zdalne urządzenie przez
   `sshpass`+`ssh-copy-id` - hasło do zdalnej maszyny używane raz, przez
   zmienną środowiskową (nie argv, gdzie `ps` by je widział), nigdzie nie
   zapisywane. Sprawdzone naprawdę: cały łańcuch (generuj → wyślij →
   prawdziwe bezhasłowe SSH) na żywym `sshd`.

   **Śledzenie wdrożeń** - lista urządzeń, na które klucz wysłano, przy
   każdym pigułka: zielona = klucz na urządzeniu wciąż zgadza się z
   aktualnym lokalnym (`~/.ssh/id_ed25519.pub`), czerwona = klucz od tego
   czasu wygenerowano ponownie bez ponownego wysłania (urządzenie ma stary,
   już niepasujący klucz). Pierwszy stan trzymany lokalnie przez to
   narzędzie, niederywowalny z systemu (`nas_monitor/state_store.py`,
   `/etc/nas-monitor/*.json`) - ten sam mechanizm posłuży do loga operacji.
   "Usuń z urządzenia" naprawdę usuwa właściwą linię z `authorized_keys`
   (dopasowanie po treści zapisanej przy wysyłce, nie po aktualnym kluczu -
   działa też dla nieaktualnych wpisów), zostawiając inne wpisy nietknięte.
   Po drodze złapane i naprawione ręcznie dwa realne błędy: zmienne
   środowiskowe nie przechodzą przez SSH do zdalnej powłoki (próba
   przekazania tak treści klucza zostawiłaby pusty wzorzec i `grep -vF ""`
   wyczyściłby cały plik), i `grep -v` zwraca kod wyjścia 1, gdy usuwana
   linia była jedyną w pliku (to sukces, nie błąd - naiwne `grep && mv`
   by to pomijało).

   Dwie dalsze poprawki: (a) `window.alert()` blokuje cały wątek JS,
   więc trzymając otwarty komunikat sukcesu, zaległe 20-sekundowe
   odświeżenia odpalały się hurtowo po jego zamknięciu, dając widoczny
   "przebłysk" starych danych - zamienione na nieblokujące powiadomienia
   (`showToast`, prawy dolny róg); (b) lista wdrożeń pokazuje teraz
   przyjazną nazwę urządzenia (np. "vOMV") zamiast surowego adresu -
   opcjonalne pole przy wysyłce, nie automatyczne rozpoznawanie po DNS
   (na typowej domowej sieci zwrotne DNS zwykle i tak nie działa
   niezawodnie dla własnoręcznie skonfigurowanych urządzeń).

   **⚠️ Incydent i poprawka bezpieczeństwa nazwy pliku klucza**: klucz
   generowany był pod domyślną, konwencjonalną nazwą `~/.ssh/id_ed25519`
   - dla zwykłego, prawdziwego konta logowania (nie dedykowanego wyłącznie
   do udziałów) to jest dokładnie ta sama ścieżka, gdzie leży własny,
   osobisty klucz (np. do GitHuba). Wygenerowanie klucza przez to narzędzie
   dla takiego konta nadpisało realny, używany klucz GitHub, co zerwało
   dostęp `git push` do repozytorium. Naprawione: własna, jednoznacznie
   nazwana ścieżka `~/.ssh/id_ed25519_nasmonitor`, która nigdy nie może się
   zetknąć z żadnym cudzym kluczem - potwierdzone testem odtwarzającym
   dokładnie ten scenariusz (prawdziwy osobisty klucz + wygenerowanie
   klucza nas-monitor obok, hash osobistego klucza identyczny przed/po).
6. **Zakładki zamiast jednej długiej strony** - ✅ zrobione. Pasek zakładek
   pod nagłówkiem (Dyski i macierze / Użytkownicy / Certyfikaty / Udziały /
   Sieć), wybór zapamiętywany w `localStorage`.
7. **Sieć** - ✅ **wykrywanie** zrobione (`nas_monitor/network.py`):
   nazwa hosta, który system zarządza siecią (NetworkManager /
   systemd-networkd / ifupdown / nierozpoznany - wykrywane naprawdę, nie
   zakładane, bo różne maszyny w tym projekcie różnią się pod tym
   względem), serwery DNS, oraz każdy interfejs z prawdziwym stanem
   (adres, maska, brama, MAC, up/down) czytanym na żywo przez `ip -j`,
   niezależnie od tego, który z powyższych systemów nim zarządza.

   **Zmiana ustawień jeszcze NIE jest zrobiona** - celowo, to jedyne
   miejsce w całym narzędziu, gdzie błąd może odciąć dostęp do samego
   dashboardu. Ustalony plan zabezpieczenia: nowe ustawienia stosowane
   od razu, ale z zaplanowanym automatycznym powrotem do poprzednich po
   **30 sekundach**, chyba że administrator potwierdzi w interfejsie, że
   nowe ustawienia działają - dokładnie taki wzorzec jak w pfSense/OPNsense.
   Do zaprojektowania: mechanizm powrotu musi przeżyć nawet, gdyby sama
   aplikacja/worker padły w międzyczasie (prawdopodobnie `systemd-run
   --on-active=30s` jako niezależny, zaplanowany timer, nie wątek w
   samym procesie Flask).

   **Tailscale** - w dwóch oddzielnych krokach na wyraźną prośbę: (1)
   instalacja + `tailscale up` (bezpieczne, tylko dodaje dostęp, link do
   zalogowania pokazany w interfejsie) - jeszcze nie zrobione; (2)
   "ukrycie" z sieci lokalnej (blokada dostępu po zwykłym IP) - osobny,
   świadomy krok dopiero po potwierdzeniu że Tailscale faktycznie działa,
   żeby nie łączyć dwóch ryzyk zablokowania się naraz.

   Poprawki po pierwszym realnym teście: interfejsy tunelowe (Tailscale,
   podobne VPN) często zgłaszają jądru stan "unknown" zamiast "up" mimo że
   realnie działają - dodane pole `effective_up` (stan "unknown" + realny
   adres = liczy się jako aktywny dla kropki statusu; surowy stan nadal
   pokazany osobno, uczciwie). DNS pokazujący `100.100.100.100` zamiast
   adresów lokalnego routera to oczekiwane zachowanie Tailscale MagicDNS
   (podmienia `/etc/resolv.conf`), nie błąd.

   **Typ karty sieciowej** - dopisek przy nazwie interfejsu np. "(USB)",
   "(WiFi)", "(wbudowana)" - żeby łatwiej rozpoznać, która karta jest
   którą (frustrowało to na OMV). Czytane wprost z `/sys/class/net/<if>`
   - obecność katalogu `wireless` do wykrycia WiFi, a symlink
   `device/subsystem` do typu magistrali (usb/pci/virtio) - nie zgadywane
   z nazwy interfejsu (którą mogłaby zmienić reguła udev). Brak w ogóle
   symlinku `device` = interfejs czysto wirtualny (Tailscale, Docker,
   mosty, veth). Sprawdzone testem odtwarzającym dokładnie zestaw kart
   z produkcji (eno1, enx-USB, wlp2s0, tailscale0) i na prawdziwym sysfs
   w sandboxie.

   **Zaplanowane, jeszcze nie zaczęte**: redundancja/bonding kart
   sieciowych, gdy wykryte zostanie więcej niż jedna - do ustalenia z Tomkiem
   dokładny scenariusz (failover vs. agregacja przepustowości) i czy to
   dotyczy komputera czy raczej ustawień switcha/routera.
8. **Zdalne montowanie** - jeszcze nie omówione w szczegółach.
9. **Backupy przez rsync (lub inne metody)** - jeszcze nie omówione w
   szczegółach.
10. **Log operacji** - osobna sekcja z jasnym podziałem: co się wykonało
   poprawnie, co się nie udało i dlaczego. Zaplanowane jako następny krok,
   żeby objąć od razu wszystkie istniejące operacje (użytkownicy, udziały,
   klucze) zamiast dorabiać to osobno do każdej. Magazyn stanu
   (`state_store.py`) już gotowy od funkcji certyfikatów.
