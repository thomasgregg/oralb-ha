"""Regression coverage for release metadata formatting failures."""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location('release_notes', Path(__file__).parents[1] / 'tools/check_release_notes.py')
notes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(notes)


def test_consistent_release():
    assert notes.validate('v1.0.0', 'Session recovery', '## Changes\n\n### Details\n\n- Fixed a session.\n') == []


def test_reject_inconsistent_titles():
    for title in ['v1.0.0', 'Oral-B Live v1.0.0', 'v1.0.0 — Session recovery', '1.0.0 - Session recovery', 'session recovery', '']:
        assert notes.validate('v1.0.0', title, '## Changes\n')


def test_reject_literal_line_breaks():
    assert notes.validate('v1.0.0', 'Diagnostics', r'## Changes\n\n- Fix')


def test_reject_orphan_and_wrong_heading_levels():
    for body in ['### Validation\n', '# Changes\n', '#### Changes\n', 'No sections']:
        assert notes.validate('v1.0.0', 'Diagnostics', body)


def test_ignore_code_headings():
    assert not notes.validate('v1.0.0', 'Diagnostics', '## Changes\n\n```text\n# example\n```\n')


def test_nested_fence_markers_do_not_close_the_outer_block():
    for inner in ['```', '~~~', '````text']:
        body = f'## Changes\n\n````text\n{inner}\n# example\n````\n'
        assert not notes.validate('v1.0.0', 'Diagnostics', body)


def test_fence_closes_with_same_marker_and_sufficient_length():
    for opening, closing in [('```', '```'), ('```', '````'), ('~~~', '~~~~')]:
        body = f'## Changes\n\n{opening}\n# example\n{closing}\n# Wrong level\n'
        assert notes.validate('v1.0.0', 'Diagnostics', body)


def test_indented_headings_obey_the_same_rules():
    for indent in ['', ' ', '  ', '   ']:
        for wrong in ['# Wrong level', '#### Wrong level', '#']:
            body = f'## Changes\n{indent}{wrong}\n'
            assert notes.validate('v1.0.0', 'Diagnostics', body)
        assert not notes.validate('v1.0.0', 'Diagnostics', f'{indent}## Changes\n')
    # Four leading spaces make an indented code block, not a heading.
    assert not notes.validate('v1.0.0', 'Diagnostics', '## Changes\n\n    # example\n')


def test_indented_fences_preserve_code_headings():
    for indent in [' ', '  ', '   ']:
        body = f'## Changes\n\n{indent}```text\n# example\n{indent}```\n'
        assert not notes.validate('v1.0.0', 'Diagnostics', body)


def test_hacs_adds_version_once():
    tag = 'v0.7.39'
    title = 'Off-charger battery refresh and reliable session recovery'
    assert not notes.validate(tag, title, '## Changes\n')
    assert f'{tag} - {title}'.count(tag) == 1
    assert notes.validate(tag, f'{tag} — {title}', '## Changes\n')


def test_beta_title_must_omit_version():
    assert notes.validate('v0.7.39b2', 'v0.7.39b2 — Session recovery beta', '## Changes\n')
    assert not notes.validate('v0.7.39b2', 'Session recovery beta', '## Changes\n')
