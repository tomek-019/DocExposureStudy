#!/usr/bin/env python3
"""
verify.py — etap WERYFIKACJI TRAFNOŚCI klasyfikacji (krok 5 harmonogramu v3, dziennik 2026-11-10).

Podkomendy:
  draw    (wersja 1) losowanie próbki i przygotowanie ponownego pobrania
  check   (wersja 2) porównanie sha256 pobranych plików z pełną próbą i wybór końcowej próbki
Kolejne (formularz oceny, precyzja i czułość) w następnych wersjach.

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


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = p.add_subparsers(dest="cmd", required=True)
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
