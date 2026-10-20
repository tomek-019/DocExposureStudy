#!/usr/bin/env python3
"""
extract.py — etap EKSTRAKCJI: exiftool -> CSV (szeroki i opcjonalnie długi) z pseudonimizacją,
potem usunięcie surowych plików (zasada 2). Logika bez zmian względem `meta2csv.py extract` (v5).

Przykład:
  python3 extract.py --in dane/inst01/pliki --manifest dane/inst01/pliki/manifest.csv \
                     --out dane/wyniki.csv --long dane/wyniki_long.csv
"""
import argparse, csv, hashlib, hmac, json, os, re, secrets, shutil, subprocess, sys
from pathlib import Path

# ---------- pseudonimizacja (zasady danych osobowych v1, pkt 3) ----------

# Domyślna ścieżka klucza jak w v5 — zmiana ścieżki = nowy klucz = inne pseudonimy niż w pilocie.
DOMYSLNY_KLUCZ = "~/.config/meta2csv/key.hex"

# Tagi (bez grupy), których wartość w całości traktujemy jako potencjalne imię/nazwisko/login.
PERSON_TAGS = re.compile(
    r"^(Author|Authors|Creator|LastModifiedBy|LastSavedBy|Manager|Contributor|Artist|"
    r"OwnerName|Owner|By-line|Writer|Copyright|Rights|Editor|Operator|Company-?User)$",
    re.I,
)
# Wyjątki: pola o tej nazwie, które w danej grupie zawierają nazwę aplikacji, nie osobę.
NOT_PERSON = {"PDF:Creator"}  # w słowniku Info PDF /Creator = aplikacja źródłowa

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
    return "" if v is None else str(v)


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
            for j, info in enumerate(infos):
                inner = member_name(info)
                safe = re.sub(r"[^\w.\-]", "_", Path(inner.replace("\\", "/")).name)[:120] or "plik"
                target = dest / f"{j:03d}_{safe}"            # płaska nazwa => brak zip-slip
                if not target.exists():
                    with z.open(info) as src, open(target, "wb") as f:
                        shutil.copyfileobj(src, f)
                mtime = "%04d-%02d-%02d %02d:%02d:%02d" % info.date_time
                out[target.name] = (zp.name, inner, mtime)
        print(f"[+] rozpakowano {zp.name}: {len(infos)} plików", file=sys.stderr)
    return out

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

    res = subprocess.run([exe, "-j", "-G1", "-a", "-s", "-charset", "filename=utf8", *map(str, files)],
                         capture_output=True, text=True)
    try:
        data = json.loads(res.stdout or "[]")
    except json.JSONDecodeError:
        sys.exit("[x] Nie udało się sparsować wyjścia exiftool:\n" + res.stderr)

    rows, long_rows, all_cols = [], [], set()
    zip_warn = False
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
        tags = {}
        for k, v in d.items():
            if k.startswith(DROP_PREFIXES):
                continue
            s = to_str(v)
            tag = k.split(":", 1)[-1]
            if s and PERSON_TAGS.match(tag) and k not in NOT_PERSON:
                s = pseud(key, s, "P"); pseud_fields.append(k)
            else:
                s, ch = scrub_text(key, s)
                if ch: pseud_fields.append(k)
            tags[k] = s
        row["n_tagow"] = len(tags)
        row["n_niepustych"] = sum(1 for v in tags.values() if v != "")
        row["pola_pseudonimizowane"] = ";".join(pseud_fields)
        row.update(tags)
        all_cols.update(tags)
        rows.append(row)
        for k, v in tags.items():
            long_rows.append({"plik": rel, "url": row["url"], "instytucja": row["instytucja"],
                              "grupa": k.split(":", 1)[0], "tag": k.split(":", 1)[-1], "wartosc": v,
                              "pseudonimizowane": int(k in pseud_fields)})

    if zip_warn:
        print("[!] UWAGA: exiftool nie ma Archive::Zip — metadane DOCX/XLSX/PPTX NIE zostały odczytane.\n"
              "    Zainstaluj: sudo apt install libarchive-zip-perl i uruchom ponownie.", file=sys.stderr)
        if not a.allow_missing_zip:
            sys.exit(2)

    missing_url = [r["plik"] for r in rows if not r["url"]]
    if missing_url:
        print(f"[!] {len(missing_url)} plików bez URL w manifeście: {', '.join(missing_url[:5])}...", file=sys.stderr)

    fixed = ["plik", "sha256", "url", "instytucja", "exiftool_ver", "z_archiwum", "data_w_archiwum", "n_tagow",
             "n_niepustych", "pola_pseudonimizowane"]
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
        lf = ["plik", "url", "instytucja", "grupa", "tag", "wartosc", "pseudonimizowane"]
        if lp.exists():
            with open(lp, encoding="utf-8-sig") as f:
                long_rows = [r for r in csv.DictReader(f) if r["plik"] not in new_names] + long_rows
        with open(lp, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=lf); w.writeheader(); w.writerows(long_rows)
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
    p.add_argument("--out", required=True)
    p.add_argument("--long", help="opcjonalny CSV w formacie długim (plik, tag, wartość)")
    p.add_argument("--key-file", default=DOMYSLNY_KLUCZ, help="klucz HMAC (poza katalogiem danych)")
    p.add_argument("--keep-files", action="store_true", help="NIE usuwaj surowych plików po ekstrakcji (tylko do debugowania)")
    p.add_argument("--allow-missing-zip", action="store_true", help="nie przerywaj, gdy brak Archive::Zip")
    a = p.parse_args()
    cmd_extract(a)


if __name__ == "__main__":
    main()
