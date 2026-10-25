"""The monthly close as a LangGraph agent, with a person at every checkpoint the engine leaves open.

    START → run_month → cards ⇄ (one card per pass) → sign → report → END
                          │ interrupt: a card for the person it is addressed to
                          │                         │ interrupt: the close reviewer signs, per customer

The graph orchestrates; it never decides an amount. `run_month` is `tpa.cycle.step` with judgment
plugged in, so every posting still goes through `tpa.execute` and `tpa.ledger.post_journal`. What the
engine leaves for a person (escalation cards, signatures) becomes a LangGraph `interrupt`; the
person's reply resumes the graph through the same checks the API uses: `judgment.answer` (role,
segregation of duties, reasoning on an override) and `gates.sign_period` (a failing gate blocks).

Resume values:
    card  {"answer": option id or label, "answered_by"?: actor id, "reasoning"?: str}
          or {"skip": true} to leave the card in the inbox (the period stays ESCALATED)
    sign  {"signed_by"?: actor id (default close_reviewer), "customers": [customer ids to sign]}

Each node opens its own connection and commits before it returns, as an API request does, so a
checkpointed graph can be resumed later, from another process, against the same database.
"""

import operator
from collections.abc import Callable
from datetime import datetime, time
from typing import Annotated, TypedDict

import psycopg
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from tpa import judgment, players
from tpa.asof import sim_clock
from tpa.constants import period_start
from tpa.cycle import UNRESOLVED, step
from tpa.db import connect
from tpa.gates import GateFailure, run_gates, sign_period
from tpa.rows import all_rows
from tpa.sod import SegregationOfDutiesError


class CloseState(TypedDict, total=False):
    period: str                                       # the month this close runs
    states: dict[str, str]                            # customer → period state after the run
    log: Annotated[list[str], operator.add]
    answered: Annotated[list[dict], operator.add]     # judgment.answer results
    skipped: Annotated[list[str], operator.add]       # escalation ids left in the inbox
    signed: Annotated[list[str], operator.add]        # customers signed in this close
    open_cards: list[str]


def now(cur: psycopg.Cursor) -> datetime:
    """When a person acts between steps: the morning of the period the clock points at (as `tpa.api`)."""
    period, _ = sim_clock(cur)
    return datetime.combine(period_start(period), time(7, 0))


def card(esc: dict) -> dict:
    return {"kind": "card", "escalation_id": esc["id"], "finding_id": esc["finding_id"], "to_role": esc["to_role"],
            "customer_id": esc["customer_id"], "control_id": esc["control_id"], "question": esc["question"],
            "options": [{"id": o["id"], "label": o["label"], "recommended": o.get("recommended", False)}
                        for o in esc["options"]],
            "recommended": esc["recommended"], "recommendation_basis": esc["recommendation_basis"]}


def build(db: Callable[[], psycopg.Connection] = connect, checkpointer=None):
    """Compile the close graph. `db` opens a connection; a checkpointer is needed to interrupt and resume."""

    def run_month(state: CloseState) -> dict:
        with db() as conn:
            with conn.cursor() as cur:
                current, _ = sim_clock(cur)
            conn.commit()
            if state.get("period") and state["period"] != current:
                raise ValueError(f"the clock is at {current}; step that period first")
            summary = step(conn, judgment.hooks())
        return {"period": summary["period"], "states": summary["states"], "log": summary["log"]}

    def waiting(cur: psycopg.Cursor, skipped: list[str]) -> list[dict]:
        return [e for e in judgment.open_escalations(cur) if e["id"] not in skipped]

    def cards(state: CloseState) -> dict:
        with db() as conn, conn.cursor() as cur:
            queue = waiting(cur, state.get("skipped", []))
            conn.commit()
            if not queue:
                return {}
            esc = queue[0]
            reply = interrupt(card(esc))
            if reply.get("skip"):
                return {"skipped": [esc["id"]], "log": [f"{esc['id']} left in the inbox for the {esc['to_role']}"]}
            option = next((o for o in esc["options"] if reply.get("answer") in (o["id"], o["label"])), None)
            if option is None:
                return {"log": [f"{esc['id']}: {reply.get('answer')!r} is not an option; asked again"]}
            who = reply.get("answered_by") or players.actor_for(esc["to_role"], esc["customer_id"])
            try:
                result = judgment.answer(cur, esc["id"], option["id"], who, now(cur),
                                         reasoning=reply.get("reasoning") or option["label"])
            except SegregationOfDutiesError as err:
                conn.commit()              # keep the record of the blocked attempt
                return {"log": [f"{esc['id']}: refused, {err}"]}
            except (PermissionError, ValueError) as err:
                conn.rollback()
                return {"log": [f"{esc['id']}: refused, {err}"]}
            conn.commit()
        return {"answered": [result], "log": [f"{esc['id']}: {result['decision']} by {who}"
                                               + (f", precedent {result['precedent_id']}" if result["precedent_id"] else "")]}

    def more_cards(state: CloseState) -> str:
        with db() as conn, conn.cursor() as cur:
            left = waiting(cur, state.get("skipped", []))
            conn.commit()
        return "cards" if left else "sign"

    def sign(state: CloseState) -> dict:
        period = state["period"]
        with db() as conn, conn.cursor() as cur:
            at = now(cur)
            unsigned = all_rows(cur, "SELECT customer_id, state FROM periods WHERE period = %s AND signed_at IS NULL "
                                     "ORDER BY customer_id", (period,))
            ready = []
            for row in unsigned:
                cur.execute("SELECT COUNT(*) FROM findings WHERE period = %s AND customer_id = %s AND state = ANY(%s)",
                            (period, row["customer_id"], list(UNRESOLVED)))
                open_findings = cur.fetchone()[0]
                failed = [n for n, r in run_gates(cur, period, row["customer_id"], at).items() if not r.passed]
                ready.append({"customer_id": row["customer_id"], "state": row["state"],
                              "unresolved": open_findings, "failed_gates": failed})
            conn.commit()
            if not ready:
                return {}
            reply = interrupt({"kind": "sign", "period": period, "customers": ready})
            signer, log, signed = reply.get("signed_by", "close_reviewer"), [], []
            for cid in reply.get("customers", []):
                entry = next((r for r in ready if r["customer_id"] == cid), None)
                if entry is None:
                    log.append(f"{period} {cid}: nothing to sign")
                elif entry["unresolved"]:
                    log.append(f"{period} {cid}: {entry['unresolved']} unresolved findings, not signed")
                else:
                    try:
                        sign_period(cur, period, cid, signer, at)
                        conn.commit()      # a later customer's failure must not undo this signature
                        signed.append(cid)
                        log.append(f"{period} {cid}: signed by {signer}")
                    except (GateFailure, PermissionError) as err:
                        conn.rollback()
                        log.append(str(err))
            cur.execute("SELECT bool_and(state = 'SIGNED') FROM periods WHERE period = %s", (period,))
            if cur.fetchone()[0]:
                cur.execute("UPDATE periods SET state = 'CLOSED' WHERE period = %s AND state = 'SIGNED'", (period,))
            conn.commit()
        return {"signed": signed, "log": log}

    def report(state: CloseState) -> dict:
        with db() as conn, conn.cursor() as cur:
            rows = all_rows(cur, "SELECT customer_id, state FROM periods WHERE period = %s ORDER BY customer_id",
                            (state["period"],))
            open_cards = [e["id"] for e in judgment.open_escalations(cur)]
            conn.commit()
        return {"states": {r["customer_id"]: r["state"] for r in rows}, "open_cards": open_cards}

    g = StateGraph(CloseState)
    g.add_node("run_month", run_month)
    g.add_node("cards", cards)
    g.add_node("sign", sign)
    g.add_node("report", report)
    g.add_edge(START, "run_month")
    g.add_conditional_edges("run_month", more_cards, ["cards", "sign"])
    g.add_conditional_edges("cards", more_cards, ["cards", "sign"])
    g.add_edge("sign", "report")
    g.add_edge("report", END)
    return g.compile(checkpointer=checkpointer)
