"""Phase 2 (2.8): store discovery, per-store-view translations, full media gallery."""
import base64
import copy
import io

from PIL import Image

from odoo.tests import tagged

from odoo.addons.sdlc_magento_connector.services.magento_api import MagentoAPI

from .common import MagentoTestCommon, build_product, make_response, once

WEBSITES = [
    {"id": 1, "code": "base", "name": "Main Website", "default_group_id": 1},
    {"id": 0, "code": "admin", "name": "Admin", "default_group_id": 0},
]
GROUPS = [
    {"id": 0, "website_id": 0, "root_category_id": 0, "default_store_id": 0, "name": "Default", "code": "default"},
    {"id": 1, "website_id": 1, "root_category_id": 2, "default_store_id": 1,
     "name": "Main Website Store", "code": "main_website_store"},
]
VIEWS = [
    {"id": 1, "code": "default", "name": "Default Store View", "website_id": 1, "store_group_id": 1, "is_active": 1},
    {"id": 2, "code": "fr", "name": "French", "website_id": 1, "store_group_id": 1, "is_active": 1},
    {"id": 3, "code": "de", "name": "German", "website_id": 1, "store_group_id": 1, "is_active": 1},
    {"id": 0, "code": "admin", "name": "Admin", "website_id": 0, "store_group_id": 0, "is_active": 1},
]
CONFIGS = [
    {"id": 1, "code": "default", "website_id": 1, "locale": "en_US", "base_currency_code": "USD",
     "default_display_currency_code": "USD", "base_url": "https://magento.example.test/"},
    {"id": 2, "code": "fr", "website_id": 1, "locale": "fr_FR", "base_currency_code": "USD",
     "default_display_currency_code": "EUR", "base_url": "https://magento.example.test/fr/"},
    {"id": 3, "code": "de", "website_id": 1, "locale": "de_DE", "base_currency_code": "USD",
     "default_display_currency_code": "EUR", "base_url": "https://magento.example.test/de/"},
]


def png_b64(color):
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), color).save(buffer, "PNG")
    return base64.b64encode(buffer.getvalue()).decode()


IMAGES = {11: png_b64("red"), 12: png_b64("green"), 13: png_b64("blue"), 14: png_b64("yellow")}


def gallery(*entry_ids, files=None):
    files = files or {}
    entries = []
    for position, entry_id in enumerate(entry_ids, start=1):
        entries.append({
            "id": entry_id, "media_type": "image", "label": "Image %s" % entry_id,
            "position": position, "disabled": False,
            "types": ["image", "small_image", "thumbnail"] if position == 1 else [],
            "file": files.get(entry_id, "/m/b/mb01-%s.jpg" % entry_id),
        })
    return entries


@tagged("post_install", "-at_install", "magento")
class TestP2Stores(MagentoTestCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.attribute_set = cls.env["magento.attribute.set"].create({
            "instance_id": cls.instance.id, "magento_id": "4", "name": "Default",
        })

    def setUp(self):
        super().setUp()
        self.store_data = {
            "get_websites": copy.deepcopy(WEBSITES), "get_store_groups": copy.deepcopy(GROUPS),
            "get_store_views": copy.deepcopy(VIEWS), "get_store_configs": copy.deepcopy(CONFIGS),
        }
        for name in self.store_data:
            self.patch_api(name, side_effect=lambda api, n=name: copy.deepcopy(self.store_data[n]))
        self.media_fetches = []

        def _media(api, sku, entry_id):
            self.media_fetches.append(int(entry_id))
            return {"id": entry_id, "content": {"base64_encoded_data": IMAGES[int(entry_id)], "type": "image/png"}}

        self.patch_api("get_product_media", side_effect=_media)

    # ------------------------------------------------------------------ helpers
    def _install_langs(self):
        for code in ("fr_FR", "de_DE"):
            self.env["res.lang"]._activate_lang(code)

    def _discover(self):
        return self.instance._magento_discover_stores()

    def _view(self, code):
        return self.instance.magento_store_view_ids.filtered(lambda v: v.code == code)

    def _bag(self, sku="24-MB01", name="Joust Duffle Bag", magento_id=1):
        product = self.create_product(sku, name)
        mapping = self.create_map(product, magento_id, attribute_set_id=self.attribute_set.id,
                                  name=name, price=34.0)
        return product, mapping

    def _bag_data(self, sku="24-MB01", name="Joust Duffle Bag", entries=()):
        data = build_product(1, sku, name, price=34.0)
        data["custom_attributes"] += [
            {"attribute_code": "short_description", "value": "A duffle bag"},
        ]
        data["media_gallery_entries"] = list(entries)
        return data

    # ------------------------------------------------------------------ discovery
    def test_discovery_creates_hierarchy_and_is_idempotent(self):
        counts = self._discover()
        self.assertEqual(
            (counts["websites"], counts["groups"], counts["store_views"], counts["created"]),
            (1, 1, 3, 5),
        )
        website = self.instance.magento_website_ids
        self.assertEqual(len(website), 1, "The admin website (id 0) is not a storefront")
        self.assertEqual((website.code, website.magento_id), ("base", 1))
        self.assertTrue(website.is_default)
        self.assertEqual(website.default_group_id.code, "main_website_store")
        self.assertEqual(website.base_currency_id.name, "USD")
        views = self.instance.magento_store_view_ids
        self.assertEqual(sorted(views.mapped("code")), ["de", "default", "fr"])
        default = self._view("default")
        self.assertTrue(default.is_default)
        self.assertTrue(default.is_instance_default)
        self.assertFalse(self._view("fr").is_default)
        self.assertEqual(self._view("fr").lang_id.code, "fr_FR")
        self.assertEqual(self._view("fr").display_currency_id.name, "EUR")
        self.assertEqual(self._view("de").group_id, website.default_group_id)
        self.assertEqual(website.default_group_id.default_store_view_id, default)
        # Blank scope fields of the instance come from the default store view.
        self.assertEqual((self.instance.store_code, self.instance.store_id, self.instance.website_id),
                         ("default", 1, 1))

        # Second run: nothing new, nothing changed.
        ids = (website.id, views.ids)
        counts = self._discover()
        self.assertEqual((counts["created"], counts["updated"], counts["archived"]), (0, 0, 0))
        self.assertEqual((self.instance.magento_website_ids.id, self.instance.magento_store_view_ids.ids), ids)

        # A language picked by hand is kept; a view gone from Magento is archived.
        self._view("fr").lang_id = self.env.ref("base.lang_fr_BE")
        self.store_data["get_store_views"] = [v for v in VIEWS if v["code"] != "de"]
        counts = self._discover()
        self.assertEqual(counts["archived"], 1)
        self.assertEqual(sorted(self.instance.magento_store_view_ids.mapped("code")), ["default", "fr"])
        self.assertEqual(self._view("fr").lang_id.code, "fr_BE")

    def test_locale_to_language_mapping(self):
        View = self.env["magento.store.view"]
        expected = {
            "fr_FR": "fr_FR", "de_DE": "de_DE", "en_GB": "en_GB", "pt_BR": "pt_BR",
            "zh_Hans_CN": "zh_CN", "zh_Hant_TW": "zh_TW", "nl-NL": "nl_NL",
        }
        for locale, code in expected.items():
            self.assertEqual(View._magento_lang_for_locale(locale).code, code, locale)
        self.assertFalse(View._magento_lang_for_locale("xx_YY"))
        self.assertFalse(View._magento_lang_for_locale(False))
        # Unknown country of a known language: the language's main locale.
        self.assertEqual(View._magento_lang_for_locale("fr_XX").code, "fr_FR")

    def test_store_codes_helper(self):
        self._discover()
        website = self.instance.magento_website_ids
        self.assertEqual(website._magento_store_codes(), ["default", "fr", "de"])
        self._view("de").active_for_sync = False
        self.assertEqual(website._magento_store_codes(sync_only=True), ["default", "fr"])
        self.assertEqual(website._magento_store_codes(), ["default", "fr", "de"])

    def test_translation_views_need_installed_language(self):
        self._discover()
        self.assertFalse(self.instance._magento_translation_store_views(),
                         "fr_FR / de_DE are not installed yet")
        self._view("fr").action_magento_activate_lang()
        self.assertTrue(self._view("fr").lang_installed)
        self.assertEqual(self.instance._magento_translation_store_views().mapped("code"), ["fr"])
        self._install_langs()
        self.assertEqual(self.instance._magento_translation_store_views().mapped("code"), ["fr", "de"])
        self._view("de").active_for_sync = False
        self.assertEqual(self.instance._magento_translation_store_views().mapped("code"), ["fr"])

    # ------------------------------------------------------------------ translations
    def test_translation_import_writes_only_differing_values(self):
        self._install_langs()
        self._discover()
        data = self._bag_data()
        store_values = {
            # name and short description translated; description equal to the default
            "fr": dict(copy.deepcopy(data), name="Sac de sport Joust", custom_attributes=[
                {"attribute_code": "description", "value": "<p>Joust Duffle Bag</p>"},
                {"attribute_code": "short_description", "value": "Un sac de sport"},
            ]),
            # only the (HTML, term-translated) description, with another structure
            "de": dict(copy.deepcopy(data), custom_attributes=[
                {"attribute_code": "description",
                 "value": "<p>Eine Sporttasche.</p><ul><li>Robust</li><li>Leicht</li></ul>"},
                {"attribute_code": "short_description", "value": "A duffle bag"},
            ]),
        }
        store_calls = self.patch_api(
            "get_product_for_store",
            side_effect=lambda api, sku, code: copy.deepcopy(store_values[code]))
        self.patch_api("get_products", side_effect=once([copy.deepcopy(data)]))
        self.patch_api("get_product", side_effect=lambda api, sku: copy.deepcopy(data))
        counts = self.instance.sync_products()
        self.assertEqual(counts["errors"], 0, counts)

        mapping = self.env["magento.product.map"].search([("instance_id", "=", self.instance.id),
                                                          ("sku", "=", "24-MB01")])
        template = mapping.odoo_product_id.product_tmpl_id
        self.assertEqual(sorted(c.args[2] for c in store_calls.call_args_list), ["de", "fr"])
        self.assertEqual(template.with_context(lang="en_US").name, "Joust Duffle Bag")
        self.assertEqual(template.with_context(lang="fr_FR").name, "Sac de sport Joust")
        self.assertEqual(template.with_context(lang="fr_FR").description_sale, "Un sac de sport")
        self.assertEqual(template.with_context(lang="en_US").description_sale, "A duffle bag")
        names = template._fields["name"]._get_stored_translations(template)
        self.assertNotIn("de_DE", names, "A value equal to the default is not stored")
        descriptions = template._fields["description"]._get_stored_translations(template) or {}
        self.assertNotIn("fr_FR", descriptions, "The French description equals the default")
        self.assertIn("Eine Sporttasche.", template.with_context(lang="de_DE").description)
        self.assertIn("<li>Leicht</li>", template.with_context(lang="de_DE").description)
        self.assertEqual(template.with_context(lang="en_US").description, "<p>Joust Duffle Bag</p>")
        self.assertEqual(template.with_context(lang="de_DE").description_sale, "A duffle bag")

        # Imported translations are in sync: a push does not send them back.
        update = self.patch_api("update_product_for_store", return_value={})
        mapping._magento_export_translations(MagentoAPI(self.instance))
        self.assertFalse(update.called)

        # Importing again is a no-op.
        self.patch_api("get_products", side_effect=once([copy.deepcopy(data)]))
        self.instance.sync_products()
        self.assertEqual(template.with_context(lang="fr_FR").name, "Sac de sport Joust")

    def test_translation_export_sends_store_code_urls_with_translated_fields_only(self):
        self._install_langs()
        self._discover()
        product, mapping = self._bag()
        template = product.product_tmpl_id
        template.with_context(magento_skip_push=True).update_field_translations(
            "name", {"fr_FR": "Sac de sport Joust"})
        template.with_context(magento_skip_push=True).update_field_translations(
            "description_sale", {"en_US": "A duffle bag", "fr_FR": "Un sac de sport"})

        default_push = self.patch_api("update_product", return_value={"sku": "24-MB01"})
        store_push = self.patch_api("update_product_for_store", return_value={"sku": "24-MB01"})
        mapping._magento_push_now(set())
        self.assertEqual(default_push.call_count, 1, "The default (all-scope) push comes first")
        self.assertEqual(store_push.call_count, 1, "Only the French view has a translation")
        _api, sku, code, payload = store_push.call_args.args
        self.assertEqual((sku, code), ("24-MB01", "fr"))
        self.assertEqual(payload, {"product": {
            "sku": "24-MB01", "name": "Sac de sport Joust",
            "custom_attributes": [{"attribute_code": "short_description", "value": "Un sac de sport"}],
        }})
        # Same translation: not sent again.
        mapping._magento_push_now(set())
        self.assertEqual(store_push.call_count, 1)

    def test_store_scope_requests_use_the_store_code_url(self):
        """The store code is the URL scope, never a fallback to another scope."""
        payload = {"product": {"sku": "24-MB01", "name": "Sac de sport Joust"}}
        api = MagentoAPI(self.instance)
        requests_sent = []

        def _request(api_self, method, endpoint, **kwargs):
            requests_sent.append((method, endpoint, kwargs.get("payload")))
            return make_response(200, {"sku": "24-MB01"})

        self.patch_api("_request", side_effect=_request)
        api.update_product_for_store("24-MB01", "fr", payload)
        api.get_product_for_store("24 MB/01", "de")
        self.assertEqual(requests_sent[0], ("put", "/rest/fr/V1/products/24-MB01", payload))
        self.assertEqual(requests_sent[1][:2], ("get", "/rest/de/V1/products/24%20MB%2F01"))

    def test_translation_edit_in_odoo_queues_a_push(self):
        self._install_langs()
        self._discover()
        product, mapping = self._bag()
        product.product_tmpl_id.update_field_translations("name", {"fr_FR": "Sac Joust"})
        job = self.env["magento.job"].search([("identity_key", "=", "push-magento.product.map-%s" % mapping.id)])
        self.assertEqual(len(job), 1)
        self.assertIn("translations", job.args[0])

    # ------------------------------------------------------------------ gallery
    def _import_gallery(self, data):
        self.patch_api("get_products", side_effect=once([copy.deepcopy(data)]))
        self.patch_api("get_product", side_effect=lambda api, sku: copy.deepcopy(data))
        counts = self.instance.sync_products()
        self.assertEqual(counts["errors"], 0, counts)
        return self.env["magento.product.map"].search([("instance_id", "=", self.instance.id),
                                                       ("sku", "=", data["sku"])])

    def test_gallery_import_of_three_images_is_idempotent(self):
        data = self._bag_data(entries=gallery(11, 12, 13))
        mapping = self._import_gallery(data)
        media = mapping.magento_media_ids
        self.assertEqual(len(media), 3)
        main = media.filtered("is_main")
        self.assertEqual(main.magento_entry_id, "11")
        self.assertEqual(main.roles, "image,small_image,thumbnail")
        self.assertFalse(main.image, "The main image is the product image")
        self.assertEqual(mapping.magento_image_entry_id, "11")
        self.assertEqual(base64.b64decode(mapping.odoo_product_id.image_1920),
                         base64.b64decode(IMAGES[11]))
        extras = (media - main).sorted("position")
        self.assertEqual(extras.mapped("magento_entry_id"), ["12", "13"])
        self.assertEqual(extras.mapped("origin"), ["magento", "magento"])
        self.assertFalse(any(extras.mapped("created_by_connector")))
        self.assertEqual([base64.b64decode(e.image) for e in extras],
                         [base64.b64decode(IMAGES[12]), base64.b64decode(IMAGES[13])])
        # The product import fetched the main image, the gallery import the two others.
        self.assertEqual(sorted(self.media_fetches), [11, 12, 13])

        # Second import: same records, no extra image fetched.
        self.media_fetches.clear()
        ids = media.ids
        signatures = media.mapped("sync_signature")
        mapping = self._import_gallery(data)
        self.assertEqual(mapping.magento_media_ids.ids, ids)
        self.assertEqual(mapping.magento_media_ids.mapped("sync_signature"), signatures)
        self.assertEqual(self.media_fetches, [11], "Only the product import's main image")
        counts = mapping._magento_import_gallery(MagentoAPI(self.instance), copy.deepcopy(data))
        self.assertEqual(counts, {"created": 0, "updated": 0, "removed": 0, "fetched": 0})

        # A changed file is fetched again; same content: nothing rewritten but the file.
        moved = self._bag_data(entries=gallery(11, 12, 13, files={12: "/m/b/mb01-12_1.jpg"}))
        counts = mapping._magento_import_gallery(MagentoAPI(self.instance), moved)
        self.assertEqual((counts["fetched"], counts["created"], counts["updated"]), (1, 0, 1))
        self.assertEqual(mapping.magento_media_ids.filtered(lambda m: m.magento_entry_id == "12").file,
                         "/m/b/mb01-12_1.jpg")

        # An image removed in Magento goes away in Odoo.
        counts = mapping._magento_import_gallery(
            MagentoAPI(self.instance), self._bag_data(entries=gallery(11, 12)))
        self.assertEqual(counts["removed"], 1)
        self.assertEqual(sorted(mapping.magento_media_ids.mapped("magento_entry_id")), ["11", "12"])

    def test_gallery_export_never_deletes_foreign_images(self):
        data = self._bag_data(entries=gallery(11, 12, 13))
        mapping = self._import_gallery(data)
        magento = {"entries": copy.deepcopy(data["media_gallery_entries"])}
        self.patch_api("get_product", side_effect=lambda api, sku: dict(
            copy.deepcopy(data), media_gallery_entries=copy.deepcopy(magento["entries"])))

        def _add(api, sku, payload):
            entry = dict(payload["entry"], id=14, file="/o/d/odoo-14.png")
            entry.pop("content", None)
            magento["entries"].append(entry)
            return "14"

        add = self.patch_api("add_product_media", side_effect=_add)
        update = self.patch_api("update_product_media", return_value=True)
        delete = self.patch_api("delete_product_media", return_value=True)
        self.patch_api("update_product", return_value={})
        api = MagentoAPI(self.instance)

        # Nothing changed in Odoo: nothing sent.
        self.assertEqual(mapping._magento_export_gallery(api), {"added": 0, "updated": 0, "deleted": 0})
        self.assertFalse(add.called or update.called or delete.called)

        # An image added in Odoo is created in Magento, without taking the main roles.
        new = self.env["magento.product.media"].create({
            "map_id": mapping.id, "label": "Side view", "position": 4, "image": IMAGES[14],
        })
        mapping._magento_push_now(set())
        self.assertEqual(add.call_count, 1)
        entry = add.call_args.args[2]["entry"]
        self.assertEqual((entry["label"], entry["position"], entry["types"]), ("Side view", 4, []))
        self.assertEqual(entry["content"]["type"], "image/png")
        self.assertEqual(entry["content"]["base64_encoded_data"], IMAGES[14])
        self.assertEqual((new.magento_entry_id, new.created_by_connector), ("14", True))
        self.assertFalse(update.called)

        # A Magento image relabelled in Odoo is updated by entry id, content unchanged.
        foreign = mapping.magento_media_ids.filtered(lambda m: m.magento_entry_id == "12")
        foreign.label = "Front view"
        mapping._magento_push_now(set())
        self.assertEqual(update.call_count, 1)
        _api, sku, entry_id, payload = update.call_args.args
        self.assertEqual((sku, str(entry_id), payload["entry"]["label"]), ("24-MB01", "12", "Front view"))
        self.assertNotIn("content", payload["entry"])
        self.assertEqual(add.call_count, 1)

        # Deleting a Magento image in Odoo never deletes it in Magento...
        foreign.unlink()
        mapping._magento_push_now(set())
        self.assertFalse(delete.called)
        # ... an image the connector created is deleted.
        new.unlink()
        self.assertEqual(mapping.magento_gallery_pending_delete, "14")
        mapping._magento_push_now(set())
        self.assertEqual([c.args[2] for c in delete.call_args_list], ["14"])
        self.assertFalse(mapping.magento_gallery_pending_delete)
        self.assertNotIn("11", [str(c.args[2]) for c in delete.call_args_list])

    def test_variant_images_are_kept_on_the_variant(self):
        self._install_langs()
        self._discover()
        color = self.env["product.attribute"].create({
            "name": "Bag Color", "create_variant": "always",
            "value_ids": [(0, 0, {"name": "Blue"}), (0, 0, {"name": "Black"})],
        })
        template = self.env["product.template"].with_context(**self.no_push_ctx(), skip_odoo_sync=True).create({
            "name": "Joust Duffle Bag",
            "attribute_line_ids": [(0, 0, {"attribute_id": color.id, "value_ids": [(6, 0, color.value_ids.ids)]})],
        })
        blue, black = template.product_variant_ids
        blue.with_context(**self.no_push_ctx(), skip_odoo_sync=True).default_code = "24-MB01-Blue"
        mapping = self.create_map(blue, 21, sku="24-MB01-Blue", attribute_set_id=self.attribute_set.id)
        self.assertEqual(mapping._magento_gallery_owner(), ("product_variant_id", blue))
        self.assertFalse(mapping._magento_translation_target(),
                         "A variant's text belongs to its configurable's template")

        data = self._bag_data(sku="24-MB01-Blue", entries=gallery(11, 12))
        store_calls = self.patch_api("get_product_for_store", return_value={})
        counts = mapping._magento_import_gallery(MagentoAPI(self.instance), data)
        self.assertEqual(counts["created"], 2)
        self.assertEqual(mapping._magento_import_translations(MagentoAPI(self.instance), data), {})
        self.assertFalse(store_calls.called)
        extra = mapping.magento_media_ids.filtered(lambda m: not m.is_main)
        self.assertEqual(extra.magento_entry_id, "12")
        self.assertEqual(base64.b64decode(extra.image), base64.b64decode(IMAGES[12]))
        self.assertEqual(mapping.magento_image_entry_id, "11")
        # The other variant keeps no image of this SKU.
        self.assertFalse(self.env["magento.product.media"].search([("map_id.odoo_product_id", "=", black.id)]))
