# Constrain the MVP trust boundary

The MVP will operate only on trusted, bundled Fixture Repositories selected from a registry
by fixture identifier and will require approval of the exact candidate diff before applying
it. Repository path containment limits agent file access, but is not presented as a sandbox
for hostile test code; arbitrary filesystem paths, local repositories, and untrusted
repositories are explicitly outside the MVP boundary.
