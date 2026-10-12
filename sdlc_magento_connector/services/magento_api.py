import http.client
import logging
import os
import socket
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from itertools import islice

import requests
from requests.compat import urlparse, urlunparse
from requests.utils import quote
from urllib3 import exceptions as urllib3_exceptions

_logger = logging.getLogger(__name__)


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
        self._store_code = None
        self._guest_cart_rest_prefix = "/rest/V1"
        self.headers = {"Content-Type": "application/json"}
        # The token is restricted to Magento managers (field ``groups``). Syncs may
        # be run by Magento users or cron, so read only the secret with sudo.
        access_token = getattr(instance.sudo(), "access_token", None)
        if isinstance(access_token, str):
            access_token = access_token.strip()
        if access_token:
            self.headers["Authorization"] = f"Bearer {access_token}"

        self.verify = bool(instance.verify_ssl)
        self._container_gateway_ip = self._detect_container_gateway_ip()
        self._base_urls = self._build_base_urls()

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
        ``MAX_RETRIES`` times with exponential backoff."""
        method = (method or "get").lower()
        idempotent = method in self.IDEMPOTENT_METHODS
        retry_statuses = self.RETRY_STATUSES if idempotent else self.RETRY_STATUSES_UNSAFE_METHODS
        request_kwargs = {
            "headers": self.headers,
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
