from flask import Flask, render_template, request, jsonify
import requests
import re
from bs4 import BeautifulSoup
from urllib.parse import quote_plus

app = Flask(__name__)

ONVISTA = "https://www.onvista.de"

ALLOWED_ISSUERS = {
    "BNP Paribas",
    "J.P. Morgan",
    "Société Générale",
    "Vontobel",
}

S = requests.Session()
S.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
})


def num(s):
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).replace("\xa0", " ").strip()
    m = re.search(r"-?\d[\d.\s]*,\d+|-?\d+(?:\.\d+)?", s)
    if not m:
        return None
    x = m.group(0).replace(" ", "")
    if "," in x:
        x = x.replace(".", "").replace(",", ".")
    try:
        return float(x)
    except ValueError:
        return None


def normalize_issuer(text):
    t = (text or "").lower()
    if "bnp" in t:
        return "BNP Paribas"
    if "j.p. morgan" in t or "jp morgan" in t or "jpmorgan" in t:
        return "J.P. Morgan"
    if "société générale" in t or "societe generale" in t:
        return "Société Générale"
    if "vontobel" in t:
        return "Vontobel"
    return None


def google_product_url(wkn):
    """
    Resolve the full Onvista product URL.
    Onvista product URLs contain an internal numeric id, so constructing
    /Knock-Outs/<WKN> directly causes a 404.
    """
    q = f'site:onvista.de/derivate/Knock-Outs/ "{wkn}"'
    url = "https://www.google.com/search?q=" + quote_plus(q)
    r = S.get(url, timeout=15)
    r.raise_for_status()

    # Google result links may contain the direct Onvista URL or /url?q=...
    patterns = [
        rf'https://www\.onvista\.de/derivate/Knock-Outs/\d+-{re.escape(wkn)}-[A-Z0-9]+',
        rf'https%3A%2F%2Fwww\.onvista\.de%2Fderivate%2FKnock-Outs%2F\d+-{re.escape(wkn)}-[A-Z0-9]+',
    ]
    for pat in patterns:
        m = re.search(pat, r.text, re.I)
        if m:
            found = m.group(0)
            if found.startswith("https%3A"):
                from urllib.parse import unquote
                found = unquote(found)
            return found.split("&")[0]

    return None


def onvista_search_product_url(wkn):
    """
    Fallback: Onvista's own search page.
    """
    candidates = [
        f"{ONVISTA}/suche/?searchValue={quote_plus(wkn)}",
        f"{ONVISTA}/suche?searchValue={quote_plus(wkn)}",
        f"{ONVISTA}/suche/?query={quote_plus(wkn)}",
    ]

    rx = re.compile(
        rf'(/derivate/Knock-Outs/\d+-{re.escape(wkn)}-[A-Z0-9]+)',
        re.I
    )

    for url in candidates:
        try:
            r = S.get(url, timeout=15)
            if r.status_code != 200:
                continue
            m = rx.search(r.text)
            if m:
                return ONVISTA + m.group(1)
        except requests.RequestException:
            pass
    return None


def resolve_product_url(wkn):
    url = onvista_search_product_url(wkn)
    if url:
        return url
    url = google_product_url(wkn)
    if url:
        return url
    raise ValueError("Die Onvista-Produktseite zur WKN wurde nicht gefunden.")


def parse_product(html, url, requested_wkn):
    soup = BeautifulSoup(html, "html.parser")
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))

    title = soup.find("h1")
    title_text = title.get_text(" ", strip=True) if title else ""

    # Identity
    wkn = requested_wkn.upper()
    isin = None
    m = re.search(r"\b(DE[A-Z0-9]{10})\b", text, re.I)
    if m:
        isin = m.group(1).upper()

    issuer = normalize_issuer(text)

    # Direction/type
    u = (title_text + " " + text[:2500]).upper()
    direction = "long" if ("TURBO LONG" in u or "(CALL)" in u or " TYP CALL" in u) else (
        "short" if ("TURBO SHORT" in u or "(PUT)" in u or " TYP PUT" in u) else None
    )

    # Label-anchored values: do not infer from table positions.
    def labelled(label, suffix=r"(?:EUR|USD|CHF|Pkt\.)?"):
        m = re.search(
            rf"\b{label}\b\s*([0-9.\s]+,[0-9]+)\s*{suffix}",
            text, re.I
        )
        return num(m.group(1)) if m else None

    strike = labelled(r"Basispreis")
    ko = labelled(r"K\.?O\.?")
    leverage = None
    m = re.search(r"\bHebel\s+([0-9.,]+)\s*x\b", text, re.I)
    if m:
        leverage = num(m.group(1))

    ratio = None
    m = re.search(r"\bBezugsverhältnis\s+([0-9.,]+)", text, re.I)
    if m:
        ratio = num(m.group(1))

    ko_distance = None
    m = re.search(
        r"\bAbstand\s+K\.?O\.?\s+[0-9.\s]+,[0-9]+\s*(?:EUR|USD|CHF|Pkt\.)?\s*\(([0-9.,]+)\s*%\)",
        text, re.I
    )
    if m:
        ko_distance = num(m.group(1))

    # Top quote block. Restrict the search window so later tables don't win.
    bid = ask = None
    m = re.search(r"\bGeld\b.{0,100}?([0-9.\s]+,[0-9]+)\s*(?:EUR|USD|CHF)", text, re.I)
    if m:
        bid = num(m.group(1))
    m = re.search(r"\bBrief\b.{0,100}?([0-9.\s]+,[0-9]+)\s*(?:EUR|USD|CHF)", text, re.I)
    if m:
        ask = num(m.group(1))

    # Basiswert block: name + quoted underlying price.
    underlying = None
    spot = None
    m = re.search(
        r"\bBasiswert\s+(.+?)\s+"
        r"(?:Xetra|Tradegate|Nasdaq|NYSE|Stuttgart|Frankfurt|gettex|Eurex|BNP Paribas)"
        r"\s*[·|].{0,100}?([0-9.\s]+,[0-9]+)\s*(EUR|USD|CHF|Pkt\.)",
        text, re.I
    )
    if m:
        underlying = m.group(1).strip()
        spot = num(m.group(2))

    if not underlying:
        # Product title often contains "... AUF BAYER AG".
        m = re.search(r"\bAUF\s+(.+?)(?:\s+AG\b|\s+SE\b|\s+PLC\b|$)", title_text, re.I)
        if m:
            underlying = m.group(1).strip().title()

    if ko_distance is None and spot and ko:
        ko_distance = ((ko / spot - 1) * 100) if direction == "short" else ((spot / ko - 1) * 100)

    return {
        "wkn": wkn,
        "isin": isin,
        "name": title_text or f"{underlying or ''} Knock-out".strip(),
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
        "maturity": "open end" if "open end" in text.lower() else "",
        "direction": direction,
        "profileUrl": url,
    }


def load_wkn(wkn):
    url = resolve_product_url(wkn)
    r = S.get(url, timeout=20)
    r.raise_for_status()
    return parse_product(r.text, url, wkn)


@app.get("/")
def home():
    return render_template("index.html")


@app.get("/api/search")
def api_search():
    q = (request.args.get("q") or "").strip().upper()
    direction = (request.args.get("direction") or "long").lower()
    max_lev = float(request.args.get("maxLeverage") or 50)
    max_ko = float(request.args.get("maxKoDistance") or 100)

    wanted = set(request.args.getlist("issuer")) & ALLOWED_ISSUERS
    if not wanted:
        wanted = set(ALLOWED_ISSUERS)

    if not re.fullmatch(r"[A-Z0-9]{6}", q):
        return jsonify({
            "ok": False,
            "error": "Bitte eine sechsstellige WKN eingeben."
        }), 400

    try:
        p = load_wkn(q)

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
            "source": "Onvista",
            "sourceUrl": p["profileUrl"],
            "count": 1,
            "products": [p],
        })

    except requests.HTTPError as e:
        status = getattr(e.response, "status_code", "?")
        return jsonify({
            "ok": False,
            "error": f"Onvista/Suche hat den Abruf abgewiesen ({status})."
        }), 502
    except Exception as e:
        return jsonify({
            "ok": False,
            "error": "Produkt konnte nicht geladen werden.",
            "detail": str(e)
        }), 502


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
