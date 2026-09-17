"""Manifest identities are independent of filesystem paths and backend IDs."""

from dataclasses import dataclass
from typing import Literal, cast
from uuid import UUID


@dataclass(frozen=True)
class OwnedDirectory:
    root_id: UUID
    session_id: UUID
    resource_id: UUID
    purpose: Literal["artifact", "codex_home"] = "artifact"

    def manifest(self) -> dict[str, str]:
        return {
            "kind": "owned_directory_v1",
            "root_id": str(self.root_id),
            "session_id": str(self.session_id),
            "resource_id": str(self.resource_id),
            "purpose": self.purpose,
        }

    @classmethod
    def from_manifest(cls, value: dict[str, str]) -> OwnedDirectory:
        if (
            set(value)
            not in (
                {"kind", "root_id", "session_id", "resource_id"},
                {"kind", "root_id", "session_id", "resource_id", "purpose"},
            )
            or value.get("kind") != "owned_directory_v1"
        ):
            raise ValueError("Unsupported resource manifest")
        purpose = value.get("purpose", "artifact")
        if purpose not in {"artifact", "codex_home"}:
            raise ValueError("Unsupported resource purpose")
        try:
            return cls(
                UUID(value["root_id"]),
                UUID(value["session_id"]),
                UUID(value["resource_id"]),
                cast(Literal["artifact", "codex_home"], purpose),
            )
        except ValueError:
            raise ValueError("Invalid resource identity") from None

    @property
    def name(self) -> str:
        return f"{self.session_id.hex}-{self.resource_id.hex}"
