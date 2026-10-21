from mcinst.downloads.models import AttemptFailure
from mcinst.errors import McinstError


class DownloadError(McinstError):
    """Invalid input or a permanent local filesystem failure."""


class DownloadBusy(DownloadError):
    """This downloader already has an active transfer for the target."""


class DownloadFailed(DownloadError):
    def __init__(self, path: str, failures: tuple[AttemptFailure, ...]):
        self.failures = failures
        reasons = "；".join(f"{f.source} #{f.attempt}: {f.reason}" for f in failures)
        super().__init__(f"下载失败：{path}（{reasons}）")
