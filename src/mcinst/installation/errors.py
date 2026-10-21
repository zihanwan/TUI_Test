from mcinst.errors import McinstError


class InstallationError(McinstError):
    """The instance cannot be safely assembled or its state cannot be read."""
