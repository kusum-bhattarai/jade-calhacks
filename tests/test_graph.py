"""The close as a LangGraph agent: October runs, the person each card is for answers it, the close reviewer signs."""

import uuid

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from tpa import players
from tpa.cycle import run_through
from tpa.graph import build
from tpa.rows import all_rows, one_row
from tpa.seed.generate import build as seed
from tpa.seed.load import load


@pytest.fixture
def september(conn):
    """January to September played by the stand-in humans; the clock points at October."""
    load(conn, seed(1337, 24), load_terms=False)
    run_through(conn, "2026-09", players.hooks())
    return conn


def start(period: str | None = "2026-10"):
    graph = build(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": str(uuid.uuid4())}, "recursion_limit": 200}
    return graph, config, graph.invoke({"period": period} if period else {}, config)


def request(result) -> dict:
    return result["__interrupt__"][0].value


def sign_all(result) -> dict:
    return {"customers": [c["customer_id"] for c in request(result)["customers"]]}


def test_the_close_runs_to_signature(september):
    graph, config, result = start()
    answered = []
    while request(result)["kind"] == "card":
        card = request(result)
        assert any(o["id"] == card["recommended"] and o["recommended"] for o in card["options"])
        answered.append(card["escalation_id"])
        result = graph.invoke(Command(resume={"answer": card["recommended"]}), config)
    assert answered, "October leaves at least one card for a person"
    result = graph.invoke(Command(resume=sign_all(result)), config)

    assert "__interrupt__" not in result
    assert result["open_cards"] == []
    assert set(result["states"].values()) == {"CLOSED"}
    with september.cursor() as cur:
        for eid in answered:
            esc = one_row(cur, "SELECT * FROM escalations WHERE id = %s", (eid,))
            assert esc["answered_by"] == players.actor_for(esc["to_role"],
                                                           one_row(cur, "SELECT customer_id FROM findings WHERE id = %s",
                                                                   (esc["finding_id"],))["customer_id"])
            assert esc["became_precedent_id"], "an answer becomes a precedent"
        signed = all_rows(cur, "SELECT signed_by FROM periods WHERE period = '2026-10'")
    assert {r["signed_by"] for r in signed} == {"close_reviewer"}


def test_the_wrong_person_is_refused_and_asked_again(september):
    graph, config, result = start()
    card = request(result)
    result = graph.invoke(Command(resume={"answer": card["recommended"], "answered_by": "sales.walmart"}), config)

    assert request(result)["escalation_id"] == card["escalation_id"]
    assert any("refused" in line and card["escalation_id"] in line for line in result["log"])
    with september.cursor() as cur:
        assert one_row(cur, "SELECT answered_at FROM escalations WHERE id = %s",
                       (card["escalation_id"],))["answered_at"] is None


def test_a_card_left_in_the_inbox_holds_its_customer(september):
    graph, config, result = start()
    card = request(result)
    result = graph.invoke(Command(resume={"skip": True}), config)
    while request(result)["kind"] == "card":
        result = graph.invoke(Command(resume={"answer": request(result)["recommended"]}), config)
    held = next(c for c in request(result)["customers"] if c["customer_id"] == card["customer_id"])
    assert held["unresolved"] > 0
    result = graph.invoke(Command(resume=sign_all(result)), config)

    assert card["escalation_id"] in result["open_cards"]
    assert card["customer_id"] not in result["signed"]
    assert result["states"][card["customer_id"]] != "CLOSED"


def test_the_clock_decides_which_month_runs(september):
    with pytest.raises(ValueError, match="the clock is at 2026-10"):
        start("2026-12")
