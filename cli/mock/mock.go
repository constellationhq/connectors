// Package mock serves the Constellation workflow results endpoints (same paths, query
// parameters, cursor semantics and JSON as the API) from a scenario, so a bridge can run
// without an account.
package mock

import (
	"bytes"
	_ "embed"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/constellationhq/connectors/cli/results"
)

//go:embed demo.json
var demoJSON []byte

var (
	connectionPattern = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{0,62}$`)
	runIDPattern      = regexp.MustCompile(`^run_\d{8}T\d{6}_[0-9a-f]{8}$`)
)

// Entry is one feed entry of a scenario: a run's items as one feed output publishes them.
// Phase orders what a mock shows: an entry is in the feed once the mock's phase reaches it.
type Entry struct {
	Phase     int              `json:"phase"`
	Published string           `json:"published"`
	RunID     string           `json:"run_id"`
	Workflow  string           `json:"workflow"`
	Template  string           `json:"template"`
	Window    [2]string        `json:"window"`
	Schema    string           `json:"schema,omitempty"`
	Items     []map[string]any `json:"items"`
}

// Cursor is the entry's feed cursor: when it was published, then its run.
func (e Entry) Cursor() string { return e.Published + "#" + e.RunID }

type Scenario struct {
	Name    string  `json:"name"`
	Entries []Entry `json:"entries"`
}

// Demo is the demo fleet's scenario: NOAA-20 (norad-43013) passes over NASA Fairbanks, a run
// in an item shape newer than bridges read, and Ka-band rain-fade swap runs on the
// geostationary satellites with an action in every state (advice, pending, approved, rejected
// and a status bridges do not know), the run published again once one is approved, and a later
// run sending a standing approval again (the same decision_id) and a new approval of an action
// approved before (a new decision_id), plus a rain-fade advisory with a note and no action (no
// backup pass) and an RFI watch run suggesting a reroute.
func Demo() Scenario {
	var s Scenario
	d := json.NewDecoder(bytes.NewReader(demoJSON))
	d.UseNumber()
	if err := d.Decode(&s); err != nil {
		panic(err)
	}
	sort.SliceStable(s.Entries, func(i, j int) bool { return s.Entries[i].Cursor() < s.Entries[j].Cursor() })
	return s
}

// Request is a request the mock answered.
type Request struct {
	Method string
	Path   string
	Query  url.Values
	Body   map[string]any
}

// Mock is an http.Handler for the feed endpoints. The x-api-key header is accepted, not required.
type Mock struct {
	Scenario Scenario
	// Phase is the last phase whose entries are in the feed.
	Phase int
	// FailFirstPoll answers the first GET /predictions/feed 503, as an outage would.
	FailFirstPoll bool
	// MaxPageRecords caps page_records (0: no cap), so a small run arrives in pages.
	MaxPageRecords int
	// Settings replace a custom connector's manifest defaults in its feed outputs.
	Settings map[string]any
	// ReplayOnce, from phase 2, answers the first read after the newest entry from the start of
	// the feed, as a feed replaying after a failover.
	ReplayOnce bool

	mu        sync.Mutex
	polled    bool
	replayed  bool
	requests  []Request
	manifests map[string]map[string]any
}

// New is a mock of `s` with every entry in the feed.
func New(s Scenario) *Mock {
	m := &Mock{Scenario: s}
	for _, e := range s.Entries {
		m.Phase = max(m.Phase, e.Phase)
	}
	return m
}

// SetPhase puts the entries up to `phase` in the feed.
func (m *Mock) SetPhase(phase int) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.Phase = phase
}

// Requests are the requests answered so far, oldest first.
func (m *Mock) Requests() []Request {
	m.mu.Lock()
	defer m.mu.Unlock()
	return append([]Request(nil), m.requests...)
}

type queryError struct{ name, msg string }

func reply(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if body != nil {
		_ = json.NewEncoder(w).Encode(body)
	}
}

func invalid(w http.ResponseWriter, where, name, msg string) {
	reply(w, 422, map[string]any{"detail": []any{map[string]any{"loc": []string{where, name}, "msg": msg, "type": "value_error"}}})
}

func (m *Mock) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	var body map[string]any
	if r.Method == http.MethodPost {
		d := json.NewDecoder(r.Body)
		d.UseNumber()
		if err := d.Decode(&body); err != nil {
			invalid(w, "body", "", "a JSON object is required")
			return
		}
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	m.requests = append(m.requests, Request{r.Method, r.URL.Path, r.URL.Query(), body})
	switch r.Method + " " + r.URL.Path {
	case "GET /predictions/feed":
		m.feed(w, r.URL.Query())
	case "POST /predictions/feed/checkin":
		m.checkIn(w, body)
	case "POST /predictions/feed/deliveries":
		m.deliver(w, body)
	case "GET /account/state":
		reply(w, 200, map[string]any{"account_id": "acct_demo", "account_type": "individual", "plan": "operator",
			"entitlements": map[string]any{"live_ingest": true, "minting": true}, "act_mode": "live"})
	default:
		reply(w, 404, map[string]any{"detail": "Not Found"})
	}
}

func intParam(q url.Values, name string, fallback, lo, hi int) (int, *queryError) {
	raw := q.Get(name)
	if raw == "" {
		return fallback, nil
	}
	n, err := strconv.Atoi(raw)
	if err != nil || n < lo || n > hi {
		return 0, &queryError{name, fmt.Sprintf("must be a whole number from %d to %d", lo, hi)}
	}
	return n, nil
}

func (m *Mock) feed(w http.ResponseWriter, q url.Values) {
	limit, bad := intParam(q, "limit", 20, 1, 20)
	pageRecords, bad2 := intParam(q, "page_records", 0, 1, 5000)
	page, bad3 := intParam(q, "page", 0, 0, 1<<30)
	consumer, cursor := q.Get("consumer"), q.Get("cursor")
	for _, b := range []*queryError{bad, bad2, bad3} {
		if b != nil {
			invalid(w, "query", b.name, b.msg)
			return
		}
	}
	if q.Has("consumer") && !connectionPattern.MatchString(consumer) {
		invalid(w, "query", "consumer", "must match "+connectionPattern.String())
		return
	}
	if q.Has("cursor") && (cursor == "" || len(cursor) > 128) {
		invalid(w, "query", "cursor", "must be 1 to 128 characters")
		return
	}
	if q.Has("from") && q.Get("from") != "latest" {
		invalid(w, "query", "from", "must be latest")
		return
	}
	if m.FailFirstPoll && !m.polled {
		m.polled = true
		reply(w, 503, map[string]any{"detail": "a stand-in outage: the bridge should retry"})
		return
	}
	m.polled = true
	if m.MaxPageRecords > 0 && pageRecords > 0 {
		pageRecords = min(pageRecords, m.MaxPageRecords)
	}
	visible := m.visible()
	var oldest *string
	if len(visible) > 0 {
		c := visible[0].Cursor()
		oldest = &c
	}
	if q.Has("from") {
		// Every entry here has an output to every connection: a first start reads from just
		// before the newest.
		start := ""
		if len(visible) > 1 {
			start = visible[len(visible)-2].Cursor()
		}
		reply(w, 200, results.Page{Entries: []results.Entry{}, Next: start, Oldest: oldest})
		return
	}
	if q.Has("cursor") {
		for _, e := range visible {
			if e.Cursor() == cursor {
				otlp, more := pageOf(m.encode(e, consumer), orDefault(pageRecords, 5000), page)
				reply(w, 200, results.Entry{Cursor: cursor, OTLP: otlp, NextPage: more})
				return
			}
		}
		reply(w, 404, map[string]any{"detail": map[string]any{"error": "unknown_cursor"}})
		return
	}
	after := q.Get("after")
	if m.ReplayOnce && !m.replayed && m.Phase >= 2 && len(visible) > 0 && after == visible[len(visible)-1].Cursor() {
		m.replayed, after = true, ""
	}
	entries := []results.Entry{}
	for _, e := range visible {
		if e.Cursor() > after && len(entries) < limit {
			otlp, more := m.encode(e, consumer), 0
			if pageRecords > 0 {
				otlp, more = pageOf(otlp, pageRecords, 0)
			}
			entries = append(entries, results.Entry{Cursor: e.Cursor(), OTLP: otlp, NextPage: more})
		}
	}
	next := after
	if len(entries) > 0 {
		next = entries[len(entries)-1].Cursor
	}
	reply(w, 200, results.Page{Entries: entries, Next: next, Oldest: oldest})
}

func orDefault(n, fallback int) int {
	if n > 0 {
		return n
	}
	return fallback
}

func (m *Mock) visible() []Entry {
	var out []Entry
	for _, e := range m.Scenario.Entries {
		if e.Phase <= m.Phase {
			out = append(out, e)
		}
	}
	return out
}

var checkInFields = map[string]bool{"consumer": true, "system": true, "manifest": true, "target": true,
	"system_version": true, "bridge_version": true, "mapping_versions": true, "can": true, "widget": true}

func (m *Mock) checkIn(w http.ResponseWriter, body map[string]any) {
	for k := range body {
		if !checkInFields[k] {
			invalid(w, "body", k, "extra inputs are not permitted")
			return
		}
	}
	consumer, _ := body["consumer"].(string)
	system, _ := body["system"].(string)
	manifest, hasManifest := body["manifest"].(map[string]any)
	switch {
	case !connectionPattern.MatchString(consumer):
		invalid(w, "body", "consumer", "must match "+connectionPattern.String())
	case !strings.Contains(" yamcs lattice cosmos custom ", " "+system+" "):
		invalid(w, "body", "system", "must be yamcs, lattice, cosmos or custom")
	case (system == "custom") != hasManifest:
		invalid(w, "body", "manifest", "a custom bridge, and only a custom bridge, checks in with a manifest")
	case hasManifest && manifest["name"] != consumer:
		invalid(w, "body", "manifest", "a manifest's name is the bridge's consumer")
	default:
		if hasManifest {
			if m.manifests == nil {
				m.manifests = map[string]map[string]any{}
			}
			m.manifests[consumer] = manifest
		}
		w.WriteHeader(204)
	}
}

var deliveryFields = map[string]bool{"consumer": true, "run_id": true, "cursor": true, "applied": true, "target": true}

func (m *Mock) deliver(w http.ResponseWriter, body map[string]any) {
	for k := range body {
		if !deliveryFields[k] {
			invalid(w, "body", k, "extra inputs are not permitted")
			return
		}
	}
	consumer, _ := body["consumer"].(string)
	run, _ := body["run_id"].(string)
	cursor, _ := body["cursor"].(string)
	target, _ := body["target"].(string)
	applied, ok := body["applied"].(map[string]any)
	if body["applied"] == nil {
		applied, ok = map[string]any{}, true
	}
	whole := ok && len(applied) <= 20
	for _, n := range applied {
		if _, err := strconv.Atoi(fmt.Sprint(n)); err != nil {
			whole = false
		}
	}
	switch {
	case !connectionPattern.MatchString(consumer):
		invalid(w, "body", "consumer", "must match "+connectionPattern.String())
	case !runIDPattern.MatchString(run):
		invalid(w, "body", "run_id", "must match "+runIDPattern.String())
	case cursor == "" || len(cursor) > 128:
		invalid(w, "body", "cursor", "must be 1 to 128 characters")
	case !whole:
		invalid(w, "body", "applied", "at most 20 whole-number counts")
	case len(target) > 200:
		invalid(w, "body", "target", "at most 200 characters")
	default:
		for _, e := range m.visible() {
			if e.RunID == run {
				w.WriteHeader(204)
				return
			}
		}
		reply(w, 404, map[string]any{"detail": map[string]any{"error": "unknown_run"}})
	}
}

// encode is the entry as the feed sends it to `consumer`: one feed output to that connection
// (to "demo" when no consumer is named), with its manifest's settings at their defaults.
func (m *Mock) encode(e Entry, consumer string) results.OTLP {
	connection := consumer
	if connection == "" {
		connection = "demo"
	}
	settings := map[string]any{}
	if manifest, ok := m.manifests[consumer]; ok {
		declared, _ := manifest["settings"].(map[string]any)
		for k, s := range declared {
			s, _ := s.(map[string]any)
			settings[k] = s["default"]
		}
	}
	for k, v := range m.Settings {
		settings[k] = v
	}
	output, _ := json.Marshal(map[string]any{"connection": connection, "system": "custom", "mapping_version": 2,
		"mapping": map[string]any{"system": "custom", "settings": settings}, "tokens": map[string]any{}})
	schema := e.Schema
	if schema == "" {
		schema = results.Schema
	}
	published, _ := time.Parse("20060102T150405.000000", e.Published)
	nanos := strconv.FormatInt(published.UnixNano(), 10)
	var version any
	if len(e.Items) > 0 {
		version = e.Items[0]["workflow_version"]
	}
	var logs results.ResourceLogs
	logs.Resource.Attributes = attributes([][2]any{
		{"constellation.tenant", "demo"}, {"constellation.workflow", e.Workflow},
		{"constellation.workflow_version", version}, {"constellation.template", e.Template},
		{"constellation.run_id", e.RunID}, {"constellation.window.from", e.Window[0]},
		{"constellation.window.to", e.Window[1]}, {"constellation.connection", connection},
		{"constellation.system", "custom"}, {"constellation.output", string(output)},
	})
	records := []results.LogRecord{}
	for _, item := range e.Items {
		severity, _ := item["severity"].(string)
		if severity == "" {
			severity = "info"
		}
		var facets []any
		for _, f := range []string{"until", "value", "explain", "series", "severity", "action"} {
			if _, ok := item[f]; ok {
				facets = append(facets, f)
			}
		}
		entity, _ := item["entity"].(map[string]any)
		records = append(records, results.LogRecord{
			TimeUnixNano: nanos, ObservedTimeUnixNano: nanos,
			SeverityNumber: map[string]int{"info": 9, "warning": 13, "critical": 17}[severity],
			SeverityText:   strings.ToUpper(severity),
			Body:           anyValue(item),
			Attributes: attributes([][2]any{
				{"constellation.schema", schema}, {"constellation.entity.type", entity["type"]},
				{"constellation.entity.id", entity["id"]}, {"constellation.time.target", item["target_at"]},
				{"constellation.time.until", item["until"]}, {"constellation.facets", facets},
			}),
		})
	}
	logs.ScopeLogs = []results.ScopeLogs{{Scope: map[string]any{"name": "constellation.workflows"}, LogRecords: records}}
	return results.OTLP{ResourceLogs: []results.ResourceLogs{logs}}
}

func attributes(pairs [][2]any) []results.KeyValue {
	var out []results.KeyValue
	for _, p := range pairs {
		if p[1] != nil {
			out = append(out, results.KeyValue{Key: p[0].(string), Value: anyValue(p[1])})
		}
	}
	return out
}

// anyValue is a JSON value as an OTLP AnyValue; object keys sorted, so an entry encodes the same
// every time.
func anyValue(v any) map[string]any {
	switch v := v.(type) {
	case bool:
		return map[string]any{"boolValue": v}
	case json.Number:
		if strings.ContainsAny(string(v), ".eE") {
			f, _ := v.Float64()
			return map[string]any{"doubleValue": f}
		}
		return map[string]any{"intValue": string(v)}
	case string:
		return map[string]any{"stringValue": v}
	case map[string]any:
		keys := make([]string, 0, len(v))
		for k := range v {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		values := []any{}
		for _, k := range keys {
			if v[k] != nil {
				values = append(values, map[string]any{"key": k, "value": anyValue(v[k])})
			}
		}
		return map[string]any{"kvlistValue": map[string]any{"values": values}}
	case []any:
		values := []any{}
		for _, x := range v {
			values = append(values, anyValue(x))
		}
		return map[string]any{"arrayValue": map[string]any{"values": values}}
	}
	return map[string]any{"stringValue": fmt.Sprint(v)}
}

// pageOf is records page*size to (page+1)*size of an entry, each under its own resource, and
// the next page's number while records are left (0 when none). A resource with no records is
// on the first page: it is a run's whole answer for its output.
func pageOf(otlp results.OTLP, size, page int) (results.OTLP, int) {
	type at struct {
		resource int
		record   results.LogRecord
	}
	var flat []at
	has := map[int]bool{}
	for i, r := range otlp.ResourceLogs {
		for _, s := range r.ScopeLogs {
			for _, rec := range s.LogRecords {
				flat = append(flat, at{i, rec})
				has[i] = true
			}
		}
	}
	kept := map[int][]results.LogRecord{}
	if page == 0 {
		for i := range otlp.ResourceLogs {
			if !has[i] {
				kept[i] = nil
			}
		}
	}
	for _, a := range flat[min(page*size, len(flat)):min((page+1)*size, len(flat))] {
		kept[a.resource] = append(kept[a.resource], a.record)
	}
	out := results.OTLP{ResourceLogs: []results.ResourceLogs{}}
	for i, r := range otlp.ResourceLogs {
		records, ok := kept[i]
		if !ok {
			continue
		}
		if records != nil {
			r.ScopeLogs = []results.ScopeLogs{{Scope: r.ScopeLogs[0].Scope, LogRecords: records}}
		}
		out.ResourceLogs = append(out.ResourceLogs, r)
	}
	more := 0
	if (page+1)*size < len(flat) {
		more = page + 1
	}
	return out, more
}
