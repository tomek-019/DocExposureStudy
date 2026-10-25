# DocExposureStudy

Narzędzie badawcze przygotowane na potrzeby pracy inżynierskiej pt. *„Analiza ekspozycji informacji w metadanych publicznie dostępnych dokumentów polskich instytucji z wykorzystaniem metod OSINT”*.

*Research tool for an engineering thesis on information exposure in the metadata of publicly available documents.*

## Co robi

1. Przeszukuje stronę instytucji (sitemapa i linki) i wyszukuje publicznie dostępne dokumenty (PDF, pliki Office/OpenDocument, obrazy). Jeśli instytucja publikuje dokumenty na dodatkowej, wskazanej przez siebie domenie (np. Biuletyn Informacji Publicznej na platformie zewnętrznej albo strona relacji inwestorskich), przeszukiwana jest także ta domena.
2. Pobiera losową próbę do 100 plików.
3. Odczytuje wyłącznie metadane plików (exiftool), np. program, w którym powstał dokument, i daty. Treść dokumentów nie jest analizowana.
4. Usuwa pobrane pliki zaraz po odczycie metadanych.

## Zasady

- Tylko pliki publicznie dostępne, bez logowania i bez omijania zabezpieczeń.
- Przestrzega `robots.txt`, także przy przekierowaniach na inny serwer.
- Co najmniej 5 sekund przerwy między zapytaniami do jednego serwera, maksymalnie 1500 zapytań na instytucję (łącznie dla wszystkich jej domen).
- Dane osobowe z metadanych (np. autor dokumentu) są pseudonimizowane i przechowywane wyłącznie lokalnie.
- W pracy instytucje nie są wymieniane z nazwy; wyniki są prezentowane zbiorczo.

## Pliki

| Plik | Etap |
|---|---|
| `tools/sample.py` | losowanie instytucji z oficjalnych wykazów |
| `tools/discover.py` | wyszukiwanie dokumentów i losowanie próby |
| `tools/fetch.py` | pobieranie plików |
| `tools/extract.py` | odczyt metadanych, pseudonimizacja, usunięcie plików |
| `tools/normalize.py` | ujednolicenie metadanych do wspólnych pól |
| `tools/audit.py` | kontrola zgodności zapytań z zasadami |
| `tools/common.py` | User-Agent, robots.txt, limit tempa, log zapytań |
| `summarize.py` | podsumowanie wyników |
