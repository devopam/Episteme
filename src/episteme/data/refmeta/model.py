"""One release of a reference code list, ready to load."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CodeRow:
    code: str
    display: str | None
    status: str | None = None
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class CodeRelease:
    system_key: str
    release_id: str
    licence_notice: str
    source_file: str
    codes: tuple[CodeRow, ...]
    version: str | None = None
    oid: str | None = None

    def __post_init__(self) -> None:
        if not self.licence_notice.strip():
            raise ValueError(f"{self.system_key}: licence notice is empty")
        if not self.codes:
            raise ValueError(f"{self.system_key}: release {self.release_id} has no codes")
        seen: set[str] = set()
        for row in self.codes:
            if not row.code:
                raise ValueError(f"{self.system_key}: a row has an empty code")
            if row.code in seen:
                raise ValueError(f"{self.system_key}: duplicate code {row.code!r}")
            seen.add(row.code)
