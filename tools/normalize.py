#!/usr/bin/env python3
"""
normalize.py — etap NORMALIZACJI: surowe tagi exiftool (wyniki_long.csv) -> jeden wiersz na dokument
z polami kanonicznymi (dokumenty.csv). Mapa pól: MAPA poniżej (wersja WERSJA_MAPY, dziennik 2026-10-22).

Dla każdego pola kanonicznego:
  - wartość = pierwsza niepusta według priorytetu źródeł (zapis formatu przed XMP/IPTC),
  - <pole>_zrodlo = tag, z którego wzięto wartość,
  - <pole>_rozbiezne = 1, gdy inne źródło tego pola ma inną wartość (różnice samego zapisu się nie liczą).
Daty: <pole>_lokalna (czas zapisany w pliku), <pole>_utc (gdy znana strefa), <pole>_strefa (przesunięcie
albo "brak"). Wartości osobowe pozostają pseudonimami z extract.py; dla pól osobowych (POLA_OSOBOWE)
<pole>_typ = typ wartości z extract.py (osoba/login/email/organizacja/zaslepka/nieustalone) dla wybranego źródła.

Kontrola kompletności: każdy niepusty tag musi być zmapowany albo jawnie ignorowany (IGNOROWANE);
pozostałe są zgłaszane jako NIEZNANE (konsola + opcjonalnie --raport JSON). Kod wyjścia 3, gdy są nieznane.

Przykład:
  python3 tools/normalize.py --long dane/pilot/wyniki_long.csv --szeroki dane/pilot/wyniki.csv \\
                             --out dane/pilot/dokumenty.csv --raport dane/pilot/normalize_raport.json
"""
import argparse, collections, csv, datetime as dt, fnmatch, json, re, sys
from pathlib import Path

# 1.1 (2026-10-22): id_poprzednika i numer_seryjny_wewnetrzny jako osobne pola
# 1.2 (2026-10-27): XMP-pdf:Creator i PostScript:Creator -> program_tworzacy (wyjątek warunkowy w extract.py);
#                   kolumny <pole>_typ dla pól osobowych (typ wartości z extract.py)
WERSJA_MAPY = "1.2"

# Grupy notatek producenta (MakerNotes): z nich bierzemy tylko numery seryjne, właściciela i firmware.
MN = ["Canon", "CanonCustom", "Nikon", "NikonCustom", "FujiFilm", "Samsung", "Olympus", "Sony", "Panasonic",
      "Pentax", "Sigma", "Ricoh", "Kodak", "Casio", "Minolta", "Leica", "Apple", "GoPro", "DJI", "HP"]


def mn(tag):
    return [f"{g}:{tag}" for g in MN]


# ---------- MAPA pól kanonicznych v1 (zaakceptowana 2026-10-22) ----------
# (pole, rodzaj, [źródła w kolejności priorytetu]); źródło = "Grupa:Tag", dopuszczalne wzorce fnmatch.
# rodzaj: tekst | data | minuty | liczba
MAPA = [
    # osoby i organizacje (pseudonimy, poza firma)
    ("autor", "tekst", ["PDF:Author", "XMP-dc:Creator", "XML:Creator", "FlashPix:Author", "IFD0:Artist",
                        "IFD0:XPAuthor", "IPTC:By-line", "XML:EL_Author", "PostScript:For"]),
    ("ostatnio_zapisal", "tekst", ["XML:LastModifiedBy", "FlashPix:LastModifiedBy", "PDF:LastSavedBy"]),
    ("prawa_autorskie", "tekst", ["IFD0:Copyright", "IPTC:CopyrightNotice", "XMP-dc:Rights"]),
    ("wlasciciel_urzadzenia", "tekst", ["ExifIFD:OwnerName", "XMP-aux:OwnerName", *mn("OwnerName")]),
    ("firma", "tekst", ["XML:Company", "FlashPix:Company"]),
    # oprogramowanie i system
    ("program_tworzacy", "tekst", ["PDF:Creator", "XMP-xmp:CreatorTool", "XMP-pdf:Creator", "XML:Application",
                                   "FlashPix:Software", "IFD0:Software", "PNG:Software",
                                   "XMP-illustrator:CreatorSubTool", "PostScript:Creator"]),
    ("program_pdf", "tekst", ["PDF:Producer", "XMP-pdf:Producer"]),
    ("wersja_aplikacji", "tekst", ["XML:AppVersion", "FlashPix:AppVersion"]),
    ("program_edycji_obrazu", "tekst", ["IFD0:ProcessingSoftware"]),
    ("biblioteka_xmp", "tekst", ["XMP-x:XMPToolkit"]),
    ("system", "tekst", ["FlashPix:System", "XMP-GIMP:Platform"]),
    ("szablon", "tekst", ["XML:Template", "FlashPix:Template", "XML:ELDocType"]),
    ("wersja_pdf", "tekst", ["PDF:PDFVersion", "XMP-pdf:PDFVersion"]),
    # urządzenia
    ("urzadzenie_producent", "tekst", ["IFD0:Make"]),
    ("urzadzenie_model", "tekst", ["IFD0:Model"]),
    ("numer_seryjny", "tekst", ["ExifIFD:SerialNumber", "XMP-aux:SerialNumber", *mn("SerialNumber")]),
    ("numer_seryjny_wewnetrzny", "tekst", [*mn("InternalSerialNumber")]),
    ("obiektyw", "tekst", ["ExifIFD:LensModel", "XMP-aux:Lens", "XMP-exifEX:LensModel", "Composite:LensID"]),
    ("firmware", "tekst", ["XMP-aux:Firmware", *mn("FirmwareVersion")]),
    # daty
    ("data_utworzenia", "data", ["PDF:CreateDate", "XML:CreateDate", "MS-DOC:CreateDate", "FlashPix:CreateDate",
                                 "ExifIFD:DateTimeOriginal", "ExifIFD:CreateDate", "XMP-xmp:CreateDate",
                                 "XMP-photoshop:DateCreated", "Composite:DateTimeCreated", "PostScript:CreateDate"]),
    ("data_modyfikacji", "data", ["PDF:ModifyDate", "XML:ModifyDate", "MS-DOC:ModifyDate", "FlashPix:ModifyDate",
                                  "IFD0:ModifyDate", "XMP-xmp:ModifyDate"]),
    ("data_metadanych", "data", ["XMP-xmp:MetadataDate"]),
    ("data_wydruku", "data", ["XML:LastPrinted", "MS-DOC:LastPrinted", "FlashPix:LastPrinted", "PDF:DateLastPrinted"]),
    # aktywność
    ("czas_edycji_min", "minuty", ["XML:TotalEditTime", "MS-DOC:TotalEditTime", "FlashPix:TotalEditTime"]),
    ("liczba_rewizji", "liczba", ["XML:RevisionNumber", "MS-DOC:RevisionNumber", "FlashPix:RevisionNumber"]),
    # treść w metadanych (jawna)
    ("tytul", "tekst", ["PDF:Title", "XMP-dc:Title", "XMP-dc:Title-*", "FlashPix:Title", "PostScript:Title"]),
    ("temat", "tekst", ["PDF:Subject", "XMP-dc:Description", "IFD0:ImageDescription"]),
    ("slowa_kluczowe", "tekst", ["PDF:Keywords", "XML:Keywords", "XMP-pdf:Keywords"]),
    ("komentarz", "tekst", ["File:Comment", "IPTC:SpecialInstructions"]),
    ("linki", "tekst", ["XML:HLinks"]),
    ("jezyk", "tekst", ["PDF:Language", "XMP-dc:Language", "XML:Language", "FlashPix:LanguageCode"]),
    # pochodzenie, historia, cechy pliku
    ("format", "tekst", ["File:FileType"]),
    ("id_dokumentu", "tekst", ["XMP-xmpMM:DocumentID"]),
    ("id_oryginalu", "tekst", ["XMP-xmpMM:OriginalDocumentID"]),
    ("id_poprzednika", "tekst", ["XMP-xmpMM:DerivedFromDocumentID"]),
    ("historia_programy", "tekst", ["XMP-xmpMM:HistorySoftwareAgent"]),
    ("historia_akcje", "tekst", ["XMP-xmpMM:HistoryAction"]),
    ("historia_daty", "tekst", ["XMP-xmpMM:HistoryWhen"]),
    ("historia_konwersje", "tekst", ["XMP-xmpMM:HistoryParameters"]),
    ("nazwa_pliku_zrodlowego", "tekst", ["XMP-xmpMM:PreservedFileName", "XMP-crs:RawFileName"]),
    ("pdfa_czesc", "tekst", ["XMP-pdfaid:Part"]),
    ("pdfa_zgodnosc", "tekst", ["XMP-pdfaid:Conformance"]),
    ("pdf_tagowany", "tekst", ["PDF:TaggedPDF"]),
    ("szyfrowanie", "tekst", ["PDF:Encryption"]),
    ("tresc_ai", "tekst", ["PDF:ContainsAiGeneratedContent", "XMP-ContainsAiGeneratedContent:*"]),
    ("gps_szer", "tekst", ["GPS:GPSLatitude", "Composite:GPSLatitude", "XMP-exif:GPSLatitude"]),
    ("gps_dl", "tekst", ["GPS:GPSLongitude", "Composite:GPSLongitude", "XMP-exif:GPSLongitude"]),
    ("gps_data", "tekst", ["Composite:GPSDateTime"]),
]

# Pola osobowe: obok wartości (pseudonimu) zapisywany typ wartości z extract.py (decyzja A7, mapa 1.2).
POLA_OSOBOWE = ("autor", "ostatnio_zapisal", "prawa_autorskie", "wlasciciel_urzadzenia")
# Źródła programu objęte w extract.py wyjątkiem warunkowym: gdy reguła rozpoznała osobę, wartość jest
# pseudonimem i NIE może trafić do program_tworzacy (dziennik 2026-10-27).
ZRODLA_WARUNKOWE = {"PostScript:Creator", "XMP-pdf:Creator"}
PSEUDONIM_RE = re.compile(r"^P-[0-9a-f]{10}$")

# ---------- IGNOROWANE (jawna lista; wzorce fnmatch na "Grupa:Tag") ----------
IGNOROWANE = [
    # całe grupy techniczne
    "ICC*:*", "JFIF:*", "IFD1:*", "InteropIFD:*", "PreviewIFD:*", "PNG-pHYs:*", "Adobe:*", "PrintIM:*",
    "Photoshop:*", "XMP-xmpTPg:*", "XMP-crs:*", "XMP-rdf:*", "System:*", "ExifTool:*",
    # notatki producenta i Composite (zmapowane wyjątki mają pierwszeństwo przed ignorowaniem)
    *[f"{g}:*" for g in MN], "Composite:*",
    # File: tylko FileType i Comment są mapowane
    "File:*",
    # parametry ekspozycji i obrazu (EXIF/XMP-aux)
    "ExifIFD:ExifVersion", "ExifIFD:ColorSpace", "ExifIFD:ExposureTime", "ExifIFD:FNumber",
    "ExifIFD:ExposureCompensation", "ExifIFD:MeteringMode", "ExifIFD:Flash", "ExifIFD:FocalLength",
    "ExifIFD:ExposureProgram", "ExifIFD:ISO", "ExifIFD:ExposureMode", "ExifIFD:WhiteBalance",
    "ExifIFD:SceneCaptureType", "ExifIFD:FlashpixVersion", "ExifIFD:ExifImageWidth", "ExifIFD:ExifImageHeight",
    "ExifIFD:ComponentsConfiguration", "ExifIFD:ShutterSpeedValue", "ExifIFD:ApertureValue",
    "ExifIFD:MaxApertureValue", "ExifIFD:SubSecTime*", "ExifIFD:CustomRendered", "ExifIFD:SensitivityType",
    "ExifIFD:FocalPlane*", "ExifIFD:LightSource", "ExifIFD:SensingMethod", "ExifIFD:FileSource",
    "ExifIFD:Sharpness", "ExifIFD:DigitalZoomRatio", "ExifIFD:CompressedBitsPerPixel", "ExifIFD:SceneType",
    "ExifIFD:FocalLengthIn35mmFormat", "ExifIFD:BrightnessValue", "ExifIFD:SubjectDistanceRange",
    "ExifIFD:Contrast", "ExifIFD:Saturation", "ExifIFD:LensInfo", "ExifIFD:LensSerialNumber", "ExifIFD:Padding",
    "ExifIFD:GainControl", "ExifIFD:OffsetSchema", "ExifIFD:OffsetTime*", "ExifIFD:RecommendedExposureIndex",
    "ExifIFD:CFAPattern", "ExifIFD:MakerNoteUnknownText", "ExifIFD:RelatedSoundFile", "ExifIFD:ExposureIndex",
    "ExifIFD:LensMake", "ExifIFD:SubjectDistance", "ExifIFD:ImageUniqueID",
    "IFD0:XResolution", "IFD0:YResolution", "IFD0:ResolutionUnit", "IFD0:Orientation",
    "IFD0:YCbCrPositioning", "IFD0:ReferenceBlackWhite", "IFD0:ImageWidth", "IFD0:ImageHeight", "IFD0:Padding",
    "XMP-aux:LensInfo", "XMP-aux:LensSerialNumber", "XMP-aux:LensID", "XMP-aux:ImageNumber",
    "XMP-aux:ApproximateFocusDistance", "XMP-aux:FlashCompensation",
    "GPS:GPSVersionID", "GPS:GPSAltitudeRef", "GPS:GPSTimeStamp", "GPS:GPSDateStamp",
    "GPS:GPSLatitudeRef", "GPS:GPSLongitudeRef",
    "PNG:ImageWidth", "PNG:ImageHeight", "PNG:BitDepth", "PNG:ColorType", "PNG:Compression", "PNG:Filter",
    "PNG:Interlace", "PNG:SRGBRendering", "PNG:Gamma",
    # statystyki i ustawienia Office
    "XML:ScaleCrop", "XML:LinksUpToDate", "XML:SharedDoc", "XML:HyperlinksChanged", "XML:Words", "XML:Paragraphs",
    "XML:DocSecurity", "XML:Pages", "XML:Characters", "XML:Lines", "XML:HeadingPairs", "XML:CharactersWithSpaces",
    "XML:TitlesOfParts", "XML:PresentationFormat", "XML:Slides", "XML:Notes", "XML:HiddenSlides", "XML:MMClips",
    "XML:ContentTypeId", "XML:TemplateVersion", "XML:EurolookVersion", "XML:CreatedUsing", "XML:Formatting",
    "XML:LastEditedUsing", "XML:Type", "XML:EL_Language", "XML:Tag_DocHome",
    "MS-DOC:Words", "MS-DOC:Characters", "MS-DOC:Pages", "MS-DOC:Paragraphs", "MS-DOC:Lines",
    "FlashPix:Identification", "FlashPix:DocFlags", "FlashPix:Word97", "FlashPix:Pages", "FlashPix:Words",
    "FlashPix:Characters", "FlashPix:Security", "FlashPix:CodePage", "FlashPix:Lines", "FlashPix:Paragraphs",
    "FlashPix:CharCountWithSpaces", "FlashPix:ScaleCrop", "FlashPix:LinksUpToDate", "FlashPix:SharedDoc",
    "FlashPix:HyperlinksChanged", "FlashPix:HeadingPairs", "FlashPix:CompObjUserTypeLen", "FlashPix:CompObjUserType",
    "FlashPix:TitleOfParts",
    # układ i cechy techniczne PDF / XMP
    "PDF:Linearized", "PDF:PageCount", "PDF:PageLayout", "PDF:MediaBox", "PDF:HasXFA", "PDF:Trapped", "PDF:PageMode",
    "PDF:ContainerVersion", "PDF:CreatorVersion", "PDF:UserAccess",
    "XMP-dc:Format", "XMP-pdf:Trapped", "XMP-pdfuaid:Part", "XMP-pdfx:CreatorVersion", "XMP-xmp:Thumbnail*",
    "XMP-xmp:Rating", "XMP-xmpMM:InstanceID", "XMP-xmpMM:DerivedFromInstanceID", "XMP-xmpMM:HistoryInstanceID",
    "XMP-xmpMM:HistoryChanged", "XMP-xmpMM:RenditionClass", "XMP-xmpMM:DerivedFromRenditionClass",
    "XMP-xmpMM:DerivedFromOriginalDocumentID", "XMP-xmpMM:VersionID", "XMP-dc:Date",
    "XMP-illustrator:StartupProfile", "XMP-illustrator:IsFileSavedViaInstantSave",
    "XMP-GIMP:Api", "XMP-GIMP:TimeStamp", "XMP-GIMP:Version",
    "IPTC:CodedCharacterSet", "IPTC:ApplicationRecordVersion", "IPTC:EnvelopeRecordVersion",
    "IPTC:DateCreated", "IPTC:TimeCreated", "IPTC:DigitalCreationDate", "IPTC:DigitalCreationTime",
    "PostScript:AI*", "PostScript:BoundingBox",          # 1.2: PostScript:Creator przeniesiony do program_tworzacy
]

# Formaty z listy badania (decyzja 2026-10-18) wg File:FileType; inne (np. XML/XHTML z ZIP-ów sprzed filtra)
# są pomijane i liczone.
FORMATY = {"PDF", "DOC", "DOCX", "DOCM", "DOTX", "DOT", "XLS", "XLSX", "XLSM", "PPT", "PPTX", "PPTM", "PPS",
           "PPSX", "ODT", "ODS", "ODP", "RTF", "PNG", "JPEG", "TIFF"}

PUSTE = {"", "()", "[]", "-"}

# ---------- daty ----------
RE_DATA = re.compile(
    r"^(\d{4})[:\-](\d{2})[:\-](\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?)?\s*(Z|[+\-]\d{2}:?\d{2})?$")
RE_DATA_US = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([AP]M)$", re.I)


def parsuj_date(s):
    """-> (lokalna 'YYYY-MM-DDTHH:MM:SS', utc 'YYYY-MM-DDTHH:MM:SSZ' | '', strefa '+HH:MM' | 'brak') albo None."""
    s = s.strip()
    m = RE_DATA.match(s)
    if m:
        y, mo, d, h, mi, se, tz = m.groups()
        try:
            lok = dt.datetime(int(y), int(mo), int(d), int(h or 0), int(mi or 0), int(se or 0))
        except ValueError:
            return None
        if not tz:
            return lok.isoformat(), "", "brak"
        if tz == "Z":
            off = dt.timedelta(0); strefa = "+00:00"
        else:
            t = tz.replace(":", "")
            off = dt.timedelta(hours=int(t[1:3]), minutes=int(t[3:5])) * (1 if t[0] == "+" else -1)
            strefa = f"{t[0]}{t[1:3]}:{t[3:5]}"
        return lok.isoformat(), (lok - off).isoformat() + "Z", strefa
    m = RE_DATA_US.match(s)
    if m:
        mo, d, y, h, mi, se, ap = m.groups()
        h = int(h) % 12 + (12 if ap.upper() == "PM" else 0)
        try:
            lok = dt.datetime(int(y), int(mo), int(d), h, int(mi), int(se or 0))
        except ValueError:
            return None
        return lok.isoformat(), "", "brak"
    return None


RE_CZAS = re.compile(r"(\d+(?:\.\d+)?)\s*(day|hour|min(?:ute)?|sec(?:ond)?|s)s?\b", re.I)
JEDN = {"day": 1440, "hour": 60, "min": 1, "minute": 1, "sec": 1 / 60, "second": 1 / 60, "s": 1 / 60}


def parsuj_minuty(s):
    tr = RE_CZAS.findall(s)
    if not tr:
        return None
    return round(sum(float(n) * JEDN[j.lower()] for n, j in tr), 2)


# ---------- logika ----------

def pasuje(klucz, wzorce):
    return any(fnmatch.fnmatchcase(klucz, w) for w in wzorce)


def _nrm(x):
    return " ".join(x.casefold().split())


def takie_same(rodzaj, a, b):
    """Czy dwie wartości z różnych źródeł niosą to samo (różnice samego zapisu nie są rozbieżnością).
    Daty: porównanie z dokładnością do minuty (część formatów nie zapisuje sekund); gdy któraś data nie ma
    strefy, porównujemy czas zapisany w pliku (lokalny), w przeciwnym razie czas UTC; dwie daty bez strefy
    różniące się o pełne 1–14 h traktujemy jako tę samą (różny zapis strefy)."""
    if rodzaj == "data":
        pa, pb = parsuj_date(a), parsuj_date(b)
        if not pa or not pb:
            return a.strip() == b.strip()
        if pa[1] and pb[1]:
            return pa[1][:16] == pb[1][:16]
        if pa[0][:16] == pb[0][:16]:
            return True
        if not pa[1] and not pb[1]:
            # decyzja 2026-10-23: obie daty bez strefy, różnica o pełne 1–14 h przy tych samych minutach
            # = różny sposób zapisu strefy (np. MS-DOC i FlashPix w plikach DOC), nie rozbieżność
            ra = dt.datetime.fromisoformat(pa[0]).replace(second=0)
            rb = dt.datetime.fromisoformat(pb[0]).replace(second=0)
            godz = abs((ra - rb).total_seconds()) / 3600
            return godz == int(godz) and 1 <= godz <= 14
        return False
    if rodzaj == "minuty":
        return parsuj_minuty(a) == parsuj_minuty(b)
    # teksty: bez wielkości liter i nadmiarowych spacji; listy porównywane jako zbiory elementów
    # (PDF zapisuje listy z " | ", XMP z przecinkiem — to różnica zapisu, nie treści)
    def elementy(x):
        return sorted(" ".join(e.casefold().split()) for e in re.split(r"\s*[|,;]\s*", x) if e.strip())
    if elementy(a) == elementy(b):
        return True
    # decyzja 2026-10-23: wartość zawarta w drugiej (np. "GIMP 2.10" w "GIMP 2.10.38") = ta sama informacja
    # podana mniej szczegółowo, nie rozbieżność
    na, nb = _nrm(a), _nrm(b)
    return na in nb or nb in na


def rozbiezne(rodzaj, wartosci):
    return any(not takie_same(rodzaj, wartosci[0], w) for w in wartosci[1:])


def normalizuj_plik(tagi, typy=None):
    """tagi: {'Grupa:Tag': wartość}; typy: {'Grupa:Tag': typ wartości z extract.py}.
    -> (wiersz z polami kanonicznymi, set zmapowanych kluczy)."""
    typy = typy or {}
    wiersz, uzyte = {}, set()
    for pole, rodzaj, zrodla in MAPA:
        kand = []                                  # [(klucz, wartość)] w kolejności priorytetu
        for z in zrodla:
            for k in sorted(k for k in tagi if fnmatch.fnmatchcase(k, z)):
                if k not in (x[0] for x in kand):
                    kand.append((k, tagi[k]))
        uzyte.update(k for k, _ in kand)
        kand = [(k, v) for k, v in kand if v.strip() not in PUSTE]
        if pole == "program_tworzacy":
            # wyjątek warunkowy (extract.py): pseudonim = rozpoznana osoba, nie program
            kand = [(k, v) for k, v in kand if not (k in ZRODLA_WARUNKOWE and PSEUDONIM_RE.match(v.strip()))]
        wybor = kand[0] if kand else None
        if wybor and rodzaj == "tekst":
            # decyzja 2026-10-23: gdy wartość z priorytetu zawiera się w innej, bierzemy pełniejszą (dłuższą);
            # przy równej długości zostaje kolejność priorytetu
            pelniejsze = [c for c in kand if _nrm(wybor[1]) in _nrm(c[1])]
            wybor = max(pelniejsze, key=lambda c: len(c[1].strip()))
        roz = int(rozbiezne(rodzaj, [v for _, v in kand])) if len(kand) > 1 else 0
        if rodzaj == "data":
            p = parsuj_date(wybor[1]) if wybor else None
            wiersz[f"{pole}_lokalna"] = p[0] if p else ""
            wiersz[f"{pole}_utc"] = p[1] if p else ""
            wiersz[f"{pole}_strefa"] = p[2] if p else ""
            wiersz[f"{pole}_surowa"] = wybor[1] if wybor and not p else ""   # nieczytelny format
        elif rodzaj == "minuty":
            m = parsuj_minuty(wybor[1]) if wybor else None
            wiersz[pole] = "" if m is None else m
        elif rodzaj == "liczba":
            wiersz[pole] = wybor[1].strip() if wybor else ""
        else:
            wiersz[pole] = wybor[1].strip() if wybor else ""
        wiersz[f"{pole}_zrodlo"] = wybor[0] if wybor else ""
        if pole in POLA_OSOBOWE:
            wiersz[f"{pole}_typ"] = typy.get(wybor[0], "") if wybor else ""
        if len(zrodla) > 1:
            wiersz[f"{pole}_rozbiezne"] = roz
    return wiersz, uzyte


def kolumny_pol():
    k = []
    for pole, rodzaj, zrodla in MAPA:
        if rodzaj == "data":
            k += [f"{pole}_lokalna", f"{pole}_utc", f"{pole}_strefa", f"{pole}_surowa"]
        else:
            k.append(pole)
        k.append(f"{pole}_zrodlo")
        if pole in POLA_OSOBOWE:
            k.append(f"{pole}_typ")
        if len(zrodla) > 1:
            k.append(f"{pole}_rozbiezne")
    return k


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--long", required=True, help="wyniki_long.csv z extract.py")
    ap.add_argument("--szeroki", help="wyniki.csv z extract.py (sha256, z_archiwum, data_w_archiwum)")
    ap.add_argument("--out", required=True, help="dokumenty.csv")
    ap.add_argument("--raport", help="raport JSON: nieznane tagi, rozbieżności, pominięte formaty")
    a = ap.parse_args()

    pliki = collections.OrderedDict()                # plik -> {"meta": {...}, "tagi": {...}, "typy": {...}}
    with open(a.long, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            p = pliki.setdefault(r["plik"], {"meta": {"url": r["url"], "instytucja": r["instytucja"]},
                                             "tagi": {}, "typy": {}})
            if r["wartosc"].strip():
                k = f"{r['grupa']}:{r['tag']}"
                p["tagi"][k] = r["wartosc"]
                if r.get("typ_wartosci"):            # kolumna od extract.py z 2026-10-26; starsze CSV jej nie mają
                    p["typy"][k] = r["typ_wartosci"]
    meta_s = {}
    if a.szeroki:
        with open(a.szeroki, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                meta_s[r["plik"]] = {k: r.get(k, "") for k in ("sha256", "z_archiwum", "data_w_archiwum")}

    wyniki, pominiete = [], collections.Counter()
    nieznane = collections.defaultdict(lambda: {"pliki": 0, "instytucje": set(), "przyklad": ""})
    rozb = collections.Counter()
    typy_autora = collections.Counter()
    for plik, d in pliki.items():
        fmt = d["tagi"].get("File:FileType", "")
        if fmt not in FORMATY:
            pominiete[fmt or "(brak FileType)"] += 1
            continue
        w, uzyte = normalizuj_plik(d["tagi"], d["typy"])
        for k, v in d["tagi"].items():
            if k in uzyte or pasuje(k, IGNOROWANE):
                continue
            n = nieznane[k]; n["pliki"] += 1; n["instytucje"].add(d["meta"]["instytucja"])
            n["przyklad"] = n["przyklad"] or v[:60]
        for k, v in w.items():
            if k.endswith("_rozbiezne") and v:
                rozb[k[:-len("_rozbiezne")]] += 1
        typy_autora[w.get("autor_typ") or ("(brak autora)" if not w.get("autor") else "(brak typu)")] += 1
        w["n_pol_kanonicznych"] = sum(1 for pole, rodzaj, _ in MAPA
                                      if pole != "format" and (w.get(pole) not in ("", None) if rodzaj != "data"
                                                               else w.get(f"{pole}_lokalna") or w.get(f"{pole}_surowa")))
        w["rozbiezne_pola"] = ";".join(k[:-len("_rozbiezne")] for k, v in w.items() if k.endswith("_rozbiezne") and v)
        wyniki.append({"plik": plik, **d["meta"], **meta_s.get(plik, {}), "wersja_mapy": WERSJA_MAPY, **w})

    stale = ["plik", "instytucja", "url", "sha256", "z_archiwum", "data_w_archiwum", "wersja_mapy",
             "n_pol_kanonicznych", "rozbiezne_pola"]
    kol = stale + kolumny_pol()
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    with open(a.out, "w", encoding="utf-8-sig", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=kol, extrasaction="ignore"); wr.writeheader(); wr.writerows(wyniki)

    print(f"[+] {len(wyniki)} dokumentów -> {a.out} (mapa v{WERSJA_MAPY}, {len(kol)} kolumn)", file=sys.stderr)
    if pominiete:
        print(f"[i] pominięte (format spoza listy): {dict(pominiete)}", file=sys.stderr)
    print("[i] typ autora (dokumenty): " + ", ".join(f"{k}={v}" for k, v in typy_autora.most_common()),
          file=sys.stderr)
    print("[i] rozbieżności między źródłami (pole: dokumenty): " +
          (", ".join(f"{k}={v}" for k, v in rozb.most_common()) or "brak"), file=sys.stderr)
    if nieznane:
        print(f"[!] NIEZNANE tagi ({len(nieznane)}) — dopisz do MAPA albo IGNOROWANE:", file=sys.stderr)
        for k, n in sorted(nieznane.items(), key=lambda x: -x[1]["pliki"]):
            print(f"    {k}: {n['pliki']} plików {sorted(n['instytucje'])} | np. {n['przyklad']!r}", file=sys.stderr)
    else:
        print("[+] nieznane tagi: 0", file=sys.stderr)
    if a.raport:
        Path(a.raport).write_text(json.dumps({
            "wersja_mapy": WERSJA_MAPY, "dokumentow": len(wyniki), "pominiete_formaty": dict(pominiete),
            "typy_autora": dict(typy_autora), "rozbieznosci": dict(rozb),
            "nieznane": {k: {"pliki": n["pliki"], "instytucje": sorted(n["instytucje"]), "przyklad": n["przyklad"]}
                         for k, n in nieznane.items()},
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    sys.exit(3 if nieznane else 0)


if __name__ == "__main__":
    main()
