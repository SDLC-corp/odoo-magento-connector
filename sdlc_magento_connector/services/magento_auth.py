"""OAuth 1.0a request signing (RFC 5849) for the Magento 2 REST API.

Magento 2.4.4+ no longer accepts an integration access token as a standalone
Bearer token by default (``oauth/consumer/enable_integration_as_bearer``).
The supported method is to sign every request with the integration's four
credentials: Consumer Key, Consumer Secret, Access Token and Access Token
Secret. Magento accepts HMAC-SHA256 and HMAC-SHA1.

Pure standard library: no oauthlib dependency. (The Adobe IMS helper at the end,
used by Adobe Commerce as a Cloud Service, posts with ``requests``.)

Only the query string and the ``oauth_*`` protocol parameters are signed.
Magento REST bodies are JSON (not ``application/x-www-form-urlencoded``), so,
per RFC 5849 section 3.4.1.3.1, the body is never part of the signature.
"""
import base64
import hashlib
import hmac
import secrets
import time
from urllib.parse import quote, unquote, urlsplit, urlunsplit

SIGNATURE_METHODS = {
    "HMAC-SHA256": hashlib.sha256,
    "HMAC-SHA1": hashlib.sha1,
}
_DEFAULT_PORTS = {"http": 80, "https": 443}


def percent_encode(value):
    """RFC 3986 percent-encoding (RFC 5849 section 3.6): only ALPHA, DIGIT, '-', '.', '_', '~' stay."""
    if value is None:
        value = ""
    if not isinstance(value, str):
        value = str(value)
    return quote(value.encode("utf-8"), safe="-._~")


def normalize_base_url(url):
    """Base string URI (RFC 5849 section 3.4.1.2).

    Lowercase scheme and host, default port dropped, query and fragment removed.
    """
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    port = parts.port
    netloc = host if port is None or _DEFAULT_PORTS.get(scheme) == port else f"{host}:{port}"
    path = parts.path or "/"
    return urlunsplit((scheme, netloc, path, "", ""))


def _param_pairs(params):
    """Flatten a dict / list of pairs into ``(str, str)`` pairs (lists repeat the key)."""
    if not params:
        return []
    items = params.items() if isinstance(params, dict) else params
    pairs = []
    for key, value in items:
        if value is None:
            continue  # requests drops None-valued params as well
        if isinstance(value, (list, tuple)):
            pairs.extend((str(key), "" if v is None else str(v)) for v in value)
        else:
            pairs.append((str(key), str(value)))
    return pairs


def encode_query(params):
    """Encode query params the RFC 3986 way (space -> ``%20``, never ``+``).

    The connector sends this exact string, so the query Magento decodes
    (``rawurldecode``) is exactly the one that was signed.
    """
    return "&".join(f"{percent_encode(k)}={percent_encode(v)}" for k, v in _param_pairs(params))


def parse_query(query):
    """Split a raw query string into decoded pairs (strict percent-decoding, as Magento does)."""
    pairs = []
    for chunk in (query or "").split("&"):
        if not chunk:
            continue
        key, _, value = chunk.partition("=")
        pairs.append((unquote(key), unquote(value)))
    return pairs


def normalize_parameters(pairs):
    """Parameters normalization (RFC 5849 section 3.4.1.3.2)."""
    encoded = sorted((percent_encode(k), percent_encode(v)) for k, v in pairs)
    return "&".join(f"{k}={v}" for k, v in encoded)


def signature_base_string(method, url, params=None, oauth_params=None):
    """Signature base string (RFC 5849 section 3.4.1).

    ``url`` may carry a query string: it is merged with ``params``, as an HTTP
    client would do when it sends the request.
    """
    query_pairs = parse_query(urlsplit(url).query)
    pairs = list(query_pairs) + _param_pairs(params) + _param_pairs(oauth_params)
    pairs = [(k, v) for k, v in pairs if k != "oauth_signature" and k != "realm"]
    return "&".join((
        percent_encode(method.upper()),
        percent_encode(normalize_base_url(url)),
        percent_encode(normalize_parameters(pairs)),
    ))


def hmac_signature(base_string, consumer_secret, token_secret, signature_method="HMAC-SHA256"):
    digest = SIGNATURE_METHODS.get(signature_method)
    if digest is None:
        raise ValueError(f"Unsupported OAuth signature method: {signature_method}")
    key = f"{percent_encode(consumer_secret)}&{percent_encode(token_secret)}"
    raw = hmac.new(key.encode("utf-8"), base_string.encode("utf-8"), digest).digest()
    return base64.b64encode(raw).decode("ascii")


def oauth_parameters(method, url, params, consumer_key, consumer_secret, token, token_secret,
                     signature_method="HMAC-SHA256", nonce=None, timestamp=None, version="1.0"):
    """Return the signed ``oauth_*`` protocol parameters as an ordered dict."""
    oauth = {
        "oauth_consumer_key": consumer_key or "",
        "oauth_nonce": nonce or secrets.token_hex(16),
        "oauth_signature_method": signature_method,
        "oauth_timestamp": str(int(timestamp if timestamp is not None else time.time())),
    }
    if token:
        oauth["oauth_token"] = token
    if version:
        oauth["oauth_version"] = version
    base = signature_base_string(method, url, params, oauth)
    oauth["oauth_signature"] = hmac_signature(base, consumer_secret, token_secret, signature_method)
    return oauth


def sign(method, url, params, consumer_key, consumer_secret, token, token_secret,
         signature_method="HMAC-SHA256", nonce=None, timestamp=None, version="1.0"):
    """Sign one request and return the ``Authorization`` header value (``OAuth ...``).

    ``params`` are the query parameters the HTTP client will send (dict or list
    of pairs); a JSON body must NOT be passed. A fresh nonce/timestamp is used
    unless given (tests).
    """
    oauth = oauth_parameters(
        method, url, params, consumer_key, consumer_secret, token, token_secret,
        signature_method=signature_method, nonce=nonce, timestamp=timestamp, version=version,
    )
    return "OAuth " + ", ".join(f'{percent_encode(k)}="{percent_encode(v)}"' for k, v in oauth.items())


# ---------------------------------------------------------------------------
# Adobe IMS OAuth 2 server-to-server (Adobe Commerce as a Cloud Service, roadmap 3.9)
# ---------------------------------------------------------------------------
# ACCS does not accept integration (OAuth 1.0a) or admin tokens. A server-to-server
# credential from the Adobe Developer Console (client id + client secret) is exchanged
# for a bearer access token at the IMS token endpoint with ``grant_type=client_credentials``
# and the ``commerce.accs`` scope. Documented at:
#   https://developer.adobe.com/commerce/services/cloud/guides/rest/authentication
#   https://experienceleague.adobe.com/en/docs/commerce-learn/tutorials/getting-started/commerce-as-a-cloud-service/webapi-rest/customer
# The documented response is ``{"access_token": "...", "token_type": "bearer",
# "expires_in": 86399}`` (seconds, about 24 hours).
#
# Validated against Adobe's documented payloads with simulated responses only: the
# connector has not been run against a live ACCS tenant.
IMS_TOKEN_URL = "https://ims-na1.adobelogin.com/ims/token/v3"
IMS_DEFAULT_SCOPES = "openid,AdobeID,commerce.accs"
# Refresh a cached token this many seconds before it expires.
IMS_REFRESH_MARGIN = 300
# Used when IMS omits ``expires_in`` (it does not in the documented response).
IMS_DEFAULT_LIFETIME = 3600


class ImsTokenError(Exception):
    """Adobe IMS did not return a usable access token."""


def fetch_ims_token(client_id, client_secret, scopes=None, *, token_url=None, timeout=30,
                    verify=True, now=None):
    """Exchange server-to-server credentials for an IMS access token.

    ``POST https://ims-na1.adobelogin.com/ims/token/v3`` (form encoded):
    ``grant_type=client_credentials``, ``client_id``, ``client_secret``, ``scope``.

    Returns ``{"access_token", "token_type", "expires_in", "expires_at"}`` where
    ``expires_at`` is an epoch timestamp. Raises ``ImsTokenError`` with Adobe's
    ``error`` / ``error_description`` when the request is refused, and lets
    ``requests`` connection errors propagate (they are transient).
    """
    import requests  # local: keep the OAuth 1.0a helpers dependency free

    if not (client_id or "").strip() or not (client_secret or "").strip():
        raise ImsTokenError("Adobe IMS needs a client ID and a client secret.")
    data = {
        "grant_type": "client_credentials",
        "client_id": client_id.strip(),
        "client_secret": client_secret.strip(),
        "scope": (scopes or IMS_DEFAULT_SCOPES).replace(" ", ""),
    }
    response = requests.post(
        token_url or IMS_TOKEN_URL, data=data, timeout=timeout, verify=verify,
        headers={"Accept": "application/json"},
    )
    try:
        body = response.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    if response.status_code >= 400 or not body.get("access_token"):
        detail = body.get("error_description") or body.get("error") or (response.text or "").strip()[:200]
        raise ImsTokenError(
            "Adobe IMS refused the token request (HTTP %s): %s"
            % (response.status_code, detail or "no access_token in the response")
        )
    try:
        expires_in = int(body.get("expires_in") or 0)
    except (TypeError, ValueError):
        expires_in = 0
    if expires_in <= 0:
        expires_in = IMS_DEFAULT_LIFETIME
    issued = time.time() if now is None else now
    return {
        "access_token": body["access_token"],
        "token_type": body.get("token_type") or "bearer",
        "expires_in": expires_in,
        "expires_at": issued + expires_in,
    }


def ims_token_is_fresh(expires_at, now=None, margin=IMS_REFRESH_MARGIN):
    """True while a token expiring at ``expires_at`` (epoch) is usable for ``margin`` more seconds."""
    if not expires_at:
        return False
    current = time.time() if now is None else now
    return float(expires_at) - margin > current


def parse_authorization_header(header):
    """Parse an ``OAuth k="v", ...`` header back into a dict (used by tests and diagnostics)."""
    if not header or not header.startswith("OAuth "):
        return {}
    result = {}
    for part in header[len("OAuth "):].split(","):
        key, _, value = part.strip().partition("=")
        result[unquote(key)] = unquote(value.strip('"'))
    return result
