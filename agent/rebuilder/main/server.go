// JSON-lines contract server for a rebuilt Go engine. This file is NOT model-written: it only
// handles framing, game ids and error kinds, so the Verifier can drive the rebuild with the
// same client it uses for the original program (the same contract as server.js).
//
// engine.go (package main, model-written) must define:
//
//	func NewGame(players int, deck []string) any
//	func LegalActions(state any) []map[string]any   // sorted by type, then card
//	func Act(state any, action map[string]any) (any, []map[string]any)   // new state, events
//	func View(state any) map[string]any
//	var DeckCounts map[string]int                   // optional
//	var UnknownBehaviors []string
//
// and may call Unknown("what") for behavior no confirmed rule covers.
package main

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
)

// UnknownError is what Unknown panics with.
type UnknownError struct{ Desc string }

func (e *UnknownError) Error() string { return "unknown behavior: " + e.Desc }

// Unknown marks behavior the confirmed rules do not establish. It never guesses.
func Unknown(desc string) { panic(&UnknownError{desc}) }

type requestError struct{ kind, message string }

func (e *requestError) Error() string { return e.message }

var (
	games   = map[string]any{}
	counter = 0
)

func sameAction(a, b map[string]any) bool {
	at, _ := a["type"].(string)
	bt, _ := b["type"].(string)
	ac, _ := a["card"].(string)
	bc, _ := b["card"].(string)
	return at == bt && ac == bc
}

func getGame(params map[string]any) any {
	id, _ := params["game_id"].(string)
	state, ok := games[id]
	if !ok {
		panic(&requestError{"game_not_found", "unknown game_id"})
	}
	return state
}

// Deterministic shuffle of the observed deck composition. It does NOT reproduce the original
// program's shuffle (that is not observable); the Verifier takes its scripts from the original.
func defaultRandomScript(seed uint32) map[string]any {
	if DeckCounts == nil {
		panic(&requestError{"invalid_request", "random_script not supported"})
	}
	names := make([]string, 0, len(DeckCounts))
	for name := range DeckCounts {
		names = append(names, name)
	}
	for i := 1; i < len(names); i++ { // stable order, whatever the map gave us
		for j := i; j > 0 && names[j] < names[j-1]; j-- {
			names[j], names[j-1] = names[j-1], names[j]
		}
	}
	deck := []string{}
	for _, name := range names {
		for i := 0; i < DeckCounts[name]; i++ {
			deck = append(deck, name)
		}
	}
	x := seed
	if x == 0 {
		x = 1
	}
	next := func() uint32 { x = x*1664525 + 1013904223; return x }
	for i := len(deck) - 1; i > 0; i-- {
		j := int(next() % uint32(i+1))
		deck[i], deck[j] = deck[j], deck[i]
	}
	return map[string]any{"deck": deck}
}

func asInt(v any) (int, bool) {
	f, ok := v.(float64)
	if !ok || f != float64(int(f)) {
		return 0, false
	}
	return int(f), true
}

func handle(method string, params map[string]any) any {
	switch method {
	case "random_script":
		seed, ok := asInt(params["seed"])
		if !ok {
			panic(&requestError{"invalid_request", "seed must be an integer"})
		}
		return defaultRandomScript(uint32(seed))
	case "new_game":
		players, ok := asInt(params["players"])
		if !ok || players < 2 || players > 4 {
			panic(&requestError{"invalid_request", "players must be an integer from 2 to 4"})
		}
		script, _ := params["script"].(map[string]any)
		raw, ok := script["deck"].([]any)
		if !ok {
			panic(&requestError{"invalid_request", "script.deck must be a list"})
		}
		deck := make([]string, len(raw))
		for i, c := range raw {
			deck[i], _ = c.(string)
		}
		state := NewGame(players, deck)
		counter++
		id := fmt.Sprintf("g-%04d", counter)
		games[id] = state
		return map[string]any{"game_id": id, "view": View(state)}
	case "legal_actions":
		return LegalActions(getGame(params))
	case "view":
		return View(getGame(params))
	case "act":
		state := getGame(params)
		action, ok := params["action"].(map[string]any)
		if _, typed := action["type"].(string); !ok || !typed {
			panic(&requestError{"invalid_request", "action must be an object with a type"})
		}
		legal := false
		for _, candidate := range LegalActions(state) {
			if sameAction(candidate, action) {
				legal = true
				break
			}
		}
		if !legal {
			panic(&requestError{"illegal_action", "action is not legal right now"})
		}
		next, events := Act(state, action)
		games[params["game_id"].(string)] = next
		if events == nil {
			events = []map[string]any{}
		}
		return map[string]any{"events": events, "view": View(next)}
	}
	panic(&requestError{"invalid_request", "unknown method " + method})
}

func reply(id any, params map[string]any, method string) (out map[string]any) {
	defer func() {
		if r := recover(); r != nil {
			kind, message := "internal_error", fmt.Sprint(r)
			if re, ok := r.(*requestError); ok {
				kind, message = re.kind, re.message
			} else if ue, ok := r.(*UnknownError); ok {
				message = ue.Error()
			}
			out = map[string]any{"id": id, "ok": false, "error": map[string]any{"kind": kind, "message": message}}
		}
	}()
	return map[string]any{"id": id, "ok": true, "result": handle(method, params)}
}

func main() {
	if len(os.Args) > 1 && os.Args[1] == "--unknown-behaviors" {
		list := UnknownBehaviors
		if list == nil {
			list = []string{}
		}
		out, _ := json.Marshal(list)
		fmt.Println(string(out))
		return
	}
	scanner := bufio.NewScanner(os.Stdin)
	scanner.Buffer(make([]byte, 1<<20), 1<<26)
	writer := bufio.NewWriter(os.Stdout)
	defer writer.Flush()
	for scanner.Scan() {
		line := scanner.Bytes()
		if len(line) == 0 {
			continue
		}
		var request struct {
			ID     any            `json:"id"`
			Method string         `json:"method"`
			Params map[string]any `json:"params"`
		}
		var out map[string]any
		if err := json.Unmarshal(line, &request); err != nil {
			out = map[string]any{"id": nil, "ok": false,
				"error": map[string]any{"kind": "invalid_request", "message": "bad JSON"}}
		} else {
			if request.Params == nil {
				request.Params = map[string]any{}
			}
			out = reply(request.ID, request.Params, request.Method)
		}
		encoded, _ := json.Marshal(out)
		writer.Write(encoded)
		writer.WriteByte('\n')
		writer.Flush()
	}
}
