from patch_code_agent.graph import build_graph


def test_graph_builds_a_plan(tmp_path):
    source = tmp_path / "cart.py"
    source.write_text("def total(items):\n    return sum(items)\n")

    result = build_graph().invoke(
        {
            "issue": "Fix the cart total",
            "workspace_path": str(tmp_path),
            "status": "created",
        },
        config={"configurable": {"thread_id": "test-run"}},
    )

    assert result["status"] == "planned"
    assert result["inspected_files"] == ["cart.py"]
    assert len(result["plan"]) == 2
