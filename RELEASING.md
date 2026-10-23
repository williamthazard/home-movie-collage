# Releasing

Publishing to PyPI is automatic: publishing a GitHub release runs
[`.github/workflows/publish.yml`](.github/workflows/publish.yml), which builds the package and
uploads it through PyPI trusted publishing (no passwords or tokens).

1. Bump `version` in `pyproject.toml` (PyPI never accepts the same version twice), commit and push.
2. Publish a release whose tag is `v` plus that version:

   ```
   gh release create v0.1.2 --generate-notes
   ```

   or on GitHub: Releases → Draft a new release → new tag `v0.1.2` → Publish.
3. Watch it under the repository's Actions tab (`gh run watch`). The build stops before
   uploading anything if the tag does not match the version in `pyproject.toml`.

## One-time PyPI setup (already done once it has published)

On [pypi.org](https://pypi.org), with two-factor authentication on, go to
*Your account → Publishing → Add a new pending publisher → GitHub* and enter:

| Field | Value |
|---|---|
| PyPI Project Name | `home-movie-collage` |
| Owner | `williamthazard` |
| Repository name | `home-movie-collage` |
| Workflow name | `publish.yml` |
| Environment name | `pypi` |
