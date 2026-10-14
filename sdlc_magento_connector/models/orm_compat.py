"""Odoo 17's ``Model._read_group`` API on Odoo 16.

Odoo 17+ ``_read_group(domain, groupby, aggregates)`` returns one tuple per group:
the group values (records for relational fields, dates for ``field:granularity``,
raw values otherwise) followed by the aggregates. Odoo 16's ``read_group`` returns
dicts instead; :func:`read_group` converts them, so the connector's callers keep
their 17+ shape.
"""

from odoo import fields as odoo_fields


def read_group(model, domain, groupby=(), aggregates=(), offset=0, limit=None, order=None):
    groupby, aggregates = list(groupby), list(aggregates)
    specs = [agg for agg in aggregates if agg != "__count"]
    # Odoo 16 cannot order by "__count": sort those results here (limit/offset after).
    by_count = bool(order) and order.split()[0] == "__count"
    descending = by_count and order.lower().endswith("desc")
    rows = model.read_group(domain, specs, groupby, offset=0 if by_count else offset,
                            limit=None if by_count else limit,
                            orderby=False if by_count else (order or False), lazy=False)
    if by_count:
        rows = sorted(rows, key=lambda row: row["__count"], reverse=descending)
        rows = rows[offset:offset + limit] if limit else rows[offset:]
    result = []
    for row in rows:
        values = []
        for spec in groupby:
            fname, _, granularity = spec.partition(":")
            field = model._fields[fname]
            value = row.get(spec)
            if field.type in ("many2one", "many2many"):
                rec_id = value[0] if isinstance(value, (tuple, list)) and value else value
                value = model.env[field.comodel_name].browse(rec_id or ())
            elif field.type in ("date", "datetime") and granularity:
                bounds = (row.get("__range") or {}).get(spec) or {}
                start = bounds.get("from")
                value = (odoo_fields.Datetime.to_datetime(start) if field.type == "datetime"
                         else odoo_fields.Date.to_date(start)) if start else False
            values.append(value)
        for spec in aggregates:
            values.append(row["__count"] if spec == "__count" else row[spec.partition(":")[0]])
        result.append(tuple(values))
    return result
