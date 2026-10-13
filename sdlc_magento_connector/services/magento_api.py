import http.client
import json
import logging
import os
import re
import socket
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from itertools import islice

import requests
from requests.compat import urlparse, urlunparse
from requests.utils import quote
from urllib3 import exceptions as urllib3_exceptions

from odoo.exceptions import UserError

from . import magento_auth

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Adobe Commerce as a Cloud Service (ACCS, roadmap 3.9)
# ---------------------------------------------------------------------------
# ACCS REST URLs are ``https://<server>.api.commerce.adobe.com/<tenant-id>/V1/<endpoint>``:
# no ``/rest`` and no store code in the path; the store scope travels in the ``Store``
# HTTP header (``all``, ``default`` or a store view code). Async bulk is
# ``/<tenant-id>/V1/async/bulk/<route>`` (``async/bulk`` after ``V1``, not before).
#   https://developer.adobe.com/commerce/webapi/rest/  (SaaS section)
#   https://developer.adobe.com/commerce/webapi/rest/use-rest/bulk-endpoints/
# Every PaaS path the connector builds (``/rest[/<store>][/async/bulk]/V1/...``) is
# rewritten in ``MagentoAPI._request`` when ``instance.platform == 'accs'``; nothing
# changes for Magento Open Source / Adobe Commerce PaaS.
#
# Validated against Adobe's documented API with simulated responses only; not yet
# tested against a live ACCS tenant.
PLATFORM_ACCS = "accs"
_PAAS_REST_PATH = re.compile(
    r"^/rest(?:/(?P<store>[^/?]+))?/(?P<bulk>async/bulk/)?V1(?P<rest>(?:[/?].*)?)$"
)
# The guest and customer (self-service) REST APIs do not exist on ACCS (Adobe: "use the
# GraphQL APIs"), and the bulk/async form of POST /V1/customers is not supported.
ACCS_UNAVAILABLE = (
    (re.compile(r"^V1/guest-carts(?:[/?]|$)"), "guest_cart",
     "the guest cart REST API (/V1/guest-carts), used to export Odoo orders to Magento"),
    (re.compile(r"^V1/(?:carts|customers)/mine(?:[/?]|$)"), "customer_self",
     "the customer self-service REST API (/V1/carts/mine, /V1/customers/me)"),
    (re.compile(r"^V1/customers/me(?:[/?]|$)"), "customer_self",
     "the customer self-service REST API (/V1/carts/mine, /V1/customers/me)"),
    (re.compile(r"^V1/async/bulk/customers(?:[/?]|$)"), "bulk_customers",
     "the async/bulk form of POST /V1/customers"),
)


class MagentoCapabilityError(UserError):
    """The Magento platform of the instance does not offer this REST API.

    Raised before any HTTP request, so a feature that cannot work (e.g. the guest cart
    order export on Adobe Commerce as a Cloud Service) fails with a clear message
    instead of an obscure 404.
    """

    def __init__(self, message, capability=None):
        super().__init__(message)
        self.capability = capability


class MagentoAPI:
    # Transient failures retried with exponential backoff (Retry-After honoured).
    MAX_RETRIES = 3
    BACKOFF_BASE = 1.0  # seconds: 1, 2, 4
    MAX_BACKOFF = 30.0  # never sleep longer than this per attempt
    RETRY_STATUSES = frozenset({429, 502, 503, 504})
    # For non-idempotent calls (POST) only retry when Magento certainly did not
    # process the request: rate limiting / unavailable, or connection never made.
    RETRY_STATUSES_UNSAFE_METHODS = frozenset({429, 503})
    IDEMPOTENT_METHODS = frozenset({"get", "put", "delete", "head", "options"})
    TIMEOUT = 60

    def __init__(self, instance):
        self.instance = instance
        self.base_url = instance.base_url.rstrip("/")
        # Adobe Commerce as a Cloud Service: tenant URLs, Store header, IMS bearer token.
        self.platform = getattr(instance, "platform", None) or "open_source_paas"
        self.accs_tenant = str(getattr(instance, "accs_tenant_id", "") or "").strip().strip("/")
        self._instance_sudo = instance.sudo()
        self._store_code = None
        self._guest_cart_rest_prefix = "/rest/V1"
        self.headers = {"Content-Type": "application/json"}
        # Credentials are restricted to Magento managers (field ``groups``). Syncs may
        # be run by Magento users or cron, so read only the secrets with sudo.
        self._auth = self._load_auth(instance.sudo())

        self.verify = bool(instance.verify_ssl)
        self._container_gateway_ip = self._detect_container_gateway_ip()
        self._base_urls = self._build_base_urls()

    # ------------------------------------------------------------------
    # Authentication (OAuth 1.0a signing or legacy Bearer token)
    # ------------------------------------------------------------------
    @staticmethod
    def _load_auth(instance_sudo):
        def _secret(name):
            value = getattr(instance_sudo, name, None)
            return value.strip() if isinstance(value, str) else ""

        # Instances created before OAuth support have no auth_method: they keep Bearer.
        auth_method = getattr(instance_sudo, "auth_method", None) or "bearer"
        signature = getattr(instance_sudo, "oauth_signature_method", None) or "sha256"
        return {
            "method": auth_method,
            "token": _secret("access_token"),
            "consumer_key": _secret("consumer_key"),
            "consumer_secret": _secret("consumer_secret"),
            "token_secret": _secret("access_token_secret"),
            "signature_method": "HMAC-SHA1" if signature == "sha1" else "HMAC-SHA256",
        }

    def _auth_prepare_query(self, endpoint, params):
        """OAuth 1.0a: append the query to the URL ourselves (RFC 3986, a space is
        ``%20``, never ``+``) so the signed query is byte for byte what Magento
        receives. Bearer requests keep passing ``params`` to requests."""
        if self._auth["method"] != "oauth1" or not params:
            return endpoint, params
        query = magento_auth.encode_query(params)
        if not query:
            return endpoint, None
        separator = "&" if "?" in endpoint else "?"
        return f"{endpoint}{separator}{query}", None

    def _auth_header(self, method, url, params=None):
        """``Authorization`` value for one request; OAuth signs every call with a
        fresh nonce/timestamp (so each retry and fallback URL is re-signed)."""
        auth = self._auth
        if auth["method"] == "ims":
            # Adobe IMS (ACCS): bearer token fetched / refreshed near expiry, see _ims_token.
            token = self._ims_token()
            return f"Bearer {token}" if token else None
        if auth["method"] == "oauth1":
            return magento_auth.sign(
                method, url, params,
                auth["consumer_key"], auth["consumer_secret"],
                auth["token"], auth["token_secret"],
                signature_method=auth["signature_method"],
            )
        if auth["token"]:
            return f"Bearer {auth['token']}"
        return None

    def _request_headers(self, method, url, params=None):
        headers = dict(self.headers)
        authorization = self._auth_header(method, url, params)
        if authorization:
            headers["Authorization"] = authorization
        return headers

    # ------------------------------------------------------------------
    # Adobe IMS token (auth_method ``ims``) and ACCS URL building (roadmap 3.9)
    # ------------------------------------------------------------------
    def _ims_token(self, force_refresh=False):
        """Cached Adobe IMS access token of the instance, fetched or refreshed near its
        expiry by ``magento.instance._magento_ims_token``."""
        getter = getattr(self._instance_sudo, "_magento_ims_token", None)
        if getter is None:
            return ""
        return getter(force_refresh=force_refresh) or ""

    @property
    def is_accs(self):
        return self.platform == PLATFORM_ACCS

    def _accs_tenant_prefix(self):
        """``/<tenant>``, or ``""`` when the base URL already ends with the tenant id."""
        if not self.accs_tenant:
            raise MagentoCapabilityError(
                "Adobe Commerce as a Cloud Service needs the tenant ID of the instance "
                "(the last part of the REST endpoint shown in Cloud Manager)."
            )
        if self.base_url.lower().endswith("/" + self.accs_tenant.lower()):
            return ""
        return "/" + quote(self.accs_tenant, safe="")

    def _accs_endpoint(self, endpoint):
        """``(endpoint, extra headers)`` of a PaaS path on ACCS.

        ``/rest/V1/x`` -> ``/<tenant>/V1/x``; ``/rest/<store>/V1/x`` -> ``/<tenant>/V1/x``
        with ``Store: <store>``; ``/rest/<store>/async/bulk/V1/<route>`` ->
        ``/<tenant>/V1/async/bulk/<route>`` with ``Store: <store>``. Other paths are kept.
        Raises ``MagentoCapabilityError`` for the REST APIs ACCS does not offer.
        """
        match = _PAAS_REST_PATH.match(endpoint or "")
        if not match:
            return endpoint, None
        rest = match.group("rest") or ""
        if match.group("bulk"):
            path = "V1/async/bulk" + rest
        else:
            path = "V1" + rest
        for pattern, capability, label in ACCS_UNAVAILABLE:
            if pattern.match(path):
                raise MagentoCapabilityError(
                    "Not available on Adobe Commerce as a Cloud Service: %s. Adobe does not "
                    "offer this REST API on ACCS (the guest and customer REST APIs are "
                    "replaced by GraphQL there)." % label,
                    capability=capability,
                )
        store = match.group("store")
        headers = {"Store": store} if store else None
        return "%s/%s" % (self._accs_tenant_prefix(), path), headers

    def _is_containerized_runtime(self):
        # Docker/Podman usually expose one of these markers in Linux containers.
        if os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"):
            return True
        try:
            with open("/proc/1/cgroup", "r", encoding="utf-8") as handle:
                data = handle.read().lower()
            return any(token in data for token in ("docker", "podman", "containerd", "kubepods"))
        except OSError:
            return False

    def _detect_container_gateway_ip(self):
        if not self._is_containerized_runtime():
            return None

        # Resolve the container default gateway from /proc/net/route.
        try:
            with open("/proc/net/route", "r", encoding="utf-8") as handle:
                for line in handle.readlines()[1:]:
                    parts = line.strip().split()
                    if len(parts) < 3:
                        continue
                    destination = parts[1]
                    gateway_hex = parts[2]
                    if destination != "00000000" or gateway_hex == "00000000":
                        continue
                    try:
                        octets = [str(int(gateway_hex[idx:idx + 2], 16)) for idx in (6, 4, 2, 0)]
                    except ValueError:
                        return None
                    return ".".join(octets)
        except OSError:
            return None
        return None

    def _build_base_urls(self):
        base_urls = [self.base_url]
        parsed = urlparse(self.base_url)
        host = (parsed.hostname or "").strip().lower()
        if host not in {"localhost", "127.0.0.1", "::1"}:
            return base_urls

        # Only add container-to-host fallbacks when Odoo itself runs in a container.
        if not self._is_containerized_runtime():
            return base_urls

        # Keep localhost usable in common deployment layouts:
        # - native host: localhost / 127.0.0.1 / ::1
        # - docker/podman container -> host machine:
        #   host.docker.internal / host.containers.internal / gateway.docker.internal
        # - container default gateway detected from /proc/net/route
        shared_fallback_hosts = [
            "host.docker.internal",
            "host.containers.internal",
            "gateway.docker.internal",
        ]
        if self._container_gateway_ip:
            shared_fallback_hosts.append(self._container_gateway_ip)

        fallback_hosts = {
            "localhost": [
                "127.0.0.1",
                "::1",
                *shared_fallback_hosts,
            ],
            "127.0.0.1": [
                "localhost",
                "::1",
                *shared_fallback_hosts,
            ],
            "::1": [
                "localhost",
                "127.0.0.1",
                *shared_fallback_hosts,
            ],
        }.get(host, [])

        auth = ""
        if parsed.username:
            auth = parsed.username
            if parsed.password is not None:
                auth += f":{parsed.password}"
            auth += "@"
        port = f":{parsed.port}" if parsed.port else ""

        for fallback_host in fallback_hosts:
            netloc_host = fallback_host
            if ":" in netloc_host and not netloc_host.startswith("["):
                netloc_host = f"[{netloc_host}]"
            fallback_netloc = f"{auth}{netloc_host}{port}"
            fallback_url = urlunparse(
                (parsed.scheme, fallback_netloc, parsed.path, parsed.params, parsed.query, parsed.fragment)
            ).rstrip("/")
            if fallback_url not in base_urls:
                base_urls.append(fallback_url)
        return base_urls

    def _ssl_error(self, url, exc):
        """Build a clear SSL error. We never silently fall back to verify=False."""
        return requests.exceptions.SSLError(
            f"SSL certificate verification failed for {url}: {exc}. "
            "If this Magento server uses a self-signed or internal certificate, either install "
            "its CA certificate on the Odoo server or untick 'Verify SSL' (verify_ssl) on the "
            "Magento instance.",
            request=getattr(exc, "request", None),
            response=getattr(exc, "response", None),
        )

    def _retry_delay(self, attempt, response=None):
        """Seconds to wait before retry ``attempt`` (0-based); honours Retry-After."""
        delay = self.BACKOFF_BASE * (2 ** attempt)
        retry_after = response.headers.get("Retry-After") if response is not None else None
        if retry_after:
            retry_after = retry_after.strip()
            try:
                delay = float(retry_after)
            except ValueError:
                try:
                    when = parsedate_to_datetime(retry_after)
                    if when.tzinfo is None:
                        when = when.replace(tzinfo=timezone.utc)
                    delay = (when - datetime.now(timezone.utc)).total_seconds()
                except (TypeError, ValueError, IndexError):
                    pass
        return max(0.0, min(delay, self.MAX_BACKOFF))

    @staticmethod
    def _connection_never_established(exc):
        """True only when ``exc`` proves the request never reached Magento.

        That is a connect timeout, or a failure to open the connection (urllib3
        ``NewConnectionError``, which covers refused connections and, as
        ``NameResolutionError``, DNS failures), possibly wrapped in ``MaxRetryError``.
        Anything else (``ProtocolError`` "Connection aborted", ``RemoteDisconnected``,
        read timeouts, unknown errors) may come after the request was sent, so it
        is treated as unsafe to repeat for a non-idempotent call.
        """
        if isinstance(exc, requests.exceptions.ConnectTimeout):
            return True
        if isinstance(exc, requests.exceptions.Timeout):
            return False
        seen = set()
        pending = [exc]
        while pending:
            err = pending.pop()
            if err is None or id(err) in seen:
                continue
            seen.add(id(err))
            if isinstance(err, (urllib3_exceptions.ProtocolError, http.client.RemoteDisconnected,
                                urllib3_exceptions.ReadTimeoutError)):
                return False
            if isinstance(err, (urllib3_exceptions.NewConnectionError, socket.gaierror, ConnectionRefusedError)):
                return True
            pending.append(getattr(err, "reason", None))
            pending.append(err.__cause__)
            pending.append(err.__context__)
            pending.extend(arg for arg in getattr(err, "args", ()) if isinstance(arg, BaseException))
        return False

    def _sleep(self, seconds):
        if seconds > 0:
            time.sleep(seconds)

    def _request(self, method, endpoint, *, params=None, payload=None):
        """Send one HTTP request, trying the container fallback hosts, retrying
        transient failures (429/502/503/504, connection errors, timeouts) up to
        ``MAX_RETRIES`` times with exponential backoff.

        On Adobe Commerce as a Cloud Service the PaaS path is first rewritten to the
        tenant URL (``_accs_endpoint``). With Adobe IMS auth a 401 forces one token
        refresh and one retry (the cached token may have been revoked early)."""
        extra_headers = None
        if self.is_accs:
            endpoint, extra_headers = self._accs_endpoint(endpoint)
        try:
            return self._send_request(method, endpoint, params=params, payload=payload,
                                      extra_headers=extra_headers)
        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            if status != 401 or self._auth["method"] != "ims":
                raise
            _logger.info("Magento %s %s: HTTP 401 with an IMS token, refreshing it once",
                         (method or "get").upper(), endpoint)
            if not self._ims_token(force_refresh=True):
                raise
            return self._send_request(method, endpoint, params=params, payload=payload,
                                      extra_headers=extra_headers)

    def _send_request(self, method, endpoint, *, params=None, payload=None, extra_headers=None):
        """One final path of ``_request``: fallback hosts, retries and backoff."""
        method = (method or "get").lower()
        idempotent = method in self.IDEMPOTENT_METHODS
        retry_statuses = self.RETRY_STATUSES if idempotent else self.RETRY_STATUSES_UNSAFE_METHODS
        endpoint, params = self._auth_prepare_query(endpoint, params)
        request_kwargs = {
            "timeout": self.TIMEOUT,
            "verify": self.verify,
        }
        if params is not None:
            request_kwargs["params"] = params
        if payload is not None:
            request_kwargs["json"] = payload

        for attempt in range(self.MAX_RETRIES + 1):
            primary_exc = None
            last_exc = None
            retry_response = None
            retryable = False
            for base_url in list(self._base_urls):
                url = f"{base_url}{endpoint}"
                request_kwargs["headers"] = self._request_headers(method, url, params)
                if extra_headers:
                    request_kwargs["headers"].update(extra_headers)
                _logger.debug("Magento %s %s (attempt %s)", method.upper(), url, attempt + 1)
                try:
                    response = requests.request(method, url, **request_kwargs)
                except requests.exceptions.SSLError as exc:
                    # SSLError subclasses ConnectionError: handle first, never retry, never downgrade.
                    raise self._ssl_error(url, exc) from exc
                except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
                    if base_url == self.base_url and primary_exc is None:
                        primary_exc = exc
                    last_exc = exc
                    # A POST that failed after the request was sent (read timeout,
                    # "Connection aborted", RemoteDisconnected...) may have been applied by
                    # Magento: retrying it could create a duplicate invoice/shipment/order.
                    if idempotent or self._connection_never_established(exc):
                        retryable = True
                    continue

                # Remember a working fallback host so later calls try it first.
                if base_url != self._base_urls[0]:
                    self._base_urls.remove(base_url)
                    self._base_urls.insert(0, base_url)

                if response.status_code in retry_statuses and attempt < self.MAX_RETRIES:
                    retry_response = response
                    retryable = True
                    break
                response.raise_for_status()
                return response

            if attempt >= self.MAX_RETRIES or not retryable:
                break
            delay = self._retry_delay(attempt, retry_response)
            _logger.warning(
                "Magento %s %s: transient failure (%s), retry %s/%s in %.1fs",
                method.upper(),
                endpoint,
                retry_response.status_code if retry_response is not None else type(last_exc).__name__,
                attempt + 1,
                self.MAX_RETRIES,
                delay,
            )
            self._sleep(delay)

        if primary_exc is not None:
            raise primary_exc
        if last_exc is not None:
            raise last_exc
        raise requests.exceptions.RequestException("Magento request failed without response.")

    @staticmethod
    def _json(response):
        """Decode a JSON body; empty bodies (e.g. 204) decode to ``{}``."""
        if response is None or response.status_code == 204 or not (response.content or b"").strip():
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise requests.exceptions.InvalidJSONError(
                f"Magento returned a non-JSON response (HTTP {response.status_code}) for {response.url}",
                response=response,
            ) from exc

    def _get(self, endpoint, params=None):
        return self._json(self._request("get", endpoint, params=params))

    def _post(self, endpoint, payload=None):
        return self._json(self._request("post", endpoint, payload=payload))

    def _put(self, endpoint, payload=None):
        return self._json(self._request("put", endpoint, payload=payload))

    def _resolve_store_code(self):
        if self._store_code is not None:
            return self._store_code

        self._store_code = ""
        explicit_code = str(getattr(self.instance, "store_code", "") or "").strip()
        if explicit_code:
            self._store_code = explicit_code
            return self._store_code

        try:
            store_id = int(getattr(self.instance, "store_id", 0) or 0)
        except (TypeError, ValueError):
            store_id = 0
        if not store_id:
            return self._store_code

        try:
            store_configs = self.get_store_configs() or []
        except Exception:
            return self._store_code

        for config in store_configs:
            candidate_id = config.get("id")
            if candidate_id is None:
                candidate_id = config.get("store_id")
            try:
                candidate_id = int(candidate_id)
            except (TypeError, ValueError):
                continue
            if candidate_id != store_id:
                continue
            code = (config.get("code") or config.get("store_code") or "").strip()
            if code:
                self._store_code = code
                break
        return self._store_code

    def _get_store_rest_prefix(self):
        store_code = self._resolve_store_code()
        if store_code:
            return f"/rest/{quote(store_code, safe='')}/V1"
        return "/rest/V1"

    def _guest_cart_endpoint(self, suffix=""):
        suffix = str(suffix or "")
        if suffix and not suffix.startswith("/"):
            suffix = "/" + suffix
        return f"{self._guest_cart_rest_prefix}/guest-carts{suffix}"

    def _rest_prefix_candidates(self, include_all_scope=False):
        prefixes = []
        if include_all_scope:
            prefixes.append("/rest/all/V1")
        store_prefix = self._get_store_rest_prefix()
        if store_prefix:
            prefixes.append(store_prefix)
        prefixes.append("/rest/V1")
        unique = []
        seen = set()
        for prefix in prefixes:
            if prefix in seen:
                continue
            seen.add(prefix)
            unique.append(prefix)
        return unique

    def _request_with_prefix_fallback(
        self,
        method,
        endpoint_builder,
        *,
        include_all_scope=False,
        payload=None,
        params=None,
    ):
        last_exc = None
        prefixes = self._rest_prefix_candidates(include_all_scope=include_all_scope)
        for idx, prefix in enumerate(prefixes):
            endpoint = endpoint_builder(prefix)
            try:
                if method == "get":
                    return self._get(endpoint, params=params)
                if method == "post":
                    return self._post(endpoint, payload)
                if method == "put":
                    return self._put(endpoint, payload)
                raise ValueError(f"Unsupported HTTP method: {method}")
            except requests.exceptions.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else 0
                # 401/403 are real auth/permission errors: surface them, never mask them
                # by retrying another store prefix.
                can_retry = idx < (len(prefixes) - 1) and status in (400, 404)
                if can_retry:
                    last_exc = exc
                    continue
                raise
        if last_exc:
            raise last_exc
        raise requests.exceptions.HTTPError("Magento request failed without HTTP response.")

    # ------------------------------------------------------------------
    # Paginated searchCriteria
    # ------------------------------------------------------------------
    MAX_PAGE_SIZE = 300  # Magento rejects searchCriteria[pageSize] > 300
    DEFAULT_PAGE_SIZE = 100
    DEFAULT_SORT = (("updated_at", "ASC"), ("entity_id", "ASC"))
    # Key of the record id in each search *result* (used to detect a repeated
    # page). Sorting uses the collection column ``entity_id`` for orders,
    # products and customers alike (an EAV product collection has no ``id``
    # column), but product and customer results expose it as ``id``.
    ID_KEYS = {
        "order": "entity_id",
        "product": "id",
        "customer": "id",
        "invoice": "entity_id",
        "creditmemo": "entity_id",
        "shipment": "entity_id",
        "attribute_set": "attribute_set_id",
    }
    # Defensive hard stop against a server that never ends a listing.
    MAX_PAGES = 100000

    @classmethod
    def clamp_page_size(cls, page_size):
        """Page size within [1, 300] (Magento's maximum searchCriteria pageSize)."""
        try:
            page_size = int(page_size or cls.DEFAULT_PAGE_SIZE)
        except (TypeError, ValueError):
            page_size = cls.DEFAULT_PAGE_SIZE
        return max(1, min(cls.MAX_PAGE_SIZE, page_size))

    @staticmethod
    def build_search_params(filters=None, sort=None, page_size=None, current_page=None, fields=None):
        """Flatten a search into Magento ``searchCriteria`` query parameters.

        ``filters`` is a list of filter groups: groups are ANDed, the filters of
        one group are ORed. A group is one ``(field, value[, condition])`` tuple
        or a list of them. ``sort`` is a sequence of ``(field, "ASC"|"DESC")``.
        """
        params = {}
        for g_idx, group in enumerate(filters or ()):
            if group and isinstance(group[0], str):
                group = [group]
            for f_idx, flt in enumerate(group):
                condition = flt[2] if len(flt) > 2 and flt[2] else "eq"
                prefix = "searchCriteria[filter_groups][%d][filters][%d]" % (g_idx, f_idx)
                params[prefix + "[field]"] = flt[0]
                params[prefix + "[value]"] = "" if flt[1] is None else str(flt[1])
                params[prefix + "[condition_type]"] = condition
        for s_idx, (field, direction) in enumerate(sort or ()):
            params["searchCriteria[sortOrders][%d][field]" % s_idx] = field
            params["searchCriteria[sortOrders][%d][direction]" % s_idx] = (direction or "ASC").upper()
        if page_size is not None:
            params["searchCriteria[pageSize]"] = int(page_size)
        if current_page is not None:
            params["searchCriteria[currentPage]"] = int(current_page)
        if fields:
            params["fields"] = fields
        if not params:
            params["searchCriteria"] = ""
        return params

    def iter_search(self, endpoint, *, filters=None, sort=DEFAULT_SORT, page_size=DEFAULT_PAGE_SIZE,
                    fields=None, id_key="entity_id", stats=None, include_all_scope=True):
        """Yield every item of a Magento search endpoint, fetching page by page.

        ``endpoint`` is a path (``/rest/V1/orders``) or a callable ``prefix -> path``
        (store-code prefix fallback, as the product endpoints use).

        Stops on an empty page, a short page, once ``total_count`` items were
        yielded, or when Magento echoes a page already seen: for a ``currentPage``
        past the end Magento returns the last page again, so a page whose ids
        (``id_key``) were all yielded already ends the listing. An item is never
        yielded twice. ``stats`` (a dict), when given, receives ``pages``,
        ``items`` and ``total_count``.
        """
        page_size = self.clamp_page_size(page_size)
        if stats is None:
            stats = {}
        stats.setdefault("pages", 0)
        stats.setdefault("items", 0)
        stats.setdefault("total_count", None)
        seen = set()
        yielded = 0
        current_page = 1
        while current_page <= self.MAX_PAGES:
            params = self.build_search_params(
                filters=filters, sort=sort, page_size=page_size, current_page=current_page, fields=fields,
            )
            if callable(endpoint):
                response = self._request_with_prefix_fallback(
                    "get", endpoint, include_all_scope=include_all_scope, params=params,
                )
            else:
                response = self._get(endpoint, params=params)
            if isinstance(response, list):
                items = response
            elif isinstance(response, dict):
                items = response.get("items") or []
                try:
                    if response.get("total_count") is not None:
                        stats["total_count"] = int(response["total_count"])
                except (TypeError, ValueError):
                    pass
            else:
                items = []
            stats["pages"] += 1
            if not items:
                break
            fresh = []
            for item in items:
                key = item.get(id_key) if isinstance(item, dict) else None
                key = repr(item) if key is None else str(key)
                if key in seen:
                    continue
                seen.add(key)
                fresh.append(item)
            if not fresh:
                break  # Magento echoed the last page for a page past the end.
            for item in fresh:
                yielded += 1
                stats["items"] = yielded
                yield item
            if stats["total_count"] is not None and yielded >= stats["total_count"]:
                break
            if len(items) < page_size:
                break
            current_page += 1

    def _search_list(self, endpoint, *, updated_from=None, filters=None, page_size=None, lazy=False,
                     stats=None, limit=None, sort=DEFAULT_SORT, id_key="entity_id"):
        """Shared body of the ``get_<entity>s`` list getters.

        ``updated_from`` (naive UTC datetime or ``YYYY-MM-DD HH:MM:SS``) adds an
        ``updated_at gteq`` filter. ``lazy=True`` returns a generator (pages are
        fetched while it is consumed), otherwise a list. ``limit`` caps the items.
        """
        filters = list(filters or ())
        if updated_from:
            if isinstance(updated_from, datetime):
                updated_from = updated_from.strftime("%Y-%m-%d %H:%M:%S")
            filters.append(("updated_at", str(updated_from), "gteq"))
        page_size = self.clamp_page_size(page_size)
        if limit:
            page_size = min(page_size, max(1, int(limit)))
        iterator = self.iter_search(
            endpoint, filters=filters, sort=sort, page_size=page_size, id_key=id_key, stats=stats,
        )
        if limit:
            iterator = islice(iterator, max(1, int(limit)))
        return iterator if lazy else list(iterator)

    def add_order_comment(self, order_id, comment="", status=None, notify=False):
        """Add a status-history comment to a Magento order (optionally changing its status).

        ``POST /V1/orders/{order_id}/comments``. This is the supported way to change an
        order status; it does not rewrite the order like ``PUT /V1/orders`` does.
        """
        status_history = {
            "comment": comment or "",
            "is_customer_notified": int(bool(notify)),
            "is_visible_on_front": 0,
        }
        if status:
            status_history["status"] = status
        payload = {"statusHistory": status_history}
        return self._request_with_prefix_fallback(
            "post",
            lambda prefix: f"{prefix}/orders/{order_id}/comments",
            payload=payload,
        )

    def get_store_configs(self):
        return self._get("/rest/V1/store/storeConfigs")

    def get_websites(self):
        return self._get("/rest/V1/store/websites")

    def get_products(self, updated_from=None, *, page_size=None, lazy=False, stats=None, limit=None,
                     filters=None):
        """Products (search result rows), paged; see ``_search_list``."""
        return self._search_list(
            lambda prefix: f"{prefix}/products",
            updated_from=updated_from, filters=filters, page_size=page_size, lazy=lazy,
            stats=stats, limit=limit, id_key=self.ID_KEYS["product"],
        )

    def _search_products(self, params):
        response = self._request_with_prefix_fallback(
            "get",
            lambda prefix: f"{prefix}/products",
            include_all_scope=True,
            params=params,
        )
        if isinstance(response, dict):
            return response.get("items", [])
        return []

    def get_product_by_id(self, product_id):
        try:
            pid = int(product_id)
        except (TypeError, ValueError):
            return {}

        filter_templates = (
            {"field": "id", "value": str(pid), "condition_type": "eq"},
            {"field": "entity_id", "value": str(pid), "condition_type": "eq"},
        )
        for flt in filter_templates:
            params = {
                "searchCriteria[filter_groups][0][filters][0][field]": flt["field"],
                "searchCriteria[filter_groups][0][filters][0][value]": flt["value"],
                "searchCriteria[filter_groups][0][filters][0][condition_type]": flt["condition_type"],
            }
            try:
                items = self._search_products(params)
            except requests.exceptions.HTTPError:
                items = []
            if items:
                return items[0]
        return {}

    def get_attribute_sets(self, *, page_size=None):
        return self._search_list(
            "/rest/V1/products/attribute-sets/sets/list",
            page_size=page_size,
            sort=(("attribute_set_id", "ASC"),),
            id_key=self.ID_KEYS["attribute_set"],
        )

    def create_attribute_set(self, name, skeleton_id=4):
        payload = {
            "attributeSet": {
                "attribute_set_name": name,
                "sort_order": 0,
            },
            "skeletonId": int(skeleton_id),
        }
        return self._post("/rest/V1/products/attribute-sets", payload)

    def update_attribute_set(self, attribute_set_id, name):
        payload = {
            "attributeSet": {
                "attribute_set_id": int(attribute_set_id),
                "attribute_set_name": name,
            }
        }
        return self._put(f"/rest/V1/products/attribute-sets/{attribute_set_id}", payload)

    def get_orders(self, updated_from=None, *, page_size=None, lazy=False, stats=None, limit=None,
                   filters=None):
        """Orders, paged and sorted by updated_at then entity_id; see ``_search_list``."""
        return self._search_list(
            "/rest/V1/orders",
            updated_from=updated_from, filters=filters, page_size=page_size, lazy=lazy,
            stats=stats, limit=limit, id_key=self.ID_KEYS["order"],
        )

    def _get_order_documents(self, endpoint, kind, order_id=None, page_size=None):
        """Invoices / credit memos / shipments, optionally of one order, paged."""
        filters = [("order_id", str(order_id), "eq")] if order_id else None
        return self._search_list(
            endpoint, filters=filters, page_size=page_size,
            sort=(("entity_id", "ASC"),), id_key=self.ID_KEYS[kind],
        )

    def get_order(self, order_id):
        return self._get(f"/rest/V1/orders/{order_id}")

    def get_order_transactions(self, order_id):
        payload = self._get(f"/rest/V1/orders/{order_id}/transactions")
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            items = payload.get("items")
            if isinstance(items, list):
                return items
            transactions = payload.get("transactions")
            if isinstance(transactions, list):
                return transactions
        return []

    def get_invoices(self, order_id=None, *, page_size=None):
        return self._get_order_documents("/rest/V1/invoices", "invoice", order_id, page_size)

    def get_invoice(self, invoice_id):
        return self._get(f"/rest/V1/invoices/{invoice_id}")

    def get_credit_memos(self, order_id=None, *, page_size=None):
        return self._get_order_documents("/rest/V1/creditmemos", "creditmemo", order_id, page_size)

    def get_credit_memo(self, credit_memo_id):
        return self._get(f"/rest/V1/creditmemo/{credit_memo_id}")

    def get_shipments(self, order_id=None, *, page_size=None):
        return self._get_order_documents("/rest/V1/shipments", "shipment", order_id, page_size)

    def get_shipment(self, shipment_id):
        return self._get(f"/rest/V1/shipment/{shipment_id}")

    def create_order(self, payload):
        return self._post("/rest/V1/orders", payload)

    def update_order(self, order_id, payload):
        return self._put(f"/rest/V1/orders/{order_id}", payload)

    def create_invoice(self, order_id, payload):
        return self._post(f"/rest/V1/order/{order_id}/invoice", payload)

    def create_credit_memo(self, order_id, payload):
        return self._post(f"/rest/V1/order/{order_id}/refund", payload)

    def create_shipment(self, order_id, payload):
        return self._post(f"/rest/V1/order/{order_id}/ship", payload)

    def create_guest_cart(self):
        store_prefix = self._get_store_rest_prefix()
        if store_prefix != "/rest/V1":
            try:
                self._guest_cart_rest_prefix = store_prefix
                return self._post(f"{store_prefix}/guest-carts")
            except requests.exceptions.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else 0
                if status not in (400, 404):
                    raise
        self._guest_cart_rest_prefix = "/rest/V1"
        return self._post("/rest/V1/guest-carts")

    def add_guest_cart_item(self, cart_id, sku, qty):
        payload = {
            "cartItem": {
                "quote_id": cart_id,
                "sku": sku,
                "qty": qty,
            }
        }
        return self._post(self._guest_cart_endpoint(f"{cart_id}/items"), payload)

    def set_guest_shipping_information(self, cart_id, payload):
        return self._post(self._guest_cart_endpoint(f"{cart_id}/shipping-information"), payload)

    def set_guest_payment_information(self, cart_id, payload):
        return self._post(self._guest_cart_endpoint(f"{cart_id}/payment-information"), payload)

    def get_categories(self):
        return self._get("/rest/V1/categories")

    def get_category(self, category_id):
        return self._get(f"/rest/V1/categories/{category_id}")

    def create_category(self, payload):
        return self._post("/rest/V1/categories", payload)

    def update_category(self, category_id, payload):
        return self._put(f"/rest/V1/categories/{category_id}", payload)

    def get_product(self, sku):
        safe_sku = quote(str(sku or "").strip(), safe="")
        return self._request_with_prefix_fallback(
            "get",
            lambda prefix: f"{prefix}/products/{safe_sku}",
            include_all_scope=True,
        )

    def create_product(self, payload):
        return self._request_with_prefix_fallback(
            "post",
            lambda prefix: f"{prefix}/products",
            include_all_scope=True,
            payload=payload,
        )

    def update_product(self, sku, payload):
        safe_sku = quote(str(sku or "").strip(), safe="")
        return self._request_with_prefix_fallback(
            "put",
            lambda prefix: f"{prefix}/products/{safe_sku}",
            include_all_scope=True,
            payload=payload,
        )

    def add_product_media(self, sku, payload):
        safe_sku = quote(str(sku or "").strip(), safe="")
        return self._request_with_prefix_fallback(
            "post",
            lambda prefix: f"{prefix}/products/{safe_sku}/media",
            include_all_scope=True,
            payload=payload,
        )

    def update_product_media(self, sku, entry_id, payload):
        safe_sku = quote(str(sku or "").strip(), safe="")
        return self._request_with_prefix_fallback(
            "put",
            lambda prefix: f"{prefix}/products/{safe_sku}/media/{entry_id}",
            include_all_scope=True,
            payload=payload,
        )

    def get_product_media(self, sku, entry_id):
        safe_sku = quote(str(sku or "").strip(), safe="")
        return self._request_with_prefix_fallback(
            "get",
            lambda prefix: f"{prefix}/products/{safe_sku}/media/{entry_id}",
            include_all_scope=True,
        )

    def get_customers(self, updated_from=None, *, page_size=None, lazy=False, stats=None, limit=None,
                      filters=None):
        """Customers, paged and sorted by updated_at then entity_id; see ``_search_list``."""
        return self._search_list(
            "/rest/V1/customers/search",
            updated_from=updated_from, filters=filters, page_size=page_size, lazy=lazy,
            stats=stats, limit=limit, id_key=self.ID_KEYS["customer"],
        )

    def get_customer(self, customer_id):
        return self._get(f"/rest/V1/customers/{customer_id}")

    def create_customer(self, payload):
        return self._post("/rest/V1/customers", payload)

    def update_customer(self, customer_id, payload):
        return self._put(f"/rest/V1/customers/{customer_id}", payload)

    def get_country(self, country_code):
        safe_code = quote(str(country_code or "").strip(), safe="")
        return self._get(f"/rest/V1/directory/countries/{safe_code}")

    # ------------------------------------------------------------------
    # Bulk / async transport (roadmap 1.9)
    # ------------------------------------------------------------------
    # Magento bulk operation statuses (GET /V1/bulk/:uuid/detailed-status).
    BULK_STATUS_COMPLETE = 1
    BULK_STATUS_RETRIABLY_FAILED = 2
    BULK_STATUS_FAILED = 3
    BULK_STATUS_OPEN = 4
    BULK_STATUS_REJECTED = 5
    # ``webapi/validation/complex_array_limit`` defaults to 20 when input limits are on.
    DEFAULT_BULK_CHUNK_SIZE = 20
    MAX_BULK_CHUNK_SIZE = 500
    BULK_METHODS = frozenset({"post", "put"})
    # Route parameter (``byXxx`` segment, lower-camel name) -> keys holding its value in a
    # request body, looked up at the top level, then one level down (``product.sku``).
    _BULK_ROUTE_PARAM_KEYS = {
        "sku": ("sku",),
        "productSku": ("productSku", "sku"),
        "itemId": ("itemId", "item_id"),
        "customerId": ("customerId", "id"),
        "categoryId": ("categoryId", "id"),
        "id": ("id",),
    }

    def _bulk_chunk_size(self):
        try:
            size = int(getattr(self.instance, "bulk_chunk_size", 0) or self.DEFAULT_BULK_CHUNK_SIZE)
        except (TypeError, ValueError):
            size = self.DEFAULT_BULK_CHUNK_SIZE
        return max(1, min(size, self.MAX_BULK_CHUNK_SIZE))

    @staticmethod
    def _bulk_route(route):
        """``products/bySku`` from ``/V1/products/bySku``, ``V1/products/bySku`` or ``products/bySku``."""
        route = str(route or "").strip().strip("/")
        if route.startswith("V1/"):
            route = route[3:]
        if not route:
            raise ValueError("A Magento bulk route is required.")
        return route

    def _bulk_endpoint(self, route, store_code=None):
        """PaaS / on-premise bulk path: ``/rest/<store|all>/async/bulk/V1/<route>``.

        (On Adobe Commerce as a Cloud Service ``_request`` rewrites it to
        ``/<tenant>/V1/async/bulk/<route>`` with the ``Store`` header.)
        """
        store = str(store_code or "").strip() or "all"
        return f"/rest/{quote(store, safe='')}/async/bulk/V1/{self._bulk_route(route)}"

    @staticmethod
    def format_magento_message(message, parameters=None):
        """Substitute Magento's ``%1``/``%name`` placeholders with ``parameters``."""
        text = str(message or "")
        if isinstance(parameters, (list, tuple)):
            # Highest index first so %1 does not eat the start of %10.
            for idx in range(len(parameters), 0, -1):
                text = text.replace(f"%{idx}", str(parameters[idx - 1]))
        elif isinstance(parameters, dict):
            for key in sorted(parameters, key=lambda k: -len(str(k))):
                text = text.replace(f"%{key}", str(parameters[key]))
        return text

    @classmethod
    def bulk_error_message(cls, exc, limit=1000):
        """One-line message for a failed request, ``HTTP <status>: <Magento message>``."""
        response = getattr(exc, "response", None)
        if response is not None:
            try:
                data = response.json()
            except ValueError:
                data = None
            if isinstance(data, dict) and data.get("message"):
                detail = cls.format_magento_message(data["message"], data.get("parameters"))
            else:
                detail = (getattr(response, "text", "") or "").strip() or type(exc).__name__
            text = f"HTTP {response.status_code}: {detail}"
        else:
            text = f"{type(exc).__name__}: {exc}"
        text = " ".join(text.split())
        return text if len(text) <= limit else text[:limit] + "..."

    @classmethod
    def bulk_status_for_exception(cls, exc):
        """Bulk status a failed synchronous request maps to: 2 when retrying may
        succeed (connection errors, timeouts, HTTP 429/5xx), 3 otherwise."""
        if isinstance(exc, requests.exceptions.SSLError):
            return cls.BULK_STATUS_FAILED
        if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
            return cls.BULK_STATUS_RETRIABLY_FAILED
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status is not None and (status == 429 or status >= 500):
            return cls.BULK_STATUS_RETRIABLY_FAILED
        return cls.BULK_STATUS_FAILED

    @staticmethod
    def _bulk_aborts_batch(exc):
        """After this error, sending the remaining items is pointless: credentials
        rejected (401/403) or Magento unreachable."""
        if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
            return True
        status = getattr(getattr(exc, "response", None), "status_code", None)
        return status in (401, 403)

    def _bulk_route_value(self, item, name):
        keys = self._BULK_ROUTE_PARAM_KEYS.get(name, (name,))
        if not isinstance(item, dict):
            return None
        for key in keys:
            if item.get(key) not in (None, ""):
                return item[key]
        for value in item.values():
            if isinstance(value, dict):
                for key in keys:
                    if value.get(key) not in (None, ""):
                        return value[key]
        return None

    def _bulk_sync_path(self, route, item):
        """Synchronous path of one bulk item: ``products/bySku`` + ``{"product": {"sku": "A B"}}``
        -> ``products/A%20B``. Raises ValueError when a route parameter has no value."""
        segments = []
        for segment in self._bulk_route(route).split("/"):
            if len(segment) > 2 and segment.startswith("by") and segment[2].isupper():
                name = segment[2].lower() + segment[3:]
                value = self._bulk_route_value(item, name)
                if value in (None, ""):
                    raise ValueError(f"Bulk item has no value for route parameter '{name}'.")
                segment = quote(str(value).strip(), safe="")
            segments.append(segment)
        return "/".join(segments)

    def _bulk_sync_item(self, method, route, item, store_code=None):
        path = self._bulk_sync_path(route, item)
        if store_code:
            store = quote(str(store_code).strip(), safe="")
            return self._json(self._request(method, f"/rest/{store}/V1/{path}", payload=item))
        return self._request_with_prefix_fallback(
            method, lambda prefix: f"{prefix}/{path}", include_all_scope=True, payload=item,
        )

    def _bulk_sync_chunk(self, method, route, chunk, store_code, state):
        """Synchronous fallback: send the items of one chunk one by one through the
        normal endpoint and report them like Magento's bulk status (1 / 2 / 3)."""
        request_items = []
        for idx, item in enumerate(chunk):
            abort = state.get("abort")
            if abort is not None:
                request_items.append({
                    "id": idx, "status": self.bulk_status_for_exception(abort),
                    "result_message": self.bulk_error_message(abort), "error_code": None,
                })
                continue
            try:
                self._bulk_sync_item(method, route, item, store_code=store_code)
            except requests.exceptions.RequestException as exc:
                if self._bulk_aborts_batch(exc):
                    state["abort"] = exc
                request_items.append({
                    "id": idx,
                    "status": self.bulk_status_for_exception(exc),
                    "result_message": self.bulk_error_message(exc),
                    "error_code": getattr(getattr(exc, "response", None), "status_code", None),
                })
            except ValueError as exc:  # route parameter missing from the item
                request_items.append({
                    "id": idx, "status": self.BULK_STATUS_FAILED,
                    "result_message": str(exc), "error_code": None,
                })
            else:
                request_items.append({
                    "id": idx, "status": self.BULK_STATUS_COMPLETE, "result_message": "", "error_code": None,
                })
        return {
            "bulk_uuid": False,
            "request_items": request_items,
            "errors": any(r["status"] != self.BULK_STATUS_COMPLETE for r in request_items),
            "items": chunk,
            "sync": True,
        }

    def async_bulk(self, method, route, items, *, store_code=None):
        """Send ``items`` (one request body each) to a Magento bulk endpoint.

        ``route`` is the bulk route after ``V1/``, route parameters already rewritten
        (``:sku`` -> ``bySku``), e.g. ``products/bySku`` or ``inventory/source-items``.
        Items are sent in chunks of the instance's ``bulk_chunk_size`` (default 20) to
        ``/rest/<store_code or 'all'>/async/bulk/V1/<route>``.

        Returns one dict per chunk: ``bulk_uuid``, ``request_items`` (``id`` = index in
        the chunk), ``errors``, plus ``items`` (the chunk) and ``sync``. A chunk Magento
        refused has ``error`` (the exception) and no ``bulk_uuid``; after a 401/403 or a
        connection failure the remaining chunks are not sent and carry the same error.
        Acceptance only means Magento queued the work: poll ``get_bulk_detailed_status``.

        When the instance's ``use_async_bulk`` is off, the items are sent one by one to
        the synchronous endpoint instead (``products/bySku`` -> ``PUT /V1/products/<sku>``,
        the value read from the item) and ``request_items`` carry the final bulk status
        of each item (1 complete, 2 retriably failed, 3 failed) and ``result_message``.
        """
        method = (method or "post").lower()
        if method not in self.BULK_METHODS:
            raise ValueError(f"Unsupported Magento bulk method: {method.upper()} (POST or PUT only).")
        route = self._bulk_route(route)
        items = list(items or [])
        size = self._bulk_chunk_size()
        chunks = [items[i:i + size] for i in range(0, len(items), size)]
        state = {"abort": None}
        if not getattr(self.instance, "use_async_bulk", False):
            return [self._bulk_sync_chunk(method, route, chunk, store_code, state) for chunk in chunks]

        endpoint = self._bulk_endpoint(route, store_code)
        results = []
        for chunk in chunks:
            if state["abort"] is not None:
                results.append({"bulk_uuid": False, "request_items": [], "errors": True,
                                "items": chunk, "sync": False, "error": state["abort"]})
                continue
            try:
                response = self._json(self._request(method, endpoint, payload=chunk))
            except requests.exceptions.RequestException as exc:
                if self._bulk_aborts_batch(exc):
                    state["abort"] = exc
                results.append({"bulk_uuid": False, "request_items": [], "errors": True,
                                "items": chunk, "sync": False, "error": exc})
                continue
            response = response if isinstance(response, dict) else {}
            results.append({
                "bulk_uuid": response.get("bulk_uuid") or False,
                "request_items": response.get("request_items") or [],
                "errors": bool(response.get("errors")),
                "items": chunk,
                "sync": False,
            })
        return results

    def get_bulk_status(self, bulk_uuid):
        """``GET /V1/bulk/:uuid/status``: ``operations_list`` with id, status, result_message."""
        return self._get(f"/rest/V1/bulk/{quote(str(bulk_uuid or '').strip(), safe='')}/status")

    def get_bulk_detailed_status(self, bulk_uuid):
        """``GET /V1/bulk/:uuid/detailed-status``: adds serialized_data / result_serialized_data."""
        return self._get(f"/rest/V1/bulk/{quote(str(bulk_uuid or '').strip(), safe='')}/detailed-status")

    # ------------------------------------------------------------------
    # Bulk price storage (roadmap 2.9 / 3.4): /V1/products/{base,special,tier}-price*
    # ------------------------------------------------------------------
    # These endpoints are natively bulk and synchronous: one request carries a list of
    # prices; Magento saves the valid rows and answers with the rejected ones as a list of
    # ``{"message": ..., "parameters": ...}`` (an empty list: everything was saved).
    @staticmethod
    def _price_failures(result):
        return [row for row in result if isinstance(row, dict)] if isinstance(result, list) else []

    def update_base_prices(self, prices):
        """``POST /V1/products/base-prices``: ``[{sku, price, store_id}]`` -> failed items."""
        return self._price_failures(self._post("/rest/V1/products/base-prices", {"prices": list(prices)}))

    def get_base_prices(self, skus):
        """``POST /V1/products/base-prices-information``: ``[{sku, price, store_id}]``."""
        return self._post("/rest/V1/products/base-prices-information", {"skus": list(skus)}) or []

    def update_special_prices(self, prices):
        """``POST /V1/products/special-price``: ``[{sku, price, store_id, price_from, price_to}]``
        -> failed items. Dates are ``Y-m-d H:i:s``; a date left out is not cleared."""
        return self._price_failures(self._post("/rest/V1/products/special-price", {"prices": list(prices)}))

    def delete_special_prices(self, prices):
        """``POST /V1/products/special-price-delete``: removes the special price and both
        dates of each (sku, store_id) -> failed items."""
        return self._price_failures(
            self._post("/rest/V1/products/special-price-delete", {"prices": list(prices)})
        )

    def get_special_prices(self, skus):
        """``POST /V1/products/special-price-information``:
        ``[{sku, price, store_id, price_from, price_to}]``."""
        return self._post("/rest/V1/products/special-price-information", {"skus": list(skus)}) or []

    def update_tier_prices(self, prices):
        """``POST /V1/products/tier-prices`` (add or update):
        ``[{sku, price, price_type, website_id, customer_group, quantity}]`` -> failed items.
        ``customer_group`` is a group code or ``ALL GROUPS``."""
        return self._price_failures(self._post("/rest/V1/products/tier-prices", {"prices": list(prices)}))

    def replace_tier_prices(self, prices):
        """``PUT /V1/products/tier-prices``: replaces ALL tier prices of the SKUs sent."""
        return self._price_failures(self._put("/rest/V1/products/tier-prices", {"prices": list(prices)}))

    def delete_tier_prices(self, prices):
        """``POST /V1/products/tier-prices-delete``: removes the tier prices matching
        (sku, website_id, customer_group, quantity, price) -> failed items."""
        return self._price_failures(
            self._post("/rest/V1/products/tier-prices-delete", {"prices": list(prices)})
        )

    def get_tier_prices(self, skus):
        """``POST /V1/products/tier-prices-information``."""
        return self._post("/rest/V1/products/tier-prices-information", {"skus": list(skus)}) or []
    # Store hierarchy and store-view scope (roadmap 2.8)
    # ------------------------------------------------------------------
    def get_store_groups(self):
        """``GET /V1/store/storeGroups``: id, website_id, root_category_id, default_store_id."""
        return self._get("/rest/V1/store/storeGroups")

    def get_store_views(self):
        """``GET /V1/store/storeViews``: id, code, website_id, store_group_id, is_active."""
        return self._get("/rest/V1/store/storeViews")

    @staticmethod
    def _store_scope_prefix(store_code):
        """``/rest/<store_code>/V1``. Store-view reads and writes never fall back to another
        scope: a write without a store code would change the default store view."""
        code = str(store_code or "").strip()
        if not code:
            raise ValueError("A Magento store code is required for a store-view request.")
        return f"/rest/{quote(code, safe='')}/V1"

    def get_product_for_store(self, sku, store_code):
        """``GET /rest/<store_code>/V1/products/<sku>``: the product's store-view values
        (falling back to the default values where the view has none)."""
        safe_sku = quote(str(sku or "").strip(), safe="")
        return self._get(f"{self._store_scope_prefix(store_code)}/products/{safe_sku}")

    def update_product_for_store(self, sku, store_code, payload):
        """``PUT /rest/<store_code>/V1/products/<sku>``: store-view values (translations).
        Send only the store-scoped fields to change."""
        safe_sku = quote(str(sku or "").strip(), safe="")
        return self._put(f"{self._store_scope_prefix(store_code)}/products/{safe_sku}", payload)

    def delete_product_media(self, sku, entry_id):
        """``DELETE /rest/all/V1/products/<sku>/media/<entry_id>``."""
        safe_sku = quote(str(sku or "").strip(), safe="")
        safe_id = quote(str(entry_id or "").strip(), safe="")
        return self._json(self._request("delete", f"/rest/all/V1/products/{safe_sku}/media/{safe_id}"))

    # ------------------------------------------------------------------
    # Adobe Commerce B2B (roadmap 3.6)
    # ------------------------------------------------------------------
    # Only present on Adobe Commerce with the B2B modules; Magento Open Source answers 404
    # ("Request does not match any route"). Endpoints and payloads from:
    #   https://developer.adobe.com/commerce/webapi/rest/b2b/integrations
    #   https://developer.adobe.com/commerce/webapi/rest/b2b/company-object
    #   https://developer.adobe.com/commerce/webapi/rest/b2b/company-structures
    #   https://developer.adobe.com/commerce/webapi/rest/b2b/credit-manage
    #   https://developer.adobe.com/commerce/webapi/rest/b2b/shared-cat-manage
    #   https://developer.adobe.com/commerce/webapi/rest/b2b/shared-cat-product-assign
    #   https://developer.adobe.com/commerce/webapi/rest/b2b/shared-cat-company
    #   https://developer.adobe.com/commerce/webapi/rest/b2b/negotiable-manage
    #   https://developer.adobe.com/commerce/webapi/rest/b2b/negotiable-update
    #   https://developer.adobe.com/commerce/webapi/rest/b2b/negotiable-order-workflow
    # Validated against these documented payloads with simulated responses only; not yet
    # tested against a live Adobe Commerce store.
    B2B_ID_SORT = (("entity_id", "ASC"),)

    def probe_b2b(self):
        """``GET /V1/company?searchCriteria[pageSize]=1``: raises HTTPError 404 without B2B."""
        return self._get("/rest/V1/company", params={"searchCriteria[pageSize]": 1, "searchCriteria[currentPage]": 1})

    def get_companies(self, *, filters=None, page_size=None, lazy=False, limit=None):
        """``GET /V1/company`` (searchCriteria), paged by entity id."""
        return self._search_list(
            "/rest/V1/company", filters=filters, page_size=page_size, lazy=lazy, limit=limit,
            sort=self.B2B_ID_SORT, id_key="id",
        )

    def get_company(self, company_id):
        """``GET /V1/company/:companyId``."""
        return self._get(f"/rest/V1/company/{quote(str(company_id), safe='')}")

    def create_company(self, company):
        """``POST /V1/company`` with ``{"company": {...}}``; returns the saved company."""
        return self._post("/rest/V1/company", {"company": company})

    def update_company(self, company_id, company):
        """``PUT /V1/company/:companyId`` with ``{"company": {..., "id": id}}``."""
        body = dict(company, id=int(company_id))
        return self._put(f"/rest/V1/company/{quote(str(company_id), safe='')}", {"company": body})

    def get_company_relations(self, params=None):
        """``GET /V1/company/relations`` (parent / child companies)."""
        return self._get("/rest/V1/company/relations", params=params or {"searchCriteria": ""})

    def get_company_hierarchy(self, company_id):
        """``GET /V1/hierarchy/:id``: ``[{structure_id, entity_id, entity_type
        ("customer" | "team"), structure_parent_id}]`` of one company."""
        result = self._get(f"/rest/V1/hierarchy/{quote(str(company_id), safe='')}")
        return result if isinstance(result, list) else []

    def get_company_credit(self, company_id):
        """``GET /V1/companyCredits/company/:companyId``: ``{id, company_id, credit_limit,
        balance, currency_code, exceed_limit, available_limit}``."""
        return self._get(f"/rest/V1/companyCredits/company/{quote(str(company_id), safe='')}")

    def get_shared_catalogs(self, *, page_size=None):
        """``GET /V1/sharedCatalog`` (searchCriteria): ``{id, name, description,
        customer_group_id, type (0 custom, 1 public), created_at, created_by, store_id,
        tax_class_id}``."""
        return self._search_list(
            "/rest/V1/sharedCatalog", page_size=page_size, sort=self.B2B_ID_SORT, id_key="id",
        )

    def get_shared_catalog(self, catalog_id):
        return self._get(f"/rest/V1/sharedCatalog/{quote(str(catalog_id), safe='')}")

    def get_shared_catalog_products(self, catalog_id):
        """``GET /V1/sharedCatalog/:id/products``: a list of SKUs."""
        result = self._get(f"/rest/V1/sharedCatalog/{quote(str(catalog_id), safe='')}/products")
        return [str(sku) for sku in result if sku not in (None, "")] if isinstance(result, list) else []

    def assign_shared_catalog_products(self, catalog_id, skus):
        """``POST /V1/sharedCatalog/:id/assignProducts`` with ``{"products": [{"sku": ...}]}``."""
        payload = {"products": [{"sku": sku} for sku in skus]}
        return self._post(f"/rest/V1/sharedCatalog/{quote(str(catalog_id), safe='')}/assignProducts", payload)

    def get_shared_catalog_companies(self, catalog_id):
        """``GET /V1/sharedCatalog/:id/companies``. Adobe documents the answer as a JSON
        *string* holding the id list (``"[\\"1\\",\\"2\\"]"``); a plain list is accepted too."""
        result = self._get(f"/rest/V1/sharedCatalog/{quote(str(catalog_id), safe='')}/companies")
        if isinstance(result, str):
            try:
                result = json.loads(result or "[]")
            except ValueError:
                result = []
        return [str(cid) for cid in result if cid not in (None, "")] if isinstance(result, list) else []

    def assign_shared_catalog_companies(self, catalog_id, company_ids):
        """``POST /V1/sharedCatalog/:id/assignCompanies`` with ``{"companies": [{"id": ...}]}``."""
        payload = {"companies": [{"id": int(cid)} for cid in company_ids]}
        return self._post(f"/rest/V1/sharedCatalog/{quote(str(catalog_id), safe='')}/assignCompanies", payload)

    def get_negotiable_quote(self, quote_id):
        """``GET /V1/carts/:cartId`` (admin token): the quote with ``items`` and
        ``extension_attributes.negotiable_quote``."""
        return self._get(f"/rest/V1/carts/{quote(str(quote_id), safe='')}")

    def search_negotiable_quotes(self, updated_from=None, *, page_size=None, lazy=False, limit=None):
        """Quotes from ``GET /V1/carts/search`` updated since ``updated_from``, keeping the
        ones that carry ``extension_attributes.negotiable_quote``. (Adobe documents no
        REST list endpoint for negotiable quotes; single quotes are read with
        ``get_negotiable_quote``.)"""
        rows = self._search_list(
            "/rest/V1/carts/search", updated_from=updated_from, page_size=page_size,
            lazy=True, limit=limit, id_key="id",
        )
        result = (
            row for row in rows
            if isinstance(row, dict) and ((row.get("extension_attributes") or {}).get("negotiable_quote"))
        )
        return result if lazy else list(result)

    def get_negotiable_quote_comments(self, quote_id):
        """``GET /V1/negotiableQuote/:quoteId/comments``: ``[{entity_id, parent_id,
        creator_type (2 seller, 3 buyer), is_decline, is_draft, creator_id, comment,
        created_at, attachments}]``."""
        result = self._get(f"/rest/V1/negotiableQuote/{quote(str(quote_id), safe='')}/comments")
        return result if isinstance(result, list) else []

    def update_negotiable_quote(self, quote_id, negotiable_quote):
        """``PUT /V1/negotiableQuote/:quoteId`` with ``{"quote": {"id": id,
        "extension_attributes": {"negotiable_quote": {...}}}}`` (e.g.
        ``negotiated_price_type`` 1 % / 2 amount / 3 proposed total,
        ``negotiated_price_value``, ``expiration_period``)."""
        payload = {"quote": {
            "id": int(quote_id),
            "extension_attributes": {"negotiable_quote": dict(negotiable_quote)},
        }}
        return self._put(f"/rest/V1/negotiableQuote/{quote(str(quote_id), safe='')}", payload)

    def submit_negotiable_quote(self, quote_id, comment=""):
        """``POST /V1/negotiableQuote/submitToCustomer`` with ``{"quoteId", "comment"}``."""
        return self._post("/rest/V1/negotiableQuote/submitToCustomer",
                          {"quoteId": int(quote_id), "comment": comment or ""})

    def decline_negotiable_quote(self, quote_id, reason):
        """``POST /V1/negotiableQuote/decline`` with ``{"quoteId", "reason"}``."""
        return self._post("/rest/V1/negotiableQuote/decline",
                          {"quoteId": int(quote_id), "reason": reason or ""})
