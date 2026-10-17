# Release notes

Every historical release is archived here. Use `vX.Y.Z — Sentence case summary`
for the GitHub release title and the first `#` heading in its archive file.
Use `##` for body sections and `###` only for subsections within a section.
Prefer `## Changes`, followed by relevant validation, compatibility, or testing
sections. Preserve historical facts, caveats, credits, and links; do not invent
validation results or rewrite old behavior to describe the current integration.

Before publishing or editing a release:

1. Create or update `releases/<tag>.md` with real line breaks.
2. Run `python3 tools/check_release_notes.py`.
3. Copy the title from the first heading. Save the body (without that heading)
   to a UTF-8 file and pass it to `gh release create/edit --notes-file`.
   Do not embed multiline notes in shell arguments or use literal `\n` escapes.
4. Read the release back through the GitHub API and compare title and body.

CI validates the archive on every push and pull request. The release workflow
also validates live release titles and bodies on publication, edits, and manual
runs. Publication validation runs before uploading the integration ZIP.
