#!/usr/bin/env python3
"""
verify.py — etap WERYFIKACJI TRAFNOŚCI klasyfikacji (krok 5 harmonogramu v3, dziennik 2026-11-10).

Podkomendy:
  draw    (wersja 1) losowanie próbki i przygotowanie ponownego pobrania
  check   (wersja 2) porównanie sha256 pobranych plików z pełną próbą i wybór końcowej próbki
  formularz (wersja 3) formularz oceny ocena.csv: wiersz na znalezisko (bez wartości) + kontrola pominięć
  pokaz     (wersja 3) podgląd dokumentu na terminal: JAWNE tagi exiftool, ich pola kanoniczne, znaleziska
  wyniki    (wersja 4) precyzja, czułość (z 95% przedziałem Wilsona), trafność wrażliwości, kategorii i typu
            wartości z wypełnionego formularza -> wyniki.json, bledy.csv; konsola: tylko liczby

Kryteria oceny (decyzje 2026-11-10):
  ocena = T   reguła zadziałała zgodnie z definicją w taksonomii v1 (kategoria i wrażliwość przyjęte z taksonomii);
              OSB-01–06: typ wartości poprawny; gdy N — poprawny typ w kolumnie „poprawne”
  uwagi       zastrzeżenia do samej taksonomii (nie wchodzą do liczb)
  pominięcie  dopisany wiersz typ_wiersza=pominiecie (nr, id_reguly, która powinna zadziałać, pole, zrodlo = tag):
              informacja w metadanych, którą taksonomia przewiduje, a znaleziska brak (do czułości)
  spoza       dopisany wiersz typ_wiersza=spoza (nr, zrodlo, uwagi): informacja spoza taksonomii (tylko uwagi)
  <nr>-P      ocena = T po sprawdzeniu dokumentu pod kątem pominięć (także gdy pominięć nie ma)

Próbka (decyzje 2026-11-10):
  część losowa    losowanie warstwowe wg instytucji: --n (domyślnie 5) dokumentów + --rezerwa (domyślnie 2)
                  na instytucję; ocena wszystkich znalezisk dokumentu (precyzja i czułość reguł)
  część celowana  dla każdego typu wartości pól osobowych, który w części losowej (podstawowej) ma mniej niż
                  --min-typ (domyślnie 10) wartości: dokumenty z wartością tego typu, liczba brakująca
                  + --rezerwa-typ (domyślnie 5); ocena tylko typu tych wartości (precyzja typu)
  wykluczone      PDF z uszkodzoną strukturą (ta sama definicja co w report.py)
Wartość = para (dokument, pole osobowe) z dokumenty.csv: autor, ostatnio_zapisal, prawa_autorskie,
wlasciciel_urzadzenia (te same pola, które ocenia classify.py).

Kolejność w pliku probka.csv = kolejność użycia: przy porównaniu sha256 brane są kolejne pliki zgodne,
rezerwa zastępuje pliki zmienione lub niedostępne. Ziarno zapisane w losowanie.json.

Wyjście w --out:
  probka.csv          część, instytucja, kolejność, rezerwa, typ celowany, plik, adres do pobrania, sha256
  <KOD>/urls.txt      adresy do pobrania w formacie fetch.py („URL,instytucja”; plik z ZIP -> adres archiwum)
  pobierz.sh          polecenia fetch.py dla wszystkich instytucji (zakres --domena z podsumowanie.json discover)
  losowanie.json      ziarno, data, parametry, sha256 dokumenty.csv, liczby
Konsola: tylko liczby. probka.csv i urls.txt zawierają adresy instytucji — tylko lokalnie.

Wyjście check: porownanie.csv (status każdego pliku: zgodny / inny_sha256 / niedostepny) i wybrane.csv
(końcowa próbka). Pliki z ZIP: porównywany sha256 pliku w archiwum (jak w extract.py).

Przykład:
  python3 tools/verify.py check --dokumenty dane/proba/dokumenty.csv --out dane/weryfikacja
  python3 tools/verify.py formularz --znaleziska dane/proba/znaleziska.csv --out dane/weryfikacja
  python3 tools/verify.py pokaz --nr 1 --dokumenty dane/proba/dokumenty.csv \\
                               --znaleziska dane/proba/znaleziska.csv --out dane/weryfikacja | less -S
  python3 tools/verify.py wyniki --out dane/weryfikacja [--bez-niewybranych]
  python3 tools/verify.py draw --dokumenty dane/proba/dokumenty.csv --long dane/proba/wyniki_long.csv \\
                               --proba-dir dane/proba --out dane/weryfikacja
"""
import argparse, collections, csv, hashlib, json, random, shlex, sys, time
from pathlib import Path

from report import uszkodzone_z_long          # ta sama definicja uszkodzonych PDF co w zestawieniach RQ3

POLA_OSOBOWE = ("autor", "ostatnio_zapisal", "prawa_autorskie", "wlasciciel_urzadzenia")
TYPY = ("osoba", "login", "email", "organizacja", "nieustalone", "zaslepka")
KOLUMNY = ["czesc", "instytucja", "kolejnosc", "rezerwa", "typ_celowany", "wartosci_typu", "plik",
           "url_pobrania", "z_archiwum", "sha256"]


def sha256_pliku(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def url_pobrania(w):
    """Plik z archiwum ZIP ma url „<adres archiwum>#<ścieżka w archiwum>” — pobieramy całe archiwum."""
    return (w.get("url") or "").split("#", 1)[0].strip()


def wartosci_typu(w, typ):
    """Liczba pól osobowych dokumentu z wartością danego typu."""
    return sum(1 for p in POLA_OSOBOWE if (w.get(p) or "").strip() and w.get(p + "_typ") == typ)


def cmd_draw(a):
    out = Path(a.out)
    if (out / "probka.csv").exists() and not a.nadpisz:
        sys.exit(f"[x] {out / 'probka.csv'} już istnieje — losowanie robi się raz. (--nadpisz, jeśli naprawdę od nowa)")
    with open(a.dokumenty, encoding="utf-8-sig", newline="") as f:
        dok = list(csv.DictReader(f))
    if a.szeroki:                                  # sha256 z wyniki.csv, gdy dokumenty.csv go nie ma
        with open(a.szeroki, encoding="utf-8-sig", newline="") as f:
            sha = {r["plik"]: r.get("sha256", "") for r in csv.DictReader(f)}
        for w in dok:
            w["sha256"] = w.get("sha256") or sha.get(w["plik"], "")
    brak_sha = sum(1 for w in dok if not w.get("sha256"))
    brak_url = sum(1 for w in dok if not url_pobrania(w))
    if brak_sha or brak_url:
        sys.exit(f"[x] dokumenty bez sha256: {brak_sha}, bez URL: {brak_url} — podaj --szeroki wyniki.csv "
                 f"albo sprawdź dane wejściowe")

    uszk = uszkodzone_z_long(a.long) if a.long else set()
    if not a.long:
        print("[!] bez --long: uszkodzone PDF NIE zostały wykluczone", file=sys.stderr)
    kandydaci = sorted((w for w in dok if w["plik"] not in uszk), key=lambda w: w["plik"])   # stała kolejność

    ziarno = a.ziarno if a.ziarno is not None else random.SystemRandom().randrange(2**31)
    wiersze = []

    # ---- część losowa: warstwy = instytucje ----
    wg_inst = collections.defaultdict(list)
    for w in kandydaci:
        wg_inst[w["instytucja"]].append(w)
    w_losowej = set()
    podstawowe = []
    for kod in sorted(wg_inst):
        lista = list(wg_inst[kod])
        random.Random(f"{ziarno}:losowa:{kod}").shuffle(lista)        # niezależny strumień na instytucję
        wybrane = lista[:a.n + a.rezerwa]
        if len(wybrane) < a.n + a.rezerwa:
            print(f"[!] {kod}: tylko {len(wybrane)} dokumentów do losowania", file=sys.stderr)
        for i, w in enumerate(wybrane, 1):
            rez = int(i > a.n)
            wiersze.append({"czesc": "losowa", "instytucja": kod, "kolejnosc": i, "rezerwa": rez,
                            "typ_celowany": "", "wartosci_typu": "", "plik": w["plik"],
                            "url_pobrania": url_pobrania(w), "z_archiwum": w.get("z_archiwum", ""),
                            "sha256": w["sha256"]})
            w_losowej.add(w["plik"])
            if not rez:
                podstawowe.append(w)

    # ---- część celowana: typy z mniej niż --min-typ wartości w części podstawowej ----
    w_podst = {t: sum(wartosci_typu(w, t) for w in podstawowe) for t in TYPY}
    uzyte = set(w_losowej)
    potrzeba = {}
    for t in TYPY:
        brak = max(0, a.min_typ - w_podst[t])
        if not brak:
            continue
        pula = [w for w in kandydaci if w["plik"] not in uzyte and wartosci_typu(w, t)]
        random.Random(f"{ziarno}:celowana:{t}").shuffle(pula)
        # dokumentów tyle, żeby wartości wystarczyło na brak + rezerwę (dokument może mieć kilka wartości typu)
        wybrane, suma = [], 0
        for w in pula:
            if suma >= brak + a.rezerwa_typ:
                break
            wybrane.append(w); suma += wartosci_typu(w, t)
        potrzeba[t] = {"brak": brak, "dokumentow": len(wybrane), "wartosci": suma, "pula": len(pula)}
        if suma < brak:
            print(f"[!] typ {t}: w całej próbie za mało wartości do uzupełnienia ({suma} < {brak})", file=sys.stderr)
        for i, w in enumerate(wybrane, 1):
            wiersze.append({"czesc": "celowana", "instytucja": w["instytucja"], "kolejnosc": i, "rezerwa": "",
                            "typ_celowany": t, "wartosci_typu": wartosci_typu(w, t), "plik": w["plik"],
                            "url_pobrania": url_pobrania(w), "z_archiwum": w.get("z_archiwum", ""),
                            "sha256": w["sha256"]})
            uzyte.add(w["plik"])

    # ---- zapis ----
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "probka.csv", "w", encoding="utf-8-sig", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=KOLUMNY); wr.writeheader(); wr.writerows(wiersze)

    adresy = collections.defaultdict(list)                 # instytucja -> unikalne adresy (ZIP raz)
    for w in wiersze:
        if w["url_pobrania"] not in adresy[w["instytucja"]]:
            adresy[w["instytucja"]].append(w["url_pobrania"])
    polecenia, brak_zakresu = [], []
    for kod in sorted(adresy):
        d = out / kod; d.mkdir(exist_ok=True)
        (d / "urls.txt").write_text(f"# weryfikacja trafności, ziarno {ziarno}, instytucja {kod}\n"
                                    + "".join(f"{u},{kod}\n" for u in adresy[kod]), encoding="utf-8")
        pods = Path(a.proba_dir) / kod / "podsumowanie.json" if a.proba_dir else None
        domeny = []
        if pods and pods.exists():
            domeny = json.loads(pods.read_text(encoding="utf-8")).get("zakres_domeny") or []
        if not domeny:
            brak_zakresu.append(kod)
        dom = " ".join(f"--domena {shlex.quote(x)}" for x in domeny)
        polecenia.append(f"python3 tools/fetch.py --urls {shlex.quote(str(d / 'urls.txt'))} "
                         f"--out {shlex.quote(str(d / 'pliki'))} {dom}".rstrip())
    (out / "pobierz.sh").write_text(
        "#!/usr/bin/env bash\n# ponowne pobranie próbki do weryfikacji trafności (uruchamiać z ~/inz)\nset -u\n"
        + "\n".join(polecenia) + "\n", encoding="utf-8")
    if brak_zakresu:
        print(f"[!] bez zakresu --domena (brak podsumowanie.json): {', '.join(brak_zakresu)}", file=sys.stderr)

    meta = {"ziarno": ziarno, "data_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "dokumenty_csv": Path(a.dokumenty).name, "dokumenty_sha256": sha256_pliku(a.dokumenty),
            "parametry": {"n": a.n, "rezerwa": a.rezerwa, "min_typ": a.min_typ, "rezerwa_typ": a.rezerwa_typ},
            "dokumentow": len(dok), "wykluczone_uszkodzone": len(uszk & {w["plik"] for w in dok}),
            "losowa_podstawowe": len(podstawowe), "losowa_rezerwa": len(w_losowej) - len(podstawowe),
            "wartosci_typow_w_podstawowej": w_podst, "celowana": potrzeba,
            "adresow_do_pobrania": sum(len(v) for v in adresy.values())}
    (out / "losowanie.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- konsola: tylko liczby ----
    print(f"[+] ziarno {ziarno}; dokumentów {len(dok)}, wykluczone uszkodzone {meta['wykluczone_uszkodzone']}",
          file=sys.stderr)
    print(f"[+] część losowa: {len(podstawowe)} podstawowych + {meta['losowa_rezerwa']} rezerwowych "
          f"({len(wg_inst)} instytucji)", file=sys.stderr)
    print("[i] wartości typów w części podstawowej: " + ", ".join(f"{t} {w_podst[t]}" for t in TYPY),
          file=sys.stderr)
    for t, p in potrzeba.items():
        print(f"[+] część celowana {t}: brakuje {p['brak']}, wybrano {p['dokumentow']} dok. "
              f"({p['wartosci']} wartości; pula {p['pula']} dok.)", file=sys.stderr)
    print(f"[+] {meta['adresow_do_pobrania']} adresów do pobrania -> {out}/<KOD>/urls.txt, polecenia: "
          f"{out / 'pobierz.sh'}", file=sys.stderr)


ZIP_MAX_MEMBERS = 500                     # jak w extract.py (ochrona przed zip bomb)
ZIP_MAX_TOTAL = 500 * 1024 * 1024


def sha_czlonkow_zip(p):
    """{sha256: nazwa w archiwum} dla plików w ZIP-ie; {} gdy to nie ZIP albo archiwum za duże."""
    import zipfile
    if not zipfile.is_zipfile(p):
        return {}
    wynik = {}
    with zipfile.ZipFile(p) as z:
        infos = [i for i in z.infolist() if not i.is_dir()]
        if len(infos) > ZIP_MAX_MEMBERS or sum(i.file_size for i in infos) > ZIP_MAX_TOTAL:
            return {}
        for i in infos:
            h = hashlib.sha256()
            with z.open(i) as f:
                for blok in iter(lambda: f.read(1 << 16), b""):
                    h.update(blok)
            wynik[h.hexdigest()] = i.filename
    return wynik


def cmd_check(a):
    """Porównanie sha256 pobranych plików z pełną próbą i wybór końcowej próbki (rezerwa zastępuje braki)."""
    out = Path(a.out)
    meta = json.loads((out / "losowanie.json").read_text(encoding="utf-8"))
    n, min_typ = meta["parametry"]["n"], meta["parametry"]["min_typ"]
    with open(out / "probka.csv", encoding="utf-8-sig", newline="") as f:
        probka = list(csv.DictReader(f))
    if sha256_pliku(a.dokumenty) != meta["dokumenty_sha256"]:
        sys.exit("[x] dokumenty.csv różni się od użytego przy losowaniu (sha256) — liczby typów byłyby niespójne")
    with open(a.dokumenty, encoding="utf-8-sig", newline="") as f:
        dok = {w["plik"]: w for w in csv.DictReader(f)}

    # pobrane pliki: (instytucja, url) -> ścieżka lokalna (ostatni wiersz „ok” w manifeście)
    pobrane = {}
    for kod in sorted({r["instytucja"] for r in probka}):
        man = out / kod / "pliki" / "manifest.csv"
        if not man.exists():
            print(f"[!] {kod}: brak manifestu — pliki tej instytucji niedostępne", file=sys.stderr)
            continue
        with open(man, encoding="utf-8", newline="") as f:
            for m in csv.DictReader(f):
                if m["status"] == "ok" and m["plik"] and (out / kod / "pliki" / m["plik"]).exists():
                    pobrane[(kod, m["url"])] = out / kod / "pliki" / m["plik"]

    cache_sha, cache_zip = {}, {}
    for r in probka:
        sciezka = pobrane.get((r["instytucja"], r["url_pobrania"]))
        r["lokalny_plik"], r["w_archiwum"] = "", ""
        if not sciezka:
            r["status"] = "niedostepny"
            continue
        r["lokalny_plik"] = str(sciezka)
        if r["z_archiwum"]:
            if sciezka not in cache_zip:
                cache_zip[sciezka] = sha_czlonkow_zip(sciezka)
            czlonek = cache_zip[sciezka].get(r["sha256"])
            r["status"] = "zgodny" if czlonek else "inny_sha256"
            r["w_archiwum"] = czlonek or ""
        else:
            if sciezka not in cache_sha:
                cache_sha[sciezka] = sha256_pliku(sciezka)
            r["status"] = "zgodny" if cache_sha[sciezka] == r["sha256"] else "inny_sha256"

    # ---- wybór: część losowa — pierwsze n zgodnych w kolejności (rezerwa zastępuje braki) ----
    wybrane = []
    for kod in sorted({r["instytucja"] for r in probka if r["czesc"] == "losowa"}):
        rs = sorted((r for r in probka if r["czesc"] == "losowa" and r["instytucja"] == kod),
                    key=lambda r: int(r["kolejnosc"]))
        ok = [r for r in rs if r["status"] == "zgodny"][:n]
        if len(ok) < n:
            print(f"[!] {kod}: tylko {len(ok)} z {n} dokumentów zgodnych (rezerwa wyczerpana)", file=sys.stderr)
        wybrane += ok
    licz = {t: sum(wartosci_typu(dok[r["plik"]], t) for r in wybrane) for t in TYPY}
    w_losowej = dict(licz)

    # ---- część celowana: dobierane w kolejności, aż typ ma min_typ wartości ----
    for t in TYPY:
        rs = sorted((r for r in probka if r["czesc"] == "celowana" and r["typ_celowany"] == t),
                    key=lambda r: int(r["kolejnosc"]))
        for r in rs:
            if licz[t] >= min_typ:
                break
            if r["status"] == "zgodny":
                wybrane.append(r); licz[t] += wartosci_typu(dok[r["plik"]], t)

    for r in probka:
        r["wybrany"] = int(r in wybrane)
    kol = KOLUMNY + ["status", "lokalny_plik", "w_archiwum", "wybrany"]
    with open(out / "porownanie.csv", "w", encoding="utf-8-sig", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=kol); wr.writeheader(); wr.writerows(probka)
    with open(out / "wybrane.csv", "w", encoding="utf-8-sig", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=kol); wr.writeheader(); wr.writerows(wybrane)

    # ---- konsola: tylko liczby ----
    for czesc in ("losowa", "celowana"):
        c = collections.Counter(r["status"] for r in probka if r["czesc"] == czesc)
        print(f"[i] {czesc}: " + ", ".join(f"{s} {c[s]}" for s in ("zgodny", "inny_sha256", "niedostepny")),
              file=sys.stderr)
    nl = sum(1 for r in wybrane if r["czesc"] == "losowa")
    rez = sum(1 for r in wybrane if r["czesc"] == "losowa" and r["rezerwa"] == "1")
    print(f"[+] wybrane: losowa {nl} (w tym z rezerwy {rez}), celowana "
          f"{sum(1 for r in wybrane if r['czesc'] == 'celowana')} -> {out / 'wybrane.csv'}", file=sys.stderr)
    print("[i] wartości typów (losowa / razem z celowaną): "
          + ", ".join(f"{t} {w_losowej[t]}/{licz[t]}" for t in TYPY), file=sys.stderr)
    za_malo = [t for t in TYPY if licz[t] < min_typ]
    if za_malo:
        print(f"[!] poniżej {min_typ} wartości: {', '.join(za_malo)}", file=sys.stderr)


# ---------- wersja 3: formularz oceny i podgląd dokumentu (kryteria: decyzje 2026-11-10) ----------
# Znalezisko trafne (T) = reguła zadziałała zgodnie ze swoją definicją w taksonomii v1: wartość jest tym, co zakłada
# reguła (np. OPR-01 program z wersją, OSB-02 login), a pole kanoniczne wzięło właściwą wartość. Kategoria i wrażliwość
# reguły są przyjęte z taksonomii; zastrzeżenia do taksonomii -> kolumna uwagi (nie wchodzą do liczb).
# Pominięcie (do czułości) = informacja w metadanych pliku, którą taksonomia przewiduje, a znaleziska brak.
# Informacje spoza taksonomii -> wiersz „spoza” (tylko uwagi, nie wchodzą do liczb).
# OSB-01–06 (typ wartości pola osobowego): ocena = czy typ poprawny; gdy N — poprawny typ w kolumnie „poprawne”.
# „nieustalone” jest trafne, gdy z samej wartości nie da się rzetelnie przypisać żadnego innego typu.
REGULY_TYPU = {"OSB-01": "osoba", "OSB-02": "login", "OSB-03": "email", "OSB-04": "organizacja",
               "OSB-05": "nieustalone", "OSB-06": "zaslepka"}
KOLUMNY_OCENY = ["id_wiersza", "nr", "czesc", "typ_wiersza", "plik", "format", "id_reguly", "pole", "zrodlo",
                 "kategoria", "wrazliwosc", "ocena", "poprawne", "uwagi"]


def wczytaj_wybrane(out):
    """Końcowa próbka z check; nr = pozycja w wybrane.csv (ta sama w formularzu i podglądzie)."""
    with open(Path(out) / "wybrane.csv", encoding="utf-8-sig", newline="") as f:
        return [dict(r, nr=i) for i, r in enumerate(csv.DictReader(f), 1)]


def cmd_formularz(a):
    out = Path(a.out)
    cel = out / "ocena.csv"
    if cel.exists() and not a.nadpisz:
        sys.exit(f"[x] {cel} już istnieje — nie nadpisuję wypełnianego formularza (--nadpisz, jeśli pusty)")
    wybrane = wczytaj_wybrane(out)
    with open(a.znaleziska, encoding="utf-8-sig", newline="") as f:
        zn = collections.defaultdict(list)
        for z in csv.DictReader(f):
            zn[z["plik"]].append(z)
    wiersze = []
    for r in wybrane:
        zs = zn.get(r["plik"], [])
        if r["czesc"] == "celowana":                 # tylko precyzja typu: wartości celowanego typu
            zs = [z for z in zs if REGULY_TYPU.get(z["id_reguly"]) == r["typ_celowany"]]
        for j, z in enumerate(zs, 1):
            wiersze.append({"id_wiersza": f"{r['nr']:02d}-{j:02d}", "nr": r["nr"], "czesc": r["czesc"],
                            "typ_wiersza": "znalezisko", "plik": r["plik"], "format": z.get("format", ""),
                            "id_reguly": z["id_reguly"], "pole": z["pole"], "zrodlo": z["zrodlo"],
                            "kategoria": z["kategoria"], "wrazliwosc": z["wrazliwosc"],
                            "ocena": "", "poprawne": "", "uwagi": ""})
        if r["czesc"] == "losowa":                   # potwierdzenie, że dokument sprawdzono pod kątem pominięć
            wiersze.append({"id_wiersza": f"{r['nr']:02d}-P", "nr": r["nr"], "czesc": r["czesc"],
                            "typ_wiersza": "kontrola_pominiec", "plik": r["plik"], "ocena": "", "uwagi": ""})
    with open(cel, "w", encoding="utf-8-sig", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=KOLUMNY_OCENY, restval=""); wr.writeheader(); wr.writerows(wiersze)
    c = collections.Counter(w["czesc"] for w in wiersze if w["typ_wiersza"] == "znalezisko")
    print(f"[+] formularz -> {cel}: dokumentów {len(wybrane)}, znalezisk do oceny: losowa {c['losowa']}, "
          f"celowana {c['celowana']}; wierszy kontroli pominięć {sum(1 for r in wybrane if r['czesc'] == 'losowa')}",
          file=sys.stderr)


def cmd_pokaz(a):
    """JAWNE wartości tylko na terminal: tagi exiftool dokumentu nr, pola kanoniczne i znaleziska. Nic nie zapisuje
    poza plikiem tymczasowym (plik z ZIP), usuwanym od razu."""
    import fnmatch, shutil, tempfile, zipfile
    import extract, normalize                       # te same funkcje dekodowania i ta sama mapa pól co w potoku
    out = Path(a.out)
    wybrane = {r["nr"]: r for r in wczytaj_wybrane(out)}
    if a.nr not in wybrane:
        sys.exit(f"[x] brak dokumentu nr {a.nr} (zakres 1–{len(wybrane)})")
    r = wybrane[a.nr]
    with open(a.dokumenty, encoding="utf-8-sig", newline="") as f:
        dok = next((w for w in csv.DictReader(f) if w["plik"] == r["plik"]), {})
    with open(a.znaleziska, encoding="utf-8-sig", newline="") as f:
        zn = [z for z in csv.DictReader(f) if z["plik"] == r["plik"]]

    exe, _ = extract.check_exiftool()
    tmp = None
    try:
        sciezka = Path(r["lokalny_plik"])
        if r["w_archiwum"]:
            tmp = Path(tempfile.mkdtemp(prefix="weryf_"))
            with zipfile.ZipFile(sciezka) as z, z.open(r["w_archiwum"]) as src, open(tmp / "plik", "wb") as dst:
                shutil.copyfileobj(src, dst)
            sciezka = tmp / "plik"
        dane = extract.uruchom_exiftool(exe, [sciezka])[0]
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)

    # do którego pola kanonicznego trafia tag (★ = źródło wybrane w dokumenty.csv), czy jest ignorowany
    zrodla = {}
    for pole, _, wzorce in normalize.MAPA:
        for wz in wzorce:
            zrodla.setdefault(wz, []).append(pole)
    wybrane_zr = {dok.get(f"{p}_zrodlo"): p for p, _, _ in normalize.MAPA if dok.get(f"{p}_zrodlo")}

    def opis_tagu(k):
        pola = sorted({p for wz, ps in zrodla.items() if fnmatch.fnmatchcase(k, wz) for p in ps})
        if k in wybrane_zr:
            return f"★ {wybrane_zr[k]}"
        if pola:
            return "→ " + ",".join(pola)
        if any(fnmatch.fnmatchcase(k, w) for _, ws in normalize.ZNACZNIKI for w in ws):
            return "znacznik"
        return "ign" if normalize.pasuje(k, normalize.IGNOROWANE) else "?"

    print(f"### DOKUMENT nr {a.nr} [{r['czesc']}{(' / ' + r['typ_celowany']) if r['typ_celowany'] else ''}] "
          f"{dok.get('format', '')}  — JAWNE WARTOŚCI: nie zapisuj, nie wysyłaj")
    print("\n== tagi exiftool (★ = źródło pola kanonicznego, → = inne źródło pola, ign = ignorowany, "
          "[typ] = typ wartości liczony teraz, [ścieżka] = flaga ścieżki)")
    for k, v in sorted(dane.items()):
        if k.startswith(extract.DROP_PREFIXES):
            continue
        s = extract.to_str(v)
        if not s:
            continue
        znaczki = []
        if extract.czy_osobowy(k):
            znaczki.append(f"[{extract.typ_wartosci(s)}]")
        if extract.zawiera_sciezke(s):
            znaczki.append("[ścieżka]")
        print(f"  {k:42s} {opis_tagu(k):26s} {' '.join(znaczki):14s} {s[:a.szerokosc]}")
    print("\n== znaleziska (id_wiersza w formularzu = nr-kolejny)")
    if r["czesc"] == "celowana":
        zn = [z for z in zn if REGULY_TYPU.get(z["id_reguly"]) == r["typ_celowany"]]
        print("   (część celowana: oceniasz tylko typ tych wartości)")
    for j, z in enumerate(zn, 1):
        print(f"  {a.nr:02d}-{j:02d}  {z['id_reguly']:7s} {z['wrazliwosc']}  {z['kategoria']:38s} "
              f"{z['pole']:22s} {z['zrodlo']}")
    if not zn:
        print("   brak znalezisk")
    if r["czesc"] == "losowa":
        print(f"\n   pominięcia: dopisz wiersze „pominiecie” / „spoza” z nr {a.nr}; potem {a.nr:02d}-P ocena = T")


# ---------- wersja 4: wyniki weryfikacji (precyzja, czułość, trafność typu) ----------
# Konwencje formularza (2026-11-10): N + puste „poprawne” = znalezisko, którego nie powinno być (tylko precyzja);
# N + reguła (albo typ dla OSB) w „poprawne” = zła reguła zamiast właściwej (błąd precyzji reguły zastosowanej
# i pominięcie reguły właściwej); wiersz „pominiecie” = brak znaleziska (czułość); „spoza” = poza liczbami.
# Wiersz pominięcia z „(→)” w uwagach = informacja w niewybranym źródle pola; liczony osobno (--bez-niewybranych).

def wilson(k, n, z=1.96):
    """95% przedział Wilsona dla odsetka k/n -> (dolna, górna); (0, 0) dla n = 0."""
    if not n:
        return 0.0, 0.0
    p = k / n
    m = z * ((p * (1 - p) + z * z / (4 * n)) / n) ** 0.5
    d = 1 + z * z / n
    return (p + z * z / (2 * n) - m) / d, (p + z * z / (2 * n) + m) / d


def cmd_wyniki(a):
    import classify                                  # wrażliwość i kategoria reguł z jednego źródła (REGULY)
    out = Path(a.out)
    meta_reg = {r[0]: {"kategoria": r[3], "wrazliwosc": r[4]} for r in classify.REGULY}
    reg_typu = {t: r for r, t in REGULY_TYPU.items()}
    with open(a.ocena or out / "ocena.csv", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    # ---- kontrola kompletności formularza ----
    bledy = []
    for r in rows:
        if r["typ_wiersza"] == "znalezisko" and r["ocena"].strip().upper() not in ("T", "N"):
            bledy.append(f"{r['id_wiersza']}: brak oceny T/N")
        if r["typ_wiersza"] == "znalezisko" and r["ocena"].strip().upper() == "N" and r["poprawne"].strip():
            p = r["poprawne"].strip()
            if p not in meta_reg and p not in reg_typu:
                bledy.append(f"{r['id_wiersza']}: nieznana wartość „poprawne”: {p}")
        if r["typ_wiersza"] == "kontrola_pominiec" and r["ocena"].strip().upper() != "T":
            bledy.append(f"{r['id_wiersza']}: dokument niesprawdzony pod kątem pominięć")
        if r["typ_wiersza"] == "pominiecie" and r["id_reguly"].strip() not in meta_reg:
            bledy.append(f"nr {r['nr']}: pominięcie bez poprawnego id_reguly")
    if bledy:
        print("[!] formularz niekompletny:\n    " + "\n    ".join(bledy[:20]), file=sys.stderr)
        if not a.mimo_bledow:
            sys.exit(1)

    def regula_poprawna(r):
        """ID reguły, która powinna była zadziałać (albo '' gdy znaleziska nie powinno być)."""
        p = r["poprawne"].strip()
        return reg_typu.get(p, p)

    zn = [r for r in rows if r["typ_wiersza"] == "znalezisko"]
    los = [r for r in zn if r["czesc"] == "losowa"]
    T = [r for r in los if r["ocena"].upper() == "T"]
    N = [r for r in los if r["ocena"].upper() == "N"]
    pom_all = [r for r in rows if r["typ_wiersza"] == "pominiecie"]
    niewybrane = [r for r in pom_all if "(→)" in r["uwagi"]]
    pom = [r for r in pom_all if not (a.bez_niewybranych and r in niewybrane)]
    fn_zla = [r for r in N if regula_poprawna(r)]                 # zła reguła zamiast właściwej
    fn = [regula_poprawna(r) for r in fn_zla] + [r["id_reguly"].strip() for r in pom]

    def odsetek(k, n):
        lo, hi = wilson(k, n)
        return {"k": k, "n": n, "wartosc": round(k / n, 4) if n else None,
                "przedzial95": [round(lo, 4), round(hi, 4)]}

    wyn = {"dokumentow_losowa": len({r["nr"] for r in los} | {r["nr"] for r in rows
                                     if r["typ_wiersza"] == "kontrola_pominiec"}),
           "znalezisk_losowa": len(los), "pominiec": len(pom), "pominiec_niewybrane_zrodlo": len(niewybrane),
           "liczone_niewybrane": not a.bez_niewybranych,
           "spoza": sum(1 for r in rows if r["typ_wiersza"] == "spoza"),
           "precyzja": odsetek(len(T), len(T) + len(N)),
           "czulosc": odsetek(len(T), len(T) + len(fn))}
    # trafność wrażliwości: znalezisko ma właściwą wrażliwość, także gdy reguła jest inna (np. OPR-09 vs URZ-02)
    ok_wr = len(T) + sum(1 for r in fn_zla if meta_reg[regula_poprawna(r)]["wrazliwosc"] == r["wrazliwosc"])
    wyn["trafnosc_wrazliwosci"] = odsetek(ok_wr, len(los))
    ok_kat = len(T) + sum(1 for r in fn_zla if meta_reg[regula_poprawna(r)]["kategoria"] == r["kategoria"])
    wyn["trafnosc_kategorii"] = odsetek(ok_kat, len(los))

    # ---- podziały: precyzja wg reguły zastosowanej, czułość wg reguły właściwej ----
    def podzial(klucz):
        tp, fp, fnn = collections.Counter(), collections.Counter(), collections.Counter()
        for r in T:
            tp[klucz(r["id_reguly"])] += 1
        for r in N:
            fp[klucz(r["id_reguly"])] += 1
        for rid in fn:
            fnn[klucz(rid)] += 1
        return {k: {"T": tp[k], "N": fp[k], "pominiete": fnn[k],
                    "precyzja": round(tp[k] / (tp[k] + fp[k]), 4) if tp[k] + fp[k] else None,
                    "czulosc": round(tp[k] / (tp[k] + fnn[k]), 4) if tp[k] + fnn[k] else None}
                for k in sorted(set(tp) | set(fp) | set(fnn))}
    wyn["wg_kategorii"] = podzial(lambda rid: meta_reg[rid]["kategoria"])
    wyn["wg_wrazliwosci"] = podzial(lambda rid: meta_reg[rid]["wrazliwosc"])
    wyn["wg_reguly"] = podzial(lambda rid: rid)
    wyn["wg_sektora"] = {}
    for s in sorted({r["plik"].split("-")[0] for r in los}):
        ts = sum(1 for r in T if r["plik"].startswith(s + "-")); ns = sum(1 for r in N if r["plik"].startswith(s + "-"))
        wyn["wg_sektora"][s] = odsetek(ts, ts + ns)

    # ---- typ wartości (OSB-01–06): obie części ----
    osb = [r for r in zn if r["id_reguly"] in REGULY_TYPU]
    macierz = collections.Counter()
    for r in osb:
        przyp = REGULY_TYPU[r["id_reguly"]]
        prawd = przyp if r["ocena"].upper() == "T" else (r["poprawne"].strip() or "brak_wartosci")
        macierz[(przyp, prawd)] += 1
    wyn["typ_wartosci"] = {
        "trafnosc": odsetek(sum(1 for r in osb if r["ocena"].upper() == "T"), len(osb)),
        "precyzja_wg_typu": {t: odsetek(macierz[(t, t)], sum(v for (p, _), v in macierz.items() if p == t))
                             for t in TYPY},
        "macierz": {f"{p} -> {t}": v for (p, t), v in sorted(macierz.items())}}

    # ---- lista błędów (do kroku 7) ----
    grupy = collections.Counter((r["id_reguly"], regula_poprawna(r) or "(brak)", r["uwagi"].strip()) for r in N)
    grupy.update((f"(pominięcie) {r['id_reguly']}", r["zrodlo"], r["uwagi"].strip()) for r in pom_all)
    with open(out / "bledy.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f); w.writerow(["regula", "poprawna_lub_tag", "uwagi", "liczba"])
        for (a1, a2, a3), n in grupy.most_common():
            w.writerow([a1, a2, a3, n])
    (out / "wyniki.json").write_text(json.dumps(wyn, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- konsola: tylko liczby ----
    def fmt(d):
        return f"{100 * d['wartosc']:.1f}% ({d['k']}/{d['n']}; 95% CI {100 * d['przedzial95'][0]:.1f}–" \
               f"{100 * d['przedzial95'][1]:.1f}%)" if d["n"] else "—"
    print(f"[+] część losowa: {wyn['dokumentow_losowa']} dokumentów, {len(los)} znalezisk, pominięć {len(pom)} "
          f"(w tym z niewybranego źródła {len(niewybrane)}, liczone: {'tak' if not a.bez_niewybranych else 'nie'}), "
          f"spoza {wyn['spoza']}", file=sys.stderr)
    print(f"    precyzja:             {fmt(wyn['precyzja'])}", file=sys.stderr)
    print(f"    czułość:              {fmt(wyn['czulosc'])}", file=sys.stderr)
    print(f"    trafność wrażliwości: {fmt(wyn['trafnosc_wrazliwosci'])}", file=sys.stderr)
    print(f"    trafność kategorii:   {fmt(wyn['trafnosc_kategorii'])}", file=sys.stderr)
    print("[i] wg kategorii (T / N / pominięte):", file=sys.stderr)
    for k, v in wyn["wg_kategorii"].items():
        print(f"    {k:40s} {v['T']:4d} {v['N']:3d} {v['pominiete']:3d}", file=sys.stderr)
    print("[i] precyzja wg sektora: " + ", ".join(f"{s} {fmt(v)}" for s, v in wyn["wg_sektora"].items()),
          file=sys.stderr)
    print(f"[+] typ wartości (OSB, obie części): {fmt(wyn['typ_wartosci']['trafnosc'])}", file=sys.stderr)
    for t, v in wyn["typ_wartosci"]["precyzja_wg_typu"].items():
        print(f"    {t:12s} {fmt(v)}", file=sys.stderr)
    print("    pomyłki: " + ", ".join(f"{k} {v}" for k, v in wyn["typ_wartosci"]["macierz"].items()
                                     if k.split(" -> ")[0] != k.split(" -> ")[1]), file=sys.stderr)
    print(f"[+] szczegóły -> {out / 'wyniki.json'}, błędy pogrupowane -> {out / 'bledy.csv'}", file=sys.stderr)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = p.add_subparsers(dest="cmd", required=True)
    wy = sp.add_parser("wyniki", help="precyzja, czułość i trafność typu z wypełnionego formularza")
    wy.add_argument("--out", required=True, help="katalog weryfikacji (wyniki.json i bledy.csv trafiają tutaj)")
    wy.add_argument("--ocena", help="wypełniony formularz (domyślnie <out>/ocena.csv)")
    wy.add_argument("--bez-niewybranych", action="store_true",
                    help="nie licz pominięć z niewybranego źródła pola (uwaga z „(→)”) do czułości")
    wy.add_argument("--mimo-bledow", action="store_true", help="licz mimo niekompletnego formularza")
    wy.set_defaults(func=cmd_wyniki)
    fo = sp.add_parser("formularz", help="formularz oceny ocena.csv (bez jawnych wartości)")
    fo.add_argument("--znaleziska", required=True, help="znaleziska.csv z classify.py")
    fo.add_argument("--out", required=True, help="katalog weryfikacji (z wybrane.csv)")
    fo.add_argument("--nadpisz", action="store_true")
    fo.set_defaults(func=cmd_formularz)
    po = sp.add_parser("pokaz", help="podgląd dokumentu nr N na terminal (JAWNE wartości, nic nie zapisuje)")
    po.add_argument("--nr", type=int, required=True)
    po.add_argument("--dokumenty", required=True)
    po.add_argument("--znaleziska", required=True)
    po.add_argument("--out", required=True)
    po.add_argument("--szerokosc", type=int, default=90, help="maks. długość wyświetlanej wartości")
    po.set_defaults(func=cmd_pokaz)
    c = sp.add_parser("check", help="porównanie sha256 pobranych plików i wybór końcowej próbki")
    c.add_argument("--dokumenty", required=True, help="dokumenty.csv z normalize.py (te same co przy draw)")
    c.add_argument("--out", required=True, help="katalog weryfikacji (z probka.csv i losowanie.json)")
    c.set_defaults(func=cmd_check)
    d = sp.add_parser("draw", help="losowanie próbki do weryfikacji i listy adresów do ponownego pobrania")
    d.add_argument("--dokumenty", required=True, help="dokumenty.csv z normalize.py")
    d.add_argument("--long", help="wyniki_long.csv z extract.py (wykluczenie uszkodzonych PDF)")
    d.add_argument("--szeroki", help="wyniki.csv z extract.py (sha256, gdy brak w dokumenty.csv)")
    d.add_argument("--proba-dir", help="katalog pełnej próby z <KOD>/podsumowanie.json (zakres --domena)")
    d.add_argument("--out", required=True)
    d.add_argument("--n", type=int, default=5, help="dokumentów na instytucję w części losowej (domyślnie 5)")
    d.add_argument("--rezerwa", type=int, default=2, help="rezerwowych na instytucję (domyślnie 2)")
    d.add_argument("--min-typ", type=int, default=10, help="min. wartości każdego typu (domyślnie 10)")
    d.add_argument("--rezerwa-typ", type=int, default=5, help="rezerwa wartości w części celowanej (domyślnie 5)")
    d.add_argument("--ziarno", type=int, help="ziarno (domyślnie losowe; zapisywane w losowanie.json)")
    d.add_argument("--nadpisz", action="store_true")
    d.set_defaults(func=cmd_draw)
    a = p.parse_args(); a.func(a)


if __name__ == "__main__":
    main()
