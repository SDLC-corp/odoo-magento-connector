"""19.0.1.1.0 post-migration.

Backfill ``magento_instance.company_id`` again, in case rows slipped through
before the ORM made the column required. (The legacy no-op static crons are
removed by the ``<delete>`` in ``data/cron.xml``.)
"""
import logging

_logger = logging.getLogger("odoo.addons.sdlc_magento_connector.migrations.19.0.1.1.0.post")


def migrate(cr, version):
    if not version:
        return
    cr.execute(
        """SELECT 1 FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'magento_instance' AND column_name = 'company_id'"""
    )
    if not cr.fetchone():
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
