from flask import Flask, render_template, request, jsonify
import requests, re, json
from bs4 import BeautifulSoup
from urllib.parse import urljoin

app = Flask(__name__)

SEARCH_API = "https://api.onvista.de/api/v1/instruments/search"
ONVISTA = "https://www.onvista.de"
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

    candidates = []
    ql = q.lower()
    for d in walk(data):
        text = json.dumps(d, ensure_ascii=False).lower()
        score = 0
        if ql in text:
            score += 20
        wkn = str(d.get("wkn") or deep_first(d, ["wkn"]) or "").lower()
        isin = str(d.get("isin") or deep_first(d, ["isin"]) or "").lower()
        if wkn == ql:
            score += 50
        if isin == ql:
            score += 50
        if "knock" in text or "turbo" in text:
            score += 10
        if score:
            candidates.append((score, d))

    if not candidates:
        raise ValueError("Kein passendes Instrument in der Onvista-Suche gefunden.")

    candidates.sort(key=lambda x: x[0], reverse=True)
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


def find_product_url(q):
    inst = search_onvista_instrument(q)
    if inst.get("url") and "/derivate/Knock-Outs/" in inst["url"]:
        return inst, inst["url"]

    # fallback to Onvista's search page
    r = session.get(ONVISTA + "/suche/", params={"searchValue": q}, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        txt = a.get_text(" ", strip=True)
        if q.lower() in (href + " " + txt).lower() and "/derivate/Knock-Outs/" in href:
            return inst, urljoin(ONVISTA, href)

    raise ValueError("Onvista hat keine Knock-Out-Produktseite für diese WKN/ISIN geliefert.")


def parse_visible_product_page(html):
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)

    def grab(pattern):
        m = re.search(pattern, text, re.I)
        return m.group(1).strip() if m else None

    issuer = normalize_issuer(grab(r"Emittent\s+(.+?)\s+(?:WKN|ISIN|Hinzufügen|Vergleichen|Handeln|Geld)"))
    wkn = grab(r"\bWKN\s+([A-Z0-9]{6})\b")
    isin = grab(r"\bISIN\s+([A-Z]{2}[A-Z0-9]{9}[0-9])\b")

    strike = parse_num(grab(r"\bBasispreis\s+([0-9.,]+)\s*(?:EUR|USD|CHF|Pkt\.)"))
    ko = parse_num(grab(r"\bK\.?O\.?\s+([0-9.,]+)\s*(?:EUR|USD|CHF|Pkt\.)"))
    leverage = parse_num(grab(r"\bHebel\s+([0-9.,]+)\s*x"))
    ratio = parse_num(grab(r"\bBezugsverhältnis\s+([0-9.,]+)"))
    bid = parse_num(grab(r"\bGeld\s+(?:•\s*)?(?:[\d.]+\s*Stk\.\s*)?([0-9.,]+)\s*(?:EUR|USD|CHF|Pkt\.)"))
    ask = parse_num(grab(r"\bBrief\s+(?:•\s*)?(?:[\d.]+\s*Stk\.\s*)?([0-9.,]+)\s*(?:EUR|USD|CHF|Pkt\.)"))

    title = soup.find("h1")
    name = title.get_text(" ", strip=True) if title else None
    upper = (name or "").upper()
    direction = "long" if "LONG" in upper or "CALL" in upper else ("short" if "SHORT" in upper or "PUT" in upper else None)

    underlying = None
    spot = None
    m = re.search(r"\bBasiswert\s+(.+?)\s+(?:Xetra|Nasdaq|NYSE|Stuttgart|gettex|Frankfurt|Tradegate).*?([0-9][0-9.,]*)\s*(EUR|USD|CHF|Pkt\.)", text, re.I)
    if m:
        underlying = m.group(1).strip()
        spot = parse_num(m.group(2))

    ko_distance = parse_num(grab(r"\bAbstand K\.?O\.?.*?\(([0-9.,]+)\s*%\)"))
    if ko_distance is None and spot and ko:
        ko_distance = ((spot / ko - 1) * 100) if direction != "short" else ((ko / spot - 1) * 100)

    return {
        "wkn": wkn,
        "isin": isin,
        "name": name,
        "issuer": issuer,
        "underlying": underlying,
        "spot": spot,
        "strike": strike,
        "ko": ko,
        "ratio": ratio,
        "bid": bid,
        "ask": ask,
        "leverage": leverage,
        "koDistance": ko_distance,
        "maturity": "",
        "direction": direction,
    }


def enrich_from_next_data(html, product):
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
        "strike": ["strike", "basispreis", "basePrice", "exercisePrice"],
        "ko": ["knockOutBarrier", "knockoutBarrier", "koBarrier", "barrier"],
        "ratio": ["ratio", "subscriptionRatio", "conversionRatio"],
        "spot": ["underlyingPrice", "underlyingLast", "lastUnderlyingPrice"],
        "bid": ["bid", "bidPrice"],
        "ask": ["ask", "askPrice"],
        "leverage": ["leverage", "leverageAsk"],
        "underlying": ["underlyingName", "underlyingShortName"],
    }
    for field, keys in mapping.items():
        if product.get(field) not in (None, ""):
            continue
        v = deep_first(data, keys)
        if field in {"strike", "ko", "ratio", "spot", "bid", "ask", "leverage"}:
            v = parse_num(v)
        if v not in (None, ""):
            product[field] = v

    if not product.get("issuer"):
        product["issuer"] = normalize_issuer(deep_first(data, ["issuerName", "issuer", "issuerShortName"]))
    return product


def search_by_wkn_or_isin(q):
    inst, url = find_product_url(q)
    r = session.get(url, timeout=20)
    r.raise_for_status()
    product = parse_visible_product_page(r.text)
    product = enrich_from_next_data(r.text, product)
    product["wkn"] = product.get("wkn") or inst.get("wkn") or (q.upper() if len(q) == 6 else None)
    product["isin"] = product.get("isin") or inst.get("isin")
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
    wanted = set(request.args.getlist("issuer")) & ALLOWED_ISSUERS or set(ALLOWED_ISSUERS)

    if not q:
        return jsonify({"ok": False, "error": "Bitte WKN oder ISIN eingeben."}), 400

    try:
        q_clean = q.replace(" ", "").upper()
        is_wkn = bool(re.fullmatch(r"[A-Z0-9]{6}", q_clean))
        is_isin = bool(re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", q_clean))

        if not (is_wkn or is_isin):
            return jsonify({
                "ok": False,
                "error": "Diese Version ist für die zuverlässige WKN-/ISIN-Suche gebaut. Bitte z. B. BY02UE eingeben."
            }), 400

        p = search_by_wkn_or_isin(q_clean)

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
