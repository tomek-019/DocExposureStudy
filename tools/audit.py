#!/usr/bin/env python3
"""
audit.py — sprawdza logi żądań (discover + fetch) pod kątem zasad pozyskiwania (zasada 1, dziennik).

  python3 tools/audit.py dane/pilot/GMINA-01 [dane/pilot/UCZELNIA-01 ...]
  python3 tools/audit.py dane/test/*/ --robots-online     # dla starych logów bez migawek robots.txt

W każdym katalogu instytucji czyta wszystkie *zadania*.jsonl (discover i fetch) oraz robots.jsonl.
Sprawdza:
  1. User-Agent każdego żądania = UA badania z common.py,
  2. brak żądań do wykluczonych domen (gov.pl, PWr), także po przekierowaniu,
  3. odstęp między żądaniami do jednego hosta >= 5 s (koniec poprzedniego -> początek następnego),
  4. brak żądań do ścieżek zabronionych w robots.txt (na zapisanej migawce robots.txt z czasu crawla),
  5. sygnały przeciążenia serwera (HTTP 429/503) — do wiadomości,
  6. przekierowania na inny host (do wiadomości): od UA 1.3 każdy krok jest osobnym wpisem w logu (sprawdzanym
     w pkt 1–4), zablokowane kroki mają pole "uwagi"; w logach sprzed 1.3 (pole url_koncowy) robots.txt hosta
     docelowego nie był sprawdzany — raport podaje ich liczbę i hosty docelowe.
Kod wyjścia 0 = wszystko OK, 1 = co najmniej jedno naruszenie.
"""
import argparse, collections, json, sys, urllib.parse
from pathlib import Path

import common

TOLERANCJA_S = 0.05      # zaokrąglenia znaczników czasu w logu


def wczytaj_jsonl(p):
    with open(p, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def parsery_robots(katalog: Path, ua: str, online: bool):
    """{base: ZasadyRobots} z migawek (dopasowanie RFC 9309); przy --robots-online brakujące pobiera teraz."""
    wynik = {}
    p = katalog / "robots.jsonl"
    if p.exists():
        for z in wczytaj_jsonl(p):
            rp = common.ZasadyRobots(z.get("tresc") or "", tryb=z["tryb"])
            wynik.setdefault(z["base"], rp)          # pierwsza migawka = ta, według której działał crawler
    if online:
        log = common.LogZadan(katalog / "zadania_audit.jsonl")
        robots = common.Robots(common.Klient(ua, log, common.Limiter(), etap="audit"))
        return wynik, robots
    return wynik, None


def audytuj(katalog: Path, ua: str, online: bool):
    pliki = sorted(katalog.glob("*zadania*.jsonl"))
    pliki = [p for p in pliki if p.name != "zadania_audit.jsonl"]
    # log fetch leży obok katalogu plików: <KOD>/pliki_zadania.jsonl — jest w tym samym katalogu instytucji
    zad = []
    for p in pliki:
        for e in wczytaj_jsonl(p):
            e["_plik"] = p.name
            zad.append(e)
    if not zad:
        return {"katalog": str(katalog), "blad": "brak logów *zadania*.jsonl"}, False
    zad.sort(key=lambda e: e["t_epoch"] - (e.get("czas_ms") or 0) / 1000)

    naruszenia = collections.defaultdict(list)
    # 1. UA
    for e in zad:
        if e.get("ua") != ua:
            naruszenia["ua"].append(e["url"])
    # 2. domeny wykluczone
    for e in zad:
        if common.url_wykluczony(e["url"]) or (e.get("url_koncowy") and common.url_wykluczony(e["url_koncowy"])):
            naruszenia["domena_wykluczona"].append(e.get("url_koncowy") or e["url"])
    # 3. odstępy per host
    koniec = {}
    min_odstep = {}
    for e in zad:
        h = e["host"]
        start = e["t_epoch"] - (e.get("czas_ms") or 0) / 1000
        if h in koniec:
            odst = start - koniec[h]
            min_odstep[h] = min(min_odstep.get(h, 1e9), odst)
            if odst < common.MIN_ODSTEP_S - TOLERANCJA_S and h not in common.HOSTY_LOKALNE:
                naruszenia["odstep"].append(f"{h}: {odst:.2f} s przed {e['url']}")
        koniec[h] = e["t_epoch"]
    # 4. robots.txt
    migawki, robots_online = parsery_robots(katalog, ua, online)
    bez_migawki = set()
    for e in zad:
        if e.get("cel") == "robots":
            continue
        pu = urllib.parse.urlsplit(e["url"])
        base = f"{pu.scheme}://{pu.netloc}"
        rp = migawki.get(base)
        if rp is None:
            if robots_online:
                if not robots_online.wolno(e["url"]):
                    naruszenia["robots"].append(e["url"] + " (wg robots.txt pobranego teraz)")
            else:
                bez_migawki.add(base)
            continue
        if not rp.can_fetch(ua, e["url"]):
            naruszenia["robots"].append(e["url"])
    # 5. sygnały przeciążenia
    przeciazenie = [e["url"] for e in zad if e.get("status") in (429, 503)]
    # 6. przekierowania na inny host
    przek = collections.Counter()
    stare_cele = collections.Counter()
    for e in zad:
        if e.get("przekierowanie_do"):
            przek["zablokowane: " + e["uwagi"] if e.get("uwagi") else "wykonane (krok sprawdzony)"] += 1
        elif e.get("url_koncowy") and e.get("cel") != "robots":   # robots.txt wolno śledzić (RFC 9309)
            a = common.normalizuj_host(urllib.parse.urlsplit(e["url"]).netloc)
            b = common.normalizuj_host(urllib.parse.urlsplit(e["url_koncowy"]).netloc)
            if a != b:
                rodzaj = "www/bez www" if a.removeprefix("www.") == b.removeprefix("www.") else "inny host"
                przek[f"przed 1.3, {rodzaj} (robots celu niesprawdzany)"] += 1
                if rodzaj == "inny host":
                    stare_cele[b] += 1

    rap = {
        "katalog": str(katalog),
        "logi": [p.name for p in pliki],
        "zadan": len(zad),
        "wg_etapu": dict(collections.Counter(e.get("etap") for e in zad)),
        "wg_statusu": dict(collections.Counter(str(e.get("status")) for e in zad)),
        "hosty": dict(collections.Counter(e["host"] for e in zad)),
        "min_odstep_s": {h: round(v, 2) for h, v in min_odstep.items()},
        "naruszenia": {k: len(v) for k, v in naruszenia.items()},
        "przyklady_naruszen": {k: v[:5] for k, v in naruszenia.items()},
        "http_429_503": len(przeciazenie),
        "robots_niezweryfikowane_hosty": sorted(bez_migawki),
        "przekierowania": dict(przek),
        "przekierowania_przed_1_3_cele": dict(stare_cele.most_common(10)),
    }
    ok = not naruszenia and not bez_migawki
    return rap, ok


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("katalogi", nargs="+", help="katalogi instytucji (z zadania_discover.jsonl / pliki_zadania.jsonl)")
    p.add_argument("--ua", default=common.USER_AGENT)
    p.add_argument("--robots-online", action="store_true",
                   help="gdy brak migawki robots.txt: pobierz robots.txt teraz (1 żądanie na host, z UA i limitem)")
    p.add_argument("--json", help="zapisz pełny raport do pliku JSON")
    a = p.parse_args()
    raporty, wszystko_ok = [], True
    for k in a.katalogi:
        rap, ok = audytuj(Path(k), a.ua, a.robots_online)
        raporty.append(rap); wszystko_ok &= ok
        print(f"\n== {k}  [{'OK' if ok else 'PROBLEM'}]")
        if "blad" in rap:
            print("  " + rap["blad"]); continue
        print(f"  żądań: {rap['zadan']} {rap['wg_etapu']} | statusy: {rap['wg_statusu']} | 429/503: {rap['http_429_503']}")
        print(f"  min odstęp per host: {rap['min_odstep_s']}")
        print(f"  naruszenia: {rap['naruszenia'] or 'brak'}")
        for kat, przyk in rap["przyklady_naruszen"].items():
            for x in przyk:
                print(f"    [{kat}] {x}")
        if rap["przekierowania"]:
            print(f"  przekierowania: {rap['przekierowania']}")
            if rap["przekierowania_przed_1_3_cele"]:
                print(f"    hosty docelowe (przed 1.3): {rap['przekierowania_przed_1_3_cele']}")
        if rap["robots_niezweryfikowane_hosty"]:
            print(f"  ! brak migawki robots.txt dla: {rap['robots_niezweryfikowane_hosty']} "
                  f"(stare logi? użyj --robots-online)")
    if a.json:
        Path(a.json).write_text(json.dumps(raporty, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n# WYNIK: {'wszystko zgodne z zasadami' if wszystko_ok else 'są problemy — patrz wyżej'}")
    sys.exit(0 if wszystko_ok else 1)


if __name__ == "__main__":
    main()
