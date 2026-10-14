"""SQL declarations of the 19.0 code base, translated for Odoo 18.

Odoo 19 declares constraints and indexes with ``models.Constraint``,
``models.Index`` and ``models.UniqueIndex``. Odoo 18 expects
``_sql_constraints`` and indexes created in ``init``. The connector's models
import these module-local helpers instead, so the declarations keep their
19.0 shape without patching ``odoo.models`` for every other module.
"""

from psycopg2 import sql


class Constraint:
    """``models.Constraint``: appended to the class's ``_sql_constraints``."""

    def __init__(self, definition, message):
        self.definition = definition
        self.message = message

    def __set_name__(self, owner, name):
        constraints = list(owner.__dict__.get("_sql_constraints", ()))
        constraints.append((name.lstrip("_"), self.definition, self.message))
        owner._sql_constraints = constraints


class Index:
    """``models.Index``: created (if missing) by the model's ``init``."""

    unique = False

    def __init__(self, definition, message=None):
        self.definition = definition
        self.message = message

    def __set_name__(self, owner, name):
        indexes = list(owner.__dict__.get("_magento_compat_indexes", ()))
        indexes.append((name.lstrip("_"), self.definition, self.unique))
        owner._magento_compat_indexes = indexes
        if owner.__dict__.get("_magento_compat_init_installed"):
            return
        owner._magento_compat_init_installed = True
        original_init = owner.__dict__.get("init")

        def init(recordset):
            if original_init:
                original_init(recordset)
            else:
                super(owner, recordset).init()
            for key, definition, unique in owner._magento_compat_indexes:
                index_name = ("%s_%s" % (recordset._table, key))[:63]
                recordset.env.cr.execute(
                    sql.SQL("CREATE {}INDEX IF NOT EXISTS {} ON {} ").format(
                        sql.SQL("UNIQUE ") if unique else sql.SQL(""),
                        sql.Identifier(index_name),
                        sql.Identifier(recordset._table),
                    ) + sql.SQL(definition)
                )

        owner.init = init


class UniqueIndex(Index):
    """``models.UniqueIndex``: a unique (possibly partial) index."""

    unique = True
