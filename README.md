# KO Live – mobile Web-App

Diese Version besteht aus Frontend + kleinem Python-Backend.

## Was sie macht
- mobile Produktsuche nach Basiswert, z. B. `DAX Future`, `DAX`, `NVIDIA`
- Long / Short
- nur BNP Paribas, J.P. Morgan, Société Générale, Vontobel
- Hebelfilter bis 50
- KO-Abstandsfilter
- übernimmt gefundene Produkte in den Szenario-Rechner
- Szenarien -10 % bis +20 % plus freie Eingabe
- lokale Speicherung im Browser

## Live-Daten
Das Backend liest die öffentliche Knock-out-Produktsuche der Börse Stuttgart serverseitig ein und filtert anschließend auf die vier gewünschten Emittenten.
Die Quelle kann ihre HTML-Struktur oder URL-Parameter jederzeit ändern. Deshalb ist die Datenanbindung bewusst in `app.py` gekapselt.

## Lokal starten
1. Python 3.11+ installieren
2. Terminal in diesem Ordner öffnen
3. `pip install -r requirements.txt`
4. `python app.py`
5. Im Browser `http://127.0.0.1:8000` öffnen

## Mobil nutzen
Für iPhone/Android muss die App auf einem Webserver laufen. Sie ist für Render, Railway, Fly.io oder einen eigenen Server vorbereitet.
Startkommando für Hosting: `gunicorn app:app`

Danach die öffentliche HTTPS-Adresse in Safari öffnen und "Zum Home-Bildschirm" wählen.

## Einschränkung
Die Live-Anbindung konnte in der Erstellungsumgebung nicht gegen die reale Börse-Stuttgart-Seite ausgeführt werden. Falls die Börse Stuttgart den Namen des freien Suchparameters oder das Tabellen-Markup geändert hat, muss nur `fetch_finder()`/`parse_finder()` in `app.py` angepasst werden.
