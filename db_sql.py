"""Explicit SQL execution adapter for the supported database drivers.

Only positional qmark placeholders outside SQL literals/comments are translated
for psycopg. This is deliberately opt-in; it is not a general SQL dialect
translator.
"""
import sqlite3

from db_compat import database_dialect


def qmark_to_format(sql):
    """Translate SQL qmark bind markers to psycopg %s markers.

    Quoted strings/identifiers, line comments, nested block comments, and
    PostgreSQL dollar-quoted strings are preserved. This helper is intended
    only for statements whose unquoted question marks are bind markers.
    PostgreSQL's JSON '?' operator must not be used in statements passed here.
    """
    if not isinstance(sql, str):
        raise TypeError("SQL statement must be a string")

    out = []
    i = 0
    length = len(sql)
    state = "normal"
    block_depth = 0
    dollar_tag = None

    while i < length:
        char = sql[i]
        nxt = sql[i + 1] if i + 1 < length else ""

        if state == "normal":
            if char == "'":
                state = "single"
                out.append(char)
                i += 1
                continue
            if char == '"':
                state = "double"
                out.append(char)
                i += 1
                continue
            if char == "-" and nxt == "-":
                state = "line_comment"
                out.extend((char, nxt))
                i += 2
                continue
            if char == "/" and nxt == "*":
                state = "block_comment"
                block_depth = 1
                out.extend((char, nxt))
                i += 2
                continue
            if char == "$":
                end = sql.find("$", i + 1)
                if end != -1:
                    tag_body = sql[i + 1:end]
                    if tag_body == "" or (
                        tag_body[0].isalpha() or tag_body[0] == "_"
                    ) and all(c.isalnum() or c == "_" for c in tag_body):
                        dollar_tag = sql[i:end + 1]
                        state = "dollar"
                        out.append(dollar_tag)
                        i = end + 1
                        continue
            if char == "?":
                out.append("%s")
            else:
                out.append(char)
            i += 1
            continue

        if state == "single":
            out.append(char)
            if char == "'" and nxt == "'":
                out.append(nxt)
                i += 2
                continue
            if char == "\\" and nxt:
                out.append(nxt)
                i += 2
                continue
            if char == "'":
                state = "normal"
            i += 1
            continue

        if state == "double":
            out.append(char)
            if char == '"' and nxt == '"':
                out.append(nxt)
                i += 2
                continue
            if char == '"':
                state = "normal"
            i += 1
            continue

        if state == "line_comment":
            out.append(char)
            if char == "\n":
                state = "normal"
            i += 1
            continue

        if state == "block_comment":
            if char == "/" and nxt == "*":
                block_depth += 1
                out.extend((char, nxt))
                i += 2
                continue
            if char == "*" and nxt == "/":
                block_depth -= 1
                out.extend((char, nxt))
                i += 2
                if block_depth == 0:
                    state = "normal"
                continue
            out.append(char)
            i += 1
            continue

        if state == "dollar":
            if sql.startswith(dollar_tag, i):
                out.append(dollar_tag)
                i += len(dollar_tag)
                dollar_tag = None
                state = "normal"
                continue
            out.append(char)
            i += 1
            continue

    return "".join(out)


def execute_compat(connection, sql, params=()):
    """Execute a qmark-parameterized statement on SQLite or psycopg 3.

    The caller owns transaction boundaries and must supply positional values.
    """
    dialect = database_dialect(connection)
    if dialect == "sqlite":
        return connection.execute(sql, params)
    return connection.execute(qmark_to_format(sql), params)



def executemany_compat(connection, sql, seq_of_params):
    """Execute a qmark-parameterized batch on SQLite or psycopg 3.

    This helper does not commit; the caller owns the transaction.
    """
    dialect = database_dialect(connection)
    if dialect == "sqlite":
        return connection.executemany(sql, seq_of_params)
    cursor = connection.cursor()
    cursor.executemany(qmark_to_format(sql), seq_of_params)
    return cursor
