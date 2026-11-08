package main

// Test fixture: the same counter game as toy_game.py, written as an engine.go.
// Tests flip TOY_MODE to "bug" (wrong at 3) or "unknown" (an explicit unknown at 3).

const TOY_MODE = "good"

var UnknownBehaviors = func() []string {
	if TOY_MODE == "unknown" {
		return []string{"what vent does at pressure 3"}
	}
	return []string{}
}()

type toy struct{ players, n, turn int }

func vent(before int) int {
	if TOY_MODE == "bug" && before == 3 {
		return 2
	}
	if TOY_MODE == "unknown" && before == 3 {
		Unknown("vent at 3")
	}
	return 1
}

var DeckCounts = map[string]int{"vent": 21, "gear1": 21}

func NewGame(players int, deck []string) any { return &toy{players: players, turn: 1} }

func LegalActions(state any) []map[string]any {
	s := state.(*toy)
	if s.n >= 5 {
		return []map[string]any{}
	}
	return []map[string]any{{"type": "draw"}, {"type": "play", "card": "vent"}}
}

func Act(state any, action map[string]any) (any, []map[string]any) {
	s := state.(*toy)
	before := s.n
	if action["type"] == "draw" {
		s.n++
	} else {
		s.n -= vent(before)
	}
	s.turn++
	return s, []map[string]any{{"type": "pressure", "value": s.n}}
}

func View(state any) map[string]any {
	s := state.(*toy)
	alive := make([]bool, s.players)
	for i := range alive {
		alive[i] = true
	}
	var winner any
	if s.n >= 5 {
		winner = 0
	}
	return map[string]any{
		"players": s.players, "hands": [][]string{{}, {}}, "alive": alive, "discard": []string{},
		"draw_pile_size": 10, "current_seat": 0, "actions_left": 1, "pressure": s.n, "turn": s.turn,
		"over": s.n >= 5, "winner": winner,
	}
}
