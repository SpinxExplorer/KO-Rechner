from flask import Flask, render_template, request, jsonify
import requests
import re
import json
from bs4 import BeautifulSoup
from urllib.parse import urljoin

app = Flask(__name__)

ONVISTA = "https://www.onvista.de"
SEARCH_API = "https://api.onvista.de/api/v1/instruments/search"
ALLOWED_ISSUERS = {"BNP Paribas", "J.P. Morgan", "Société Générale", "Vontobel"}

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Mobile/15E148 Safari/604.1",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    "Accept": "application/json,text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://www.onvista.de/",
    "Cache-Control": "no-cache",
})


def parse_num(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).replace("\xa0", " ").strip()
    if not s or s in {"-", "–", "n.a.", "n.a"}:
        return None
    s = re.sub(r"[^\d,.\-]", "", s)
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def normalize_issuer(value):
    t = (value or "").lower()
    if "bnp" in t:
        return "BNP Paribas"
    if "j.p. morgan" in t or "jp morgan" in t or "jpmorgan" in t:
        return "J.P. Morgan"
    if "société générale" in t or "societe generale" in t:
        return "Société Générale"
    if "vontobel" in t:
        return "Vontobel"
    return None


def all_dicts(obj):
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from all_dicts(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from all_dicts(value)


def deep_first(obj, keys):
    wanted = {k.lower() for k in keys}
    for d in all_dicts(obj):
        for key, value in d.items():
            if str(key).lower() in wanted and value not in (None, "", [], {}):
                return value
    return None


def search_api_record(query):
    r = session.get(SEARCH_API, params={"searchValue": query}, timeout=15)
    r.raise_for_status()
    data = r.json()
    q = query.upper()
    candidates = []
    for d in all_dicts(data):
        wkn = str(d.get("wkn") or deep_first(d, ["wkn"]) or "").upper()
        isin = str(d.get("isin") or deep_first(d, ["isin"]) or "").upper()
        blob = json.dumps(d, ensure_ascii=False).upper()
        score = 0
        if wkn == q: score += 1000
        if isin == q: score += 1000
        if q in blob: score += 50
        if "KNOCK" in blob or "TURBO" in blob: score += 40
        if "DERIVAT" in blob or "HEBEL" in blob: score += 10
        if score:
            candidates.append((score, d))
    if not candidates:
        return {}
    candidates.sort(key=lambda item: item[0], reverse=True)
    best = candidates[0][1]
    url = best.get("url") or best.get("link") or best.get("path") or deep_first(best, ["url", "link", "path", "seoUrl"])
    if url and str(url).startswith("/"):
        url = urljoin(ONVISTA, str(url))
    return {
        "wkn": best.get("wkn") or deep_first(best, ["wkn"]),
        "isin": best.get("isin") or deep_first(best, ["isin"]),
        "name": best.get("name") or best.get("displayName") or best.get("label") or deep_first(best, ["name", "displayName", "label", "shortName"]),
        "url": url,
    }


def find_product_url(query):
    rec = search_api_record(query)
    if rec.get("url") and "/derivate/Knock-Outs/" in rec["url"]:
        return rec, rec["url"]
    r = session.get(ONVISTA + "/suche/", params={"searchValue": query}, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        hay = (href + " " + a.get_text(" ", strip=True)).upper()
        if query.upper() in hay and "/DERIVATE/KNOCK-OUTS/" in href.upper():
            return rec, urljoin(ONVISTA, href)
    raise ValueError("Onvista hat keine passende Knock-Out-Produktseite gefunden.")


def parse_visible_product(html):
    soup = BeautifulSoup(html, "html.parser")
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    title = soup.find("h1")
    name = title.get_text(" ", strip=True) if title else None

    wkn = None
    isin = None
    m = re.search(r"\bWKN\s+([A-Z0-9]{6})\b", text, re.I)
    if m: wkn = m.group(1).upper()
    m = re.search(r"\bISIN\s+([A-Z]{2}[A-Z0-9]{9}[0-9])\b", text, re.I)
    if m: isin = m.group(1).upper()

    upper = f" {name or ''} ".upper()
    if " LONG " in upper or " CALL " in upper:
        direction = "long"
    elif " SHORT " in upper or " PUT " in upper:
        direction = "short"
    else:
        snippet = text[:5000].upper()
        direction = "long" if "(CALL)" in snippet else ("short" if "(PUT)" in snippet else None)

    issuer = None
    for candidate in ALLOWED_ISSUERS:
        if candidate.lower() in text.lower():
            issuer = candidate
            break

    def g(pattern):
        m = re.search(pattern, text, re.I)
        return parse_num(m.group(1)) if m else None

    strike = g(r"\bBasispreis\s+([0-9.\s]+,[0-9]+)\s*(?:EUR|USD|CHF|Pkt\.)")
    ko = g(r"\bK\.?O\.?(?:-Schwelle)?\s+([0-9.\s]+,[0-9]+)\s*(?:EUR|USD|CHF|Pkt\.)")
    leverage = g(r"\bHebel\s+([0-9.,]+)\s*x\b")
    ratio = g(r"\bBezugsverhältnis\s+([0-9.,]+)\b")
    bid = g(r"\bGeld\b.{0,80}?([0-9.\s]+,[0-9]+)\s*(?:EUR|USD|CHF)\b")
    ask = g(r"\bBrief\b.{0,80}?([0-9.\s]+,[0-9]+)\s*(?:EUR|USD|CHF)\b")
    ko_distance = g(r"\bAbstand\s+K\.?O\.?\s+[0-9.\s]+,[0-9]+\s*(?:EUR|USD|CHF|Pkt\.)\s*\(([0-9.,]+)\s*%\)")

    underlying = None
    spot = None
    m = re.search(
        r"\bBasiswert\s+(.+?)\s+(?:Xetra|Tradegate|Nasdaq|NYSE|Stuttgart|Frankfurt|gettex|Eurex|L&S|Lang & Schwarz)\s*[·|].{0,80}?([0-9.\s]+,[0-9]+)\s*(EUR|USD|CHF|Pkt\.)",
        text, re.I
    )
    if m:
        underlying = m.group(1).strip()
        spot = parse_num(m.group(2))

    if not underlying and name:
        m = re.search(r"\bAUF\s+(.+?)(?:\s+AG|\s+SE|\s+PLC|\s+NV|\s*$)", name, re.I)
        if m:
            underlying = m.group(1).strip()

    return {
        "wkn": wkn, "isin": isin, "name": name, "issuer": issuer,
        "underlying": underlying, "spot": spot, "strike": strike, "ko": ko,
        "ratio": ratio, "bid": bid, "ask": ask, "leverage": leverage,
        "koDistance": ko_distance, "maturity": "", "direction": direction,
    }


def enrich_missing_from_next_data(html, product):
    soup = BeautifulSoup(html, "html.parser")
    tag = soup.find("script", id="__NEXT_DATA__")
    if not tag:
        return product
    try:
        data = json.loads(tag.string or tag.get_text())
    except Exception:
        return product

    mapping = {
        "wkn": ["wkn"],
        "isin": ["isin"],
        "strike": ["strike", "exercisePrice", "basePrice"],
        "ko": ["knockOutBarrier", "knockoutBarrier", "koBarrier"],
        "ratio": ["ratio", "subscriptionRatio", "conversionRatio"],
        "spot": ["underlyingPrice", "underlyingLast", "lastUnderlyingPrice"],
        "bid": ["bidPrice"],
        "ask": ["askPrice"],
        "leverage": ["leverage", "leverageAsk"],
        "underlying": ["underlyingName", "underlyingShortName"],
    }
    numeric = {"strike", "ko", "ratio", "spot", "bid", "ask", "leverage"}
    for field, keys in mapping.items():
        if product.get(field) not in (None, ""):
            continue
        value = deep_first(data, keys)
        if field in numeric:
            value = parse_num(value)
        if value not in (None, ""):
            product[field] = value

    if not product.get("issuer"):
        product["issuer"] = normalize_issuer(deep_first(data, ["issuerName", "issuer", "issuerShortName"]))

    if product.get("koDistance") is None and product.get("spot") and product.get("ko"):
        if product.get("direction") == "short":
            product["koDistance"] = (product["ko"] / product["spot"] - 1) * 100
        else:
            product["koDistance"] = (product["spot"] / product["ko"] - 1) * 100
    return product


def load_product(query):
    rec, url = find_product_url(query)
    r = session.get(url, timeout=20)
    r.raise_for_status()
    product = parse_visible_product(r.text)
    product = enrich_missing_from_next_data(r.text, product)
    product["wkn"] = product.get("wkn") or rec.get("wkn") or (query if len(query) == 6 else None)
    product["isin"] = product.get("isin") or rec.get("isin")
    product["name"] = product.get("name") or rec.get("name") or "Onvista Knock-out"
    product["profileUrl"] = url
    return product


@app.get("/")
def home():
    return render_template("index.html")


@app.get("/api/search")
def api_search():
    q = (request.args.get("q") or "").strip().upper()
    direction = (request.args.get("direction") or "long").lower()
    max_lev = float(request.args.get("maxLeverage") or 50)
    max_ko = float(request.args.get("maxKoDistance") or 100)
    wanted = set(request.args.getlist("issuer")) & ALLOWED_ISSUERS or set(ALLOWED_ISSUERS)

    if not q:
        return jsonify({"ok": False, "error": "Bitte WKN oder ISIN eingeben."}), 400

    is_wkn = bool(re.fullmatch(r"[A-Z0-9]{6}", q))
    is_isin = bool(re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", q))
    if not (is_wkn or is_isin):
        return jsonify({"ok": False, "error": "Diese stabile Version sucht zunächst gezielt nach WKN oder ISIN."}), 400

    try:
        p = load_product(q)
        if p.get("issuer") and p["issuer"] not in wanted:
            return jsonify({"ok": True, "source": "Onvista", "count": 0, "products": []})
        if p.get("direction") and p["direction"] != direction:
            return jsonify({"ok": True, "source": "Onvista", "count": 0, "products": []})
        if p.get("leverage") is not None and p["leverage"] > max_lev:
            return jsonify({"ok": True, "source": "Onvista", "count": 0, "products": []})
        if p.get("koDistance") is not None and p["koDistance"] > max_ko:
            return jsonify({"ok": True, "source": "Onvista", "count": 0, "products": []})
        return jsonify({
            "ok": True,
            "source": "Onvista WKN/ISIN",
            "sourceUrl": p.get("profileUrl"),
            "count": 1,
            "products": [p],
        })
    except requests.Timeout:
        return jsonify({"ok": False, "error": "Onvista hat nicht rechtzeitig geantwortet."}), 504
    except requests.HTTPError as e:
        status = getattr(e.response, "status_code", "?")
        return jsonify({"ok": False, "error": f"Onvista hat den Abruf abgewiesen ({status})."}), 502
    except Exception as e:
        return jsonify({"ok": False, "error": "Die WKN/ISIN konnte nicht sauber aus Onvista geladen werden.", "detail": str(e)}), 502


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
