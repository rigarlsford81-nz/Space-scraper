"""Daily listing watch.

For each company in config.json:
  1. SEC EDGAR (official): has it got a stock ticker / exchange, or has it
     filed IPO or SPAC-merger paperwork?
  2. SEC full-text search: is it named in someone else's IPO or merger
     filing (how a SPAC deal shows up)?
  3. Nasdaq IPO calendar: is it on the filed, upcoming or priced list?
  4. Google News: any recent headlines about an IPO, listing or SPAC deal?
Sends a phone notification (GitHub issue, plus ntfy if set up) whenever a
company's status moves up a level.
Writes docs/status.json (with a history of past checks) and docs/index.html.
Standard library only, so there is nothing to install.
"""
import datetime as dt
import html
import json
import os
import pathlib
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

ROOT = pathlib.Path(__file__).parent
DOCS = ROOT / "docs"
CONFIG = json.loads((ROOT / "config.json").read_text())

# Forms that mean a company is going public (or merging with a listed SPAC).
IPO_FORMS = {
    "S-1": "IPO registration filed",
    "S-1/A": "IPO registration amended",
    "F-1": "IPO registration filed (foreign issuer)",
    "F-1/A": "IPO registration amended (foreign issuer)",
    "424B4": "Final IPO prospectus, shares pricing",
    "8-A12B": "Registered to trade on an exchange",
    "S-4": "Merger registration (possible SPAC deal)",
    "425": "Merger communication (possible SPAC deal)",
    "DRSLTR": "Confidential IPO filing correspondence released",
}
NEWS_WORDS = ("ipo", "go public", "goes public", "going public", "listing",
              "listed", "spac", "nasdaq", "nyse", "s-1", "stock market debut",
              "merger", "acquire", "acquisition", "shares")


def fetch(url, browser=False):
    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36") if browser else \
        f"ListingWatch personal tool {CONFIG['contact_email']}"  # SEC requires a contact email
    req = urllib.request.Request(url, headers={
        "User-Agent": ua, "Accept": "application/json, text/xml, */*",
        "Accept-Encoding": "identity",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def check_sec(cik):
    if not cik:
        return {"ok": False, "note": "No SEC record set for this company."}
    cik10 = cik.zfill(10)
    try:
        data = json.loads(fetch(f"https://data.sec.gov/submissions/CIK{cik10}.json"))
    except Exception as e:
        return {"ok": False, "note": f"Couldn't reach the SEC today ({e.__class__.__name__})."}
    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accs = recent.get("accessionNumber", [])
    ipo_hits = []
    for form, date, acc in zip(forms, dates, accs):
        if form in IPO_FORMS:
            ipo_hits.append({
                "form": form, "date": date, "meaning": IPO_FORMS[form],
                "url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/",
            })
    latest = [{"form": f, "date": d} for f, d in list(zip(forms, dates))[:3]]
    return {
        "ok": True,
        "tickers": data.get("tickers", []),
        "exchanges": [x for x in data.get("exchanges", []) if x],
        "ipo_filings": ipo_hits,
        "latest_filings": latest,
        "edgar_url": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik10}",
    }


def check_news(query):
    q = urllib.parse.quote(query)
    url = f"https://news.google.com/rss/search?q={q}+when:30d&hl=en-NZ&gl=NZ&ceid=NZ:en"
    try:
        root = ET.fromstring(fetch(url))
    except Exception as e:
        return {"ok": False, "note": f"Couldn't reach Google News today ({e.__class__.__name__}).", "items": []}
    items = []
    for item in root.iter("item"):
        title = item.findtext("title", "")
        low = title.lower()
        items.append({
            "title": title,
            "link": item.findtext("link", ""),
            "date": item.findtext("pubDate", "")[:16],
            "relevant": any(w in low for w in NEWS_WORDS),
        })
    items.sort(key=lambda i: not i["relevant"])
    return {"ok": True, "items": items[:8]}


def check_sec_mentions(name):
    """Full-text search for the name in other companies' IPO/merger filings (last year)."""
    since = (dt.date.today()
