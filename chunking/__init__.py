"""Offline chunk-preparation mappers for the AgentForge knowledge base.

Each subpackage turns one kind of source into JSON chunks the indexer can load:
`openapi`, `sql` (tbls JSON), `db` (live database), `code`, `cli_docs`, `document`.
See README.md for the workflow.
"""
