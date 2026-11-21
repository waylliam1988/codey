"""Derived prediction counts shared by full export and scoped management views."""

from codey.types.json_value import JsonObject


def prediction_calibration(predictions: tuple[JsonObject, ...]) -> JsonObject:
    supported = sum(row.get("status") == "supported" for row in predictions)
    contradicted = sum(row.get("status") == "contradicted" for row in predictions)
    judged = supported + contradicted
    mean = (supported + 1) / (judged + 2)
    variance = (supported + 1) * (contradicted + 1) / ((judged + 2) ** 2 * (judged + 3))
    return {"supported": supported, "contradicted": contradicted, "unjudged": len(predictions) - judged,
        "judged_samples": judged, "conditional_beta_mean": mean, "conditional_beta_variance": variance,
        "interpretation": "only matched, judged predictions; missing observations are not satisfaction"}
