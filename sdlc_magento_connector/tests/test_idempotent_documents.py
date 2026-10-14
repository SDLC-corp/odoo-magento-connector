"""Item 1.7: idempotent invoice / shipment / credit memo pushes.

POST /V1/order/{id}/invoice|ship|refund is not idempotent. Before posting, the
connector looks for a matching, unlinked Magento document and links it; after an
ambiguous failure it looks again once before raising.
"""
from unittest.mock import patch

import requests

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import MagentoTestCommon

LOGGER = "odoo.addons.sdlc_magento_connector.models.sale_order_inherit"


def _doc(entity_id, items, created_at="2026-10-01 10:00:00", **extra):
    """Magento invoice/shipment/credit memo as returned by the search endpoints."""
    vals = {
        "entity_id": entity_id,
        "increment_id": "%09d" % (100000000 + entity_id),
        "order_id": 7001,
        "created_at": created_at,
        "items": [{"order_item_id": item_id, "qty": qty} for item_id, qty in items],
    }
    vals.update(extra)
    return vals


@tagged("post_install", "-at_install", "magento")
class TestIdempotentDocuments(MagentoTestCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        ctx = dict(skip_magento_sync=True, magento_skip_push=True, skip_odoo_sync=True)
        Product = cls.env["product.product"].with_context(**ctx)
        cls.bag = Product.create({"name": "Joust Duffle Bag", "default_code": "24-MB01",
                                  "type": "consu", "list_price": 34.0})
        cls.hoodie = Product.create({"name": "Chaz Kangeroo Hoodie-XS-Black",
                                     "default_code": "MH01-XS-Black", "type": "consu",
                                     "list_price": 52.0})
        cls.partner = cls.env["res.partner"].create({"name": "Veronica Costello",
                                                     "email": "roni_cost@example.com"})
        cls.order = cls.env["sale.order"].with_context(**ctx).create({
            "partner_id": cls.partner.id,
            "magento_instance_id": cls.instance.id,
            "magento_order_id": "7001",
            "magento_status": "processing",
            "order_line": [
                Command.create({"product_id": cls.bag.id, "product_uom_qty": 1.0,
                                "price_unit": 34.0, "tax_id": [Command.clear()]}),
                Command.create({"product_id": cls.hoodie.id, "product_uom_qty": 2.0,
                                "price_unit": 52.0, "tax_id": [Command.clear()]}),
            ],
        }).with_env(cls.env)
        cls.bag_line, cls.hoodie_line = cls.order.order_line
        # Simple line: item 11. Configurable: child 22 of parent 21 (pushed as 21).
        cls.bag_line.write({"magento_item_id": "11"})
        cls.hoodie_line.write({"magento_item_id": "22", "magento_parent_item_id": "21"})

    def setUp(self):
        super().setUp()
        SaleOrder = type(self.env["sale.order"])
        self.startPatcher(patch.object(
            SaleOrder, "_magento_refresh_from_magento", lambda order, api, magento_id: None))

    # ------------------------------------------------------------------ fixtures
    def _move(self, move_type, qtys=None):
        """Posted invoice/refund for the order (no push): qtys = {sale line: qty}."""
        qtys = qtys or {self.bag_line: 1.0, self.hoodie_line: 2.0}
        ctx = dict(skip_magento_sync=True, default_move_type=move_type)
        move = self.env["account.move"].with_context(**ctx).create({
            "move_type": move_type,
            "partner_id": self.partner.id,
            "invoice_origin": self.order.name,
            "invoice_line_ids": [
                Command.create(line._prepare_invoice_line(quantity=qty))
                for line, qty in qtys.items()
            ],
        })
        move.with_context(skip_magento_sync=True).action_post()
        move = move.with_env(self.env)
        self.assertEqual(move.magento_sale_order_id, self.order)
        self.assertEqual(move.magento_instance_id, self.instance)
        return move

    def _picking(self):
        """Fresh done outgoing delivery of the order, not linked to Magento."""
        Picking = self.env["stock.picking"].with_context(skip_magento_sync=True)
        picking_type = self.env["stock.picking.type"].search([
            ("code", "=", "outgoing"), ("company_id", "=", self.env.company.id)], limit=1)
        picking = Picking.create({
            "picking_type_id": picking_type.id,
            "partner_id": self.partner.id,
            "location_id": picking_type.default_location_src_id.id,
            "location_dest_id": self.env.ref("stock.stock_location_customers").id,
            "move_ids": [
                Command.create({
                    "name": line.product_id.display_name,
                    "product_id": line.product_id.id,
                    "product_uom_qty": line.product_uom_qty,
                    "product_uom": line.product_uom.id,
                    "sale_line_id": line.id,
                    "location_id": picking_type.default_location_src_id.id,
                    "location_dest_id": self.env.ref("stock.stock_location_customers").id,
                })
                for line in (self.bag_line, self.hoodie_line)
            ],
        })
        picking.write({"sale_id": self.order.id})
        picking.action_confirm()
        for move in picking.move_ids:
            move.quantity = move.product_uom_qty
            move.picked = True
        picking._action_done()
        picking = picking.with_env(self.env)
        self.assertEqual(picking.state, "done")
        self.assertEqual(picking.magento_instance_id, self.instance)
        return picking

    # Full document (bag 1 + hoodie 2). Magento lists the configurable parent
    # and its child; both map to the hoodie line and count once.
    FULL = ((11, 1.0), (21, 2.0), (22, 2.0))

    def _kind_setup(self, kind):
        """(record, push callable, list getter, create method, id field, magento doc extra)."""
        if kind == "invoice":
            record = self._move("out_invoice")
            return record, record._magento_create_invoice, "get_invoices", "create_invoice", \
                "magento_invoice_id", {}
        if kind == "credit_memo":
            record = self._move("out_refund")
            return record, record._magento_create_credit_memo, "get_credit_memos", \
                "create_credit_memo", "magento_credit_memo_id", \
                {"grand_total": record.amount_total, "tax_amount": 0.0,
                 "subtotal": record.amount_total}
        record = self._picking()
        return record, record._magento_create_shipment, "get_shipments", "create_shipment", \
            "magento_shipment_id", {}

    def _mock(self, getter, create, docs=(), create_kwargs=None):
        list_mock = self.patch_api(getter, return_value=list(docs))
        create_mock = self.patch_api(create, **(create_kwargs or {"return_value": 999}))
        # Read-back after a POST (shipment increment id / credit memo totals).
        self.patch_api("get_shipment", return_value={"entity_id": 999, "increment_id": "000000999"})
        self.patch_api("get_credit_memo", return_value={"entity_id": 999})
        return list_mock, create_mock

    # ------------------------------------------------------------------ (a)
    def _check_already_linked(self, kind):
        record, push, getter, create, field, _extra = self._kind_setup(kind)
        record.with_context(skip_magento_sync=True).write({field: "555"})
        list_mock, create_mock = self._mock(getter, create)
        push()
        self.assertFalse(list_mock.called, "%s: linked document must not query Magento" % kind)
        self.assertFalse(create_mock.called, "%s: linked document must not POST" % kind)
        self.assertFalse(self.http_attempts)
        self.assertEqual(record[field], "555")

    def test_a_invoice_already_linked(self):
        self._check_already_linked("invoice")

    def test_a_shipment_already_linked(self):
        self._check_already_linked("shipment")

    def test_a_credit_memo_already_linked(self):
        self._check_already_linked("credit_memo")

    # ------------------------------------------------------------------ (b)
    def _check_matching_exists(self, kind):
        record, push, getter, create, field, extra = self._kind_setup(kind)
        older = _doc(601, self.FULL, created_at="2026-10-01 10:00:00", **extra)
        newer = _doc(602, self.FULL, created_at="2026-10-02 10:00:00", **extra)
        _list, create_mock = self._mock(getter, create, docs=[older, newer])
        push()
        self.assertFalse(create_mock.called, "%s: matching Magento document must not be re-posted" % kind)
        self.assertEqual(record[field], "602", "%s: the most recent unlinked match is linked" % kind)

    def test_b_invoice_matching_exists(self):
        self._check_matching_exists("invoice")

    def test_b_shipment_matching_exists(self):
        self._check_matching_exists("shipment")
        self.assertTrue(self.env["stock.picking"].search([("magento_shipment_id", "=", "602")])
                        .magento_shipment_increment_id)

    def test_b_credit_memo_matching_exists(self):
        self._check_matching_exists("credit_memo")

    # ------------------------------------------------------------------ (c)
    def _check_non_matching_posts(self, kind, docs_fn):
        record, push, getter, create, field, extra = self._kind_setup(kind)
        _list, create_mock = self._mock(getter, create, docs=docs_fn(record, extra))
        push()
        self.assertEqual(create_mock.call_count, 1, "%s: no match, so POST" % kind)
        payload = create_mock.call_args.args[2]
        self.assertEqual(
            sorted((i["order_item_id"], i["qty"]) for i in payload["items"]),
            [(11, 1.0), (21, 2.0)],
        )
        self.assertEqual(record[field], "999")

    def _other_qty(self, record, extra):
        return [_doc(701, ((11, 1.0), (21, 1.0), (22, 1.0)), **extra)]

    def test_c_invoice_non_matching_posts(self):
        self._check_non_matching_posts("invoice", self._other_qty)

    def test_c_shipment_non_matching_posts(self):
        self._check_non_matching_posts("shipment", self._other_qty)

    def test_c_credit_memo_non_matching_posts(self):
        self._check_non_matching_posts("credit_memo", self._other_qty)

    def test_c_credit_memo_other_total_posts(self):
        # Same items, different grand total: a different credit memo (e.g. adjustment fee).
        self._check_non_matching_posts("credit_memo", lambda record, extra: [
            _doc(702, self.FULL, grand_total=record.amount_total + 5.0)])

    def test_c_invoice_unmapped_item_posts(self):
        # An extra Magento item without an Odoo line makes the document different.
        self._check_non_matching_posts("invoice", lambda record, extra: [
            _doc(703, self.FULL + ((99, 1.0),))])

    # ------------------------------------------------------------------ (d)
    def _check_timeout_then_found(self, kind):
        record, push, getter, create, field, extra = self._kind_setup(kind)
        created = _doc(801, self.FULL, **extra)
        list_mock = self.patch_api(getter, side_effect=[[], [created]])
        create_mock = self.patch_api(create, side_effect=requests.exceptions.ReadTimeout("read timed out"))
        with self.assertLogs(LOGGER, level="WARNING"):
            push()
        self.assertEqual(create_mock.call_count, 1)
        self.assertEqual(list_mock.call_count, 2, "%s: one re-check after the timeout" % kind)
        self.assertEqual(record[field], "801")

    def test_d_invoice_timeout_but_created(self):
        self._check_timeout_then_found("invoice")

    def test_d_shipment_timeout_but_created(self):
        self._check_timeout_then_found("shipment")

    def test_d_credit_memo_timeout_but_created(self):
        self._check_timeout_then_found("credit_memo")

    def test_d_invoice_502_but_created(self):
        record, push, getter, create, field, extra = self._kind_setup("invoice")
        response = requests.models.Response()
        response.status_code = 502
        self.patch_api(getter, side_effect=[[], [_doc(802, self.FULL)]])
        self.patch_api(create, side_effect=requests.exceptions.HTTPError("502", response=response))
        with self.assertLogs(LOGGER, level="WARNING"):
            push()
        self.assertEqual(record[field], "802")

    def test_d_invoice_400_is_not_rechecked(self):
        record, push, getter, create, field, extra = self._kind_setup("invoice")
        response = requests.models.Response()
        response.status_code = 400
        list_mock = self.patch_api(getter, return_value=[])
        self.patch_api(create, side_effect=requests.exceptions.HTTPError("400", response=response))
        with self.assertRaises(UserError):
            push()
        self.assertEqual(list_mock.call_count, 1, "A 4xx is a certain failure: no re-check")
        self.assertFalse(record[field])

    # ------------------------------------------------------------------ (e)
    def _check_timeout_not_found_then_retry(self, kind):
        record, push, getter, create, field, extra = self._kind_setup(kind)
        list_mock = self.patch_api(getter, return_value=[])
        create_mock = self.patch_api(create, side_effect=requests.exceptions.ReadTimeout("read timed out"))
        with self.assertLogs(LOGGER, level="WARNING"), self.assertRaises(UserError):
            push()
        self.assertEqual(list_mock.call_count, 2)
        self.assertFalse(record[field])

        # The POST had in fact been applied: the retry finds and links it.
        self.patch_api(getter, return_value=[_doc(901, self.FULL, **extra)])
        create_mock = self.patch_api(create, return_value=999)
        push()
        self.assertFalse(create_mock.called, "%s: the retry must not POST again" % kind)
        self.assertEqual(record[field], "901")

    def test_e_invoice_timeout_not_found_then_retry(self):
        self._check_timeout_not_found_then_retry("invoice")

    def test_e_shipment_timeout_not_found_then_retry(self):
        self._check_timeout_not_found_then_retry("shipment")

    def test_e_credit_memo_timeout_not_found_then_retry(self):
        self._check_timeout_not_found_then_retry("credit_memo")

    # ------------------------------------------------------------------ (f)
    def _check_linked_elsewhere_not_reused(self, kind):
        record, push, getter, create, field, extra = self._kind_setup(kind)
        if kind == "shipment":
            other = self._picking()
        else:
            other = self._move(record.move_type)
        other.with_context(skip_magento_sync=True).write({field: "1001"})
        _list, create_mock = self._mock(getter, create, docs=[_doc(1001, self.FULL, **extra)])
        push()
        self.assertEqual(create_mock.call_count, 1,
                         "%s: a document linked to another Odoo record is never reused" % kind)
        self.assertEqual(record[field], "999")
        self.assertEqual(other[field], "1001")

    def test_f_invoice_linked_elsewhere_not_reused(self):
        self._check_linked_elsewhere_not_reused("invoice")

    def test_f_shipment_linked_elsewhere_not_reused(self):
        self._check_linked_elsewhere_not_reused("shipment")

    def test_f_credit_memo_linked_elsewhere_not_reused(self):
        self._check_linked_elsewhere_not_reused("credit_memo")

    # ------------------------------------------------------------------ action_post / lock
    def test_action_post_links_existing_invoice(self):
        """User-triggered path: posting the invoice links the existing Magento invoice."""
        self.patch_api("get_invoices", return_value=[_doc(611, self.FULL)])
        create_mock = self.patch_api("create_invoice", return_value=999)
        move = self.env["account.move"].with_context(skip_magento_sync=True).create({
            "move_type": "out_invoice",
            "partner_id": self.partner.id,
            "invoice_line_ids": [
                Command.create(self.bag_line._prepare_invoice_line(quantity=1.0)),
                Command.create(self.hoodie_line._prepare_invoice_line(quantity=2.0)),
            ],
        }).with_env(self.env)
        move.action_post()
        self.assertFalse(create_mock.called)
        self.assertEqual(move.magento_invoice_id, "611")

    def test_concurrent_push_is_refused(self):
        """FOR UPDATE NOWAIT: a row locked by another transaction gives a clear UserError."""
        row = self.env.ref("base.be")  # committed row never written by this test
        with self.env.registry.cursor() as other_cr:
            other_cr.execute("SELECT id FROM res_country WHERE id = %s FOR UPDATE NOWAIT", [row.id])
            with self.assertRaisesRegex(UserError, "already running"):
                self.env["sale.order"]._magento_lock_push_row(row)
            other_cr.rollback()
        # The test transaction is still usable after the refused lock.
        self.env["sale.order"]._magento_lock_push_row(row)
