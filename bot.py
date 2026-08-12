#!/usr/bin/env python3
"""
Samfundet ticket bot — reserves tickets via Billig and opens Stripe checkout.

Usage:
  python bot.py --event 5423-toga --email you@example.com
  python bot.py --event 5279-annika --membercard 12345678 --price-type member
  python bot.py --event 5423-toga --email you@example.com --wait-until 14:00 --count 2
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from datetime import datetime, date
from http.cookiejar import CookieJar
from typing import Literal

PAY_URL = "https://billettsalg.samfundet.no/pay"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)

PriceType = Literal["member", "non-member", "auto"]


@dataclass
class PriceGroup:
    field_name: str
    label: str
    price: int


@dataclass
class BuyForm:
    action: str
    token: str
    price_groups: list[PriceGroup]


@dataclass
class BotConfig:
    event: str
    email: str | None = None
    membercard: str | None = None
    price_type: PriceType = "auto"
    count: int = 1
    wait_until: datetime | None = None
    poll_interval: float = 0.25
    max_attempts: int | None = None
    no_browser: bool = False


# Global event to signal all threads to exit immediately on interrupt
stop_event = threading.Event()


def log(msg: str, prefix: str = "") -> None:
    prefix_part = f"[{prefix}] " if prefix else ""
    lines = msg.splitlines()
    if not lines:
        print(prefix_part)
        return
    formatted = "\n".join(f"{prefix_part}{line}" for line in lines)
    print(formatted)


def build_opener() -> urllib.request.OpenerDirector:
    jar = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = [("User-Agent", USER_AGENT)]
    return opener


def buy_url(event_slug: str) -> str:
    slug = event_slug.removeprefix("https://www.samfundet.no/arrangement/")
    slug = slug.removeprefix("/arrangement/")
    return f"https://www.samfundet.no/arrangement/{slug}/buy"


def fetch(opener: urllib.request.OpenerDirector, url: str) -> str:
    with opener.open(url, timeout=15) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_buy_form(html: str) -> BuyForm | None:
    if "action=\"https://billettsalg.samfundet.no/pay\"" not in html:
        return None

    token_match = re.search(r'name="authenticity_token" value="([^"]+)"', html)
    if not token_match:
        return None

    price_groups: list[PriceGroup] = []
    for row in re.finditer(
        r"<tr class='price-group-row'>(.*?)</tr>", html, re.DOTALL
    ):
        chunk = row.group(1)
        name_match = re.search(r'name="(price_\d+_count)"', chunk)
        label_match = re.search(r"<td>\s*([^<]+?)\s*</td>", chunk)
        price_match = re.search(r"data-price='(\d+)'", chunk)
        if not (name_match and label_match and price_match):
            continue
        price_groups.append(
            PriceGroup(
                field_name=name_match.group(1),
                label=label_match.group(1).strip(),
                price=int(price_match.group(1)),
            )
        )

    if not price_groups:
        return None

    return BuyForm(
        action=PAY_URL,
        token=token_match.group(1),
        price_groups=sorted(price_groups, key=lambda pg: pg.price),
    )


def pick_price_group(
    groups: list[PriceGroup], price_type: PriceType
) -> PriceGroup:
    if len(groups) == 1:
        return groups[0]

    labels = [g.label.lower() for g in groups]
    if price_type == "member" or (
        price_type == "auto" and any("medlem" in l and "ikke" not in l for l in labels)
    ):
        for g in groups:
            label = g.label.lower()
            if "medlem" in label and "ikke" not in label:
                return g

    if price_type == "non-member" or price_type == "auto":
        for g in groups:
            if "ikke" in g.label.lower():
                return g

    return groups[0]


def wait_until(target: datetime, prefix: str = "") -> None:
    while not stop_event.is_set():
        now = datetime.now()
        if now >= target:
            return
        remaining = (target - now).total_seconds()
        if remaining > 60:
            log(f"Waiting… {int(remaining)}s until {target.strftime('%H:%M:%S')}", prefix)
            sleep_time = min(30, remaining - 30)
            for _ in range(int(sleep_time * 2)):
                if stop_event.is_set():
                    return
                time.sleep(0.5)
        elif remaining > 5:
            log(f"Waiting… {remaining:.0f}s", prefix)
            for _ in range(2):
                if stop_event.is_set():
                    return
                time.sleep(0.5)
        else:
            time.sleep(0.05)


def poll_buy_page(
    opener: urllib.request.OpenerDirector,
    url: str,
    *,
    wait_until_time: datetime | None,
    poll_interval: float,
    max_attempts: int | None,
    prefix: str = "",
) -> BuyForm:
    if wait_until_time:
        wait_until(wait_until_time, prefix)
        if stop_event.is_set():
            raise InterruptedError("Stopped")

    attempts = 0
    while (max_attempts is None or attempts < max_attempts) and not stop_event.is_set():
        attempts += 1
        try:
            html = fetch(opener, url)
        except urllib.error.HTTPError as exc:
            log(f"HTTP {exc.code} — retrying…", prefix)
            for _ in range(int(poll_interval * 4)):
                if stop_event.is_set():
                    raise InterruptedError("Stopped")
                time.sleep(0.25)
            if poll_interval % 0.25 > 0:
                time.sleep(poll_interval % 0.25)
            continue
        except urllib.error.URLError as exc:
            log(f"Network error: {exc.reason} — retrying…", prefix)
            for _ in range(int(poll_interval * 4)):
                if stop_event.is_set():
                    raise InterruptedError("Stopped")
                time.sleep(0.25)
            if poll_interval % 0.25 > 0:
                time.sleep(poll_interval % 0.25)
            continue

        form = parse_buy_form(html)
        if form:
            return form

        if attempts == 1:
            log("Buy page not open yet — polling…", prefix)
        
        for _ in range(int(poll_interval * 4)):
            if stop_event.is_set():
                raise InterruptedError("Stopped")
            time.sleep(0.25)
        if poll_interval % 0.25 > 0:
            time.sleep(poll_interval % 0.25)

    if stop_event.is_set():
        raise InterruptedError("Stopped")
    raise RuntimeError("Buy page never became available")


def submit_purchase(
    opener: urllib.request.OpenerDirector,
    form: BuyForm,
    *,
    referer: str,
    count: int,
    price_group: PriceGroup,
    email: str | None,
    membercard: str | None,
) -> str:
    data: dict[str, str] = {
        "utf8": "✓",
        "authenticity_token": form.token,
        "commit": "Til betaling",
    }

    for pg in form.price_groups:
        data[pg.field_name] = str(count if pg.field_name == price_group.field_name else 0)

    if membercard:
        data["ticket_type"] = "card"
        data["membercard"] = membercard
    elif email:
        data["ticket_type"] = "paper"
        data["email"] = email
    else:
        raise ValueError("Provide --email or --membercard")

    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(form.action, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Referer", referer)

    with opener.open(req, timeout=30) as resp:
        checkout_url = resp.url
        if "checkout.stripe.com" not in checkout_url:
            page = resp.read().decode("utf-8", errors="replace")
            if "handlekurv" in checkout_url or "bsession=" in checkout_url:
                msg = re.search(r"id=\"dynamic-error\"[^>]*>([^<]+)", page)
                raise RuntimeError(
                    f"Purchase failed: {msg.group(1).strip() if msg else checkout_url}"
                )
            raise RuntimeError(f"Unexpected redirect: {checkout_url}")
        return checkout_url


def parse_wait_until(value: str) -> datetime:
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            parsed = datetime.strptime(value, fmt)
            return datetime.combine(date.today(), parsed.time())
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"Invalid time: {value!r} (use HH:MM or HH:MM:SS)")


def load_config(path: str) -> list[BotConfig]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        raise ValueError("Configuration file must contain a JSON list of bot objects.")

    configs: list[BotConfig] = []
    for idx, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"Config item at index {idx} must be an object.")

        event = item.get("event")
        if not event or not isinstance(event, str):
            raise ValueError(f"Config item at index {idx} is missing a valid 'event' string.")

        email = item.get("email")
        membercard = item.get("membercard")
        if not email and not membercard:
            raise ValueError(f"Config item at index {idx} ({event}) must provide either 'email' or 'membercard'.")

        price_type = item.get("price_type", "auto")
        if price_type not in ("member", "non-member", "auto"):
            raise ValueError(f"Config item at index {idx} ({event}) has invalid price_type: {price_type!r}")

        count = item.get("count", 1)
        if not isinstance(count, int) or count < 1 or count > 9:
            raise ValueError(f"Config item at index {idx} ({event}) count must be an integer between 1 and 9.")

        wait_until_str = item.get("wait_until")
        wait_until_val: datetime | None = None
        if wait_until_str:
            try:
                wait_until_val = parse_wait_until(wait_until_str)
            except Exception as e:
                raise ValueError(f"Config item at index {idx} ({event}) has invalid wait_until: {e}")

        configs.append(
            BotConfig(
                event=event,
                email=email,
                membercard=membercard,
                price_type=price_type,
                count=count,
                wait_until=wait_until_val,
                poll_interval=float(item.get("poll_interval", 0.25)),
                max_attempts=item.get("max_attempts"),
                no_browser=bool(item.get("no_browser", False)),
            )
        )
    return configs


def run_single_bot(config: BotConfig) -> int:
    prefix = config.event
    url = buy_url(config.event)
    opener = build_opener()

    log(f"Event buy page: {url}", prefix)
    try:
        form = poll_buy_page(
            opener,
            url,
            wait_until_time=config.wait_until,
            poll_interval=config.poll_interval,
            max_attempts=config.max_attempts,
            prefix=prefix,
        )
    except InterruptedError:
        log("Interrupted.", prefix)
        return 130
    except Exception as exc:
        log(f"Error while polling: {exc}", prefix)
        return 1

    log("Price groups:", prefix)
    for pg in form.price_groups:
        log(f"  {pg.label}: {pg.price} kr ({pg.field_name})", prefix)

    selected = pick_price_group(form.price_groups, config.price_type)
    log(f"Selected: {selected.label} × {config.count} ({selected.price * config.count} kr)", prefix)

    try:
        checkout_url = submit_purchase(
            opener,
            form,
            referer=url,
            count=config.count,
            price_group=selected,
            email=config.email,
            membercard=config.membercard,
        )
    except Exception as exc:
        log(f"Purchase submission failed: {exc}", prefix)
        return 1

    log(f"Stripe checkout ready:\n{checkout_url}\n", prefix)
    if config.no_browser:
        log("Complete payment in your browser.", prefix)
    else:
        webbrowser.open(checkout_url)
        log("Opened Stripe checkout — complete payment there.", prefix)

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Samfundet ticket bot")
    parser.add_argument(
        "--event",
        help="Event slug or URL, e.g. 5423-toga or 5279-annika (required unless --config is used)",
    )
    parser.add_argument("--email", help="Email for digital tickets")
    parser.add_argument("--membercard", help="Samfundet member card number")
    parser.add_argument(
        "--price-type",
        choices=["member", "non-member", "auto"],
        default="auto",
        help="Which price group to buy (default: auto = cheapest member tier)",
    )
    parser.add_argument("--count", type=int, default=1, help="Number of tickets (1-9)")
    parser.add_argument(
        "--wait-until",
        type=parse_wait_until,
        help="Wait until local time before polling, e.g. 14:00",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=0.25,
        help="Seconds between buy-page polls (default: 0.25)",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=None,
        help="Stop after N poll attempts (default: unlimited)",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Print Stripe URL instead of opening browser",
    )
    parser.add_argument(
        "--config",
        help="Path to JSON configuration file to run multiple bots in parallel",
    )
    args = parser.parse_args()

    configs: list[BotConfig] = []
    if args.config:
        try:
            configs = load_config(args.config)
        except Exception as exc:
            parser.error(f"Failed to load config: {exc}")
        if args.no_browser:
            for c in configs:
                c.no_browser = True
    else:
        if not args.event:
            parser.error("the following arguments are required: --event (or specify --config)")
        if not args.email and not args.membercard:
            parser.error("Provide --email or --membercard")
        if args.count < 1 or args.count > 9:
            parser.error("--count must be between 1 and 9")

        configs.append(
            BotConfig(
                event=args.event,
                email=args.email,
                membercard=args.membercard,
                price_type=args.price_type,
                count=args.count,
                wait_until=args.wait_until,
                poll_interval=args.poll_interval,
                max_attempts=args.max_attempts,
                no_browser=args.no_browser,
            )
        )

    if len(configs) == 1:
        return run_single_bot(configs[0])

    log(f"Starting {len(configs)} bots in parallel...")
    exit_code = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(configs)) as executor:
        futures = {executor.submit(run_single_bot, config): config for config in configs}
        try:
            for future in concurrent.futures.as_completed(futures):
                config = futures[future]
                try:
                    res = future.result()
                    if res != 0:
                        exit_code = res
                except Exception as exc:
                    log(f"Bot failed with unhandled exception: {exc}", config.event)
                    exit_code = 1
        except KeyboardInterrupt:
            stop_event.set()
            log("Shutting down parallel bots. Please wait...")
            executor.shutdown(wait=True)
            raise

    return exit_code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nStopped.")
        raise SystemExit(130)
