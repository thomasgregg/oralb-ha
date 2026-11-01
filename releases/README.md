# Release notes

Every historical release is archived here. Use a sentence-case summary (for example, `Off-charger battery refresh`)
for the GitHub release title and the first `#` heading in its archive file.
Do not include the version in the title: HACS adds the release tag before it,
so a version-prefixed title appears twice. The archive filename retains the tag.
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
also validates live release titles and bodies on publication and edits. Manual
runs validate the archived notes. Publication validation runs before uploading
the integration ZIP.

## HACS publication checks

Keep `custom_components/oralb_live/manifest.json` aligned with the release tag.
Run the complete pinned test suite and wait for unit tests, HACS and Hassfest
checks before publishing the tested commit. The release workflow attaches
`oralb_live.zip` with the integration files at the ZIP root, as configured by
`hacs.json`; standalone tools are downloaded from repository source instead.

After publication, wait for the Release workflow to finish, download the
published ZIP, and verify its manifest version and source against the released
commit. Check that the release is stable/latest when that is intended. Updating
README or protocol documentation alone does not require another HACS release.
