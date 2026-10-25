#!/usr/bin/env python3
"""
fetch.py — etap POBIERANIA: pobiera pliki z listy URL-i do katalogu, tworzy manifest plik->URL.

Logika jak w `meta2csv.py fetch` (v5), z trzema zmianami z modułu common:
  1. domeny gov.pl i PWr są odrzucane przed jakimkolwiek żądaniem (status: wykluczona_domena),
  2. odstęp min. 5 s liczony per host i obejmujący także żądanie o robots.txt,
  3. każde żądanie HTTP trafia do logu JSONL (domyślnie <out>_zadania.jsonl, obok katalogu plików).
Dodatkowo: częściowo pobrany plik jest usuwany, gdy transfer przerwie błąd.
Przekierowania (UA 1.3): każdy krok sprawdzany (wykluczenia, robots.txt celu, odstęp); z --domena także
zakres (host = jedna z domen albo jej subdomena; --domena można powtarzać) — zalecane: zakres_domeny
z podsumowanie.json discover.

Przykład:
  python3 fetch.py --urls urls.txt --out dane/inst01/pliki [--domena gmina-x.pl]
  (User-Agent z common.py; --ua tylko do testów)
"""
import argparse, csv, hashlib, re, sys, time, urllib.parse
from pathlib import Path

import common

MAX_ROZMIAR = 200 * 2**20     # pliki większe niż 200 MB są pomijane


def cmd_fetch(a):
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    urls = [l.strip() for l in Path(a.urls).read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.lstrip().startswith("#")]

    if not common.ua_skonfigurowany(a.ua):
        zewn = [u for u in urls if common.normalizuj_host(urllib.parse.urlsplit(u.split(",")[0].strip()).netloc)
                not in common.HOSTY_LOKALNE]
        if zewn:
            sys.exit("[x] User-Agent nie jest skonfigurowany (PROJEKT/REPO_URL w common.py). "
                     "Bez tego nie pobieram z hostów zewnętrznych.")

    zakres = None
    if a.domena:
        domeny = [common.normalizuj_host(d) for d in a.domena]

        def zakres(u):
            h = common.normalizuj_host(urllib.parse.urlsplit(u).netloc)
            return any(h == d or h.endswith("." + d) for d in domeny)

    # log poza katalogiem plików — inaczej extract potraktowałby go jak dokument
    log = common.LogZadan(a.log or out.with_name(out.name + "_zadania.jsonl"))
    klient = common.Klient(a.ua, log, common.Limiter(a.delay), etap="fetch", zakres=zakres)
    robots = common.Robots(klient)

    man_path = out / "manifest.csv"
    new = not man_path.exists()
    # numeracja kontynuowana między partiami (inaczej nazwy plików powtarzałyby się w wynikach)
    offset = 0 if new else sum(1 for _ in open(man_path, encoding="utf-8")) - 1
    with open(man_path, "a", newline="", encoding="utf-8") as mf:
        w = csv.writer(mf)
        if new:
            w.writerow(["plik", "url", "instytucja", "pobrano_utc", "status",
                        "content_type", "bajty", "sha256", "rozszerzenie"])

        def wiersz(plik, url, inst, ts, status, ct="", bajty="", sha="", rozsz=""):
            w.writerow([plik, url, inst, ts, status, ct, bajty, sha, rozsz]); mf.flush()
        for i, line in enumerate(urls):
            parts = [p.strip() for p in line.split(",", 1)]
            url, inst = parts[0], (parts[1] if len(parts) > 1 else "")
            pu = urllib.parse.urlsplit(url)
            # Link przekierowujący z wyników Google (google.*/url?q=... lub ?url=...) -> adres docelowy
            if re.match(r"(www\.)?google\.[a-z.]+$", pu.netloc) and pu.path == "/url":
                qs = urllib.parse.parse_qs(pu.query)
                target = (qs.get("q") or qs.get("url") or [""])[0]
                if target.startswith("http"):
                    print(f"[i] link z Google zamieniony na docelowy: {target}", file=sys.stderr)
                    url = target; pu = urllib.parse.urlsplit(url)
            ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            if common.url_wykluczony(url):
                print(f"[-] wykluczona domena: {url}", file=sys.stderr)
                wiersz("", url, inst, ts, "wykluczona_domena"); continue
            if zakres and not zakres(url):
                print(f"[-] poza zakresem {', '.join(a.domena)}: {url}", file=sys.stderr)
                wiersz("", url, inst, ts, "poza_zakresem"); continue
            if not robots.wolno(url):
                print(f"[-] robots.txt zabrania: {url}", file=sys.stderr)
                wiersz("", url, inst, ts, "robots_disallow"); continue
            name = Path(urllib.parse.unquote(pu.path)).name or "index"
            name = re.sub(r"[^\w.\-]", "_", name)[:120]
            dest = out / f"{offset + i:04d}_{name}"
            try:
                r = klient.otworz(url, timeout=60, cel="dokument")
                ctype = (r.headers.get("Content-Type") or "").lower()
                final = r.geturl()
                if ctype.startswith(("text/html", "application/xhtml")):
                    # strona WWW (np. strona pobierania, logowanie, przekierowanie), a nie dokument
                    klient.zamknij(r, uwagi="html_nie_dokument")
                    wiersz("", url, inst, ts, f"html_nie_dokument ({final})", ctype)
                    print(f"[!] {url}: serwer zwrócił stronę HTML, nie plik", file=sys.stderr)
                    continue
                rozsz = common.rozszerzenie_z_odpowiedzi(final, ctype, r.headers.get("Content-Disposition") or "")
                if not rozsz:
                    klient.zamknij(r, uwagi="typ_spoza_listy")
                    wiersz("", url, inst, ts, "typ_spoza_listy", ctype)
                    print(f"[-] {url}: typ spoza listy formatów ({ctype})", file=sys.stderr)
                    continue
                if not dest.name.lower().endswith("." + rozsz):
                    dest = dest.with_name(dest.name[:120] + "." + rozsz)   # np. pobierz.php?id=1 -> ..._pobierz.php.pdf
                h = hashlib.sha256(); n = 0
                try:
                    with open(dest, "wb") as f:
                        while True:
                            blok = r.read(1 << 16)
                            if not blok:
                                break
                            n += len(blok)
                            if n > MAX_ROZMIAR:
                                raise ValueError(f"plik większy niż {MAX_ROZMIAR // 2**20} MB")
                            h.update(blok); f.write(blok)
                except Exception as e:
                    dest.unlink(missing_ok=True)
                    klient.zamknij(r, uwagi=f"przerwany transfer: {e}")
                    raise
                klient.zamknij(r, bajty=n)
                wiersz(dest.name, url, inst, ts, "ok", ctype, n, h.hexdigest(), rozsz)
                print(f"[+] {url} -> {dest.name}", file=sys.stderr)
            except Exception as e:
                wiersz("", url, inst, ts, f"error: {e}")
                print(f"[!] {url}: {e}", file=sys.stderr)
    p = klient.przekierowania
    if any(p.values()):
        print(f"[i] przekierowania: {p}", file=sys.stderr)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--urls", required=True, help="plik tekstowy: 'URL' lub 'URL,instytucja' w linii; # = komentarz")
    p.add_argument("--out", required=True)
    p.add_argument("--domena", action="append",
                   help="zakres instytucji (domena + subdomeny; można powtarzać); przekierowania poza nim są blokowane")
    p.add_argument("--delay", type=float, default=common.MIN_ODSTEP_S,
                   help=f"sekundy między żądaniami do jednego hosta (min. {common.MIN_ODSTEP_S} poza hostami lokalnymi)")
    p.add_argument("--ua", default=common.USER_AGENT, help="User-Agent (domyślnie z common.py; zmiana tylko do testów)")
    p.add_argument("--log", help="log żądań JSONL (domyślnie <out>_zadania.jsonl; nie umieszczaj go w <out>)")
    a = p.parse_args()
    cmd_fetch(a)


if __name__ == "__main__":
    main()
