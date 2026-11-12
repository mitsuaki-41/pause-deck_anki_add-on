"""
pause-deck - Anki add-on
"""

import copy
import json
import time
from typing import Optional

from anki import deck_config_pb2

from aqt import gui_hooks, mw
from aqt.operations import QueryOp
from aqt.qt import QAction, QMenu, QTimer, qconnect
from aqt.utils import showInfo, showWarning, tooltip, askUser


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def get_config() -> dict:
    conf = mw.addonManager.getConfig(__name__)
    if conf is None:
        conf = {"paused_decks": {}}
    if "paused_decks" not in conf:
        conf["paused_decks"] = {}
    return conf


def save_config(conf: dict) -> None:
    mw.addonManager.writeConfig(__name__, conf)


# ---------------------------------------------------------------------------
# Deck hierarchy helpers
# ---------------------------------------------------------------------------

def _get_deck_name(deck_id: int) -> Optional[str]:
    """Return the current name for a deck id, or None if deleted."""
    deck = mw.col.decks.get(deck_id, default=False)
    if deck:
        return deck["name"]
    return None


def _is_ancestor(ancestor_name: str, descendant_name: str) -> bool:
    """True if ancestor_name is a proper ancestor of descendant_name."""
    return descendant_name.startswith(ancestor_name + "::")


def is_deck_or_ancestor_paused(deck_id: int, paused_decks: dict) -> Optional[str]:
    """If the deck or any of its ancestors is paused, return that deck's name."""
    deck_name = _get_deck_name(deck_id)
    if deck_name is None:
        return None

    for pid, pinfo in paused_decks.items():
        paused_name = _get_deck_name(int(pid))
        if paused_name is None:
            continue
        # exact match
        if int(pid) == deck_id:
            return paused_name
        # paused deck is ancestor of target
        if _is_ancestor(paused_name, deck_name):
            return paused_name
    return None


def is_deck_or_descendant_paused(deck_id: int, paused_decks: dict) -> Optional[str]:
    """If the deck or any of its descendants is paused, return that deck's name."""
    deck_name = _get_deck_name(deck_id)
    if deck_name is None:
        return None

    for pid, pinfo in paused_decks.items():
        paused_name = _get_deck_name(int(pid))
        if paused_name is None:
            continue
        # exact match
        if int(pid) == deck_id:
            return paused_name
        # paused deck is descendant of target
        if _is_ancestor(deck_name, paused_name):
            return paused_name
    return None


def _has_hierarchy_conflict(deck_id: int, paused_decks: dict) -> Optional[str]:
    """Return conflicting deck name if pausing deck_id would conflict, else None."""
    deck_name = _get_deck_name(deck_id)
    if deck_name is None:
        return None

    for pid, pinfo in paused_decks.items():
        paused_name = _get_deck_name(int(pid))
        if paused_name is None:
            continue
        if int(pid) == deck_id:
            return paused_name
        if _is_ancestor(paused_name, deck_name):
            return paused_name
        if _is_ancestor(deck_name, paused_name):
            return paused_name
    return None


# ---------------------------------------------------------------------------
# Duration formatting
# ---------------------------------------------------------------------------

def _format_duration(seconds: float) -> str:
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h"
    minutes = int(seconds // 60)
    return f"{minutes}m" if minutes > 0 else "<1m"


# ---------------------------------------------------------------------------
# Scheduling helpers
# ---------------------------------------------------------------------------

QUEUE_NEW = 0
QUEUE_LEARNING = 1          # intraday learning/relearning; due is a timestamp
QUEUE_REVIEW = 2            # review; due is a scheduler day
QUEUE_DAY_LEARNING = 3      # interday learning/relearning; due is a scheduler day

# Suspended/buried cards have a negative queue, so their original due format is
# inferred from the stored due value. Unix timestamps are far larger than
# scheduler-day numbers.
TIMESTAMP_DUE_THRESHOLD = 100_000_000


def _deck_tree_ids(col, deck_id: int) -> list[int]:
    """Return the selected deck and all descendants, excluding filtered decks."""
    deck = col.decks.get(deck_id, default=False)
    if not deck or deck.get("dyn"):
        return []
    return [int(did) for did in col.decks.deck_and_child_ids(deck_id)
            if not col.decks.is_filtered(did)]


def _set_new_today_limit(col, deck_id: int, value: Optional[int]) -> None:
    """Set the temporary Today only new-card limit for one deck.

    Passing None disables the temporary override, making the This deck value
    (or the preset value when no deck override exists) effective again.
    """
    configs = col.decks.get_deck_configs_for_update(deck_id)

    request = deck_config_pb2.UpdateDeckConfigsRequest()
    request.target_deck_id = int(deck_id)

    # Preserve the existing deck configuration presets.
    for entry in configs.all_config:
        request.configs.add().CopyFrom(entry.config)

    request.limits.CopyFrom(configs.current_deck.limits)

    if value is None:
        request.limits.ClearField("new_today")
        request.limits.new_today_active = False
    else:
        request.limits.new_today = max(0, int(value))
        request.limits.new_today_active = True

    col.decks.update_deck_configs(request)


def _today_new_limit_is_zero(col, deck_id: int) -> bool:
    limits = col.decks.get_deck_configs_for_update(deck_id).current_deck.limits
    return (
        bool(limits.new_today_active)
        and limits.HasField("new_today")
        and int(limits.new_today) == 0
    )


def _get_legacy_new_limit_state(col, deck_id: int) -> dict:
    """Read a This deck override saved by older versions of this add-on."""
    limits = col.decks.get_deck_configs_for_update(deck_id).current_deck.limits
    present = limits.HasField("new")
    return {"present": bool(present), "value": int(limits.new) if present else 0}


def _set_legacy_new_limit(col, deck_id: int, value: Optional[int]) -> None:
    """Restore a This deck override left by an older add-on version."""
    configs = col.decks.get_deck_configs_for_update(deck_id)

    request = deck_config_pb2.UpdateDeckConfigsRequest()
    request.target_deck_id = int(deck_id)

    # Preserve the existing deck configuration presets.
    for entry in configs.all_config:
        request.configs.add().CopyFrom(entry.config)

    request.limits.CopyFrom(configs.current_deck.limits)
    
    if value is None:
        request.limits.ClearField("new")
    else:
        request.limits.new = max(0, int(value))
    col.decks.update_deck_configs(request)


def _restore_legacy_new_limits(col, saved_limits: dict) -> None:
    for did_str, state in saved_limits.items():
        did = int(did_str)
        if not col.decks.get(did, default=False):
            continue
        _set_legacy_new_limit(
            col, did,
            int(state["value"]) if state.get("present") else None,
        )


def _shift_due_dates(col, deck_ids: list[int], elapsed_days: int) -> int:
    if elapsed_days == 0 or not deck_ids:
        return 0

    from anki.utils import ids2str

    ids_sql = ids2str(deck_ids)
    card_ids = col.db.list(
        f"select id from cards where did in {ids_sql} "
        "and queue in (1, 2, 3, -1, -2, -3) and type != 0"
    )

    adjusted = 0
    seconds_delta = elapsed_days * 86400

    print(
        f"SHIFT START: deck_ids={deck_ids}, "
        f"elapsed_days={elapsed_days}, "
        f"seconds_delta={seconds_delta}, "
        f"cards_found={len(card_ids)}"
    )

    for cid in card_ids:
        card = col.get_card(cid)
        old_due = int(card.due)
        old_queue = card.queue

        if card.queue == QUEUE_LEARNING:
            from datetime import datetime, timedelta

            shifted = datetime.fromtimestamp(card.due) + timedelta(days=elapsed_days)
            card.due = int(
                shifted.replace(
                    hour=0,
                    minute=0,
                    second=0,
                    microsecond=0,
                ).timestamp()
            )
        
        elif card.queue in (QUEUE_REVIEW, QUEUE_DAY_LEARNING):
            card.due += elapsed_days
        elif card.queue in (-1, -2, -3):
            if int(card.due) >= TIMESTAMP_DUE_THRESHOLD:
                card.due += seconds_delta
            else:
                card.due += elapsed_days
        else:
            continue

        print(
            f"BEFORE SAVE: id={cid}, queue={old_queue}, "
            f"old_due={old_due}, new_due={card.due}"
        )

        col.update_card(card)

        saved_due = col.db.scalar(
            "select due from cards where id = ?", cid
        )

        print(
            f"AFTER SAVE: id={cid}, "
            f"expected_due={card.due}, saved_due={saved_due}"
        )

        adjusted += 1

    print(f"SHIFT END: adjusted={adjusted}")

    return adjusted

def _get_initial_pause_shift_days(col, deck_ids: list[int], today: int) -> int:
    """Move the oldest scheduled card to tomorrow."""
    from anki.utils import ids2str

    if not deck_ids:
        return 1

    ids_sql = ids2str(deck_ids)

    # Find the oldest card using scheduler-day due values.
    oldest_day_due = col.db.scalar(
        f"select min(due) from cards "
        f"where did in {ids_sql} "
        f"and type != 0 "
        f"and queue in (2, 3, -1, -2, -3) "
        f"and due < {TIMESTAMP_DUE_THRESHOLD}"
    )

    # Find intraday learning cards (queue=1).
    intraday_dues = col.db.list(
        f"select due from cards "
        f"where did in {ids_sql} "
        f"and type != 0 "
        f"and queue = 1"
    )

    oldest_due_days = []

    if oldest_day_due is not None:
        oldest_due_days.append(int(oldest_day_due))

    # Convert Unix timestamps to scheduler-day values.
    today_start = int(col.sched.day_cutoff) - 86400

    for due in intraday_dues:
        due_day = today + int((int(due) - today_start) // 86400)
        oldest_due_days.append(due_day)

    if not oldest_due_days:
        return 1

    oldest_due = min(oldest_due_days)

    return max(1, today - oldest_due + 1)

def _pause_day_from_timestamp(col, pause_timestamp: float) -> int:
    """Estimate the scheduler day containing a timestamp using today's cutoff."""
    today = int(col.sched.today)
    today_start = int(col.sched.day_cutoff) - 86400
    return today + int((pause_timestamp - today_start) // 86400)


# ---------------------------------------------------------------------------
# Pause deck
# ---------------------------------------------------------------------------

def pause_deck(deck_id: int) -> None:
    global _collection_op_running
    conf = get_config()
    paused = conf["paused_decks"]
    deck_id_str = str(deck_id)

    if deck_id_str in paused:
        tooltip("This deck is already paused.")
        return

    conflict = _has_hierarchy_conflict(deck_id, paused)
    if conflict:
        showWarning(
            f"Cannot pause this deck because a related deck "
            f"is already paused: \"{conflict}\".\n\n"
            f"Unpause it first to avoid conflicts."
        )
        return

    deck_name = _get_deck_name(deck_id)
    if deck_name is None:
        showWarning("Deck not found.")
        return
    if _collection_op_running:
        tooltip("Another Pause Collection operation is still running.")
        return

    _collection_op_running = True
    pause_start = time.time()

    def do_pause(col) -> dict:
        deck_ids = _deck_tree_ids(col, deck_id)
        if not deck_ids:
            raise RuntimeError("Deck not found or is a filtered deck.")

        today = int(col.sched.today)
        limits_changed = []
        shifted = 0
        try:
            # Use Today only so the deck's persistent This deck setting is not
            # overwritten. The temporary override is cleared on Unpause.
            for did in deck_ids:
                _set_new_today_limit(col, did, 0)
                limits_changed.append(did)

            # Calculate the shift required to move the oldest card to tomorrow.
            pause_shift_days = _get_initial_pause_shift_days(
                col, deck_ids, today
            )

            # Shift all scheduled cards by the calculated number of days.
            shifted = _shift_due_dates(
                col, deck_ids, pause_shift_days
            )

        except Exception:
            # Best-effort rollback if setup fails part-way through.
            try:
                if shifted:
                    _shift_due_dates(col, deck_ids, -pause_shift_days)
                for did in limits_changed:
                    _set_new_today_limit(col, did, None)
            except Exception:
                pass
            raise

        return {
            "pause_start": pause_start,
            "last_shift_day": today,
            "deck_name": deck_name,
            "deck_ids": deck_ids,
            "deck_days": {str(did): today for did in deck_ids},
            "pause_shift_applied": True,
            "cards_shifted_at_pause": shifted,
            "new_limits": {},
        }

    def on_success(info: dict) -> None:
        global _collection_op_running
        _collection_op_running = False
        conf2 = get_config()
        conf2["paused_decks"][deck_id_str] = info
        save_config(conf2)
        tooltip(f"Paused deck: {deck_name}")
        if mw.deckBrowser:
            mw.deckBrowser.refresh()

    def on_failure(exc) -> None:
        global _collection_op_running
        _collection_op_running = False
        showWarning(f"Could not pause deck:\n{exc}")

    QueryOp(parent=mw, op=do_pause, success=on_success).failure(on_failure).with_progress(
        f"Pausing \"{deck_name}\"..."
    ).run_in_background()


# ---------------------------------------------------------------------------
# Deck browser visual indicator
# ---------------------------------------------------------------------------

def on_deck_browser_will_render(deck_browser, content) -> None:
    """Inject CSS + JS into stats section to show pause badges."""
    conf = get_config()
    paused = conf["paused_decks"]
    if not paused:
        return

    paused_info = {}
    for did_str, info in paused.items():
        name = _get_deck_name(int(did_str)) or info.get("deck_name", "")
        elapsed = time.time() - info["pause_start"]
        paused_info[did_str] = {
            "name": name,
            "display": name.split("::")[-1] if name else "",
            "duration": _format_duration(elapsed),
        }

    js_data = json.dumps(paused_info)

    snippet = f"""
<style>
a.deck {{
    min-width: 0 !important;
    width: auto !important;
}}
.pause-badge {{
    display: inline-block;
    background-color: #7e22e6;
    color: white;
    font-size: 0.75em;
    padding: 2px 7px;
    border-radius: 3px;
    margin-left: 8px;
    vertical-align: baseline;
    font-weight: normal;
}}
</style>
<script>
(function() {{
    var pausedDecks = {js_data};
    var done = false;

    function addBadges() {{
        if (done) return;
        document.querySelectorAll('.pause-badge').forEach(function(el) {{ el.remove(); }});
        var found = 0;

        for (var deckId in pausedDecks) {{
            var info = pausedDecks[deckId];
            var target = null;

            // Strategy 1: <tr id="deckId">
            var row = document.getElementById(deckId);
            if (row) target = row.querySelector('a.deck') || row.querySelector('td');

            // Strategy 2: onclick containing the deck ID
            if (!target) {{
                var el = document.querySelector('[onclick*="' + deckId + '"]');
                if (el) target = el;
            }}

            // Strategy 3: data attributes
            if (!target) {{
                var el = document.querySelector('[data-id="' + deckId + '"]');
                if (el) target = el.querySelector('a, span') || el;
            }}

            // Strategy 4: match by display name text
            if (!target && info.display) {{
                var links = document.querySelectorAll('a, td span, span');
                for (var i = 0; i < links.length; i++) {{
                    var txt = links[i].childNodes;
                    var direct = '';
                    for (var j = 0; j < txt.length; j++) {{
                        if (txt[j].nodeType === 3) direct += txt[j].textContent;
                    }}
                    if (direct.trim() === info.display) {{
                        target = links[i];
                        break;
                    }}
                }}
            }}

            if (target && !target.parentNode.querySelector('.pause-badge')) {{
                var badge = document.createElement('span');
                badge.className = 'pause-badge';
                badge.textContent = '\\u23f8 ' + info.duration;
                target.after(badge);
                found++;
            }}
        }}

        if (found > 0) done = true;
    }}

    setTimeout(addBadges, 200);
    setTimeout(addBadges, 800);
    setTimeout(addBadges, 2000);
}})();
</script>
"""
    # Try stats first, fall back to tree
    if hasattr(content, 'stats'):
        content.stats += snippet
    elif hasattr(content, 'tree'):
        content.tree += snippet


# ---------------------------------------------------------------------------
# Unpause deck — apply any pending day shifts, then restore new-card limits
# ---------------------------------------------------------------------------

def unpause_deck(deck_id: int) -> None:
    global _collection_op_running
    conf = get_config()
    paused = conf["paused_decks"]
    deck_id_str = str(deck_id)

    if deck_id_str not in paused:
        tooltip("This deck is not paused.")
        return
    if _collection_op_running:
        tooltip("Another Pause Collection operation is still running.")
        return

    info = copy.deepcopy(paused[deck_id_str])
    pause_start = float(info["pause_start"])
    pause_duration_secs = max(0.0, time.time() - pause_start)
    duration_str = _format_duration(pause_duration_secs)
    deck_name = _get_deck_name(deck_id) or info.get("deck_name", "Unknown")

    if not askUser(
        f"Unpause deck \"{deck_name}\"?\n\n"
        f"Paused for: {duration_str}\n"
        f"Due dates will be shifted for any missed scheduler day(s), then moved "
        f"back by one day.\n"
        f"The temporary Today only new-card limit will be cleared.\n\n"
        f"It is recommended to create a backup before proceeding.",
        title="Unpause Deck",
    ):
        return

    _collection_op_running = True

    def do_unpause(col) -> dict:
        deck_ids = _deck_tree_ids(col, deck_id)
        today = int(col.sched.today)
        fallback_day = int(info.get("last_shift_day", _pause_day_from_timestamp(col, pause_start)))
        deck_days = info.get("deck_days", {})
        adjusted_forward = 0
        total_pending_days = 0

        # Catch up scheduler-day rollovers that occurred while Anki was closed
        # or while the day-change hook was missed.
        for did in deck_ids:
            last_day = int(deck_days.get(str(did), fallback_day))
            pending_days = max(0, today - last_day)
            adjusted_forward += _shift_due_dates(col, [did], pending_days)
            total_pending_days += pending_days

        # Pause activation itself always applied +1 day in current versions.
        # Reverse exactly that one day, regardless of the pause duration.
        reversed_count = 0
        if info.get("pause_shift_applied", False):
            reversed_count = _shift_due_dates(col, deck_ids, -1)

        # Clear Today only and fall back to the existing This deck/preset limit.
        for did in deck_ids:
            _set_new_today_limit(col, did, None)

        # If this entry was created by an older add-on version, it may have
        # changed the persistent This deck limit to zero. Restore that saved
        # value before removing the old state.
        legacy_limits = info.get("new_limits", {})
        if legacy_limits:
            _restore_legacy_new_limits(col, legacy_limits)

        return {
            "adjusted": adjusted_forward + reversed_count,
            "forward_adjusted": adjusted_forward,
            "reversed": reversed_count,
            "pending_days": total_pending_days,
        }

    def on_success(result: dict) -> None:
        global _collection_op_running
        _collection_op_running = False

        conf2 = get_config()
        conf2["paused_decks"].pop(deck_id_str, None)

        save_config(conf2)

        tooltip(
            f"Unpaused: {deck_name}, "
            f"{result['reversed']} cards restored, "
            f"{result['pending_days']} pending days processed.",
            period=6000,
        )

        if mw.deckBrowser:
            mw.deckBrowser.refresh()

    def on_failure(exc) -> None:
        global _collection_op_running
        _collection_op_running = False
        showWarning(f"Could not unpause deck:\n{exc}")

    QueryOp(parent=mw, op=do_unpause, success=on_success).failure(on_failure).with_progress(
        f"Unpausing \"{deck_name}\"..."
    ).run_in_background()


# ---------------------------------------------------------------------------
# Deck browser context menu (gear icon)
# ---------------------------------------------------------------------------

def on_deck_options_menu(menu: QMenu, deck_id: int) -> None:
    conf = get_config()
    paused = conf["paused_decks"]
    deck_id_str = str(deck_id)

    if deck_id_str in paused:
        info = paused[deck_id_str]
        elapsed = time.time() - info["pause_start"]
        duration_str = _format_duration(elapsed)
        action = menu.addAction(f"Unpause Deck (paused {duration_str})")
        qconnect(action.triggered, lambda: unpause_deck(deck_id))
    else:
        action = menu.addAction("Pause Deck")
        qconnect(action.triggered, lambda: pause_deck(deck_id))




# ---------------------------------------------------------------------------
# Tools menu — Manage Paused Decks dialog
# ---------------------------------------------------------------------------

def _show_manage_dialog() -> None:
    from aqt.qt import (
        QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
        QScrollArea, QWidget, Qt,
    )

    conf = get_config()
    paused = conf["paused_decks"]

    if not paused:
        showInfo("No decks are currently paused.")
        return

    dlg = QDialog(mw)
    dlg.setWindowTitle("Manage Paused Decks")
    dlg.setMinimumWidth(420)

    layout = QVBoxLayout(dlg)

    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    container = QWidget()
    container_layout = QVBoxLayout(container)

    for did_str, info in list(paused.items()):
        deck_id = int(did_str)
        elapsed = time.time() - info["pause_start"]
        name = _get_deck_name(deck_id) or info.get("deck_name", "Unknown")

        row = QHBoxLayout()
        label = QLabel(f"<b>{name}</b> — paused for {_format_duration(elapsed)}")
        label.setWordWrap(True)
        row.addWidget(label, stretch=1)

        btn = QPushButton("Unpause")
        # Capture deck_id and dlg by value
        def make_unpause(did=deck_id, d=dlg):
            def handler():
                d.accept()
                unpause_deck(did)
            return handler
        qconnect(btn.clicked, make_unpause())
        row.addWidget(btn)

        container_layout.addLayout(row)

    container_layout.addStretch()
    scroll.setWidget(container)
    layout.addWidget(scroll)

    close_btn = QPushButton("Close")
    qconnect(close_btn.clicked, dlg.reject)
    layout.addWidget(close_btn, alignment=Qt.AlignmentFlag.AlignRight)

    dlg.exec()


_tools_menu_added = False
_collection_op_running = False
_rollover_timer = None


def _sync_paused_decks() -> None:
    """Catch up paused decks to the current scheduler day and enforce Today only=0."""
    global _collection_op_running
    if mw.col is None or _collection_op_running:
        return

    conf = get_config()
    paused = conf["paused_decks"]
    if not paused:
        return

    today = int(mw.col.sched.today)
    needs_sync = False

    for did_str, info in paused.items():
        root_id = int(did_str)
        current_ids = _deck_tree_ids(mw.col, root_id)
        deck_days = info.get("deck_days", {})
        legacy_limits = info.get("new_limits", {})

        if not deck_days or legacy_limits:
            needs_sync = True
            break

        for did in current_ids:
            key = str(did)
            if key not in deck_days or int(deck_days.get(key, today)) < today:
                needs_sync = True
                break
            if not _today_new_limit_is_zero(mw.col, did):
                needs_sync = True
                break
        if needs_sync:
            break

    if not needs_sync:
        return

    snapshot = copy.deepcopy(paused)
    _collection_op_running = True

    def do_sync(col) -> dict:
        results = {}
        current_today = int(col.sched.today)

        for did_str, info in snapshot.items():
            root_id = int(did_str)
            if not col.decks.get(root_id, default=False):
                continue

            deck_ids = _deck_tree_ids(col, root_id)
            deck_days = copy.deepcopy(info.get("deck_days", {}))
            legacy_limits = copy.deepcopy(info.get("new_limits", {}))

            # Migrate old entries that changed This deck to zero. Restore the
            # original persistent override, then use Today only from now on.
            if legacy_limits:
                _restore_legacy_new_limits(col, legacy_limits)

            adjusted = 0
            shifted_days = 0

            for child_id in deck_ids:
                key = str(child_id)
                is_new_descendant = key not in deck_days

                _set_new_today_limit(col, child_id, 0)

                if is_new_descendant:
                    # A newly discovered subdeck joins the pause now.
                    child_shift_days = _get_initial_pause_shift_days(
                        col, [child_id], current_today
                    )

                    adjusted += _shift_due_dates(
                        col, [child_id], child_shift_days
                    )

                    deck_days[key] = current_today
                    continue

                last_day = int(deck_days.get(key, current_today))
                elapsed_days = max(0, current_today - last_day)

                adjusted += _shift_due_dates(
                    col, [child_id], elapsed_days
                )

                shifted_days += elapsed_days
                deck_days[key] = current_today
                continue

                last_day = int(deck_days.get(key, current_today))
                elapsed_days = max(0, current_today - last_day)
                adjusted += _shift_due_dates(col, [child_id], elapsed_days)
                shifted_days += elapsed_days
                deck_days[key] = current_today

            results[did_str] = {
                "last_shift_day": current_today,
                "deck_ids": deck_ids,
                "deck_days": deck_days,
                "new_limits": {},
                "pause_shift_applied": bool(info.get("pause_shift_applied", False)),
                "cards_shifted_at_sync": adjusted,
                "scheduler_days_shifted_at_sync": shifted_days,
            }

        return results

    def on_success(results: dict) -> None:
        global _collection_op_running
        conf2 = get_config()
        for did_str, result in results.items():
            if did_str in conf2["paused_decks"]:
                conf2["paused_decks"][did_str].update(result)
        save_config(conf2)
        _collection_op_running = False
        if results and mw.deckBrowser:
            mw.deckBrowser.refresh()

    def on_failure(exc) -> None:
        global _collection_op_running
        _collection_op_running = False
        showWarning(f"pause-deck could not update scheduling:\n{exc}")

    QueryOp(parent=mw, op=do_sync, success=on_success).failure(on_failure).run_in_background()


def _on_day_did_change() -> None:
    """Anki scheduler-day event: shift paused decks once for elapsed days."""
    _sync_paused_decks()


def _schedule_rollover_timer() -> None:
    """Schedule one wake-up at the next cutoff as a fallback for missed hooks.

    This is a single-shot timer, not periodic polling. It is useful in Anki
    versions where day_did_change may be skipped while the reviewer is open.
    """
    global _rollover_timer
    if mw.col is None or not get_config()["paused_decks"]:
        if _rollover_timer is not None:
            _rollover_timer.stop()
        return

    if _rollover_timer is None:
        _rollover_timer = QTimer(mw)
        _rollover_timer.setSingleShot(True)
        qconnect(_rollover_timer.timeout, _on_rollover_timer)

    cutoff = float(mw.col.sched.day_cutoff)
    delay_ms = max(1000, int((cutoff - time.time()) * 1000) + 500)
    _rollover_timer.start(delay_ms)


def _on_rollover_timer() -> None:
    _sync_paused_decks()
    _schedule_rollover_timer()
    
def _on_focus_did_change(new, old) -> None:
    if new is None:
        return

    _sync_paused_decks()
    _schedule_rollover_timer()

def _on_operation_did_execute(changes, handler) -> None:
    if (
        getattr(changes, "deck", False)
        or getattr(changes, "config", False)
    ):
        _sync_paused_decks()

    if getattr(changes, "config", False):
        _schedule_rollover_timer()


def setup_tools_menu() -> None:
    global _tools_menu_added
    if not _tools_menu_added:
        _tools_menu_added = True
        action = QAction("Manage Paused Decks...", mw)
        qconnect(action.triggered, _show_manage_dialog)
        mw.form.menuTools.addAction(action)

    # Catch up after profile open, then wait for the next scheduler cutoff.
    _sync_paused_decks()
    _schedule_rollover_timer()


# ---------------------------------------------------------------------------
# Hook registration
# ---------------------------------------------------------------------------

gui_hooks.deck_browser_will_show_options_menu.append(on_deck_options_menu)
gui_hooks.deck_browser_will_render_content.append(on_deck_browser_will_render)
gui_hooks.profile_did_open.append(setup_tools_menu)
gui_hooks.day_did_change.append(_on_day_did_change)
gui_hooks.operation_did_execute.append(_on_operation_did_execute)
gui_hooks.focus_did_change.append(_on_focus_did_change)
