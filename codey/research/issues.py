"""Field-local research diagnostics, never a retry controller."""


class ResearchIssue(ValueError):
    def __init__(self, code: str, path: str, message: str, *, allowed_refs: tuple[str, ...] = (),
                 requires_acquisition: bool = False, allowed_actions: tuple[str, ...] = ()) -> None:
        self.code = code
        self.path = path
        self.message = message
        self.allowed_refs = allowed_refs
        self.requires_acquisition = requires_acquisition
        self.allowed_actions = allowed_actions
        super().__init__(str(self))

    def to_payload(self) -> dict[str, object]:
        return {"code": self.code, "path": self.path, "message": self.message,
                "allowed_refs": list(self.allowed_refs), "requires_acquisition": self.requires_acquisition,
                "allowed_actions": list(self.allowed_actions)}

    def __str__(self) -> str:
        refs = "; available citations: " + ", ".join(f"[{ref}]" for ref in self.allowed_refs) if self.allowed_refs else ""
        action = ("Allowed repairs: " + ", ".join(self.allowed_actions) + "." if self.allowed_actions else
                  "Acquire missing evidence or state a specific gap." if self.requires_acquisition else
                  "This field repair does not require a new search or write.")
        return f"{self.path}: {self.message}{refs}. {action}"
