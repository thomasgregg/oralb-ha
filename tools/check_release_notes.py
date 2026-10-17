"""Validate archived notes and GitHub release event payloads (standard library only)."""
import argparse
import json
from pathlib import Path
import re


def validate(tag, title, body):
    errors = []
    if not re.fullmatch(re.escape(tag) + r" — [A-Z0-9].+", title):
        errors.append("title must be '<tag> — <Sentence case summary>'")
    if "\\n" in body or "\\r" in body:
        errors.append("body contains escaped line breaks; use a UTF-8 notes file")
    headings = []
    fence = None
    for line in body.splitlines():
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence is not None:
            if (
                marker
                and marker[1][0] == fence[0]
                and len(marker[1]) >= fence[1]
                and not marker[2].strip()
            ):
                fence = None
            continue
        if marker and (marker[1][0] != '`' or '`' not in marker[2]):
            fence = (marker[1][0], len(marker[1]))
            continue
        match = re.match(r"^ {0,3}(#{1,6})(?:[ \t]+|$)(.*)$", line)
        if match:
            depth = len(match[1])
            if depth not in (2, 3):
                errors.append("body headings must use ## sections or ### subsections")
            if depth == 3 and not headings:
                errors.append("subsection has no parent section")
            headings.append(depth)
    if not headings:
        errors.append("body needs at least one ## section")
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--event', type=Path)
    args = parser.parse_args()
    failures = []
    if args.event:
        release = json.loads(args.event.read_text())['release']
        entries = [(release['tag_name'], release['name'] or '', release['body'] or '')]
    else:
        entries = []
        for path in sorted(Path('releases').glob('v*.md')):
            title, _, body = path.read_text().partition('\n\n')
            entries.append((path.stem, title.removeprefix('# '), body))
        if not entries:
            failures.append('No archived release notes found')
    for tag, title, body in entries:
        failures.extend(f'{tag}: {error}' for error in validate(tag, title, body))
    if failures:
        raise SystemExit('\n'.join(failures))
    print(f'Validated {len(entries)} release notes')


if __name__ == '__main__':
    main()
