package mock

import (
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"

	"github.com/constellationhq/connectors/cli/results"
)

func serve(t *testing.T, m *Mock) *results.Client {
	t.Helper()
	server := httptest.NewServer(m)
	t.Cleanup(server.Close)
	return &results.Client{URL: server.URL, HTTP: server.Client()}
}

func status(err error) int {
	var apiErr *results.Error
	if errors.As(err, &apiErr) {
		return apiErr.Status
	}
	return 0
}

func TestTheFeedPagesByCursorOldestFirst(t *testing.T) {
	s := Demo()
	c := serve(t, New(s))
	page, err := c.Results("", "", 20, 0)
	if err != nil {
		t.Fatal(err)
	}
	if len(page.Entries) != len(s.Entries) || page.Next != s.Entries[len(s.Entries)-1].Cursor() || *page.Oldest != s.Entries[0].Cursor() {
		t.Fatalf("got %d entries, next %q, oldest %q", len(page.Entries), page.Next, *page.Oldest)
	}
	for i := 1; i < len(page.Entries); i++ {
		if page.Entries[i-1].Cursor >= page.Entries[i].Cursor {
			t.Fatalf("not oldest first: %q then %q", page.Entries[i-1].Cursor, page.Entries[i].Cursor)
		}
	}
	// after is exclusive, limit bounds the page, next is where to ask from.
	page, _ = c.Results(s.Entries[1].Cursor(), "", 2, 0)
	if len(page.Entries) != 2 || page.Entries[0].Cursor != s.Entries[2].Cursor() || page.Next != s.Entries[3].Cursor() {
		t.Fatalf("after %q: %+v", s.Entries[1].Cursor(), page)
	}
	// Caught up: nothing, and next stays where it was.
	page, _ = c.Results(s.Entries[len(s.Entries)-1].Cursor(), "", 20, 0)
	if len(page.Entries) != 0 || page.Next != s.Entries[len(s.Entries)-1].Cursor() {
		t.Fatalf("caught up: %+v", page)
	}
}

func TestTheFeedRefusesWhatTheAPIRefuses(t *testing.T) {
	c := serve(t, New(Demo()))
	for _, path := range []string{
		"/predictions/feed?limit=0", "/predictions/feed?limit=21", "/predictions/feed?page_records=0",
		"/predictions/feed?page_records=5001", "/predictions/feed?consumer=Not_A_Slug", "/predictions/feed?page=-1",
		"/predictions/feed?from=oldest",
	} {
		if err := c.Call("GET", path, nil, nil); status(err) != 422 {
			t.Errorf("%s: %v, want 422", path, err)
		}
	}
	if _, err := c.RunPage("20300101T000000.000000#run_nope", "", 0, 10); status(err) != 404 {
		t.Errorf("unknown cursor: %v, want 404", err)
	}
	if err := c.Call("GET", "/predictions/nothing", nil, nil); status(err) != 404 {
		t.Errorf("unknown path: %v", err)
	}
}

func TestAPagedRunJoinsToTheWholeRun(t *testing.T) {
	m := New(Demo())
	c := serve(t, m)
	whole, _ := c.Results("", "my-bridge", 20, 0)
	paged, err := c.Results("", "my-bridge", 20, 1)
	if err != nil {
		t.Fatal(err)
	}
	for i, e := range paged.Entries {
		if records := len(whole.Entries[i].OTLP.ResourceLogs[0].ScopeLogs[0].LogRecords); (records > 1) != (e.NextPage == 1) {
			t.Fatalf("%s with %d records has next_page %d", e.Cursor, records, e.NextPage)
		}
		joined, err := results.Whole(e, func(cursor string, n int) (results.Entry, error) {
			return c.RunPage(cursor, "my-bridge", n, 1)
		})
		if err != nil {
			t.Fatal(err)
		}
		if !reflect.DeepEqual(joined.OTLP, whole.Entries[i].OTLP) {
			t.Fatalf("%s joined is not the whole run", e.Cursor)
		}
	}
}

func TestMaxPageRecordsPagesEvenALargePageSize(t *testing.T) {
	m := New(Demo())
	m.MaxPageRecords = 1
	page, _ := serve(t, m).Results("", "", 20, 400)
	if page.Entries[0].NextPage != 1 {
		t.Fatalf("next_page %d", page.Entries[0].NextPage)
	}
}

func TestEachEntryIsOneOutputToTheConsumerWithItsManifestDefaults(t *testing.T) {
	c := serve(t, New(Demo()))
	manifest := map[string]any{"name": "my-bridge", "title": "Mine",
		"settings": map[string]any{"label": map[string]any{"type": "string", "title": "Label", "default": "forecast"}}}
	if err := c.Call("POST", "/predictions/feed/checkin", map[string]any{"consumer": "my-bridge", "system": "custom", "manifest": manifest}, nil); err != nil {
		t.Fatal(err)
	}
	page, _ := c.Results("", "my-bridge", 1, 0)
	var logs []string
	parts := results.Unpack(page.Entries[0].OTLP, "my-bridge", func(m string) { logs = append(logs, m) })
	if len(parts) != 1 || len(parts[0].Items) != 3 {
		t.Fatalf("parts %+v", parts)
	}
	var output map[string]any
	_ = json.Unmarshal([]byte(parts[0].Resource["constellation.output"].(string)), &output)
	if got := output["mapping"].(map[string]any)["settings"]; !reflect.DeepEqual(got, map[string]any{"label": "forecast"}) {
		t.Fatalf("settings %v", got)
	}
	if results.Unpack(page.Entries[0].OTLP, "another-bridge", nil) != nil {
		t.Fatal("another connection's output was unpacked")
	}
	// The run in a newer item shape is skipped, and said so.
	page, _ = c.Results(page.Next, "my-bridge", 1, 0)
	if parts := results.Unpack(page.Entries[0].OTLP, "my-bridge", func(m string) { logs = append(logs, m) }); parts != nil || len(logs) != 1 || !strings.Contains(logs[0], "constellation.item/v9") {
		t.Fatalf("parts %v, logs %v", parts, logs)
	}
}

func TestTheDemoHasAnItemInEveryActionState(t *testing.T) {
	states := map[string]bool{}
	unknown := false
	for _, e := range Demo().Entries {
		for _, item := range e.Items {
			states[results.ActionState(item)] = true
			s, _ := item["action_status"].(string)
			unknown = unknown || (s != "" && !strings.Contains(" pending approved rejected ", " "+s+" "))
		}
	}
	for _, s := range []string{"", "advice", "approved", "pending", "rejected"} {
		if !states[s] {
			t.Errorf("no item in state %q", s)
		}
	}
	if !unknown {
		t.Error("no item with a status bridges do not know")
	}
}

// A custom bridge that passes conformance has seen every item shape the API sends.
func TestTheDemoHasAnRFIRerouteAndAnAdvisoryWithNoAction(t *testing.T) {
	reroute, advisory := false, false
	for _, e := range Demo().Entries {
		for _, item := range e.Items {
			action, _ := item["action"].(map[string]any)
			reroute = reroute || (e.Template == "rfi-watch" && action["verb"] == "reroute")
			_, noted := item["note"].(string)
			advisory = advisory || (noted && action == nil && item["action_status"] == nil)
		}
	}
	if !reroute || !advisory {
		t.Fatalf("rfi-watch reroute %v, advisory with a note and no action %v", reroute, advisory)
	}
}

func TestPhasesHoldEntriesBack(t *testing.T) {
	m := New(Demo())
	m.SetPhase(1)
	page, _ := serve(t, m).Results("", "", 20, 0)
	if len(page.Entries) != 4 {
		t.Fatalf("%d entries in phase 1", len(page.Entries))
	}
}

func TestCheckInsAndReportsAreValidatedAsTheAPIDoes(t *testing.T) {
	m := New(Demo())
	m.FailFirstPoll = true
	c := serve(t, m)
	for _, body := range []map[string]any{
		{"consumer": "b", "system": "custom"},
		{"consumer": "b", "system": "yamcs", "manifest": map[string]any{"name": "b"}},
		{"consumer": "b", "system": "custom", "manifest": map[string]any{"name": "c"}},
		{"consumer": "b", "system": "openmct"},
		{"consumer": "b", "system": "yamcs", "secret": "x"},
	} {
		if err := c.Call("POST", "/predictions/feed/checkin", body, nil); status(err) != 422 {
			t.Errorf("%v: %v, want 422", body, err)
		}
	}
	if err := c.Call("POST", "/predictions/feed/checkin", map[string]any{"consumer": "b", "system": "yamcs"}, nil); err != nil {
		t.Error(err)
	}
	e := Demo().Entries[0]
	report := map[string]any{"consumer": "b", "run_id": e.RunID, "cursor": e.Cursor(), "applied": map[string]int{"passes": 3}}
	if err := c.Call("POST", "/predictions/feed/deliveries", report, nil); err != nil {
		t.Error(err)
	}
	report["run_id"] = "run_20000101T000000_00000000"
	if err := c.Call("POST", "/predictions/feed/deliveries", report, nil); status(err) != 404 {
		t.Errorf("unknown run: %v", err)
	}
	report["run_id"], report["applied"] = e.RunID, map[string]float64{"passes": 1.5}
	if err := c.Call("POST", "/predictions/feed/deliveries", report, nil); status(err) != 422 {
		t.Errorf("fractional count: %v", err)
	}
	if _, err := c.Results("", "b", 20, 0); status(err) != 503 {
		t.Errorf("first poll: %v, want 503", err)
	}
	if _, err := c.Results("", "b", 20, 0); err != nil {
		t.Errorf("second poll: %v", err)
	}
}

// olderAPI is the mock as an API before contract 1.5.0: it ignores from=latest.
type olderAPI struct{ *Mock }

func (o olderAPI) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	q.Del("from")
	r.URL.RawQuery = q.Encode()
	o.Mock.ServeHTTP(w, r)
}

func TestAFirstStartAsksOnceWhereToReadFrom(t *testing.T) {
	for _, phase := range []int{0, 1, 2} {
		m := New(Demo())
		m.SetPhase(phase)
		if _, err := results.StartCursor(serve(t, m), "me"); err != nil {
			t.Fatal(err)
		}
		if len(m.Requests()) != 1 || m.Requests()[0].Query.Get("from") != "latest" {
			t.Fatalf("phase %d: %d requests, want one from=latest", phase, len(m.Requests()))
		}
	}
}

func TestAFirstStartFailsPlainlyAgainstAnAPIThatIgnoresFromLatest(t *testing.T) {
	old := New(Demo())
	server := httptest.NewServer(olderAPI{old})
	t.Cleanup(server.Close)
	_, err := results.StartCursor(&results.Client{URL: server.URL, HTTP: server.Client()}, "me")
	if err == nil || !strings.Contains(err.Error(), "contract 1.5.0 or later") || len(old.Requests()) != 1 {
		t.Fatalf("err %v after %d requests, want the minimum contract named after one", err, len(old.Requests()))
	}
}
