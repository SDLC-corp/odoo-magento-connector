"""SQL helpers of the 19.0 code base, translated for Odoo 16.

* ``Constraint``, ``Index`` and ``UniqueIndex`` stand in for Odoo 19's
  ``models.Constraint``/``models.Index``/``models.UniqueIndex``: they build
  ``_sql_constraints`` and create indexes in ``init``.
* ``SQL`` and ``execute`` stand in for Odoo 17's ``odoo.tools.SQL`` and for
  cursors that accept it.

The connector's models import these module-local helpers, so the code keeps its
19.0 shape without patching ``odoo`` for every other module.
"""

import re

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


# ---------------------------------------------------------------------------
# odoo.tools.SQL (Odoo 17+)
# ---------------------------------------------------------------------------
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_NAMED = re.compile(r"%\((\w+)\)s")
_ESCAPED = "\x01"  # placeholder for "%%" while named parameters are rewritten


class SQL:
    """Composable SQL with parameters: the subset of Odoo 17's ``odoo.tools.SQL`` the
    connector uses. ``SQL(code, *args)`` or ``SQL(code, **kwargs)``: an argument that
    is itself ``SQL`` is inlined, any other value becomes a ``%s`` parameter. Also
    ``SQL.identifier()`` and ``sql.join()``. Run it with :func:`execute`."""

    __slots__ = ("_code", "_params")

    def __init__(self, code="", *args, **kwargs):
        if isinstance(code, SQL):
            self._code, self._params = code._code, code._params
            return
        if args and kwargs:
            raise TypeError("SQL() takes positional or named arguments, not both")
        if kwargs:
            names = []

            def positional(match):
                names.append(match.group(1))
                return "%s"

            code = _NAMED.sub(positional, code.replace("%%", _ESCAPED)).replace(_ESCAPED, "%%")
            args = tuple(kwargs[name] for name in names)
        if not args:
            # Like Odoo 17, the code is still run through parameter formatting later,
            # so a literal "%" is written "%%" even without arguments.
            self._code, self._params = code, ()
            return
        parts, params = [], []
        for arg in args:
            if isinstance(arg, SQL):
                parts.append(arg._code)  # code is always in "formatted" form, as in Odoo 17
                params.extend(arg._params)
            else:
                parts.append("%s")
                params.append(arg)
        self._code = code % tuple(parts)
        self._params = tuple(params)

    @property
    def code(self):
        return self._code

    @property
    def params(self):
        return list(self._params)

    def __repr__(self):
        return "SQL(%r, %r)" % (self._code, self._params)

    def __bool__(self):
        return bool(self._code)

    def __eq__(self, other):
        return isinstance(other, SQL) and (self._code, self._params) == (other._code, other._params)

    def __hash__(self):
        return hash((self._code, self._params))

    def join(self, args):
        args = [arg if isinstance(arg, SQL) else SQL("%s", arg) for arg in args]
        if not args:
            return SQL()
        return SQL(self._code.join(["%s"] * len(args)), *args)

    @classmethod
    def identifier(cls, name, subname=None):
        for part in (name, subname):
            if part is not None and not _IDENT.match(part):
                raise ValueError("Invalid SQL identifier %r" % part)
        if subname is None:
            return cls('"%s"' % name)
        return cls('"%s"."%s"' % (name, subname))


def execute(cr, query, params=None, **kwargs):
    """``cr.execute`` that also accepts :class:`SQL` (Odoo 16 cursors take a string)."""
    if isinstance(query, SQL):
        return cr.execute(query._code, query._params, **kwargs)  # always formatted, as in Odoo 17
    return cr.execute(query, params, **kwargs)
