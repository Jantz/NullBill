class NullBillError(Exception):
    """A failure the owner must fix. Carries one or more messages."""

    def __init__(self, *messages: str) -> None:
        super().__init__("\n".join(messages))
        self.messages = list(messages)
