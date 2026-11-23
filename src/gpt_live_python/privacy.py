import json
import re
from typing import Any

PRIVATE = re.compile(
    r"^(api.?key|authorization|access_token|client_secret|password|headers|.*_key|"
    r"phoneNumber|customerName|email|addressRemark|countryCode|contactId|output)$",
    re.I,
)


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key == "arguments" and isinstance(item, str):
                try:
                    result[key] = json.dumps(redact(json.loads(item)), ensure_ascii=False)
                except json.JSONDecodeError:
                    result[key] = "[arguments omitted]"
            elif PRIVATE.match(key):
                result[key] = "[redacted]"
            else:
                result[key] = redact(item)
        return result
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def reject_secrets(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if re.match(r"^(api.?key|authorization|access_token|client_secret|password|headers)$", key, re.I):
                raise ValueError("Credentials and authentication headers are not allowed in events/configuration")
            reject_secrets(item)
    elif isinstance(value, list):
        for item in value:
            reject_secrets(item)
