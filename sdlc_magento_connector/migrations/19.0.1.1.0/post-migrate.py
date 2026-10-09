"""19.0.1.1.0 post-migration.

* The static crons in ``data/cron.xml`` are ``noupdate="1"``, so an upgrade
  never refreshes their code and they would stay the old no-ops
  (``model.sync_products()`` / ``model.sync_orders()`` on an empty recordset).
  When a cron still carries that legacy code, copy the code currently shipped
  in ``data/cron.xml``. Crons that an administrator edited are left alone.
* Backfill ``magento_instance.company_id`` again, in case rows slipped
  through before the ORM made the column required.
"""
import logging

from lxml import etree

from odoo.tools.misc import file_open

_logger = logging.getLogger("odoo.addons.sdlc_magento_connector.migrations.19.0.1.1.0.post")

MODULE = "sdlc_magento_connector"
LEGACY_CRON_CODE = {
    "cron_magento_product_sync": "model.sync_products()",
    "cron_magento_order_sync": "model.sync_orders()",
}


def _shipped_cron_code():
    try:
        with file_open("%s/data/cron.xml" % MODULE, "rb") as handle:
            tree = etree.parse(handle)
    except (OSError, etree.XMLSyntaxError) as exc:
        _logger.warning("Could not read data/cron.xml: %s", exc)
        return {}
    codes = {}
    for record in tree.iterfind(".//record[@model='ir.cron']"):
        field = record.find("field[@name='code']")
        if field is not None and field.text and field.text.strip():
            codes[record.get("id")] = field.text.strip()
    return codes


def _refresh_legacy_crons(cr):
    shipped = _shipped_cron_code()
    for xml_name, legacy_code in LEGACY_CRON_CODE.items():
        cr.execute(
            """
            SELECT a.id, a.code
              FROM ir_model_data d
              JOIN ir_cron c ON c.id = d.res_id
              JOIN ir_act_server a ON a.id = c.ir_actions_server_id
             WHERE d.module = %s AND d.name = %s AND d.model = 'ir.cron'
            """,
            (MODULE, xml_name),
        )
        row = cr.fetchone()
        if not row:
            continue
        action_id, current = row
        new_code = shipped.get(xml_name)
        if (current or "").strip() != legacy_code or not new_code or new_code == legacy_code:
            continue
        cr.execute("UPDATE ir_act_server SET code = %s WHERE id = %s", (new_code, action_id))
        _logger.info("Cron %s: legacy code %r replaced by shipped code %r", xml_name, legacy_code, new_code)


def _backfill_company(cr):
    cr.execute("SELECT to_regclass('magento_instance') IS NOT NULL")
    if not cr.fetchone()[0]:
        return
    cr.execute(
        """
        UPDATE magento_instance
           SET company_id = (SELECT id FROM res_company ORDER BY id LIMIT 1)
         WHERE company_id IS NULL
        """
    )
    if cr.rowcount:
        _logger.info("magento_instance: company_id backfilled on %s instance(s)", cr.rowcount)


def migrate(cr, version):
    if not version:
        return
    _backfill_company(cr)
    _refresh_legacy_crons(cr)
