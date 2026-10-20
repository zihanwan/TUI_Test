from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit, urlunsplit

MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
BMCLAPI_BASE = "https://bmclapi2.bangbang93.com"


class SourcePolicy(StrEnum):
    AUTO = "auto"
    OFFICIAL = "official"
    BMCLAPI = "bmclapi"


@dataclass(frozen=True)
class SourceCandidate:
    name: str
    url: str


def metadata_candidates(url: str, policy: SourcePolicy) -> list[SourceCandidate]:
    """Route only known Mojang metadata hosts; never rewrite arbitrary URL text."""
    parts = urlsplit(url)
    official = SourceCandidate("official", url)
    known_hosts = {"piston-meta.mojang.com", "launchermeta.mojang.com", "launcher.mojang.com"}
    if (
        parts.scheme != "https"
        or parts.hostname not in known_hosts
        or parts.username is not None
        or parts.password is not None
        or parts.port not in (None, 443)
    ):
        raise ValueError("不支持的原版元数据地址：需要已知 Mojang 主机的 HTTPS 地址")
    mirror_parts = urlsplit(BMCLAPI_BASE)
    mirror = SourceCandidate(
        "bmclapi", urlunsplit(("https", mirror_parts.netloc, parts.path, parts.query, ""))
    )
    if policy == SourcePolicy.OFFICIAL:
        return [official]
    if policy == SourcePolicy.BMCLAPI:
        return [mirror]
    return [mirror, official]


def download_candidates(url: str, policy: SourcePolicy) -> list[SourceCandidate]:
    """Route vanilla artifacts by origin; preserve their paths and query strings."""
    parts = urlsplit(url)
    prefixes = {
        "piston-meta.mojang.com": "",
        "launchermeta.mojang.com": "",
        "launcher.mojang.com": "",
        "piston-data.mojang.com": "",
        "libraries.minecraft.net": "/maven",
        "resources.download.minecraft.net": "/assets",
    }
    if (
        parts.scheme not in ("http", "https")
        or parts.hostname not in prefixes
        or parts.username is not None
        or parts.password is not None
        or parts.port not in (None, 80 if parts.scheme == "http" else 443)
        or parts.fragment
    ):
        raise ValueError("不支持的原版文件来源地址")
    # Some historical descriptors use HTTP; these known origins support HTTPS.
    official_url = urlunsplit(("https", parts.hostname, parts.path, parts.query, ""))
    official = SourceCandidate("official", official_url)
    mirror_host = urlsplit(BMCLAPI_BASE).netloc
    mirror = SourceCandidate(
        "bmclapi",
        urlunsplit(("https", mirror_host, prefixes[parts.hostname] + parts.path, parts.query, "")),
    )
    if policy == SourcePolicy.OFFICIAL:
        return [official]
    if policy == SourcePolicy.BMCLAPI:
        return [mirror]
    return [mirror, official]
