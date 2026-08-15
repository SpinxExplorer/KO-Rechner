
from flask import Flask, render_template, request, jsonify
import requests, re
from bs4 import BeautifulSoup
from urllib.parse import urlencode

app = Flask(__name__)

ALLOWED_ISSUERS = {
    "BNP Paribas": ["BNP Paribas", "BNP"],
    "J.P. Morgan": ["J.P. Morgan", "JPMorgan", "JPM"],
    "Société Générale": ["Société Générale", "Societe Generale", "SG"],
    "Vontobel": ["Vontobel"]
}

# Börse Stuttgart Finder. We deliberately use a server-side request, not browser scraping,
# so the mobile frontend is not blocked by CORS.
FINDER_URL = "https://www.boerse-stuttgart.de/en/tools/finder-tools/knock-out/"

def n(s):
    if s is None: return None
    s = str(s).replace("\xa0"," ").strip()
    if s in ("", "-", "–"): return None
    # German/English number formats
    s = re.sub(r"[^\d,.\-]", "", s)
    if not s: return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".","").replace(",",".")
        else:
            s = s.replace(",","")
    elif "," in s:
        s = s.replace(",",".")
    try: return float(s)
    except: return None

def normalize_issuer(s):
    lo = (s or "").lower()
    if "bnp" in lo: return "BNP Paribas"
    if "j.p" in lo or "jpmorgan" in lo or "jp morgan" in lo: return "J.P. Morgan"
    if "soci" in lo or "societe" in lo or "sg " in (" "+lo+" "): return "Société Générale"
    if "vontobel" in lo: return "Vontobel"
    return s.strip() if s else ""

def parse_finder(html):
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    # Works with semantic tables and is intentionally tolerant of minor markup changes.
    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td","th"])]
        if len(cells) < 8: 
            continue
        if cells[0].strip().lower() in ("wkn","isin"):
            continue

        # Current Börse Stuttgart finder layout:
        # WKN, segment, name, bid, ask, exercise, underlying, basicprice,
        # KO barrier, leverage ask, KO distance, maturity, issuer, action
        if len(cells) >= 13:
            row = {
                "wkn": cells[0].strip(),
                "name": cells[2].strip(),
                "bid": n(cells[3]),
                "ask": n(cells[4]),
                "optionType": cells[5].strip().lower(),
                "underlying": cells[6].strip(),
                "strike": n(cells[7]),
                "ko": n(cells[8]),
                "leverage": n(cells[9]),
                "koDistance": abs(n(cells[10]) or 0),
                "maturity": cells[11].strip(),
                "issuer": normalize_issuer(cells[12]),
            }
            if re.fullmatch(r"[A-Z0-9]{6}", row["wkn"] or ""):
                rows.append(row)
    return rows

def fetch_finder(query):
    # The public finder accepts URL filters. `search` is passed as a free-text hint.
    # If the site changes its parameter naming, the parser still reports a clear live-source error.
    params = {
        "commercialSegment": "EASY EUWAX",
        "search": query,
    }
    r = requests.get(FINDER_URL, params=params, timeout=15, headers={
        "User-Agent":"Mozilla/5.0 (compatible; KO-Rechner/1.0)"
    })
    r.raise_for_status()
    return parse_finder(r.text), r.url

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
        return jsonify({"ok":False,"error":"Bitte Basiswert eingeben."}), 400

    try:
        rows, source_url = fetch_finder(q)
        qlo = q.lower()
        aliases = qlo.split()
        out = []
        for x in rows:
            if x["issuer"] not in wanted:
                continue
            typ = x["optionType"]
            if direction == "long" and typ not in ("call","long"):
                continue
            if direction == "short" and typ not in ("put","short"):
                continue
            if x["leverage"] is not None and x["leverage"] > max_lev:
                continue
            if x["koDistance"] is not None and x["koDistance"] > max_ko:
                continue
            hay = (x["underlying"]+" "+x["name"]+" "+x["wkn"]).lower()
            # loose matching: "DAX Future" should also match "DAX Performance-Index"
            if "dax" in qlo:
                if "dax" not in hay: continue
            elif aliases and not any(a in hay for a in aliases):
                continue
            out.append(x)
        out.sort(key=lambda z: (z["leverage"] is None, -(z["leverage"] or 0)))
        return jsonify({"ok":True,"source":"Börse Stuttgart","sourceUrl":source_url,"count":len(out),"products":out[:100]})
    except Exception as e:
        return jsonify({
            "ok":False,
            "error":"Live-Daten konnten aktuell nicht geladen werden.",
            "detail":str(e)
        }), 502

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
