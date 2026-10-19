"""Connection to the hawkeye database (needs database.user set in config.yaml).

    from db import get_connection

    with get_connection().cursor() as cursor:
        cursor.execute("SELECT * FROM items WHERE id = %s", (item_id,))
        row = cursor.fetchone()   # dict, e.g. {"id": 1, "name": "..."}
"""
import os
from pathlib import Path

import boto3
import pymysql
import pymysql.cursors

CA_BUNDLE = str(Path(__file__).resolve().parent / "rds-ca-bundle.pem")

# Reused across invocations while the Lambda container stays warm
_connection = None


def get_connection():
    global _connection
    if _connection is not None:
        try:
            _connection.ping(reconnect=False)
            return _connection
        except pymysql.MySQLError:
            _connection = None

    host = os.environ["DB_HOST"]
    port = int(os.environ["DB_PORT"])
    user = os.environ["DB_USER"]

    # IAM auth token, valid for 15 minutes; signed locally, so no network call
    token = boto3.client("rds").generate_db_auth_token(DBHostname=host, Port=port, DBUsername=user)

    _connection = pymysql.connect(
        host=host,
        port=port,
        user=user,
        password=token,
        database=os.environ["DB_NAME"],
        ssl={"ca": CA_BUNDLE},
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
        connect_timeout=5,
    )
    return _connection
