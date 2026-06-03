"""A small local web app for the head-to-head comparison game.

Shows Player A vs Player B (same position) with their three stat categories;
the human picks one, which updates user ratings (Elo). Rankings and the
resulting auction values are viewable per position. Built on Flask and kept
self-contained (inline templates) so it's easy to run locally:

    python -m fantasy_football.cli serve

Flask is an optional dependency (``pip install -e ".[web]"``).
"""

from __future__ import annotations

from sqlalchemy import select

from .db import create_db_engine, get_sessionmaker, init_db
from .models import Team
from .ratings import (
    comparison_count,
    next_matchup,
    ranking,
    record_pick,
    seed_ratings,
    user_rating_tiers,
)
from .valuation import ALL_POSITIONS, DEFAULT_LEAGUE, LeagueConfig, compute_values

_PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FF Tiers - {{ title }}</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: system-ui, sans-serif; margin: 0; padding: 1rem;
         max-width: 760px; margin-inline: auto; }
  a { color: inherit; }
  h1 { font-size: 1.25rem; } h2 { font-size: 1rem; opacity: .8; }
  .nav { display:flex; gap:1rem; flex-wrap:wrap; margin-bottom:1rem; }
  .cards { display:grid; grid-template-columns:1fr 1fr; gap:1rem; }
  form.pick { margin:0; }
  button.card { width:100%; text-align:left; cursor:pointer; border:2px solid #8884;
        border-radius:14px; padding:1rem; background:transparent; font:inherit; }
  button.card:hover { border-color:#39f; }
  .name { font-size:1.2rem; font-weight:700; margin-bottom:.5rem; }
  .stat { display:flex; justify-content:space-between; padding:.15rem 0; }
  .stat b { font-variant-numeric: tabular-nums; }
  table { width:100%; border-collapse:collapse; font-variant-numeric:tabular-nums; }
  th,td { text-align:right; padding:.3rem .5rem; border-bottom:1px solid #8883; }
  th:nth-child(2),td:nth-child(2){ text-align:left; }
  .vs { text-align:center; opacity:.6; margin:.5rem 0; }
  .pos-grid { display:grid; grid-template-columns:repeat(3,1fr); gap:.6rem; }
  .pos-grid a { border:1px solid #8884; border-radius:12px; padding:.8rem; text-decoration:none; }
  .muted { opacity:.6; font-size:.85rem; }
</style></head>
<body>
<div class="nav"><a href="/">&#127944; Home</a>{{ extra_nav|safe }}</div>
{{ body|safe }}
</body></html>"""


def _render(title: str, body: str, extra_nav: str = "") -> str:
    from flask import render_template_string

    return render_template_string(_PAGE, title=title, body=body, extra_nav=extra_nav)


def _stat_block(row, coaching: dict) -> str:
    """One entity's card: identity, coaching, ranks, and the three categories."""
    if row is None:
        return "<div class='muted'>no value data</div>"
    hc, oc, pc = coaching.get(row.team, ("", "", ""))
    rookie = " &#127376;" if row.is_rookie else ""  # rookie badge
    return (
        f"<div class='name'>{row.name}{rookie}</div>"
        f"<div class='muted'>{row.position} &middot; {row.team or '?'} "
        f"&middot; Pos #{row.pos_rank} &middot; Ovr #{row.overall_rank}</div>"
        f"<div class='muted'>HC {hc or '?'} &middot; OC {oc or '?'} &middot; PC {pc or '?'}</div>"
        f"<div class='stat'><span>Last-yr total</span><b>{row.total:.1f}</b></div>"
        f"<div class='stat'><span>Last-yr PPG</span><b>{row.ppg:.1f}</b></div>"
        f"<div class='stat'><span>3-yr weighted</span><b>{row.w3yr:.1f}</b></div>"
    )


def create_app(db_path: str | None = None, config: LeagueConfig = DEFAULT_LEAGUE):
    """Build the Flask app. Ensures tables exist and seeds ratings if empty."""
    from flask import Flask, redirect, request

    engine = create_db_engine(db_path)
    init_db(engine)
    Session = get_sessionmaker(engine)

    # Seed ratings (idempotent) and cache static display stats (the three
    # categories don't change as you pick; only tiers/$ do).
    with Session() as s:
        seed_ratings(s, config=config)
    with Session() as s:
        values = compute_values(s, config=config)
        coaching = {
            t.abbreviation: (t.head_coach or "", t.offensive_coordinator or "", t.play_caller or "")
            for t in s.scalars(select(Team))
        }
    value_by_key = {r.key: r for rows in values.values() for r in rows}

    app = Flask(__name__)
    app.config["SESSION"] = Session
    app.config["LEAGUE"] = config
    app.config["VALUES"] = value_by_key
    app.config["COACHING"] = coaching

    @app.route("/")
    def home():
        with Session() as s:
            items = []
            for pos in ALL_POSITIONS:
                n = comparison_count(s, pos)
                items.append(
                    f"<a href='/play/{pos}'><b>{pos}</b><br>"
                    f"<span class='muted'>{n} picks &middot; ranking &raquo;</span></a>"
                )
        body = (
            "<h1>Tier-builder</h1>"
            "<p class='muted'>Pick the player you'd rather draft. Repeat to refine "
            "tiers. Your picks become user ratings &rarr; tiers &rarr; auction $.</p>"
            f"<div class='pos-grid'>{''.join(items)}</div>"
        )
        return _render("Home", body)

    @app.route("/play/<pos>")
    def play(pos: str):
        pos = pos.upper()
        with Session() as s:
            match = next_matchup(s, pos)
            n = comparison_count(s, pos)
        if match is None:
            return _render(pos, f"<h1>{pos}</h1><p>Not enough rated {pos} entities yet.</p>")
        a, b = match
        va = app.config["VALUES"].get(a.key)
        vb = app.config["VALUES"].get(b.key)

        coaching = app.config["COACHING"]

        def card(other_key: str, block: str) -> str:
            return (
                f"<form class='pick' method='post' action='/pick'>"
                f"<input type='hidden' name='pos' value='{pos}'>"
                f"<input type='hidden' name='a' value='{a.key}'>"
                f"<input type='hidden' name='b' value='{b.key}'>"
                f"<button class='card' name='winner' value='{other_key}'>{block}</button>"
                f"</form>"
            )

        body = (
            f"<h1>{pos} &mdash; who'd you rather?</h1>"
            f"<div class='cards'>{card(a.key, _stat_block(va, coaching))}"
            f"{card(b.key, _stat_block(vb, coaching))}</div>"
            f"<p class='vs'><a href='/play/{pos}'>&#8635; different pair</a> "
            f"&middot; <span class='muted'>{n} picks</span></p>"
        )
        nav = f" &middot; <a href='/ranking/{pos}'>{pos} ranking</a>"
        return _render(pos, body, extra_nav=nav)

    @app.route("/pick", methods=["POST"])
    def pick():
        pos = request.form["pos"]
        a, b = request.form["a"], request.form["b"]
        winner = request.form["winner"]
        loser = b if winner == a else a
        with Session() as s:
            record_pick(s, winner, loser)
        return redirect(f"/play/{pos}")

    @app.route("/ranking/<pos>")
    def show_ranking(pos: str):
        pos = pos.upper()
        with Session() as s:
            tiers = user_rating_tiers(s)
            priced = compute_values(s, config=app.config["LEAGUE"], manual_tiers=tiers)
            order = ranking(s, pos)
        dollars = {r.key: r for r in priced.get(pos, [])}
        rows = []
        for i, r in enumerate(order, 1):
            v = dollars.get(r.key)
            rows.append(
                f"<tr><td>{i}</td><td>{r.name}</td><td>{r.rating:.0f}</td>"
                f"<td>{tiers.get(r.key, '')}</td>"
                f"<td>{('$%.0f' % v.dollars) if v else ''}</td>"
                f"<td>{r.comparisons}</td></tr>"
            )
        body = (
            f"<h1>{pos} ranking</h1>"
            f"<p class='muted'>Ordered by your user rating; tiers from those "
            f"ratings; $ smoothed by tier.</p>"
            "<table><tr><th>#</th><th>Player</th><th>Rating</th><th>Tier</th>"
            "<th>$</th><th>Picks</th></tr>"
            f"{''.join(rows)}</table>"
        )
        nav = f" &middot; <a href='/play/{pos}'>play {pos}</a>"
        return _render(f"{pos} ranking", body, extra_nav=nav)

    return app
