"""Work out a station's provider and id from the URL of its web page."""
import html
import re
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from . import db
from .providers import USER_AGENT, get_json

MAX_PAGE_BYTES = 3 * 1024 * 1024

# Audacy's pages name a timezone the old way; these are the modern equivalents.
LEGACY_ZONES = {"US/Eastern": "America/New_York", "US/Central": "America/Chicago", "US/Mountain": "America/Denver",
                "US/Pacific": "America/Los_Angeles", "US/Alaska": "America/Anchorage", "US/Hawaii": "Pacific/Honolulu",
                "US/Arizona": "America/Phoenix"}

# iHeart's feed gives a station's city and state but no timezone. For a state that sits entirely in one zone
# the zone follows; split states (FL, TX, IN, KY, TN, MI, KS, NE, ND, SD, ID, NV, OR) are left for you to choose.
STATE_ZONES = {}
for _zone, _states in {
    "America/New_York": "CT DE DC GA ME MD MA NH NJ NY NC OH PA RI SC VT VA WV",
    "America/Chicago": "AL AR IL IA LA MN MS MO OK WI",
    "America/Denver": "CO MT NM UT WY",
    "America/Los_Angeles": "CA WA",
    "America/Phoenix": "AZ",
    "America/Anchorage": "AK",
    "Pacific/Honolulu": "HI",
}.items():
    STATE_ZONES.update(dict.fromkeys(_states.split(), _zone))


class DetectError(ValueError):
    """Carries a message that is fine to show the user."""


def _host_is(host, domain):
    return host == domain or host.endswith("." + domain)


def _fetch_page(url):
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
    with urlopen(req, timeout=12) as resp:
        final = urlparse(resp.geturl()).hostname or ""
        if not (_host_is(final, "iheart.com") or _host_is(final, "audacy.com")):
            raise DetectError("that page redirected somewhere other than iHeart or Audacy")
        return resp.read(MAX_PAGE_BYTES).decode("utf-8", "replace"), resp.geturl()


def _iheart_station(station_id):
    """The station's record from iHeart's API, or None if the id isn't a live station."""
    try:
        data = get_json("https://us.api.iheart.com/api/v2/content/liveStations/%d" % int(station_id))
    except (OSError, ValueError):
        return None
    hits = data.get("hits") or []
    return hits[0] if hits and str(hits[0].get("id")) == str(station_id) else None


def _detect_iheart(url, host, path):
    # www.iheart.com/live/97-1-wash-fm-2501/ puts the id at the end of the path.
    match = re.search(r"/live/(?:[a-z0-9-]*-)?(\d{2,8})/?$", path)
    candidates = [match.group(1)] if match else []
    website = host
    if not candidates:
        # A station subdomain like washfm.iheart.com: its page mentions the id near the top.
        page, _ = _fetch_page(url)
        candidates = list(dict.fromkeys(re.findall(r'"id":(\d{3,6})', page)))[:8]
    for candidate in candidates:
        record = _iheart_station(candidate)
        if not record:
            continue
        # For a subdomain page, only accept the station whose website is that subdomain.
        if host != "www.iheart.com" and record.get("website", "").lower() != website:
            continue
        markets = record.get("markets") or []
        market = next((m for m in markets if m.get("primary")), markets[0] if markets else {})
        return {"provider": "iheart", "provider_id": str(record["id"]), "name": record.get("name") or "",
                "timezone": STATE_ZONES.get(market.get("stateAbbreviation"))}
    raise DetectError("couldn't find an iHeart station on that page. For www.iheart.com, use the station's "
                      "own page (it looks like iheart.com/live/<name>-<number>/); or enter the details by hand.")


def _detect_audacy(url):
    page, _ = _fetch_page(url)
    match = (re.search(r"STATION_ROC_ID\s*=\s*['\"]([0-9]+-[0-9]+)['\"]", page)
             or re.search(r"\ba2Id\s*=\s*['\"]([0-9]+-[0-9]+)['\"]", page))
    if not match:
        raise DetectError("couldn't find an Audacy station code on that page. Use the station's own page, "
                          "such as audacy.com/kroq, or enter the details by hand.")
    title = re.search(r"<title>([^<]{1,200})</title>", page)
    name = html.unescape(title.group(1)).split("|")[-1].strip() if title else ""
    zone = re.search(r"const timezone\s*=\s*[\"']([A-Za-z_/]+)[\"']", page)
    zone = LEGACY_ZONES.get(zone.group(1), zone.group(1)) if zone else None
    return {"provider": "audacy", "provider_id": match.group(1), "name": name[:60], "timezone": db.valid_timezone(zone)}


def detect(url):
    """Returns {"provider", "provider_id", "name", "timezone"} for an iHeart or Audacy station page.
    The timezone is None when the page doesn't make it clear."""
    url = (url or "").strip()
    if url and "://" not in url:
        url = "https://" + url
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not host:
        raise DetectError("that doesn't look like a web address")
    if _host_is(host, "iheart.com"):
        return _detect_iheart(url, host, parsed.path)
    if _host_is(host, "audacy.com"):
        return _detect_audacy(url)
    raise DetectError("I can only read iHeart and Audacy station pages. For another kind of station, "
                      "a provider has to be added to the app first.")
