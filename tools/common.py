"""
common.py — wspólne elementy etapów sieciowych (fetch, w bloku B także discover).

Zawiera:
  - konfigurację tożsamości crawlera (User-Agent) i minimalnego odstępu,
  - filtr wykluczonych domen (gov.pl, PWr),
  - limiter tempa per host (obejmuje także żądania o robots.txt),
  - pamięć podręczną robots.txt pobieranego jawnym User-Agentem,
  - log każdego żądania HTTP w formacie JSONL (podstawa audytu z kamienia milowego).
"""
import json, sys, time, urllib.error, urllib.parse, urllib.request, urllib.robotparser
from pathlib import Path

# ---------- konfiguracja (decyzja 2026-10-20) ----------
PROJEKT = "DocExposureStudy"                                 # nazwa projektu w User-Agencie
WERSJA = "1.0"
REPO_URL = "https://github.com/tomek-019/DocExposureStudy"   # publiczne repozytorium z README badania
USER_AGENT = f"{PROJEKT}/{WERSJA} (+{REPO_URL})"

MIN_ODSTEP_S = 5.0                                           # min. odstęp między żądaniami do jednego hosta
# Hosty lokalne (testy na własnym serwerze) mogą mieć mniejszy odstęp; nigdy hosty zewnętrzne.
HOSTY_LOKALNE = {"127.0.0.1", "localhost", "::1"}

# Wykluczone domeny: host równy sufiksowi albo kończący się ".<sufiks>".
WYKLUCZONE_SUFIKSY = ("gov.pl", "pwr.edu.pl", "pwr.wroc.pl")

# ---------- formaty (decyzja 2026-10-18: lista TalTech + zip, rtf, warianty z makrami i szablony) ----------
# rozszerzenie -> rodzina
RODZINY = {
    "docx": "docx", "doc": "docx", "docm": "docx", "dotx": "docx", "dot": "docx",
    "xlsx": "xlsx", "xls": "xlsx", "xlsm": "xlsx",
    "pptx": "pptx", "ppt": "pptx", "pptm": "pptx", "pps": "pptx", "ppsx": "pptx",
    "odt": "odt", "ods": "ods", "odp": "odp",
    "pdf": "pdf", "rtf": "rtf",
    "png": "png", "jpg": "jpg", "jpeg": "jpg", "tiff": "tiff", "tif": "tiff",
    "zip": "zip",
}
# Content-Type -> rozszerzenie (typy standardowe; dla typów ogólnych decyduje rozszerzenie z URL/nagłówka)
MIME = {
    "application/pdf": "pdf",
    "application/msword": "doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.ms-word.document.macroenabled.12": "docm",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.template": "dotx",
    "application/vnd.ms-excel": "xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.ms-excel.sheet.macroenabled.12": "xlsm",
    "application/vnd.ms-powerpoint": "ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/vnd.ms-powerpoint.presentation.macroenabled.12": "pptm",
    "application/vnd.openxmlformats-officedocument.presentationml.slideshow": "ppsx",
    "application/vnd.oasis.opendocument.text": "odt",
    "application/vnd.oasis.opendocument.spreadsheet": "ods",
    "application/vnd.oasis.opendocument.presentation": "odp",
    "application/rtf": "rtf", "text/rtf": "rtf",
    "image/png": "png", "image/jpeg": "jpg", "image/tiff": "tiff",
    "application/zip": "zip", "application/x-zip-compressed": "zip",
}
MIME_OGOLNE = {"application/octet-stream", "binary/octet-stream", "application/force-download",
               "application/download", "application/x-download"}


def rozszerzenie_z_url(url: str) -> str:
    sciezka = urllib.parse.unquote(urllib.parse.urlsplit(url).path)
    nazwa = sciezka.rsplit("/", 1)[-1]
    return nazwa.rsplit(".", 1)[-1].lower() if "." in nazwa else ""


def rozszerzenie_z_odpowiedzi(url: str, content_type: str, content_disposition: str = "") -> str:
    """Rozszerzenie z listy formatów albo '' (to nie dokument z listy)."""
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in MIME:
        return MIME[ct]
    if ct in MIME_OGOLNE or not ct:
        import re
        m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', content_disposition or "", re.I)
        kand = urllib.parse.unquote(m.group(1)).rsplit(".", 1)[-1].lower() if m and "." in m.group(1) else ""
        if kand in RODZINY:
            return kand
        e = rozszerzenie_z_url(url)
        return e if e in RODZINY else ""
    return ""


def ua_skonfigurowany(ua: str) -> bool:
    return "NAZWA-DO-USTALENIA" not in ua and "KONTO/REPO" not in ua


def normalizuj_host(host: str) -> str:
    h = (host or "").strip().lower().rstrip(".")
    if h.startswith("[") and "]" in h:            # IPv6 w nawiasach
        return h[1:h.index("]")]
    if h.count(":") == 1:                          # host:port
        h = h.split(":", 1)[0]
    try:
        h = h.encode("idna").decode("ascii")       # IDN -> punycode
    except UnicodeError:
        pass
    return h


def host_wykluczony(host: str) -> bool:
    h = normalizuj_host(host)
    return any(h == s or h.endswith("." + s) for s in WYKLUCZONE_SUFIKSY)


def url_wykluczony(url: str) -> bool:
    return host_wykluczony(urllib.parse.urlsplit(url).netloc)


# ---------- log żądań ----------

class LogZadan:
    """Dopisuje jeden wiersz JSON na każde żądanie HTTP (także nieudane i robots.txt)."""

    def __init__(self, sciezka):
        self.sciezka = Path(sciezka)
        self.sciezka.parent.mkdir(parents=True, exist_ok=True)

    def zapisz(self, **pola):
        pola.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z")
        pola.setdefault("t_epoch", round(time.time(), 3))
        with open(self.sciezka, "a", encoding="utf-8") as f:
            f.write(json.dumps(pola, ensure_ascii=False) + "\n")


# ---------- limiter tempa ----------

class Limiter:
    """Pilnuje min. odstępu między KOŃCEM poprzedniego a POCZĄTKIEM następnego żądania do tego samego hosta."""

    def __init__(self, odstep_s: float = MIN_ODSTEP_S):
        self.odstep_s = odstep_s
        self._ostatnie = {}

    def odstep_dla(self, host: str) -> float:
        if normalizuj_host(host) in HOSTY_LOKALNE:
            return self.odstep_s
        return max(self.odstep_s, MIN_ODSTEP_S)

    def czekaj(self, host: str):
        h = normalizuj_host(host)
        last = self._ostatnie.get(h)
        if last is not None:
            reszta = self.odstep_dla(h) - (time.monotonic() - last)
            if reszta > 0:
                time.sleep(reszta)

    def oznacz(self, host: str):
        self._ostatnie[normalizuj_host(host)] = time.monotonic()


# ---------- klient HTTP z limiterem i logiem ----------

class Klient:
    def __init__(self, ua: str, log: LogZadan, limiter: Limiter, etap: str):
        self.ua, self.log, self.limiter, self.etap = ua, log, limiter, etap
        self.liczba_zadan = 0                      # do budżetu crawla

    def otworz(self, url: str, timeout: float, cel: str):
        """Zwraca otwartą odpowiedź (context manager). Wywołujący MUSI wywołać zamknij() po odczycie."""
        host = urllib.parse.urlsplit(url).netloc
        if host_wykluczony(host):
            raise PermissionError(f"wykluczona domena: {host}")
        self.limiter.czekaj(host)
        self.liczba_zadan += 1
        t0 = time.monotonic()
        req = urllib.request.Request(url, headers={"User-Agent": self.ua})
        try:
            r = urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            self.limiter.oznacz(host)
            self.log.zapisz(etap=self.etap, cel=cel, host=normalizuj_host(host), url=url, metoda="GET",
                            status=e.code, ua=self.ua, czas_ms=int((time.monotonic() - t0) * 1000))
            raise
        except Exception as e:
            self.limiter.oznacz(host)
            self.log.zapisz(etap=self.etap, cel=cel, host=normalizuj_host(host), url=url, metoda="GET",
                            status=None, blad=str(e), ua=self.ua, czas_ms=int((time.monotonic() - t0) * 1000))
            raise
        r._mls = (host, url, cel, t0)
        return r

    def zamknij(self, r, bajty=None, uwagi=None):
        host, url, cel, t0 = r._mls
        final = r.geturl()
        r.close()
        self.limiter.oznacz(host)
        pola = dict(etap=self.etap, cel=cel, host=normalizuj_host(host), url=url, metoda="GET",
                    status=r.status, ua=self.ua, czas_ms=int((time.monotonic() - t0) * 1000),
                    content_type=r.headers.get("Content-Type"), bajty=bajty)
        if final != url:
            pola["url_koncowy"] = final
            if host_wykluczony(urllib.parse.urlsplit(final).netloc):
                pola["uwagi"] = "przekierowanie do wykluczonej domeny"
        if uwagi:
            pola["uwagi"] = uwagi
        self.log.zapisz(**pola)


# ---------- robots.txt ----------

class Robots:
    """robots.txt pobierany NASZYM User-Agentem przez Klienta (limiter + log).
    Semantyka jak w v5 / urllib.robotparser: 401/403 => wszystko zabronione, inne 4xx => wszystko dozwolone,
    5xx lub błąd sieci => wszystko zabronione (ostrożnie)."""

    def __init__(self, klient: Klient):
        self.klient = klient
        self._cache = {}

    def parser(self, base: str) -> urllib.robotparser.RobotFileParser:
        if base in self._cache:
            return self._cache[base]
        rp = urllib.robotparser.RobotFileParser(base + "/robots.txt")
        try:
            r = self.klient.otworz(base + "/robots.txt", timeout=30, cel="robots")
            try:
                tresc = r.read()
            except Exception:
                self.klient.zamknij(r, uwagi="błąd odczytu treści")
                raise
            self.klient.zamknij(r, bajty=len(tresc))
            rp.parse(tresc.decode("utf-8", errors="replace").splitlines())
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                rp.disallow_all = True
            elif 400 <= e.code < 500:
                rp.allow_all = True
            print(f"[!] robots.txt {base}: HTTP {e.code}", file=sys.stderr)
        except Exception as e:
            rp.disallow_all = True
            print(f"[!] robots.txt niedostępny dla {base}: {e} (nie pobieram z tego hosta)", file=sys.stderr)
        self._cache[base] = rp
        return rp

    def wolno(self, url: str) -> bool:
        pu = urllib.parse.urlsplit(url)
        return self.parser(f"{pu.scheme}://{pu.netloc}").can_fetch(self.klient.ua, url)
