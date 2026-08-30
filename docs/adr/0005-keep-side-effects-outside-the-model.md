# Keep side effects outside the model boundary

The model may inspect a Run Workspace only through constrained list, read, and search
operations and may propose a structured Candidate Patch. PatchCodeAgent itself applies an
approved patch and executes the Fixture Repository's declared Verification argv; the model
receives neither arbitrary shell access nor direct write access.
