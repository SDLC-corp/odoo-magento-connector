"""Price export (roadmap 2.9; ground work for 3.4 tier and customer-group prices).

A ``magento.price.rule`` pushes the prices of one Odoo pricelist to one Magento instance,
in one Magento scope, as one kind of price:

* **base**: the pricelist price for quantity 1 -> ``price`` (``POST /V1/products/base-prices``);
* **special**: the pricelist price for quantity 1, when a pricelist item applies, ->
  ``special_price`` with ``special_from_date`` / ``special_to_date`` taken from that
  item's start / end dates (``POST /V1/products/special-price``);
* **tier**: every pricelist item with a minimum quantity above 1 that applies to the
  product and makes it cheaper than the quantity-1 price -> one Magento tier price per
  quantity and customer group (``POST /V1/products/tier-prices``, fixed price). The group
  is ``ALL GROUPS``, one ``magento.customer.group``, or every group whose ``pricelist_id``
  is the rule's pricelist; Magento expects the group *code* (our group ``name``).

What is pushed
    Every mapped simple / virtual / downloadable product of the instance with a SKU and an
    Odoo product, configurable children (variants) included. Prices come from
    ``pricelist._compute_price_rule`` (the batch form of ``_get_product_price``), in the
    pricelist currency, rounded to the "Product Price" precision.

Scope
    Global: ``store_id`` 0 (base, special) / ``website_id`` 0 (tier). Website / store view:
    the rule's store id and website id. Magento rejects non-global rows while its price
    scope (``catalog/price/scope``) is global; those rejections land in the Error Center.

Only changes are sent
    ``magento.price.push.state`` keeps, per rule, SKU and price key, the values last
    accepted by Magento. An entry equal to its state is not sent again. A state with no
    entry any more (pricelist item removed or expired, product unmapped, scope, kind or
    group changed) is withdrawn: special and tier prices the connector created are deleted
    in Magento (``special-price-delete`` / ``tier-prices-delete``); a special price is only
    deleted while Magento still holds the price we pushed, so a price changed in Magento
    since is left alone. Base prices are never deleted, their state is just dropped.

Failures
    The price endpoints save the valid rows of a request and return the others as
    ``[{"message", "parameters"}]`` (no row index). Each failure is attributed to the SKU
    found in its parameters and recorded in the Error Center (entity ``product``, ref the
    SKU, message ``price: ...``); its state is not updated, so the next push retries it.
    A failure that names no SKU of the request is recorded on the rule
    (``price-rule-<id>``) and the whole request is retried next time. A successful push
    of a SKU resolves its open ``price:`` issue.

Runs
    As a queued job (identity ``prices-<rule id>``, channel ``prices-<instance id>``): the
    rule's **Push Prices Now** button, and an inactive-by-default cron that queues every
    active rule with "Scheduled Push" on.

Prices imported from Magento (roadmap 3.4)
    Pricelist items created or adopted by the tier price import
    (``magento.tier.price.import``) hold what Magento already has. An entry whose winning
    pricelist item is such an item is marked ``magento_owned``: it is neither sent nor
    withdrawn (its push state, if any, is kept), so an import is never pushed back as a
    change and an imported percentage tier is never turned into a fixed one.

Store models of the multi-store work (``magento.website`` / ``magento.store.view``) are
used softly: when they are installed, the rule shows the store view name of its store id
and fills the website id / code from it; nothing here imports or requires them.
"""
import logging
import re
from collections import defaultdict

import requests

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import float_compare, float_round

from ..services.magento_api import MagentoAPI
from .magento_sync_issue import MagentoSyncIssue as _BaseSyncIssue

_logger = logging.getLogger(__name__)

PRICE_KINDS = [
    ("base", "Base Price"),
    ("special", "Special Price"),
    ("tier", "Tier Prices"),
]
PRICE_SCOPES = [
    ("global", "Global (all websites)"),
    ("store", "Website / Store View"),
]
GROUP_MODES = [
    ("all", "All Groups"),
    ("group", "One Customer Group"),
    ("mapped", "Groups Mapped to this Pricelist"),
]
PUSH_STATES = [
    ("done", "Done"),
    ("partial", "Partially Failed"),
    ("failed", "Failed"),
]
ALL_GROUPS = "ALL GROUPS"
TIER_PRICE_TYPE = "fixed"
# Magento product types that carry their own price.
_PRICED_TYPES = ("simple", "virtual", "downloadable")
_PRICE_ISSUE_PREFIX = "price: "
_PLACEHOLDER_RE = re.compile(r"%([A-Za-z_]\w*)")
# Parameter order of Magento's tier price validation messages (TierPriceValidator), which
# the REST layer returns as a plain list; extra leading parameters come before them.
_TIER_PARAM_TAIL = ("SKU", "websiteId", "customerGroup", "qty")


def _price_failure_values(row):
    params = row.get("parameters")
    if isinstance(params, dict):
        return [str(v) for v in params.values() if v not in (None, "")]
    if isinstance(params, (list, tuple)):
        return [str(v) for v in params if v not in (None, "")]
    return []


def price_failure_message(row):
    """Readable text of one failed price item ``{"message", "parameters"}``.

    Magento renders some messages itself; others keep ``%name`` placeholders while the
    REST layer sends the parameters as a list (keys dropped), in the order Magento built
    them: ``%1``-style placeholders, the tier validator order and appearance order are
    tried in that order.
    """
    message = str(row.get("message") or "")
    params = row.get("parameters")
    if isinstance(params, dict):
        return MagentoAPI.format_magento_message(message, params)
    if not isinstance(params, (list, tuple)) or not params:
        return message
    if re.search(r"%\d", message):
        return MagentoAPI.format_magento_message(message, list(params))
    names = list(dict.fromkeys(_PLACEHOLDER_RE.findall(message)))
    if not names:
        return message
    if set(_TIER_PARAM_TAIL) <= set(names):
        names = [n for n in names if n not in _TIER_PARAM_TAIL] + list(_TIER_PARAM_TAIL)
    if len(names) != len(params):
        return "%s (%s)" % (message, ", ".join("" if v is None else str(v) for v in params))
    values = dict(zip(names, ("" if v is None else v for v in params)))
    return MagentoAPI.format_magento_message(message, values)


def _magento_datetime(value):
    return fields.Datetime.to_string(value) if value else False


class MagentoPriceRule(models.Model):
    _name = "magento.price.rule"
    _description = "Magento Price Export Rule"
    _order = "instance_id, sequence, id"

    name = fields.Char(required=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    instance_id = fields.Many2one("magento.instance", required=True, index=True, ondelete="cascade")
    company_id = fields.Many2one(related="instance_id.company_id", store=True, index=True, readonly=True)
    pricelist_id = fields.Many2one(
        "product.pricelist", string="Pricelist", required=True, ondelete="restrict",
        domain="['|', ('company_id', '=', False), ('company_id', '=', company_id)]",
        help="Odoo pricelist whose prices are pushed. Prices are sent in the pricelist "
             "currency, which should be the base currency of the Magento scope.",
    )
    currency_id = fields.Many2one(related="pricelist_id.currency_id", string="Currency")
    kind = fields.Selection(
        PRICE_KINDS, string="Price Kind", required=True, default="base",
        help="Base Price: the pricelist price for quantity 1 becomes the Magento price.\n"
             "Special Price: the pricelist price for quantity 1, when a pricelist item applies, "
             "becomes the Magento special price; the item's start / end dates become its "
             "From / To dates.\n"
             "Tier Prices: pricelist items with a minimum quantity above 1 become Magento "
             "tier prices for the chosen customer group(s).",
    )
    scope = fields.Selection(
        PRICE_SCOPES, string="Magento Scope", required=True, default="global",
        help="Global: store 0 (base and special prices) and website 0 (tier prices). "
             "Website / Store View: the store id and website id below. Magento only accepts "
             "non-global prices when its price scope (Catalog > Price > Catalog Price Scope) "
             "is set to Website.",
    )
    magento_store_id = fields.Integer(
        string="Magento Store ID",
        help="store_id of the base and special prices when the scope is Website / Store View.",
    )
    magento_website_id = fields.Integer(
        string="Magento Website ID",
        help="website_id of the tier prices when the scope is Website / Store View.",
    )
    website_code = fields.Char(
        string="Website Code",
        help="Magento website code, for reference (filled from the store view when the "
             "multi-store models are installed).",
    )
    store_view_name = fields.Char(string="Store View", compute="_compute_store_view_name")
    effective_store_id = fields.Integer(string="Store ID Sent", compute="_compute_effective_scope")
    effective_website_id = fields.Integer(string="Website ID Sent", compute="_compute_effective_scope")
    customer_group_mode = fields.Selection(
        GROUP_MODES, string="Customer Groups", required=True, default="all",
        help="Tier prices only. All Groups: Magento's ALL GROUPS. One Customer Group: the "
             "group below. Groups Mapped to this Pricelist: every customer group of the "
             "instance whose pricelist is this rule's pricelist.",
    )
    customer_group_id = fields.Many2one(
        "magento.customer.group", string="Customer Group", ondelete="restrict",
        domain="[('instance_id', '=', instance_id)]",
    )
    customer_group_codes = fields.Char(string="Group Codes Sent", compute="_compute_customer_group_codes")
    auto_push = fields.Boolean(
        string="Scheduled Push", default=True,
        help="Include this rule in the scheduled price push (cron 'Magento: Push Prices', "
             "inactive by default).",
    )
    last_push_at = fields.Datetime(string="Last Push", readonly=True, copy=False)
    last_push_state = fields.Selection(PUSH_STATES, string="Last Push Result", readonly=True, copy=False)
    last_push_summary = fields.Text(string="Last Push Summary", readonly=True, copy=False)
    push_state_ids = fields.One2many("magento.price.push.state", "rule_id", string="Pushed Prices",
                                     readonly=True, copy=False)
    push_state_count = fields.Integer(string="Pushed Price Count", compute="_compute_push_state_count")

    # ------------------------------------------------------------------
    # Computes / constraints
    # ------------------------------------------------------------------
    @api.depends("scope", "magento_store_id", "magento_website_id")
    def _compute_effective_scope(self):
        for rule in self:
            is_global = rule.scope == "global"
            rule.effective_store_id = 0 if is_global else rule.magento_store_id
            rule.effective_website_id = 0 if is_global else rule.magento_website_id

    def _compute_push_state_count(self):
        counts = dict(self.env["magento.price.push.state"]._read_group(
            [("rule_id", "in", self.ids)], ["rule_id"], ["__count"],
        ))
        for rule in self:
            rule.push_state_count = counts.get(rule, 0)

    @api.depends("kind", "customer_group_mode", "customer_group_id", "pricelist_id", "instance_id")
    def _compute_customer_group_codes(self):
        for rule in self:
            rule.customer_group_codes = ", ".join(rule._magento_customer_group_codes()) if rule.kind == "tier" else False

    @api.depends("instance_id", "magento_store_id", "scope")
    def _compute_store_view_name(self):
        for rule in self:
            view = rule._magento_soft_store_view()
            rule.store_view_name = (view.display_name if view else False) if rule.scope == "store" else False

    @api.constrains("instance_id", "customer_group_id", "customer_group_mode", "kind")
    def _check_customer_group(self):
        for rule in self:
            if rule.kind != "tier" or rule.customer_group_mode != "group":
                continue
            if not rule.customer_group_id:
                raise ValidationError(_("Choose the customer group of tier price rule %s.", rule.name))
            if rule.customer_group_id.instance_id != rule.instance_id:
                raise ValidationError(_("The customer group of rule %s belongs to another Magento instance.",
                                        rule.name))

    @api.constrains("magento_store_id", "magento_website_id")
    def _check_scope_ids(self):
        for rule in self:
            if rule.magento_store_id < 0 or rule.magento_website_id < 0:
                raise ValidationError(_("Magento store and website ids cannot be negative."))

    @api.constrains("pricelist_id", "instance_id")
    def _check_pricelist_company(self):
        for rule in self:
            company = rule.pricelist_id.company_id
            if company and rule.instance_id.company_id and company != rule.instance_id.company_id:
                raise ValidationError(_("The pricelist of rule %s belongs to another company than its "
                                        "Magento instance.", rule.name))

    @api.onchange("scope", "instance_id")
    def _onchange_scope(self):
        for rule in self:
            if rule.scope == "store" and rule.instance_id and not rule.magento_store_id:
                rule.magento_store_id = rule.instance_id.store_id
                rule.magento_website_id = rule.instance_id.website_id

    @api.onchange("magento_store_id")
    def _onchange_magento_store_id(self):
        """Soft link to the multi-store models: fill the website from the store view."""
        for rule in self:
            website_id, website_code = rule._magento_soft_store_website()
            if website_id is not None:
                rule.magento_website_id = website_id
            if website_code:
                rule.website_code = website_code

    @api.ondelete(at_uninstall=False)
    def _unlink_except_owned_prices(self):
        if self.env.context.get("magento_price_force_unlink"):
            return
        owned = self.env["magento.price.push.state"].sudo().search_count(
            [("rule_id", "in", self.ids), ("kind", "in", ("special", "tier"))]
        )
        if owned:
            raise UserError(_(
                "These rules still own %s special or tier price(s) in Magento. Use "
                "'Remove from Magento' first, or archive the rules.", owned,
            ))

    # ------------------------------------------------------------------
    # Soft integration with magento.website / magento.store.view (multi-store work)
    # ------------------------------------------------------------------
    def _magento_soft_store_view(self):
        """The ``magento.store.view`` of this rule's instance and store id, when that model
        is installed; an empty value otherwise. Field names are probed, never assumed."""
        self.ensure_one()
        if "magento.store.view" not in self.env or not self.instance_id or not self.magento_store_id:
            return None
        StoreView = self.env["magento.store.view"]
        id_field = next((f for f in ("magento_store_id", "store_id", "magento_id") if f in StoreView._fields), None)
        if not id_field or "instance_id" not in StoreView._fields:
            return None
        value = self.magento_store_id
        if StoreView._fields[id_field].type == "char":
            value = str(value)
        try:
            return StoreView.search([("instance_id", "=", self.instance_id.id), (id_field, "=", value)], limit=1) or None
        except Exception:  # noqa: BLE001 - optional integration must never break the rule form
            _logger.debug("magento.store.view lookup failed", exc_info=True)
            return None

    def _magento_soft_store_website(self):
        """``(website_id, website_code)`` of the soft-linked store view, ``(None, None)``
        when unknown."""
        self.ensure_one()
        view = self._magento_soft_store_view()
        if not view:
            return None, None
        website = view["website_id"] if "website_id" in view._fields else None
        if isinstance(website, models.BaseModel):
            wid = next((website[f] for f in ("magento_website_id", "magento_id", "website_id")
                        if f in website._fields and website[f] not in (None, False, "")), None)
            code = website["code"] if "code" in website._fields else None
        else:
            wid, code = website, None
        if code is None:
            code = next((view[f] for f in ("website_code",) if f in view._fields), None)
        try:
            wid = int(wid) if wid not in (None, False, "") else None
        except (TypeError, ValueError):
            wid = None
        return wid, code or None

    # ------------------------------------------------------------------
    # What to push
    # ------------------------------------------------------------------
    def _magento_customer_group_codes(self):
        self.ensure_one()
        if self.customer_group_mode == "group":
            return [self.customer_group_id.name] if self.customer_group_id.name else []
        if self.customer_group_mode == "mapped":
            if not self.instance_id or not self.pricelist_id:
                return []
            groups = self.env["magento.customer.group"].sudo().search([
                ("instance_id", "=", self.instance_id.id), ("pricelist_id", "=", self.pricelist_id.id),
            ], order="magento_group_id")
            return [g.name for g in groups if g.name]
        return [ALL_GROUPS]

    def _magento_price_maps(self):
        """Product maps whose price is pushed: priced types with a SKU and a product."""
        self.ensure_one()
        maps = self.env["magento.product.map"].sudo().search([
            ("instance_id", "=", self.instance_id.id),
            ("odoo_product_id", "!=", False),
            ("sku", "!=", False),
            ("sku", "!=", ""),
            "|", ("type_id", "in", _PRICED_TYPES), ("type_id", "=", False),
        ], order="id")
        return maps.filtered(lambda m: m.odoo_product_id.active)

    @api.model
    def _price_digits(self):
        return self.env["decimal.precision"].precision_get("Product Price")

    def _magento_entry(self, product_map, key, price, **extra):
        entry = {
            "key": key,
            "sku": product_map.sku,
            "map_id": product_map.id,
            "product_id": product_map.odoo_product_id.id,
            "kind": self.kind,
            "price": float_round(price, precision_digits=self._price_digits()),
            "store_id": self.effective_store_id,
            "website_id": self.effective_website_id,
            "price_from": False,
            "price_to": False,
            "customer_group": False,
            "quantity": 0.0,
        }
        entry.update(extra)
        return entry

    def _magento_imported_item_ids(self):
        """Ids of the pricelist items of this rule's pricelist that the tier price import
        owns (prices that come from Magento: never pushed back)."""
        self.ensure_one()
        if "magento.tier.price.import" not in self.env or not self.pricelist_id:
            return set()
        return set(self.env["magento.tier.price.import"].sudo().search([
            ("pricelist_id", "=", self.pricelist_id.id), ("item_id", "!=", False),
        ]).item_id.ids)

    def _magento_desired_entries(self, maps=None):
        """``{(sku, key): entry}`` this rule wants in Magento now. An entry priced by an
        imported item carries ``magento_owned=True`` (see the module docstring)."""
        self.ensure_one()
        maps = self._magento_price_maps() if maps is None else maps
        products = maps.mapped("odoo_product_id")
        if not products:
            return {}
        pricelist = self.pricelist_id.sudo()
        now = fields.Datetime.now()
        base = pricelist._compute_price_rule(products, 1.0, date=now)
        Item = self.env["product.pricelist.item"].sudo()
        owned = self._magento_imported_item_ids()
        desired = {}
        if self.kind == "base":
            key = "base|s%s" % self.effective_store_id
            for product_map in maps:
                price, rule_id = base[product_map.odoo_product_id.id]
                desired[(product_map.sku, key)] = self._magento_entry(
                    product_map, key, price, magento_owned=rule_id in owned)
        elif self.kind == "special":
            key = "special|s%s" % self.effective_store_id
            for product_map in maps:
                price, rule_id = base[product_map.odoo_product_id.id]
                if not rule_id:
                    continue  # no pricelist item applies (removed or expired): no special price
                item = Item.browse(rule_id)
                desired[(product_map.sku, key)] = self._magento_entry(
                    product_map, key, price,
                    price_from=_magento_datetime(item.date_start),
                    price_to=_magento_datetime(item.date_end),
                    magento_owned=rule_id in owned,
                )
        elif self.kind == "tier":
            desired = self._magento_desired_tier_entries(maps, products, base, now, owned)
        return desired

    def _magento_desired_tier_entries(self, maps, products, base, now, owned=frozenset()):
        groups = self._magento_customer_group_codes()
        if not groups:
            return {}
        pricelist = self.pricelist_id.sudo()
        tier_items = pricelist._get_applicable_rules(products, now).filtered(lambda i: i.min_quantity > 1)
        if not tier_items:
            return {}
        Item = self.env["product.pricelist.item"].sudo()
        products_by_qty = defaultdict(lambda: self.env["product.product"])
        for product in products:
            for qty in {i.min_quantity for i in tier_items if i._is_applicable_for(product, i.min_quantity)}:
                products_by_qty[qty] |= product
        digits = self._price_digits()
        prices = {}  # product id -> {qty: (price, imported)}
        for qty, qty_products in products_by_qty.items():
            for product_id, (price, rule_id) in pricelist._compute_price_rule(qty_products, qty, date=now).items():
                if rule_id in owned:
                    # Came from Magento: keep the key (never withdrawn), never send it.
                    prices.setdefault(product_id, {})[qty] = (price, True)
                    continue
                if not rule_id or Item.browse(rule_id).min_quantity <= 1:
                    continue  # the winning item is not a quantity break
                if float_compare(price, base[product_id][0], precision_digits=digits) >= 0:
                    continue  # not cheaper than one unit: no tier
                prices.setdefault(product_id, {})[qty] = (price, False)
        desired = {}
        for product_map in maps:
            for qty, (price, imported) in sorted(prices.get(product_map.odoo_product_id.id, {}).items()):
                for group in groups:
                    key = "tier|w%s|%s|%s" % (self.effective_website_id, group, "%g" % qty)
                    desired[(product_map.sku, key)] = self._magento_entry(
                        product_map, key, price, customer_group=group, quantity=qty,
                        magento_owned=imported,
                    )
        return desired

    # ------------------------------------------------------------------
    # Payloads
    # ------------------------------------------------------------------
    @staticmethod
    def _magento_price_payload(entry):
        """Magento body of one entry (a dict, or a push state for deletions)."""
        kind = entry["kind"]
        payload = {"sku": entry["sku"], "price": entry["price"]}
        if kind == "tier":
            payload.update({
                "price_type": TIER_PRICE_TYPE,
                "website_id": entry["website_id"],
                "customer_group": entry["customer_group"],
                "quantity": entry["quantity"],
            })
            return payload
        payload["store_id"] = entry["store_id"]
        if kind == "special":
            for name in ("price_from", "price_to"):
                if entry[name]:
                    payload[name] = entry[name]
        return payload

    @staticmethod
    def _magento_state_entry(state):
        return {
            "key": state.key, "sku": state.sku, "kind": state.kind, "price": state.price,
            "store_id": state.store_id, "website_id": state.website_id,
            "price_from": state.price_from or False, "price_to": state.price_to or False,
            "customer_group": state.customer_group or False, "quantity": state.quantity,
            "map_id": state.map_id.id, "product_id": state.product_id.id, "state_id": state.id,
        }

    def _magento_same(self, state, entry):
        return (
            float_compare(state.price, entry["price"], precision_digits=self._price_digits()) == 0
            and (state.price_from or False) == (entry["price_from"] or False)
            and (state.price_to or False) == (entry["price_to"] or False)
        )

    # ------------------------------------------------------------------
    # Failure attribution
    # ------------------------------------------------------------------
    @staticmethod
    def _magento_attribute_failures(failures, chunk):
        """``({chunk index: [message]}, [unattributed message])`` for Magento's failed items.

        A failure belongs to the entries whose SKU is one of its parameters (or appears as
        ``SKU = <sku>`` in its text); for tier prices, narrowed to the entries whose
        customer group and quantity it also names, when it does.
        """
        by_index = defaultdict(list)
        unattributed = []
        for row in failures:
            text = price_failure_message(row)
            values = set(_price_failure_values(row))
            values.update(m.strip() for m in re.findall(r"SKU = ([^,]+),", text))
            matches = [i for i, e in enumerate(chunk) if e["sku"] in values]
            if not matches:
                unattributed.append(text)
                continue
            narrowed = []
            for i in matches:
                entry = chunk[i]
                if entry["kind"] != "tier":
                    continue
                qty_named = any(_same_number(v, entry["quantity"]) for v in values)
                group_named = any(v.lower() == str(entry["customer_group"]).lower() for v in values)
                if qty_named and group_named:
                    narrowed.append(i)
            for i in narrowed or matches:
                by_index[i].append(text)
        return by_index, unattributed

    # ------------------------------------------------------------------
    # Push
    # ------------------------------------------------------------------
    def _magento_push_prices(self, remove_all=False, force=False):
        """Push the changed prices of each rule (job entry point); return a summary per rule.

        ``remove_all``: withdraw everything the rule pushed (special and tier prices are
        deleted in Magento, base price states are dropped) and push nothing.
        ``force``: send every price, changed or not.
        """
        summaries = {}
        for rule in self:
            if not rule.active and not remove_all:
                summaries[rule.id] = "archived"
                continue
            summaries[rule.id] = rule._magento_push_one(remove_all=remove_all, force=force)
        return summaries

    def _magento_push_one(self, remove_all=False, force=False):
        self.ensure_one()
        rule = self.sudo()
        instance = rule.instance_id
        api_client = MagentoAPI(instance)
        desired = {} if remove_all else rule._magento_desired_entries()
        states = rule.push_state_ids
        state_by_key = {(s.sku, s.key): s for s in states}
        stats = defaultdict(int)
        stats["unchanged"] = 0

        to_send, to_delete, date_resets = [], [], []
        for key, entry in desired.items():
            if entry.get("magento_owned"):
                stats["imported"] += 1  # imported from Magento: not pushed back
                continue
            state = state_by_key.get(key)
            if state and not force and rule._magento_same(state, entry):
                stats["unchanged"] += 1
                continue
            if state and entry["kind"] == "special" and (
                (state.price_from and not entry["price_from"]) or (state.price_to and not entry["price_to"])
            ):
                date_resets.append(state)  # Magento keeps a date left out: delete first
            to_send.append(entry)
        for key, state in state_by_key.items():
            if key not in desired:
                to_delete.append(state)

        dropped = [s for s in to_delete if s.kind == "base"]
        if dropped:
            stats["dropped"] += len(dropped)
            rule.env["magento.price.push.state"].sudo().browse([s.id for s in dropped]).unlink()

        ctx = {"api": api_client, "stats": stats, "abort": None}
        special_deletes = [s for s in to_delete if s.kind == "special"] + date_resets
        tier_deletes = [s for s in to_delete if s.kind == "tier"]
        if special_deletes:
            rule._magento_delete_special(ctx, special_deletes)
        if tier_deletes:
            rule._magento_send(ctx, "delete_tier_prices",
                               [rule._magento_state_entry(s) for s in tier_deletes], rule._magento_on_deleted)
        method = {"base": "update_base_prices", "special": "update_special_prices",
                  "tier": "update_tier_prices"}
        valid = []
        for entry in to_send:
            if entry["price"] < 0:
                rule._magento_record_failure(
                    entry, _("negative price %s computed by pricelist %s, not sent.",
                             entry["price"], rule.pricelist_id.display_name))
                stats["failed"] += 1
            else:
                valid.append(entry)
        for kind in ("base", "special", "tier"):
            entries = [e for e in valid if e["kind"] == kind]
            if entries:
                rule._magento_send(ctx, method[kind], entries, rule._magento_on_pushed)

        summary = rule._magento_push_summary(stats, remove_all)
        failed = stats["failed"] + stats["unattributed"]
        sent_ok = stats["sent"] + stats["deleted"]
        rule.write({
            "last_push_at": fields.Datetime.now(),
            "last_push_state": "done" if not failed else ("partial" if sent_ok else "failed"),
            "last_push_summary": summary,
        })
        abort = ctx["abort"]
        if abort is not None and MagentoAPI.bulk_status_for_exception(abort) == MagentoAPI.BULK_STATUS_RETRIABLY_FAILED:
            # Magento unreachable: let the job retry later (sent prices are resent, harmlessly).
            raise abort
        return summary

    def _magento_push_summary(self, stats, remove_all=False):
        parts = [
            _("%s sent", stats["sent"]),
            _("%s unchanged", stats["unchanged"]),
            _("%s removed", stats["deleted"]),
            _("%s failed", stats["failed"]),
        ]
        if stats["dropped"]:
            parts.append(_("%s base price state(s) dropped", stats["dropped"]))
        if stats["imported"]:
            parts.append(_("%s imported from Magento, not pushed back", stats["imported"]))
        if stats["foreign"]:
            parts.append(_("%s special price(s) changed in Magento, left alone", stats["foreign"]))
        if stats["unattributed"]:
            parts.append(_("%s request(s) with errors not tied to a SKU", stats["unattributed"]))
        prefix = _("Removed from Magento: ") if remove_all else ""
        return prefix + ", ".join(parts)

    def _magento_send(self, ctx, method, entries, on_success):
        """Send ``entries`` in chunks through ``MagentoAPI.<method>``; record each outcome."""
        self.ensure_one()
        api_client, stats = ctx["api"], ctx["stats"]
        size = api_client._bulk_chunk_size()
        is_delete = method.startswith("delete_")
        for start in range(0, len(entries), size):
            chunk = entries[start:start + size]
            payloads = [self._magento_price_payload(e) for e in chunk]
            if ctx["abort"] is not None:
                self._magento_chunk_error(chunk, ctx["abort"])
                stats["failed"] += len(chunk)
                continue
            try:
                failures = getattr(api_client, method)(payloads)
            except requests.exceptions.RequestException as exc:
                if MagentoAPI._bulk_aborts_batch(exc):
                    ctx["abort"] = exc
                self._magento_chunk_error(chunk, exc)
                stats["failed"] += len(chunk)
                continue
            by_index, unattributed = self._magento_attribute_failures(failures, chunk)
            if unattributed:
                stats["unattributed"] += 1
                self._magento_record_rule_failure(unattributed, payloads)
                continue  # cannot tell which rows were saved: resend the request next time
            ok_skus = set()
            for idx, entry in enumerate(chunk):
                if idx in by_index:
                    self._magento_record_failure(entry, "; ".join(dict.fromkeys(by_index[idx])))
                    stats["failed"] += 1
                else:
                    on_success(entry)
                    stats["deleted" if is_delete else "sent"] += 1
                    ok_skus.add(entry["sku"])
            failed_skus = {chunk[i]["sku"] for i in by_index}
            self._magento_resolve_price_issues(ok_skus - failed_skus)

    def _magento_delete_special(self, ctx, states):
        """Delete special prices we own: only where Magento still holds the price we pushed."""
        api_client, stats = ctx["api"], ctx["stats"]
        size = api_client._bulk_chunk_size()
        digits = self._price_digits()
        owned = []
        for start in range(0, len(states), size):
            chunk = states[start:start + size]
            if ctx["abort"] is not None:
                self._magento_chunk_error([self._magento_state_entry(s) for s in chunk], ctx["abort"])
                stats["failed"] += len(chunk)
                continue
            try:
                current = api_client.get_special_prices(sorted({s.sku for s in chunk}))
            except requests.exceptions.RequestException as exc:
                if MagentoAPI._bulk_aborts_batch(exc):
                    ctx["abort"] = exc
                self._magento_chunk_error([self._magento_state_entry(s) for s in chunk], exc)
                stats["failed"] += len(chunk)
                continue
            live = {}
            for row in current if isinstance(current, list) else []:
                if isinstance(row, dict) and row.get("sku"):
                    live[(row["sku"], _as_int(row.get("store_id")))] = row.get("price")
            for state in chunk:
                price = live.get((state.sku, state.store_id))
                if price is not None and float_compare(_as_float(price), state.price, precision_digits=digits) == 0:
                    owned.append(self._magento_state_entry(state))
                else:
                    # Gone, or changed in Magento since we pushed it: not ours any more.
                    stats["foreign" if price is not None else "deleted"] += 1
                    state.unlink()
        if owned:
            self._magento_send(ctx, "delete_special_prices", owned, self._magento_on_deleted)

    def _magento_on_pushed(self, entry):
        States = self.env["magento.price.push.state"].sudo()
        vals = {
            "price": entry["price"],
            "price_from": entry["price_from"] or False,
            "price_to": entry["price_to"] or False,
            "pushed_at": fields.Datetime.now(),
            "map_id": entry.get("map_id") or False,
            "product_id": entry.get("product_id") or False,
        }
        state = States.search([("rule_id", "=", self.id), ("sku", "=", entry["sku"]), ("key", "=", entry["key"])],
                              limit=1)
        if state:
            state.write(vals)
        else:
            vals.update({
                "rule_id": self.id, "sku": entry["sku"], "key": entry["key"], "kind": entry["kind"],
                "store_id": entry["store_id"], "website_id": entry["website_id"],
                "customer_group": entry["customer_group"] or False, "quantity": entry["quantity"],
            })
            States.create(vals)

    def _magento_on_deleted(self, entry):
        state = self.env["magento.price.push.state"].sudo().browse(entry.get("state_id")).exists()
        state.unlink()

    # ------------------------------------------------------------------
    # Error Center
    # ------------------------------------------------------------------
    def _magento_record_failure(self, entry, message, severity="permanent"):
        record = self.env["magento.product.map"].sudo().browse(entry.get("map_id")).exists() or None
        try:
            with self.env.cr.savepoint():
                self.env["magento.sync.issue"]._record_failure(
                    self.instance_id, "product", entry["sku"], _PRICE_ISSUE_PREFIX + str(message),
                    payload=dict(self._magento_price_payload(entry), rule=self.display_name),
                    record=record, severity=severity,
                )
        except Exception as exc:  # noqa: BLE001 - bookkeeping must never break the push
            _logger.warning("Magento price rule %s: could not record the failure of %s: %s",
                            self.id, entry.get("sku"), exc)

    def _magento_chunk_error(self, chunk, exc):
        status = MagentoAPI.bulk_status_for_exception(exc)
        severity = "transient" if status == MagentoAPI.BULK_STATUS_RETRIABLY_FAILED else "permanent"
        message = MagentoAPI.bulk_error_message(exc)
        for entry in chunk:
            self._magento_record_failure(entry, message, severity=severity)

    def _magento_record_rule_failure(self, messages, payloads):
        try:
            with self.env.cr.savepoint():
                self.env["magento.sync.issue"]._record_failure(
                    self.instance_id, "other", "price-rule-%s" % self.id,
                    _PRICE_ISSUE_PREFIX + "; ".join(dict.fromkeys(messages)),
                    payload={"rule": self.display_name, "prices": payloads}, severity="permanent",
                )
        except Exception as exc:  # noqa: BLE001
            _logger.warning("Magento price rule %s: could not record a failure: %s", self.id, exc)

    def _magento_resolve_price_issues(self, skus):
        if not skus:
            return
        Issue = self.env["magento.sync.issue"].sudo()
        keys = [Issue._make_trace_key(self.instance_id, "product", sku) for sku in skus]
        issues = Issue.search([
            ("trace_key", "in", keys), ("state", "=", "open"), ("message", "=like", _PRICE_ISSUE_PREFIX + "%"),
        ])
        if issues:
            issues.write({"state": "resolved", "resolved_by": False, "resolved_date": fields.Datetime.now()})

    # ------------------------------------------------------------------
    # Queue / actions
    # ------------------------------------------------------------------
    def _magento_check_manager(self):
        if not self.env.user.has_group("sdlc_magento_connector.group_magento_manager"):
            raise AccessError(_("Only Magento administrators can push prices to Magento."))

    def _magento_enqueue_push(self, remove_all=False, force=False, description=None):
        jobs = self.env["magento.job"]
        if remove_all:
            pattern, kwargs = "prices-remove-%s", {"remove_all": True}
        elif force:
            pattern, kwargs = "prices-full-%s", {"force": True}
        else:
            pattern, kwargs = "prices-%s", None
        for rule in self:
            jobs |= jobs.enqueue(
                rule, "_magento_push_prices",
                kwargs=kwargs,
                identity_key=pattern % rule.id,
                instance=rule.instance_id,
                channel="prices-%s" % rule.instance_id.id,
                description=description or _("Push prices to Magento (%s)", rule.display_name),
            )
        return jobs

    def _magento_notify(self, title, message):
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"title": title, "message": message, "type": "info", "sticky": False},
        }

    def action_push_prices(self):
        """Queue the price push of the selected rules (one pending job per rule)."""
        self._magento_check_manager()
        rules = self.filtered("active")
        if not rules:
            raise UserError(_("Archived price rules are not pushed."))
        jobs = rules._magento_enqueue_push()
        return self._magento_notify(
            _("Price push queued"),
            _("%(count)s job(s) will push the prices: %(jobs)s.", count=len(jobs),
              jobs=", ".join(jobs.mapped("display_name"))),
        )

    def action_resend_all(self):
        """Queue a push that sends every price again, changed or not."""
        self._magento_check_manager()
        rules = self.filtered("active")
        if not rules:
            raise UserError(_("Archived price rules are not pushed."))
        jobs = rules._magento_enqueue_push(force=True, description=_("Resend all prices to Magento"))
        return self._magento_notify(
            _("Full price push queued"),
            _("%(count)s job(s) will send every price again: %(jobs)s.", count=len(jobs),
              jobs=", ".join(jobs.mapped("display_name"))),
        )

    def action_remove_from_magento(self):
        """Queue the removal of the special and tier prices these rules created."""
        self._magento_check_manager()
        jobs = self._magento_enqueue_push(remove_all=True)
        return self._magento_notify(
            _("Price removal queued"),
            _("%(count)s job(s) will remove the prices these rules pushed: %(jobs)s.",
              count=len(jobs), jobs=", ".join(jobs.mapped("display_name"))),
        )

    def action_view_push_states(self):
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id(
            "sdlc_magento_connector.action_magento_price_push_state"
        )
        action["domain"] = [("rule_id", "=", self.id)]
        action["context"] = {"default_rule_id": self.id}
        return action

    @api.model
    def _cron_magento_push_prices(self):
        """Scheduled (inactive by default): queue every active rule with Scheduled Push on."""
        rules = self.sudo().search([("auto_push", "=", True), ("instance_id.active", "=", True)])
        return rules._magento_enqueue_push(description=_("Scheduled price push"))


def _as_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _same_number(text, number):
    try:
        return float(text) == float(number)
    except (TypeError, ValueError):
        return False


class MagentoPricePushState(models.Model):
    _name = "magento.price.push.state"
    _description = "Magento Pushed Price"
    _order = "rule_id, sku, key"
    _rec_name = "sku"

    _uniq_rule_sku_key = models.UniqueIndex(
        "(rule_id, sku, key)",
        "This price was already recorded for this rule.",
    )

    rule_id = fields.Many2one("magento.price.rule", required=True, index=True, ondelete="cascade")
    instance_id = fields.Many2one(related="rule_id.instance_id", store=True, index=True)
    company_id = fields.Many2one(related="rule_id.company_id", store=True, index=True)
    sku = fields.Char(string="SKU", required=True, index=True)
    key = fields.Char(required=True, help="Kind, scope and, for tier prices, group and quantity.")
    kind = fields.Selection(PRICE_KINDS, required=True)
    map_id = fields.Many2one("magento.product.map", string="Product Mapping", ondelete="set null")
    product_id = fields.Many2one("product.product", string="Product", ondelete="set null")
    store_id = fields.Integer(string="Store ID")
    website_id = fields.Integer(string="Website ID")
    customer_group = fields.Char()
    quantity = fields.Float(digits="Product Unit")
    price = fields.Float(digits="Product Price")
    price_from = fields.Char(string="From")
    price_to = fields.Char(string="To")
    pushed_at = fields.Datetime(string="Pushed")


class MagentoInstance(models.Model):
    _inherit = "magento.instance"

    price_rule_ids = fields.One2many("magento.price.rule", "instance_id", string="Price Rules")
    price_rule_count = fields.Integer(compute="_compute_price_rule_count")

    def _compute_price_rule_count(self):
        counts = dict(self.env["magento.price.rule"]._read_group(
            [("instance_id", "in", self.ids)], ["instance_id"], ["__count"],
        ))
        for instance in self:
            instance.price_rule_count = counts.get(instance, 0)

    def action_view_price_rules(self):
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id(
            "sdlc_magento_connector.action_magento_price_rule"
        )
        action["domain"] = [("instance_id", "=", self.id)]
        action["context"] = {"default_instance_id": self.id}
        return action


class MagentoSyncIssue(models.Model):
    _inherit = "magento.sync.issue"

    # Retrying a price failure replays the rule's push job (failed prices were not
    # recorded as pushed, so the push sends them again).
    _REPLAYABLE_METHODS = (*_BaseSyncIssue._REPLAYABLE_METHODS, "_magento_push_prices")
