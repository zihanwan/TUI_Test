class McinstError(Exception):
    """An expected failure that can be presented by any frontend."""


class MetadataError(McinstError):
    """All eligible sources failed to provide usable metadata."""


class VersionNotFound(McinstError):
    """The selected version is absent from the current manifest."""
