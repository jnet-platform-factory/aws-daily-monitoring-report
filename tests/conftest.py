"""Make `src` importable the way Lambda does, with no AWS needed.

`template.yaml` sets `CodeUri: app/` and `Handler: src.handler.lambda_handler`,
so `src` is a top-level package at runtime. Putting `app/` on the path
reproduces that exactly.

The handler builds its boto3 clients at import time. Creating a client needs a
region but no credentials, and no test lets one make a call: each replaces the
clients it touches with a stub.
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("RECIPIENT_EMAIL", "ops@example.com")
os.environ.setdefault("SENDER_EMAIL", "reports@example.com")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
