#!/usr/bin/env python3
"""
extract.py — etap EKSTRAKCJI: exiftool -> CSV (szeroki i opcjonalnie długi) z pseudonimizacją,
potem usunięcie surowych plików (zasada 2). Logika bez zmian względem `meta2csv.py extract` (v5), z dodatkiem:
typ wartości pól osobowych (osoba/login/email/organizacja/zaslepka/nieustalone) liczony przed pseudonimizacją
(kolumny typ_wartosci / typy_wartosci), tryb --podglad do strojenia reguły (JAWNE wartości, tylko terminal)
oraz tryb --ksztalty (zamaskowane kształty wartości, bezpieczne do przekazania dalej).

Przykład:
  python3 extract.py --in dane/inst01/pliki --manifest dane/inst01/pliki/manifest.csv \\
                     --out dane/wyniki.csv --long dane/wyniki_long.csv
  python3 extract.py --in dane/inst01/pliki --podglad | less    # strojenie typów (jawne wartości!)
  python3 extract.py --in dane/inst01/pliki --ksztalty          # kształty do przekazania
"""
import argparse, csv, hashlib, hmac, json, os, re, secrets, shutil, subprocess, sys
from pathlib import Path

import common

# ---------- pseudonimizacja (zasady danych osobowych v1, pkt 3) ----------

# Domyślna ścieżka klucza jak w v5 — zmiana ścieżki = nowy klucz = inne pseudonimy niż w pilocie.
DOMYSLNY_KLUCZ = "~/.config/meta2csv/key.hex"

# Tagi (bez grupy), których wartość w całości traktujemy jako potencjalne imię/nazwisko/login.
# 2026-10-22: dodane CopyrightNotice, Credit, Photographer, For (PostScript, Illustrator) — w pilocie
# jawne imiona i nazwiska w tagach spoza listy; dodatkowo każdy tag zawierający "Author" (np. XPAuthor,
# EL_Author z szablonów Eurolook) — patrz czy_osobowy().
PERSON_TAGS = re.compile(
    r"^(Author|Authors|Creator|LastModifiedBy|LastSavedBy|Manager|Contributor|Artist|"
    r"OwnerName|Owner|By-line|Writer|Copyright|Rights|Editor|Operator|Company-?User|"
    r"CopyrightNotice|Credit|Photographer|For|"
    # 2026-10-27: Initial-creator (ODF: pierwotny autor), CreditLine (podpis zdjęcia), WebStatement (adres strony,
    # bywa z nazwiskiem), RegionName (osoba przypisana do twarzy), PersonInImage (IPTC: osoby na zdjęciu)
    r"Initial-creator|CreditLine|WebStatement|RegionName|PersonInImage)$",
    re.I,
)
# Wyjątki: pola o tej nazwie, które w danej grupie zawierają nazwę aplikacji, nie osobę.
NOT_PERSON = {"PDF:Creator"}  # w słowniku Info PDF /Creator = aplikacja źródłowa
# Wyjątek warunkowy (decyzja 2026-10-27, opcja B): zwykle program, nie osoba — Adobe DSC 3.0: %%Creator to
# „usually the name of the document composition software”; XMP-pdf:Creator spoza specyfikacji XMP, w danych program.
# Pseudonimizowane tylko wtedy, gdy reguła typu rozpozna osobę; w przeciwnym razie wartość zostaje jawna.
WARUNKOWE = {"PostScript:Creator", "XMP-pdf:Creator"}


def czy_osobowy(klucz: str) -> bool:
    """klucz = 'Grupa:Tag'. True, gdy wartość tagu w całości może być osobą (do pseudonimizacji)."""
    if klucz in NOT_PERSON:
        return False
    tag = klucz.split(":", 1)[-1]
    return bool(PERSON_TAGS.match(tag)) or "author" in tag.lower()

# ---------- typ wartości pól osobowych (decyzja A7, 2026-10-26) ----------
# Liczony na PRAWDZIWEJ wartości przed pseudonimizacją; do CSV trafia tylko typ, nigdy wartość.
# Kolejność: email -> zaślepka -> organizacja -> login -> osoba -> nieustalone (wygrywa pierwsze dopasowanie).
# 2026-10-27: typ „login” (nazwa konta: jeden ciąg bez spacji, np. jkowalski, jan.kowalski, jk12);
# typ „email” (adres e-mail; sprawdzany przed organizacją, bo ORG_RE łapie domeny .pl/.com).
TYPY_RANGA = {"email": 5, "osoba": 4, "login": 3, "nieustalone": 2, "organizacja": 1, "zaslepka": 0}  # kilka części: najwyższa

ZASLEPKI = {
    "admin", "administrator", "user", "użytkownik", "uzytkownik", "owner", "właściciel", "wlasciciel",
    "autor", "author", "imię i nazwisko", "imie i nazwisko", "name", "unknown", "nieznany", "default",
    "guest", "gość", "gosc", "test", "windows user", "microsoft office user", "użytkownik systemu windows",
    # 2026-10-28 (weryfikacja „nieustalone” na pełnej próbie):
    "system pdf", "oem", "private", "pracownik", "pracownik organizacji",
}
# Słowa, których obecność w części oznacza zaślepkę (2026-10-27: „Unknown Creator”, „your user name”, „Dell User”).
ZASLEPKI_SLOWA = {"unknown", "user", "username", "name", "użytkownik", "uzytkownik", "nieznany", "nieznana"}
# Rdzenie słów wskazujących organizację (dopasowanie od początku wyrazu, bez rozróżniania wielkości liter).
ORG_RE = re.compile(
    r"(?i)\b(urz[aą]d|gmin|starostw|powiat|uniwersyte|politechnik|akademi|uczelni|szko[lł]|wydzia[lł]|"
    r"instytut|katedr|biur[oaz]|dzia[lł]\b|dzia[lł]u|referat|sekretariat|kancelari|zesp[oó][lł]|"
    r"rad(a|y|zie|ę)\b|fundacj|stowarzysz|sp[oó][lł]k|sp[oó][lł]dziel|university|office\b|department|"
    r"ochron[aay]\s+[śs]rodowisk|kadr[ya]?\b|ksi[eę]gowo[śs]|promocj|sekcj|zarz[aą]d\b|redakcj|"
    # stanowiska (decyzja 2026-10-27, opcja A): wartość opisuje urząd, nie osobę — typ „organizacja”
    # „wójt” tylko jako całe słowo (z odmianą): bez tego „wojtek.xxx” (login z imieniem) wpadał w organizację
    r"burmistrz|w[oó]jt(a|em|owi|ów|y)?\b|starost|prezydent|dyrektor|kierownik|so[lł]tys|prezes|sekretarz|skarbnik|"
    # 2026-10-28: jednostki i stanowiska uczelni z weryfikacji „nieustalone”
    r"dziekanat|bibliotek)"
    r"|\bsp\.?\s*z\s*o\.?\s*o\b|\bs\.\s?a\.?(\s|$)|\b(ltd|inc|gmbh|llc|corp)\b\.?"
    r"|www\.|\.(pl|com|eu|org|net|edu)\b"
)
# Typowe skróty nazw jednostek (z rozróżnieniem wielkości liter, np. „UG Xyz”, „ZS nr 1”).
ORG_SKROTY_RE = re.compile(r"\b(UG|UM|UMiG|UMIG|US|RCL|SP|ZS|ZSP|ZSZ|MOPS|GOPS|MGOPS|GOK|MOK|OSP|BIP|PUP|ZUS|ZGK|ZDP|PSP)\b")
# Elementy wycinane przed klasyfikacją (np. „© 2021 Jan Kowalski” -> „Jan Kowalski”).
OCZYSC_RE = re.compile(
    r"(?i)©|\(c\)|\bcopyright\b|\ball rights reserved\b|\bwszelkie prawa zastrzeżone\b|\b(19|20)\d\d\b"
)
WYRAZ_RE = re.compile(r"^[^\W\d_]+(?:-[^\W\d_]+)*$")        # same litery, opcjonalnie z łącznikiem
INICJAL_RE = re.compile(r"^[^\W\d_]\.$")                     # „J.”
LOGIN_RE = re.compile(r"^(?=.*[^\W\d_])[\w.\-]+$")          # jeden ciąg: litery (+ cyfry, . _ -), min. 1 litera


def typ_czesci(v: str) -> str:
    if EMAIL_RE.search(v):                          # przed czyszczeniem: OCZYSC_RE rozcinałby np. jan.1985@...
        return "email"
    v = " ".join(OCZYSC_RE.sub(" ", v).replace(",", " ").split()).strip(" .;:-_/")
    if not any(c.isalpha() for c in v) or len(v) < 3 or v.casefold() in ZASLEPKI:
        return "zaslepka"
    if any(w in ZASLEPKI_SLOWA for w in re.findall(r"[^\W\d_]+", v.casefold())):
        return "zaslepka"
    if ORG_RE.search(v) or ORG_SKROTY_RE.search(v):
        return "organizacja"
    if " " not in v and LOGIN_RE.match(v):
        # same litery (z łącznikiem): login tylko pisany małymi (jkowalski); „Kowalski”, „ANNA”, „Kowalska-Nowak”
        # mogą być samym imieniem/nazwiskiem -> nieustalone. Cyfry lub . _ (jk12, jan.kowalski, JK_Kowalski) -> login.
        if WYRAZ_RE.match(v):
            return "login" if v.islower() else "nieustalone"
        return "login"
    t = v.split()
    pelne =[x for x in t if WYRAZ_RE.match(x) and len(x) >= 2]
    # nieznany skrót obok zwykłych wyrazów (np. „ABC Kowalski”) -> nie osoba; całość wielkimi literami jest OK
    skrot = not v.isupper() and any(x.isalpha() and x.isupper() and 2 <= len(x) <= 4 for x in t)
    # całość wielkimi i każdy wyraz ≤3 litery (np. kod jednostki „AB CDE”) -> nie osoba; „JAN KOWALSKI” nadal osoba
    if v.isupper() and all(len(x) <= 3 for x in t):
        return "nieustalone"
    if 2 <= len(t) <= 4 and pelne and not skrot and all(WYRAZ_RE.match(x) or INICJAL_RE.match(x) for x in t):
        return "osoba"
    return "nieustalone"


# Adres strony jako osobna część wartości (decyzja 2026-10-27): „Jan Kowalski www.jkfoto.pl” = osoba + organizacja.
URL_RE = re.compile(
    r"(?i)https?://\S+|\bwww\.[\w.-]+(?:/\S*)?|\b(?:[\w-]+\.)+(?:pl|com|eu|org|net|edu|info|io|de|uk)\b(?:/\S*)?"
)
SEPARATORY = " \t-–—/|:,;"


NAWIAS_RE = re.compile(r"\s[(\[]([^()\[\]]*)[)\]]")      # „Jan Kowalski (jkowal)” — dopisek w nawiasie po spacji


def bez_niedomknietych(v: str) -> str:
    """Usuwa nawiasy bez pary (np. „Jan Kowalski[”); sparowane zostają („Writer(R) 2.0”)."""
    for o, z in ("()", "[]"):
        if v.count(o) != v.count(z):
            v = v.replace(o, " ").replace(z, " ")
    return v


def typy_czesci(czesc: str) -> list:
    """Typy jednej części: e-mail w całości; dopiski w nawiasach (po spacji) jako osobne części;
    adresy stron -> organizacja; reszta -> typ_czesci()."""
    czesc = bez_niedomknietych(czesc)
    dopiski = NAWIAS_RE.findall(czesc)
    if dopiski and not EMAIL_RE.search(czesc):
        reszta = NAWIAS_RE.sub(" ", czesc)
        return [t for c in [reszta, *dopiski] if c.strip(SEPARATORY) for t in typy_czesci(c)]
    if EMAIL_RE.search(czesc):
        return [typ_czesci(czesc)]
    adresy = URL_RE.findall(czesc)
    if not adresy:
        return [typ_czesci(czesc)]
    reszta = URL_RE.sub(" ", czesc).strip(SEPARATORY)
    return ["organizacja"] + ([typ_czesci(reszta)] if reszta else [])


SEP_CZESCI_RE = re.compile(r"\s+\|\s+|;|\s+[-–—]\s+")


def podziel_poza_nawiasami(value: str) -> list:
    """Dzieli wartość separatorami części (SEP_CZESCI_RE), pomijając fragmenty w nawiasach () i []."""
    glebokosc, maska = 0, []
    for c in value:                       # maska: znaki w nawiasach zastąpione „x”, długość bez zmian
        if c in "([":
            glebokosc += 1
        elif c in ")]" and glebokosc:
            glebokosc -= 1
            maska.append(c); continue
        maska.append("x" if glebokosc and c not in "([" else c)
    czesci, start = [], 0
    for m in SEP_CZESCI_RE.finditer("".join(maska)):
        czesci.append(value[start:m.start()]); start = m.end()
    return czesci + [value[start:]]


def typ_wartosci(value: str) -> str:
    """osoba / login / email / organizacja / zaslepka / nieustalone dla prawdziwej wartości pola osobowego."""
    # części: „A | B”, „A; B”, „A - B” (myślnik ze spacjami, np. „Kowalska - Nowak”, „Jan Kowalski - stock.adobe.com”);
    # dzielone tylko POZA nawiasami — „Jan Kowalski (C:\X\A - B.doc)” zostaje jedną częścią
    czesci = [c for c in podziel_poza_nawiasami(value) if c.strip(SEPARATORY)] or [value]
    return max((t for c in czesci for t in typy_czesci(c)), key=TYPY_RANGA.get)

# Ścieżka bezwzględna / udział sieciowy (decyzja 2026-10-28): flaga liczona na PRAWDZIWEJ wartości przed
# pseudonimizacją — inaczej ścieżka w polu osobowym (np. MS-DOC:LastSavedBy „Jan Kowalski (C:\...)”) znika w P-….
# Zapisywana jest tylko flaga, nigdy ścieżka. Reguła WZR-01 (R3) w classify.py.
SCIEZKA_RE = re.compile(r"(?i)(?:^|[\s(\"'=])(?:[a-z]:\\|\\\\[\w.$-]+\\|/(?:home|users|mnt|media|srv|var)/)|file://")


def zawiera_sciezke(s: str) -> bool:
    return bool(SCIEZKA_RE.search(s))

EMAIL_RE = re.compile(r"([A-Za-z0-9._%+-]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
# Ścieżki z nazwą użytkownika: C:\Users\jkowalski\..., C:\Documents and Settings\x\..., /home/x/, /Users/x/
USERPATH_RE = re.compile(
    r"(?i)((?:[A-Z]:)?[\\/]+(?:Users|Documents and Settings|home)[\\/]+)([^\\/:*?\"<>|\r\n]+)"
)


def load_key(path: Path) -> bytes:
    if path.exists():
        return bytes.fromhex(path.read_text().strip())
    path.parent.mkdir(parents=True, exist_ok=True)
    k = secrets.token_bytes(32)
    path.write_text(k.hex())
    os.chmod(path, 0o600)
    print(f"[i] Utworzono nowy klucz pseudonimizacji: {path} (trzymaj poza katalogiem z danymi)", file=sys.stderr)
    return k


def pseud(key: bytes, value: str, prefix: str = "P") -> str:
    norm = " ".join(value.strip().casefold().split())
    return f"{prefix}-" + hmac.new(key, norm.encode("utf-8"), hashlib.sha256).hexdigest()[:10]


def scrub_text(key: bytes, s: str) -> tuple[str, bool]:
    """Zamienia e-maile (lokalna część) i nazwy użytkowników w ścieżkach w dowolnym polu."""
    changed = False

    def em(m):
        nonlocal changed; changed = True
        return f"{pseud(key, m.group(1), 'E')}@{m.group(2)}"   # domena zostaje (wskazuje organizację)

    def up(m):
        nonlocal changed; changed = True
        return m.group(1) + pseud(key, m.group(2), "U")

    s = EMAIL_RE.sub(em, s)
    s = USERPATH_RE.sub(up, s)
    return s, changed


def to_str(v) -> str:
    if isinstance(v, list):
        return " | ".join(to_str(x) for x in v)
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False)
    return "" if v is None else napraw_utf8(dekoduj_napis_pdf(str(v)))


# ---------- UTF-8 odczytany jako PDFDocEncoding (problem wykryty 2026-10-28 w tytułach) ----------
# Generator zapisał napis w UTF-8 bez znacznika kodowania, czytnik zinterpretował bajty jako PDFDocEncoding:
# „zaÅ‡” = „zał” (C5 82), „podjÄŽte” = „podjęte” (C4 99). Czasem dwukrotnie („Ã—â•fi” -> „Ä–” -> „ą”).
# Odwracamy: znaki -> bajty wg PDFDocEncoding -> ścisły UTF-8. Gdy bajty nie są poprawnym UTF-8 (zwykły
# polski tekst, np. „łódź” -> F3 9B… ), wartość zostaje bez zmian. Na koniec NFC: „s”+akcent (zapis rozłożony,
# m.in. macOS) -> „ś”, żeby ta sama wartość dawała ten sam pseudonim.
import unicodedata
_PDFDOC_SPEC = {  # PDFDocEncoding: kody, które różnią się od Latin-1 (0x18–0x1F, 0x80–0xA0)
    0x18: "˘", 0x19: "ˇ", 0x1A: "ˆ", 0x1B: "˙", 0x1C: "˝", 0x1D: "˛", 0x1E: "˚", 0x1F: "˜",
    0x80: "•", 0x81: "†", 0x82: "‡", 0x83: "…", 0x84: "—", 0x85: "–", 0x86: "ƒ", 0x87: "⁄", 0x88: "‹",
    0x89: "›", 0x8A: "−", 0x8B: "‰", 0x8C: "„", 0x8D: "“", 0x8E: "”", 0x8F: "‘", 0x90: "’", 0x91: "‚",
    0x92: "™", 0x93: "ﬁ", 0x94: "ﬂ", 0x95: "Ł", 0x96: "Œ", 0x97: "Š", 0x98: "Ÿ", 0x99: "Ž", 0x9A: "ı",
    0x9B: "ł", 0x9C: "œ", 0x9D: "š", 0x9E: "ž", 0xA0: "€",
}
_ZNAK_NA_BAJT = {c: b for b, c in _PDFDOC_SPEC.items()}
_ZNAK_NA_BAJT.update({chr(b): b for b in range(0xA1, 0x100) if b != 0xAD})
_UTF8_WIELOBAJT = re.compile(rb"[\xc2-\xf4][\x80-\xbf]")
licznik_utf8 = 0                                 # raportowany na końcu ekstrakcji


def napraw_utf8(s: str) -> str:
    global licznik_utf8
    oryg, wynik = s, s
    for _ in range(3):                           # podwójne kodowanie bywa w danych; więcej przebiegów nie trzeba
        if wynik.isascii():
            break
        try:
            bajty = bytes(ord(c) if ord(c) < 0x80 else _ZNAK_NA_BAJT[c] for c in wynik)
            nowy = bajty.decode("utf-8")         # ścisły: zwykły tekst nie przejdzie
        except (KeyError, UnicodeDecodeError):
            break
        if not _UTF8_WIELOBAJT.search(bajty) or nowy == wynik:
            break
        wynik = nowy
    wynik = unicodedata.normalize("NFC", wynik)
    if wynik != oryg:
        licznik_utf8 += 1
    return wynik


# ---------- napisy PDF zapisane dosłownie (problem wykryty 2026-10-27 trybem --ksztalty) ----------
# Część generatorów wpisuje do XMP (np. XMP-dc:Creator) napis w składni PDF zamiast tekstu:
# BOM UTF-16 i znaki jako sekwencje ósemkowe, np. "\376\377\000J\000a\000n" = "Jan".
# Bez dekodowania ta sama osoba dostaje inny pseudonim niż w PDF:Author, a typ wartości wychodzi „nieustalone”.
NAPIS_PDF_RE = re.compile(r"^\\(376\\377|377\\376)")
_ESC_PDF_RE = re.compile(r"\\([0-7]{1,3}|.)", re.S)
_ESC_PDF = {"n": b"\n", "r": b"\r", "t": b"\t", "b": b"\b", "f": b"\f"}
licznik_zdekodowanych = 0                        # raportowany na końcu ekstrakcji


def dekoduj_napis_pdf(s: str) -> str:
    """Napis w składni PDF z BOM UTF-16 (\\376\\377 = BE, \\377\\376 = LE) -> zwykły tekst. Inne wartości bez zmian."""
    global licznik_zdekodowanych
    if not NAPIS_PDF_RE.match(s):
        return s
    def bajty(m):
        e = m.group(1)
        if e[0] in "01234567":
            return chr(int(e, 8) & 0xFF)
        return _ESC_PDF.get(e, e.encode("latin-1", "replace")).decode("latin-1")   # \\ \( \) -> znak
    surowe = _ESC_PDF_RE.sub(bajty, s).encode("latin-1", "replace")
    kodowanie = "utf-16-be" if surowe.startswith(b"\xfe\xff") else "utf-16-le"
    tekst = surowe[2:].decode(kodowanie, "replace").strip("\x00")
    licznik_zdekodowanych += 1
    return tekst


# ---------- tryb --ksztalty (decyzja 2026-10-27, wariant B) ----------
# Kształt wartości = zamaskowana postać, którą można przekazać dalej (do rozmowy z Claude) zamiast jawnej
# wartości. Litery -> klasy wyrazów, cyfry -> 9, interpunkcja zostaje. Jawnie zostają WYŁĄCZNIE: słowa ogólne
# z listy SLOWA_OGOLNE, rdzenie organizacji z ORG_RE, skróty jednostek z ORG_SKROTY_RE i końcówki domen z TLD.
# Imiona, nazwiska, loginy i nazwy własne nie przechodzą (dodatkowo pilnuje tego bezpiecznik w ksztalt()).
SLOWA_OGOLNE = {w for z in ZASLEPKI for w in z.split()} | {
    "to", "by", "your", "my", "the", "of", "and", "for", "creator", "unknown", "copyright", "all", "rights",
    "reserved", "wszelkie", "prawa", "zastrzeżone", "zastrzezone", "nr", "im", "stock", "photo", "foto", "fot",
    "www", "http", "https",
    # stanowiska i funkcje (klasa błędów #5 z analizy podglądu 2026-10-27)
    "burmistrz", "burmistrza", "wójt", "wojt", "wójta", "wojta", "starosta", "prezydent", "dyrektor",
    "kierownik", "sołtys", "soltys", "sekretarz", "skarbnik", "prezes", "zastępca", "zastepca", "inspektor",
    "specjalista", "referent", "główny", "glowny", "księgowy", "ksiegowy", "ochrona", "środowiska",
    "srodowiska", "kadry", "księgowość", "ksiegowosc", "promocja", "redakcja", "zarząd", "zarzad",
}
TLD = {"pl", "com", "eu", "org", "net", "edu", "gov", "info", "de", "uk", "io"}
_TOKEN_RE = re.compile(r"[^\W\d_]+|\d+|\s+|.", re.S)


def _klasa_wyrazu(w: str) -> str:
    """Klasa ciągu liter: A (1 wielka), a (1 mała), AA (2–4 wielkie), AA+ (≥5 wielkich), Aa (Wielka+małe),
    a (same małe, ≥2 → 'aa'), aA (mieszane, np. UMiG)."""
    if len(w) == 1:
        return "A" if w.isupper() else "a"
    if w.isupper():
        return "AA" if len(w) <= 4 else "AA+"
    if w.islower():
        return "aa"
    if w[0].isupper() and w[1:].islower():
        return "Aa"
    return "aA"


def ksztalt(value: str) -> str:
    """Zamaskowany kształt wartości (bez imion, nazwisk, loginów). Przykłady:
    'Jan Kowalski' -> 'Aa Aa'; 'jkowalski' -> 'aa'; 'jan.kowalski@uczelnia.edu.pl' -> 'aa.aa@aa.«edu».«pl»';
    'Urząd Gminy Xyz' -> '«urząd» «gmin…» Aa'; 'J. Kowalska - Nowak' -> 'A. Aa - Aa'."""
    tokeny = _TOKEN_RE.findall(value)
    wynik, poprzedni = [], ""
    for t in tokeny:
        if t.isspace():
            wynik.append(" ")
        elif t.isdigit():
            wynik.append("9")
        elif t.isalpha():
            cf = t.casefold()
            m = ORG_RE.match(t)
            if cf in SLOWA_OGOLNE:
                wynik.append(f"«{cf}»")
            elif poprzedni == "." and cf in TLD:
                wynik.append(f"«{cf}»")
            elif m and m.group(1):
                wynik.append(f"«{m.group(1).casefold()}{'…' if len(m.group(1)) < len(t) else ''}»")
            elif ORG_SKROTY_RE.fullmatch(t):
                wynik.append(f"«{t}»")
            else:
                wynik.append(_klasa_wyrazu(t))
        else:
            wynik.append(t)                       # interpunkcja i znaki specjalne (©, @, -, _, (, ), |)
        if not t.isspace():
            poprzedni = t
    s = "".join(wynik)
    # bezpiecznik: żaden niewymieniony z nazwy wyraz oryginału (≥3 litery) nie może pojawić się w kształcie
    jawne = {x.casefold() for x in re.findall(r"«([^»]+?)…?»", s)}
    for w in re.findall(r"[^\W\d_]{3,}", value):
        cf = w.casefold()
        if cf not in jawne and not any(cf == j or cf.startswith(j) for j in jawne) and cf in s.casefold():
            return "[ukryty: bezpiecznik]"
    return s


def flagi_wartosci(value: str) -> str:
    """Cechy wartości istotne dla reguły typu (bez treści)."""
    f = []
    if EMAIL_RE.search(value): f.append("email")
    if re.search(r"(?i)www\.|https?://|\b[\w-]+\.(pl|com|eu|org|net|edu)\b", value) and "email" not in f:
        f.append("domena")
    if re.search(r"[()\[\]]", value): f.append("nawias")
    if re.search(r"\s-\s", value): f.append("myslnik_ze_spacjami")
    if re.search(r"\s+\|\s+|;", value): f.append("wiele_czesci")
    if re.search(r"\d", value): f.append("cyfry")
    if re.search(r"[._]", value) and not f.count("email") and not f.count("domena"): f.append("kropka_podkreslnik")
    if ORG_RE.search(value) or ORG_SKROTY_RE.search(value): f.append("regula_org")
    return ",".join(f)


# ---------- extract ----------

DROP_PREFIXES = ("ZIP:", "System:FileAccessDate", "System:FileInodeChangeDate",
                 "System:FilePermissions", "System:Directory", "System:FileModifyDate",
                 "ExifTool:ExifToolVersion", "SourceFile")

def check_exiftool():
    exe = shutil.which("exiftool")
    if not exe:
        sys.exit("[x] Brak exiftool. Debian/Ubuntu: sudo apt install libimage-exiftool-perl libarchive-zip-perl")
    ver = subprocess.run([exe, "-ver"], capture_output=True, text=True).stdout.strip()
    return exe, ver

UNZIP_DIR = "_rozpakowane"
ZIP_MAX_MEMBERS = 500
ZIP_MAX_TOTAL = 500 * 1024 * 1024   # 500 MB po rozpakowaniu (ochrona przed zip bomb)

def is_plain_zip(p: Path) -> bool:
    """ZIP, który nie jest kontenerem OOXML (docx/xlsx/pptx) ani ODF (odt/ods)."""
    import zipfile
    if not zipfile.is_zipfile(p):
        return False
    try:
        with zipfile.ZipFile(p) as z:
            names = set(z.namelist())
    except Exception:
        return False
    return "[Content_Types].xml" not in names and "mimetype" not in names

def member_name(info) -> str:
    n = info.filename
    if not (info.flag_bits & 0x800):          # brak flagi UTF-8: Windows zapisuje w stronie OEM (PL: cp852)
        try:
            n = n.encode("cp437").decode("cp852")
        except Exception:
            pass
    return n

def expand_zips(indir: Path) -> dict:
    """Rozpakowuje zwykłe ZIP-y do indir/_rozpakowane/<zip>/ (płasko, bez ścieżek z archiwum).
    Zwraca {nazwa_pliku_wyjściowego: (nazwa_zipa, ścieżka_w_archiwum, data_mod_w_archiwum)}."""
    import zipfile
    out = {}
    for zp in sorted(p for p in indir.iterdir() if p.is_file() and is_plain_zip(p)):
        dest = indir / UNZIP_DIR / zp.name
        dest.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zp) as z:
            infos = [i for i in z.infolist() if not i.is_dir()]
            if len(infos) > ZIP_MAX_MEMBERS or sum(i.file_size for i in infos) > ZIP_MAX_TOTAL:
                print(f"[!] {zp.name}: za duże archiwum, pomijam", file=sys.stderr); continue
            pominiete = 0
            for j, info in enumerate(infos):
                inner = member_name(info)
                # tylko formaty z listy (decyzja 2026-10-21); zagnieżdżone ZIP-y i np. XML/XHTML pomijane
                rozsz = inner.rsplit(".", 1)[-1].lower() if "." in inner.rsplit("/", 1)[-1] else ""
                if rozsz not in common.RODZINY or rozsz == "zip":
                    pominiete += 1
                    continue
                safe = re.sub(r"[^\w.\-]", "_", Path(inner.replace("\\", "/")).name)[:120] or "plik"
                target = dest / f"{j:03d}_{safe}"            # płaska nazwa => brak zip-slip
                if not target.exists():
                    with z.open(info) as src, open(target, "wb") as f:
                        shutil.copyfileobj(src, f)
                mtime = "%04d-%02d-%02d %02d:%02d:%02d" % info.date_time
                out[target.name] = (zp.name, inner, mtime)
        print(f"[+] rozpakowano {zp.name}: {len(infos) - pominiete} plików"
              + (f" (pominięto {pominiete} spoza listy formatów)" if pominiete else ""), file=sys.stderr)
    return out

def uruchom_exiftool(exe, files):
    res = subprocess.run([exe, "-j", "-G1", "-a", "-s", "-charset", "filename=utf8", *map(str, files)],
                         capture_output=True, text=True)
    try:
        return json.loads(res.stdout or "[]")
    except json.JSONDecodeError:
        sys.exit("[x] Nie udało się sparsować wyjścia exiftool:\n" + res.stderr)


def wartosci_osobowe(data):
    """Generator (klucz, wartość) dla niepustych pól osobowych — wspólny dla --podglad i --ksztalty."""
    for d in data:
        for k, v in d.items():
            if k.startswith(DROP_PREFIXES) or not czy_osobowy(k):
                continue
            s = to_str(v)
            if s:
                yield k, s


def podglad(data):
    """Tryb --podglad: typy wartości pól osobowych na terminal. Nic nie zapisuje i nic nie usuwa.
    UWAGA: wypisuje JAWNE wartości — tylko do przejrzenia lokalnie (| less), nie zapisywać, nie wysyłać."""
    import collections
    licz = collections.Counter()                      # typ -> liczba wystąpień
    wart = collections.defaultdict(lambda: [0, set(), ""])   # wartość -> [liczba, tagi, typ]
    for k, s in wartosci_osobowe(data):
        t = typ_wartosci(s)
        licz[t] += 1
        w = wart[s]; w[0] += 1; w[1].add(k); w[2] = t
    print(f"### PODGLĄD (nic nie zapisano): {len(data)} plików, {sum(licz.values())} wartości w polach osobowych")
    for t in sorted(TYPY_RANGA, key=TYPY_RANGA.get, reverse=True):
        print(f"   {t:12s} {licz[t]:6d} wystąpień, {sum(1 for w in wart.values() if w[2] == t):5d} unikalnych")
    print("### ^ te liczby możesz przekazać dalej; PONIŻEJ JAWNE WARTOŚCI — nie zapisuj i nie wysyłaj")
    for t in sorted(TYPY_RANGA, key=TYPY_RANGA.get, reverse=True):
        print(f"\n== {t}")
        for s, (n, tagi, tt) in sorted(wart.items(), key=lambda x: (-x[1][0], x[0])):
            if tt == t:
                print(f"{n:5d}  {s[:80]:80s}  {','.join(sorted(tagi))[:60]}")


def ksztalty(data):
    """Tryb --ksztalty: dla każdej pary (typ, kształt) liczba wystąpień, liczba różnych wartości, flagi i tagi.
    Wypisuje WYŁĄCZNIE kształty (bez jawnych wartości) — wynik można przekazać dalej. Nic nie zapisuje."""
    import collections
    grupy = collections.defaultdict(lambda: {"n": 0, "wartosci": set(), "tagi": set(), "flagi": set()})
    for k, s in wartosci_osobowe(data):
        t = typ_wartosci(s)
        g = grupy[(t, ksztalt(s)[:70])]
        g["n"] += 1; g["wartosci"].add(s); g["tagi"].add(k)       # pełny klucz Grupa:Tag (np. PostScript:Creator)
        g["flagi"].update(x for x in flagi_wartosci(s).split(",") if x)
    licz = collections.Counter()
    for (t, _), g in grupy.items():
        licz[t] += g["n"]
    print(f"### KSZTAŁTY (bez jawnych wartości, można przekazać): {len(data)} plików, "
          f"{sum(licz.values())} wartości w polach osobowych")
    print("### legenda: Aa = Wielka+małe, aa = małe, A/a = 1 litera, AA = 2–4 wielkie, AA+ = ≥5 wielkich, "
          "aA = mieszane, 9 = cyfry, «x» = słowo ogólne / rdzeń / skrót / domena")
    for t in sorted(TYPY_RANGA, key=TYPY_RANGA.get, reverse=True):
        wiersze = sorted(((k[1], g) for k, g in grupy.items() if k[0] == t), key=lambda x: (-x[1]["n"], x[0]))
        print(f"\n== {t} ({licz[t]} wystąpień)")
        print(f"{'n':>5} {'uniq':>4}  {'kształt':40s}  {'flagi':32s}  tagi")
        for kszt, g in wiersze:
            print(f"{g['n']:5d} {len(g['wartosci']):4d}  {kszt:40s}  {','.join(sorted(g['flagi']))[:32]:32s}  "
                  f"{','.join(sorted(g['tagi']))[:90]}")


def cmd_extract(a):
    exe, ver = check_exiftool()
    key = load_key(Path(a.key_file).expanduser())
    indir = Path(a.inp)
    zip_members = expand_zips(indir)
    files = sorted(p for p in indir.rglob("*")
                   if p.is_file() and p.name != "manifest.csv" and not is_plain_zip(p))
    if not files:
        sys.exit(f"[x] Brak plików w {indir}")

    manifest = {}
    if a.manifest:
        with open(a.manifest, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("plik"):
                    manifest[row["plik"]] = row

    data = uruchom_exiftool(exe, files)
    if a.ksztalty:
        return ksztalty(data)
    if a.podglad:
        return podglad(data)

    rows, long_rows, all_cols = [], [], set()
    zip_warn = False
    n_warunkowe_jawne = 0                       # wartości z WARUNKOWE pozostawione jawnie (typ inny niż osoba)
    for d in data:
        src = Path(d.get("SourceFile", ""))
        rel = src.relative_to(indir).as_posix() if src.is_absolute() or str(src).startswith(str(indir)) else src.name
        if "Archive::Zip" in to_str(d.get("ExifTool:Warning", "")):
            zip_warn = True
        zm = zip_members.get(src.name) if src.parent.parent.name == UNZIP_DIR else None
        if zm:
            m = manifest.get(zm[0]) or {}
            inner_scrubbed, _ = scrub_text(key, zm[1])
            url = (m.get("url", "") + "#" + inner_scrubbed) if m.get("url") else ""
        else:
            m = manifest.get(src.name) or manifest.get(rel) or {}
            url = m.get("url", "")
        # klucz pliku w wynikach: <instytucja>/<ścieżka> (2026-10-27) — fetch numeruje pliki od 0000 w każdej
        # instytucji, więc przy jednym wyniki.csv dla całej próby sama nazwa mogłaby się powtórzyć i nadpisać wiersz
        inst = m.get("instytucja", "")
        rel = f"{inst}/{rel}" if inst else rel
        row = {
            "plik": rel,
            "sha256": hashlib.sha256(src.read_bytes()).hexdigest(),
            "url": url,
            "instytucja": m.get("instytucja", ""),
            "exiftool_ver": ver,
            "z_archiwum": zm[0] if zm else "",
            "data_w_archiwum": zm[2] if zm else "",
        }
        pseud_fields = []
        typy = {}
        tags = {}
        sciezki = set()                                  # klucze tagów, których prawdziwa wartość zawiera ścieżkę
        for k, v in d.items():
            if k.startswith(DROP_PREFIXES):
                continue
            s = to_str(v)
            if s and zawiera_sciezke(s):
                sciezki.add(k)                           # przed pseudonimizacją; zapisujemy tylko flagę
            if s and czy_osobowy(k) and k in WARUNKOWE and typ_wartosci(s) != "osoba":
                s, ch = scrub_text(key, s)               # program: zostaje jawny (e-maile/ścieżki nadal czyszczone)
                if ch: pseud_fields.append(k)
                n_warunkowe_jawne += 1
            elif s and czy_osobowy(k):
                typy[k] = typ_wartosci(s)               # przed pseudonimizacją, potem wartość znika
                m = EMAIL_RE.fullmatch(s.strip())
                if m:   # sam adres (decyzja 2026-10-27): jak w scrub_text — część lokalna pseudonimem, domena jawna
                    s = f"{pseud(key, m.group(1), 'E')}@{m.group(2)}"
                else:   # wszystko inne, także adres z dopiskiem (np. „Jan Kowalski <jan@x.pl>”) -> całość pseudonimem
                    s = pseud(key, s, "P")
                pseud_fields.append(k)
            else:
                s, ch = scrub_text(key, s)
                if ch: pseud_fields.append(k)
            tags[k] = s
        row["n_tagow"] = len(tags)
        row["n_niepustych"] = sum(1 for v in tags.values() if v != "")
        row["pola_pseudonimizowane"] = ";".join(pseud_fields)
        row["typy_wartosci"] = ";".join(f"{k}={t}" for k, t in typy.items())
        row["tagi_ze_sciezka"] = ";".join(sorted(sciezki))
        row.update(tags)
        all_cols.update(tags)
        rows.append(row)
        for k, v in tags.items():
            long_rows.append({"plik": rel, "url": row["url"], "instytucja": row["instytucja"],
                              "grupa": k.split(":", 1)[0], "tag": k.split(":", 1)[-1], "wartosc": v,
                              "pseudonimizowane": int(k in pseud_fields), "typ_wartosci": typy.get(k, ""),
                              "sciezka": int(k in sciezki)})

    if zip_warn:
        print("[!] UWAGA: exiftool nie ma Archive::Zip — metadane DOCX/XLSX/PPTX NIE zostały odczytane.\n"
              "    Zainstaluj: sudo apt install libarchive-zip-perl i uruchom ponownie.", file=sys.stderr)
        if not a.allow_missing_zip:
            sys.exit(2)

    if n_warunkowe_jawne:
        print(f"[i] {n_warunkowe_jawne} wartości z {sorted(WARUNKOWE)} pozostawiono jawnie (typ inny niż osoba)",
              file=sys.stderr)
    if licznik_utf8:
        print(f"[i] naprawiono {licznik_utf8} wartości: UTF-8 odczytany jako PDFDocEncoding lub zapis rozłożony (NFC)",
              file=sys.stderr)
    if licznik_zdekodowanych:
        print(f"[i] zdekodowano {licznik_zdekodowanych} wartości zapisanych jako napis PDF (UTF-16 ósemkowo)",
              file=sys.stderr)

    missing_url = [r["plik"] for r in rows if not r["url"]]
    if missing_url:
        print(f"[!] {len(missing_url)} plików bez URL w manifeście: {', '.join(missing_url[:5])}...", file=sys.stderr)

    fixed = ["plik", "sha256", "url", "instytucja", "exiftool_ver", "z_archiwum", "data_w_archiwum", "n_tagow",
             "n_niepustych", "pola_pseudonimizowane", "typy_wartosci", "tagi_ze_sciezka"]
    # Dopisywanie do istniejących wyników (pliki są usuwane po ekstrakcji, więc kolejne partie trzeba łączyć)
    out_p = Path(a.out)
    new_names = {r["plik"] for r in rows}
    if out_p.exists():
        with open(out_p, encoding="utf-8-sig") as f:
            old = [r for r in csv.DictReader(f) if r["plik"] not in new_names]
        for r in old:
            all_cols.update(k for k in r if k not in fixed)
        rows = old + rows
        print(f"[i] dopisuję do istniejącego {out_p} ({len(old)} wcześniejszych plików)", file=sys.stderr)
    cols = fixed + sorted(all_cols)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    with open(out_p, "w", newline="", encoding="utf-8-sig") as f:   # utf-8-sig: poprawne polskie znaki w Excelu
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader(); w.writerows(rows)
    print(f"[+] {len(rows)} plików łącznie -> {out_p} ({len(cols)} kolumn)", file=sys.stderr)
    if a.long:
        lp = Path(a.long)
        lf = ["plik", "url", "instytucja", "grupa", "tag", "wartosc", "pseudonimizowane", "typ_wartosci", "sciezka"]
        if lp.exists():
            with open(lp, encoding="utf-8-sig") as f:
                long_rows = [r for r in csv.DictReader(f) if r["plik"] not in new_names] + long_rows
        with open(lp, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=lf, restval=""); w.writeheader(); w.writerows(long_rows)
        print(f"[+] format długi -> {lp} ({len(long_rows)} wierszy)", file=sys.stderr)

    # Minimalizacja: surowe pliki usuwane po zapisaniu wyników (zostaje manifest.csv i sha256 w CSV)
    if not a.keep_files:
        n_del = 0
        for p in files:
            p.unlink(missing_ok=True); n_del += 1
        for p in indir.iterdir():
            if p.is_file() and p.name != "manifest.csv" and is_plain_zip(p):
                p.unlink(); n_del += 1
        shutil.rmtree(indir / UNZIP_DIR, ignore_errors=True)
        print(f"[+] usunięto {n_del} surowych plików z {indir} (zostaje manifest.csv)", file=sys.stderr)

def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--in", dest="inp", required=True)
    p.add_argument("--manifest", help="CSV z kolumnami plik,url[,instytucja]")
    p.add_argument("--out", help="CSV wynikowy (wymagany, chyba że --podglad lub --ksztalty)")
    p.add_argument("--long", help="opcjonalny CSV w formacie długim (plik, tag, wartość)")
    p.add_argument("--key-file", default=DOMYSLNY_KLUCZ, help="klucz HMAC (poza katalogiem danych)")
    p.add_argument("--keep-files", action="store_true", help="NIE usuwaj surowych plików po ekstrakcji (tylko do debugowania)")
    tryb = p.add_mutually_exclusive_group()
    tryb.add_argument("--podglad", action="store_true",
                      help="tylko podgląd typów wartości pól osobowych na terminal (JAWNE wartości; nic nie zapisuje ani nie usuwa)")
    tryb.add_argument("--ksztalty", action="store_true",
                      help="tylko zamaskowane kształty wartości pól osobowych (bez jawnych wartości; nic nie zapisuje ani nie usuwa)")
    p.add_argument("--allow-missing-zip", action="store_true", help="nie przerywaj, gdy brak Archive::Zip")
    a = p.parse_args()
    if not a.out and not (a.podglad or a.ksztalty):
        p.error("--out jest wymagany (poza trybami --podglad i --ksztalty)")
    try:
        cmd_extract(a)
    except BrokenPipeError:                       # np. wyjście z `less` przed końcem podglądu
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(0)


if __name__ == "__main__":
    main()
