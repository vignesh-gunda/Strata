# Pressure: the true rules (sealed)

**Sealed. Never give this file to an agent, and never put its text in a prompt.**
It is the answer key for the final report. Agents get only the `dig-site/` folder.

## Base rules (2 to 4 players)

- **Setup:** deal 4 cards to each seat, round-robin from the top of the scripted deck. The rest is the draw pile. Shared pressure starts at 0.
- **Turn:** the current seat has 1 action. An action is one of:
  - play one card from hand
  - draw the top card, only if the pile is not empty and the hand holds fewer than 6 cards
  - pass, only when nothing else is legal

  When the seat runs out of actions, play passes to the next living seat in the current direction.
- **gear1, gear2, gear3:** add 1, 2 or 3 to pressure. If pressure reaches 12 or more, the player who played the card is eliminated. Their hand goes to the discard pile, pressure resets to 0, and the turn passes immediately.
- **vent:** lower pressure by 3, never below 0.
- **reverse:** flip the turn direction.
- **double:** the next seat to take a turn gets 2 actions instead of 1.
- **jam:** the next living seat in the current direction is jammed. A jammed seat's next card play is discarded with no effect, which clears the jam. Drawing is not affected.
- **scan:** reveal the top 2 cards of the draw pile to the player.
- **End:** there is always a winner. The last living seat wins. If the game reaches turn 200, or nobody has any cards left, the living seat holding the fewest cards wins (the lower seat number breaks a tie).

## Deck (42 cards)

| Card | Count |
|---|---|
| gear1 | 8 |
| gear2 | 8 |
| gear3 | 6 |
| vent | 6 |
| reverse | 4 |
| double | 4 |
| jam | 3 |
| scan | 3 |

## Hidden mutations

Each run turns on 0 to 6 of these, chosen by seed. They are defined in `blackbox/rules.py`.

| Mutation | Effect |
|---|---|
| `vent_four` | vent lowers pressure by 4 |
| `stacking_double` | each double adds one more action, instead of capping at 2 |
| `hot_restart` | after an overflow, pressure resets to 4 instead of 0 |
| `jam_blocks_draw` | a jam also wastes a draw: the draw does nothing and clears the jam |
| `reverse_skips` | reverse also skips the next seat |
| `short_scan` | scan reveals only 1 card |

## Hidden state

The view never shows: the draw pile's order, the turn direction, which seats are jammed, pending extra actions, the pressure limit, or the active mutations.
