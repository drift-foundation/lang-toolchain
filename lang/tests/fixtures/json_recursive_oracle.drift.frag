// ─────────────────────────────────────────────────────────────────────
// TEST ORACLE — the PRESERVED RECURSIVE parser (frozen 2026-07-27).
//
// The DURABLE differential-parity + performance-baseline ORACLE for the
// iterative std.json parser.  Kept OUT of the production stdlib (it is
// appended only to a throwaway stdlib copy by
// `lang/tests/driver/_json_oracle_stdlib.py`), so it never ships and never
// contributes to the ownership corpus.  NOT for production use: it recurses
// one native frame per nesting level (the DoS this rewrite closed), so the
// differential exercises it only on bounded depths.
//
// This fragment is a FROZEN baseline: its content hash is pinned by
// `test_std_json_recursive_iterative_differential.py::test_oracle_fragment_is_pinned`.
// The comparative gate needs a stable reference, so this file is retained
// (not "scheduled for removal") — any edit MUST re-pin the hash and be
// justified as a deliberate baseline change.  Every function here is a
// verbatim copy of the former recursive parser with a `_rec` suffix.
// ─────────────────────────────────────────────────────────────────────

fn _parse_array_rec(text: &String, idx: &mut Int, ctx: &_ParseCtx, depth: Int, sp: &mut Optional<_SpanTree>) nothrow -> core.Result<JsonNode, JsonErrorData> {
	val n = text.byte_length();
	val arr_start = *idx;
	if *idx >= n or core.string_byte_at(text, *idx) != cast<Byte>(91) {
		return core.Result::Err(_err_parse(text, "invalid-syntax", *idx));
	}
	if _over_limit(ctx.cfg.limits.max_depth, depth) {
		return core.Result::Err(_err_parse(text, "limit-depth", arr_start));
	}
	*idx = *idx + 1;
	_skip_ws(text, idx);
	var values: Array<JsonNode> = [];
	var items: Array<_SpanTree> = [];
	if *idx < n and core.string_byte_at(text, *idx) == cast<Byte>(93) {
		*idx = *idx + 1;
		if ctx.locate {
			*sp = Optional::Some(_SpanTree::Arr(span = JsonByteSpan(start = arr_start, end = *idx), items = move items));
		}
		return core.Result::Ok(JsonNode::Array(move values));
	}
	while true {
		val elem_start = *idx;
		// Enforce the item limit BEFORE consuming the (max+1)th element.
		if _over_limit(ctx.cfg.limits.max_array_items, values.len + 1) {
			return core.Result::Err(_err_parse(text, "limit-array-items", elem_start));
		}
		var child_sp: Optional<_SpanTree> = Optional::None();
		match _parse_value_rec(text, idx, ctx, depth + 1, child_sp) {
			core.Result::Err(e) => { return core.Result::Err(move e); },
			core.Result::Ok(v) => { values.push(move v); }
		}
		if ctx.locate {
			match child_sp {
				Some(c) => { items.push(move c); },
				None => { }
			}
		}
		_skip_ws(text, idx);
		if *idx >= n {
			return core.Result::Err(_err_parse(text, "invalid-syntax", *idx));
		}
		val b = core.string_byte_at(text, *idx);
		if b == cast<Byte>(44) {
			*idx = *idx + 1;
			_skip_ws(text, idx);
			continue;
		}
		if b == cast<Byte>(93) {
			*idx = *idx + 1;
			if ctx.locate {
				*sp = Optional::Some(_SpanTree::Arr(span = JsonByteSpan(start = arr_start, end = *idx), items = move items));
			}
			return core.Result::Ok(JsonNode::Array(move values));
		}
		return core.Result::Err(_err_parse(text, "invalid-syntax", *idx));
	}
	return core.Result::Err(_err_parse(text, "invalid-syntax", *idx));
}

fn _parse_object_throwing_rec(text: &String, idx: &mut Int, ctx: &_ParseCtx, depth: Int, sp: &mut Optional<_SpanTree>) -> core.Result<JsonNode, JsonErrorData> {
	val n = text.byte_length();
	val obj_start = *idx;
	if *idx >= n or core.string_byte_at(text, *idx) != cast<Byte>(123) {
		return core.Result::Err(_err_parse(text, "invalid-syntax", *idx));
	}
	if _over_limit(ctx.cfg.limits.max_depth, depth) {
		return core.Result::Err(_err_parse(text, "limit-depth", obj_start));
	}
	*idx = *idx + 1;
	_skip_ws(text, idx);
	var fields = containers.hash_map<type String, std.json.JsonNode>();
	// `max_object_fields` counts member OCCURRENCES (incl. duplicates), not the
	// unique map size — so a flood of repeated keys can't bypass the limit.
	var member_count = 0;
	// Located sidecar (populated only when ctx.locate): every key occurrence in
	// source order, plus the surviving value span per key (mirrors `fields`).
	var occurrences: Array<_KeyOccurrence> = [];
	var vspans = containers.hash_map<type String, _SpanTree>();
	if *idx < n and core.string_byte_at(text, *idx) == cast<Byte>(125) {
		*idx = *idx + 1;
		if ctx.locate {
			*sp = Optional::Some(_SpanTree::Obj(span = JsonByteSpan(start = obj_start, end = *idx), occurrences = move occurrences, values = move vspans));
		}
		return core.Result::Ok(JsonNode::Object(move fields));
	}
	while true {
		val key_start = *idx;
		var key = "";
		match _parse_string(text, idx, ctx) {
			core.Result::Err(e) => { return core.Result::Err(move e); },
			core.Result::Ok(v) => { key = v; }
		}
		val key_end = *idx;
		if ctx.locate {
			occurrences.push(_KeyOccurrence(key = key.clone(), key_span = JsonByteSpan(start = key_start, end = key_end)));
		}
		// Reject: fail at the SECOND occurrence of a key, before its value is
		// parsed (one contains_key probe; immediate failure at `key_start`).
		match ctx.cfg.duplicate_keys {
			DuplicateKeyPolicy::Reject() => {
				if fields.contains_key(key) {
					return core.Result::Err(_err_parse_key(text, "duplicate-key", key_start, move key));
				}
			},
			default => { }
		}
		// Enforce the field-occurrence limit BEFORE consuming this member's value.
		member_count = member_count + 1;
		if _over_limit(ctx.cfg.limits.max_object_fields, member_count) {
			return core.Result::Err(_err_parse(text, "limit-object-fields", key_start));
		}
		_skip_ws(text, idx);
		if *idx >= n or core.string_byte_at(text, *idx) != cast<Byte>(58) {
			return core.Result::Err(_err_parse(text, "invalid-syntax", *idx));
		}
		*idx = *idx + 1;
		_skip_ws(text, idx);
		var child_sp: Optional<_SpanTree> = Optional::None();
		match _parse_value_rec(text, idx, ctx, depth + 1, child_sp) {
			core.Result::Err(e) => { return core.Result::Err(move e); },
			core.Result::Ok(v) => {
				match ctx.cfg.duplicate_keys {
					DuplicateKeyPolicy::KeepFirst() => {
						// one probe: stores when absent, drops key+value when present.
						// The value-span map mirrors `fields` under the same policy.
						if ctx.locate {
							match child_sp {
								Some(c) => { vspans.insert_if_absent(key.clone(), move c); },
								None => { }
							}
						}
						fields.insert_if_absent(move key, move v);
					},
					default => {
						// Reject (unique here) and KeepLast both insert; KeepLast's
						// overwrite IS keep-last (the returned old value is dropped).
						if ctx.locate {
							match child_sp {
								Some(c) => { vspans.insert(key.clone(), move c); },
								None => { }
							}
						}
						fields.insert(move key, move v);
					}
				}
			}
		}
		_skip_ws(text, idx);
		if *idx >= n {
			return core.Result::Err(_err_parse(text, "invalid-syntax", *idx));
		}
		val b = core.string_byte_at(text, *idx);
		if b == cast<Byte>(44) {
			*idx = *idx + 1;
			_skip_ws(text, idx);
			continue;
		}
		if b == cast<Byte>(125) {
			*idx = *idx + 1;
			if ctx.locate {
				*sp = Optional::Some(_SpanTree::Obj(span = JsonByteSpan(start = obj_start, end = *idx), occurrences = move occurrences, values = move vspans));
			}
			return core.Result::Ok(JsonNode::Object(move fields));
		}
		return core.Result::Err(_err_parse(text, "invalid-syntax", *idx));
	}
	return core.Result::Err(_err_parse(text, "invalid-syntax", *idx));
}

fn _parse_object_rec(text: &String, idx: &mut Int, ctx: &_ParseCtx, depth: Int, sp: &mut Optional<_SpanTree>) nothrow -> core.Result<JsonNode, JsonErrorData> {
	return try _parse_object_throwing_rec(text, idx, ctx, depth, sp) catch { core.Result::Err(_err_parse(text, "internal-error", *idx)) };
}

fn _parse_value_rec(text: &String, idx: &mut Int, ctx: &_ParseCtx, depth: Int, sp: &mut Optional<_SpanTree>) nothrow -> core.Result<JsonNode, JsonErrorData> {
	val n = text.byte_length();
	if *idx >= n {
		return core.Result::Err(_err_parse(text, "invalid-syntax", *idx));
	}
	val start = *idx;
	val b = core.string_byte_at(text, *idx);
	if b == cast<Byte>(110) {
		match _parse_literal(text, idx, "null", JsonNode::Null()) {
			core.Result::Err(e) => { return core.Result::Err(move e); },
			core.Result::Ok(node) => { _set_leaf(ctx, sp, start, *idx); return core.Result::Ok(move node); }
		}
	}
	if b == cast<Byte>(116) {
		match _parse_literal(text, idx, "true", JsonNode::Bool(true)) {
			core.Result::Err(e) => { return core.Result::Err(move e); },
			core.Result::Ok(node) => { _set_leaf(ctx, sp, start, *idx); return core.Result::Ok(move node); }
		}
	}
	if b == cast<Byte>(102) {
		match _parse_literal(text, idx, "false", JsonNode::Bool(false)) {
			core.Result::Err(e) => { return core.Result::Err(move e); },
			core.Result::Ok(node) => { _set_leaf(ctx, sp, start, *idx); return core.Result::Ok(move node); }
		}
	}
	if b == cast<Byte>(34) {
		match _parse_string(text, idx, ctx) {
			core.Result::Err(e) => { return core.Result::Err(move e); },
			core.Result::Ok(v) => { _set_leaf(ctx, sp, start, *idx); return core.Result::Ok(JsonNode::String(move v)); }
		}
	}
	if b == cast<Byte>(91) {
		return _parse_array_rec(text, idx, ctx, depth, sp);
	}
	if b == cast<Byte>(123) {
		return _parse_object_rec(text, idx, ctx, depth, sp);
	}
	if b == cast<Byte>(45) or _is_digit(b) {
		match _parse_number(text, idx, ctx) {
			core.Result::Err(e) => { return core.Result::Err(move e); },
			core.Result::Ok(node) => { _set_leaf(ctx, sp, start, *idx); return core.Result::Ok(move node); }
		}
	}
	return core.Result::Err(_err_parse(text, "invalid-syntax", *idx));
}

// Recursive twin of `_parse_document` (calls `_parse_value_rec`).
fn _parse_document_rec(text: &String, ctx: &_ParseCtx, root_sp: &mut Optional<_SpanTree>) nothrow -> core.Result<JsonNode, JsonErrorData> {
	if _over_limit(ctx.cfg.limits.max_document_bytes, text.byte_length()) {
		return core.Result::Err(_err_parse(text, "limit-document-bytes", 0));
	}
	var idx = 0;
	_skip_ws(text, idx);
	val root_start = idx;
	match _parse_value_rec(text, idx, ctx, 1, root_sp) {
		core.Result::Err(e) => { return core.Result::Err(move e); },
		core.Result::Ok(v) => {
			_skip_ws(text, idx);
			if idx != text.byte_length() {
				return core.Result::Err(_err_parse(text, "invalid-syntax", idx));
			}
			match _check_top_level(v, ctx, root_start, text) {
				Optional::Some(e) => { return core.Result::Err(move e); },
				Optional::None() => { }
			}
			return core.Result::Ok(move v);
		}
	}
}

/// TEST ORACLE: recursive parse under an explicit config (non-located).
pub fn _oracle_parse_with_config(text: &String, cfg: &JsonParseConfig) nothrow -> core.Result<JsonNode, JsonErrorData> {
	match _validate_limits(cfg.limits) {
		Optional::Some(e) => { return core.Result::Err(move e); },
		Optional::None() => { }
	}
	val ctx = _ParseCtx(cfg = *cfg, legacy = false, locate = false);
	var sp: Optional<_SpanTree> = Optional::None();
	return _parse_document_rec(text, ctx, sp);
}

/// TEST ORACLE: recursive parse WITH location (span tree), mirroring
/// `parse_located` — returns a JsonDoc so span trees can be compared.
pub fn _oracle_parse_located(text: &String, cfg: &JsonParseConfig) nothrow -> core.Result<JsonDoc, JsonErrorData> {
	match _validate_limits(cfg.limits) {
		Optional::Some(e) => { return core.Result::Err(move e); },
		Optional::None() => { }
	}
	val ctx = _ParseCtx(cfg = *cfg, legacy = false, locate = true);
	var sp: Optional<_SpanTree> = Optional::None();
	match _parse_document_rec(text, ctx, sp) {
		core.Result::Err(e) => { return core.Result::Err(move e); },
		core.Result::Ok(node) => {
			match sp {
				Some(tree) => { return core.Result::Ok(JsonDoc(root = move node, spans = move tree, text = *text)); },
				None => { return core.Result::Err(_err_parse(text, "internal-error", 0)); }
			}
		}
	}
}
