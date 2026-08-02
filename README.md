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
  errors.py           - wspólne: budowanie wyników błędów jako kod+kontekst (nigdy gotowy tekst) - patrz sekcja i18n niżej
  monitor.py         - rdzeń: dyski, SMART, RAID (czysty odczyt)
  users.py           - rdzeń: użytkownicy i grupy systemowe (wykrywanie + tworzenie)
  smb.py             - backend SMB: hasła Samby, dowiązanie do kont systemowych
  smb_shares.py       - backend SMB: udziały (tworzenie/edycja/usuwanie pod /srv, testparm+rollback)
  ssh_keys.py          - klucze SSH per użytkownik: generowanie + wysyłanie na zdalne urządzenie
  state_store.py        - mały lokalny magazyn JSON na stan, którego nie da się wyczytać z systemu (śledzenie wdrożeń kluczy, log operacji)
  network.py             - wykrywanie sieci: hostname, backend (NM/networkd/ifupdown), interfejsy, DNS - tylko odczyt
  network_mutate.py        - mutacja sieci (IP/brama/DNS): walidacja, nmcli, snapshot+auto-cofnięcie 30s
  oplog.py                - log operacji: co się wykonało, sukces/błąd, pełny szczegół na żądanie
  auth.py                   - konto administratora (poziom aplikacji, nie system/PAM): hashowanie hasła, sesje
  setup_admin.py              - skrypt CLI wywoływany przez install.sh, ustawia konto admina (hasło przez stdin)
  app.py              - Flask app, wszystkie trasy
  templates/
    dashboard.html
    login.html                - osobna, minimalna strona logowania
  static/
    style.css
    dashboard.js      - odpytuje /api/status, /api/users, /api/shares co 20s, bez frameworków
    login.js                  - obsługa formularza logowania
    i18n/
      index.js           - funkcja t(), wykrywanie/przełączanie języka, aplikowanie tłumaczeń do DOM
      pl.js, en.js         - słowniki tłumaczeń (UI, komunikaty, kody błędów/ostrzeżeń, log)
tests/
  test_monitor.py     - testy dysków/SMART/RAID na przykładowych danych
  test_users.py        - testy kont/grup systemowych
  test_smb.py           - testy warstwy SMB (użytkownicy)
  test_smb_shares.py     - testy warstwy SMB (udziały) - w tym prawdziwe testy na tmpdir dla configparser
  test_ssh_keys.py        - testy kluczy SSH
  test_network.py         - testy wykrywania sieci
  test_network_mutate.py    - testy mutacji sieci (walidacja, snapshot/apply/revert, potwierdzenie, samonaprawa po restarcie)
  test_oplog.py             - testy logu operacji (persystencja, limit, filtr czasowy)
  test_auth.py                - testy logowania (walidacja hasła, hashowanie, sesje, wyłącznik awaryjny)
  test_setup_admin.py           - testy skryptu CLI ustawiającego konto admina
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
   Sieć / Log), wybór zapamiętywany w `localStorage`.
7. **Sieć** - ✅ **wykrywanie** zrobione (`nas_monitor/network.py`):
   nazwa hosta, który system zarządza siecią (NetworkManager /
   systemd-networkd / ifupdown / nierozpoznany - wykrywane naprawdę, nie
   zakładane, bo różne maszyny w tym projekcie różnią się pod tym
   względem), oraz każdy interfejs z prawdziwym stanem (adres, maska,
   brama, DNS, MAC, up/down) czytanym na żywo, niezależnie od tego,
   który z powyższych systemów nim zarządza.

   **Zmiana ustawień** - ✅ zrobiona, opisana niżej jako osobny punkt
   (razem z auto-cofnięciem 30s).

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
   pokazany osobno, uczciwie).

   **DNS pokazywany per-interfejs, nie globalnie** - pierwsza wersja
   czytała `/etc/resolv.conf`, czyli jeden, scalony widok tego, kogo
   systemd-resolved aktualnie traktuje jako resolver pierwszego wyboru
   dla całego systemu. Z aktywnym Tailscale (MagicDNS) to zawsze pokazywało
   `100.100.100.100` (adres resolvera Tailscale) zamiast realnych DNS-ów
   danego połączenia - myląco wyglądało jak błąd, ale to inne pytanie niż
   zadaje panel ustawień sieci w GNOME (który pyta konkretne połączenie,
   nie system globalnie). Naprawione: DNS czytany per-interfejs przez
   `nmcli device show <if>` (pole `IP4.DNS`), dokładnie to co pokazuje
   panel GNOME - zgodne 1:1 sprawdzone na żywym przykładzie.

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

   **Mutacja ustawień sieciowych (IP/brama/DNS)** - ✅ zrobione, tylko
   NetworkManager (`nmcli`) na start - inne backendy (systemd-networkd,
   ifupdown) świadomie poza zakresem na razie, `install.sh` ostrzega
   (nie blokuje instalacji) jeśli NetworkManager nie jest aktywny. To
   jedyna mutacja w całym narzędziu, która przy błędzie może odciąć
   admina od samego dashboardu, więc ma dedykowaną warstwę bezpieczeństwa
   (`nas_monitor/network_mutate.py`):
   - Walidacja przez `ipaddress` (stdlib) zamiast ręcznego regexa, plus
     sprawdzenie że brama leży w tej samej podsieci co nowy adres
   - Snapshot obecnej konfiguracji (`nmcli connection show`) przed każdą
     zmianą
   - Stan "oczekująca zmiana" zapisany na dysk (`state_store.py`), NIE w
     zmiennej w pamięci procesu - `nas-monitor.service` odpala gunicorn z
     2 workerami (osobne procesy!), więc żądanie "zastosuj" i późniejsze
     "potwierdź" mogą trafić do różnych procesów
   - Timer 30s (server-side, `threading.Timer`) automatycznie cofa zmianę
     jeśli nie zostanie potwierdzona - ale zawsze najpierw sprawdza stan
     zapisany na dysku, więc jest bezpieczny niezależnie od tego, który
     worker co obsłużył
   - Samonaprawa przy starcie usługi: jeśli serwis zrestartuje się
     w trakcie okna 30s (np. przez `systemctl restart` po kolejnym
     wdrożeniu), przy starcie sprawdza czy coś czeka na potwierdzenie -
     cofa od razu albo uzbraja nowy, krótszy timer na resztę czasu
   - Frontend: dialog edycji (zamiast edycji wprost w karcie - decyzja
     Tomka, dla spójności z resztą UI) + baner z odliczaniem,
     przeglądarka automatycznie podąża za nowym adresem, ale **tylko**
     jeśli edytowany interfejs to ten, przez który akurat jest otwarty
     dashboard - edycja innego interfejsu niczego nie przełącza
8. **Zdalne montowanie** - jeszcze nie omówione w szczegółach.
9. **Backupy przez rsync (lub inne metody)** - jeszcze nie omówione w
   szczegółach.
10. **Log operacji** - ✅ zrobione. Osobna zakładka "Log", jedna implementacja
   obejmująca od razu wszystkie mutujące operacje (użytkownicy, udziały,
   klucze SSH) zamiast dorabiania per-funkcja. Świadomie NIE jest to log
   w stylu konsoli - każdy wpis to zwinięty nagłówek (co się stało + pigułka
   sukces/błąd + czas), pełny szczegół (treść błędu / komunikat) widoczny
   dopiero po rozwinięciu, z przyciskiem kopiowania. Do tego: wyszukiwanie
   wpisów po zakresie czasowym, przycisk "Wyczyść log" i konfigurowalny
   limit liczby przechowywanych wpisów (domyślnie 50, zakres 10-1000,
   najstarsze wpisy usuwane automatycznie po przekroczeniu). Zapisane przez
   `state_store.py` (jak wdrożenia kluczy SSH) - przetrwa restart usługi.
11. **Wersje językowe (i18n)** - ✅ zrobione, PL/EN na start, łatwe do
   rozszerzenia. Architektura celowo rozdziela dwie sprawy:

   - **Backend nigdy nie generuje tekstu dla użytkownika.** Każda mutująca
     funkcja zwraca `error_code` (stały, nietłumaczony identyfikator jak
     `"users.already_exists"`) + `error_context` (dane do interpolacji,
     np. `{"username": "wieslaw"}`) zamiast gotowego polskiego zdania -
     zobacz `nas_monitor/errors.py`. To samo dla ostrzeżeń
     (`warning_code`/`warning_context`, lista `warnings` bo jeden wynik
     może nieść więcej niż jedno). Log operacji też nie zapisuje gotowego
     tekstu - `oplog.log_event()` przyjmuje `category`+`action`+`status`+
     `params`, więc historia przetłumaczy się poprawnie nawet wstecz, po
     zmianie języka.
   - **Cały tekst mieszka po stronie frontendu**, w
     `nas_monitor/static/i18n/{pl,en}.js` (zwykłe pliki `<script>`, bez
     buildowania) + `index.js` (funkcja `t(klucz, dane)`, wykrywanie
     języka z zapisanego wyboru/języka przeglądarki, przełącznik w
     nagłówku). Dodanie kolejnego języka to jeden nowy plik na wzór
     `pl.js`/`en.js` - backend i reszta frontendu się nie zmieniają.
   - Statyczne etykiety w HTML oznaczone `data-i18n="klucz"` (tłumaczone
     przy starcie i przy każdej zmianie języka); treści generowane przez
     JS (tabele, dialogi, tosty, potwierdzenia) wywołują `t()` bezpośrednio.
   - Zweryfikowane end-to-end w sandboxie przez jsdom (symulowany DOM +
     zamockowane API): renderowanie wszystkich zakładek w obu językach,
     przełączanie w locie, zero brakujących kluczy tłumaczeń. 176 testów
     jednostkowych backendu zaktualizowanych pod kody błędów zamiast
     dopasowywania fragmentów polskiego tekstu.
12. **Logowanie / konto administratora** - ✅ zrobione
    (`nas_monitor/auth.py`). Kluczowa decyzja: konto **wyłącznie na
    poziomie aplikacji**, nigdy konto systemowe/PAM - tak jak
    OMV/Portainer/Proxmox, żeby "dostęp do dashboardu" nigdy nie
    mieszał się z "dostępem SSH do maszyny".
    - Hasło hashowane przez `werkzeug.security` (PBKDF2) - biblioteka
      już jest zależnością Flaska, zero nowych pakietów
    - Wymogi hasła: min. 10 znaków, przynajmniej jedna litera i jedna
      cyfra; wielkie litery/znaki specjalne dozwolone, nie wymagane
    - Sesje: podpisane ciasteczka Flaska, domyślnie do zamknięcia
      przeglądarki (konfigurowalne w panelu konta na godziny). Klucz
      podpisujący generowany raz i trwale zapisany - gunicorn ma 2
      procesy robocze, więc każdy musi czytać ten sam klucz, inaczej
      sesja podpisana przez proces A nie zwalidowałaby się w procesie B
    - `install.sh` pyta o nazwę konta (Enter = `admin`) i hasło
      (dwukrotnie, walidacja w bashu przed przekazaniem dalej) przy
      pierwszej instalacji - pomija pytanie przy ponownym uruchomieniu,
      jeśli konto już istnieje. Hasło nigdy jako argument linii poleceń
      (widoczne przez `ps`) - przekazywane do `nas_monitor/setup_admin.py`
      przez stdin
    - Wyłącznik awaryjny: `AUTH_ENABLED=0` w `nas-monitor.service`
      (zakomentowana linia z instrukcją) - wymaga dostępu SSH/roota,
      celowo, żeby web UI nie mógł sam siebie ominąć
    - Panel konta (ikonka w nagłówku obok motywu): zmiana hasła, czas
      trwania sesji, wylogowanie
    - Jeśli sesja wygaśnie w trakcie pracy, dowolne zapytanie API
      zwracające 401 automatycznie przekierowuje na `/login`
    - 40 nowych testów (auth.py + setup_admin.py), plus pełny test na
      żywo przez klienta Flask: niezalogowany dostęp, złe hasło,
      poprawne logowanie, wylogowanie, wyłącznik awaryjny, stan "jeszcze
      nieskonfigurowane" (nigdy nie blokuje, dopóki `install.sh` nie
      uruchomi setupu)
