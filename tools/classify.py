#!/usr/bin/env python3
"""
classify.py — etap KLASYFIKACJI: dokumenty.csv (normalize.py) -> znaleziska.csv.
Każde znalezisko = (dokument, reguła): kategoria, wrażliwość R0–R3, ID reguły, pole kanoniczne, tag źródłowy.
Wartości NIE są zapisywane (są w dokumenty.csv); znaleziska.csv zawiera tylko klasyfikację.

Reguły: taksonomia v1 (dziennik 2026-10-28), skala wrażliwości wg adaptacji Skrauča 2026:
R0 informacja bez znaczenia dla poufności, R1 niska, R2 umiarkowana, R3 wysoka.

Rodzaje warunków:
  niepuste          pole ma wartość
  typ=<t>           typ wartości pola osobowego (<pole>_typ z normalize.py)
  wersja / bez_wersji   wartość pasuje / nie pasuje do WERSJA_RE (w obu przypadkach: nie jest nazwą urządzenia)
  urzadzenie        wartość pola programu jest nazwą urządzenia (URZADZENIE_RE; krok 7 pkt 1)
  wzorzec=<regex>   wartość pasuje do wyrażenia; bez_wzorca=<regex> — nie pasuje
  znacznik=<nazwa>  kolumna znaczniki
  sciezka           tag z flagą ścieżki (tagi_ze_sciezka, liczona w extract.py przed pseudonimizacją)

Przykład:
  python3 tools/classify.py --dokumenty dane/proba/dokumenty.csv --out dane/proba/znaleziska.csv
"""
import argparse, collections, csv, re, sys
from pathlib import Path

WERSJA_REGUL = "1.1"   # 1.1 (2026-10-28): poprawka HOST_RE (fałszywe trafienia WZR-03 na wersjach)
# Zmiany kroku 7 (wersja 1.2 nadawana przy zamrożeniu):
#   pkt 1: nazwy urządzeń (skanery, drukarki, urządzenia wielofunkcyjne) w polach programu -> URZ-02, nie OPR
#   pkt 2: wersje bez kropki: oznaczenie Creative Suite (CS6), wydanie Chromium/Skia (m74), liczba po nazwie (FineReader 14)

# Jawna wersja oprogramowania: numer z kropką albo rok wydania (decyzja 2026-10-28, np. „Word 2016”);
# krok 7 pkt 2: także „CS6”, „Skia/PDF m74” i liczba 1–2-cyfrowa jako osobne słowo po nazwie („FineReader 14”,
# „PDFMaker 11 dla programu Word”). Trzycyfrowe liczby (np. „365”) nie są wersją. report.py (P2/P3) używa
# tego samego zakresu wersji (RE_WERSJA tam).
WERSJA_RE = (r"\d+\.\d+|\b(?:19|20)\d{2}\b|\bCS\d{1,2}\b|\bSkia/PDF m\d+\b"
             r"|(?<=[^\W\d_]) \d{1,2}(?=$|[\s()|])")
# Nazwa pliku roboczego w tytule/temacie (np. „Microsoft Word - pismo_v3.docx”), decyzja 2026-10-28.
NAZWA_PLIKU_RE = r"^Microsoft (?:Word|Excel|PowerPoint) - |\.(?:docx?|xlsx?|pptx?|odt|rtf|pdf)\b"
# Nazwa urządzenia w polu programu (krok 7 pkt 1): skanery i urządzenia wielofunkcyjne zapisują w PDF:Producer /
# PDF:Creator własną nazwę i model. Linie produktów i modele, nie oprogramowanie producenta (np. „Canon MF Toolbox”,
# „Epson Scan”, „ScanSnap Manager” zostają programami).
URZADZENIE_RE = (
    r"(?i)\b(?:imageRUNNER|iR-ADV|i-SENSYS|imageCLASS|imageFORMULA)\b"
    r"|\bCanon\s+(?:MF\d|LBP\d|DR-|iR[\s-]?C?\d|PIXMA|MAXIFY)"
    r"|\b(?:WorkCentre|WorkCenter|AltaLink|VersaLink|DocuCentre|DocuPrint|ApeosPort|Apeos|PrimeLink)\b"
    r"|\bXerox\s+[BCD]\d{2,4}\b"
    r"|\bbizhub\b|\bKONICA\s*MINOLTA\b|\bineo\b|\bKM_[A-Z]?\d{3,4}[a-z]{0,2}\b"     # KM_C454e: skrót Konica Minolta
    r"|\bRICOH\s+(?:MP|IM|SP|Aficio|Pro)\b|\bAficio\b|\bnashuatec\b"
    r"|\bSHARP\s+(?:MX|AR|BP)-|\b(?:MX|BP)-[A-Z]?\d{3,4}"
    r"|\bTASKalfa\b|\bECOSYS\b|\bKYOCERA\b|\bUTAX\b|\bTriumph-Adler\b"
    r"|\bHP\s+(?:Color\s+)?(?:LaserJet|ScanJet|OfficeJet|PageWide|DeskJet|Digital\s+Send(?:er|ing))"
    r"|\bBrother\s+(?:MFC|DCP|ADS|HL)-?\w|\b(?:MFC|DCP|ADS)-[A-Z]?\d{3,4}"
    r"|\bEPSON\s+(?:WF-|ET-|L\d|DS-|WorkForce|EcoTank|Perfection)"
    r"|\bLexmark\b|\be-STUDIO"
    r"|\bSamsung\s+(?:SCX|CLX|SL|M\d)|\b(?:SCX|CLX)-\d"
    r"|\bOKI\s?(?:MC|MB|ES)\d|\bKX-MB\d|\bfi-\d{4}[A-Z]?\b|\bAvision\b|\bPlustek\b"
    r"|\bMFP\b|\bcopier\b|\bmultifunction\b")
EMAIL_PSEUD_RE = r"\bE-[0-9a-f]{10}@"                        # e-mail po pseudonimizacji w extract.py
# 2026-10-28 poprawka: IP wymaga 4 oktetów i nie może być częścią dłuższego ciągu (wcześniej „10.1.16” z wersji
# programu dawało fałszywe R3 — 22 trafienia w pełnej próbie)
HOST_RE = (r"(?i)\b[\w-]+\.(?:local|lan|corp|intranet|internal)\b"
           r"|(?<![\d.])(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}(?![\d.]|\.\d)")

POLA_OSOBOWE = ("autor", "ostatnio_zapisal", "prawa_autorskie", "wlasciciel_urzadzenia")
POLA_PROGRAMOW = ("program_tworzacy", "program_pdf", "program_edycji_obrazu", "historia_programy")
POLA_DAT = ("data_utworzenia", "data_modyfikacji", "data_metadanych", "data_wydruku")
# pola tekstowe przeszukiwane wzorcami WZR (bez pól osobowych — tam e-mail ma typ „email”)
POLA_WZR = ("firma", "program_tworzacy", "program_pdf", "program_edycji_obrazu", "szablon", "tytul", "temat",
            "slowa_kluczowe", "komentarz", "linki", "historia_programy", "historia_konwersje", "nazwa_pliku_zrodlowego")

K_OSO, K_OPR, K_URZ, K_CZA = "osoby i organizacje", "oprogramowanie i systemy", "urządzenia", "czas i aktywność"
K_TRS, K_POC, K_FMT = "treść w metadanych", "pochodzenie i identyfikatory dokumentu", "cechy formatu"
K_INF, K_LOK = "infrastruktura", "lokalizacja"

# (id, pola, warunek, kategoria, wrażliwość). Reguły OSB-01–06 obejmują wszystkie pola osobowe (decyzja 2026-10-28).
REGULY = [
    ("OSB-01", POLA_OSOBOWE, "typ=osoba", K_OSO, "R2"),
    ("OSB-02", POLA_OSOBOWE, "typ=login", K_OSO, "R3"),
    ("OSB-03", POLA_OSOBOWE, "typ=email", K_OSO, "R2"),
    ("OSB-04", POLA_OSOBOWE, "typ=organizacja", K_OSO, "R1"),
    ("OSB-05", POLA_OSOBOWE, "typ=nieustalone", K_OSO, "R1"),
    ("OSB-06", POLA_OSOBOWE, "typ=zaslepka", K_OSO, "R0"),
    ("OSB-15", ("firma",), "niepuste", K_OSO, "R1"),
    ("OPR-01", ("program_tworzacy",), "wersja", K_OPR, "R2"),
    ("OPR-09", ("program_tworzacy",), "bez_wersji", K_OPR, "R1"),
    ("OPR-02", ("program_pdf",), "wersja", K_OPR, "R2"),
    ("OPR-10", ("program_pdf",), "bez_wersji", K_OPR, "R1"),
    ("OPR-03", ("wersja_aplikacji",), "niepuste", K_OPR, "R2"),
    ("OPR-04", ("program_edycji_obrazu",), "wersja", K_OPR, "R2"),
    ("OPR-11", ("program_edycji_obrazu",), "bez_wersji", K_OPR, "R1"),
    ("OPR-05", ("biblioteka_xmp",), "niepuste", K_OPR, "R2"),
    ("OPR-06", ("system",), "niepuste", K_OPR, "R1"),
    ("OPR-07", ("szablon",), "niepuste", K_OPR, "R0"),
    ("OPR-08", ("wersja_pdf",), "niepuste", K_FMT, "R0"),
    ("POC-04", ("historia_programy",), "wersja", K_OPR, "R2"),
    ("POC-14", ("historia_programy",), "bez_wersji", K_OPR, "R1"),
    ("URZ-01", ("urzadzenie_producent",), "niepuste", K_URZ, "R1"),
    ("URZ-02", ("urzadzenie_model",), "niepuste", K_URZ, "R1"),
    ("URZ-02", POLA_PROGRAMOW, "urzadzenie", K_URZ, "R1"),     # pkt 1: model urządzenia zapisany jako program
    ("URZ-03", ("numer_seryjny",), "niepuste", K_URZ, "R3"),
    ("URZ-04", ("numer_seryjny_wewnetrzny",), "niepuste", K_URZ, "R3"),
    ("URZ-05", ("obiektyw",), "niepuste", K_URZ, "R1"),
    ("URZ-06", ("firmware",), "niepuste", K_URZ, "R2"),
    ("DAT-01", ("data_utworzenia",), "niepuste", K_CZA, "R1"),
    ("DAT-02", ("data_modyfikacji",), "niepuste", K_CZA, "R1"),
    ("DAT-03", ("data_metadanych",), "niepuste", K_CZA, "R1"),
    ("DAT-04", ("data_wydruku",), "niepuste", K_CZA, "R1"),
    ("AKT-01", ("czas_edycji_min",), "niepuste", K_CZA, "R1"),
    ("AKT-02", ("liczba_rewizji",), "niepuste", K_CZA, "R1"),
    ("TRS-07", ("tytul", "temat"), f"wzorzec={NAZWA_PLIKU_RE}", K_TRS, "R1"),
    ("TRS-01", ("tytul",), f"bez_wzorca={NAZWA_PLIKU_RE}", K_TRS, "R0"),
    ("TRS-02", ("temat",), f"bez_wzorca={NAZWA_PLIKU_RE}", K_TRS, "R0"),
    ("TRS-03", ("slowa_kluczowe",), "niepuste", K_TRS, "R0"),
    ("TRS-04", ("komentarz",), "niepuste", K_TRS, "R1"),
    ("TRS-05", ("linki",), "niepuste", K_TRS, "R1"),
    ("TRS-06", ("jezyk",), "niepuste", K_FMT, "R0"),
    ("POC-01", ("id_dokumentu",), "niepuste", K_POC, "R1"),
    ("POC-02", ("id_oryginalu",), "niepuste", K_POC, "R1"),
    ("POC-03", ("id_poprzednika",), "niepuste", K_POC, "R1"),
    ("POC-05", ("historia_akcje",), "niepuste", K_CZA, "R1"),
    ("POC-06", ("historia_daty",), "niepuste", K_CZA, "R1"),
    ("POC-07", ("historia_konwersje",), "niepuste", K_CZA, "R1"),
    ("POC-08", ("nazwa_pliku_zrodlowego",), "niepuste", K_POC, "R1"),
    ("POC-09", ("pdfa_czesc",), "niepuste", K_FMT, "R0"),
    ("POC-10", ("pdfa_zgodnosc",), "niepuste", K_FMT, "R0"),
    ("POC-11", ("pdf_tagowany",), "niepuste", K_FMT, "R0"),
    ("POC-12", ("szyfrowanie",), "niepuste", K_FMT, "R0"),
    ("POC-13", ("tresc_ai",), "niepuste", K_FMT, "R1"),
    ("LOK-01", ("gps_szer",), "niepuste", K_LOK, "R3"),
    ("LOK-02", ("gps_dl",), "niepuste", K_LOK, "R3"),
    ("LOK-03", ("gps_data",), "niepuste", K_CZA, "R1"),
    ("ZNC-01", ("znaczniki",), "znacznik=system_dokumentow", K_OPR, "R1"),
    ("ZNC-02", ("znaczniki",), "znacznik=etykieta_msip", K_INF, "R2"),
    ("ZNC-03", ("znaczniki",), "znacznik=sharepoint", K_OPR, "R1"),
    ("ZNC-04", ("znaczniki",), "znacznik=grammarly", K_OPR, "R1"),
    ("ZNC-05", ("znaczniki",), "znacznik=xmp_attrib", K_POC, "R2"),
    ("ZNC-06", ("znaczniki",), "znacznik=c2pa", K_POC, "R1"),
    ("ZNC-07", ("znaczniki",), "znacznik=pdfx", K_FMT, "R0"),
    ("WZR-01", ("tagi_ze_sciezka",), "sciezka", K_INF, "R3"),
    ("WZR-02", POLA_WZR, f"wzorzec={EMAIL_PSEUD_RE}", K_OSO, "R2"),
    ("WZR-03", POLA_WZR, f"wzorzec={HOST_RE}", K_INF, "R3"),
]
RANGA = {"R0": 0, "R1": 1, "R2": 2, "R3": 3}


def czy_urzadzenie(v: str) -> bool:
    """Wartość pola programu jest nazwą urządzenia (krok 7 pkt 1); używane też w report.py."""
    return bool(re.search(URZADZENIE_RE, v or ""))


def wartosc(r: dict, pole: str) -> str:
    if pole in POLA_DAT:
        return r.get(f"{pole}_lokalna", "") or r.get(f"{pole}_surowa", "")
    return (r.get(pole) or "").strip()


def dopasuj(r: dict, pole: str, war: str):
    """-> lista tagów źródłowych, dla których reguła trafia (pusta = brak trafienia)."""
    if war == "sciezka":
        return [t for t in r.get("tagi_ze_sciezka", "").split(";") if t]
    if war.startswith("znacznik="):
        return [war.split("=", 1)[1]] if war.split("=", 1)[1] in r.get("znaczniki", "").split(";") else []
    v = wartosc(r, pole)
    if not v:
        return []
    zrodlo = [r.get(f"{pole}_zrodlo", "") or pole]
    if war == "niepuste":
        return zrodlo
    if war.startswith("typ="):
        typ = r.get(f"{pole}_typ", "") or "nieustalone"      # brak typu (starsze CSV) = nieustalone
        return zrodlo if typ == war[4:] else []
    if war == "urzadzenie":
        return zrodlo if czy_urzadzenie(v) else []
    if war == "wersja":
        return zrodlo if re.search(WERSJA_RE, v) and not czy_urzadzenie(v) else []
    if war == "bez_wersji":
        return [] if re.search(WERSJA_RE, v) or czy_urzadzenie(v) else zrodlo
    if war.startswith("wzorzec="):
        return zrodlo if re.search(war[8:], v, re.I) else []
    if war.startswith("bez_wzorca="):
        return [] if re.search(war[11:], v, re.I) else zrodlo
    raise ValueError(f"nieznany warunek: {war}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dokumenty", required=True, help="dokumenty.csv z normalize.py")
    ap.add_argument("--out", required=True, help="znaleziska.csv")
    a = ap.parse_args()

    with open(a.dokumenty, encoding="utf-8-sig", newline="") as f:
        dok = list(csv.DictReader(f))
    if dok and "tagi_ze_sciezka" not in dok[0]:
        print("[!] dokumenty.csv bez kolumny tagi_ze_sciezka (stary extract/normalize) — WZR-01 nie zadziała",
              file=sys.stderr)

    zn, traf = [], collections.Counter()
    for r in dok:
        for id_, pola, war, kat, wr in REGULY:
            for pole in pola:
                for zrodlo in dopasuj(r, pole, war):
                    zn.append({"plik": r["plik"], "instytucja": r["instytucja"],
                               "sektor": r["instytucja"].split("-")[0], "format": r.get("format", ""),
                               "id_reguly": id_, "kategoria": kat, "wrazliwosc": wr,
                               "pole": pole, "zrodlo": zrodlo, "wersja_regul": WERSJA_REGUL})
                    traf[id_] += 1

    kol = ["plik", "instytucja", "sektor", "format", "id_reguly", "kategoria", "wrazliwosc", "pole", "zrodlo",
           "wersja_regul"]
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    with open(a.out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=kol); w.writeheader(); w.writerows(zn)

    # ---- podsumowanie (same liczby) ----
    print(f"[+] {len(zn)} znalezisk w {len(dok)} dokumentach -> {a.out} (reguły v{WERSJA_REGUL})", file=sys.stderr)
    maks = {}
    for z in zn:
        maks[z["plik"]] = max(maks.get(z["plik"], "R0"), z["wrazliwosc"], key=RANGA.get)
    sektory = sorted({r["instytucja"].split("-")[0] for r in dok})
    print("### najwyższa wrażliwość dokumentu (odsetek dokumentów sektora; „brak” = żadnego znaleziska)")
    for s in sektory:
        ds = [r["plik"] for r in dok if r["instytucja"].startswith(s + "-")]
        c = collections.Counter(maks.get(p, "brak") for p in ds)
        print(f"   {s:9s} n={len(ds):4d}  " + "  ".join(f"{k} {100 * c[k] / len(ds):5.1f}%"
                                                        for k in ("brak", "R0", "R1", "R2", "R3")))
    print("### znaleziska R1–R3: kategoria × wrażliwość (liczba znalezisk)")
    kw = collections.Counter((z["kategoria"], z["wrazliwosc"]) for z in zn if z["wrazliwosc"] != "R0")
    for kat in sorted({k for k, _ in kw}):
        print(f"   {kat:40s} " + "  ".join(f"{r} {kw[(kat, r)]:5d}" for r in ("R1", "R2", "R3")))
    print("### trafienia reguł (0 = reguła sprawdzona, brak w próbie)")
    print("   " + ", ".join(f"{id_} {traf[id_]}" for id_ in dict.fromkeys(r[0] for r in REGULY)))


if __name__ == "__main__":
    main()
