#!/usr/bin/env python3
"""
pola.py — ZESTAWIENIE PÓL do budowy taksonomii: dla każdego pola kanonicznego z dokumenty.csv (normalize.py)
liczy, w ilu dokumentach jest wypełnione (ogółem, w sektorach, w rodzajach formatu), ile ma różnych wartości,
z jakich tagów pochodzi i — dla pól osobowych — jakie ma typy wartości. Na koniec znaczniki systemów i usług.

Wynik zawiera WYŁĄCZNIE liczby, nazwy pól i nazwy tagów — żadnych wartości — więc można go przekazać dalej.

Przykład:
  python3 tools/pola.py --dokumenty dane/proba/dokumenty.csv --out dane/proba/zestawienie_pol.csv
"""
import argparse, collections, csv, sys
from pathlib import Path

import normalize          # jedna lista pól i grup: MAPA, POLA_OSOBOWE (bez kopiowania definicji)

# Grupy pól jak w komentarzach mapy (dziennik 2026-10-22) — tylko do czytelności zestawienia.
GRUPY = {
    "osoby i organizacje": ["autor", "ostatnio_zapisal", "prawa_autorskie", "wlasciciel_urzadzenia", "firma"],
    "oprogramowanie i system": ["program_tworzacy", "program_pdf", "wersja_aplikacji", "program_edycji_obrazu",
                                "biblioteka_xmp", "system", "szablon", "wersja_pdf"],
    "urządzenia": ["urzadzenie_producent", "urzadzenie_model", "numer_seryjny", "numer_seryjny_wewnetrzny",
                   "obiektyw", "firmware"],
    "daty": ["data_utworzenia", "data_modyfikacji", "data_metadanych", "data_wydruku"],
    "aktywność": ["czas_edycji_min", "liczba_rewizji"],
    "treść w metadanych": ["tytul", "temat", "slowa_kluczowe", "komentarz", "linki", "jezyk"],
    "pochodzenie i cechy pliku": ["id_dokumentu", "id_oryginalu", "id_poprzednika", "historia_programy",
                                  "historia_akcje", "historia_daty", "historia_konwersje", "nazwa_pliku_zrodlowego",
                                  "pdfa_czesc", "pdfa_zgodnosc", "pdf_tagowany", "szyfrowanie", "tresc_ai"],
    "lokalizacja": ["gps_szer", "gps_dl", "gps_data"],
}
SEKTORY = ("GMINA", "UCZELNIA", "SPOLKA")
OFFICE = {"DOC", "DOCX", "DOCM", "DOTX", "DOT", "XLS", "XLSX", "XLSM", "PPT", "PPTX", "PPTM", "PPS", "PPSX",
          "ODT", "ODS", "ODP", "RTF"}
OBRAZY = {"PNG", "JPEG", "TIFF"}


def rodzaj_formatu(fmt: str) -> str:
    return "pdf" if fmt == "PDF" else "office" if fmt in OFFICE else "obraz" if fmt in OBRAZY else "inny"


def wartosc(r: dict, pole: str, rodzaj: str) -> str:
    """Wartość pola w wierszu dokumenty.csv (dla dat: czas lokalny albo zapis nieczytelny)."""
    if rodzaj == "data":
        return r.get(f"{pole}_lokalna", "") or r.get(f"{pole}_surowa", "")
    return r.get(pole, "")


def procent(n, d):
    return f"{100 * n / d:.1f}" if d else ""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dokumenty", required=True, help="dokumenty.csv z normalize.py")
    ap.add_argument("--out", required=True, help="zestawienie_pol.csv (same liczby, bez wartości)")
    a = ap.parse_args()

    with open(a.dokumenty, encoding="utf-8-sig", newline="") as f:
        dok = list(csv.DictReader(f))
    if not dok:
        sys.exit("[x] pusty dokumenty.csv")
    sektor = lambda r: r["instytucja"].split("-")[0]
    mian_s = collections.Counter(sektor(r) for r in dok)
    mian_f = collections.Counter(rodzaj_formatu(r.get("format", "")) for r in dok)
    grupa_pola = {p: g for g, pola in GRUPY.items() for p in pola}

    wiersze = []
    for pole, rodzaj, _ in normalize.MAPA:
        if pole == "format":
            continue
        z = [r for r in dok if wartosc(r, pole, rodzaj)]
        w = {"grupa": grupa_pola.get(pole, "inne"), "pole": pole, "n_dok": len(z), "proc_ogolem": procent(len(z), len(dok))}
        for s in SEKTORY:
            w[f"proc_{s}"] = procent(sum(1 for r in z if sektor(r) == s), mian_s[s])
        for fr in ("pdf", "office", "obraz"):
            w[f"proc_{fr}"] = procent(sum(1 for r in z if rodzaj_formatu(r.get("format", "")) == fr), mian_f[fr])
        w["n_instytucji"] = len({r["instytucja"] for r in z})
        # liczba RÓŻNYCH wartości (bez ich wypisywania): ogółem i najwięcej w jednej instytucji
        w["n_roznych"] = len({wartosc(r, pole, rodzaj) for r in z})
        na_inst = collections.defaultdict(set)
        for r in z:
            na_inst[r["instytucja"]].add(wartosc(r, pole, rodzaj))
        w["max_roznych_w_instytucji"] = max((len(v) for v in na_inst.values()), default=0)
        zr = collections.Counter(r.get(f"{pole}_zrodlo", "") for r in z)
        w["zrodla"] = "; ".join(f"{k} {v}" for k, v in zr.most_common())
        if pole in normalize.POLA_OSOBOWE:
            ty = collections.Counter(r.get(f"{pole}_typ", "") or "(brak typu)" for r in z)
            w["typy"] = "; ".join(f"{k} {v}" for k, v in ty.most_common())
        w["n_rozbieznych"] = sum(1 for r in z if r.get(f"{pole}_rozbiezne") == "1")
        wiersze.append(w)

    kol = ["grupa", "pole", "n_dok", "proc_ogolem", *[f"proc_{s}" for s in SEKTORY], "proc_pdf", "proc_office",
           "proc_obraz", "n_instytucji", "n_roznych", "max_roznych_w_instytucji", "n_rozbieznych", "typy", "zrodla"]
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    with open(a.out, "w", encoding="utf-8-sig", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=kol, restval=""); wr.writeheader(); wr.writerows(wiersze)

    # podsumowanie na konsolę (do przekazania)
    print(f"### ZESTAWIENIE PÓL (bez wartości): {len(dok)} dokumentów, mapa v{dok[0].get('wersja_mapy', '?')}")
    print("### mianowniki: " + ", ".join(f"{s} {mian_s[s]}" for s in SEKTORY)
          + " | " + ", ".join(f"{k} {mian_f[k]}" for k in ("pdf", "office", "obraz")))
    print(f"{'pole':26s} {'n':>5} {'%':>5} {'%GM':>5} {'%UCZ':>5} {'%SP':>5} {'%pdf':>5} {'%off':>5} {'%obr':>5} "
          f"{'inst':>4} {'różn':>5} {'maxI':>4}")
    ostatnia = None
    for w in sorted(wiersze, key=lambda x: list(GRUPY).index(x["grupa"]) if x["grupa"] in GRUPY else 99):
        if w["grupa"] != ostatnia:
            print(f"-- {w['grupa']}"); ostatnia = w["grupa"]
        print(f"{w['pole']:26s} {w['n_dok']:5d} {w['proc_ogolem']:>5} {w['proc_GMINA']:>5} {w['proc_UCZELNIA']:>5} "
              f"{w['proc_SPOLKA']:>5} {w['proc_pdf']:>5} {w['proc_office']:>5} {w['proc_obraz']:>5} "
              f"{w['n_instytucji']:4d} {w['n_roznych']:5d} {w['max_roznych_w_instytucji']:4d}")
    print("-- typy wartości pól osobowych")
    for w in wiersze:
        if w.get("typy"):
            print(f"{w['pole']:26s} {w['typy']}")
    zn = collections.Counter(x for r in dok for x in r.get("znaczniki", "").split(";") if x)
    zn_inst = {k: len({r["instytucja"] for r in dok if k in r.get("znaczniki", "").split(";")}) for k in zn}
    print("-- znaczniki (dokumenty / instytucje): "
          + (", ".join(f"{k} {v}/{zn_inst[k]}" for k, v in zn.most_common()) or "brak"))
    print(f"[+] pełne zestawienie (z tagami źródłowymi) -> {a.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
