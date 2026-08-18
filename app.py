from flask import Flask, render_template, request, jsonify
import requests, re
from bs4 import BeautifulSoup

app = Flask(__name__)

ALLOWED_ISSUERS = {
    "BNP Paribas": ["BNP Paribas", "BNP"],
    "J.P. Morgan": ["J.P. Morgan", "JPMorgan", "JPM"],
    "Société Générale": ["Société Générale", "Societe Generale", "SG"],
    "Vontobel": ["Vontobel"]
}

ONVISTA_DAX = "https://www.onvista.de/derivate/Knock-Outs/Knock-Outs-auf-DAX"

def parse_num(s):
    if s is None:
        return None
    s = str(s).replace("\xa0", " ").strip()
    if s in ("", "-", "–"):
        return None
    s = re.sub(r"[^\d,.\-]", "", s)
    if not s:
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except Exception:
        return None

def normalize_issuer(s):
    lo = (s or "").lower()
    if "bnp" in lo:
        return "BNP Paribas"
    if "j.p" in lo or "jpmorgan" in lo or "jp morgan" in lo:
        return "J.P. Morgan"
    if "soci" in lo or "societe" in lo:
        return "Société Générale"
    if "vontobel" in lo:
        return "Vontobel"
    return (s or "").strip()

def get_onvista_url(query, direction):
    q = (query or "").lower().strip()
    if any(x in q for x in ["dax", "fdax", "x-dax", "xdax"]):
        exercise_right = "1" if direction == "long" else "2"
        return (
            ONVISTA_DAX
            + "?entitySubType=KNOCKOUT_CERTIFICATE"
            + "&entityTypeUnderlying=INDEX"
            + "&entityValueUnderlying=20735"
            + f"&idExerciseRight={exercise_right}"
        )
    raise ValueError(
        "Die Live-Onvista-Anbindung unterstützt aktuell DAX / DAX Future / FDAX / X-DAX."
    )

def fetch_onvista(query, direction):
    url = get_onvista_url(query, direction)
    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1",
        "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": "https://www.onvista.de/",
        "Cache-Control": "no-cache",
    }
    r = requests.get(url, headers=headers, timeout=20)
    r.raise_for_status()
    return parse_onvista(r.text), url

def parse_onvista(html):
    soup = BeautifulSoup(html, "html.parser")
    products = []
    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        if len(cells) < 10:
            continue
        if "WKN" in cells[0] and "Basispreis" in " ".join(cells):
            continue
        wkn_match = re.search(r"\b[A-Z0-9]{6}\b", cells[0].replace(" ", ""))
        if not wkn_match:
            continue
        issuer = normalize_issuer(cells[9] if len(cells) > 9 else "")
        if issuer not in ALLOWED_ISSUERS:
            continue
        p = {
            "wkn": wkn_match.group(0),
            "name": "DAX Knock-out",
            "underlying": "DAX",
            "strike": parse_num(cells[1]),
            "ko": parse_num(cells[2]),
            "maturity": cells[3],
            "bid": parse_num(cells[4]),
            "ask": parse_num(cells[5]),
            "leverage": parse_num(cells[6]),
            "spread": parse_num(cells[7]),
            "issuer": issuer,
            "koDistance": None,
        }
        products.append(p)
    return products

@app.get("/")
def home():
    return render_template("index.html")

@app.get("/api/search")
def api_search():
    q = (request.args.get("q") or "").strip()
    direction = (request.args.get("direction") or "long").lower()
    max_lev = float(request.args.get("maxLeverage") or 50)
    max_ko = float(request.args.get("maxKoDistance") or 100)
    wanted = [x for x in request.args.getlist("issuer") if x in ALLOWED_ISSUERS]
    if not wanted:
        wanted = list(ALLOWED_ISSUERS)

    if not q:
        return jsonify({"ok": False, "error": "Bitte Basiswert eingeben."}), 400

    try:
        rows, source_url = fetch_onvista(q, direction)
        out = []
        for x in rows:
            if x["issuer"] not in wanted:
                continue
            if x["leverage"] is not None and x["leverage"] > max_lev:
                continue
            if x["leverage"] and x["leverage"] > 0:
                x["koDistance"] = 100.0 / x["leverage"]
            if x["koDistance"] is not None and x["koDistance"] > max_ko:
                continue
            out.append(x)

        out.sort(key=lambda z: (z["leverage"] is None, -(z["leverage"] or 0)))
        return jsonify({
            "ok": True,
            "source": "Onvista",
            "sourceUrl": source_url,
            "count": len(out),
            "products": out[:100]
        })
    except requests.HTTPError as e:
        return jsonify({
            "ok": False,
            "error": f"Onvista hat den Abruf abgewiesen ({e.response.status_code}).",
            "detail": "Falls Onvista Serverzugriffe blockiert, brauchen wir eine alternative Feed-/API-Lösung."
        }), 502
    except Exception as e:
        return jsonify({
            "ok": False,
            "error": "Live-Daten konnten aktuell nicht geladen werden.",
            "detail": str(e)
        }), 502

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
