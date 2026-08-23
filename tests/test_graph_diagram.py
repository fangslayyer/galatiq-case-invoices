"""The committed diagram must match the topology it claims to show.

`export_graph_image()` renders through mermaid.ink, so it is a manual step
(`--export-graph`) rather than part of every run: the case allows no external
API but Grok. This test is the offline half of that trade — it compares the
committed mermaid source against the graph as compiled today, so a topology
change that was never re-exported fails here instead of shipping a picture
that quietly lies about the pipeline.
"""

from invoiceflow.config import PROJECT_ROOT
from invoiceflow.graph import build_graph

GRAPH_MMD = PROJECT_ROOT / "docs" / "graph.mmd"


def test_committed_diagram_matches_compiled_topology(settings, db, fake_brain) -> None:
    compiled = build_graph(settings, db, fake_brain)
    assert GRAPH_MMD.read_text() == compiled.get_graph().draw_mermaid(), (
        "docs/graph.mmd no longer matches the compiled graph — "
        "re-export it with: uv run python main.py --export-graph"
    )
