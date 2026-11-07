// A deliberately bounded migration reference, not a production kernel.
// JSON remains at this executable's boundary; requests have a finite domain form.
package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"math/big"
	"os"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"unicode/utf8"
)

type object = map[string]any

func require(ok bool, message string) {
	if !ok {
		panic(message)
	}
}
func obj(value any) object {
	v, ok := value.(map[string]any)
	require(ok, "expected object")
	return v
}
func arr(value any) []any {
	v, ok := value.([]any)
	require(ok, "expected array")
	return v
}
func text(value any) string {
	v, ok := value.(string)
	require(ok && utf8.ValidString(v), "expected Unicode string")
	return v
}
func integer(value any) int64 {
	n, ok := value.(json.Number)
	require(ok, "expected integer")
	v, err := n.Int64()
	require(err == nil, "expected int64")
	return v
}
func boolean(value any) bool {
	v, ok := value.(bool)
	require(ok, "expected boolean")
	return v
}
func exact(row object, keys ...string) {
	require(len(row) == len(keys), "unexpected fields")
	for _, key := range keys {
		_, ok := row[key]
		require(ok, "missing field")
	}
}
func present(row object, key string) bool { _, ok := row[key]; return ok }
func nonempty(value any) string {
	s := text(value)
	require(strings.TrimSpace(s) != "", "empty identity")
	return s
}

// encoding/json escapes U+2028/U+2029 even with EscapeHTML(false). Python v1
// hashing does not. This encoder also distinguishes 1 from 1.0 and -0.0.
func quote(s string) string {
	require(utf8.ValidString(s), "invalid UTF-8")
	var out strings.Builder
	out.WriteByte('"')
	for _, r := range s {
		switch r {
		case '"':
			out.WriteString(`\"`)
		case '\\':
			out.WriteString(`\\`)
		case '\b':
			out.WriteString(`\b`)
		case '\f':
			out.WriteString(`\f`)
		case '\n':
			out.WriteString(`\n`)
		case '\r':
			out.WriteString(`\r`)
		case '\t':
			out.WriteString(`\t`)
		default:
			if r < 32 {
				fmt.Fprintf(&out, `\u%04x`, r)
			} else {
				out.WriteRune(r)
			}
		}
	}
	out.WriteByte('"')
	return out.String()
}
func number(n json.Number) string {
	s := string(n)
	if !strings.ContainsAny(s, ".eE") {
		value, ok := new(big.Int).SetString(s, 10)
		require(ok, "invalid integer")
		return value.String()
	}
	f, err := n.Float64()
	require(err == nil && !math.IsInf(f, 0) && !math.IsNaN(f), "nonfinite number")
	a := math.Abs(f)
	if a != 0 && (a >= 1e16 || a < 1e-4) {
		return strconv.FormatFloat(f, 'e', -1, 64)
	}
	s = strconv.FormatFloat(f, 'f', -1, 64)
	if !strings.Contains(s, ".") {
		s += ".0"
	}
	return s
}
func canonical(v any) string {
	switch value := v.(type) {
	case nil:
		return "null"
	case bool:
		if value {
			return "true"
		}
		return "false"
	case string:
		return quote(value)
	case json.Number:
		return number(value)
	case []any:
		parts := make([]string, len(value))
		for i, item := range value {
			parts[i] = canonical(item)
		}
		return "[" + strings.Join(parts, ",") + "]"
	case map[string]any:
		keys := make([]string, 0, len(value))
		for key := range value {
			keys = append(keys, key)
		}
		sort.Strings(keys)
		parts := make([]string, 0, len(keys))
		for _, key := range keys {
			parts = append(parts, quote(key)+":"+canonical(value[key]))
		}
		return "{" + strings.Join(parts, ",") + "}"
	default:
		panic("non-JSON value")
	}
}
func hash(s string) string { digest := sha256.Sum256([]byte(s)); return hex.EncodeToString(digest[:]) }

type span struct {
	Start, End              int64
	Digest, ResultRef, Tool string
	OK                      bool
}

func decodeSpans(prompt string, value any) []span {
	result := make([]span, 0)
	runes := []rune(prompt)
	previous := int64(0)
	for _, raw := range arr(value) {
		row := obj(raw)
		exact(row, "start", "end", "digest", "result_ref", "tool", "ok")
		s := span{integer(row["start"]), integer(row["end"]), nonempty(row["digest"]), nonempty(row["result_ref"]), nonempty(row["tool"]), boolean(row["ok"])}
		require(previous <= s.Start && s.Start < s.End && s.End <= int64(len(runes)), "span range")
		require(hash(quote(string(runes[s.Start:s.End]))) == s.Digest, "span digest")
		result = append(result, s)
		previous = s.End
	}
	return result
}
func spansValue(spans []span) []any {
	rows := make([]any, 0, len(spans))
	for _, s := range spans {
		rows = append(rows, object{
			"start": s.Start, "end": s.End, "digest": s.Digest, "result_ref": s.ResultRef, "tool": s.Tool, "ok": s.OK})
	}
	return rows
}

type method uint8

const (
	send method = iota
	sendTurn
	sendResults
	acknowledge
	sendBound
	acknowledgeBound
)

var methodNames = []string{"send", "send_turn", "send_tool_results", "acknowledge_tool_results", "send_tool_results_bound", "acknowledge_tool_results_bound"}
var positional = [][]string{
	{"text", "timeout"}, {"prompt", "tools", "timeout"}, {"results", "tools", "timeout"},
	{"results", "declared_tools", "timeout"},
	{"results", "window_ref", "tools", "timeout", "delivery_commit", "tool_call_observations", "tool_call_history_groups", "output_token_limit", "require_tool_call"},
	{"results", "window_ref", "declared_tools", "timeout"},
}

type toolDefinition struct {
	Name, Description string
	Parameters        object
}
type toolResult struct {
	CallID, Content string
	UserContent     *string
}
type binding struct{ ConnectionID, Revision, Model, Protocol, History string }
type window struct {
	Session, ID string
	Binding     binding
}
type providerRequest struct {
	Method  method
	Prompt  string
	Tools   *[]toolDefinition // nil is JSON null; non-nil empty is []
	Results []toolResult
	Window  *window
	Options object // Only validated option fields; map membership retains omission.
}

func decodeTools(value any) *[]toolDefinition {
	if value == nil {
		return nil
	}
	tools := make([]toolDefinition, 0)
	for _, raw := range arr(value) {
		row := obj(raw)
		exact(row, "name", "description", "parameters")
		tools = append(tools, toolDefinition{text(row["name"]), text(row["description"]), obj(row["parameters"])})
	}
	return &tools
}
func toolsValue(tools *[]toolDefinition) any {
	if tools == nil {
		return nil
	}
	rows := make([]any, 0)
	for _, t := range *tools {
		rows = append(rows, object{"name": t.Name, "description": t.Description, "parameters": t.Parameters})
	}
	return rows
}
func decodeResults(value any) []toolResult {
	rows := make([]toolResult, 0)
	for _, raw := range arr(value) {
		row := obj(raw)
		exact(row, "call_id", "content", "user_content")
		var user *string
		if row["user_content"] != nil {
			s := text(row["user_content"])
			user = &s
		}
		rows = append(rows, toolResult{text(row["call_id"]), text(row["content"]), user})
	}
	return rows
}
func resultsValue(results []toolResult) []any {
	rows := make([]any, 0)
	for _, r := range results {
		var user any
		if r.UserContent != nil {
			user = *r.UserContent
		}
		rows = append(rows, object{"call_id": r.CallID, "content": r.Content, "user_content": user})
	}
	return rows
}
func decodeWindow(value any) *window {
	row := obj(value)
	b := obj(row["binding"])
	return &window{nonempty(row["session_id"]), nonempty(row["window_id"]), binding{
		nonempty(b["connection_id"]), nonempty(b["connection_revision"]), nonempty(b["model_id"]), nonempty(b["protocol"]), nonempty(b["history_contract_id"])}}
}
func windowValue(w *window) object {
	return object{"session_id": w.Session, "window_id": w.ID, "binding": object{"connection_id": w.Binding.ConnectionID,
		"connection_revision": w.Binding.Revision, "model_id": w.Binding.Model, "protocol": w.Binding.Protocol, "history_contract_id": w.Binding.History}}
}
func pop(row object, key string) any { value := row[key]; delete(row, key); return value }
func decodeRequest(value any) providerRequest {
	row := obj(value)
	if present(row, "schema_version") {
		require(integer(row["schema_version"]) == 1, "version")
	}
	name := text(row["method"])
	index := -1
	for i, methodName := range methodNames {
		if methodName == name {
			index = i
		}
	}
	require(index >= 0, "method")
	args, kwargs := arr(row["args"]), obj(row["kwargs"])
	arguments := object{}
	for key, value := range kwargs {
		arguments[key] = value
	}
	require(len(args) <= len(positional[index]), "argument count")
	for i, value := range args {
		key := positional[index][i]
		require(!present(arguments, key), "duplicate argument")
		arguments[key] = value
	}
	r := providerRequest{Method: method(index)}
	if r.Method == send {
		r.Prompt = text(pop(arguments, "text"))
	} else {
		toolKey := "tools"
		if r.Method == acknowledge || r.Method == acknowledgeBound {
			toolKey = "declared_tools"
		}
		r.Tools = decodeTools(pop(arguments, toolKey))
		if r.Method == sendTurn {
			r.Prompt = text(pop(arguments, "prompt"))
		} else {
			r.Results = decodeResults(pop(arguments, "results"))
		}
		if r.Method == sendBound || r.Method == acknowledgeBound {
			r.Window = decodeWindow(pop(arguments, "window_ref"))
		}
		if r.Method == acknowledge || r.Method == acknowledgeBound {
			require(r.Tools != nil, "acknowledgement tools")
		}
	}
	for key, value := range arguments {
		isAck := r.Method == acknowledge || r.Method == acknowledgeBound
		if isAck {
			require(key == "timeout", "ack option")
		}
		switch key {
		case "timeout":
			if value != nil {
				n, ok := value.(json.Number)
				require(ok, "timeout")
				f, err := n.Float64()
				require(err == nil && f > 0 && !math.IsInf(f, 0), "timeout")
			}
		case "tool_call_history_groups", "output_token_limit":
			require(integer(value) >= 0, "counter")
		case "tool_call_observations":
			text(value)
		case "require_tool_call":
			require(r.Method != send, "native option")
			boolean(value)
		case "tool_call_json":
			require(r.Method == send, "text option")
			boolean(value)
		case "tool_call_contract":
			require(r.Method == send, "text option")
			text(value)
		case "tool_call_schema":
			require(r.Method == send, "text option")
			if value != nil {
				obj(value)
			}
		case "tool_call_results":
			require(r.Method == send, "text option")
			if value != nil {
				decodeSpans(r.Prompt, value)
			}
		case "delivery_commit":
			require(r.Method == sendBound, "delivery option")
			if value != nil {
				obj(value)
			}
		default:
			panic("unknown option")
		}
	}
	r.Options = arguments
	return r
}
func encodeRequest(r providerRequest) object {
	var args []any
	switch r.Method {
	case send:
		args = []any{r.Prompt}
	case sendTurn:
		args = []any{r.Prompt, toolsValue(r.Tools)}
	case sendBound, acknowledgeBound:
		args = []any{resultsValue(r.Results), windowValue(r.Window), toolsValue(r.Tools)}
	default:
		args = []any{resultsValue(r.Results), toolsValue(r.Tools)}
	}
	return object{"method": methodNames[r.Method], "args": args, "kwargs": r.Options}
}
func reply(value any) any {
	row := obj(value)
	protocol, content := text(row["protocol"]), text(row["text"])
	if protocol == "text" {
		exact(row, "protocol", "text")
		return object{"protocol": protocol, "text": content}
	}
	require(protocol == "native", "protocol")
	exact(row, "protocol", "text", "reasoning", "finish", "raw", "tool_calls")
	finish := text(row["finish"])
	require(finish == "complete" || finish == "output_limit" || finish == "protocol_error", "finish")
	calls := make([]any, 0)
	for _, raw := range arr(row["tool_calls"]) {
		call := obj(raw)
		exact(call, "id", "name", "arguments")
		calls = append(calls, object{"id": text(call["id"]), "name": text(call["name"]), "arguments": obj(call["arguments"])})
	}
	return object{"protocol": protocol, "text": content, "reasoning": text(row["reasoning"]), "finish": finish, "raw": obj(row["raw"]), "tool_calls": calls}
}
func passage(value any) any {
	row := obj(value)
	url, textHash := text(row["url"]), text(row["text_hash"])
	start, end := integer(row["start"]), integer(row["end"])
	pin := ""
	if row["source_version"] != nil {
		pin = "\n" + canonical([]any{row["source_version"], row["evidence_version"]})
	}
	return "p" + hash(fmt.Sprintf("%s\n%s\n%d:%d%s", url, textHash, start, end, pin))[:12]
}

var fingerprint = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)

func isInteger(v any) bool {
	n, ok := v.(json.Number)
	if !ok {
		return false
	}
	_, ok = new(big.Int).SetString(string(n), 10)
	return ok
}
func integerSign(v any) int {
	n, ok := v.(json.Number)
	require(ok, "expected integer")
	value, ok := new(big.Int).SetString(string(n), 10)
	require(ok, "expected integer")
	return value.Sign()
}
func verification(value any) object {
	row, ok := value.(map[string]any)
	if !ok {
		return object{"gap": "verification_result_missing", "succeeded": false}
	}
	command, _ := row["command"].(string)
	code, rev, fp := row["exit_code"], row["workspace_revision"], row["workspace_fingerprint"]
	gap := ""
	if strings.TrimSpace(command) == "" || !isInteger(code) {
		gap = "verification_result_missing"
	} else if rev == nil || fp == nil || fp == "" {
		gap = "verification_identity_missing"
	} else if !isInteger(rev) {
		gap = "verification_identity_invalid"
	} else {
		f, ok := fp.(string)
		if !ok || integerSign(rev) < 1 || !fingerprint.MatchString(strings.TrimSpace(f)) {
			gap = "verification_identity_invalid"
		}
	}
	success := gap == "" && integerSign(code) == 0
	if present(row, "passed") {
		p, ok := row["passed"].(bool)
		success = success && ok && p
	}
	return object{"gap": gap, "succeeded": success}
}

func latestVerification(value any) any {
	row := obj(value)
	epoch := integer(row["edit_revision"])
	type key struct{ Command, Cwd string }
	latest := map[key]object{}
	order := []key{}
	for _, raw := range arr(row["rows"]) {
		record := obj(raw)
		if !isInteger(record["revision"]) || integer(record["revision"]) != epoch {
			continue
		}
		command := strings.TrimSpace(text(record["command"]))
		cwd := "."
		if record["cwd"] != nil {
			cwd = strings.TrimSpace(text(record["cwd"]))
			if cwd == "" {
				cwd = "."
			}
		}
		identity := key{command, cwd}
		if _, exists := latest[identity]; !exists {
			order = append(order, identity)
		}
		fact := verification(record)
		fact["command"], fact["cwd"] = command, cwd
		latest[identity] = fact
	}
	result := make([]any, 0)
	for _, key := range order {
		result = append(result, latest[key])
	}
	return result
}

// This slice models states with no effects/batches only; other input is rejected.
// Recovery effect/CAS faults are validated by Python's durable test suite.
func reduce(value any) any {
	row := obj(value)
	leaf := ""
	if row["leaf"] != nil {
		leaf = text(row["leaf"])
	}
	a := object{"kind": "fail_invariant", "leaf": leaf, "reason": "", "effect_id": "", "effect_ids": []any{},
		"delivery_batch_id": "", "driver": "", "response_ref": "", "request_ref": "", "work_ref": ""}
	if row["leaf"] == nil {
		a["reason"] = "missing_operation_state"
		return a
	}
	known := map[string]bool{}
	for _, name := range []string{"accepted", "writer_running", "writer_settled", "completion_proof_recorded", "repair_context_admitted", "repair_running", "repair_settled", "provider_request_ready", "provider_effect_pending", "provider_response_ready", "tool_effect_pending", "tool_delivery_pending", "plan_ready", "completion_pending", "control_ready", "closing", "kernel_settled", "terminal"} {
		known[name] = true
	}
	if !known[leaf] {
		a["reason"] = "unknown_leaf"
		return a
	}
	get := func(key string) string {
		if row[key] == nil {
			return ""
		}
		return text(row[key])
	}
	cancel, _ := row["cancel_requested"].(bool)
	closing, _ := row["closing_intent"].(bool)
	if cancel && !closing && (leaf == "writer_running" || leaf == "repair_running" || leaf == "provider_request_ready" || leaf == "provider_response_ready" || leaf == "plan_ready" || leaf == "completion_pending" || leaf == "control_ready" || leaf == "closing" || leaf == "kernel_settled" || leaf == "tool_delivery_pending") {
		a["kind"], a["reason"], a["driver"] = "begin_closing", "stopped", get("driver")
		return a
	}
	switch leaf {
	case "terminal":
		a["kind"] = "terminal"
	case "provider_request_ready":
		if get("response_ref") != "" || get("request_ref") == "" {
			a["reason"] = "prepared_request_effects_mismatch"
		} else {
			a["kind"], a["request_ref"], a["driver"] = "submit_provider_request", get("request_ref"), get("driver")
		}
	case "provider_effect_pending":
		a["reason"] = "missing_provider_effect_intent"
	case "provider_response_ready":
		a["reason"] = "missing_provider_response_fact"
	case "tool_effect_pending":
		a["reason"] = "empty_tool_pending"
	case "tool_delivery_pending":
		panic("effect/batch reducer outside reference slice")
	case "plan_ready", "completion_pending", "control_ready", "closing", "kernel_settled":
		answered, _ := row["question_answered"].(bool)
		if leaf == "kernel_settled" && answered {
			a["kind"], a["work_ref"] = "resume_question", get("work_ref")
			return a
		}
		if (leaf == "control_ready" || leaf == "closing" || leaf == "kernel_settled") && get("work_ref") == "" {
			a["reason"] = "execution_input_missing"
			return a
		}
		a["kind"] = map[string]string{"plan_ready": "execute_tool_plan", "completion_pending": "evaluate_completion", "control_ready": "prepare_control_delivery", "closing": "close_protocol", "kernel_settled": "return_kernel_result"}[leaf]
		a["response_ref"], a["work_ref"], a["driver"] = get("response_ref"), get("work_ref"), get("driver")
	case "writer_running", "repair_running":
		a["kind"] = "prepare_turn"
	default:
		a["kind"] = "continue_operation"
	}
	return a
}

type outcome struct {
	OK    bool `json:"ok"`
	Value any  `json:"value,omitempty"`
}

func execute(operation string, value any) (result outcome) {
	defer func() {
		if recover() != nil {
			result = outcome{OK: false}
		}
	}()
	var output any
	switch operation {
	case "request":
		output = encodeRequest(decodeRequest(value))
	case "reply":
		output = reply(value)
	case "span":
		row := obj(value)
		output = spansValue(decodeSpans(text(row["text"]), row["spans"]))
	case "passage":
		output = passage(value)
	case "verification":
		output = verification(value)
	case "latest_verification":
		output = latestVerification(value)
	case "reduce":
		output = reduce(value)
	case "digest":
		output = hash(canonical(value))
	default:
		panic("unknown reference operation")
	}
	return outcome{OK: true, Value: output}
}
func parseValue(raw []byte) any {
	// Go's JSON decoder otherwise replaces lone UTF-16 surrogates with U+FFFD.
	// The Python contract rejects them; preserve that failure instead of repairing.
	require(utf8.Valid(raw), "invalid UTF-8")
	for i := 0; i < len(raw); i++ {
		if raw[i] != '\\' {
			continue
		}
		i++
		require(i < len(raw), "escape")
		if raw[i] != 'u' {
			continue
		}
		require(i+4 < len(raw), "Unicode escape")
		n, err := strconv.ParseUint(string(raw[i+1:i+5]), 16, 16)
		require(err == nil, "Unicode escape")
		i += 4
		if n >= 0xD800 && n <= 0xDBFF {
			require(i+6 < len(raw) && raw[i+1] == '\\' && raw[i+2] == 'u', "unpaired surrogate")
			low, err := strconv.ParseUint(string(raw[i+3:i+7]), 16, 16)
			require(err == nil && low >= 0xDC00 && low <= 0xDFFF, "unpaired surrogate")
			i += 6
		} else {
			require(n < 0xDC00 || n > 0xDFFF, "unpaired surrogate")
		}
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	var value any
	require(decoder.Decode(&value) == nil, "JSON value")
	return value
}
func executeRaw(operation string, raw []byte) (result outcome) {
	defer func() {
		if recover() != nil {
			result = outcome{OK: false}
		}
	}()
	return execute(operation, parseValue(raw))
}
func main() {
	data, err := io.ReadAll(io.LimitReader(os.Stdin, 32<<20))
	if err != nil {
		panic(err)
	}
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.UseNumber()
	var cases []struct {
		Operation string          `json:"operation"`
		Value     json.RawMessage `json:"value"`
	}
	if err := decoder.Decode(&cases); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	results := make([]outcome, len(cases))
	for i, c := range cases {
		results[i] = executeRaw(c.Operation, c.Value)
	}
	encoder := json.NewEncoder(os.Stdout)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(results); err != nil {
		panic(err)
	}
}
