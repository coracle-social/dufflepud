from raddoo.core import ensure_list, first, eager
from psycopg2.sql import SQL, Identifier, Literal, Composed


__all__ = ['sql', 'identifier', 'literal', 'build_select', 'build_update']


def is_safe(value):
    return isinstance(value, (Composed, SQL, Identifier, Literal))


def sql(x):
    return x if is_safe(x) else SQL(x)


def identifier(x):
    # Quote table and column if both are specified
    return x if is_safe(x) else SQL(".").join([
        Identifier(p) for p in x.split(".", 1)
    ])


def literal(x):
    return x if is_safe(x) else Literal(x)


def prep_select(select):
    if is_safe(select):
        return select

    return SQL(', ').join([identifier(s) for s in select])


@eager
def prep_where_clauses(where):
    if is_safe(where):
        yield where

    if type(where) == dict:
        for k, v in where.items():
            yield SQL(' = ').join([identifier(k), literal(v)])
    else:
        for op, cond in where:
            if op == 'not':
                yield SQL('{} ({})').format(
                    SQL(op.upper()),
                    SQL(' AND ').join(prep_where_clauses(cond))
                )
            elif op in {'and', 'or'}:
                yield SQL(f' {op.upper()} ').join(prep_where_clauses(cond))
            elif op in {'=', '!=', '!=', '>', '<', '>=', '<='}:
                yield SQL(f' {op} ').join([identifier(cond[0]), literal(cond[1])])
            else:
                raise ValueError(op)


def prep_where(where):
    if is_safe(where):
        return where

    return SQL("WHERE {}").format(SQL(" AND ").join(prep_where_clauses(where)))


def prep_group(group):
    if is_safe(group):
        return group

    return SQL(', ').join([identifier(g) for g in group])


def prep_order(order):
    if is_safe(order):
        return order

    order_bys = []
    for k, d in order:
        assert is_safe(d) or d in {'asc', 'desc'}, f"Invalid order by: {k} {d}"

        order_bys.append(SQL(' ').join([identifier(k), sql(d.upper())]))

    return SQL("ORDER BY {}").format(SQL(", ").join(order_bys))


def prep_set(data):
    return SQL("SET {}").format(SQL(", ").join([
        SQL(' = ').join([identifier(k), literal(v)])
        for k, v in data.items()
    ]))


def build_select(table, select=SQL("*"), **kw):
    sql = SQL("SELECT {} FROM {}").format(prep_select(select), identifier(table))

    if 'where' in kw:
        sql = SQL(' ').join([sql, prep_where(kw.pop('where'))])

    if 'group' in kw:
        sql = SQL(' ').join([sql, prep_group(kw.pop('group'))])

    if 'order' in kw:
        sql = SQL(' ').join([sql, prep_order(kw.pop('order'))])

    if 'limit' in kw:
        sql = SQL(' LIMIT ').join([sql, literal(kw.pop('limit'))])

    if 'offset' in kw:
        sql = SQL(' OFFSET ').join([sql, literal(kw.pop('offset'))])

    return sql


def build_insert(table, data):
    data = ensure_list(data)
    fields = list(first(data).keys())

    return SQL("INSERT INTO {} ({}) VALUES {}").format(
        identifier(table),
        SQL(", ").join([identifier(f) for f in fields]),
        SQL(", ").join([
            SQL("({})").format(
                SQL(", ").join([literal(row[f]) for f in fields])
            )
            for row in data
        ]),
    )


def build_update(table, data, where):
    return SQL("UPDATE {} {} {}").format(
        identifier(table),
        prep_set(data),
        prep_where(where),
    )
