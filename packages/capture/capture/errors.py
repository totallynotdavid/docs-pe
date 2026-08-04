class CaptureError(RuntimeError):
    pass


class RejectedError(CaptureError):
    """The site rejected an otherwise valid lookup."""
