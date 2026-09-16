"""Attribution evidence for vendored and ported third-party work."""

import hashlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
NOTICES = REPO_ROOT / "THIRD_PARTY_NOTICES.md"
LICENSES = Path(__file__).parent / "fixtures" / "licenses"

# Exact copies of the upstream LICENSE files at the pinned commits.
UPSTREAM_LICENSES = {
    "yplib-4748c21.LICENSE": (
        "Copyright (c) 2026 Shaun Lastra",
        "4b04b6cbdd9336fbd2325836ee1ec50c9d3086c795eea099a89174f4471ec3e9",
    ),
    "openbluetoothprinter-c21f3b9.LICENSE": (
        "Copyright (c) 2026 Yuhang",
        "58cbb30371fc921208187657fc484a69a63dc78a9e6c6a249da2d034a209b256",
    ),
}

MIT_GRANT = (
    "Permission is hereby granted, free of charge, to any person obtaining a copy"
)
MIT_WARRANTY_DISCLAIMER = 'THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY'


@pytest.mark.parametrize(("filename", "expected"), UPSTREAM_LICENSES.items())
def test_notices_contain_full_mit_notice(
    filename: str, expected: tuple[str, str]
) -> None:
    """THIRD_PARTY_NOTICES.md reproduces each upstream MIT notice in full."""
    copyright_line, sha256 = expected
    upstream = (LICENSES / filename).read_bytes()

    assert hashlib.sha256(upstream).hexdigest() == sha256
    text = upstream.decode()
    assert text.startswith("MIT License\n")
    assert copyright_line in text
    assert MIT_GRANT in text
    assert MIT_WARRANTY_DISCLAIMER in text

    assert text in NOTICES.read_text()
