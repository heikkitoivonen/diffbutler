"""Manifest identities are independent of filesystem paths and backend IDs."""

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class OwnedDirectory:
    root_id: UUID
    session_id: UUID
    resource_id: UUID

    def manifest(self) -> dict[str, str]:
        return {
            "kind": "owned_directory_v1",
            "root_id": str(self.root_id),
            "session_id": str(self.session_id),
            "resource_id": str(self.resource_id),
        }

    @classmethod
    def from_manifest(cls, value: dict[str, str]) -> OwnedDirectory:
        if (
            set(value) != {"kind", "root_id", "session_id", "resource_id"}
            or value.get("kind") != "owned_directory_v1"
        ):
            raise ValueError("Unsupported resource manifest")
        try:
            return cls(
                UUID(value["root_id"]), UUID(value["session_id"]), UUID(value["resource_id"])
            )
        except ValueError:
            raise ValueError("Invalid resource identity") from None

    @property
    def name(self) -> str:
        return f"{self.session_id.hex}-{self.resource_id.hex}"
