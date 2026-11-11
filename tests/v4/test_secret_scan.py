"""Characterize secret screening before and after its move into v4 core."""

from __future__ import annotations

import pytest

from aiflow.memory import guard
from aiflow.v4.core import secret_scan


SAFE_TEXTS = ("", "ordinary question", "first safe text", "second safe text")
SECRET_SAMPLES = (
    ("private key", "-----BEGIN RSA " + "PRIVATE KEY-----"),
    ("AWS access key", "AKIA" + "Q" * 16),
    ("GitHub token", "ghp_" + "a" * 30),
    ("GitHub token", "github_pat_" + "a" * 20),
    ("API key", "sk-" + "b" * 30),
    ("Slack token", "xoxb-" + "c" * 20),
    ("JWT", "eyJhbGciOiJIUzI1NiJ9" + "." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0" + "." + "abcdefghijklmnop"),
    ("bearer token", "Bearer" + " " + "d" * 30),
    ("credentials in a URL", "https://user" + ":" + "password@service.invalid/path"),
    ("password or token assignment", "pass" + "word=hunter2hunter2"),
)
MESSAGE = "looks like it contains a secret ({name}); remove it and propose again"


def test_legacy_scanner_accepts_empty_ordinary_and_multiple_safe_texts():
    assert guard.scan_secrets(*SAFE_TEXTS) is None
    assert secret_scan.scan_secrets(*SAFE_TEXTS) is None


def test_legacy_and_core_paths_reexport_the_same_objects():
    assert guard.Rejected is secret_scan.Rejected
    assert guard.SECRETS is secret_scan.SECRETS
    assert guard.scan_secrets is secret_scan.scan_secrets


@pytest.mark.parametrize(("name", "sample"), SECRET_SAMPLES)
def test_legacy_scanner_rejects_each_existing_pattern_without_echoing_sample(name, sample):
    with pytest.raises(guard.Rejected) as caught:
        guard.scan_secrets("prefix " + sample + " suffix")

    assert str(caught.value) == MESSAGE.format(name=name)
    assert sample not in str(caught.value)


def test_legacy_scanner_uses_pattern_order_for_multiple_matches():
    first = "-----BEGIN RSA " + "PRIVATE KEY-----"
    second = "AKIA" + "Q" * 16

    with pytest.raises(guard.Rejected) as caught:
        guard.scan_secrets(first + " " + second)

    assert str(caught.value) == MESSAGE.format(name="private key")
    assert first not in str(caught.value) and second not in str(caught.value)

    with pytest.raises(secret_scan.Rejected) as core_caught:
        secret_scan.scan_secrets(first + " " + second)

    assert str(core_caught.value) == str(caught.value)


def test_core_scanner_matches_legacy_scanner_for_every_pattern():
    for name, sample in SECRET_SAMPLES:
        with pytest.raises(secret_scan.Rejected) as core_caught:
            secret_scan.scan_secrets("prefix " + sample + " suffix")
        with pytest.raises(guard.Rejected) as legacy_caught:
            guard.scan_secrets("prefix " + sample + " suffix")

        assert type(core_caught.value) is type(legacy_caught.value) is guard.Rejected
        assert str(core_caught.value) == str(legacy_caught.value) == MESSAGE.format(name=name)
        assert sample not in str(core_caught.value)


@pytest.mark.parametrize("field", ("tags", "source", "repo"))
def test_legacy_entry_rejects_secret_metadata_without_echoing_value(field):
    secret = "ghp_" + "m" * 30
    metadata = {
        "tags": {"tags": [secret]},
        "source": {"source": {"note": secret}},
        "repo": {"repo": secret},
    }[field]
    entry = {
        "kind": "lesson",
        "scope": "project",
        "namespace": "JOOservices",
        "project": "JAVM",
        "title": "Safe title",
        "body": "Safe body",
        **metadata,
    }

    with pytest.raises(guard.Rejected) as caught:
        guard.check_entry(entry)

    assert secret not in str(caught.value)
