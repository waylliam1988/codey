"""Field-local research diagnostics, never a retry controller."""


class ResearchIssue(ValueError):
    def __init__(self, code: str, path: str, message: str, *, allowed_refs: tuple[str, ...] = (),
                 requires_acquisition: bool = False) -> None:
        self.code = code
        self.path = path
        self.message = message
        self.allowed_refs = allowed_refs
        self.requires_acquisition = requires_acquisition
        super().__init__(str(self))

    def __str__(self) -> str:
        refs = "; available: " + ", ".join(self.allowed_refs) if self.allowed_refs else ""
        action = "Acquire missing evidence or state a specific gap." if self.requires_acquisition else "This field repair does not require a new search or write."
        return f"{self.path}: {self.message}{refs}. {action}"
