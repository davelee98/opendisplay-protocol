# Dev Notes

## 2026-07-06: manifest.json temporarily pinned to a py-opendisplay fork commit

`custom_components/opendisplay/manifest.json` currently points `py-opendisplay` at a
git commit on `davelee98/py-opendisplay` (fork of `OpenDisplay/py-opendisplay`)
instead of a released PyPI version, to test unreleased library changes against a
live HAOS instance:

```json
"py-opendisplay[silabs-ota] @ git+https://github.com/davelee98/py-opendisplay.git@742adad9644ba178cff5e78fa1665ef7724bd843"
```

**Before merging/shipping this branch**, revert this to a pinned PyPI release once
the corresponding `py-opendisplay` changes are actually published, e.g.:

```json
"py-opendisplay[silabs-ota]==<released-version>",
```
