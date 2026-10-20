#!/usr/bin/env python3
"""
przeglad.py — szybki przegląd wyników meta2csv (format długi).

  python3 przeglad.py pilot/wyniki_long.csv            # podsumowanie + ciekawe pola per plik
  python3 przeglad.py pilot/wyniki_long.csv --all      # wszystkie niepuste tagi per plik

Wynik (bez --all) jest zwięzły i nadaje się do wklejenia do rozmowy.
"""
import csv, re, sys
from collections import Counter, defaultdict

# Tagi potencjalnie istotne dla ekspozycji (dopasowanie po nazwie tagu, bez grupy)
INTERESTING = re.compile(
    r"^(Author|Creator|LastModifiedBy|LastSavedBy|Manager|Company|Contributor|Copyright|Rights|"
    r"Producer|CreatorTool|Application|AppVersion|Software|Template|HyperlinkBase|"
    r"Title|Subject|Description|Keywords|Comments?|Category|"
    r"CreateDate|ModifyDate|MetadataDate|TotalEditTime|RevisionNumber|"
    r"DocumentID|InstanceID|OriginalDocumentID|DerivedFrom.*|History.*|"
    r"Warning|Error|Linearized|Encryption|PDFVersion)$", re.I)

SKIP_GROUPS = {"System", "File"}

def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    path, show_all = sys.argv[1], "--all" in sys.argv
    rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
    files = defaultdict(list)
    ftype, finst = {}, {}
    for r in rows:
        files[r["plik"]].append(r)
        finst[r["plik"]] = r["instytucja"]
        if r["tag"] == "FileType":
            ftype[r["plik"]] = r["wartosc"]

    n = len(files)
    print(f"# Plików: {n}")
    print("# Typy: " + ", ".join(f"{k}={v}" for k, v in Counter(ftype.values()).most_common()))
    print("# Instytucje: " + ", ".join(f"{k}={v}" for k, v in Counter(finst.values()).most_common()))

    # W ilu plikach dany tag jest niepusty
    tag_files = defaultdict(set)
    for f, rs in files.items():
        for r in rs:
            if r["wartosc"].strip() and r["grupa"] not in SKIP_GROUPS:
                tag_files[r["tag"]].add(f)
    print("\n## Najczęstsze niepuste tagi (tag: w ilu plikach)")
    for tag, fs in sorted(tag_files.items(), key=lambda x: (-len(x[1]), x[0]))[:40]:
        print(f"  {tag}: {len(fs)}/{n}")

    pseud = Counter(r["tag"] for r in rows if r.get("pseudonimizowane") == "1")
    print("\n## Pola z danymi osobowymi (pseudonimizowane): " +
          (", ".join(f"{k}={v}" for k, v in pseud.most_common()) or "brak"))

    print("\n## Per plik" + (" (wszystkie tagi)" if show_all else " (wybrane pola)"))
    for f in sorted(files):
        print(f"\n--- {f}  [{finst[f]}, {ftype.get(f, '?')}]")
        seen = set()
        for r in files[f]:
            v = r["wartosc"].strip()
            if not v or r["grupa"] in SKIP_GROUPS:
                continue
            if not show_all and not INTERESTING.match(r["tag"]):
                continue
            key = (r["tag"], v)
            if key in seen:            # ten sam tag z różnych grup (XMP/PDF) z tą samą wartością
                continue
            seen.add(key)
            print(f"  {r['grupa']}:{r['tag']} = {v[:150]}")

if __name__ == "__main__":
    main()
