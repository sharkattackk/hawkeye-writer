import json
import os


def handler(event, context):
    """Lambda entry point. Replace with your function's logic."""
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"message": f"Hello from {os.environ.get('APP_NAME')}"}),
    }
