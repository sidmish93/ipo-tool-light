"""ECM deal items from NSE filings and a short list of newspapers."""

from __future__ import annotations

import datetime
import email.utils
import html
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

NSE_ANNOUNCEMENTS = "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml"
NSE_OFFER_DOCS = "https://nsearchives.nseindia.com/content/RSS/Offer_Documents.xml"

PRESS_SKIP = (
    "grey market",
    "gmp",
    "subscription status",
    "subscribed",
    "lists at",
    "listing gain",
    "listing loss",
    "debuts at",
    "allotment status",
)

_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class Deal:
    id: str
    kind: str
    company: str
    summary: str
    source: str
    url: str
    published: datetime.datetime | None

    def format(self) -> str:
        lines = [f"{self.kind} · {self.company}", f"{self.source} · {self.summary}"]
        if self.published:
            lines.append(self.published.astimezone(IST).strftime("%d %b %Y, %I:%M %p IST"))
        if self.url:
            lines.append(self.url)
        return "\n".join(lines)


def prospectus_kind(text: str) -> str | None:
    """DRHP, UDRHP, and RHP. Longer forms are tested first so DRHP is not read as RHP."""
    low = _SPACE.sub(" ", text or "").lower()
    if "udrhp" in low or "updated draft red herring" in low:
        return "UDRHP"
    if "drhp" in low or "draft red herring" in low:
        return "DRHP"
    if re.search(r"(?<![a-z])rhp\b", low) or "red herring prospectus" in low:
        return "RHP"
    return None


def classify(text: str) -> str | None:
    """Return a deal label, or None when the text is not an ECM launch."""
    low = " " + _SPACE.sub(" ", text or "").lower() + " "
    if "net asset value" in low or " per unit " in low or "ipo etf" in low:
        return None
    prospectus = prospectus_kind(low)
    if prospectus:
        return prospectus
    if re.search(r"\bqip\b", low) or "qualified institutional placement" in low:
        return "QIP"
    if "block deal" in low or "bulk deal" in low:
        return "Block"
    if "offer for sale" in low:
        return "OFS"
    if "rights issue" in low or "right issue" in low:
        return "Rights"
    if "preferential" in low:
        return "Preferential"
    if "buy-back" in low or "buyback" in low or "buy back" in low:
        return "Buyback"
    if (
        "initial public offer" in low
        or "public issue" in low
        or re.search(r"\bfpo\b", low)
        or "follow-on public" in low
        or "follow on public" in low
    ):
        return "IPO"
    return None


def classify_press(title: str) -> str | None:
    low = (title or "").lower()
    if any(skip in low for skip in PRESS_SKIP):
        return None
    kind = classify(title)
    if kind:
        return kind
    if re.search(r"\bipo\b", low):
        return "IPO"
    return None


def nse_deals() -> list[Deal]:
    deals = _nse_announcements()
    deals.extend(_nse_offer_docs())
    return deals


def _credible(url: str) -> bool:
    host = urllib.parse.urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    allowed = {
        "economictimes.indiatimes.com",
        "m.economictimes.indiatimes.com",
        "livemint.com",
        "business-standard.com",
        "moneycontrol.com",
        "reuters.com",
    }
    return host in allowed


def press_deals() -> list[Deal]:
    query = (
        '"qualified institutional placement" OR QIP OR "block deal" OR "bulk deal" '
        'OR "offer for sale" OR "preferential issue" OR "rights issue" '
        'OR "initial public offering" OR DRHP OR "IPO opens" when:1d'
    )
    url = (
        "https://news.google.com/rss/search?q="
        + urllib.parse.quote(query)
        + "&hl=en-IN&gl=IN&ceid=IN:en"
    )
    deals = []
    for item in _items(url):
        source_el = item.find("source")
        source_url = (source_el.get("url") if source_el is not None else "") or ""
        source_name = _text(source_el)
        if not _credible(source_url):
            continue
        title = _text(item.find("title"))
        title = re.sub(r"\s+-\s+" + re.escape(source_name) + r"$", "", title).strip()
        kind = classify_press(title)
        if not kind:
            continue
        link = _text(item.find("link")) or _text(item.find("guid"))
        deals.append(
            Deal(
                id="press:" + (link or title),
                kind=kind,
                company=title,
                summary=source_name or "Press",
                source="Press",
                url=link,
                published=_when(item),
            )
        )
    return deals


def _nse_announcements() -> list[Deal]:
    deals = []
    for item in _items(NSE_ANNOUNCEMENTS):
        company = _text(item.find("title"))
        desc = _plain(item.find("description"))
        kind = classify(company + " " + desc)
        if not kind:
            continue
        summary = desc.split("|SUBJECT:")[0].strip()
        summary = re.sub(r"^" + re.escape(company) + r"\s+has informed the Exchange (about|regarding)\s+", "", summary, flags=re.I)
        summary = summary.strip(" '\".").strip()
        if summary[:1].islower():
            summary = summary[:1].upper() + summary[1:]
        link = _text(item.find("link")) or _text(item.find("guid"))
        deals.append(
            Deal(
                id="nse:" + (_text(item.find("guid")) or link or summary),
                kind=kind,
                company=company or "NSE filing",
                summary=summary[:300] or kind,
                source="NSE filing",
                url=link,
                published=_when(item),
            )
        )
    return deals


def _nse_offer_docs() -> list[Deal]:
    deals = []
    for item in _items(NSE_OFFER_DOCS):
        company = _text(item.find("title"))
        desc = _plain(item.find("description"))
        low = desc.lower()
        if not company:
            continue
        if "disclosure document" in low or "commercial paper" in low:
            continue
        kind = prospectus_kind(low + " " + (_text(item.find("link")) or ""))
        if kind is None and not any(word in low for word in ("ipo", "fpo")):
            continue
        summary = {
            "UDRHP": "Filed a UDRHP",
            "DRHP": "Filed a DRHP",
            "RHP": "Filed an RHP",
        }.get(kind, "Filed for an IPO")
        link = _text(item.find("link")) or _text(item.find("guid"))
        deals.append(
            Deal(
                id="offer:" + (_text(item.find("guid")) or link or company or desc),
                kind=kind or classify(company + " " + desc) or "IPO",
                company=company,
                summary=summary,
                source="NSE offer document",
                url=link,
                published=_when(item),
            )
        )
    specific = {deal.company for deal in deals if deal.summary != "Filed for an IPO"}
    return [
        deal for deal in deals
        if deal.summary != "Filed for an IPO" or deal.company not in specific
    ]


def _items(url: str) -> list[ET.Element]:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=40) as response:
        root = ET.fromstring(response.read())
    return root.findall(".//item")


def _text(node: ET.Element | None) -> str:
    if node is None or node.text is None:
        return ""
    return _SPACE.sub(" ", html.unescape(node.text)).strip()


def _plain(node: ET.Element | None) -> str:
    raw = node.text if node is not None and node.text else ""
    if node is not None and not raw:
        raw = "".join(node.itertext())
    return _SPACE.sub(" ", html.unescape(_TAG.sub(" ", raw))).strip()


def _when(item: ET.Element) -> datetime.datetime | None:
    raw = _text(item.find("pubDate"))
    if not raw:
        return None
    parsed = None
    try:
        parsed = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        parsed = None
    if parsed is None:
        for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M"):
            try:
                parsed = datetime.datetime.strptime(raw, fmt).replace(tzinfo=IST)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed
