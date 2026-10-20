#!/usr/bin/env python3
"""
sample.py — losowanie instytucji do badania z oficjalnych wykazów (operatów).

Dwa kroki:

1) draw — wczytuje operaty, stosuje wykluczenia automatyczne i ustala LOSOWĄ KOLEJNOŚĆ
   wszystkich instytucji w każdym sektorze (z zapisanym ziarnem). Wynik: kolejka.csv.
     python3 tools/sample.py draw --gminy Baza_teleadresowa_jst.xls --spolki GPW_Wskazniki.htm \
         [--uczelnie Wykaz_uczelni.htm] --out losowanie/

2) Weryfikacja ręczna: w kolejka.csv wypełniasz kolumny `decyzja` (T = bierzemy, N = zastępujemy)
   i `powod` PO KOLEI od pozycji 1 w każdym sektorze, aż będzie --n przyjętych.
   Kolumnę `www` możesz poprawić (np. ustalony ręcznie adres strony spółki).

3) finalize — sprawdza, że decyzje podjęto po kolei (bez przeskakiwania pozycji),
   i zapisuje próbę (proba.csv) oraz log zastąpień (zastapienia.csv).
     python3 tools/sample.py finalize --out losowanie/ [--n 4]

Wykluczenia automatyczne (decyzje z dziennika): adres w gov.pl lub PWr; gminy: tylko GW/GM/GMW
(bez miast na prawach powiatu, powiatów, województw, dzielnic); spółki: tylko ISIN zaczynający się od PL.
Reguły ręczne (decyzja N + powód): spółka Skarbu Państwa, infrastruktura krytyczna / usługi kluczowe,
regulamin strony zakazuje automatycznego pobierania, brak działającej strony, PWr/Wrocław-PWr,
robots.txt blokuje wszystko, za mało dokumentów (po discover).
"""
import argparse, csv, hashlib, io, json, random, re, sys, time
from pathlib import Path

import common

SEKTORY = ("gminy", "uczelnie", "spolki")
POLA = ["sektor", "poz", "id", "nazwa", "www", "info", "wykluczenie_auto", "decyzja", "powod"]
POWODY_RECZNE = [
    "skarb_panstwa", "infrastruktura_krytyczna", "regulamin_zakazuje", "brak_strony",
    "robots_blokuje_wszystko", "za_malo_dokumentow", "domena_wykluczona", "inny (opisz)",
]


def sha256(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def czysc_www(surowe: str) -> str:
    """Pierwszy sensowny adres z pola WWW operatu albo '' gdy nieczytelny."""
    s = (surowe or "").strip().lower()
    s = s.replace("htpps://", "https://").replace("htps://", "https://").replace("www:", "www.")
    for kand in re.split(r"[\s,;]+", s):
        kand = kand.strip(" .,;/")
        if not kand:
            continue
        if "://" not in kand:
            kand = "https://" + kand
        host = kand.split("://", 1)[1].split("/", 1)[0]
        if re.fullmatch(r"[\w\-]+(\.[\w\-]+)+", host) and "." in host:
            return kand
    return ""


def wykluczenie_www(www: str) -> str:
    if not www:
        return ""
    return "domena_wykluczona (gov.pl/PWr)" if common.url_wykluczony(www) else ""


# ---------- operaty ----------

def operat_gminy(path):
    import xlrd
    sh = xlrd.open_workbook(path).sheet_by_index(0)
    nag = [str(x).strip() for x in sh.row_values(0)]
    ix = {k: nag.index(k) for k in ("Kod_TERYT", "nazwa_samorządu", "Województwo", "Powiat", "typ_JST",
                                    "adres www jednostki")}
    out = []
    for r in range(1, sh.nrows):
        w = sh.row_values(r)
        typ = str(w[ix["typ_JST"]]).strip()
        if typ not in ("GW", "GM", "GMW"):
            continue
        teryt = w[ix["Kod_TERYT"]]
        teryt = str(int(teryt)).zfill(7) if isinstance(teryt, float) else str(teryt).strip().zfill(7)
        surowe = str(w[ix["adres www jednostki"]])
        www = czysc_www(surowe)
        excl = wykluczenie_www(www)
        info = f"{typ}; woj. {w[ix['Województwo']]}; pow. {w[ix['Powiat']]}"
        if not www:
            info += f"; adres w operacie nieczytelny: {surowe.strip()!r}"
        out.append({"id": teryt, "nazwa": str(w[ix["nazwa_samorządu"]]).strip(), "www": www,
                    "info": info, "wykluczenie_auto": excl})
    return out


def operat_spolki(path):
    import pandas as pd
    html = Path(path).read_text(encoding="utf-8", errors="replace")
    tabele = [t for t in pd.read_html(io.StringIO(html))
              if list(t.columns)[:4] == ["Lp", "Kod", "Nazwa", "Sektor"]]
    if not tabele:
        sys.exit("[x] W pliku GPW nie ma tabeli 'Lp, Kod, Nazwa, Sektor' (zakładka spółki krajowe?)")
    t = tabele[0]                     # pierwsza tabela = spółki krajowe (druga = zagraniczne)
    out = []
    for _, r in t.iterrows():
        isin = str(r["Kod"]).strip()
        excl = "" if isin.startswith("PL") else "isin_spoza_PL"
        sektor = re.sub(r"\D", "", str(r["Sektor"]))
        out.append({"id": isin, "nazwa": str(r["Nazwa"]).strip(), "www": "",
                    "info": f"sektor GPW {sektor}; karta: https://www.gpw.pl/spolka?isin={isin}",
                    "wykluczenie_auto": excl})
    return out


def operat_uczelnie(path):
    """Wykaz MNiSW: lista <ol> w <div class="editor-content">; link = strona uczelni.
    Adres sprowadzany do strony głównej (w wykazie bywają podstrony, np. /kandydaci/)."""
    from html.parser import HTMLParser

    class P(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.glebokosc_div = 0      # >0 = jesteśmy w editor-content
            self.w_li, self.href, self.tekst, self.wyn = False, None, [], []

        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            if tag == "div":
                if self.glebokosc_div:
                    self.glebokosc_div += 1
                elif "editor-content" in (a.get("class") or "").split():
                    self.glebokosc_div = 1
            elif self.glebokosc_div and tag == "li":
                self.w_li, self.href, self.tekst = True, None, []
            elif self.w_li and tag == "a" and self.href is None:
                self.href = a.get("href")

        def handle_data(self, d):
            if self.w_li:
                self.tekst.append(d)

        def handle_endtag(self, tag):
            if tag == "div" and self.glebokosc_div:
                self.glebokosc_div -= 1
            elif tag == "li" and self.w_li:
                nazwa = " ".join("".join(self.tekst).split())
                if self.href and nazwa:
                    self.wyn.append((nazwa, self.href))
                self.w_li = False

    p = P(); p.feed(Path(path).read_text(encoding="utf-8", errors="replace"))
    out = []
    for nazwa, href in p.wyn:
        pelny = czysc_www(href)
        schemat, reszta = pelny.split("://", 1) if pelny else ("https", "")
        host = common.normalizuj_host(reszta.split("/", 1)[0])
        www = f"{schemat}://{host}/" if host else ""
        info = f"adres w wykazie: {href}" if href.rstrip("/") != www.rstrip("/") else ""
        out.append({"id": re.sub(r"^www\.", "", host), "nazwa": nazwa, "www": www, "info": info,
                    "wykluczenie_auto": wykluczenie_www(www)})
    if len(out) < 30:
        print(f"[!] uczelnie: znaleziono tylko {len(out)} pozycji — sprawdź parser na zapisanej stronie",
              file=sys.stderr)
    return out


# ---------- draw / finalize ----------

def cmd_draw(a):
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    kol = out / "kolejka.csv"
    if kol.exists() and not a.nadpisz:
        sys.exit(f"[x] {kol} już istnieje — losowanie robi się raz. (--nadpisz, jeśli naprawdę chcesz od nowa)")
    ziarno = a.ziarno if a.ziarno is not None else random.SystemRandom().randrange(2**31)
    zrodla = {"gminy": (a.gminy, operat_gminy), "spolki": (a.spolki, operat_spolki), "uczelnie": (a.uczelnie, operat_uczelnie)}
    meta = {"ziarno": ziarno, "data_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "operaty": {}}
    wiersze = []
    for sektor in SEKTORY:
        plik, f = zrodla[sektor]
        if not plik:
            print(f"[i] {sektor}: brak operatu, pomijam", file=sys.stderr); continue
        rek = sorted(f(plik), key=lambda r: r["id"])           # stała kolejność przed losowaniem
        rng = random.Random(f"{ziarno}:{sektor}")              # niezależny strumień na sektor
        rng.shuffle(rek)
        for i, r in enumerate(rek, 1):
            dec = "N" if r["wykluczenie_auto"] else ""
            wiersze.append({"sektor": sektor, "poz": i, **r, "decyzja": dec,
                            "powod": r["wykluczenie_auto"]})
        n_ok = sum(1 for r in rek if not r["wykluczenie_auto"])
        meta["operaty"][sektor] = {"plik": Path(plik).name, "sha256": sha256(plik), "rekordow": len(rek),
                                   "po_wykluczeniach_auto": n_ok}
        print(f"[+] {sektor}: {len(rek)} w operacie, {n_ok} po wykluczeniach automatycznych", file=sys.stderr)
    with open(kol, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=POLA); w.writeheader(); w.writerows(wiersze)
    (out / "losowanie.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[+] ziarno {ziarno} -> {kol}\n    Wypełniaj `decyzja` (T/N) i `powod` po kolei od poz 1. "
          f"Powody ręczne: {', '.join(POWODY_RECZNE)}", file=sys.stderr)


def cmd_finalize(a):
    out = Path(a.out)
    rows = list(csv.DictReader(open(out / "kolejka.csv", encoding="utf-8-sig")))
    proba, zast, bledy = [], [], []
    for sektor in SEKTORY:
        rs = sorted((r for r in rows if r["sektor"] == sektor), key=lambda r: int(r["poz"]))
        if not rs:
            continue
        przyjete = 0
        for r in rs:
            if przyjete == a.n:
                break
            d = r["decyzja"].strip().upper()
            if d == "T":
                if not r["www"].strip():
                    bledy.append(f"{sektor} poz {r['poz']}: decyzja T, ale brak adresu www")
                przyjete += 1; proba.append(r)
            elif d == "N":
                if not r["powod"].strip():
                    bledy.append(f"{sektor} poz {r['poz']}: decyzja N bez powodu")
                zast.append(r)
            else:
                bledy.append(f"{sektor} poz {r['poz']}: brak decyzji — decyzje muszą iść po kolei "
                             f"(przyjęto {przyjete}/{a.n})")
                break
        else:
            if przyjete < a.n:
                bledy.append(f"{sektor}: operat wyczerpany, przyjęto {przyjete}/{a.n}")
    if bledy:
        print("[x] Nie można zamknąć losowania:\n  " + "\n  ".join(bledy), file=sys.stderr)
        sys.exit(1)
    for nazwa, dane in (("proba.csv", proba), ("zastapienia.csv", zast)):
        with open(out / nazwa, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=POLA); w.writeheader(); w.writerows(dane)
    for sektor in SEKTORY:
        zs = [z for z in zast if z["sektor"] == sektor]
        if zs or any(p["sektor"] == sektor for p in proba):
            auto = sum(1 for z in zs if z["wykluczenie_auto"])
            print(f"[+] {sektor}: przyjęto {sum(1 for p in proba if p['sektor'] == sektor)}, "
                  f"zastąpiono {len(zs)} (w tym auto {auto})", file=sys.stderr)
    print(f"[+] próba -> {out / 'proba.csv'}, log zastąpień -> {out / 'zastapienia.csv'}", file=sys.stderr)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = p.add_subparsers(dest="cmd", required=True)
    d = sp.add_parser("draw", help="wczytaj operaty i ustal losową kolejność")
    d.add_argument("--gminy", help="Baza teleadresowa JST (XLS, MSWiA)")
    d.add_argument("--spolki", help="zapisana strona GPW 'Wskaźniki' (HTML)")
    d.add_argument("--uczelnie", help="zapisana strona wykazu uczelni publicznych (HTML)")
    d.add_argument("--out", required=True)
    d.add_argument("--ziarno", type=int, help="ziarno (domyślnie losowe; zapisywane w losowanie.json)")
    d.add_argument("--nadpisz", action="store_true")
    d.set_defaults(func=cmd_draw)
    f = sp.add_parser("finalize", help="sprawdź decyzje i zapisz próbę")
    f.add_argument("--out", required=True)
    f.add_argument("--n", type=int, default=4, help="liczba instytucji na sektor (domyślnie 4)")
    f.set_defaults(func=cmd_finalize)
    a = p.parse_args(); a.func(a)


if __name__ == "__main__":
    main()
