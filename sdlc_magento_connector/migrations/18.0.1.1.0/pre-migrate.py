"""19.0.1.1.0 pre-migration: make existing data fit the Phase 0 unique keys.

Runs before the ORM creates the new constraints:

* Empty-string keys become NULL (imports now store False, and the unique
  indexes are partial on non-empty keys).
* ``magento_product_map``, ``magento_category``, ``magento_customer`` and
  ``magento_attribute_set`` are made unique on (instance_id, magento_id), and
  ``magento_product_map`` also on (instance_id, sku). The lowest id of each
  duplicate group is kept, foreign keys pointing at the duplicates are
  repointed to it, and the duplicates are deleted.
* ``sale_order`` is made unique on (magento_instance_id, magento_order_id).
  Orders are business documents, so duplicates are never merged or deleted:
  the lowest id keeps the Magento key and the others are detached (key
  cleared, conflict flag and note set when those columns exist).
* ``magento_instance.company_id`` is backfilled with the first company, and
  the stored ``company_id`` of the binding tables is refreshed from it.

Every statement is guarded by table and column existence checks, so the
script is a no-op on a fresh database.
"""
import logging

from psycopg2 import errors as pg_errors
from psycopg2 import sql

_logger = logging.getLogger("odoo.addons.sdlc_magento_connector.migrations.19.0.1.1.0.pre")

BINDING_TABLES = (
    # (table, instance column, key column, trim the key) -- one entry per unique index.
    # The model indexes are partial (key IS NOT NULL AND key != ''), so empty keys are
    # first set to NULL and never deduplicated.
    ("magento_product_map", "instance_id", "magento_id", True),
    ("magento_product_map", "instance_id", "sku", False),
    ("magento_category", "instance_id", "magento_id", True),
    ("magento_customer", "instance_id", "magento_id", True),
    ("magento_attribute_set", "instance_id", "magento_id", True),
)
COMPANY_RELATED_TABLES = (
    "magento_product_map",
    "magento_category",
    "magento_customer",
    "magento_sync_report",
    "magento_attribute_set",
)


def _table_exists(cr, table):
    cr.execute("SELECT to_regclass(%s) IS NOT NULL", (table,))
    return cr.fetchone()[0]


def _column_exists(cr, table, column):
    cr.execute(
        """SELECT 1 FROM information_schema.columns
            WHERE table_schema = current_schema() AND table_name = %s AND column_name = %s""",
        (table, column),
    )
    return bool(cr.fetchone())


def _columns_exist(cr, table, *columns):
    return _table_exists(cr, table) and all(_column_exists(cr, table, c) for c in columns)


def _incoming_fks(cr, table):
    """Single-column foreign keys that reference ``table``.id: [(ref_table, ref_column)]."""
    cr.execute(
        """
        SELECT cl.relname, att.attname
          FROM pg_constraint con
          JOIN pg_class cl ON cl.oid = con.conrelid
          JOIN pg_attribute att ON att.attrelid = con.conrelid AND att.attnum = con.conkey[1]
         WHERE con.contype = 'f'
           AND con.confrelid = %s::regclass
           AND array_length(con.conkey, 1) = 1
        """,
        (table,),
    )
    return cr.fetchall()


def _table_columns(cr, table):
    cr.execute(
        """SELECT column_name FROM information_schema.columns
            WHERE table_schema = current_schema() AND table_name = %s""",
        (table,),
    )
    return [r[0] for r in cr.fetchall()]


def _normalize_empty_keys(cr, table, column, trim=True):
    cr.execute(
        sql.SQL("UPDATE {t} SET {c} = NULL WHERE {c} IS NOT NULL AND btrim({c}) = ''").format(
            t=sql.Identifier(table), c=sql.Identifier(column)),
    )
    if cr.rowcount:
        _logger.info("%s: %s empty %s value(s) set to NULL", table, cr.rowcount, column)
    if trim:
        cr.execute(
            sql.SQL("UPDATE {t} SET {c} = btrim({c}) WHERE {c} <> btrim({c})").format(
                t=sql.Identifier(table), c=sql.Identifier(column)),
        )
        if cr.rowcount:
            _logger.info("%s: %s %s value(s) trimmed", table, cr.rowcount, column)


def _build_dedup_map(cr, table, instance_col, key_col):
    cr.execute("DROP TABLE IF EXISTS _mg_dedup_map")
    cr.execute(
        sql.SQL("""
            CREATE TEMP TABLE _mg_dedup_map AS
            SELECT id AS dup_id, keep_id FROM (
                SELECT id, min(id) OVER (PARTITION BY {i}, {k}) AS keep_id
                  FROM {t}
                 WHERE {i} IS NOT NULL AND {k} IS NOT NULL AND {k} != ''
            ) s
            WHERE id <> keep_id
        """).format(t=sql.Identifier(table), i=sql.Identifier(instance_col), k=sql.Identifier(key_col)),
    )
    cr.execute("SELECT count(*) FROM _mg_dedup_map")
    return cr.fetchone()[0]


def _repoint(cr, table, ref_table, ref_col):
    """Repoint ref_table.ref_col from duplicate ids to the kept id."""
    params = dict(r=sql.Identifier(ref_table), c=sql.Identifier(ref_col))
    other_columns = [c for c in _table_columns(cr, ref_table) if c != ref_col]
    if len(other_columns) == 1 and other_columns[0] != "id":
        # Many2many relation table (two FK columns, composite key): a plain
        # UPDATE could collide, so copy links to the kept row (skipping those
        # that exist) and drop the old links.
        params["o"] = sql.Identifier(other_columns[0])
        _merge_relation_links(cr, table, ref_table, ref_col, params)
        return
    try:
        with cr.savepoint(flush=False):
            cr.execute(
                sql.SQL("UPDATE {r} SET {c} = m.keep_id FROM _mg_dedup_map m WHERE {r}.{c} = m.dup_id").format(**params)
            )
            if cr.rowcount:
                _logger.info("%s: repointed %s row(s) of %s.%s", table, cr.rowcount, ref_table, ref_col)
    except pg_errors.UniqueViolation:
        _logger.warning(
            "%s: could not repoint %s.%s (unique violation); rows referencing duplicates "
            "follow the foreign key's ON DELETE rule", table, ref_table, ref_col)


def _merge_relation_links(cr, table, ref_table, ref_col, params):
    cr.execute(
        sql.SQL("""
            INSERT INTO {r} ({c}, {o})
            SELECT DISTINCT m.keep_id, x.{o} FROM {r} x JOIN _mg_dedup_map m ON x.{c} = m.dup_id
            ON CONFLICT DO NOTHING
        """).format(**params)
    )
    moved = cr.rowcount
    cr.execute(sql.SQL("DELETE FROM {r} USING _mg_dedup_map m WHERE {r}.{c} = m.dup_id").format(**params))
    _logger.info("%s: merged %s link(s) of relation %s.%s (%s old link(s) removed)",
                 table, moved, ref_table, ref_col, cr.rowcount)


def _dedup_binding(cr, table, instance_col, key_col, trim=True):
    if not _columns_exist(cr, table, "id", instance_col, key_col):
        _logger.info("%s: table or columns (%s, %s) missing, skipped", table, instance_col, key_col)
        return
    _normalize_empty_keys(cr, table, key_col, trim=trim)
    count = _build_dedup_map(cr, table, instance_col, key_col)
    if not count:
        _logger.info("%s: no duplicate (%s, %s) found", table, instance_col, key_col)
        return
    cr.execute("SELECT keep_id, array_agg(dup_id ORDER BY dup_id) FROM _mg_dedup_map GROUP BY keep_id")
    _logger.warning("%s: %s duplicate row(s) on (%s, %s); kept -> removed: %s",
                    table, count, instance_col, key_col, dict(cr.fetchall()))

    # Keep the Odoo product link if only a duplicate had it.
    if table == "magento_product_map" and _column_exists(cr, table, "odoo_product_id"):
        cr.execute("""
            UPDATE magento_product_map k
               SET odoo_product_id = d.odoo_product_id
              FROM _mg_dedup_map m
              JOIN magento_product_map d ON d.id = m.dup_id
             WHERE k.id = m.keep_id AND k.odoo_product_id IS NULL AND d.odoo_product_id IS NOT NULL
        """)

    for ref_table, ref_col in _incoming_fks(cr, table):
        _repoint(cr, table, ref_table, ref_col)

    cr.execute(sql.SQL("DELETE FROM {t} USING _mg_dedup_map m WHERE {t}.id = m.dup_id").format(
        t=sql.Identifier(table)))
    _logger.warning("%s: deleted %s duplicate row(s)", table, cr.rowcount)
    cr.execute("DROP TABLE IF EXISTS _mg_dedup_map")


def _dedup_sale_orders(cr):
    if not _columns_exist(cr, "sale_order", "magento_instance_id", "magento_order_id"):
        _logger.info("sale_order: Magento columns missing, skipped")
        return
    _normalize_empty_keys(cr, "sale_order", "magento_order_id")
    count = _build_dedup_map(cr, "sale_order", "magento_instance_id", "magento_order_id")
    if not count:
        _logger.info("sale_order: no duplicate (magento_instance_id, magento_order_id) found")
        return
    cr.execute("""
        SELECT m.keep_id, so.magento_order_id, array_agg(m.dup_id ORDER BY m.dup_id)
          FROM _mg_dedup_map m JOIN sale_order so ON so.id = m.keep_id
         GROUP BY m.keep_id, so.magento_order_id
    """)
    groups = cr.fetchall()

    sets = [sql.SQL("magento_order_id = NULL")]
    if _column_exists(cr, "sale_order", "magento_sync_conflict"):
        sets.append(sql.SQL("magento_sync_conflict = TRUE"))
    if _column_exists(cr, "sale_order", "magento_conflict_note"):
        sets.append(sql.SQL(
            "magento_conflict_note = concat_ws(E'\\n', sale_order.magento_conflict_note, "
            "'Detached by the 19.0.1.1.0 migration: duplicate of sale order id ' || m.keep_id "
            "|| ' for Magento order ' || sale_order.magento_order_id || '.')"
        ))
    cr.execute(
        sql.SQL("UPDATE sale_order SET {} FROM _mg_dedup_map m WHERE sale_order.id = m.dup_id").format(
            sql.SQL(", ").join(sets))
    )
    _logger.warning(
        "sale_order: detached %s duplicate order(s) (Magento key cleared, kept lowest id). "
        "kept id, magento_order_id, detached ids: %s", cr.rowcount, groups)
    cr.execute("DROP TABLE IF EXISTS _mg_dedup_map")


def _backfill_company(cr):
    if not _table_exists(cr, "magento_instance") or not _table_exists(cr, "res_company"):
        return
    cr.execute("SELECT id FROM res_company ORDER BY id LIMIT 1")
    row = cr.fetchone()
    if not row:
        return
    company_id = row[0]
    if not _column_exists(cr, "magento_instance", "company_id"):
        cr.execute("ALTER TABLE magento_instance ADD COLUMN company_id integer")
        _logger.info("magento_instance: added column company_id")
    cr.execute("UPDATE magento_instance SET company_id = %s WHERE company_id IS NULL", (company_id,))
    if cr.rowcount:
        _logger.info("magento_instance: company_id set to %s on %s instance(s)", company_id, cr.rowcount)

    for table in COMPANY_RELATED_TABLES:
        if not _columns_exist(cr, table, "instance_id", "company_id"):
            continue
        cr.execute(
            sql.SQL("""
                UPDATE {t} SET company_id = i.company_id
                  FROM magento_instance i
                 WHERE {t}.instance_id = i.id AND {t}.company_id IS DISTINCT FROM i.company_id
            """).format(t=sql.Identifier(table))
        )
        if cr.rowcount:
            _logger.info("%s: company_id refreshed on %s row(s)", table, cr.rowcount)


def migrate(cr, version):
    if not version:
        return
    _logger.info("sdlc_magento_connector pre-migration to 19.0.1.1.0 from %s", version)
    _backfill_company(cr)
    for table, instance_col, key_col, trim in BINDING_TABLES:
        _dedup_binding(cr, table, instance_col, key_col, trim=trim)
    _dedup_sale_orders(cr)
