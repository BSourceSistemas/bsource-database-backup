#!/usr/bin/env python3
"""Send an explicitly requested relay acceptance email from the backup image."""

import json
import sys
from datetime import UTC, datetime
from uuid import uuid4

from app.config import load_config
from app.notifier import EmailNotifier, RetryingNotifier, notification_error_type

try:
    config = load_config()
    if config.notification_provider != "email":
        raise ValueError("Email provider required")
    run_id = str(uuid4())
    RetryingNotifier(EmailNotifier(config)).send(
        "relay-acceptance",
        {"runId": run_id, "occurredAt": datetime.now(UTC).isoformat(), "test": True},
    )
except Exception as error:
    print(json.dumps({"notificationStatus": "failed", "notificationErrorType": notification_error_type(error)}))
    sys.exit(1)
print(json.dumps({"notificationStatus": "sent", "runId": run_id}))
