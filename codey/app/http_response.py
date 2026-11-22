"""Validate the status and object payload returned by application routes."""

from codey.utils.object_fields import object_fields, sequence_items


def response_fields(value: object) -> dict[str, object]:
    if not object_fields(value):
        raise TypeError("response payload must be an object with string keys")
    return value


def response_pair(value: object) -> tuple[int, dict[str, object]]:
    if not sequence_items(value) or len(value) != 2:
        raise TypeError("response must contain a status and object payload")
    status = value[0]
    if not isinstance(status, int):
        raise TypeError("response status must be an integer")
    return status, response_fields(value[1])
