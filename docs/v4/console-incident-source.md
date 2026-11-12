# Console incident preview source
**Console screens:** `cc`, `sa`

The console manifest keeps shared incident handoff and preview helpers in `console/074-incident-preview.js`.
Command Center rendering starts in `console/075-command-center.js`; Role health and the incident drawer also use the
shared helpers. These files are concatenated in manifest order inside the existing console script, so their functions
remain visible to every screen in the same IIFE.

To add another shared incident helper, put it in the incident preview fragment and keep that fragment before every
manifest entry that calls it. Add a new numbered fragment to `console/manifest.txt` in dependency order. The builder
joins source text without separators, so preserve the trailing newline and keep the existing script boundaries intact.

Deploy the manifest and all listed fragments together. A rollback must restore the matching complete prior set; do not
mix a manifest from one release with fragments from another.
