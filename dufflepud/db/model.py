from functools import lru_cache
from raddoo.core import pick_values
from dufflepud.db import db, sql


@lru_cache()
def describe(table):
    columns = db.all(sql.build_select(
        'information_schema.columns',
        select=['column_name', 'column_default', 'is_nullable', 'data_type'],
        where={'table_schema': 'public', 'table_name': table}))

    return {'columns': columns}


def one(table, where):
    return db.one(sql.build_select(table, where=where))


def get_by_id(table, id):
    return db.one(sql.build_select(table, where={'id': id}))


def insert(table, data):
    db.execute(sql.build_insert(table, data))


def update(table, data, where):
    db.execute(sql.build_update(table, data, where))


def request(query, cursor=None):
    type, where, order, limit = pick_values(['type', 'where', 'order', 'limit'], query)
    order, direction = query['order']

    if cursor:
        where += ['>' if order[1] == 'asc' else '<', [order[0], cursor]]

    return db.all(sql.build_select(type, where, [order], limit))
