import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError

from mcinst.errors import PlanError
from mcinst.installation.models import InstanceSpec


class OSRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    arch: str | None = None
    version: str | None = None


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["allow", "disallow"]
    os: OSRule | None = None
    features: dict[str, StrictBool] = Field(default_factory=dict)


def matches(pattern: str, value: str) -> bool:
    try:
        return re.fullmatch(pattern, value) is not None
    except re.error as exc:
        raise PlanError(f"无法解析平台规则正则表达式：{pattern!r}") from exc


def rules_allow(raw: object, spec: InstanceSpec) -> bool:
    if raw is None:
        return True
    if not isinstance(raw, list):
        raise PlanError("libraries.rules 必须是数组")
    # With rules present, default deny; the last matching action wins.
    allowed = False
    for item in raw:
        try:
            rule = Rule.model_validate(item)
        except ValidationError as exc:
            raise PlanError(f"不支持的平台规则：{exc}") from exc
        os = rule.os
        if os:
            if os.name is not None and os.name != spec.target.os:
                continue
            if os.arch is not None and not matches(os.arch, spec.target.arch):
                continue
            if os.version is not None:
                if spec.target.os_version is None:
                    raise PlanError("规则需要目标系统版本，请指定 --os-version")
                if not matches(os.version, spec.target.os_version):
                    continue
        if any(spec.features.get(name, False) != value for name, value in rule.features.items()):
            continue
        allowed = rule.action == "allow"
    return allowed
