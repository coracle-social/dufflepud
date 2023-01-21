import json
from decimal import Decimal
from contextlib import contextmanager
from raddoo import first, env
from psycopg2 import connect as _connect, extras, extensions
from psycopg2.errors import InterfaceError
from psycopg2.extras import RealDictCursor, RealDictRow
from psycopg2.sql import SQL, Identifier, Literal, Composed # NOQA


def unpack(value, depth=0):
    if value is None:
        return value

    t = type(value)

    if t in {int, float}:
        return value

    if t == Decimal:
        return float(value)

    if t == list:
        return [unpack(x, depth + 1) for x in value]

    if t in {dict, RealDictRow}:
        return {k: unpack(v, depth + 1) for k, v in value.items()}

    return value


class JsonAdapter(extras.Json):
    def dumps(self, obj):
        return json.dumps(obj)


extensions.register_adapter(dict, JsonAdapter)
extensions.register_adapter(list, JsonAdapter)

connection = None
cursor = None


# Connect/disconnect


def connect():
    global connection

    connection = _connect(env('DATABASE_URL'), cursor_factory=RealDictCursor)


def disconnect():
    global connection

    try:
        connection.close()
    except InterfaceError:
        pass

    connection = None


def reconnect():
    disconnect()
    connect()


connect()


# Transactions and query execution


@contextmanager
def transaction():
    global cursor

    if cursor:
        raise Exception("Already in transaction")

    with connection.cursor() as cur:
        cursor = cur

        try:
            yield
        except Exception:
            connection.rollback()

            raise
        else:
            connection.commit()
        finally:
            cursor = None


def mogrify(query):
    return cursor.mogrify(query).decode('utf-8')


def execute(query, args=None):
    if not cursor:
        raise Exception("Not in a transaction")

    try:
        cursor.execute(query, args)
    except Exception as exc:
        exc.args = (exc.args[0] + f'for query:\n\n {mogrify(query)}',)

        raise

    return cursor


def all(query, args=None):
    return unpack(execute(query, args).fetchall())


def one(query, args=None):
    return unpack(execute(query, args).fetchone())


def val(query, args=None):
    result = unpack(execute(query, args).fetchone())

    return first(result.values()) if result else None


def col(query, args=None):
    return [
        first(x.values()) for x in
        unpack(execute(query, args).fetchall())
    ]


def iter(query, args=None):
    cursor.execute(query, args)

    chunk = cursor.fetchmany(500)
    while chunk:
        yield from unpack(chunk)

        chunk = cursor.fetchmany(500)
