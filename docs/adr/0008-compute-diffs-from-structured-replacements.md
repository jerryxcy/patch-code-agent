# Compute diffs from structured file replacements

The model will propose bounded file replacements containing a path, expected content
hash, and new content. PatchCodeAgent validates those replacements and computes the exact
unified diff shown at the Approval Gate, avoiding model-generated diff parsing and stale
line-number edits while making workspace-change detection explicit.
