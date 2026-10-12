"""Phase 2 catalog (2.1, 2.2, 2.3): configurable products as templates with variants,
bundle and grouped components, Magento categories mirrored into product.category."""
import copy
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import MagentoTestCommon, build_item, build_order, build_product, once

# 1x1 PNG
PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

COLOR = {"attribute_id": 93, "attribute_code": "color", "default_frontend_label": "Color",
         "frontend_input": "select",
         "options": [{"label": " ", "value": ""}, {"label": "Black", "value": "49"},
                     {"label": "Blue", "value": "50"}, {"label": "Orange", "value": "56"}]}
SIZE = {"attribute_id": 144, "attribute_code": "size", "default_frontend_label": "Size",
        "frontend_input": "select",
        "options": [{"label": " ", "value": ""}, {"label": "S", "value": "167"},
                    {"label": "M", "value": "168"}, {"label": "L", "value": "169"}]}
ATTRIBUTES = {"93": COLOR, "144": SIZE}
COLOR_IDS = {"Black": "49", "Blue": "50", "Orange": "56"}
SIZE_IDS = {"S": "167", "M": "168", "L": "169"}


def build_child(magento_id, size, color, parent_sku="MH02", price=70.0):
    sku = "%s-%s-%s" % (parent_sku, size, color)
    child = build_product(magento_id, sku, "Teton Pullover Hoodie-%s-%s" % (size, color),
                          price=price, visibility=1, attribute_set_id=9)
    child["custom_attributes"] += [
        {"attribute_code": "size", "value": SIZE_IDS[size]},
        {"attribute_code": "color", "value": COLOR_IDS[color]},
        {"attribute_code": "url_key", "value": sku.lower()},
    ]
    return child


def build_configurable(magento_id=100, sku="MH02", children=(), category_ids=("15", "36")):
    parent = build_product(magento_id, sku, "Teton Pullover Hoodie", type_id="configurable",
                           price=0, qty=0, attribute_set_id=9)
    sizes = sorted({c["custom_attributes"][-3]["value"] for c in children})
    colors = sorted({c["custom_attributes"][-2]["value"] for c in children})
    parent["extension_attributes"].update({
        "category_links": [{"position": 0, "category_id": c} for c in category_ids],
        "configurable_product_options": [
            {"id": 3, "attribute_id": "93", "label": "Color", "position": 1,
             "values": [{"value_index": int(v)} for v in colors], "product_id": magento_id},
            {"id": 2, "attribute_id": "144", "label": "Size", "position": 0,
             "values": [{"value_index": int(v)} for v in sizes], "product_id": magento_id},
        ],
        "configurable_product_links": [c["id"] for c in children],
    })
    parent["media_gallery_entries"] = [{"id": 900, "media_type": "image", "position": 1,
                                        "types": ["image"], "file": "/m/h/mh02.jpg"}]
    return parent


def four_children():
    return [
        build_child(101, "S", "Black"),
        build_child(102, "S", "Blue"),
        build_child(103, "M", "Black"),
        build_child(104, "M", "Blue", price=72.0),
    ]


@tagged("post_install", "-at_install", "magento")
class TestP2Catalog(MagentoTestCommon):

    def setUp(self):
        super().setUp()
        self.catalog = {}
        self.children = {}
        self.attribute_fetches = []
        test = self

        def _get_product(api, sku, *args, **kwargs):
            return copy.deepcopy(test.catalog[sku])

        def _children(instance, api, sku):
            return copy.deepcopy(test.children.get(sku, []))

        def _attribute(instance, api, attribute_id):
            test.attribute_fetches.append(str(attribute_id))
            return copy.deepcopy(ATTRIBUTES[str(attribute_id)])

        def _media(api, sku, entry_id):
            if sku in ("MH02", "24-WG080"):
                return {"id": entry_id, "content": {"base64_encoded_data": PNG_B64, "type": "image/png"}}
            return {}

        self.patch_api("get_product", side_effect=_get_product)
        self.patch_api("get_product_media", side_effect=_media)
        instance_cls = type(self.env["magento.instance"])
        self.startPatcher(patch.object(instance_cls, "_magento_fetch_configurable_children", _children))
        self.startPatcher(patch.object(instance_cls, "_magento_fetch_attribute", _attribute))

    # ------------------------------------------------------------------ helpers
    def _load(self, products, children=None):
        for product in products:
            self.catalog[product["sku"]] = product
        for parent_sku, kids in (children or {}).items():
            self.children[parent_sku] = kids
            for kid in kids:
                self.catalog.setdefault(kid["sku"], kid)

    def _sync(self, rows):
        self.patch_api("get_products", side_effect=once([copy.deepcopy(r) for r in rows]))
        return self.instance.sync_products()

    def _map(self, sku):
        return self.env["magento.product.map"].with_context(active_test=False).search([
            ("instance_id", "=", self.instance.id), ("sku", "=", sku)])

    def _products(self, sku):
        return self.env["product.product"].with_context(active_test=False).search(
            [("default_code", "=", sku)])

    def _import_configurable(self, kids=None, rows=None):
        kids = kids if kids is not None else four_children()
        parent = build_configurable(children=kids)
        self._load([parent], {"MH02": kids})
        return self._sync(rows if rows is not None else [parent])

    # ------------------------------------------------------------------ 2.1
    def test_configurable_creates_one_template_with_variants_by_child_sku(self):
        result = self._import_configurable()
        self.assertEqual(result["errors"], 0, result["error_messages"])

        parent_map = self._map("MH02")
        self.assertEqual(parent_map.type_id, "configurable")
        self.assertFalse(parent_map.odoo_product_id, "a configurable binds a template, not a product")
        template = parent_map.odoo_template_id
        self.assertTrue(template)
        self.assertEqual(template.name, "Teton Pullover Hoodie")
        self.assertEqual(template.list_price, 70.0, "configurable price 0 -> cheapest child")
        self.assertTrue(template.image_1920, "the configurable's image goes on the template")

        variants = template.product_variant_ids
        self.assertEqual(len(variants), 4, "one variant per child SKU, no extra combination")
        self.assertEqual(
            sorted(variants.mapped("default_code")),
            ["MH02-M-Black", "MH02-M-Blue", "MH02-S-Black", "MH02-S-Blue"],
        )
        self.assertEqual(
            sorted(template.attribute_line_ids.mapped("attribute_id.name")), ["Color", "Size"])
        size_line = template.attribute_line_ids.filtered(lambda l: l.attribute_id.name == "Size")
        self.assertEqual(sorted(size_line.value_ids.mapped("name")), ["M", "S"],
                         "only the option values the children use")

        for kid in four_children():
            child_map = self._map(kid["sku"])
            self.assertEqual(len(child_map), 1)
            self.assertEqual(child_map.magento_id, str(kid["id"]))
            self.assertEqual(child_map.odoo_product_id.default_code, kid["sku"])
            self.assertEqual(child_map.odoo_product_id.product_tmpl_id, template)
            self.assertEqual(child_map.odoo_template_id, template)
        s_black = self._map("MH02-S-Black").odoo_product_id
        self.assertEqual(
            sorted(s_black.product_template_attribute_value_ids.mapped("name")), ["Black", "S"])
        self.assertEqual(self._map("MH02-M-Blue").price, 72.0, "each child keeps its own price")

        links = parent_map.child_link_ids
        self.assertEqual(len(links), 4)
        self.assertEqual(set(links.mapped("link_type")), {"configurable"})
        self.assertEqual(links.child_map_id, self.env["magento.product.map"].concat(
            *[self._map(k["sku"]) for k in four_children()]))
        self.assertEqual(parent_map.magento_child_count, 4)

        # Attribute and option ids cached per instance.
        bindings = self.env["magento.product.attribute"].search([("instance_id", "=", self.instance.id)])
        self.assertEqual(sorted(bindings.mapped("attribute_code")), ["color", "size"])
        color = bindings.filtered(lambda b: b.attribute_code == "color")
        self.assertEqual(sorted(color.option_ids.mapped("value")), ["49", "50", "56"])
        self.assertEqual(color.option_ids.filtered(lambda o: o.value == "49").odoo_value_id.name, "Black")

    def test_resync_is_idempotent(self):
        self._import_configurable()
        template = self._map("MH02").odoo_template_id
        variant_ids = sorted(template.product_variant_ids.ids)
        Product = self.env["product.product"].with_context(active_test=False)
        counts = (
            Product.search_count([]),
            self.env["product.template"].with_context(active_test=False).search_count([]),
            self.env["magento.product.map"].search_count([]),
            self.env["magento.product.link"].search_count([]),
            self.env["product.attribute.value"].search_count([]),
        )
        fetches = len(self.attribute_fetches)

        result = self._import_configurable()
        self.assertEqual(result["errors"], 0, result["error_messages"])
        self.assertEqual(result["updated"], 1)
        self.assertEqual(self._map("MH02").odoo_template_id, template)
        self.assertEqual(sorted(template.product_variant_ids.ids), variant_ids)
        self.assertEqual(counts, (
            Product.search_count([]),
            self.env["product.template"].with_context(active_test=False).search_count([]),
            self.env["magento.product.map"].search_count([]),
            self.env["magento.product.link"].search_count([]),
            self.env["product.attribute.value"].search_count([]),
        ))
        self.assertEqual(len(self.attribute_fetches), fetches,
                         "known attributes and options are not fetched again")

    def test_resync_adds_new_child_and_option_value(self):
        self._import_configurable()
        template = self._map("MH02").odoo_template_id
        kids = four_children() + [build_child(105, "L", "Orange")]
        self._import_configurable(kids)
        self.assertEqual(len(template.product_variant_ids), 5)
        self.assertEqual(self._map("MH02-L-Orange").odoo_product_id.product_tmpl_id, template)
        self.assertIn("L", template.attribute_line_ids.value_ids.mapped("name"))

    def test_existing_standalone_children_are_merged_not_duplicated(self):
        # Imported earlier as plain simple products (the pre-Phase-2 import).
        black = self.create_product("MH02-S-Black", "Teton Pullover Hoodie-S-Black")
        black.product_tmpl_id.with_context(**self.no_push_ctx(), skip_odoo_sync=True).image_1920 = PNG_B64
        blue = self.create_product("MH02-M-Blue", "Teton Pullover Hoodie-M-Blue")
        black_map = self.create_map(black, 101)
        blue_map = self.create_map(blue, 104)
        black_tmpl, blue_tmpl = black.product_tmpl_id, blue.product_tmpl_id
        # Something references the old template of "blue": it must be archived, not deleted.
        self.env["product.pricelist.item"].create({
            "pricelist_id": self.env["product.pricelist"].create({"name": "P2 test"}).id,
            "applied_on": "1_product", "product_tmpl_id": blue_tmpl.id, "fixed_price": 60.0,
        })

        result = self._import_configurable()
        self.assertEqual(result["errors"], 0, result["error_messages"])
        template = self._map("MH02").odoo_template_id

        for sku, product, product_map in (("MH02-S-Black", black, black_map), ("MH02-M-Blue", blue, blue_map)):
            self.assertEqual(self._products(sku), product, "%s: same product.product kept, no duplicate" % sku)
            self.assertEqual(product.product_tmpl_id, template, "%s moved under the template" % sku)
            self.assertEqual(product_map.odoo_product_id, product)
            self.assertEqual(product_map.odoo_template_id, template)
            self.assertEqual(len(self._map(sku)), 1)
        self.assertEqual(len(template.product_variant_ids), 4)
        self.assertTrue(black.image_variant_1920, "the old template's image stays on the variant")
        self.assertFalse(black_tmpl.exists(), "an unreferenced emptied template is deleted")
        self.assertTrue(blue_tmpl.exists())
        self.assertFalse(blue_tmpl.active, "a referenced emptied template is archived")

    def test_children_imported_before_their_configurable_are_merged(self):
        kids = four_children()
        parent = build_configurable(children=kids)
        self._load([parent], {"MH02": kids})
        # Magento lists the children (updated earlier) before the configurable.
        result = self._sync(kids + [parent])
        self.assertEqual(result["errors"], 0, result["error_messages"])
        template = self._map("MH02").odoo_template_id
        self.assertEqual(len(template.product_variant_ids), 4)
        for kid in kids:
            self.assertEqual(len(self._products(kid["sku"])), 1)
            self.assertEqual(self._map(kid["sku"]).odoo_product_id.product_tmpl_id, template)
        self.assertEqual(
            self.env["product.template"].with_context(active_test=False).search_count(
                [("name", "like", "Teton Pullover Hoodie-")]), 0,
            "no standalone template is left behind")

        # Children synced again afterwards: still variants, template not renamed.
        self._sync(kids)
        self.assertEqual(template.name, "Teton Pullover Hoodie")
        self.assertEqual(len(template.product_variant_ids), 4)

    def test_incompatible_standalone_child_is_replaced_and_archived(self):
        dozen = self.env.ref("uom.product_uom_dozen")
        # The first standalone child sets the template's unit; this one differs.
        self.create_map(self.create_product("MH02-S-Black", "Teton Pullover Hoodie-S-Black"), 101)
        odd = self.create_product("MH02-S-Blue", "Teton Pullover Hoodie-S-Blue", uom_id=dozen.id)
        odd_map = self.create_map(odd, 102)
        self._import_configurable()
        template = self._map("MH02").odoo_template_id
        variant = odd_map.odoo_product_id
        self.assertNotEqual(variant, odd, "a product with another unit is not moved")
        self.assertEqual(variant.product_tmpl_id, template)
        self.assertEqual(variant.default_code, "MH02-S-Blue")
        self.assertFalse(odd.active, "the old product is archived for its history")

    def test_order_line_by_child_sku_resolves_to_variant(self):
        self._import_configurable()
        self.patch_order_side_calls()
        variant = self._map("MH02-S-Blue").odoo_product_id
        parent = build_item(5001, "MH02-S-Blue", qty=2.0, price=70.0, product_id=100,
                            product_type="configurable", name="Teton Pullover Hoodie")
        child = build_item(5002, "MH02-S-Blue", qty=2.0, price=0.0, product_id=102,
                           parent_item_id=5001, name="Teton Pullover Hoodie-S-Blue")
        self.import_orders([build_order(3001, [parent, child])])
        order = self.magento_orders(3001)
        lines = order.order_line.filtered(lambda l: not l.display_type)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines.product_id, variant, "the order line uses the child SKU's variant")

    def test_template_edit_does_not_overwrite_child_skus(self):
        self._import_configurable()
        template = self._map("MH02").odoo_template_id
        template.write({"list_price": 99.0, "name": "Renamed hoodie"})
        child_map = self._map("MH02-M-Blue")
        self.assertEqual(child_map.price, 72.0)
        self.assertEqual(child_map.name, "Teton Pullover Hoodie-M-Blue")
        jobs = self.env["magento.job"].search([("identity_key", "like", "push-magento.product.map-%")])
        self.assertFalse(jobs, "no push of the child SKUs for a template-level edit")
        # A child map edit does not rename the template either.
        child_map.with_context(magento_skip_push=True).write({"name": "Other name", "price": 1.0})
        self.assertEqual(template.name, "Renamed hoodie")
        self.assertEqual(template.list_price, 99.0)

    def test_configurable_export_is_blocked(self):
        self._import_configurable()
        parent_map = self._map("MH02")
        create_mock = self.patch_api("create_product", return_value={})
        update_mock = self.patch_api("update_product", return_value={})
        with self.assertRaisesRegex(UserError, "configurable"):
            parent_map.action_update_magento_product()
        with self.assertRaisesRegex(UserError, "not supported"):
            parent_map.action_create_magento_product()
        parent_map._magento_push_now(set())
        self.assertFalse(create_mock.called or update_mock.called)

    def test_url_key_collision_blocks_simple_export(self):
        attribute_set = self.env["magento.attribute.set"].with_context(**self.no_push_ctx()).create({
            "instance_id": self.instance.id, "name": "Default", "magento_id": "4"})
        product = self.create_product("NEW-TEE", "Teton Pullover Hoodie")
        product_map = self.create_map(product, 1, attribute_set_id=attribute_set.id)
        product_map.with_context(**self.no_push_ctx(), skip_odoo_sync=True).magento_id = False
        search = self.patch_api("get_products", return_value=[{"sku": "MH02", "id": 100}])
        create_mock = self.patch_api("create_product", return_value={})
        with self.assertRaisesRegex(UserError, "teton-pullover-hoodie.*MH02"):
            product_map.action_create_magento_product()
        self.assertFalse(create_mock.called)
        self.assertEqual(search.call_args.kwargs["filters"], [("url_key", "teton-pullover-hoodie", "eq")])

    # ------------------------------------------------------------------ 2.2
    def _bundle_catalog(self):
        components = [
            build_product(11, "24-WG081-blue", "Sprite Stasis Ball 55 cm", price=23),
            build_product(12, "24-WG082-blue", "Sprite Stasis Ball 65 cm", price=27),
            build_product(13, "24-WG084", "Sprite Foam Yoga Brick", price=5),
            build_product(14, "24-WG085", "Sprite Yoga Strap 6 foot", price=14),
            build_product(15, "24-WG086", "Sprite Yoga Strap 8 foot", price=17),
        ]
        bundle = build_product(46, "24-WG080", "Sprite Yoga Companion Kit", type_id="bundle", price=0)
        bundle["extension_attributes"]["bundle_product_options"] = [
            {"option_id": 1, "title": "Sprite Stasis Ball", "required": True, "type": "radio",
             "position": 1, "sku": "24-WG080", "product_links": [
                 {"id": "1", "sku": "24-WG081-blue", "option_id": 1, "qty": 1, "position": 1,
                  "is_default": True, "price": 23, "price_type": None, "can_change_quantity": 1},
                 {"id": "2", "sku": "24-WG082-blue", "option_id": 1, "qty": 1, "position": 2,
                  "is_default": False, "price": 27, "price_type": None, "can_change_quantity": 1}]},
            {"option_id": 2, "title": "Sprite Foam Yoga Brick", "required": True, "type": "radio",
             "position": 2, "sku": "24-WG080", "product_links": [
                 {"id": "4", "sku": "24-WG084", "option_id": 2, "qty": 2, "position": 1,
                  "is_default": True, "price": 5, "price_type": None, "can_change_quantity": 1}]},
        ]
        grouped = build_product(45, "24-WG085_Group", "Set of Sprite Yoga Straps", type_id="grouped",
                                price=None)
        grouped["product_links"] = [
            {"sku": "24-WG085_Group", "link_type": "associated", "linked_product_sku": "24-WG085",
             "linked_product_type": "simple", "position": 0, "extension_attributes": {"qty": 0}},
            {"sku": "24-WG085_Group", "link_type": "associated", "linked_product_sku": "24-WG086",
             "linked_product_type": "simple", "position": 1, "extension_attributes": {"qty": 0}},
        ]
        self._load(components + [bundle, grouped])
        return components, bundle, grouped

    def test_bundle_and_grouped_import_with_links(self):
        components, bundle, grouped = self._bundle_catalog()
        # The bundle and the group come first: their components are linked when they arrive.
        result = self._sync([bundle, grouped] + components)
        self.assertEqual(result["errors"], 0, result["error_messages"])

        bundle_map = self._map("24-WG080")
        self.assertEqual(bundle_map.type_id, "bundle")
        self.assertEqual(bundle_map.odoo_product_id.default_code, "24-WG080")
        self.assertEqual(len(bundle_map.child_link_ids), 3)
        self.assertTrue(all(bundle_map.child_link_ids.mapped("child_map_id")),
                        "components imported after the bundle are linked")

        parts = bundle_map._magento_bundle_components()
        self.assertEqual([p["sku"] for p in parts], ["24-WG081-blue", "24-WG082-blue", "24-WG084"])
        self.assertEqual(parts[0]["option_title"], "Sprite Stasis Ball")
        self.assertTrue(parts[0]["required"] and parts[0]["is_default"])
        self.assertEqual(parts[0]["product"], self._map("24-WG081-blue").odoo_product_id)
        self.assertEqual(parts[2]["qty"], 2.0)
        kit = bundle_map._magento_bundle_components(default_only=True)
        self.assertEqual([p["sku"] for p in kit], ["24-WG081-blue", "24-WG084"])

        grouped_map = self._map("24-WG085_Group")
        self.assertEqual(grouped_map.type_id, "grouped")
        self.assertEqual(sorted(grouped_map.child_link_ids.mapped("child_sku")), ["24-WG085", "24-WG086"])
        self.assertEqual(grouped_map.child_link_ids.child_map_id,
                         self._map("24-WG085") | self._map("24-WG086"))
        self.assertFalse(grouped_map.odoo_product_id.sale_ok, "a grouped product is not sold as such")
        self.assertEqual(self._map("24-WG085").parent_link_ids.parent_map_id, grouped_map)

        if "mrp.bom" in self.env:
            bom = self.env["mrp.bom"].search([("product_tmpl_id", "=", bundle_map.odoo_template_id.id)])
            self.assertEqual(bom.type, "phantom")
            self.assertEqual(sorted(bom.bom_line_ids.mapped("product_id.default_code")),
                             ["24-WG081-blue", "24-WG084"])

        # Re-sync: same links, no duplicates; a removed component disappears.
        bundle["extension_attributes"]["bundle_product_options"][0]["product_links"].pop()
        self.catalog["24-WG080"] = bundle
        self._sync([bundle, grouped])
        self.assertEqual(sorted(bundle_map.child_link_ids.mapped("child_sku")), ["24-WG081-blue", "24-WG084"])
        self.assertEqual(len(grouped_map.child_link_ids), 2)

    # ------------------------------------------------------------------ 2.3
    def _category_tree(self, gear_name="Gear"):
        return {
            "id": 2, "parent_id": 1, "name": "Default Category", "is_active": True, "level": 1,
            "children_data": [
                {"id": 3, "parent_id": 2, "name": gear_name, "is_active": True, "level": 2,
                 "children_data": [
                     {"id": 4, "parent_id": 3, "name": "Bags", "is_active": True, "level": 3,
                      "children_data": []}]},
                {"id": 11, "parent_id": 2, "name": "Men", "is_active": True, "level": 2,
                 "children_data": []},
            ],
        }

    def _categ(self, magento_id):
        return self.env["magento.category"].search([
            ("instance_id", "=", self.instance.id), ("magento_id", "=", str(magento_id))]).categ_id

    def test_category_tree_mirrored_and_product_category_set(self):
        self.patch_api("get_categories", return_value=self._category_tree())
        self.instance.sync_categories()
        root = self.instance.magento_categ_root_id
        self.assertEqual(root.name, self.instance.name)
        default, gear, bags = self._categ(2), self._categ(3), self._categ(4)
        self.assertEqual(default.parent_id, root)
        self.assertEqual(gear.parent_id, default)
        self.assertEqual((bags.name, bags.parent_id), ("Bags", gear))
        self.assertEqual(self._categ(11).parent_id, default)

        # Renamed in Magento: the same Odoo category is renamed, nothing duplicated.
        count = self.env["product.category"].search_count([])
        self.patch_api("get_categories", return_value=self._category_tree("Gear & Fitness"))
        self.instance.sync_categories()
        self.assertEqual(self._categ(3), gear)
        self.assertEqual(gear.name, "Gear & Fitness")
        self.assertEqual(self.env["product.category"].search_count([]), count)

        bag = build_product(1, "24-MB01", "Joust Duffle Bag")
        bag["extension_attributes"]["category_links"] = [
            {"position": 0, "category_id": "3"}, {"position": 0, "category_id": "4"}]
        self._load([bag])
        self._sync([bag])
        template = self._map("24-MB01").odoo_product_id.product_tmpl_id
        self.assertEqual(template.categ_id, bags, "deepest Magento category by default")

        self.instance.magento_product_categ_policy = "first"
        self._sync([bag])
        self.assertEqual(template.categ_id, gear)

        # A configurable's category goes on its template; its variants are untouched.
        self.instance.magento_product_categ_policy = "deepest"
        kids = four_children()
        parent = build_configurable(children=kids, category_ids=("3", "4"))
        self._load([parent], {"MH02": kids})
        self._sync([parent])
        self.assertEqual(self._map("MH02").odoo_template_id.categ_id, bags)
        self.assertEqual(self._map("MH02").odoo_categ_id, bags)

    def test_category_policy_none_and_manual_mapping(self):
        self.instance.write({"magento_product_categ_policy": "none"})
        self.patch_api("get_categories", return_value=self._category_tree())
        self.instance.sync_categories()
        bag = build_product(1, "24-MB01", "Joust Duffle Bag")
        bag["extension_attributes"]["category_links"] = [{"position": 0, "category_id": "4"}]
        self._load([bag])
        self._sync([bag])
        template = self._map("24-MB01").odoo_product_id.product_tmpl_id
        self.assertNotEqual(template.categ_id, self._categ(4), "policy none keeps the Odoo category")

        # Mirroring off: the category hand-mapped on the Magento category is used.
        manual = self.env["product.category"].create({"name": "Luggage"})
        self.instance.write({"magento_product_categ_policy": "deepest", "magento_mirror_categories": False})
        self.env["magento.category"].search([
            ("instance_id", "=", self.instance.id), ("magento_id", "=", "4")]).categ_id = manual
        self._sync([bag])
        self.assertEqual(template.categ_id, manual)

    def test_pending_component_links_survive_and_resolve(self):
        components, bundle, _grouped = self._bundle_catalog()
        self._sync([bundle])
        bundle_map = self._map("24-WG080")
        self.assertFalse(any(bundle_map.child_link_ids.mapped("child_map_id")))
        self.assertTrue(all(not p["product"] for p in bundle_map._magento_bundle_components()))
        self._sync(components[:1])
        first = bundle_map.child_link_ids.filtered(lambda l: l.child_sku == "24-WG081-blue")
        self.assertEqual(first.child_map_id, self._map("24-WG081-blue"))

    # ------------------------------------------------------------------ product kind
    def test_products_imported_storable_services_and_kits(self):
        components, bundle, grouped = self._bundle_catalog()
        ebook = build_product(80, "EBOOK-1", "Yoga ebook", type_id="downloadable")
        voucher = build_product(81, "CLASS-1", "Yoga class", type_id="virtual")
        self._load([ebook, voucher])
        self._sync(components + [bundle, grouped, ebook, voucher])
        self._import_configurable()

        def tmpl(sku):
            return self._map(sku).odoo_template_id

        self.assertTrue(tmpl("24-WG084").is_storable, "simple product -> storable goods")
        self.assertEqual(tmpl("24-WG084").type, "consu")
        self.assertTrue(tmpl("MH02").is_storable, "configurable variants -> storable goods")
        self.assertTrue(self._map("MH02-S-Black").odoo_product_id.is_storable)
        self.assertEqual(tmpl("EBOOK-1").type, "service")
        self.assertEqual(tmpl("CLASS-1").type, "service")
        self.assertEqual((tmpl("24-WG080").type, tmpl("24-WG080").is_storable), ("consu", False),
                         "a bundle is a kit, not stocked itself")
        self.assertFalse(tmpl("24-WG085_Group").is_storable)

    def test_storable_option_off_and_products_with_moves_untouched(self):
        moved = self.create_product("24-MB01", "Joust Duffle Bag")
        self.create_map(moved, 1)
        warehouse = self.env["stock.warehouse"].search([("company_id", "=", self.env.company.id)], limit=1)
        self.env["stock.move"].create({
            "product_id": moved.id, "product_uom_qty": 1.0, "product_uom": moved.uom_id.id,
            "location_id": warehouse.lot_stock_id.id,
            "location_dest_id": self.env.ref("stock.stock_location_customers").id,
        })
        bag = build_product(1, "24-MB01", "Joust Duffle Bag")
        pack = build_product(4, "24-MB04", "Strive Shoulder Pack")
        self._load([bag, pack])
        self.instance.import_products_storable = False
        self._sync([bag, pack])
        self.assertFalse(self._map("24-MB04").odoo_product_id.is_storable, "option off: left as goods")
        self.instance.import_products_storable = True
        self._sync([bag, pack])
        self.assertTrue(self._map("24-MB04").odoo_product_id.is_storable,
                        "no stock move: switched to storable on the next import")
        self.assertFalse(moved.is_storable, "a product with stock moves is never flipped")

    def test_product_push_sends_stock_only_without_stock_push(self):
        attribute_set = self.env["magento.attribute.set"].with_context(**self.no_push_ctx()).create({
            "instance_id": self.instance.id, "name": "Default", "magento_id": "4"})
        product_map = self.create_map(self.create_product("24-MB01", "Joust Duffle Bag"), 1,
                                      attribute_set_id=attribute_set.id, quantity=7.0)
        payload = product_map._build_magento_payload()["product"]
        self.assertEqual(payload["extension_attributes"]["stock_item"]["qty"], 7.0)
        if "stock_push_enabled" in self.instance._fields:
            self.instance.stock_push_enabled = True
            payload = product_map._build_magento_payload()["product"]
            self.assertNotIn("stock_item", payload.get("extension_attributes") or {},
                             "the stock push owns Magento's quantity")

    def test_unsupported_type_still_skipped(self):
        card = build_product(77, "GIFT-50", "Gift Card", type_id="giftcard")
        self._load([card])
        result = self._sync([card])
        self.assertEqual(result["skipped"], 1)
        self.assertFalse(self._map("GIFT-50"))
