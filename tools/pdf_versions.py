#!/usr/bin/env python3
"""
pdf_versions.py — etap ODCZYTU WCZEŚNIEJSZYCH WERSJI PDF. Tylko czyta surowe pliki, niczego w nich nie zmienia i nie usuwa.

Zapis przyrostowy dokleja zmiany na końcu pliku, więc początek pliku do wcześniejszego %%EOF to wcześniejsza wersja.
exiftool czyta tylko wersję najnowszą, dlatego każdą wcześniejszą wersję zapisujemy jako plik tymczasowy i czytamy osobno.
Za granicę wersji uznajemy %%EOF poprzedzony przez `startxref N`, gdzie N > 0 wskazuje tabelę xref albo obiekt.
Odrzuca to pierwszą sekcję pliku linearyzowanego (startxref 0) i przypadkowe %%EOF w strumieniach.

Wyniki (bez jawnych wartości pól osobowych, pseudonimy te same co w extract.py):
  pdf_wersje_pliki.csv   jeden wiersz na PDF: liczba wersji, liczniki zmian, uszkodzenia, liczniki tagów w surowych bajtach
  pdf_wersje_zmiany.csv  jeden wiersz na tag, który w wersji wcześniejszej różni się od najnowszej
Kontrola: pseudonimizacja wersji najnowszej porównywana jest z wyniki_long.csv (oczekiwane 0 różnic).

Przykład:
  python3 tools/pdf_versions.py --proba dane/proba
"""
import argparse, collections, csv, hashlib, re, sys, tempfile, zipfile
from pathlib import Path

import extract

EOF_RE = re.compile(rb"%%EOF(\r\n|\r|\n)?")
STARTXREF_RE = re.compile(rb"startxref\s+(\d+)\s*$")
CEL_XREF_RE = re.compile(rb"\s*(xref|\d+\s+\d+\s+obj)")
USZKODZONY_RE = re.compile(r"(?i)invalid xref|root object|xref|trailer|corrupt|not a valid pdf")
POMIJANE_W_POROWNANIU = ("System:", "File:", "ExifTool:")   # techniczne: inne dla pliku tymczasowego
BIALE = b" \t\r\n\f\0"
PACZKA = 50                                                 # liczba PDF-ów na jedno uruchomienie exiftool
SUROWE = {"surowe_info": rb"/Info", "surowe_author": rb"/Author", "surowe_creator": rb"/Creator",
          "surowe_producer": rb"/Producer", "surowe_xmp": rb"xmpmeta"}   # same liczby wystąpień, nigdy wartości

POLA_PLIKI = ["plik", "instytucja", "sha256", "liniowy", "zaszyfrowany", "n_eof", "n_granic_odrzuconych", "liczba_wersji",
              "wersji_wczesniejszych_ok", "wersji_wczesniejszych_blad", "n_zmian", "n_usuniete_pozniej", "n_zmienione",
              "n_dodane_pozniej", "n_osobowe_tylko_wczesniej", "uszkodzony", "ostrzezenie_exiftool", "n_tagow_najnowsza",
              "n_osobowych_najnowsza", *SUROWE]
POLA_ZMIANY = ["plik", "instytucja", "nr_wersji", "liczba_wersji", "grupa", "tag", "zmiana", "wartosc_wczesniejsza",
               "wartosc_najnowsza", "pseudonimizowane", "typ_wczesniejsza", "typ_najnowsza", "sciezka_wczesniejsza"]


def wczesniejsze_wersje(dane: bytes) -> tuple:
    """Zwraca (końce wcześniejszych wersji, liczba %%EOF, liczba odrzuconych granic)."""
    naglowek = max(dane.find(b"%PDF-", 0, 1024), 0)          # offsety xref liczone są od nagłówka
    calosc = dane.rstrip(BIALE)
    konce, n_eof, odrzucone = [], 0, 0
    for m in EOF_RE.finditer(dane):
        n_eof += 1
        s = STARTXREF_RE.search(dane[max(0, m.start() - 64):m.start()])
        cel = int(s.group(1)) + naglowek if s else 0
        if not s or int(s.group(1)) == 0 or cel >= m.start() or not CEL_XREF_RE.match(dane, cel):
            odrzucone += 1
        elif dane[:m.end()].rstrip(BIALE) != calosc:
            konce.append(m.end())
    return konce, n_eof, odrzucone


def przetworz_tagi(key: bytes, d: dict) -> dict:
    """Odwzorowanie pętli po tagach z extract.cmd_extract: {klucz: (wartość, pseudonimizowane, typ, ścieżka)}."""
    wynik = {}
    for k, v in d.items():
        if k.startswith(extract.DROP_PREFIXES):
            continue
        s = extract.to_str(v)
        sciezka = bool(s and extract.zawiera_sciezke(s))
        typ, pseud = "", False
        if s and extract.czy_osobowy(k) and k in extract.WARUNKOWE and extract.typ_wartosci(s) != "osoba":
            s, pseud = extract.scrub_text(key, s)
        elif s and extract.czy_osobowy(k):
            typ = extract.typ_wartosci(s)
            m = extract.EMAIL_RE.fullmatch(s.strip())
            s = f"{extract.pseud(key, m.group(1), 'E')}@{m.group(2)}" if m else extract.pseud(key, s, "P")
            pseud = True
        else:
            s, pseud = extract.scrub_text(key, s)
        wynik[k] = (s, pseud, typ, sciezka)
    return wynik


def ostrzezenia(d: dict) -> str:
    return " | ".join(extract.to_str(d[k]) for k in ("ExifTool:Error", "ExifTool:Warning") if d.get(k))


def znajdz_pdfy(proba: Path) -> list:
    """Lista (ścieżka, klucz `instytucja/ścieżka` jak w extract.py, instytucja) dla PDF-ów rozpoznanych po nagłówku."""
    wynik = []
    for indir in sorted(p for p in proba.glob("*/pliki") if p.is_dir()):
        manifest_p = indir / "manifest.csv"
        if not manifest_p.exists():
            sys.exit(f"[x] Brak {manifest_p}")
        with open(manifest_p, encoding="utf-8") as f:
            manifest = {r["plik"]: r for r in csv.DictReader(f) if r.get("plik")}
        for p in sorted(x for x in indir.rglob("*") if x.is_file() and x.name != "manifest.csv"):
            with open(p, "rb") as f:
                if b"%PDF-" not in f.read(1024) or zipfile.is_zipfile(p):   # ZIP bez kompresji też może zawierać nagłówek
                    continue
            rel = p.relative_to(indir).as_posix()
            z_archiwum = p.parent.parent.name == extract.UNZIP_DIR
            m = manifest.get(p.parent.name if z_archiwum else p.name) or manifest.get(rel) or {}
            inst = m.get("instytucja", "") or indir.parent.name
            wynik.append((p, f"{inst}/{rel}", inst))
    return wynik


def wczytaj_long(sciezka: Path) -> dict:
    if not sciezka.exists():
        sys.exit(f"[x] Brak {sciezka} — bez niego nie ma kontroli zgodności pseudonimów")
    wynik = collections.defaultdict(dict)
    with open(sciezka, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            wynik[r["plik"]][f"{r['grupa']}:{r['tag']}"] = (r["wartosc"], r["typ_wartosci"], r["sciezka"] == "1")
    return wynik


def porownaj(wczesniejsza: dict, najnowsza: dict):
    """Tagi różniące się między wersją wcześniejszą a najnowszą: (klucz, rodzaj zmiany)."""
    for k in sorted(set(wczesniejsza) | set(najnowsza)):
        if k.startswith(POMIJANE_W_POROWNANIU):
            continue
        a, b = wczesniejsza.get(k, ("",))[0], najnowsza.get(k, ("",))[0]
        if a != b:
            yield k, "usuniete_pozniej" if a and not b else "dodane_pozniej" if b and not a else "zmienione"


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--proba", default="dane/proba", help="katalog z podkatalogami <KOD>/pliki/")
    p.add_argument("--key-file", default=extract.DOMYSLNY_KLUCZ, help="klucz HMAC z etapu extract")
    a = p.parse_args()
    proba = Path(a.proba)
    key_p = Path(a.key_file).expanduser()
    if not key_p.exists():                                  # load_key utworzyłby nowy klucz = niezgodne pseudonimy
        sys.exit(f"[x] Brak klucza {key_p}; nowy klucz dałby pseudonimy niezgodne z wyniki.csv")
    key = extract.load_key(key_p)
    exe, ver = extract.check_exiftool()
    long_ = wczytaj_long(proba / "wyniki_long.csv")
    pdfy = znajdz_pdfy(proba)
    if not pdfy:
        sys.exit(f"[x] Brak plików PDF w {proba}/*/pliki")

    wiersze_pliki, wiersze_zmiany = [], []
    rozbieznosci = collections.Counter()
    znalezione = {klucz for _, klucz, _ in pdfy}
    rozbieznosci["pdf_z_wyniki_long_nieznaleziony"] = sum(
        1 for plik, tagi in long_.items() if tagi.get("File:FileType", ("",))[0] == "PDF" and plik not in znalezione)
    tagi_zmian = collections.Counter()
    for start in range(0, len(pdfy), PACZKA):
        with tempfile.TemporaryDirectory(prefix="pdfver_") as tmp:      # usuwany także po błędzie
            paczka, do_odczytu = [], []
            for i, (sc, klucz, inst) in enumerate(pdfy[start:start + PACZKA]):
                dane = sc.read_bytes()
                konce, n_eof, odrzucone = wczesniejsze_wersje(dane)
                sciezki = []
                for j, koniec in enumerate(konce):
                    t = Path(tmp) / f"{i}_{j}.pdf"
                    t.write_bytes(dane[:koniec])
                    sciezki.append(t)
                paczka.append((sc, klucz, inst, dane, n_eof, odrzucone, sciezki))
                do_odczytu += [sc, *sciezki]
            odczyt = {d.get("SourceFile"): d for d in extract.uruchom_exiftool(exe, do_odczytu)}

            for sc, klucz, inst, dane, n_eof, odrzucone, sciezki in paczka:
                d = odczyt.get(str(sc), {})
                najnowsza = przetworz_tagi(key, d)
                wzorzec = long_.get(klucz)
                if wzorzec is None:
                    rozbieznosci["plik_poza_wyniki_long"] += 1
                else:
                    for k in set(najnowsza) | set(wzorzec):
                        if k not in najnowsza or k not in wzorzec:
                            rozbieznosci["tag_tylko_po_jednej_stronie"] += 1
                        elif najnowsza[k][0] != wzorzec[k][0]:
                            rozbieznosci["wartosc"] += 1
                        elif najnowsza[k][2] != wzorzec[k][1] or najnowsza[k][3] != wzorzec[k][2]:
                            rozbieznosci["typ_lub_sciezka"] += 1
                licz = collections.Counter()
                osobowe_tylko = set()
                ok, blad = 0, 0
                for nr, t in enumerate(sciezki, 1):
                    dw = odczyt.get(str(t), {})
                    uwagi = ostrzezenia(dw)
                    if not dw or dw.get("ExifTool:Error") or USZKODZONY_RE.search(uwagi):
                        blad += 1
                        continue
                    ok += 1
                    wcz = przetworz_tagi(key, dw)
                    for k, zmiana in porownaj(wcz, najnowsza):
                        licz[zmiana] += 1
                        tagi_zmian[k] += 1
                        w, n = wcz.get(k, ("", False, "", False)), najnowsza.get(k, ("", False, "", False))
                        if w[0] and w[1] and zmiana != "dodane_pozniej":
                            osobowe_tylko.add(k)
                        wiersze_zmiany.append({
                            "plik": klucz, "instytucja": inst, "nr_wersji": nr, "liczba_wersji": len(sciezki) + 1,
                            "grupa": k.split(":", 1)[0], "tag": k.split(":", 1)[-1], "zmiana": zmiana,
                            "wartosc_wczesniejsza": w[0], "wartosc_najnowsza": n[0], "pseudonimizowane": int(w[1] or n[1]),
                            "typ_wczesniejsza": w[2], "typ_najnowsza": n[2], "sciezka_wczesniejsza": int(w[3])})
                uwagi = ostrzezenia(d)
                wiersze_pliki.append({
                    "plik": klucz, "instytucja": inst, "sha256": hashlib.sha256(dane).hexdigest(),
                    "liniowy": int(b"/Linearized" in dane[:2048]), "zaszyfrowany": int(b"/Encrypt" in dane),
                    "n_eof": n_eof, "n_granic_odrzuconych": odrzucone, "liczba_wersji": len(sciezki) + 1,
                    "wersji_wczesniejszych_ok": ok, "wersji_wczesniejszych_blad": blad, "n_zmian": sum(licz.values()),
                    "n_usuniete_pozniej": licz["usuniete_pozniej"], "n_zmienione": licz["zmienione"],
                    "n_dodane_pozniej": licz["dodane_pozniej"], "n_osobowe_tylko_wczesniej": len(osobowe_tylko),
                    "uszkodzony": int(bool(USZKODZONY_RE.search(uwagi))), "ostrzezenie_exiftool": uwagi[:120],
                    "n_tagow_najnowsza": sum(1 for k in najnowsza if not k.startswith(POMIJANE_W_POROWNANIU)),
                    "n_osobowych_najnowsza": sum(1 for w in najnowsza.values() if w[1] and w[2]),
                    **{k: dane.count(w) for k, w in SUROWE.items()}})
        print(f"[i] {min(start + PACZKA, len(pdfy))}/{len(pdfy)}", file=sys.stderr)

    for nazwa, pola, wiersze in (("pdf_wersje_pliki.csv", POLA_PLIKI, wiersze_pliki),
                                 ("pdf_wersje_zmiany.csv", POLA_ZMIANY, wiersze_zmiany)):
        with open(proba / nazwa, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=pola); w.writeheader(); w.writerows(wiersze)

    # Konsola: same liczby, kody instytucji, nazwy tagów i skrót sha256 zamiast nazwy pliku — można przekazać dalej.
    z_wersjami = [r for r in wiersze_pliki if r["liczba_wersji"] > 1]
    print(f"exiftool {ver}; PDF: {len(wiersze_pliki)}; z wcześniejszą wersją: {len(z_wersjami)}; "
          f"wersji wcześniejszych: odczytane {sum(r['wersji_wczesniejszych_ok'] for r in wiersze_pliki)}, "
          f"nieodczytane {sum(r['wersji_wczesniejszych_blad'] for r in wiersze_pliki)}")
    print(f"pliki liniowe: {sum(r['liniowy'] for r in wiersze_pliki)}; zaszyfrowane: {sum(r['zaszyfrowany'] for r in wiersze_pliki)}; "
          f"z odrzuconą granicą: {sum(1 for r in wiersze_pliki if r['n_granic_odrzuconych'])}")
    print(f"KONTROLA zgodności z wyniki_long.csv: {dict(+rozbieznosci) or 'brak różnic'}")
    print(f"PDF z różnicą metadanych: {sum(1 for r in z_wersjami if r['n_zmian'])}; "
          f"z polem osobowym usuniętym lub zmienionym później: {sum(1 for r in z_wersjami if r['n_osobowe_tylko_wczesniej'])}")
    print("\ninstytucja: PDF / z wersją / z różnicą / z polem osobowym tylko wcześniej")
    for inst in sorted({r["instytucja"] for r in wiersze_pliki}):
        rr = [r for r in wiersze_pliki if r["instytucja"] == inst]
        print(f"  {inst}: {len(rr)} / {sum(r['liczba_wersji'] > 1 for r in rr)} / {sum(r['n_zmian'] > 0 for r in rr)} / "
              f"{sum(r['n_osobowe_tylko_wczesniej'] > 0 for r in rr)}")
    print("\nnajczęściej zmieniane tagi:")
    for k, n in tagi_zmian.most_common(20):
        print(f"  {n:5d}  {k}")
    print("\nPDF z uszkodzoną strukturą (sha256[:8], tagi, osobowe, surowe info/author/creator/producer/xmp, szyfr., ostrzeżenie):")
    for r in (r for r in wiersze_pliki if r["uszkodzony"]):
        print(f"  {r['instytucja']} {r['sha256'][:8]} tagi={r['n_tagow_najnowsza']} osob={r['n_osobowych_najnowsza']} "
              f"surowe={r['surowe_info']}/{r['surowe_author']}/{r['surowe_creator']}/{r['surowe_producer']}/{r['surowe_xmp']} "
              f"szyfr={r['zaszyfrowany']} [{r['ostrzezenie_exiftool'][:50]}]")
    print(f"\n[+] {proba}/pdf_wersje_pliki.csv, {proba}/pdf_wersje_zmiany.csv ({len(wiersze_zmiany)} zmian)", file=sys.stderr)
    if +rozbieznosci:
        print("[!] KONTROLA nie przeszła: nie opieraj się na wynikach przed wyjaśnieniem różnic", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
