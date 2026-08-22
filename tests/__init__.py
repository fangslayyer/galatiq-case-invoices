"""Test package.

LangSmith tracing is forced off here — before anything imports langsmith or
loads .env — so a developer who has tracing switched on never ships 61
fake-brain runs to their real LangSmith project.
"""

import os

for _var in (
    "LANGSMITH_TRACING",
    "LANGSMITH_TRACING_V2",
    "LANGCHAIN_TRACING",
    "LANGCHAIN_TRACING_V2",
):
    os.environ[_var] = "false"
