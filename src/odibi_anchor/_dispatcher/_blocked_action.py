"""Structured missing-prerequisite lifecycle error."""


class BlockedActionError(RuntimeError):
    """A retryable block caused solely by an unsatisfied prerequisite."""

    def __init__(self, message: str, *, required_action: str, resume_action: str,
                 argument_guidance: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.required_action = required_action
        self.resume_action = resume_action
        self.argument_guidance = argument_guidance
