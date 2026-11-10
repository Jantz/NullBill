class NullBillError(Exception):
    """A failure the owner must fix. Carries one or more messages."""

    def __init__(self, *messages: str) -> None:
        super().__init__("\n".join(messages))
        self.messages = list(messages)


class Refused(NullBillError):
    """Something refused for several reasons at once. `headline` says what
    did not happen, the messages are the reasons; a reader with room lists
    them under it, and one line is `headline: reason; reason`."""

    def __init__(self, headline: str, reasons: list[str]) -> None:
        super().__init__(*reasons)
        self.headline = headline
