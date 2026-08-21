from flask import Flask, render_template, request, jsonify
import requests, re, json
from bs4 import BeautifulSoup
from urllib.parse import urljoin

app = Flask(__name__)

ONVISTA = "https://www.onvista.de"
SEARCH_API = "https://api.onvista.de/api/v1/instruments/search"

ALLOWED_ISSUERS = {
    "BNP Paribas",
    "J.P. Morgan",
    "Société Générale",
    "Vontobel",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Mobile/15E148 Safari/604.1",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    "Accept": "application/json,text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://www.onvista.de/",
    "Cache-Control": "no-cache",
}

session = requests.Session()
session.headers.update(HEADERS)

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
    return (value or "").strip() or None

def walk(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v)

def deep_first(obj, keys):
    keys = {k.lower() for k in keys}
    for d in walk(obj):
        for k, v in d.items():
            if str(k).lower() in keys and v not in (None, "", [], {}):
                return v
    return None

def search_onvista_instrument(q):
    r = session.get(SEARCH_API, params={"searchValue": q}, timeout=15)
    r.raise_for_status()
    data = r.json()
    print("DEBUG ONVISTA SEARCH:", q, json.dumps(data, ensure_ascii=False)[:4000], flush=True)
    candidates = []
    ql = q.lower()
    for d in walk(data):
        txt = json.dumps(d, ensure_ascii=False).lower()
        score = 0
        wkn = str(d.get("wkn") or deep_first(d, ["wkn"]) or "").lower()
        isin = str(d.get("isin") or deep_first(d, ["isin"]) or "").lower()
        if wkn == ql:
            score += 100
        if isin == ql:
            score += 100
        if ql in txt:
            score += 20
        if "knock" in txt or "turbo" in txt:
            score += 20
        if score:
            candidates.append((score, d))
    if not candidates:
        raise ValueError("Kein passendes Onvista-Instrument gefunden.")
    candidates.sort(key=lambda x: x[0], reverse=True)
    best = candidates[0][1]
    url = (best.get("urls") or {}).get("WEBSITE") or best.get("url") or best.get("link") or best.get("path") or deep_first(best, ["url", "link", "path", "seoUrl"])
    if url and str(url).startswith("/"):
        url = urljoin(ONVISTA, str(url))
    return {
        "wkn": best.get("wkn") or deep_first(best, ["wkn"]),
        "isin": best.get("isin") or deep_first(best, ["isin"]),
        "name": best.get("name") or best.get("displayName") or best.get("label") or deep_first(best, ["name", "displayName", "label", "shortName"]),
        "url": url,
    }

def find_product_url(q):
    inst = search_onvista_instrument(q)
    url = inst.get("url")
    if url and "/derivate/Knock-Outs/" in url:
        return inst, url
    r = session.get(ONVISTA + "/suche/", params={"searchValue": q}, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        hay = (href + " " + a.get_text(" ", strip=True)).lower()
        if q.lower() in hay and "/derivate/Knock-Outs/" in href:
            return inst, urljoin(ONVISTA, href)
    raise ValueError("Keine Knock-Out-Produktseite zu dieser WKN/ISIN gefunden.")

def text_value_after_label(text, label, max_chars=120):
    m = re.search(rf"{re.escape(label)}\s{{0,8}}(.{{1,{max_chars}}})", text, flags=re.I)
    return m.group(1) if m else None

def first_number(s):
    if not s:
        return None
    m = re.search(r"[-+]?\d[\d.\s]*,\d+|[-+]?\d+(?:\.\d+)?", s)
    return parse_num(m.group(0)) if m else None

def parse_labeled_product_data(html):
    soup = BeautifulSoup(html, "html.parser")
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))

    wkn = None
    isin = None
    m = re.search(r"\bWKN\s+([A-Z0-9]{6})\b", text, re.I)
    if m:
        wkn = m.group(1).upper()
    m = re.search(r"\bISIN\s+([A-Z]{2}[A-Z0-9]{9}[0-9])\b", text, re.I)
    if m:
        isin = m.group(1).upper()

    title = soup.find("h1")
    name = title.get_text(" ", strip=True) if title else None
    upper = (name or text[:1200]).upper()
    if " LONG " in f" {upper} " or " CALL " in f" {upper} ":
        direction = "long"
    elif " SHORT " in f" {upper} " or " PUT " in f" {upper} ":
        direction = "short"
    else:
        direction = None

    issuer = None
    for candidate in ALLOWED_ISSUERS:
        if candidate.lower() in text.lower():
            issuer = candidate
            break

    strike = first_number(text_value_after_label(text, "Basispreis"))
    ko = (
        first_number(text_value_after_label(text, "K.O.-Schwelle"))
        or first_number(text_value_after_label(text, "K.O."))
        or first_number(text_value_after_label(text, "Knock-Out"))
    )
    ratio = (
        first_number(text_value_after_label(text, "Bezugsverhältnis"))
        or first_number(text_value_after_label(text, "Bezugsverhaeltnis"))
    )
    leverage = first_number(text_value_after_label(text, "Hebel"))
    bid = first_number(text_value_after_label(text, "Geld", 60))
    ask = first_number(text_value_after_label(text, "Brief", 60))

    underlying = None
    spot = None
    basis_segment = text_value_after_label(text, "Basiswert", 260)
    if basis_segment:
        mname = re.match(r"(.+?)(?:\s+(?:Xetra|Tradegate|Nasdaq|NYSE|Stuttgart|Frankfurt|gettex|Eurex)\b|\s+[·|])", basis_segment, re.I)
        if mname:
            underlying = mname.group(1).strip()
        

    ko_distance = None
    md = re.search(r"(?:Abstand\s+(?:K\.?O\.?|Knock-Out)|K\.?O\.?-Abstand)\D{0,40}([0-9.,]+)\s*%", text, re.I)
    if md:
        ko_distance = parse_num(md.group(1))
    if ko_distance is None and spot and ko:
        ko_distance = ((ko / spot - 1) * 100) if direction == "short" else ((spot / ko - 1) * 100)

    return {
        "wkn": wkn, "isin": isin, "name": name, "issuer": issuer,
        "underlying": underlying, "spot": spot, "strike": strike, "ko": ko,
        "ratio": ratio, "bid": bid, "ask": ask, "leverage": leverage,
        "koDistance": ko_distance, "maturity": "", "direction": direction,
    }

def enrich_missing_from_next_data(html, product):
    soup = BeautifulSoup(html, "html.parser")
    tag = soup.find("script", id="__NEXT_DATA__")
    print("DEBUG NEXT TAG FOUND:", bool(tag), flush=True)
    if not tag:
        return product
    try:
        data = json.loads(tag.string or tag.get_text())
        for d in walk(data):
            for k, v in d.items():
                if any(x in str(k).lower() for x in ["bid", "ask", "price", "kurs", "quote", "underlying"]):
                    print("DEBUG PRICE FIELD:", k, "=", v, flush=True)   
    except Exception:
        return product

    mapping = {
        "wkn": ["wkn"],
        "isin": ["isin"],
        "strike": ["strike", "exercisePrice", "basePrice"],
        "ko": ["knockOutBarrier", "knockoutBarrier", "koBarrier"],
        "ratio": ["ratio", "subscriptionRatio", "conversionRatio"],
        "spot": ["underlyingPrice", "underlyingLast", "lastUnderlyingPrice"],
        "bid": ["bid", "bidPrice"],
        "ask": ["ask", "askPrice"],
        "leverage": ["leverage", "leverageAsk"],
        "underlying": ["underlyingName", "underlyingShortName"],
    }
    numeric_fields = {"strike", "ko", "ratio", "spot", "bid", "ask", "leverage"}

    for field, keys in mapping.items():
        if field not in {"spot", "bid", "ask"} and product.get(field) not in (None, ""):
            continue
        value = None
        for key in keys:
           value = deep_first(data, [key])
           if value not in (None, "", [], {}):
               break
        print("DEBUG MAPPED FIELD:", field, "KEYS:", keys, "VALUE:", value, flush=True)     
        if field in numeric_fields:
            value = parse_num(value)
        if value not in (None, ""):
            product[field] = value

    if not product.get("issuer"):
        product["issuer"] = normalize_issuer(deep_first(data, ["issuerName", "issuer", "issuerShortName"]))

    if product.get("koDistance") is None and product.get("spot") and product.get("ko"):
        product["koDistance"] = ((product["ko"] / product["spot"] - 1) * 100) if product.get("direction") == "short" else ((product["spot"] / product["ko"] - 1) * 100)

    return product

def search_by_wkn_or_isin(q):
    inst, url = find_product_url(q)
    r = session.get(url, timeout=20)
    r.raise_for_status()
    product = parse_labeled_product_data(r.text)
    product = enrich_missing_from_next_data(r.text, product)
    print("DEBUG PRODUCT AFTER ENRICH:", json.dumps(product, ensure_ascii=False, default=str), flush=True)
    product["wkn"] = product.get("wkn") or inst.get("wkn") or (q.upper() if len(q) == 6 else None)
    product["isin"] = product.get("isin") or inst.get("isin")
    product["name"] = product.get("name") or inst.get("name") or "Onvista Knock-out"
    product["profileUrl"] = url
    return product

@app.get("/")
def home():
    return render_template("index.html")

@app.get("/api/search")
def api_search():
    q = (request.args.get("q") or "").strip()
    direction = (request.args.get("direction") or "long").lower()
    max_lev = float(request.args.get("maxLeverage") or 50)
    max_ko = float(request.args.get("maxKoDistance") or 100)

    wanted = set(request.args.getlist("issuer")) & ALLOWED_ISSUERS
    if not wanted:
        wanted = set(ALLOWED_ISSUERS)

    if not q:
        return jsonify({"ok": False, "error": "Bitte WKN oder ISIN eingeben."}), 400

    try:
        q_clean = q.replace(" ", "").upper()
        is_wkn = bool(re.fullmatch(r"[A-Z0-9]{6}", q_clean))
        is_isin = bool(re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", q_clean))

        if not (is_wkn or is_isin):
            return jsonify({"ok": False, "error": "Bitte aktuell WKN oder ISIN eingeben."}), 400

        p = search_by_wkn_or_isin(q_clean)
        print("DEBUG PRODUCT BEFORE FILTERS:", json.dumps(p, ensure_ascii=False, default=str), flush=True)

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
        return jsonify({"ok": False, "error": "Onvista hat nicht rechtzeitig geantwortet (Timeout)."}), 504
    except requests.HTTPError as e:
        status = getattr(e.response, "status_code", "?")
        return jsonify({"ok": False, "error": f"Onvista hat den Abruf abgewiesen ({status})."}), 502
    except Exception as e:
        return jsonify({"ok": False, "error": "Live-Daten konnten aktuell nicht geladen werden.", "detail": str(e)}), 502

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
