"""
Repo-root pytest configuration.

`pytest -q` (AGENTS.md rule 7) must only collect our own tests. The agent writes generated
code such as test_solution_ab12cd.py into workspace/artifacts/, and proof/soak runs copy them
into docs/; those import a `solution` module that exists only inside the sandbox, so
collecting them fails the whole run.
"""
collect_ignore_glob = ["workspace/*", "docs/*", "data/*", "logs/*", "offline_kit/*"]
