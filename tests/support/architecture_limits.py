"""One measured size-debt record, with actionable growth and stale-entry checks."""

from collections.abc import Mapping


def size_limit_violations(
    sizes: Mapping[str, int], limits: Mapping[str, int], *, threshold: int = 1000,
) -> list[str]:
    problems = []
    for name, ceiling in sorted(limits.items()):
        if type(ceiling) is not int or ceiling <= threshold:
            problems.append(f"{name}: invalid exception ceiling {ceiling!r}")
        elif name not in sizes:
            problems.append(f"{name}: remove exception for missing file")
        elif sizes[name] <= threshold:
            problems.append(f"{name}: remove exception; now {sizes[name]} <= {threshold}")
        elif sizes[name] < ceiling:
            problems.append(f"{name}: lower ceiling from {ceiling} to {sizes[name]}")
        elif sizes[name] > ceiling:
            problems.append(f"{name}: grew from {ceiling} to {sizes[name]}")
    for name, size in sorted(sizes.items()):
        if size > threshold and name not in limits:
            problems.append(f"{name}: new oversized file ({size} > {threshold})")
    return problems
