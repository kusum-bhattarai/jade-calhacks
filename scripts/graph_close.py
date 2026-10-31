"""Run one month's close as the LangGraph agent (tpa.graph), answering its cards at the terminal.

    uv run python scripts/graph_close.py                 # the month the clock points at
    uv run python scripts/graph_close.py --recommended   # take every recommendation, sign what is clean

At a card, type an option number (Enter takes the recommendation), `s` to leave it in the inbox,
and optionally who answers and why. At sign-off, list the customers to sign (Enter signs every
clean one). The graph checkpoints in memory; every answer goes through `judgment.answer`.
"""

import argparse
import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.types import Command  # noqa: E402

from tpa.graph import build  # noqa: E402


def ask_card(c: dict, recommended: bool) -> dict:
    if recommended:
        return {"answer": c["recommended"], "reasoning": c["recommendation_basis"]}
    print(f"\n{c['escalation_id']} → {c['to_role']} ({c['customer_id']}, {c['control_id']})\n{c['question']}")
    for i, o in enumerate(c["options"], 1):
        print(f"  {i}. {o['label']}{'  ← recommended' if o['recommended'] else ''}")
    pick = input("option [Enter = recommended, s = skip]: ").strip()
    if pick == "s":
        return {"skip": True}
    option = c["options"][int(pick) - 1]["id"] if pick else c["recommended"]
    who = input("answered by [Enter = the card's addressee]: ").strip() or None
    why = input("reasoning [Enter = the option's label]: ").strip() or None
    return {"answer": option, "answered_by": who, "reasoning": why}


def ask_sign(s: dict, recommended: bool) -> dict:
    clean = [c["customer_id"] for c in s["customers"] if not c["unresolved"] and not c["failed_gates"]]
    if recommended:
        return {"customers": clean}
    print(f"\nSign-off, {s['period']}:")
    for c in s["customers"]:
        status = "clean" if c["customer_id"] in clean else \
            f"{c['unresolved']} unresolved, failing: {', '.join(c['failed_gates']) or 'none'}"
        print(f"  {c['customer_id']}: {c['state']}, {status}")
    picked = input(f"customers to sign [Enter = {', '.join(clean) or 'none'}]: ").replace(",", " ").split()
    who = input("signed by [Enter = close_reviewer]: ").strip() or "close_reviewer"
    return {"customers": picked or clean, "signed_by": who}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--recommended", action="store_true", help="take every recommendation and sign what is clean")
    args = ap.parse_args()

    graph = build(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": str(uuid.uuid4())}, "recursion_limit": 500}
    result = graph.invoke({}, config)
    while "__interrupt__" in result:
        request = result["__interrupt__"][0].value
        reply = ask_card(request, args.recommended) if request["kind"] == "card" else ask_sign(request, args.recommended)
        result = graph.invoke(Command(resume=reply), config)
    print("\n".join(result.get("log", [])))
    print(json.dumps({"period": result["period"], "states": result["states"], "signed": result.get("signed", []),
                      "open_cards": result["open_cards"]}, indent=2))


if __name__ == "__main__":
    main()
