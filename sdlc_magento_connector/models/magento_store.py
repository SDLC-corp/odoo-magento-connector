"""Phase 2 (2.8): Magento websites, store groups and store views, per instance.

Magento scopes catalog values: a value written without a store code goes to the default
store view only, ``/rest/all/V1`` writes the global (admin) value, and
``/rest/<store_code>/V1`` reads or writes one store view. Translations therefore live on
store views, and website-scoped prices on the store codes of a website.

``magento.instance._magento_discover_stores()`` mirrors the hierarchy
(``/V1/store/websites``, ``/V1/store/storeGroups``, ``/V1/store/storeViews`` and
``/V1/store/storeConfigs``), maps each store view's locale to an Odoo language and its
currencies to Odoo currencies. It is idempotent: records are matched by Magento id, and
those Magento no longer returns are archived.

Contracts
---------
* ``magento.instance._magento_discover_stores()`` -> dict of counts (onboarding wizard).
* ``magento.website._magento_store_codes(sync_only=False)`` -> list of store codes of the
  websites, default store view first (pricelist export, item 2.9).
"""
import logging

import requests

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError

from ..services.magento_api import MagentoAPI
from .sql_compat import UniqueIndex

_logger = logging.getLogger(__name__)

# Magento's admin website / group / store (id 0) is not a storefront.
_ADMIN_ID = 0


def _int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class MagentoWebsite(models.Model):
    _name = "magento.website"
    _description = "Magento Website"
    _order = "instance_id, is_default desc, sequence, magento_id"

    _uniq_instance_website = UniqueIndex(
        "(instance_id, magento_id)", "This Magento website is already known for this instance.",
    )

    instance_id = fields.Many2one("magento.instance", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(related="instance_id.company_id", store=True, index=True)
    magento_id = fields.Integer(string="Magento Website ID", required=True, readonly=True)
    code = fields.Char(readonly=True)
    name = fields.Char(required=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    is_default = fields.Boolean(
        string="Default Website", readonly=True,
        help="Website of the instance's store view (else of the 'default' store view).",
    )
    default_group_id = fields.Many2one("magento.store.group", string="Default Store", readonly=True)
    group_ids = fields.One2many("magento.store.group", "website_id", string="Stores")
    store_view_ids = fields.One2many("magento.store.view", "website_id", string="Store Views")
    base_currency_id = fields.Many2one(
        "res.currency", string="Base Currency", readonly=True,
        help="Base currency of the website's default store view.",
    )
    store_view_count = fields.Integer(compute="_compute_store_view_count", string="Store View Count")

    @api.depends("store_view_ids")
    def _compute_store_view_count(self):
        for record in self:
            record.store_view_count = len(record.store_view_ids)

    def _magento_store_codes(self, sync_only=False):
        """Store codes of these websites' store views, each website's default store view
        first. ``sync_only=True`` keeps the views ticked "Sync" only.

        Website-scoped values (prices when the catalog price scope is Website) are written
        through any store code of the website: ``/rest/<code>/V1/...``."""
        codes = []
        for website in self:
            views = website.store_view_ids.filtered(lambda v: v.code)
            if sync_only:
                views = views.filtered("active_for_sync")
            for view in views.sorted(lambda v: (not v.is_default, v.sequence, v.magento_id)):
                if view.code not in codes:
                    codes.append(view.code)
        return codes


class MagentoStoreGroup(models.Model):
    _name = "magento.store.group"
    _description = "Magento Store (Group)"
    _order = "instance_id, website_id, magento_id"

    _uniq_instance_group = UniqueIndex(
        "(instance_id, magento_id)", "This Magento store is already known for this instance.",
    )

    instance_id = fields.Many2one("magento.instance", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(related="instance_id.company_id", store=True, index=True)
    magento_id = fields.Integer(string="Magento Store ID", required=True, readonly=True)
    code = fields.Char(readonly=True)
    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
    website_id = fields.Many2one("magento.website", string="Website", ondelete="cascade", index=True)
    root_category_magento_id = fields.Integer(string="Root Category ID", readonly=True)
    default_store_view_id = fields.Many2one("magento.store.view", string="Default Store View", readonly=True)
    store_view_ids = fields.One2many("magento.store.view", "group_id", string="Store Views")


class MagentoStoreView(models.Model):
    _name = "magento.store.view"
    _description = "Magento Store View"
    _order = "instance_id, website_id, is_default desc, sequence, magento_id"

    _uniq_instance_view = UniqueIndex(
        "(instance_id, magento_id)", "This Magento store view is already known for this instance.",
    )

    instance_id = fields.Many2one("magento.instance", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(related="instance_id.company_id", store=True, index=True)
    magento_id = fields.Integer(string="Magento Store View ID", required=True, readonly=True)
    code = fields.Char(readonly=True, index=True)
    name = fields.Char(required=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    website_id = fields.Many2one("magento.website", string="Website", ondelete="cascade", index=True)
    group_id = fields.Many2one("magento.store.group", string="Store", ondelete="set null")
    magento_active = fields.Boolean(string="Enabled in Magento", readonly=True, default=True)
    is_default = fields.Boolean(
        string="Default View", readonly=True,
        help="Default store view of its website (the default store's default view).",
    )
    is_instance_default = fields.Boolean(
        string="Instance Default", compute="_compute_is_instance_default",
        help="The store view the instance reads and writes by default: its values are the "
             "Odoo base values, the other views carry translations.",
    )
    locale = fields.Char(readonly=True, help="Magento locale (general/locale/code), e.g. fr_FR.")
    lang_id = fields.Many2one(
        "res.lang", string="Odoo Language", context={"active_test": False},
        help="Odoo language of the store view's locale. Translations are imported and "
             "exported for views whose language is installed in Odoo.",
    )
    lang_installed = fields.Boolean(related="lang_id.active", string="Language Installed")
    base_currency_id = fields.Many2one("res.currency", string="Base Currency", readonly=True)
    display_currency_id = fields.Many2one("res.currency", string="Display Currency", readonly=True)
    base_url = fields.Char(string="Base URL", readonly=True)
    timezone = fields.Char(readonly=True)
    active_for_sync = fields.Boolean(
        string="Sync", default=True,
        help="Import and export this store view's translations (product name, descriptions, "
             "meta fields) and use it for store-scoped values.",
    )

    @api.depends("instance_id.store_code", "instance_id.store_id", "code", "magento_id", "is_default")
    def _compute_is_instance_default(self):
        defaults = {}
        for record in self:
            instance = record.instance_id
            if instance.id not in defaults:
                defaults[instance.id] = instance._magento_default_store_view()
            record.is_instance_default = record == defaults[instance.id]

    def action_magento_activate_lang(self):
        """Install (activate) the Odoo language of these store views (Magento managers)."""
        if not self.env.user.has_group("sdlc_magento_connector.group_magento_manager"):
            raise AccessError("Only Magento managers can install the language of a store view.")
        for record in self.filtered(lambda v: v.lang_id and not v.lang_id.active):
            self.env["res.lang"].sudo()._activate_lang(record.lang_id.code)
        return True

    @api.model
    def _magento_lang_for_locale(self, locale):
        """Odoo language (active or not) of a Magento locale (``fr_FR``, ``zh_Hans_CN``...)."""
        locale = (locale or "").strip().replace("-", "_")
        if not locale:
            return self.env["res.lang"]
        Lang = self.env["res.lang"].with_context(active_test=False).sudo()
        parts = locale.split("_")
        candidates = [locale]
        if len(parts) == 3:
            # Script subtag: zh_Hans_CN -> zh_CN, sr_Latn_RS -> sr@latin.
            candidates.append("%s_%s" % (parts[0], parts[2]))
            if parts[1].lower() == "latn":
                candidates.append("%s@latin" % parts[0])
        for code in candidates:
            lang = Lang.search([("code", "=", code)], limit=1)
            if lang:
                return lang
        prefix = parts[0].lower()
        langs = Lang.search(["|", ("iso_code", "=", prefix), ("code", "=like", prefix + "\\_%")])
        if not langs:
            return Lang.browse()
        # Prefer an installed language, then the "main" one (fr_FR for fr), then any.
        main = "%s_%s" % (prefix, prefix.upper())
        return langs.sorted(lambda l: (not l.active, l.code != main, l.code))[:1]


class MagentoInstance(models.Model):
    _inherit = "magento.instance"

    magento_website_ids = fields.One2many("magento.website", "instance_id", string="Websites")
    magento_store_view_ids = fields.One2many("magento.store.view", "instance_id", string="Store Views")
    magento_store_view_count = fields.Integer(
        compute="_compute_magento_store_view_count", string="Store View Count",
    )
    magento_stores_discovered_at = fields.Datetime(string="Stores Discovered", readonly=True)

    @api.depends("magento_store_view_ids")
    def _compute_magento_store_view_count(self):
        for record in self:
            record.magento_store_view_count = len(record.magento_store_view_ids)

    # ------------------------------------------------------------------
    # Magento reads (patched in tests)
    # ------------------------------------------------------------------
    def _magento_fetch_store_data(self, api):
        """``(websites, groups, store_views, store_configs)`` lists from Magento."""
        def _list(value):
            return [row for row in value or [] if isinstance(row, dict)]
        return (
            _list(api.get_websites()),
            _list(api.get_store_groups()),
            _list(api.get_store_views()),
            _list(api.get_store_configs()),
        )

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------
    def _magento_discover_stores(self):
        """Mirror the Magento websites, stores and store views of this instance.

        Fetches ``/V1/store/websites``, ``/V1/store/storeGroups``, ``/V1/store/storeViews``
        and ``/V1/store/storeConfigs``. Records are matched by Magento id, so running it
        again updates them; records Magento no longer returns are archived. The admin
        scope (id 0) is skipped. Each store view gets its locale's Odoo language (found,
        not installed) and its currencies. Blank instance scope fields (website, store id,
        store code) are filled from the default store view.

        Raises ``UserError`` when Magento cannot be read. Returns (summed over ``self``)::

            {"websites": int, "groups": int, "store_views": int,
             "created": int, "updated": int, "archived": int}
        """
        totals = {"websites": 0, "groups": 0, "store_views": 0, "created": 0, "updated": 0, "archived": 0}
        for instance in self:
            api = MagentoAPI(instance)
            try:
                websites, groups, views, configs = instance._magento_fetch_store_data(api)
            except requests.exceptions.RequestException as exc:
                raise UserError(
                    "Could not read the Magento stores of %s: %s"
                    % (instance.name, instance._format_magento_request_error(exc, instance))
                ) from exc
            counts = instance._magento_apply_store_data(websites, groups, views, configs)
            for key, value in counts.items():
                totals[key] += value
        return totals

    def _magento_apply_store_data(self, websites, groups, views, configs):
        """Upsert the hierarchy from the Magento JSON lists; see ``_magento_discover_stores``."""
        self.ensure_one()
        counts = {"websites": 0, "groups": 0, "store_views": 0, "created": 0, "updated": 0, "archived": 0}
        Website = self.env["magento.website"].with_context(active_test=False).sudo()
        Group = self.env["magento.store.group"].with_context(active_test=False).sudo()
        View = self.env["magento.store.view"].with_context(active_test=False).sudo()
        Currency = self.env["res.currency"].with_context(active_test=False).sudo()
        currencies = {}

        def _currency(code):
            code = (code or "").strip().upper()
            if not code:
                return False
            if code not in currencies:
                currencies[code] = Currency.search([("name", "=", code)], limit=1).id or False
            return currencies[code]

        def _upsert(model, existing, magento_id, vals):
            record = existing.get(magento_id)
            if record:
                changed = {}
                for key, value in vals.items():
                    current = record[key]
                    if isinstance(current, models.BaseModel):
                        current = current.id or False
                    if current != value:
                        changed[key] = value
                if changed:
                    record.write(changed)
                    counts["updated"] += 1
                return record
            record = model.create(dict(vals, instance_id=self.id, magento_id=magento_id))
            existing[magento_id] = record
            counts["created"] += 1
            return record

        def _archive(records):
            stale = records.filtered("active")
            if stale:
                stale.write({"active": False})
                counts["archived"] += len(stale)

        # Websites
        site_by_id = {w.magento_id: w for w in Website.search([("instance_id", "=", self.id)])}
        seen_sites = Website.browse()
        for row in websites:
            magento_id = _int(row.get("id"))
            if magento_id is None or magento_id == _ADMIN_ID:
                continue
            seen_sites |= _upsert(Website, site_by_id, magento_id, {
                "name": row.get("name") or row.get("code") or "Website %s" % magento_id,
                "code": row.get("code") or False,
                "active": True,
            })
        _archive(Website.browse([w.id for w in site_by_id.values()]) - seen_sites)
        counts["websites"] = len(seen_sites)

        # Store groups
        group_by_id = {g.magento_id: g for g in Group.search([("instance_id", "=", self.id)])}
        seen_groups = Group.browse()
        group_default_view = {}
        for row in groups:
            magento_id = _int(row.get("id"))
            if magento_id is None or magento_id == _ADMIN_ID:
                continue
            website = site_by_id.get(_int(row.get("website_id")))
            seen_groups |= _upsert(Group, group_by_id, magento_id, {
                "name": row.get("name") or row.get("code") or "Store %s" % magento_id,
                "code": row.get("code") or False,
                "website_id": website.id if website else False,
                "root_category_magento_id": _int(row.get("root_category_id"), 0),
                "active": True,
            })
            group_default_view[magento_id] = _int(row.get("default_store_id"))
        _archive(Group.browse([g.id for g in group_by_id.values()]) - seen_groups)
        counts["groups"] = len(seen_groups)

        # Store views (+ store configs: locale, currencies, base URL)
        config_by_id = {}
        for row in configs:
            store_id = _int(row.get("id") if row.get("id") is not None else row.get("store_id"))
            if store_id is not None:
                config_by_id[store_id] = row
        view_by_id = {v.magento_id: v for v in View.search([("instance_id", "=", self.id)])}
        seen_views = View.browse()
        lang_model = self.env["magento.store.view"]
        for row in views:
            magento_id = _int(row.get("id"))
            if magento_id is None or magento_id == _ADMIN_ID:
                continue
            config = config_by_id.get(magento_id, {})
            website = site_by_id.get(_int(row.get("website_id")))
            group = group_by_id.get(_int(row.get("store_group_id")))
            locale = config.get("locale") or False
            vals = {
                "name": row.get("name") or row.get("code") or "Store View %s" % magento_id,
                "code": row.get("code") or False,
                "website_id": website.id if website else False,
                "group_id": group.id if group else False,
                "magento_active": bool(_int(row.get("is_active"), 1)),
                "locale": locale,
                "base_currency_id": _currency(config.get("base_currency_code")),
                "display_currency_id": _currency(config.get("default_display_currency_code")),
                "base_url": config.get("secure_base_url") or config.get("base_url") or False,
                "timezone": config.get("timezone") or False,
                "active": True,
            }
            current = view_by_id.get(magento_id)
            # The language is proposed from the locale; a language chosen by hand is kept
            # until the locale itself changes.
            if not current or not current.lang_id or current.locale != locale:
                vals["lang_id"] = lang_model._magento_lang_for_locale(locale).id or False
            seen_views |= _upsert(View, view_by_id, magento_id, vals)
        _archive(View.browse([v.id for v in view_by_id.values()]) - seen_views)
        counts["store_views"] = len(seen_views)

        # Defaults: each group's default view, each website's default group and view.
        for magento_id, group in group_by_id.items():
            view = view_by_id.get(group_default_view.get(magento_id))
            if group in seen_groups and group.default_store_view_id != (view or View):
                group.default_store_view_id = view.id if view else False
        default_views = View.browse()
        for row in websites:
            website = site_by_id.get(_int(row.get("id")))
            if not website or website not in seen_sites:
                continue
            group = group_by_id.get(_int(row.get("default_group_id")))
            if website.default_group_id != (group or Group):
                website.default_group_id = group.id if group else False
            view = group.default_store_view_id if group else View
            if not view:
                view = website.store_view_ids.sorted("magento_id")[:1]
            default_views |= view
            if view and website.base_currency_id != view.base_currency_id:
                website.base_currency_id = view.base_currency_id
        for view in seen_views:
            is_default = view in default_views
            if view.is_default != is_default:
                view.is_default = is_default

        default_site = self._magento_pick_default_website(seen_sites, seen_views)
        for website in seen_sites:
            if website.is_default != (website == default_site):
                website.is_default = website == default_site

        # Blank instance scope fields: the default store view's.
        default_view = self._magento_default_store_view()
        if default_view:
            scope = {}
            if not self.store_code and default_view.code:
                scope["store_code"] = default_view.code
            if not self.store_id:
                scope["store_id"] = default_view.magento_id
            if not self.website_id and default_view.website_id:
                scope["website_id"] = default_view.website_id.magento_id
            if scope:
                self.sudo().write(scope)
        self.sudo().write({"magento_stores_discovered_at": fields.Datetime.now()})
        return counts

    def _magento_pick_default_website(self, websites, views):
        """Magento's REST API does not flag the default website: take the instance's
        website, else the website of the ``default`` store view, else the lowest id."""
        self.ensure_one()
        if not websites:
            return websites
        if self.website_id:
            match = websites.filtered(lambda w: w.magento_id == self.website_id)
            if match:
                return match[:1]
        default_code = views.filtered(lambda v: v.code == "default").website_id
        if default_code:
            return default_code[:1]
        return websites.sorted("magento_id")[:1]

    def action_magento_discover_stores(self):
        """Button: discover websites, stores and store views."""
        self.ensure_one()
        counts = self._magento_discover_stores()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Magento Stores",
                "message": "%(websites)s website(s), %(groups)s store(s), %(store_views)s store "
                           "view(s): %(created)s created, %(updated)s updated, %(archived)s "
                           "archived." % counts,
                "type": "success",
                "sticky": False,
                "next": {"type": "ir.actions.client", "tag": "soft_reload"},
            },
        }

    # ------------------------------------------------------------------
    # Store view helpers
    # ------------------------------------------------------------------
    def _magento_default_store_view(self):
        """The store view the instance works with by default: its store code, else its
        store id, else the default view of the default website."""
        self.ensure_one()
        views = self.magento_store_view_ids
        if not views:
            return views
        if self.store_code:
            match = views.filtered(lambda v: v.code == self.store_code)
            if match:
                return match[:1]
        if self.store_id:
            match = views.filtered(lambda v: v.magento_id == self.store_id)
            if match:
                return match[:1]
        defaults = views.filtered(lambda v: v.is_default and v.website_id.is_default)
        return (defaults or views.filtered("is_default") or views.sorted("magento_id"))[:1]

    def _magento_default_lang_code(self):
        """Odoo language holding the Magento default (all store views) values: the default
        store view's language when installed, else ``en_US``."""
        self.ensure_one()
        lang = self._magento_default_store_view().lang_id
        return lang.code if lang and lang.active else "en_US"

    def _magento_translation_store_views(self):
        """Store views whose values are translations: ticked "Sync", enabled in Magento,
        with an installed Odoo language other than the default one."""
        self.ensure_one()
        default_lang = self._magento_default_lang_code()
        return self.magento_store_view_ids.filtered(
            lambda v: v.active_for_sync and v.magento_active and v.code and v.lang_id
            and v.lang_id.active and v.lang_id.code != default_lang
        ).sorted(lambda v: (not v.website_id.is_default, not v.is_default, v.sequence, v.magento_id))
