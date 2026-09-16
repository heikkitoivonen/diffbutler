"""Print license metadata for installed packages, including development dependencies."""

from importlib.metadata import distributions


def main() -> None:
    print("Package\tVersion\tDeclared license (metadata; verify before redistribution)")
    for package in sorted(distributions(), key=lambda item: item.metadata["Name"].lower()):
        metadata = package.metadata
        declared = metadata.get("License-Expression") or metadata.get("License")
        if not declared:
            declared = "; ".join(
                entry.rsplit(" :: ", 1)[-1]
                for entry in metadata.get_all("Classifier", [])
                if entry.startswith("License ::")
            )
        summary = (declared or "UNKNOWN — inspect upstream license").splitlines()[0]
        print(f"{metadata['Name']}\t{package.version}\t{summary}")


if __name__ == "__main__":
    main()
