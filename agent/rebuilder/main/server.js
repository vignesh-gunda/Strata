'use strict';
// JSON-lines contract server for a rebuilt engine. This file is NOT model-written: it only
// handles framing, game ids and error kinds, so the Verifier can drive the rebuild with the
// same client it uses for the original program.
//
//   {"id":1,"method":"act","params":{"game_id":"g-0001","action":{...}}}
//   {"id":1,"ok":true,"result":{...}}   or   {"id":1,"ok":false,"error":{"kind":...,"message":...}}
//
// engine.js must export: newGame(players, deck) -> state, legalActions(state) -> [action],
// act(state, action) -> {state, events}, view(state) -> view.  Optional: DECK_COUNTS, randomScript.

const path = require('path');
const readline = require('readline');

const engine = require(path.resolve(process.env.STRATA_ENGINE || path.join(__dirname, 'engine.js')));
const games = new Map();
let counter = 0;

class RequestError extends Error {
  constructor(kind, message) { super(message); this.kind = kind; }
}

function sameAction(a, b) {
  return a.type === b.type && (a.card || null) === (b.card || null);
}

function getGame(params) {
  const state = params && games.get(params.game_id);
  if (state === undefined) throw new RequestError('game_not_found', 'unknown game_id');
  return state;
}

// Deterministic shuffle of the observed deck composition. It does NOT reproduce the
// original program's shuffle (that algorithm is not observable); the Verifier always
// takes its scripts from the original.
function defaultRandomScript(seed) {
  if (!engine.DECK_COUNTS) throw new RequestError('invalid_request', 'random_script not supported');
  const deck = [];
  for (const [card, n] of Object.entries(engine.DECK_COUNTS)) for (let i = 0; i < n; i++) deck.push(card);
  let x = (Number(seed) >>> 0) || 1;
  const next = () => { x = (Math.imul(x, 1664525) + 1013904223) >>> 0; return x; };
  for (let i = deck.length - 1; i > 0; i--) {
    const j = next() % (i + 1);
    [deck[i], deck[j]] = [deck[j], deck[i]];
  }
  return { deck };
}

function handle(method, params) {
  params = params || {};
  switch (method) {
    case 'random_script':
      if (!Number.isInteger(params.seed)) throw new RequestError('invalid_request', 'seed must be an integer');
      return engine.randomScript ? engine.randomScript(params.seed) : defaultRandomScript(params.seed);
    case 'new_game': {
      const players = params.players;
      if (!Number.isInteger(players) || players < 2 || players > 4)
        throw new RequestError('invalid_request', 'players must be an integer from 2 to 4');
      const deck = params.script && params.script.deck;
      if (!Array.isArray(deck)) throw new RequestError('invalid_request', 'script.deck must be a list');
      let state;
      try { state = engine.newGame(players, deck.slice()); }
      catch (error) {
        if (error && error.kind === 'unknown_behavior') throw error;
        throw new RequestError('invalid_request', String(error && error.message));
      }
      const id = 'g-' + String(++counter).padStart(4, '0');
      games.set(id, state);
      return { game_id: id, view: engine.view(state) };
    }
    case 'legal_actions':
      return engine.legalActions(getGame(params));
    case 'view':
      return engine.view(getGame(params));
    case 'act': {
      const state = getGame(params);
      const action = params.action;
      if (!action || typeof action !== 'object' || typeof action.type !== 'string')
        throw new RequestError('invalid_request', 'action must be an object with a type');
      if (!engine.legalActions(state).some((a) => sameAction(a, action)))
        throw new RequestError('illegal_action', 'action is not legal right now');
      const result = engine.act(state, action);
      games.set(params.game_id, result.state);
      return { events: result.events, view: engine.view(result.state) };
    }
    default:
      throw new RequestError('invalid_request', 'unknown method ' + method);
  }
}

const rl = readline.createInterface({ input: process.stdin, terminal: false });
rl.on('line', (line) => {
  if (!line.trim()) return;
  let request;
  try { request = JSON.parse(line); }
  catch (e) { process.stdout.write(JSON.stringify({ id: null, ok: false, error: { kind: 'invalid_request', message: 'bad JSON' } }) + '\n'); return; }
  let reply;
  try {
    reply = { id: request.id, ok: true, result: handle(request.method, request.params) };
  } catch (error) {
    const kind = error && error.kind === 'unknown_behavior' ? 'internal_error'
      : (error && error.kind) || 'internal_error';
    reply = { id: request.id, ok: false, error: { kind, message: String(error && error.message) } };
  }
  process.stdout.write(JSON.stringify(reply) + '\n');
});
