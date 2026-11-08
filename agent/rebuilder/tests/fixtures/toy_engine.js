'use strict';
// Test fixture: the same counter game as verifier/tests/toy_game.py, written as an engine.js.
const BUG = process.env.TOY_BUG === '1';
function unknown(what) { throw Object.assign(new Error('unknown behavior: ' + what), { kind: 'unknown_behavior' }); }
exports.UNKNOWN_BEHAVIORS = [];
exports.DECK_COUNTS = { vent: 21, gear1: 21 };
exports.newGame = (players, deck) => ({ players, n: 0, turn: 1 });
exports.legalActions = (s) => (s.n >= 5 ? [] : [{ type: 'draw' }, { type: 'play', card: 'vent' }]);
exports.act = (s, action) => {
  const before = s.n;
  s.n += action.type === 'draw' ? 1 : (BUG && before === 3 ? -2 : -1);
  s.turn += 1;
  return { state: s, events: [{ type: 'pressure', value: s.n }] };
};
exports.view = (s) => ({
  players: s.players, hands: [[], []], alive: Array(s.players).fill(true), discard: [],
  draw_pile_size: 10, current_seat: 0, actions_left: 1, pressure: s.n, turn: s.turn,
  over: s.n >= 5, winner: s.n >= 5 ? 0 : null,
});
if (process.env.TOY_UNKNOWN === '1') {
  exports.UNKNOWN_BEHAVIORS = ['what vent does at pressure 3'];
  const act = exports.act;
  exports.act = (s, a) => { if (a.card === 'vent' && s.n === 3) unknown('vent at 3'); return act(s, a); };
}
