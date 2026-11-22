import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import import_markdown_docs as M


SAMPLE = """---
path: help-center/account/reset-password
title: Reset your password
type: user_guide
language: en
source: help-center
---

## Overview

Anyone can reset a forgotten password from the sign-in page. This paragraph is intentionally
long enough to clear the minimum-section threshold so it is kept by the splitter.

## Steps

Open the sign-in page, tap Forgot password, enter your email, then follow the link. This
section is also comfortably above the fifty-character minimum used by the splitter.
"""


def test_parse_frontmatter_splits_meta_and_body():
    meta, body = M.parse_frontmatter(SAMPLE)
    assert meta["path"] == "help-center/account/reset-password"
    assert meta["title"] == "Reset your password"
    assert meta["type"] == "user_guide"
    assert meta["language"] == "en"
    assert meta["source"] == "help-center"
    assert body.lstrip().startswith("## Overview")


def test_parse_frontmatter_requires_fence():
    with pytest.raises(ValueError):
        M.parse_frontmatter("no frontmatter here")


def test_validate_meta_accepts_valid():
    meta = {"path": "p", "title": "t", "type": "faq", "language": "ar", "source": "x"}
    M.validate_meta(meta, "file.md")  # no raise


@pytest.mark.parametrize("bad", [
    {"title": "t", "type": "faq"},
    {"path": "p", "type": "faq"},
    {"path": "p", "title": "t"},
    {"path": "p", "title": "t", "type": "nope"},
    {"path": "p", "title": "t", "type": "faq", "language": "de"},
])
def test_validate_meta_rejects_bad(bad):
    with pytest.raises(ValueError):
        M.validate_meta(bad, "file.md")


def test_split_markdown_sections_splits_on_headings():
    _, body = M.parse_frontmatter(SAMPLE)
    sections = M.split_markdown_sections(body, "Reset your password")
    headings = [s.heading for s in sections]
    assert "Overview" in headings
    assert "Steps" in headings
    assert all(len(s.content) >= M.MIN_SECTION_CHARS for s in sections)


def test_split_markdown_sections_falls_back_when_all_sections_tiny():
    body = "## A\nshort\n\n## B\ntiny\n\n## C\nalso brief here\n\n## D\nand more bits\n\n## E\nplus extra"
    sections = M.split_markdown_sections(body, "Fallback Title")
    assert len(sections) == 1
    assert sections[0].heading == "Fallback Title"


def test_split_markdown_sections_splits_oversized_section():
    para = ("word " * 80).strip()          # ~400 chars
    body = "## Big\n" + "\n\n".join([para] * 6)   # ~2400 chars under one heading
    sections = M.split_markdown_sections(body, "T")
    assert len(sections) >= 2
    assert all(s.heading == "Big" for s in sections)
