"""Auto fishing: cast, wait for the bite, reel by the bar colour, close the catch, repeat."""
import re
import time

import numpy as np

import game
import labels
import merchant
import points

# VIP: spawn -> Captain Flarg (ends on E), then shop -> fishing spot
TO_SHOP = [(0.0, 'down', 'w'), (0.7, 'down', 'a'), (5.2, 'up', 'w'), (5.2, 'up', 'a'), (5.3, 'down', 'w'),
           (6.4, 'up', 'w'), (6.7, 'down', 's'), (6.9, 'up', 's'), (7.1, 'down', ' '), (7.125, 'down', 'w'),
           (8.325, 'up', ' '), (8.525, 'up', 'w'), (8.825, 'down', 'e'), (9.125, 'up', 'e')]
TO_SPOT = [(0.0, 'down', 'd'), (2.3, 'up', 'd'), (2.3, 'down', 'w'), (4.075, 'up', 'w')]
# same two walks without VIP
TO_SHOP_FREE = [(0.0, 'down', 'w'), (0.875, 'down', 'a'), (6.725, 'up', 'w'), (6.725, 'up', 'a'), (6.825, 'down', 'w'),
                (8.2, 'up', 'w'), (8.5, 'down', 's'), (8.75, 'up', 's'), (8.95, 'down', ' '), (8.975, 'down', 'w'),
                (10.175, 'up', ' '), (10.725, 'up', 'w'), (11.025, 'down', 'e'), (11.325, 'up', 'e')]
TO_SPOT_FREE = [(0.0, 'down', 'd'), (2.875, 'up', 'd'), (2.875, 'down', 'w'), (5.075, 'up', 'w')]
ZOOM_OUT_S = 1.5         # hold O before the first cast; zoomed in you lose the bobber
ZOOM_TOPUP_S = 0.3       # short top-up before later casts
BITE_WHITE = 200         # lit is 245-255 (~230 over Remote Desktop), resting under 40
BITE_CAP = 244           # so a tinted bite still counts
BITE_POLL_S = 0.05       # the spot stays lit for the whole catch
DRY_CASTS = 2            # Fish doing nothing this many times = full bag
REEL_S = 9.0             # a reel never lasts longer
THIN = 20 / 405          # FishSol's 20 px of a 405 px bar
RUN_TOL = 20             # zone colour drifts ~15 on bright backgrounds, the fill is ~100 off
NO_BITE_S = 30.0        # then the failsafe takes a step
REEL_MIN_S = 1.0
BAR_SHOWS_S = 1.5
GONE_LOOKS = 3          # catch bar missing this many looks in a row = reel over
CLOSE_WAIT_S = 0.2
X_SHOWS_S = 2.5          # the X shows ~1.9 s after the reel
SEE_CAST_S = 2.0
LOST_RUN = 5            # this many "got away" in a row = the catch window can't be seen
SEE_CATCH_S = 5.0        # no catch window by then = fish got away
SEE_SHUT_S = 1.5
TRIES = 3
TALK_WAIT_S = 2.0
SHOP_TRIPS = 3           # tries at reaching Flarg before fishing without selling
ALIGN_REJOIN = 3         # failed squarings in a row before a rejoin
SELL_CLOSE_S = 1.0       # wait after Confirm
SELL_MAX = 100           # each Sell All clears one kind of fish
SELL_AHEAD = 0.5         # sell half the guessed kinds left before counting again
SLOW_STEP = 0.15         # extra wait per Sell All when the game drops some
SLOW_MAX = 0.6


def press(key, hold=0.05):
    game.down(key)
    time.sleep(hold)
    game.up(key)


def nap(seconds, halt):
    end = time.perf_counter() + seconds * game.PACE
    while time.perf_counter() < end:
        if halt():
            return False
        time.sleep(0.01)
    return not halt()


def walk(route, halt):
    held, last = set(), 0.0
    try:
        for t, how, key in route:
            end = time.perf_counter() + t - last
            while time.perf_counter() < end:
                if halt():
                    return False
                if game.PAUSED.is_set() and not game.ABORT:
                    # hold the walk while the user has the PC; paused time does not count
                    t0 = time.perf_counter()
                    time.sleep(0.05)
                    end += time.perf_counter() - t0
                    continue
                time.sleep(0.005)
            if how == 'down':
                game.down(key)
                held.add(key)
            else:
                game.up(key)
                held.discard(key)
            last = t
        return not halt()
    finally:
        for key in held:
            game.up(key)


RESPAWNED_S = 3.0           # back in ~1 s, but clicks are ignored a bit longer


def respawn(halt):
    if game.take_foreground() is None:
        return False
    for key, wait in (('esc', 1.25), ('r', 0.75), ('enter', RESPAWNED_S)):
        press(key)
        if not nap(wait, halt):
            return False
    return True


def pixel(xy):
    return tuple(int(v) for v in game.grab((xy[0], xy[1], xy[0] + 1, xy[1] + 1))[0, 0])


def bad_colour(c):
    """White bite flash, dark backdrop, or the grey fade-in: not a real bar colour."""
    return min(c) >= 245 or max(c) < 100 or max(c) - min(c) < 30


def majority(mask):
    """3x3 majority vote: the see-through bar gets speckled, straight edges survive."""
    h, w = mask.shape
    p = np.pad(mask, 1, mode='edge').astype(np.uint8)
    return sum(p[y:y + h, x:x + w] for y in range(3) for x in range(3)) >= 5


def run_width(rgb, colour):
    """Longest run of the bar colour on any row, or None if it's not there."""
    diff = np.abs(rgb.astype(int) - np.array(colour))
    run = majority(np.all(diff <= RUN_TOL, axis=2))
    if not (np.all(diff <= 5, axis=2) & run).any():
        return None
    best = 0
    for row in run:
        idx = np.flatnonzero(row)
        if idx.size:
            gaps = np.flatnonzero(np.diff(idx) > 1)
            starts, ends = np.r_[0, gaps + 1], np.r_[gaps, idx.size - 1]
            best = max(best, int((idx[ends] - idx[starts]).max()) + 1)
    return best


def fast_click():
    game.button('left', True)
    time.sleep(0.01)
    game.button('left', False)


def cast(halt, zoom=ZOOM_OUT_S, full_check=True):
    if game.take_foreground() is None:
        return False
    game.down('o')
    try:
        if not nap(zoom, halt):
            return False
    finally:
        game.up('o')
    points.leave_dialogue(tries=2, skip=True)               # a leftover dialogue covers Fish
    for go in range(1, TRIES + 1):
        if window_state() in ('catch', 'failed'):          # a late catch window sits over Fish
            fclick('fishingMinigameResultCloseButton')
        if not fclick('fishingMinigameFishButton', 0.1):
            return False
        st = wait_state(lambda st: st not in ('fish', 'catch', 'failed'), SEE_CAST_S, halt)
        if st not in ('fish', 'catch', 'failed'):
            return not halt()                   # cast, already biting, or unreadable: carry on
        if go == 1 and st == 'fish' and full_check and bag_full():
            return 'full'
        print('fishing: Fish still showing after the click (go %d of %d)' % (go, TRIES))
        fclick('fishingMinigameResultCloseButton')  # the centred window can leave Fish dead even when not seen as moved
    return not halt()


def bag_full():
    """The red 'Cannot Fish / not enough inventory space' note, bottom right."""
    w = game.roblox_window()
    if w is None:
        return False
    x0, y0, cw, ch = game.client_rect(w)
    img = game.grab((x0 + cw // 2, y0 + ch // 2, x0 + cw, y0 + ch))
    a = img.astype(int)
    red = (a[..., 0] >= 200) & (a[..., 0] > a[..., 1] + 90) & (a[..., 0] > a[..., 2] + 90)
    if red.sum() < 40:
        return False
    ys, xs = np.nonzero(red)
    crop = img[max(0, ys.min() - 10):ys.max() + 10, max(0, xs.min() - 10):xs.max() + 10]
    seen = labels.cannot_fish(crop, cw, ch)             # by its look first, OCR only if unsure
    if seen is not None:
        return seen
    try:
        text = ''.join(merchant.read_lines(crop)).lower().replace(' ', '')
    except Exception:
        return False
    return 'inventory' in text or 'cannotfish' in text


def icon_level():
    icon = fcentre('fishingMinigameIcon')
    return 0 if icon is None else min(pixel(icon))


def cutoff(idle):
    return min(BITE_CAP, max(BITE_WHITE, idle + 30))


def bitten(within, halt, idle=0):
    """True once the icon lights up after being dark since the cast, or the catch bar shows."""
    icon = fcentre('fishingMinigameIcon')
    if icon is None:
        return False
    global peak, dipped
    white = cutoff(idle)
    end = time.perf_counter() + within * game.PACE
    while time.perf_counter() < end and not halt():
        level = min(pixel(icon))
        peak = max(peak, level)
        if level < white:
            dipped = True
        elif dipped:
            return True
        if catch_bar_up():          # some sizes only flash the icon, dimmer over Remote Desktop
            return True
        time.sleep(BITE_POLL_S)
    return False


peak = 0            # brightest since the cast, for the log
dipped = False


def _share(rgb, colour):
    a = rgb.astype(int)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    if colour == 'blue':
        hit = (b >= 140) & (b > r + 30) & (b > g + 30)
    elif colour == 'red':
        hit = (r >= 140) & (r > g + 50) & (r > b + 20)
    elif colour == 'text':          # white or any bright colour
        hi, lo = a.max(axis=2), a.min(axis=2)
        hit = (lo >= 225) | ((hi >= 180) & (hi - lo >= 90))
    elif colour == 'dark':
        hit = a.max(axis=2) <= 120
    else:
        hit = a.min(axis=2) >= 225
    return float(hit.mean())


bugged = False      # the fishing window is sometimes centred, moving its buttons up


def _shift(on):
    """Vertical offset of the bugged (centred) window, or 0."""
    if not on:
        return 0
    w, root = game.roblox_window(), points.box('fishingMinigamePromptWindowRoot')
    if w is None or root is None:
        return 0
    _, y0, _, ch = game.client_rect(w)
    return round(y0 + ch / 2 - (root[1] + root[3]) / 2)


def fbox(name, on=None):
    b, dy = points.box(name), _shift(bugged if on is None else on)
    return None if b is None else (b[0], b[1] + dy, b[2], b[3] + dy)


def fcentre(name):
    c = points.centre(name)
    dy = 0 if name.startswith('fishingMinigameResult') else _shift(bugged)     # the catch window never moves
    return None if c is None else (c[0], c[1] + dy)


def fclick(name, settle=0.3):
    """Click where the button is now. The catch X also clicks the bugged window's X."""
    if name != 'fishingMinigameResultCloseButton':
        c = fcentre(name)
        if c is None or not game.click(*c, park=False):
            return False
        game.paced(settle)
        return True
    if not points.click(name, 0):
        return False
    root, w = fbox('fishingMinigamePromptWindowRoot', True), game.roblox_window()
    if root is not None and w is not None:
        k = game.client_rect(w)[3] / 1080
        game.click(round(root[2] - 8 * k), round(root[1] + 9 * k), park=False, settle=0)
    game.paced(settle)
    return True


def _button_state(on):
    btn = fbox('fishingMinigameFishButton', on)
    if btn is None:
        return None
    rgb = game.grab(btn)
    blue, red = _share(rgb, 'blue'), _share(rgb, 'red')
    if blue >= 0.02 and red < 0.005:
        return 'fish'
    if red >= 0.02 and blue < 0.005:
        return 'line'
    return None


def _centred_edge():
    """The centred window has a white top edge over a dark body; a blue aura or sea has neither."""
    root = fbox('fishingMinigamePromptWindowRoot', True)
    if root is None:
        return False
    cut = (root[2] - root[0]) // 10
    rows = game.grab((root[0] + cut, root[1] - 4, root[2] - cut, root[1] + 8))
    light = (rows.min(axis=2) >= 120).mean(axis=1)
    return len(light) == 12 and light[:7].max() >= 0.9 and light[7:].max() <= 0.3


def window_state():
    """'catch', 'failed', 'fish', 'line' or None if unsure. None never clicks.
    Finding the button where the bugged window puts it flips `bugged`."""
    global bugged
    head, x = points.box('fishingMinigameResultHeaderLabel'), points.box('fishingMinigameResultCloseIcon')
    shot = game.grab(x) if head and x else None             # the X sits on the window's dark body; with no window
    if shot is not None and _share(shot, 'white') >= 0.03 and _share(shot, 'dark') >= 0.5:   # a white sky is white
        top = game.grab(head)
        if _share(top, 'red') >= 0.05:
            return 'failed'
        if _share(top, 'text') >= 0.03:         # title colour changes with the fish's mutation
            return 'catch'
    for on in (False, True):                      # normal spot first so one odd read can't stick
        st = _button_state(on)
        if st is not None and (not on or _centred_edge()):
            if on != bugged:
                bugged = on
                print('fishing: the window is %s' % ('bugged into the middle of the screen' if on else 'back in place'))
            return st
    return None


def wait_state(want, seconds, halt):
    """Wait for a state (or a test on one). Returns the last state seen.
    None only counts after a few looks in a row."""
    test = want if callable(want) else (lambda st: st == want)
    end, st, blank = time.perf_counter() + seconds * game.PACE, None, 0
    while time.perf_counter() < end and not halt():
        st = window_state()
        blank = blank + 1 if st is None else 0
        if test(st) and (st is not None or blank >= 3):
            return st
        time.sleep(0.1)
    return st


def catch_bar_up():
    """Catch bar showing? Its outline is a long straight light line. None if unknown."""
    b = fbox('fishingMinigameCatchBarOuter')
    if b is None:
        return None
    rgb = game.grab((b[0], b[1] - 3, b[2], b[3] + 3))
    return bool(((rgb.min(axis=2) >= 120).mean(axis=1) >= 0.9).any())


def reel(halt):
    """Click while the bar colour is thin or gone, until the catch bar goes. Returns the click count."""
    bar, park = fbox('fishingMinigameBarOuter'), fcentre('fishingMinigameFishButton')
    if bar is None or park is None:
        return 0
    game.glide(*park)
    end = time.perf_counter() + BAR_SHOWS_S
    up = catch_bar_up()
    while not up and time.perf_counter() < end and not halt():
        time.sleep(0.02)
        up = catch_bar_up()
    if not up and window_state() == 'line':
        return None                              # no catch bar: a click here hits Exit
    # sample spot: over the success zone at the start, above the timer
    mid = (bar[0] + (bar[2] - bar[0]) * 196 // 405, bar[1] + (bar[3] - bar[1]) * 5 // 21)
    colour = pixel(mid)
    for _ in range(60):
        if not bad_colour(colour) or halt():
            break
        time.sleep(0.02)
        colour = pixel(mid)
    thin = max(3, round(THIN * (bar[2] - bar[0])))
    start = time.perf_counter()
    clicks, end, seen, gone = 0, start + REEL_S, False, 0
    while time.perf_counter() < end and not halt():
        up = catch_bar_up()
        seen = seen or bool(up)
        gone = gone + 1 if seen and up is False else 0
        if gone >= GONE_LOOKS and time.perf_counter() - start >= REEL_MIN_S:
            break                                # done, don't click into the catch window
        if bad_colour(colour):
            fast_click()                         # no real bar colour: click slow and safe
            clicks += 1
            time.sleep(0.15)
            continue
        width = run_width(game.grab(bar), colour)
        if width is None or width < thin:
            fast_click()
            clicks += 1
            time.sleep(0.025)
        time.sleep(0.01)
    return clicks


def close_catch(halt):
    """Shut the catch window once its X shows. 'caught', 'lost', 'failed' or False if stopped.
    Needs a proper click (Roblox forward, glide onto it)."""
    if not nap(CLOSE_WAIT_S, halt):
        return False
    start, st = time.perf_counter(), None
    while not halt():
        st = window_state()                       # only 'catch' once the X is drawn
        waited = time.perf_counter() - start
        if st in ('catch', 'failed') or waited > SEE_CATCH_S * game.PACE:
            break                                 # slow PCs draw it late
        if st is None and waited > X_SHOWS_S * game.PACE:
            break                                 # can't read it here, assume the X is up
        time.sleep(0.05)
    if halt():
        return False
    if st in ('fish', 'line'):
        return 'lost'
    if st == 'failed':
        # Fail-safe: keep clicking the close X until the failed popup is gone.
        # The X can land in different positions (Roblox bug), so try every known
        # exit spot on every retry: center X, window top-right, then extra
        # corners/edges of the prompt box.
        for go in range(1, TRIES + 1):
            # 1) Standard fclick: center X + window top-right (built-in positions)
            if fclick('fishingMinigameResultCloseButton', 0.1):
                if wait_state(lambda s: s != 'failed', SEE_SHUT_S, halt) != 'failed':
                    return not halt() and 'failed'
            # 2) Extra exit positions around the X (keep all, add new ones)
            root = fbox('fishingMinigamePromptWindowRoot', True)
            w = game.roblox_window()
            if root is not None and w is not None:
                k = game.client_rect(w)[3] / 1080
                rx, ry = root[0], root[1]
                rw = root[2] - root[0]
                rh = root[3] - root[1]
                extra_positions = [
                    # original fallback (right-hand X)
                    (round(root[2] - 6 * k), round(root[1] + 7 * k)),
                    # top-left corner of prompt
                    (round(rx + 4 * k), round(ry + 4 * k)),
                    # top-right corner of prompt
                    (round(root[2] - 4 * k), round(ry + 4 * k)),
                    # bottom-left corner of prompt
                    (round(rx + 4 * k), round(root[3] - 4 * k)),
                    # bottom-right corner of prompt
                    (round(root[2] - 4 * k), round(root[3] - 4 * k)),
                    # centre of prompt box
                    (round((rx + root[2]) / 2), round((ry + root[3]) / 2)),
                ]
                for cx, cy in extra_positions:
                    if game.click(cx, cy, park=False, settle=0):
                        if wait_state(lambda s: s != 'failed', SEE_SHUT_S, halt) != 'failed':
                            return not halt() and 'failed'
            print('fishing: failed window still up after its X (go %d of %d)' % (go, TRIES))
            time.sleep(0.2)
        return False
    for go in range(1, TRIES + 1):
        if not fclick('fishingMinigameResultCloseButton', 0.1):
            return False
        if wait_state(lambda st: st != 'catch', SEE_SHUT_S, halt) != 'catch':
            return not halt() and 'caught'
        # Extra exit positions around the X (keep all standard positions first)
        root = fbox('fishingMinigamePromptWindowRoot', True)
        w = game.roblox_window()
        if root is not None and w is not None:
            k = game.client_rect(w)[3] / 1080
            rx, ry = root[0], root[1]
            extra_positions = [
                (round(root[2] - 6 * k), round(root[1] + 7 * k)),
                (round(rx + 4 * k), round(ry + 4 * k)),
                (round(root[2] - 4 * k), round(ry + 4 * k)),
                (round(rx + 4 * k), round(root[3] - 4 * k)),
                (round(root[2] - 4 * k), round(root[3] - 4 * k)),
                (round((rx + root[2]) / 2), round((ry + root[3]) / 2)),
            ]
            for cx, cy in extra_positions:
                if game.click(cx, cy, park=False, settle=0):
                    if wait_state(lambda st: st != 'catch', SEE_SHUT_S, halt) != 'catch':
                        return not halt() and 'caught'
        print('fishing: catch window still up after its X (go %d of %d)' % (go, TRIES))
    return not halt() and 'caught'


def talked(halt):
    """Wait for Flarg's choices to show, pressing E once more if not. False if he never answers."""
    for go in range(2):
        if go:
            press('e')
        for _ in range(2):
            if halt():
                return False
            points.click('dialogueSkipDialogue', 0.05)
        end = time.perf_counter() + TALK_WAIT_S * game.PACE
        while time.perf_counter() < end:
            if halt():
                return False
            if points.choice('green') or points.choice('red'):
                return True
            time.sleep(0.1)
    return False


def _ocr_texts(img, det=True):
    """Run RapidOCR and return recognized text strings regardless of rapidocr version shape."""
    if img is None or img.size == 0:
        return []
    try:
        result, _ = merchant.reader()(np.ascontiguousarray(img[..., ::-1]), use_det=det, use_cls=False)
    except Exception:
        return []
    out = []
    for row in (result or ()):
        if not row:
            continue
        # Common shapes: [box, text, score], [box, (text, score)], [text, score], etc.
        txt = row[1] if len(row) >= 3 else row[0]
        if isinstance(txt, (list, tuple)):
            txt = txt[0] if txt else ''
        if txt is None:
            continue
        if not isinstance(txt, str):
            try:
                txt = str(txt)
            except Exception:
                continue
        if txt:
            out.append(txt)
    return out


def read_fish_points():
    """Fish points/coins from the top number of the three at the game client's bottom-left.
    Returns an int, or None when the window is gone or the number can't be read."""
    w = game.roblox_window()
    if w is None:
        return None
    x0, y0, cw, ch = game.client_rect(w)
    if cw <= 0 or ch <= 0:
        return None
    # The currency sits at ~0.025 of the width and ~0.895 of the height, just above
    # the two smaller stats under it. Upscale before OCR: the digits are only ~14 px tall.
    box = (round(x0 + 0.005 * cw), round(y0 + 0.875 * ch),
           round(x0 + 0.13 * cw), round(y0 + 0.925 * ch))
    img = game.grab(box)
    if img is None or img.size == 0:
        return None
    up = np.ascontiguousarray(np.asarray(img)[..., ::-1])
    up = np.repeat(np.repeat(up, 3, axis=0), 3, axis=1)
    try:
        result, _ = merchant.reader()(up, use_det=True, use_cls=False)
    except Exception:
        return None
    for row in (result or ()):
        if not row:
            continue
        txt = row[1] if len(row) >= 3 else row[0]
        if isinstance(txt, (list, tuple)):
            txt = txt[0] if txt else ''
        compact = re.sub(r'[^0-9]', '', str(txt or ''))
        if compact and 3 <= len(compact) <= 12:
            return int(compact)
    return None


def _shop_info_box():
    """Left item-description pane of the Fishing Shop, derived from slot [1,1] and the Sell button."""
    slot, btn = points.box('fishingShopSellItem[1,1]'), points.box('fishingShopSellAllButton')
    if slot is None or btn is None:
        return None
    iw, ih = slot[2] - slot[0], slot[3] - slot[1]
    x1, y1, x2, y2 = max(0, round(slot[0] - 2.4 * iw)), round(slot[1] + 1.25 * ih), slot[0], btn[1]
    return (x1, y1, x2, y2) if x2 > x1 and y2 > y1 else None


def storage_empty(select=False, halt=lambda: False):
    """True when the Fishing Shop Sell bag is empty ('Inventory [0 / ...]' or 'Empty.' after selecting [1,1])."""
    if read_count() == 0:
        return True
    if select and (halt() or not points.click('fishingShopSellItem[1,1]', 0.12)):
        return False
    box = _shop_info_box()
    if box is None:
        return False
    texts = _ocr_texts(game.grab(box), det=True)
    return any(re.sub(r'[^a-z]', '', t.lower()) in ('empty', 'ernpty', 'emptv') for t in texts)


def first_fish():
    img = points.grab('fishingShopSellItem[1,1]')
    return None if img is None or img.size == 0 else img.astype(np.int16)


def unchanged(a, b):
    return a is not None and b is not None and a.shape == b.shape and float(np.abs(a - b).mean()) < 4


_room = None                # bag size from the last full count


def read_count():
    """Fish in the bag from 'Inventory [268 / 360]', or None.
    A lost '/' ('268360') is split by the last known bag size."""
    global _room
    texts = _ocr_texts(points.grab('fishingShopSellTab'), det=False)
    if not texts:
        return None
    joined = ' '.join(str(t) for t in texts if t is not None)
    nums = re.findall(r'\d+', joined.split('[', 1)[-1])
    if len(nums) >= 2 and int(nums[0]) <= int(nums[1]):
        _room = nums[1]
        return int(nums[0])
    if len(nums) == 1 and _room and nums[0].endswith(_room) and len(nums[0]) > len(_room):
        return int(nums[0][:-len(_room)])
    return None


slow = 0.0                  # extra wait per Sell All once the game starts dropping them


def slower(say):
    global slow
    if slow < SLOW_MAX:
        slow = round(min(SLOW_MAX, slow + SLOW_STEP), 2)
        say('Sell Alls went unsold: selling %.2f s slower from now on' % slow)


def sell_all(halt):
    for name, settle in (('fishingShopSellTab', 0.1), ('fishingShopSellItem[1,1]', 0.1),
                         ('fishingShopSellAllButton', 0.3 + slow), ('fishingShopSellConfirmSellButton', 0.1 + slow)):
        if halt() or not points.click(name, settle):         # give the confirm popup a moment
            return False
    return nap(SELL_CLOSE_S + slow, halt)


def sell(halt, times=1, say=print, every=True):
    """Sells the fish at Captain Flarg. Returns the Sell Alls done, or False."""
    if not points.click('dialogueChoice[1 of 1]', 0.4):      # middle choice is Sell Fish
        return False
    closed = False
    try:
        if points.leave_dialogue() and not nap(0.3, halt): # his dialogue sits over the shop until Leave
            return False
        if halt() or not points.click('fishingShopSellTab', 0.1):
            return False
        start = count = read_count() if every else None
        done, reads, same, last, batch, per = 0, 1, 0, None, 1, 0
        empty = (count == 0) or (count is None and storage_empty(select=True, halt=halt))
        if halt():
            return False
        if empty:
            say('fish storage is empty: nothing to sell')
        while not empty and count is not None and count > 0 and done < SELL_MAX:
            guess = max(1, int(count / per * SELL_AHEAD)) if per else 2
            batch = min(SELL_MAX - done, guess, batch * 2) # stops a 1-fish first kind guessing 200 kinds left
            for _ in range(batch):
                if not sell_all(halt):
                    return False
            done += batch
            now, reads = read_count(), reads + 1
            if now == 0:
                count = 0
                break
            if now is not None and (now >= count or (batch >= 4 and per and count - now < per * batch / 4)):
                slower(say)                                 # sold nothing, or under a quarter of what it should
            if now is None or now >= count:
                same += 1                                   # twice in a row and we stop
                if same >= 2:
                    break
                continue
            same, count, per = 0, now, (count - now) / batch
        # count unreadable: sell until empty or the first slot stops changing; Sell all = no cap
        limit = SELL_MAX if every else max(1, times)
        rescued = False
        while not empty and start is None and done < limit:
            if not sell_all(halt):
                return False
            done += 1
            if done < limit and storage_empty(select=True, halt=halt):
                say('fish storage is empty after %d Sell All' % done)
                break
            if halt():
                return False
            now = first_fish()
            if unchanged(last, now):
                if not nap(1.0 + 2.0 * same, halt):         # a laggy server answers late: wait longer each time
                    return False
                now = first_fish()                          # slow grid redraw, look again
            same = same + 1 if unchanged(last, now) else 0
            if same >= (3 if every else 2):
                if not every or rescued:
                    break                                   # bag's empty
                rescued = True                              # a late "Sell Confirm" may still be waiting: answer it
                if not points.click('fishingShopSellConfirmSellButton', 2.0):
                    return False
                now = first_fish()
                if unchanged(last, now):
                    break
                same = 0
            elif same == 0:
                rescued = False
            last = now
        if start is not None:
            say('fish in the bag: %d before selling, %d after (%d Sell All, %d reads)' % (start, count, done, reads))
        closed = points.click('fishingShopCloseButton', 0.2)
        if nap(0.4, halt):
            points.leave_dialogue(skip=True)                 # his dialogue outlives the shop and covers Fish
        return done if closed else False
    finally:
        if not closed and not halt():
            points.click('fishingShopCloseButton', 0.2)
            points.leave_dialogue(skip=True)


class Fisher:
    """One fishing session: get to the spot, catch, and step through a failsafe when nothing bites."""

    def __init__(self, vip=True, sell_every=0, sell_amount=10, sell_all=True, full_check=True, say=print, report=None):
        self.shop_route, self.spot_route = (TO_SHOP, TO_SPOT) if vip else (TO_SHOP_FREE, TO_SPOT_FREE)
        self.sell_every = max(0, int(sell_every or 0))
        self.sell_amount = max(1, int(sell_amount or 1))
        self.sell_all = sell_all
        self.full_check = full_check
        global slow, bugged
        slow, bugged = 0.0, False
        self.say = say
        self.report = report        # failsafe steps go to the webhook
        self.at_spot = False
        self.cast_at = None
        self.idle = 0
        self.stage = 0
        self.caught = self.since_sell = 0
        self.points_earned = 0               # fish points/coins gained from sales this run
        self.lost_run = 0
        self.zoomed = False
        self.hold = False           # merchant: land the fish, then stop casting
        self.pause = False          # SC/BR: land the fish, then wait uncast
        self.line_out = False
        self.recast = False         # something else used the screen, cast again before waiting
        self.dry = 0
        self.after_sell = False     # next cast is the first since selling
        self.rejoin = False         # for the app: rejoin Roblox

    def cast(self, halt, zoom=None):
        global peak, dipped
        self.recast = False         # a second cast would hit Exit and cancel this one
        if zoom is None:
            zoom = ZOOM_TOPUP_S if self.zoomed else ZOOM_OUT_S
        done = cast(halt, zoom, self.full_check)
        if done == 'full':
            return self.full_bag()
        self.after_sell = False
        ok = done and nap(0.5, halt)
        self.zoomed = self.zoomed or bool(ok)
        self.line_out = bool(ok)
        self.cast_at, self.idle, peak, dipped = time.monotonic(), icon_level(), 0, False
        return ok

    def full_bag(self):
        """Sell, or rejoin if it's still full on the first cast after selling."""
        self.line_out = self.at_spot = False
        if self.after_sell:
            self.rejoin = 'inventory still full right after selling'
            self.failsafe('%s: rejoining' % self.rejoin)
        else:
            self.failsafe('inventory full: selling')
        self.since_sell = 0
        return True

    def failsafe(self, what):
        self.say(what)
        if self.report:
            self.report(what[:1].upper() + what[1:])
        points.click('fishingShopCloseButton', 0.2)          # a leftover Fishing Shop covers everything else

    def lost(self):
        """We got moved (rejoin, merchant teleport): walk back before casting."""
        self.at_spot = self.line_out = self.zoomed = False
        self.hold = False

    def reel_in(self, halt):
        """Press Exit, only if it's showing (the same spot is Fish otherwise)."""
        if window_state() == 'line':
            fclick('fishingMinigameFishButton', 0.2)
        self.line_out = False
        return not halt()

    def to_spot(self, halt):
        """Spawn -> Flarg to sell -> fishing spot, then cast."""
        self.say('to the fishing spot, selling at Captain Flarg on the way')
        self.zoomed = False                     # respawn resets the camera
        sold_ok = False
        for trip in range(1, SHOP_TRIPS + 1):
            if not respawn(halt) or not game.align_camera(halt):
                if game.align_fails >= ALIGN_REJOIN and not halt():
                    self.rejoin = 'Collection would not open %d times in a row' % game.align_fails
                    self.failsafe('%s: rejoining' % self.rejoin)
                return False
            if not walk(self.shop_route, halt) or not nap(0.3, halt):
                return False
            points_before = read_fish_points()
            if talked(halt):
                sold = sell(halt, self.sell_amount, self.say, self.sell_all)
                points_after = read_fish_points()
                if points_before is not None and points_after is not None and points_after > points_before:
                    gained = points_after - points_before
                    self.points_earned += gained
                else:
                    gained = 0
                if sold is not False:              # 0 = bag was already empty
                    sold_ok = True
                    self.say('sold the fish: %d Sell All, %d caught since the last sell, +%d points'
                             % (sold, self.since_sell, gained))
                    self.since_sell = 0
                elif halt():
                    return False
                else:
                    self.say('the sale did not go through: trying again after %d more fish' % self.sell_every
                             if self.sell_every else 'the sale did not go through')
                    self.since_sell = 0
                break
            if halt():
                return False
            self.say("Flarg's message never came up (walk %d of %d)%s" % (
                trip, SHOP_TRIPS, ': resetting and walking again' if trip < SHOP_TRIPS else ': fishing without selling'))
            if trip == SHOP_TRIPS:
                self.since_sell = 0             # so it retries after the next batch, not every fish
        if not nap(0.2, halt) or not walk(self.spot_route, halt):
            return False
        self.at_spot = True
        self.after_sell = sold_ok  # only a real sale makes a still-full bag a rejoin reason
        if not self.cast(halt):
            self.at_spot = False
            return False
        return True

    def foiled(self, halt, how):
        self.say('Fishing Failed %s: casting again' % how)
        self.line_out = False
        return True if self.pause or self.hold else self.cast(halt)

    def step(self, halt, wait=8.0):
        """Wait up to `wait` s for a bite and land it. False if stopped."""
        if game.PAUSED.is_set() and not game.ABORT:
            return True                         # paused for the user: hold, do not fish
        if not self.at_spot:
            return self.to_spot(halt)
        if (self.pause or self.hold) and not self.line_out:
            return not halt()                   # wait for items / merchant to finish
        if self.recast:
            self.recast = False
            fclick('fishingMinigameResultCloseButton', 0.1)
            return self.cast(halt)
        if bitten(wait, halt, self.idle):
            clicks = reel(halt)
            if clicks is None:
                self.say('the bite icon lit up (%d, needs %d) but no catch bar came: still waiting' % (
                    peak, cutoff(self.idle)))
                return not halt()
            landed = close_catch(halt)
            if halt():
                return False
            if not landed:
                self.rejoin = 'failed catch popup would not close'
                self.say('Fishing popup stuck: rejoining')
                return False
            self.stage = self.dry = 0
            if landed == 'failed':
                return self.foiled(halt, '(%d reel clicks)' % clicks)
            self.lost_run = self.lost_run + 1 if landed == 'lost' else 0
            if landed == 'lost' and self.lost_run >= LOST_RUN:
                if self.lost_run == LOST_RUN:
                    self.say('%d fish in a row with no catch window seen: it is not recognised here, so every reel '
                             'counts as a catch' % LOST_RUN)
                fclick('fishingMinigameResultCloseButton', 0.1)
                landed = 'caught'
            if landed == 'lost' and window_state() == 'line':
                self.say('no catch window and the line is still out (%d reel clicks): waiting for the next bite'
                         % clicks)
                return True                         # casting now would press Exit
            if landed == 'lost':
                self.say('the fish got away (%d reel clicks): not counted' % clicks)
                self.line_out = False
                return True if self.pause or self.hold else self.cast(halt)
            self.caught += 1
            self.since_sell += 1
            self.say('fish %d caught (%d reel clicks)%s' % (
                self.caught, clicks, ', %d of %d before selling' % (self.since_sell, self.sell_every)
                if self.sell_every else ''))
            if self.hold or (self.sell_every and self.since_sell >= self.sell_every):
                self.at_spot = self.line_out = False
                return True
            if self.pause:
                self.line_out = False
                self.say('fish landed: not casting until SC/BR are used')
                return True
            return self.cast(halt)
        if halt():
            return False
        st = window_state()
        if st == 'failed':
            fclick('fishingMinigameResultCloseButton', 0.1)
            return self.foiled(halt, 'before it was reeled')
        if st == 'catch':                       # the catch window came after close_catch gave up on it
            fclick('fishingMinigameResultCloseButton', 0.1)
            self.caught += 1
            self.since_sell += 1
            self.say('fish %d caught (its window came late): shut it, casting again' % self.caught)
            self.line_out = False
            return True if self.pause or self.hold else self.cast(halt)
        if st == 'fish' and nap(0.3, halt) and window_state() == 'fish':
            if self.pause or self.hold:
                self.line_out = False
                return True
            self.dry += 1
            if self.dry > DRY_CASTS:
                self.dry = 0
                return self.full_bag()
            self.say('the line is not out (Fish showing): casting again')
            return self.cast(halt)
        if time.monotonic() - self.cast_at < NO_BITE_S:
            return not halt()
        if self.pause or self.hold:
            self.say('no bite in %d s with %s waiting: reeling in' % (NO_BITE_S, 'SC/BR' if self.pause else 'a merchant'))
            return self.reel_in(halt)
        self.stage = (self.stage + 1) % 4
        self.say('bite icon: %d at the cast, %d at its brightest, a bite needs %d'
                 % (self.idle, peak, cutoff(self.idle)))
        self.failsafe({1: 'no bite in %d s: casting again' % NO_BITE_S, 3: 'no bite in %d s: casting again' % NO_BITE_S,
                       2: 'still no bite: walking back to the spot',
                       0: 'still no bite: a full bag looks like this - selling'}[self.stage])
        fclick('fishingMinigameResultCloseButton', 0.1)    # shut any leftover window first
        if self.stage in (1, 3):
            if not nap(1.0, halt):
                return False
            return self.cast(halt, 0.0 if self.zoomed else None)
        self.at_spot = False
        return True
