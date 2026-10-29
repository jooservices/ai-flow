"""Atlassian Document Format (ADF) to and from plain text, and small text helpers."""
from __future__ import annotations

import re
import unicodedata


def adf_to_text(node) -> str:
    """Flatten ADF to readable text: paragraphs and list items on their own lines, bullets as '- '."""
    out: list[str] = []

    def walk(n, in_list: bool = False) -> None:
        if isinstance(n, list):
            for c in n:
                walk(c, in_list)
            return
        if not isinstance(n, dict):
            return
        t = n.get("type")
        if t == "text":
            out.append(n.get("text", ""))
            return
        if t == "hardBreak":
            out.append("\n")
            return
        if t == "listItem":
            out.append("- ")
        if t == "heading":
            out.append("\n")
        for c in n.get("content", []) or []:
            walk(c, in_list or t in ("bulletList", "orderedList"))
        if t in ("paragraph", "heading", "codeBlock", "tableRow", "rule") and not (in_list and t == "paragraph"):
            out.append("\n")
        if t == "listItem":
            out.append("\n")

    walk(node or {})
    text = "".join(out)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def text_to_adf(text: str) -> dict:
    """Plain text to ADF: blank lines split paragraphs, '- ' lines become bullet lists, ``` fences code."""
    content: list[dict] = []
    para: list[str] = []
    bullets: list[str] = []
    code: list[str] | None = None

    def flush_para() -> None:
        if para:
            content.append({"type": "paragraph", "content": [{"type": "text", "text": "\n".join(para)}]})
            para.clear()

    def flush_bullets() -> None:
        if bullets:
            content.append({"type": "bulletList", "content": [
                {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": b}]}]}
                for b in bullets]})
            bullets.clear()

    for line in (text or "").splitlines():
        s = line.rstrip()
        if s.strip().startswith("```"):
            if code is None:
                flush_para(); flush_bullets(); code = []
            else:
                content.append({"type": "codeBlock", "content": [{"type": "text", "text": "\n".join(code) or " "}]})
                code = None
            continue
        if code is not None:
            code.append(s)
        elif s.lstrip().startswith("- "):
            flush_para(); bullets.append(s.lstrip()[2:])
        elif not s.strip():
            flush_para(); flush_bullets()
        else:
            flush_bullets(); para.append(s)
    if code is not None:
        content.append({"type": "codeBlock", "content": [{"type": "text", "text": "\n".join(code) or " "}]})
    flush_para(); flush_bullets()
    return {"type": "doc", "version": 1, "content": content or [{"type": "paragraph", "content": []}]}


def normalize_adf_text(text: str) -> str:
    """Normalize plain text to the same paragraph boundaries Jira returns after an ADF round trip."""
    return adf_to_text(text_to_adf(str(text or "")))


def slug(text: str, max_len: int = 40) -> str:
    """ASCII kebab-case for branch names: 'Thêm trang Portfolio!' -> 'them-trang-portfolio'."""
    norm = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9]+", "-", norm).strip("-").lower()
    return s[:max_len].rstrip("-") or "change"
