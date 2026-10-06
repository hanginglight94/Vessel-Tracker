import re
import urllib.parse
import logging
from typing import Optional, Dict, Any, Tuple

logger = logging.getLogger(__name__)

# Attempt to import curl_cffi for Cloudflare TLS fingerprint bypass
try:
    from curl_cffi import requests as c_requests
    HAS_CURL_CFFI = True
except ImportError:
    import requests as c_requests
    HAS_CURL_CFFI = False

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _http_get(url: str, timeout: int = 25):
    """Executes HTTP GET using curl_cffi Chrome impersonation if available."""
    kwargs = {"headers": HEADERS, "timeout": timeout}
    if HAS_CURL_CFFI:
        kwargs["impersonate"] = "chrome120"
    return c_requests.get(url, **kwargs)


def _search_vesselfinder(query: str) -> Optional[str]:
    """Helper to query VesselFinder search and return the first details page link."""
    from bs4 import BeautifulSoup
    encoded_q = urllib.parse.quote_plus(query)
    search_url = f"https://www.vesselfinder.com/vessels?name={encoded_q}"
    try:
        r = _http_get(search_url, timeout=12)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.find_all("a"):
                href = a.get("href", "")
                if "/vessels/" in href and "/details/" in href:
                    return href
    except Exception as e:
        logger.debug(f"Search query error for {query}: {e}")
    return None


def resolve_vessel_path(raw_input: str) -> Tuple[str, Optional[str]]:
    """
    Cleans user input (handles extra spaces, case, and missing word spaces)
    and resolves the corresponding VesselFinder details path.
    """
    # 1. Normalize spaces: trim leading/trailing and collapse multiple internal spaces
    clean = re.sub(r"\s+", " ", str(raw_input).strip())

    # 2. Split CamelCase (e.g. EverGiven -> Ever Given)
    clean = re.sub(r"([a-z])([A-Z])", r"\1 \2", clean)

    # 3. Direct search attempt
    details_path = _search_vesselfinder(clean)
    if details_path:
        return clean, details_path

    # 4. Missing space recovery:
    # If user entered a single word without spaces (e.g., 'evergiven' or 'queenprotocol'),
    # try splitting at reasonable word boundaries.
    if " " not in clean and len(clean) >= 6 and clean.isalpha():
        for i in range(3, len(clean) - 2):
            candidate = f"{clean[:i]} {clean[i:]}"
            details_path = _search_vesselfinder(candidate)
            if details_path:
                logger.info(f"Resolved missing space in '{clean}' -> '{candidate}'")
                return candidate, details_path

    # Fallback directly to details path for raw numbers (IMO/MMSI)
    if clean.isdigit():
        return clean, f"/vessels/details/{clean}"

    return clean, None


def fetch_vessel_telemetry(identifier: str) -> Optional[Dict[str, Any]]:
    """
    Looks up live vessel particulars, AIS telemetry, destination, and ETA
    from VesselFinder by MMSI, IMO, or vessel name.
    """
    if not identifier:
        return None

    from bs4 import BeautifulSoup

    # Clean input and resolve path
    cleaned_query, details_path = resolve_vessel_path(identifier)
    if not details_path:
        logger.warning(f"Could not locate vessel path for '{identifier}'")
        return None

    full_url = (
        details_path
        if details_path.startswith("http")
        else f"https://www.vesselfinder.com{details_path}"
    )

    try:
        r = _http_get(full_url)
        if r.status_code != 200:
            logger.warning(f"VesselFinder returned HTTP {r.status_code} for URL {full_url}")
            return None

        soup = BeautifulSoup(r.text, "html.parser")

        # 1. Extract vessel title
        title_el = soup.find("h1")
        name = title_el.text.strip() if title_el else cleaned_query

        # 2. Extract AIS location description paragraph (class="text2")
        desc = ""
        for p in soup.find_all("p", class_="text2"):
            desc = " ".join(p.text.split())
            break

        # 3. Extract Destination and ETA from the prominent destination banner
        destination = "Not specified"
        eta = "Not specified"

        dest_box = soup.find("div", class_="vi__r1") or soup.find("div", class_="vi__sbt")
        if dest_box:
            lines = [line.strip() for line in dest_box.text.splitlines() if line.strip()]
            for i, line in enumerate(lines):
                if line.lower() == "destination" and i + 1 < len(lines):
                    destination = lines[i + 1]
                elif "eta:" in line.lower():
                    eta_val = line.split(":", 1)[1].strip()
                    # Check if subsequent line contains relative time, e.g. "(in 2 days)"
                    if i + 1 < len(lines) and lines[i + 1].startswith("(") and lines[i + 1].endswith(")"):
                        eta_val += f" {lines[i + 1]}"
                    eta = eta_val

        # Fallback ETA parsing from summary text if missing
        if eta in ["Not specified", "-", ""]:
            eta_match = re.search(r"expected to arrive there on\s+([^\.]+)", r.text, re.IGNORECASE)
            if eta_match:
                eta = BeautifulSoup(eta_match.group(1), "html.parser").text.strip()

        # Fallback Destination parsing from summary text if missing
        if destination in ["Not specified", ""]:
            dest_match = re.search(r"en route to\s+(?:the port of\s+)?([^,]+),\s+sailing", r.text, re.IGNORECASE)
            if dest_match:
                destination = BeautifulSoup(dest_match.group(1), "html.parser").text.strip()

        # 4. Extract table particulars
        table_data: Dict[str, str] = {}
        for tr in soup.find_all("tr"):
            cols = tr.find_all(["td", "th"])
            if len(cols) >= 2:
                k = cols[0].text.strip().lower()
                v = cols[1].text.strip()
                if "speed" in k or "course" in k or "sog" in k:
                    table_data["course_speed"] = v
                elif "navigation status" in k or "status" in k:
                    table_data["status"] = v
                elif "position received" in k:
                    table_data["last_seen"] = v
                elif "ais type" in k or "ship type" in k:
                    table_data["type"] = v
                elif "ais flag" in k or "flag" in k:
                    table_data["flag"] = v
                elif "imo / mmsi" in k:
                    table_data["imo_mmsi"] = v

        # 5. Extract Speed if table value is empty/hidden
        speed = table_data.get("course_speed", "").strip()
        if not speed or speed == "-":
            speed_match = re.search(r"sailing at a speed of\s+([0-9\.]+\s*knots)", r.text, re.IGNORECASE)
            speed = speed_match.group(1).strip() if speed_match else "N/A"

        # 6. Extract Coordinates if present in HTML
        lat, lon = None, None
        coord_match = re.search(
            r"([0-9\.\-]+)\s*°?\s*([NS])\s*[/,]\s*([0-9\.\-]+)\s*°?\s*([EW])",
            r.text,
        )
        if coord_match:
            lat_val, lat_dir, lon_val, lon_dir = coord_match.groups()
            lat = float(lat_val) * (1 if lat_dir.upper() == "N" else -1)
            lon = float(lon_val) * (1 if lon_dir.upper() == "E" else -1)

        return {
            "name": name,
            "id": cleaned_query,
            "url": full_url,
            "summary": desc,
            "lat": lat,
            "lon": lon,
            "speed": speed,
            "status": table_data.get("status", "Under way"),
            "destination": destination,
            "eta": eta,
            "last_seen": table_data.get("last_seen", "Recently"),
            "type": table_data.get("type", "Vessel"),
            "flag": table_data.get("flag", "N/A"),
            "imo_mmsi": table_data.get("imo_mmsi", cleaned_query),
        }

    except Exception as e:
        logger.error(f"Error fetching telemetry for {cleaned_query}: {e}")
        return None
