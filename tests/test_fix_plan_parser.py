"""Tests for fix_plan_parser.

Focused on the section-header-as-task rejection that caused the
2026-04-20 ralph spin outage: 35 navigational section headers in
tian's @fix_plan.md ("After T0 (...):", "Tier 3 (...):", "What Works
Well:") were being emitted as tasks. Claude would correctly interpret
them as section markers, commit an empty "verified" doc, exit. Daemon
marked the task complete and autospawned on the next section header.
Cycle repeated until human intervention.
"""

from __future__ import annotations

import pytest

from chiefwiggum.fix_plan_parser import parse_fix_plan


@pytest.fixture
def fix_plan(tmp_path):
    def _write(body: str):
        p = tmp_path / "@fix_plan.md"
        p.write_text(body)
        return p
    return _write


class TestSectionHeaderRejection:
    def test_after_section_header_is_not_a_task(self, fix_plan):
        p = fix_plan("""# Project

## HIGH PRIORITY

#### After T0 (Stop the Bleeding):

Some prose describing what's done.

#### T0.1: Disable Jira Polling

- [ ] Turn off the poller.
""")
        tasks = parse_fix_plan(p)
        titles = [t.title for t in tasks]
        assert "After T0 (Stop the Bleeding):" not in [t.title + ":" for t in tasks]
        assert not any(t.title.rstrip().endswith(":") for t in tasks), (
            f"section headers leaked into tasks: {titles}"
        )
        # The real task still gets picked up.
        assert any(t.title.startswith("T0.1") for t in tasks), titles

    def test_tier_parenthetical_header_rejected(self, fix_plan):
        p = fix_plan("""## HIGH PRIORITY

#### Tier 3 (Confidence):

description

#### T3.1: Add Confidence Score

- [ ] yes
""")
        tasks = parse_fix_plan(p)
        assert not any("Tier 3 (Confidence)" in t.title for t in tasks)
        assert any(t.title.startswith("T3.1") for t in tasks)

    def test_what_works_well_header_rejected(self, fix_plan):
        p = fix_plan("""## HIGH PRIORITY

#### What Works Well:

Prose.

#### T1.1: Real Task

- [ ] do the thing
""")
        tasks = parse_fix_plan(p)
        assert not any(t.title == "What Works Well" for t in tasks)
        assert not any(t.title.endswith(":") for t in tasks)
        assert any("T1.1" in t.title for t in tasks)

    def test_after_task_n_header_rejected(self, fix_plan):
        p = fix_plan("""## HIGH PRIORITY

#### After Task 30:

Stuff.

#### T5.1: Continue Work

- [ ] ok
""")
        tasks = parse_fix_plan(p)
        assert not any("After Task 30" in t.title for t in tasks)

    def test_real_colon_titles_still_work(self, fix_plan):
        """Task titles WITH meaningful content after the colon must still be
        parsed. `TIER N: ...` style headers at `####` level are consumed by
        the section-pattern matcher (they set priority), not as tasks, so we
        test the dot-numbered (T0.1) and ID-based (PF-1) formats which are
        the ones that actually produce task rows with colons in their
        titles.
        """
        p = fix_plan("""## HIGH PRIORITY

#### PF-1: Timezone to Pacific

- [ ] change setting

#### T0.1: Disable Jira Recent Activity Polling

- [ ] off
""")
        tasks = parse_fix_plan(p)
        titles = [t.title for t in tasks]
        assert any("PF-1" in t and "Timezone" in t for t in titles), titles
        assert any("Disable Jira Recent Activity Polling" in t for t in titles), titles
        # Both have colons in their titles and must survive the rejection filter.
        assert all(not t.endswith(":") for t in titles), titles

    def test_mixed_real_and_header_file(self, fix_plan):
        """Simulates the actual tian @fix_plan.md structure: real tasks
        interleaved with section markers that end in colons."""
        p = fix_plan("""## HIGH PRIORITY

#### After T0 (Stop the Bleeding):
Some prose about T0 outcomes.

#### T0.1: Disable Jira Polling
- [ ] turn it off

#### After T1 (Source Attribution):
More prose.

#### T1.2: Preserve Slack Metadata
- [ ] preserve

#### What Works Well:
What works.
""")
        tasks = parse_fix_plan(p)
        titles = [t.title for t in tasks]

        # Section headers: gone.
        for header in [
            "After T0 (Stop the Bleeding):",
            "After T1 (Source Attribution):",
            "What Works Well:",
        ]:
            stripped_header = header.rstrip(":")
            assert not any(t.title == stripped_header for t in tasks), (
                f"section header survived: {stripped_header} in {titles}"
            )
            assert not any(t.title == header for t in tasks), (
                f"section header (with colon) survived: {header} in {titles}"
            )

        # Real tasks: present.
        assert any("T0.1" in t for t in titles), titles
        assert any("T1.2" in t for t in titles), titles

    def test_title_with_trailing_colon_from_any_format_rejected(self, fix_plan):
        """Same guard applies to numbered-format (### N. Title:) too."""
        p = fix_plan("""## HIGH PRIORITY

### 5. Real Work

- [ ] do it

### 6. Section Header:

- [ ] should not become a task
""")
        tasks = parse_fix_plan(p)
        titles = [t.title for t in tasks]
        # The "Section Header:" numbered entry should be rejected too.
        assert not any(t.title == "Section Header" for t in tasks), titles
        assert not any(t.title.rstrip().endswith(":") for t in tasks), titles
