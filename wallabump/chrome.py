"""The bot's own Chrome: launching, closing, thawing tabs, attaching over CDP."""

from __future__ import annotations

import json
import logging
import socket
import subprocess
import time
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager

from playwright.sync_api import (
    Browser,
    Playwright,
)
from playwright.sync_api import Error as PlaywrightError

from .logic import PROFILE_DIR, ensure_dirs

log = logging.getLogger("wallabump")


CHROME_BINARY = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CDP_PORT = 9222
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"

# The handshake waits for every open tab to report in, and a tab that was just
# nudged awake can take seconds to do so. 30s was enough for 7 tabs and not for
# 10; the cost of a generous ceiling is only paid when something is wrong.
CDP_TIMEOUT_MS = 60000

# A suspended process keeps its wall clock but loses monotonic time, so a gap
# between the two is proof the Mac slept rather than the step being slow.
SLEEP_DRIFT_SECONDS = 60.0

# A raised tab takes a moment to thaw, and raising the next one puts it straight
# back in the background, so the activations have to be spaced. Measured thaw
# across a real 10-tab window: 0.3s to 4.1s, Gmail the slowest. At 1.5s the three
# slowest tabs were backgrounded mid-wake and stayed frozen, which stalled the
# handshake — so this sits above the slowest observed, not the average.
THAW_SECONDS = 5.0


def slept_seconds(mark: tuple[float, float]) -> float:
    """Seconds the machine spent suspended since `mark` was taken.

    `time.monotonic()` stops while macOS sleeps; `time.time()` does not. The
    difference between how much each advanced is the time spent asleep.
    Returns 0.0 when the clocks agree, i.e. the process ran without a break.
    """
    wall_then, mono_then = mark
    drift = (time.time() - wall_then) - (time.monotonic() - mono_then)
    return drift if drift > 0 else 0.0


def clock_mark() -> tuple[float, float]:
    return time.time(), time.monotonic()


def cdp_reachable() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", CDP_PORT), timeout=1.5):
            return True
    except OSError:
        return False


def launch_chrome(start_url: str) -> subprocess.Popen[bytes]:
    """Start a real, non-automated Chrome with its debugging port open.

    No `--enable-automation`, no Playwright involved in the launch — Chrome
    never sees itself as automated, so none of Wallapop's re-login triggers
    fire. The profile dir keeps the session cookies, so relaunching after a
    reboot or an accidental Cmd+Q is the same as a human reopening Chrome.
    Output goes to /dev/null: Chrome's own log lines must not land in the
    terminal that launched it.
    """
    ensure_dirs()
    return subprocess.Popen(
        [
            CHROME_BINARY,
            f"--user-data-dir={PROFILE_DIR}",
            f"--remote-debugging-port={CDP_PORT}",
            "--no-first-run",
            "--no-default-browser-check",
            # Keep background tabs running. Chrome freezes long-idle ones, and a
            # frozen tab stalls every future CDP handshake (see wake_targets).
            # These three are what Playwright passes its own Chrome.
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            start_url,
        ],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def ensure_chrome(
    start_url: str, wait_seconds: float = 15.0
) -> subprocess.Popen[bytes] | None:
    """Launch Chrome if the debugging port is dead, then wait for it.

    Returns the process when this call launched it, None when a Chrome was
    already answering — that one belongs to whoever started it.
    """
    if cdp_reachable():
        return None
    log.info("Chrome not running; launching it from the saved profile.")
    process = launch_chrome(start_url)
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if cdp_reachable():
            time.sleep(2)  # let the first tab settle before attaching
            return process
        time.sleep(0.5)
    quit_chrome(process)
    raise RuntimeError(
        f"Launched Chrome but {CDP_URL} never came up within {wait_seconds:.0f}s. "
        "Run: python bump.py login"
    )


def quit_chrome(process: subprocess.Popen[bytes]) -> None:
    """Close a Chrome this script launched, cleanly enough to keep its cookies.

    SIGTERM is Chrome's orderly shutdown: it writes the profile out, so the
    sessions are there on the next launch. SIGKILL only if it will not go,
    since that skips the write and earns the next launch a "restore pages?"
    prompt.
    """
    process.terminate()
    try:
        process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        log.warning("Chrome ignored SIGTERM; killing it.")
        process.kill()
        process.wait(timeout=10)
    log.info("Closed the Chrome this run opened.")


@contextmanager
def owned_chrome(start_url: str) -> Iterator[None]:
    """Chrome for the length of one command, closed afterwards if this opened it.

    Leaving the bot's Chrome running between runs is what turned it into the
    everyday browser: macOS activates the already-running Chrome on a Dock
    click, so from its first launch every new window landed in the bot's
    profile — dragging in the tabs that freeze and stall the CDP handshake. A
    Chrome that was already running when the command started is left alone.
    """
    launched = ensure_chrome(start_url)
    try:
        yield
    finally:
        if launched is not None:
            quit_chrome(launched)


def page_targets_to_activate(
    targets: list[dict[str, str]], start_url: str
) -> list[str]:
    """Ids of every open tab, with our own catalog tab last.

    Activating a tab raises it, so whichever is activated last is the one left
    in front. Ending on the catalog tab means a run never parks the browser on
    somebody's mail. Only `page` targets can be activated; service workers and
    iframes are not tabs and do not freeze this way.
    """
    pages = [t for t in targets if t.get("type") == "page" and t.get("id")]
    ours = [t["id"] for t in pages if t.get("url", "").startswith(start_url)]
    return [t["id"] for t in pages if t["id"] not in ours] + ours


def wake_targets(start_url: str) -> None:
    """Thaw every open tab so the CDP handshake can finish.

    Chrome freezes tabs left idle in the background, and a frozen renderer
    answers no CDP command — not slowly, never. `connect_over_cdp` attaches to
    every target in the browser and waits for each one to initialise, so a
    single frozen tab stalls the whole handshake until it times out, with the
    browser itself perfectly healthy and answering browser-level CDP in
    milliseconds. Activating a tab thaws it, and the plain HTTP endpoint does
    that without a websocket.

    Spacing matters: raising a tab backgrounds the previous one, so activating
    them in a burst thaws only the last. Each one gets THAW_SECONDS to come back
    before the next is raised. A thawed tab stays thawed once backgrounded
    again, so the whole browser is awake by the time the handshake starts.

    Best effort throughout: a browser that will not list or thaw its tabs is
    for `connect_over_cdp` to report, not for this to pre-empt.
    """
    try:
        with urllib.request.urlopen(f"{CDP_URL}/json/list", timeout=5) as response:
            targets = json.load(response)
    except (OSError, ValueError):
        log.warning("Could not list Chrome's tabs to thaw them; attaching anyway.")
        return
    target_ids = page_targets_to_activate(targets, start_url)
    thawed = 0
    for target_id in target_ids:
        try:
            urllib.request.urlopen(
                f"{CDP_URL}/json/activate/{target_id}", timeout=5
            ).close()
        except OSError:
            continue
        thawed += 1
        time.sleep(THAW_SECONDS)
    log.info("Thawed %d of %d open tabs before attaching.", thawed, len(target_ids))


def attach(playwright: Playwright, start_url: str) -> Browser:
    """Connect to the already-running Chrome over CDP.

    The timeout is generous because a thawing tab takes a few seconds to answer
    its first command, and the handshake waits for every open tab in turn.
    """
    ensure_chrome(start_url)
    wake_targets(start_url)
    try:
        return playwright.chromium.connect_over_cdp(CDP_URL, timeout=CDP_TIMEOUT_MS)
    except PlaywrightError:
        # One straggler that woke too slowly is the common case, and by now it
        # has had the whole timeout to wake. A second pass re-raises whatever is
        # still frozen and costs one extra attempt on a twice-weekly run.
        log.warning("CDP handshake stalled; thawing tabs again and retrying once.")
        wake_targets(start_url)
    try:
        return playwright.chromium.connect_over_cdp(CDP_URL, timeout=CDP_TIMEOUT_MS)
    except PlaywrightError as exc:
        # Distinguish "nothing is listening" from "it answered, then stalled".
        # Reporting a stalled handshake as an unreachable browser sends you off
        # to re-run `login`, which fixes nothing.
        if cdp_reachable():
            raise RuntimeError(
                f"Chrome answered at {CDP_URL} but the CDP handshake did not "
                f"finish within {CDP_TIMEOUT_MS // 1000}s, twice. A tab Chrome "
                "froze would not thaw. Quit that Chrome and re-run to clear it."
            ) from exc
        raise RuntimeError(
            f"Chrome with remote debugging isn't reachable at {CDP_URL}. "
            "Run: python bump.py login"
        ) from exc
