"""Shared Pydantic models and closed vocabularies.

Import from the submodules directly. This package deliberately re-exports nothing, so importing
``incident_investigator.models`` never pulls in ``answer_key`` or ``scenario`` (Req 24.4, 24.8).
"""
