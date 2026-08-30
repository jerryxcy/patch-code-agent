# Accumulate approved Repair Attempts

After a Verification failure, the approved changes remain in the Run Workspace and the
next Candidate Patch is an incremental change against that state. This gives diagnosis the
actual failing code and avoids rebuilding each attempt from the fixture; the Run Report
will also retain the final aggregate diff against the immutable Fixture Repository so the
cumulative result remains understandable.
