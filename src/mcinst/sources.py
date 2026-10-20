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
