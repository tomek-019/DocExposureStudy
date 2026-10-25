#!/usr/bin/env python3
"""
discover.py — etap SZUKANIA: od strony głównej instytucji znajduje dokumenty w jej domenie
(sitemapy + linki), a na końcu losuje z nich próbę dla fetch.py.

Zasady (dziennik 2026-10-18):
  - zakres: domena i wszystkie jej subdomeny (np. gmina-x.pl, bip.gmina-x.pl),
  - budżet: 1500 żądań na instytucję (wszystkie: robots.txt, sitemapy, strony, sprawdzenia), limit głębokości,
  - dokument = link z rozszerzeniem z listy formatów; linki wyglądające na pobieranie
    (download/attachment/pobierz/plik/getfile…) bez rozszerzenia są sprawdzane po Content-Type,
  - robots.txt (jawny UA), min. 5 s per host, pomijanie gov.pl i PWr, log każdego żądania,
  - kolejność (decyzja 2026-10-23, UA 1.2): przeszukiwanie wszerz od strony głównej po linkach; strony
    z sitemap trafiają do kolejki ZAPASOWEJ, odwiedzanej dopiero po wyczerpaniu kolejki głównej
    (dokumenty z listy formatów wskazane w sitemapie od razu trafiają do kandydatów, bez żądania),
  - dodatkowe domeny instytucji (decyzja 2026-10-25, UA 1.4): --dodatkowa-domena (np. BIP gminy na platformie
    zewnętrznej, domena relacji inwestorskich spółki); zakres = ten host i jego subdomeny; wspólny budżet;
    strony brane z domen NAPRZEMIENNIE (gdy jedna się wyczerpie, budżet przechodzi na pozostałe),
  - losowanie: do --limit (domyślnie 100) dokumentów z zapisanym ziarnem (jedna pula ze wszystkich domen).

Wyjście w katalogu --out:
  kandydaci.csv     wszystkie znalezione dokumenty (url, rodzina, rozszerzenie, źródło, strona-rodzic, głębokość,
                    domena)
  urls.txt          wylosowane URL-e w formacie wejściowym fetch.py ("URL,instytucja")
  podsumowanie.json liczby, budżet, ziarno losowania, powód zakończenia, odrzucone typy odpowiedzi
  zadania_discover.jsonl  log żądań

Przykład:
  python3 discover.py --start https://www.gmina-x.pl --instytucja GMINA-01 --out dane/GMINA-01
  python3 discover.py --start https://www.gmina-y.pl --dodatkowa-domena https://gmina-y.platforma-bip.net \
                      --instytucja GMINA-03 --out dane/GMINA-03
  python3 fetch.py --urls dane/GMINA-01/urls.txt --out dane/GMINA-01/pliki
"""
import argparse, csv, gzip, json, random, re, sys, time, urllib.error, urllib.parse
import xml.etree.ElementTree as ET
from collections import deque
from html.parser import HTMLParser
from pathlib import Path

import common

# Linki, które mogą prowadzić do pliku mimo braku rozszerzenia w adresie
WZORZEC_POBIERANIA = re.compile(
    r"download|attachment|pobierz|zalacznik|załącznik|getfile|get_file|file\.php|plik|dokument|"
    r"fileadmin|/files?/|/media/|/upload", re.I)
# Rozszerzenia, których nie odwiedzamy (nie są stronami ani dokumentami z listy)
POMIJANE_ROZSZ = {
    "css", "js", "json", "xml", "rss", "atom", "ico", "svg", "gif", "webp", "bmp", "avif", "woff", "woff2",
    "ttf", "eot", "otf", "mp3", "mp4", "avi", "mov", "wmv", "webm", "ogg", "wav", "flv", "m4a", "m4v",
    "rar", "7z", "gz", "tgz", "tar", "exe", "msi", "apk", "dmg", "iso", "bin", "txt", "csv", "ics", "vcf",
    "kml", "kmz", "gpx", "eps", "psd", "ai", "dwg", "dxf", "shp", "xades", "sig", "p7s",
}
MAX_HTML = 5 * 1024 * 1024        # strona większa niż 5 MB nie jest parsowana
MAX_DLUGOSC_URL = 500
RODZINY_OBRAZOW = {"png", "jpg", "tiff"}
# Ochrona przed pułapkami (2026-10-21): limit wariantów zapytania na ścieżkę odcinał całe serwisy CMS
# z adresami typu /asp/pl_start.asp?typ=..&menu=.. i stronicowanie BIP, więc go usunięto. Nieskończone
# kalendarze/stronicowania ogranicza limit głębokości (każdy kolejny link = +1) i budżet żądań.
# Zostają: normalizacja zapytania (kolejność, parametry sesji i śledzące) i wykrywanie powtarzających
# się segmentów ścieżki (pętle względnych linków, np. /a/b/a/b/a/b/).
PARAMETRY_POMIJANE = re.compile(r"^(utm_\w+|fbclid|gclid|phpsessid|sessionid|jsessionid)$", re.I)
MAX_POWTORZEN_SEGMENTU = 3
# Odrzucone typy odpowiedzi (decyzja 2026-10-22): odpowiedź, która nie jest ani HTML, ani dokumentem
# z listy, jest liczona per Content-Type z przykładowymi adresami; przy >= PROG_OSTRZEZENIA odrzuceń
# jednego typu crawl kończy się ostrzeżeniem (próg tylko informacyjny, nie wpływa na dane).
PRZYKLADOW_NA_TYP = 3
PROG_OSTRZEZENIA = 20


class Linki(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hrefy, self.base = [], None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "base" and a.get("href"):
            self.base = a["href"]
        elif tag in ("a", "area") and a.get("href"):
            self.hrefy.append(a["href"])
        elif tag in ("iframe", "frame", "embed") and a.get("src"):
            self.hrefy.append(a["src"])
        elif tag == "object" and a.get("data"):
            self.hrefy.append(a["data"])


def normalizuj_url(href: str, baza: str) -> str:
    href = (href or "").strip()
    if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
        return ""
    u = urllib.parse.urljoin(baza, href)
    p = urllib.parse.urlsplit(u)
    if p.scheme not in ("http", "https") or not p.netloc:
        return ""
    netloc = p.netloc.lower()
    if (p.scheme == "http" and netloc.endswith(":80")) or (p.scheme == "https" and netloc.endswith(":443")):
        netloc = netloc.rsplit(":", 1)[0]
    pary = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)
            if not PARAMETRY_POMIJANE.match(k)]
    zapytanie = urllib.parse.urlencode(sorted(pary), doseq=True) if p.query else ""
    u = urllib.parse.urlunsplit((p.scheme, netloc, p.path or "/", zapytanie, ""))
    return u if len(u) <= MAX_DLUGOSC_URL else ""


def petla_segmentow(sciezka: str) -> bool:
    seg = [x for x in sciezka.split("/") if x]
    return any(seg.count(x) > MAX_POWTORZEN_SEGMENTU for x in set(seg))


class Crawler:
    def __init__(self, a):
        self.a = a
        self.out = Path(a.out); self.out.mkdir(parents=True, exist_ok=True)
        start = a.start if "://" in a.start else "https://" + a.start
        self.start = normalizuj_url(start, start)
        host = common.normalizuj_host(urllib.parse.urlsplit(self.start).netloc)
        self.domena = common.normalizuj_host(a.domena) if a.domena else re.sub(r"^www\.", "", host)
        # dodatkowe domeny: (adres startowy, zakres = host bez www. i jego subdomeny)
        self.starty = [(self.start, self.domena)]
        for d in (a.dodatkowa_domena or []):
            u = normalizuj_url(d if "://" in d else "https://" + d, d if "://" in d else "https://" + d)
            if u:
                self.starty.append((u, re.sub(r"^www\.", "", common.normalizuj_host(urllib.parse.urlsplit(u).netloc))))
        self.domeny = [z for _, z in self.starty]
        self.log = common.LogZadan(self.out / "zadania_discover.jsonl")
        # zakres i budżet przekazane do klienta: kontrola także dla kroków przekierowań (decyzja 2026-10-25)
        self.klient = common.Klient(a.ua, self.log, common.Limiter(a.delay), etap="discover",
                                    zakres=self.w_zakresie, limit_zadan=a.budzet)
        self.robots = common.Robots(self.klient)
        # osobne kolejki dla każdej domeny (obsługiwane naprzemiennie)
        self.kolejki = [deque() for _ in self.domeny]
        # kolejka zapasowa na strony z sitemap (decyzja 2026-10-23): duża sitemapa (np. aktualności) zapełniała
        # poziom 1 przed linkami ze strony głównej i budżet kończył się, zanim crawler doszedł do BIP/menu
        self.zapasy = [deque() for _ in self.domeny]
        self._rr = 0                  # wskaźnik naprzemiennego wyboru domeny
        self.stron_wg_domeny = [0 for _ in self.domeny]
        self.w_zapasie = set()        # URL-e czekające w kolejce zapasowej (promowane, gdy znajdzie je link)
        self.n_stron_z_zapasu = 0
        self.widziane = set()
        self.kandydaci = {}           # url -> wiersz
        self.n_stron = self.n_sprawdzen = self.n_sitemap = 0
        self.pominiete = {"poza_domena": 0, "wykluczona_domena": 0, "robots": 0, "glebokosc": 0, "pulapka": 0}
        self.pominiete_gdzie = {k: {} for k in self.pominiete}   # diagnostyka: host + początek ścieżki
        self.odrzucone = {}           # Content-Type -> {"sprawdzenie": n, "strona": n, "przyklady": [url]}
        self.powod = "przeszukano wszystko"
        self.js_ostrzezenie = False

    def pomin(self, kat, url):
        self.pominiete[kat] += 1
        p = urllib.parse.urlsplit(url)
        seg = [x for x in p.path.split("/") if x][:2]
        klucz = p.netloc + "/" + "/".join(seg)
        d = self.pominiete_gdzie[kat]
        d[klucz] = d.get(klucz, 0) + 1

    def odrzuc(self, ct, typ, url):
        """Odpowiedź ani HTML, ani dokument z listy: licznik per Content-Type + przykładowe adresy."""
        klucz = (ct or "").split(";")[0].strip() or "(brak Content-Type)"
        d = self.odrzucone.setdefault(klucz, {"sprawdzenie": 0, "strona": 0, "przyklady": []})
        d["sprawdzenie" if typ == "sprawdz" else "strona"] += 1
        if len(d["przyklady"]) < PRZYKLADOW_NA_TYP:
            d["przyklady"].append(url)

    # ---- pomocnicze ----
    def indeks_domeny(self, url):
        """Indeks domeny instytucji, do której należy URL (najdłuższe dopasowanie), albo None."""
        h = common.normalizuj_host(urllib.parse.urlsplit(url).netloc)
        pas = [(len(d), i) for i, d in enumerate(self.domeny) if h == d or h.endswith("." + d)]
        return max(pas)[1] if pas else None

    def w_zakresie(self, url):
        return self.indeks_domeny(url) is not None

    def budzet_ok(self):
        return self.klient.liczba_zadan < self.a.budzet

    def dodaj_kandydata(self, url, rozsz, zrodlo, rodzic, gl):
        if url in self.kandydaci:
            return
        if not self.robots.wolno(url):          # zabronione przez robots.txt nie trafiają do losowania
            self.pomin("robots", url)
            return
        if url not in self.kandydaci:
            self.kandydaci[url] = {"url": url, "rodzina": common.RODZINY[rozsz], "rozszerzenie": rozsz,
                                   "zrodlo": zrodlo, "strona_rodzic": rodzic, "glebokosc": gl,
                                   "domena": self.domeny[self.indeks_domeny(url)]}

    def przyjmij(self, url, rodzic, gl, zrodlo):
        """Klasyfikuje znaleziony URL: dokument (bez żądania), do sprawdzenia albo strona do odwiedzenia."""
        if not url:
            return
        if url in self.widziane:
            # strona z sitemapy, którą teraz znalazł link: przenosimy ją do kolejki głównej
            if zrodlo != "sitemap" and url in self.w_zapasie:
                self.w_zapasie.discard(url)
                p = urllib.parse.urlsplit(url)
                typ = "sprawdz" if WZORZEC_POBIERANIA.search(p.path + "?" + p.query) else "strona"
                if not (gl > self.a.glebokosc and typ == "strona"):
                    self.kolejki[self.indeks_domeny(url)].append((url, rodzic, gl, typ))
            return
        self.widziane.add(url)
        if common.url_wykluczony(url):
            self.pomin("wykluczona_domena", url); return
        if not self.w_zakresie(url):
            self.pomin("poza_domena", url); return
        rozsz = common.rozszerzenie_z_url(url)
        if rozsz in common.RODZINY:
            self.dodaj_kandydata(url, rozsz, zrodlo, rodzic, gl); return
        if rozsz in POMIJANE_ROZSZ:
            return
        p = urllib.parse.urlsplit(url)
        if petla_segmentow(p.path):
            self.pomin("pulapka", url); return
        typ = "sprawdz" if WZORZEC_POBIERANIA.search(p.path + "?" + p.query) else "strona"
        if gl > self.a.glebokosc and typ == "strona":
            self.pomin("glebokosc", url); return
        # link "do pobrania" sprawdzamy nawet za limitem głębokości (bez dalszego śledzenia jego linków)
        i = self.indeks_domeny(url)
        if zrodlo == "sitemap":
            self.zapasy[i].append((url, rodzic, gl, typ)); self.w_zapasie.add(url)
        else:
            self.kolejki[i].append((url, rodzic, gl, typ))

    def pobierz(self, url, cel):
        """GET z limiterem/logiem. Zwraca (content_type, content_disposition, url_koncowy, treść|None)."""
        if not self.robots.wolno(url):
            self.pomin("robots", url)
            return None
        if not self.budzet_ok():            # robots.txt mógł zużyć ostatnie żądanie
            return None
        try:
            r = self.klient.otworz(url, timeout=30, cel=cel)
        except Exception as e:
            print(f"[!] {url}: {e}", file=sys.stderr)
            return None
        ct = (r.headers.get("Content-Type") or "").lower()
        cd = r.headers.get("Content-Disposition") or ""
        final = r.geturl()
        tresc = None
        try:
            if ct.startswith(("text/html", "application/xhtml")) or cel == "sitemap":
                tresc = r.read(MAX_HTML + 1)
        except Exception as e:
            print(f"[!] {url}: błąd odczytu {e}", file=sys.stderr)
        self.klient.zamknij(r, bajty=len(tresc) if tresc is not None else None)
        return ct, cd, final, tresc

    # ---- sitemapy ----
    def sitemapy(self):
        hosty = []
        for st, _ in self.starty:
            b = f"{urllib.parse.urlsplit(st).scheme}://{urllib.parse.urlsplit(st).netloc}"
            if b not in hosty:
                hosty.append(b)
        do_pobrania = deque()
        for base in hosty:
            rp = self.robots.parser(base)
            for sm in (rp.site_maps() or []):
                do_pobrania.append((sm, 0))
            do_pobrania.append((base + "/sitemap.xml", 0))
        odwiedzone = set()
        while do_pobrania and self.budzet_ok():
            sm, poziom = do_pobrania.popleft()
            sm = normalizuj_url(sm, sm)
            if not sm or sm in odwiedzone or not self.w_zakresie(sm) or common.url_wykluczony(sm) or poziom > 3:
                continue
            odwiedzone.add(sm)
            wynik = self.pobierz(sm, "sitemap")
            if not wynik or wynik[3] is None:
                continue
            self.n_sitemap += 1
            dane = wynik[3]
            if dane[:2] == b"\x1f\x8b":
                try:
                    dane = gzip.decompress(dane)
                except Exception:
                    continue
            try:
                root = ET.fromstring(dane)
            except ET.ParseError:
                continue
            for el in root.iter():
                if el.tag.split("}")[-1] != "loc" or not el.text:
                    continue
                loc = el.text.strip()
                if root.tag.split("}")[-1] == "sitemapindex":
                    do_pobrania.append((loc, poziom + 1))
                else:
                    self.przyjmij(normalizuj_url(loc, sm), sm, 1, "sitemap")

    # ---- crawl ----
    def strona(self, url, rodzic, gl, typ):
        wynik = self.pobierz(url, "strona" if typ == "strona" else "sprawdzenie")
        if not wynik:
            return
        ct, cd, final, tresc = wynik
        if typ == "sprawdz":
            self.n_sprawdzen += 1
        rozsz = common.rozszerzenie_z_odpowiedzi(final, ct, cd)
        if rozsz:
            zrodlo = "heurystyka" if typ == "sprawdz" else "content_type"
            self.dodaj_kandydata(url, rozsz, zrodlo, rodzic, gl)
            return
        if not ct.startswith(("text/html", "application/xhtml")):
            self.odrzuc(ct, typ, url)                    # ani strona, ani dokument z listy
            return
        if tresc is None or len(tresc) > MAX_HTML:
            return
        if final != url and not self.w_zakresie(final):
            return                                       # przekierowanie poza domenę: nie parsujemy
        self.n_stron += 1
        self.stron_wg_domeny[self.indeks_domeny(url)] += 1
        if gl > self.a.glebokosc:
            return                                       # za limitem głębokości: nie śledzimy dalej
        p = Linki()
        try:
            p.feed(tresc.decode("utf-8", errors="replace"))
        except Exception:
            return
        baza = urllib.parse.urljoin(final, p.base) if p.base else final
        if gl == 0 and len(p.hrefy) < 5:
            print(f"[!] strona startowa {url} ma tylko {len(p.hrefy)} linków — możliwa strona generowana JavaScriptem",
                  file=sys.stderr)
            self.js_ostrzezenie = True
        for h in p.hrefy:
            self.przyjmij(normalizuj_url(h, baza), url, gl + 1, "link")

    def nastepny(self):
        """Następna strona: domeny naprzemiennie; w domenie najpierw kolejka główna, potem zapasowa (sitemapy)."""
        n = len(self.domeny)
        for k in range(n):
            i = (self._rr + k) % n
            if self.kolejki[i]:
                self._rr = (i + 1) % n
                return self.kolejki[i].popleft()
            while self.zapasy[i]:
                el = self.zapasy[i].popleft()
                if el[0] in self.w_zapasie:              # pomijamy przeniesione do kolejki głównej
                    self.w_zapasie.discard(el[0])
                    self.n_stron_z_zapasu += 1
                    self._rr = (i + 1) % n
                    return el
        return None

    def zostalo(self):
        return any(self.kolejki) or bool(self.w_zapasie)

    def uruchom(self):
        self.js_ostrzezenie = False
        for st, zak in self.starty:
            if common.url_wykluczony(st):
                sys.exit(f"[x] strona startowa w wykluczonej domenie: {st}")
        print(f"[i] start: {self.start} | zakres: {', '.join(d + ' i subdomeny' for d in self.domeny)} | "
              f"budżet: {self.a.budzet} żądań (wspólny)", file=sys.stderr)
        for i, (st, _) in enumerate(self.starty):
            if st not in self.widziane:
                self.widziane.add(st)
                self.kolejki[i].append((st, "", 0, "strona"))
        self.sitemapy()
        t0 = time.time()
        while True:
            if not self.budzet_ok():
                self.powod = "wyczerpany budżet"; break
            el = self.nastepny()
            if el is None:
                break
            url, rodzic, gl, typ = el
            self.strona(url, rodzic, gl, typ)
            if self.klient.liczba_zadan % 25 == 0:
                print(f"[i] żądań {self.klient.liczba_zadan}/{self.a.budzet}, stron {self.n_stron}, "
                      f"dokumentów {len(self.kandydaci)}, w kolejce {sum(map(len, self.kolejki))} (+{len(self.w_zapasie)} z sitemap)",
                      file=sys.stderr)
        if not self.budzet_ok() and self.zostalo():
            self.powod = "wyczerpany budżet"
        self.zapisz(time.time() - t0)

    # ---- wyniki i losowanie ----
    def zapisz(self, czas_s):
        pola = ["url", "rodzina", "rozszerzenie", "zrodlo", "strona_rodzic", "glebokosc", "domena"]
        kand = sorted(self.kandydaci.values(), key=lambda r: r["url"])
        with open(self.out / "kandydaci.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=pola); w.writeheader(); w.writerows(kand)
        ziarno = self.a.ziarno if self.a.ziarno is not None else random.SystemRandom().randrange(2**31)
        # Losowanie z limitem obrazów (decyzja 2026-10-20): najpierw do --limit-obrazow obrazów,
        # resztę do --limit wypełniają dokumenty; gdy dokumentów brakuje, dobieramy więcej obrazów.
        rng = random.Random(ziarno)
        obrazy = [r for r in kand if r["rodzina"] in RODZINY_OBRAZOW]
        dokumenty = [r for r in kand if r["rodzina"] not in RODZINY_OBRAZOW]
        rng.shuffle(obrazy); rng.shuffle(dokumenty)
        n_obr = min(self.a.limit_obrazow, len(obrazy), self.a.limit)
        n_dok = min(self.a.limit - n_obr, len(dokumenty))
        n_obr = min(len(obrazy), self.a.limit - n_dok)
        wyl = sorted(obrazy[:n_obr] + dokumenty[:n_dok], key=lambda r: r["url"])
        with open(self.out / "urls.txt", "w", encoding="utf-8") as f:
            f.write(f"# wylosowano {len(wyl)} z {len(kand)} (ziarno {ziarno}), instytucja {self.a.instytucja}\n")
            for r in wyl:
                f.write(f"{r['url']},{self.a.instytucja}\n")
        rodziny = {}
        for r in kand:
            rodziny[r["rodzina"]] = rodziny.get(r["rodzina"], 0) + 1
        odrzucone = dict(sorted(self.odrzucone.items(), key=lambda x: -(x[1]["sprawdzenie"] + x[1]["strona"])))
        # rozbicie na domeny (żądania liczone z logu: obejmują robots.txt, sitemapy i kroki przekierowań)
        zad_dom = [0 for _ in self.domeny]
        try:
            with open(self.log.sciezka, encoding="utf-8") as f:
                for l in f:
                    i = self.indeks_domeny(json.loads(l)["url"])
                    if i is not None:
                        zad_dom[i] += 1
        except (OSError, ValueError, KeyError):
            pass
        wg_domeny = {d: {"start": self.starty[i][0], "zadan": zad_dom[i], "stron_html": self.stron_wg_domeny[i],
                         "kandydatow": sum(1 for r in kand if r["domena"] == d),
                         "wylosowano": sum(1 for r in wyl if r["domena"] == d),
                         "w_kolejce_niezbadane": len(self.kolejki[i]),
                         "w_kolejce_zapasowej_niezbadane": sum(1 for el in self.zapasy[i] if el[0] in self.w_zapasie)}
                     for i, d in enumerate(self.domeny)}
        pods = {
            "instytucja": self.a.instytucja, "start": self.start, "zakres_domena": self.domena,
            "zakres_domeny": self.domeny, "wg_domeny": wg_domeny,
            "zakonczono_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "czas_crawla_s": round(czas_s),
            "powod_zakonczenia": self.powod, "budzet": self.a.budzet, "zadan": self.klient.liczba_zadan,
            "glebokosc_max": self.a.glebokosc, "stron_html": self.n_stron, "sitemap_pobranych": self.n_sitemap,
            "sprawdzen_heurystyka": self.n_sprawdzen, "w_kolejce_niezbadane": sum(map(len, self.kolejki)),
            "w_kolejce_zapasowej_niezbadane": len(self.w_zapasie), "stron_z_kolejki_zapasowej": self.n_stron_z_zapasu,
            "pominiete": self.pominiete, "przekierowania": self.klient.przekierowania,
            "pominiete_najczesciej": {k: dict(sorted(v.items(), key=lambda x: -x[1])[:10])
                                      for k, v in self.pominiete_gdzie.items() if v},
            "odrzucone_typy": odrzucone,
            "kandydatow": len(kand), "kandydaci_wg_rodziny": rodziny,
            "zrodla": {z: sum(1 for r in kand if r["zrodlo"] == z) for z in ("sitemap", "link", "heurystyka", "content_type")},
            "limit": self.a.limit, "limit_obrazow": self.a.limit_obrazow, "wylosowano": len(wyl),
            "wylosowano_dokumentow": n_dok, "wylosowano_obrazow": n_obr,
            "kandydatow_dokumentow": len(dokumenty), "kandydatow_obrazow": len(obrazy), "ziarno": ziarno,
            "ostrzezenie_js": self.js_ostrzezenie, "user_agent": self.a.ua,
        }
        (self.out / "podsumowanie.json").write_text(json.dumps(pods, ensure_ascii=False, indent=2), encoding="utf-8")
        if len(self.domeny) > 1:
            for d, v in wg_domeny.items():
                print(f"[i] {d}: żądań {v['zadan']}, stron {v['stron_html']}, kandydatów {v['kandydatow']}, "
                      f"wylosowano {v['wylosowano']}", file=sys.stderr)
        print(f"[+] {self.powod}: {self.klient.liczba_zadan} żądań, {self.n_stron} stron, "
              f"{len(kand)} dokumentów ({rodziny}); wylosowano {len(wyl)} "
              f"({n_dok} dok. + {n_obr} obr.) -> {self.out / 'urls.txt'}",
              file=sys.stderr)
        for ct, d in odrzucone.items():
            n = d["sprawdzenie"] + d["strona"]
            if n >= PROG_OSTRZEZENIA:
                print(f"[!] odrzucono {n} odpowiedzi typu {ct} (sprawdzenia: {d['sprawdzenie']}, "
                      f"strony: {d['strona']}) — sprawdź przykłady w podsumowanie.json (odrzucone_typy)",
                      file=sys.stderr)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--start", required=True, help="adres strony głównej instytucji")
    p.add_argument("--instytucja", required=True, help="identyfikator instytucji (np. GMINA-01)")
    p.add_argument("--out", required=True, help="katalog wyników tej instytucji")
    p.add_argument("--domena", help="zakres crawla (domyślnie host startowy bez www.), np. uni-x.edu.pl")
    p.add_argument("--dodatkowa-domena", action="append", metavar="ADRES",
                   help="dodatkowa domena instytucji (adres startowy; zakres = jej host i subdomeny); można powtarzać")
    p.add_argument("--budzet", type=int, default=1500, help="maks. liczba żądań, wspólna dla wszystkich domen (domyślnie 1500)")
    p.add_argument("--glebokosc", type=int, default=5, help="maks. głębokość linków od strony startowej (domyślnie 5)")
    p.add_argument("--limit", type=int, default=100, help="ile plików wylosować (domyślnie 100)")
    p.add_argument("--limit-obrazow", type=int, default=20,
                   help="maks. obrazów (png/jpg/tiff) w próbie; resztę wypełniają dokumenty (domyślnie 20)")
    p.add_argument("--ziarno", type=int, help="ziarno losowania (domyślnie losowe, zapisywane w podsumowaniu)")
    p.add_argument("--delay", type=float, default=common.MIN_ODSTEP_S, help="odstęp per host (min. 5 s poza localhost)")
    p.add_argument("--ua", default=common.USER_AGENT, help="User-Agent (domyślnie z common.py)")
    a = p.parse_args()
    if not common.ua_skonfigurowany(a.ua) and \
            common.normalizuj_host(urllib.parse.urlsplit(a.start if "://" in a.start else "https://" + a.start).netloc) \
            not in common.HOSTY_LOKALNE:
        sys.exit("[x] User-Agent nie jest skonfigurowany (PROJEKT/REPO_URL w common.py).")
    Crawler(a).uruchom()


if __name__ == "__main__":
    main()
