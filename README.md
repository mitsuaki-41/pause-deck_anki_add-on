# pause-deck: Anki add-on

While adding more decks to study is fine at first, the number of cards requiring review grows over time, often leading to an increasingly heavy daily workload. By allowing decks to be paused even in the middle of a study session, I aim to make learning more casual and flexible.

This add-on is a fork of the following: https://ankiweb.net/shared/info/745191666

## Installation

1. Download or clone this repository.
2. Place the add-on folder `7451916661` in your Anki add-ons directory.
- Windows: C:\Users\<Username>\AppData\Roaming\Anki2\addons21\ (or %APPDATA%\Anki2\addons21\)
- Mac: ~/Library/Application Support/Anki2/addons21/
- Linux: ~/.local/share/Anki2/addons21/ (or via Flatpak at ~/.var/app/net.ankiweb.Anki/data/Anki2/addons21/)
3. Restart Anki.

## Features

Pause individual decks, including their subdecks.
- Defer existing scheduled cards while the deck is paused.
- Automatically catch up scheduling when Anki's scheduler day changes.
- Prevent new cards from becoming available.

## Usage

1. Open Anki.
2. Select the deck gear mark you want to pause.
3. Use the add-on's pause action. To resume normal scheduling, use the unpause action.

Paused decks can also be managed through the add-on's `Manage Paused Decks...` menu.

## How It Works

### Pause

When a deck is paused:

1. The deck and its subdecks are added to the paused list.
2. The `Today only` new-card limit is set to `0` for each deck.
3. Existing scheduled cards are shifted forward so that overdue cards do not accumulate during the pause. This postpones all review cards so that the due date for the oldest scheduled learning card becomes tomorrow.

### While Paused

When the scheduler day advances, the add-on shifts scheduled cards forward by the number of elapsed days. This prevents paused decks from accumulating due cards over time.  

Newly found subdecks are automatically included in the pause.

### Unpause

When a deck is unpaused:

1. The initial pause adjustment is reversed by one day, does not reconstruct every card's exact original due date. 
2. The temporary `Today only` new-card limit is removed.
3. The deck returns to normal scheduling. This enables you to resume your reviews whenever you are ready to start again.

## License
GNU AGPL v3
