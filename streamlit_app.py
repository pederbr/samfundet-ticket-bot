#!/usr/bin/env python3
"""
Streamlit UI for the Samfundet ticket bot.

Run with:
  streamlit run streamlit_app.py
"""

from __future__ import annotations

import json
import queue
import threading
import webbrowser
from datetime import datetime

import pandas as pd
import streamlit as st

import bot as botlib

st.set_page_config(page_title="Samfundet Ticket Bot", page_icon="🎟️", layout="centered")

DEFAULT_ROW = {
    "event": "",
    "email": "",
    "membercard": "",
    "price_type": "member",
    "count": 1,
    "wait_until": "",
    "poll_interval": 0.25,
    "max_attempts": None,
    "open_browser": True,
}

if "log_queue" not in st.session_state:
    st.session_state.log_queue = queue.Queue()
if "log_lines" not in st.session_state:
    st.session_state.log_lines = []
if "running" not in st.session_state:
    st.session_state.running = False
if "threads" not in st.session_state:
    st.session_state.threads = []
if "results" not in st.session_state:
    st.session_state.results = {}
if "auto_opened" not in st.session_state:
    # Tracks which results' checkout links we've already fired the client-side
    # auto-open script for, so it doesn't re-fire (and pop a new tab) on every
    # later rerun that re-renders the same result.
    st.session_state.auto_opened = set()
if "rows" not in st.session_state:
    st.session_state.rows = pd.DataFrame([DEFAULT_ROW])
if "stop_event" not in st.session_state:
    # A per-session Event, never bot.py's module-level default — that one is
    # a single object shared by every session in this process, so using it
    # would let one browser tab's Stop button halt every other tab's bots too.
    st.session_state.stop_event = threading.Event()


def make_log_fn(log_queue: queue.Queue) -> botlib.LogFn:
    """Build a log function bound to one run's own queue via closure.

    Deliberately not routed through any module-level/global state: this app
    can serve multiple concurrent browser sessions from one process, and
    bot.py's functions only see whatever log_fn/stop_event we pass in — so
    each run's log output and Stop button stay isolated to its own session.
    """

    def log_fn(msg: str, prefix: str = "") -> None:
        prefix_part = f"[{prefix}] " if prefix else ""
        for line in msg.splitlines():
            log_queue.put(f"{prefix_part}{line}")

    return log_fn


def build_config(row: dict) -> botlib.BotConfig:
    event = str(row.get("event") or "").strip()
    if not event:
        raise ValueError("Event is required.")

    email = (str(row.get("email") or "").strip()) or None
    membercard = (str(row.get("membercard") or "").strip()) or None
    if not email and not membercard:
        raise ValueError(f"{event}: provide an email or a member card.")

    price_type = row.get("price_type") or "member"
    if price_type not in ("member", "non-member"):
        raise ValueError(f"{event}: invalid price type {price_type!r}.")

    count = int(row.get("count") or 1)
    if count < 1 or count > 9:
        raise ValueError(f"{event}: count must be between 1 and 9.")

    wait_until_val = None
    wait_until_str = str(row.get("wait_until") or "").strip()
    if wait_until_str:
        try:
            wait_until_val = botlib.parse_wait_until(wait_until_str)
        except Exception as exc:
            raise ValueError(f"{event}: {exc}")

    poll_interval = float(row.get("poll_interval") or 0.25)

    max_attempts_raw = row.get("max_attempts")
    max_attempts = None
    if max_attempts_raw not in (None, "", 0) and not pd.isna(max_attempts_raw):
        max_attempts = int(max_attempts_raw)

    return botlib.BotConfig(
        event=event,
        email=email,
        membercard=membercard,
        price_type=price_type,
        count=count,
        wait_until=wait_until_val,
        poll_interval=poll_interval,
        max_attempts=max_attempts,
        no_browser=not bool(row.get("open_browser", True)),
    )


def run_bot(
    key: str,
    config: botlib.BotConfig,
    results: dict,
    log_fn: botlib.LogFn,
    stop_event: threading.Event,
) -> None:
    prefix = config.event
    url = botlib.buy_url(config.event)
    opener = botlib.build_opener()
    log_fn(f"Event buy page: {url}", prefix)

    try:
        form = botlib.poll_buy_page(
            opener,
            url,
            wait_until_time=config.wait_until,
            poll_interval=config.poll_interval,
            max_attempts=config.max_attempts,
            prefix=prefix,
            log_fn=log_fn,
            stop_event=stop_event,
        )
    except InterruptedError:
        log_fn("Interrupted.", prefix)
        results[key] = {"event": config.event, "status": "stopped"}
        return
    except Exception as exc:
        log_fn(f"Error while polling: {exc}", prefix)
        results[key] = {"event": config.event, "status": "error", "message": str(exc)}
        return

    log_fn("Price groups:", prefix)
    for pg in form.price_groups:
        log_fn(f"  {pg.label}: {pg.price} kr ({pg.field_name})", prefix)

    selected = botlib.pick_price_group(form.price_groups, config.price_type)
    total = selected.price * config.count
    log_fn(f"Selected: {selected.label} × {config.count} ({total} kr)", prefix)

    try:
        checkout_url = botlib.submit_purchase(
            opener,
            form,
            referer=url,
            count=config.count,
            price_group=selected,
            email=config.email,
            membercard=config.membercard,
        )
    except Exception as exc:
        log_fn(f"Purchase submission failed: {exc}", prefix)
        results[key] = {"event": config.event, "status": "error", "message": str(exc)}
        return

    if config.no_browser:
        log_fn("Stripe checkout ready.", prefix)
    else:
        try:
            opened = webbrowser.open(checkout_url)
        except Exception as exc:
            log_fn(f"Stripe checkout ready, but couldn't auto-open browser: {exc}", prefix)
        else:
            if opened:
                log_fn("Stripe checkout ready — opened in your browser.", prefix)
            else:
                # webbrowser.open() returns False on failure instead of raising,
                # so this branch is the only way such a failure becomes visible.
                log_fn(
                    "Stripe checkout ready, but the browser-open command reported "
                    "failure (no exception). Use the link below instead.",
                    prefix,
                )

    results[key] = {
        "event": config.event,
        "status": "success",
        "checkout_url": checkout_url,
        "selected": selected.label,
        "total": total,
    }


def start_run(configs: dict[str, botlib.BotConfig]) -> None:
    st.session_state.log_lines = []

    results: dict = {}
    st.session_state.results = results

    log_queue: queue.Queue = queue.Queue()
    st.session_state.log_queue = log_queue
    log_fn = make_log_fn(log_queue)

    stop_event = threading.Event()
    st.session_state.stop_event = stop_event

    threads = []
    for key, config in configs.items():
        t = threading.Thread(
            target=run_bot, args=(key, config, results, log_fn, stop_event), daemon=True
        )
        threads.append(t)
    st.session_state.threads = threads
    st.session_state.running = True
    for t in threads:
        t.start()


st.title("🎟️ Samfundet Ticket Bot")
st.caption("Reserves a ticket via Billig and opens the Stripe checkout for you automatically.")

parallel_mode = st.toggle(
    "Run multiple bots in parallel",
    value=False,
    disabled=st.session_state.running,
    help="Turn this on to target several events/accounts at once, each with its own settings. "
    "Leave it off for the simple single-bot form.",
)

configs_to_start: dict[str, botlib.BotConfig] | None = None
form_errors: list[str] = []

if not st.session_state.running:
    if not parallel_mode:
        # Kept outside the form: widgets inside st.form() don't trigger a rerun
        # until submit, so a choice made in there can't reveal another widget live.
        wait_enabled = st.checkbox("Wait until a specific time before polling")
        wait_time = None
        if wait_enabled:
            wait_time = st.time_input(
                "Wait until (Norwegian time)", value=datetime.now(botlib.TZ).time()
            )

        ticket_type = st.radio("Ticket type", ["Email", "Member card"], horizontal=True)

        with st.form("simple_bot_form"):
            event = st.text_input(
                "Event", placeholder="5423-toga", help="Event slug or URL, e.g. 5423-toga"
            )

            if ticket_type == "Email":
                email = st.text_input("Email", placeholder="you@example.com")
                membercard = ""
            else:
                membercard = st.text_input("Member card number")
                email = ""

            col_price, col_count = st.columns(2)
            price_type = col_price.selectbox("Price type", ["member", "non-member"])
            count = col_count.number_input("Count", min_value=1, max_value=9, value=1, step=1)

            with st.expander("Advanced settings"):
                poll_interval = st.number_input(
                    "Poll interval (seconds)", min_value=0.05, max_value=10.0, value=0.25, step=0.05
                )
                max_attempts = st.number_input(
                    "Max attempts (0 = unlimited)", min_value=0, value=0, step=1
                )
                open_browser = st.checkbox(
                    "Automatically open Stripe checkout when ready", value=True
                )

            submitted = st.form_submit_button("🚀 Start", type="primary", use_container_width=True)

        if submitted:
            row = {
                "event": event,
                "email": email,
                "membercard": membercard,
                "price_type": price_type,
                "count": count,
                "wait_until": wait_time.strftime("%H:%M") if wait_time else "",
                "poll_interval": poll_interval,
                "max_attempts": max_attempts or None,
                "open_browser": open_browser,
            }
            try:
                cfg = build_config(row)
                configs_to_start = {f"0:{cfg.event}": cfg}
            except ValueError as exc:
                form_errors.append(str(exc))

    else:
        st.caption("Add one row per event/account — bots run in parallel.")
        edited = st.data_editor(
            st.session_state.rows,
            num_rows="dynamic",
            use_container_width=True,
            key="bot_table",
            column_config={
                "event": st.column_config.TextColumn("Event", help="Slug or URL, e.g. 5423-toga", required=True),
                "email": st.column_config.TextColumn("Email", help="For digital tickets"),
                "membercard": st.column_config.TextColumn("Member card", help="Samfundet member card number"),
                "price_type": st.column_config.SelectboxColumn(
                    "Price type", options=["member", "non-member"], default="member"
                ),
                "count": st.column_config.NumberColumn("Count", min_value=1, max_value=9, step=1, default=1),
                "wait_until": st.column_config.TextColumn(
                    "Wait until", help="Local time HH:MM or HH:MM:SS, blank = now"
                ),
                "poll_interval": st.column_config.NumberColumn(
                    "Poll interval (s)", min_value=0.05, max_value=10.0, step=0.05, default=0.25
                ),
                "max_attempts": st.column_config.NumberColumn(
                    "Max attempts", min_value=1, step=1, help="Blank = unlimited"
                ),
                "open_browser": st.column_config.CheckboxColumn(
                    "Auto-open checkout", default=True
                ),
            },
        )
        st.session_state.rows = edited

        if st.button("🚀 Start all", type="primary", use_container_width=True):
            configs: dict[str, botlib.BotConfig] = {}
            for idx, row in enumerate(edited.to_dict("records")):
                try:
                    cfg = build_config(row)
                    configs[f"{idx}:{cfg.event}"] = cfg
                except ValueError as exc:
                    form_errors.append(str(exc))
            if not configs and not form_errors:
                form_errors.append("Add at least one bot row.")
            if not form_errors:
                configs_to_start = configs

else:
    st.info("Bot(s) running — see the live log below.")
    if st.button("⏹ Stop", use_container_width=True):
        st.session_state.stop_event.set()
        st.session_state.running = False
        st.rerun()

for e in form_errors:
    st.error(e)

if configs_to_start:
    start_run(configs_to_start)
    st.rerun()

st.subheader("Live log")


@st.fragment(run_every=1 if st.session_state.running else None)
def live_log() -> None:
    while True:
        try:
            st.session_state.log_lines.append(st.session_state.log_queue.get_nowait())
        except queue.Empty:
            break
    text = "\n".join(st.session_state.log_lines[-500:]) or "…"
    st.code(text, language=None)
    if st.session_state.running and not any(t.is_alive() for t in st.session_state.threads):
        st.session_state.running = False
        st.rerun()


live_log()

if st.session_state.results:
    st.subheader("Results")
    for key, res in st.session_state.results.items():
        event = res["event"]
        if res["status"] == "success":
            with st.container(border=True):
                st.success(f"✅ **{event}** — {res['selected']} · {res['total']} kr")
                st.link_button(
                    "🔗 Open Stripe checkout",
                    res["checkout_url"],
                    use_container_width=True,
                    type="primary",
                )

                if key not in st.session_state.auto_opened:
                    st.session_state.auto_opened.add(key)
                    # Best-effort client-side auto-open: only works if the
                    # browser has popups allowed for this page — most browsers
                    # block window.open() unless it's a direct response to a
                    # user click, which finishing a background poll isn't. The
                    # link button above is the reliable fallback either way.
                    st.iframe(
                        f"<script>window.open({json.dumps(res['checkout_url'])}, '_blank');</script>",
                        height=1,
                    )
        elif res["status"] == "stopped":
            st.warning(f"**{event}** — stopped.")
        else:
            st.error(f"**{event}** — {res.get('message', 'failed')}")
