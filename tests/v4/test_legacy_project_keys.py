from __future__ import annotations

import re
from pathlib import Path


def test_product_code_defaults_and_docs_do_not_name_legacy_projects():
    root = Path(__file__).resolve().parents[2]
    pattern = re.compile(r"(?<![A-Z0-9])(?:TESTFLOW|TESTWP|JOOWT|JOOS)(?![A-Z0-9])")
    assert pattern.search("TESTWP-1") and not pattern.search("JOOSX-1")
    text_suffixes = {".Dockerfile", ".cfg", ".conf", ".css", ".env", ".html", ".ini", ".j2", ".js",
                     ".json", ".jsonl",
                     ".md", ".py", ".rules", ".service", ".sh", ".sql", ".timer", ".toml", ".txt", ".xml",
                     ".yaml", ".yml"}
    findings = []
    for relative in ("aiflow/v4", "deploy/v4", "docs", "prompts/v4"):
        for path in sorted((root / relative).rglob("*")):
            if path.is_file() and (not path.suffix or path.suffix in text_suffixes or path.name.endswith(".example")):
                content = path.read_text(encoding="utf-8")
                for number, line in enumerate(content.splitlines(), 1):
                    if pattern.search(line):
                        findings.append(f"{path.relative_to(root)}:{number}: {line.strip()}")
    assert findings == [], "legacy project keys found outside tests:\n" + "\n".join(findings)
