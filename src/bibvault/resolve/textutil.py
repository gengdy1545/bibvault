"""Title normalization shared by metadata and publisher matching."""


def normalize_title(value: str | None) -> str:
    return " ".join("".join(char.lower() if char.isalnum() else " " for char in value or "").split())
