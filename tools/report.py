#!/usr/bin/env python3
"""
report.py — etap ZESTAWIEŃ RQ2 i RQ3: dokumenty.csv (+ wyniki_long.csv) -> trzy pliki CSV.

RQ2 — elementy profilu instytucji (zamknięta lista P1–P6, dziennik 2026-11-08):
  P1 osoby           różne pseudonimy (autor, ostatnio_zapisal) o typie osoba/login/email, ich typy,
                     maks. liczba dokumentów jednego pseudonimu, liczba różnych domen e-mail
  P2 oprogramowanie  różne rodziny programów i różne pary program+wersja
  P3 wersje          a) rozrzut: rodziny z >= 2 wersjami, maks. liczba wersji w rodzinie
                     b) aktualizacje: para pseudonim x rodzina programu -> zmiana_na_nowsza / bez_zmiany /
                        niejednoznaczne / za_malo_danych (osoba = ostatnio_zapisal, w razie braku autor;
                        wersja z program_tworzacy, a gdy brak w nazwie — z wersja_aplikacji; bez program_pdf)
  P4 systemy/usługi  znaczniki (system_dokumentow, sharepoint, etykieta_msip, …) i pole system
  P5 sprzęt          różne pary producent+model, liczba różnych numerów seryjnych
  P6 wspólne źródła  identyfikatory oryginału i szablony występujące w >= 2 dokumentach

RQ3 — poziom zawartości metadanych dokumentu (dziennik 2026-11-08):
  pelne        pole osobowe o typie osoba/login/email/organizacja albo niepuste pole firma
  czesciowe    brak powyższych, ale jest oprogramowanie, czas lub urządzenie, albo zaślepka (flaga_zaslepka=1)
               (urządzenie dodane 2026-11-08: obraz z samym producentem/modelem nie jest „minimalny”)
  minimalne    pozostałe (tylko cechy formatu albo brak metadanych)
  nieustalone  plik z uszkodzoną strukturą (błąd ExifTool w wyniki_long.csv albo lista --uszkodzone)

Wyniki zawierają pseudonimy i nazwy programów — tylko lokalnie. Konsola pokazuje wyłącznie liczby.

Przykład:
  python3 tools/report.py --dokumenty dane/proba/dokumenty.csv --long dane/proba/wyniki_long.csv \\
                          --out-dir dane/proba/raport --prog-dni 180
"""
import argparse, collections, csv, datetime as dt, re, sys
from pathlib import Path

OSOBA_TYPY = {"osoba", "login", "email"}                   # P1, P3: typy oznaczające osobę
PELNE_TYPY = OSOBA_TYPY | {"organizacja"}                  # RQ3: poziom „pelne”
POLA_OSOBOWE = ("autor", "ostatnio_zapisal", "prawa_autorskie", "wlasciciel_urzadzenia")
POLA_PROGRAMOW = ("program_tworzacy", "program_pdf", "program_edycji_obrazu")
POLA_OPROG = ("program_tworzacy", "program_pdf", "wersja_aplikacji", "program_edycji_obrazu",
              "biblioteka_xmp", "system", "historia_programy")
POLA_CZASU_DATY = ("data_utworzenia", "data_modyfikacji", "data_metadanych", "data_wydruku")
POLA_CZASU_INNE = ("czas_edycji_min", "liczba_rewizji", "historia_daty")
POLA_URZADZEN = ("urzadzenie_producent", "urzadzenie_model", "obiektyw", "numer_seryjny", "firmware")
WYNIKI_P3 = ("zmiana_na_nowsza", "bez_zmiany", "niejednoznaczne", "za_malo_danych")
POZIOMY = ("pelne", "czesciowe", "minimalne", "nieustalone")
RE_USZKODZONY = re.compile(r"xref|root object", re.I)       # komunikaty ExifTool dla 10 uszkodzonych PDF

# wersja: numer z kropką (np. 16.0, 2.10.38) albo rok 1990–2039 (np. „Word 2016”); zgodne ze wzorcem classify
RE_WERSJA = re.compile(r"\b\d+(?:\.\d+)+\b|\b(?:19|20)\d{2}\b")


def sektor(kod):
    return kod.split("-")[0] if kod else ""


def niepuste(w, pole):
    return (w.get(pole) or "").strip() != ""


def wersja_z(tekst):
    """-> (schemat, krotka) albo None. Schemat 'rok' albo 'numer' — porównujemy tylko w obrębie schematu."""
    m = RE_WERSJA.search(tekst or "")
    if not m:
        return None
    s = m.group(0)
    if "." in s:
        return ("numer", tuple(int(x) for x in s.split(".")))
    return ("rok", (int(s),))


def rodzina(tekst):
    """Nazwa programu bez numerów wersji i znaków ®/™, małymi literami."""
    t = RE_WERSJA.sub(" ", tekst or "")
    t = re.sub(r"[®™]|\(r\)|\(tm\)", " ", t, flags=re.I)
    t = re.sub(r"[\s,;:()\-–_/]+", " ", t).strip().casefold()
    return t


def wersja_glowna(appver):
    """wersja_aplikacji Office, np. '16.0000' -> ('numer', (16,)); tylko część główna jest porównywalna."""
    m = re.match(r"\s*(\d+)", appver or "")
    return ("numer", (int(m.group(1)),)) if m else None


def data_dokumentu(w):
    """Data do kolejności wersji: modyfikacja, w razie braku utworzenie (UTC, gdy znana strefa)."""
    for pole in ("data_modyfikacji", "data_utworzenia"):
        for wariant in ("_utc", "_lokalna"):
            v = (w.get(pole + wariant) or "").rstrip("Z")
            if v:
                try:
                    return dt.datetime.fromisoformat(v)
                except ValueError:
                    pass
    return None


def osoba_dokumentu(w):
    """P3: ostatnio_zapisal, w razie braku autor; tylko typy osoba/login/email."""
    for pole in ("ostatnio_zapisal", "autor"):
        if niepuste(w, pole):
            return w[pole].strip() if w.get(pole + "_typ") in OSOBA_TYPY else None
    return None


def klasyfikuj_pare(punkty, prog_dni):
    """punkty: [(data, (schemat, krotka))] -> wynik P3 dla pary pseudonim x rodzina."""
    if len(punkty) < 2:
        return "za_malo_danych"
    punkty = sorted(punkty, key=lambda p: p[0])
    wersje = [p[1] for p in punkty]
    if len({w[0] for w in wersje}) > 1:                       # różne schematy (rok vs numer) -> nieporównywalne
        return "niejednoznaczne"
    if len(set(wersje)) == 1:
        okres = (punkty[-1][0] - punkty[0][0]).days
        return "bez_zmiany" if okres >= prog_dni else "za_malo_danych"
    rosnaco = all(a[1] <= b[1] for a, b in zip(wersje, wersje[1:]))
    return "zmiana_na_nowsza" if rosnaco else "niejednoznaczne"


def uszkodzone_z_long(sciezka):
    wynik = set()
    with open(sciezka, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if r.get("grupa") == "ExifTool" and r.get("tag") in ("Error", "Warning") \
                    and RE_USZKODZONY.search(r.get("wartosc", "")):
                wynik.add(r["plik"])
    return wynik


def flaga_nieustalone(w):
    return int(any(w.get(p + "_typ") == "nieustalone" for p in POLA_OSOBOWE if niepuste(w, p)))


def poziom(w, uszkodzone):
    if w["plik"] in uszkodzone:
        return "nieustalone", 0
    zaslepka = int(any(w.get(p + "_typ") == "zaslepka" for p in POLA_OSOBOWE))
    if any(w.get(p + "_typ") in PELNE_TYPY for p in POLA_OSOBOWE if niepuste(w, p)) or niepuste(w, "firma"):
        return "pelne", zaslepka
    oprog = any(niepuste(w, p) for p in POLA_OPROG)
    czas = any(niepuste(w, p + "_lokalna") or niepuste(w, p + "_surowa") for p in POLA_CZASU_DATY) \
        or any(niepuste(w, p) for p in POLA_CZASU_INNE)
    urzadzenie = any(niepuste(w, p) for p in POLA_URZADZEN)
    if oprog or czas or urzadzenie or zaslepka:
        return "czesciowe", zaslepka
    return "minimalne", 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dokumenty", required=True, help="dokumenty.csv z normalize.py")
    ap.add_argument("--long", help="wyniki_long.csv z extract.py (wykrycie uszkodzonych PDF)")
    ap.add_argument("--uszkodzone", help="opcjonalnie: plik z listą kluczy plików uszkodzonych (jeden w wierszu)")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--prog-dni", type=int, default=180, help="próg okresu dla „bez_zmiany” (domyślnie 180)")
    a = ap.parse_args()

    with open(a.dokumenty, encoding="utf-8-sig", newline="") as f:
        dok = list(csv.DictReader(f))
    uszkodzone = set()
    if a.long:
        uszkodzone |= uszkodzone_z_long(a.long)
    if a.uszkodzone:
        uszkodzone |= {l.strip() for l in Path(a.uszkodzone).read_text(encoding="utf-8").splitlines() if l.strip()}
    out = Path(a.out_dir); out.mkdir(parents=True, exist_ok=True)

    # ---------- RQ3: poziom zawartości metadanych ----------
    wiersze3 = []
    for w in dok:
        p, z = poziom(w, uszkodzone)
        wiersze3.append({"plik": w["plik"], "instytucja": w["instytucja"], "sektor": sektor(w["instytucja"]),
                         "format": w.get("format", ""), "poziom": p, "flaga_zaslepka": z,
                         "flaga_nieustalone": flaga_nieustalone(w)})

    # ---------- RQ2: profil instytucji ----------
    inst = collections.OrderedDict()
    for w in dok:
        inst.setdefault(w["instytucja"], []).append(w)
    znaczniki_wszystkie = sorted({z for w in dok for z in (w.get("znaczniki") or "").split(";") if z})

    profil, pary = [], []
    for kod, ws in inst.items():
        r = {"instytucja": kod, "sektor": sektor(kod), "dokumentow": len(ws)}
        # P1
        pseud = collections.Counter(); typy = {}
        for w in ws:
            w_dok = set()                                    # pseudonim liczony raz na dokument
            for pole in ("autor", "ostatnio_zapisal"):
                t = w.get(pole + "_typ")
                if niepuste(w, pole) and t in OSOBA_TYPY:
                    w_dok.add(w[pole].strip()); typy[w[pole].strip()] = t
            pseud.update(w_dok)
        domeny = {v.split("@", 1)[1].casefold() for v in pseud if "@" in v}
        r.update({"P1_pseudonimy": len(pseud),
                  **{f"P1_{t}": sum(1 for x in typy.values() if x == t) for t in sorted(OSOBA_TYPY)},
                  "P1_maks_dok_na_pseudonim": max(pseud.values(), default=0), "P1_domeny_email": len(domeny)})
        # P2 + P3a
        wersje_rodzin = collections.defaultdict(set)
        for w in ws:
            for pole in POLA_PROGRAMOW:
                if niepuste(w, pole):
                    rd = rodzina(w[pole]); v = wersja_z(w[pole])
                    if not v and pole == "program_tworzacy":     # Office: wersja tylko w wersja_aplikacji
                        v = wersja_glowna(w.get("wersja_aplikacji"))
                    if rd:
                        wersje_rodzin[rd]
                        if v:
                            wersje_rodzin[rd].add(v)
        r.update({"P2_rodziny_programow": len(wersje_rodzin),
                  "P2_programy_z_wersja": sum(len(v) for v in wersje_rodzin.values()),
                  "P3a_rodziny_z_wieloma_wersjami": sum(1 for v in wersje_rodzin.values() if len(v) >= 2),
                  "P3a_maks_wersji_w_rodzinie": max((len(v) for v in wersje_rodzin.values()), default=0)})
        # P3b
        punkty = collections.defaultdict(list)
        for w in ws:
            os_, d, prog = osoba_dokumentu(w), data_dokumentu(w), (w.get("program_tworzacy") or "").strip()
            if not (os_ and d and prog):
                continue
            v = wersja_z(prog) or wersja_glowna(w.get("wersja_aplikacji"))
            if v:
                punkty[(os_, rodzina(prog))].append((d, v))
        licz = collections.Counter()
        for (os_, rd), pk in punkty.items():
            wynik = klasyfikuj_pare(pk, a.prog_dni)
            licz[wynik] += 1
            okres = (max(p[0] for p in pk) - min(p[0] for p in pk)).days
            pary.append({"instytucja": kod, "sektor": sektor(kod), "pseudonim": os_, "rodzina": rd,
                         "dokumentow": len(pk), "wersji": len({p[1] for p in pk}), "okres_dni": okres,
                         "wynik": wynik})
        r.update({f"P3b_{k}": licz[k] for k in WYNIKI_P3})
        # P4
        zn = collections.Counter(z for w in ws for z in (w.get("znaczniki") or "").split(";") if z)
        r.update({"P4_rodzaje_znacznikow": len(zn), **{f"P4_{z}": zn[z] for z in znaczniki_wszystkie},
                  "P4_system_dok": sum(1 for w in ws if niepuste(w, "system"))})
        # P5
        sprzet = {(w.get("urzadzenie_producent", "").strip().casefold(), w.get("urzadzenie_model", "").strip().casefold())
                  for w in ws if niepuste(w, "urzadzenie_producent") or niepuste(w, "urzadzenie_model")}
        r.update({"P5_urzadzenia": len(sprzet),
                  "P5_numery_seryjne": len({w["numer_seryjny"].strip() for w in ws if niepuste(w, "numer_seryjny")})})
        # P6
        for pole, nazwa in (("id_oryginalu", "P6_wspolne_id_oryginalu"), ("szablon", "P6_wspolne_szablony")):
            c = collections.Counter(w[pole].strip() for w in ws if niepuste(w, pole))
            r[nazwa] = sum(1 for n in c.values() if n >= 2)
            r[nazwa + "_dok"] = sum(n for n in c.values() if n >= 2)
        profil.append(r)

    # ---------- zapis ----------
    def zapisz(nazwa, wiersze):
        if not wiersze:
            return
        kol = list(dict.fromkeys(k for w in wiersze for k in w))
        with open(out / nazwa, "w", encoding="utf-8-sig", newline="") as f:
            wr = csv.DictWriter(f, fieldnames=kol, restval=0); wr.writeheader(); wr.writerows(wiersze)

    zapisz("rq2_profil.csv", profil)
    zapisz("rq2_aktualizacje.csv", pary)
    zapisz("rq3_poziomy.csv", wiersze3)

    # ---------- konsola: tylko liczby ----------
    print(f"[+] {len(dok)} dokumentów, {len(inst)} instytucji -> {out}", file=sys.stderr)
    print(f"[i] uszkodzone (nieustalone): {len(uszkodzone & {w['plik'] for w in dok})}", file=sys.stderr)
    print("[i] RQ3 poziomy wg sektora:", file=sys.stderr)
    for s in sorted({x["sektor"] for x in wiersze3}):
        c = collections.Counter(x["poziom"] for x in wiersze3 if x["sektor"] == s)
        n = sum(c.values())
        print(f"    {s}: " + ", ".join(f"{p} {c[p]} ({100*c[p]/n:.1f}%)" for p in POZIOMY)
              + f"; zaślepka {sum(x['flaga_zaslepka'] for x in wiersze3 if x['sektor'] == s)}", file=sys.stderr)
    c = collections.Counter(p["wynik"] for p in pary)
    print(f"[i] RQ2 P3b pary pseudonim x rodzina (próg {a.prog_dni} dni): "
          + ", ".join(f"{k} {c[k]}" for k in WYNIKI_P3), file=sys.stderr)


if __name__ == "__main__":
    main()
