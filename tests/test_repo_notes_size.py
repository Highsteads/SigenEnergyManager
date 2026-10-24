#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_repo_notes_size.py
# Description: The repo's CLAUDE.md is loaded whole into every session on this repo, so it
#              holds current facts only. On 24-10-2026 its older version lines and dated
#              sections moved to ~/.claude/skills/indigo-reference/repo-history/SigenEnergyManager.md;
#              this keeps them out. The file is gitignored, so the tests skip wherever it is
#              absent (CI). Same budget as ~/bin/notes-size-check.
# Author:      CliveS & Claude Opus 5.5
# Date:        24-10-2026
# Version:     1.0
import re
from pathlib import Path

import pytest

NOTES = Path(__file__).resolve().parents[1] / "CLAUDE.md"
LIMIT = 32 * 1024
VERSION_LINE = re.compile(r"^- (?:\*\*)?(?:Version|Previous|Prior|prior|Earlier|Superseded|Then|Current)\b(?! versions\*\*)")


@pytest.mark.skipif(not NOTES.is_file(), reason="no local CLAUDE.md here (it is gitignored)")
def test_repo_notes_stay_short():
    size = NOTES.stat().st_size
    assert size <= LIMIT, (
        f"CLAUDE.md is {size:,} bytes (budget {LIMIT:,}). Keep current facts only; move dated "
        f"history to the repo-history file in indigo-reference.")


@pytest.mark.skipif(not NOTES.is_file(), reason="no local CLAUDE.md here (it is gitignored)")
def test_repo_notes_carry_one_version_line():
    lines = [l for l in NOTES.read_text(encoding="utf-8").split("\n") if VERSION_LINE.match(l)]
    assert len(lines) <= 1, (
        f"CLAUDE.md carries {len(lines)} version lines; keep the current one and move the rest "
        f"to the repo-history file: {[l[:40] for l in lines]}")
