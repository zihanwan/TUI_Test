class McinstError(Exception):
    """An expected failure that can be presented by any frontend."""


class MetadataError(McinstError):
    """All eligible sources failed to provide usable metadata."""


class VersionNotFound(McinstError):
    """The selected version is absent from the current manifest."""


class PlanError(McinstError):
    """The input cannot be resolved into a complete supported installation plan."""
