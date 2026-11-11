package results

import (
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func approval(decision string, year string) Item {
	item := Item{
		"entity":        map[string]any{"type": "pass", "id": "p"},
		"target_at":     year + "-01-01T00:00:00+00:00",
		"until":         year + "-01-01T01:00:00+00:00",
		"action":        map[string]any{"verb": "swap", "mode": "approve", "to": "q", "deadline": year + "-01-01T00:30:00+00:00"},
		"action_status": "approved",
	}
	if decision != "" {
		item["decision_id"] = decision
	}
	return item
}

// actsOn is a bridge whose state is kept in `dir` and the items Act was handed, in order.
func actsOn(dir string) (*Bridge, *[]Item) {
	var acted []Item
	b := &Bridge{Client: &Client{}, CursorFile: filepath.Join(dir, "cursor.txt"), Log: func(string) {}, Act: func(item Item, _ map[string]any, _ Run) (bool, error) {
		acted = append(acted, item)
		return true, nil
	}}
	b.load()
	return b, &acted
}

func TestAStandingApprovalIsActedOnOnceAcrossRunsAndARestart(t *testing.T) {
	dir := t.TempDir()
	b, acted := actsOn(dir)
	for range 2 {
		if _, err := b.act(approval("d1", "2099"), nil, Run{}); err != nil {
			t.Fatal(err)
		}
	}
	restarted, actedAfter := actsOn(dir)
	if done, err := restarted.act(approval("d1", "2099"), nil, Run{}); done || err != nil {
		t.Fatalf("acted again after a restart: %v %v", done, err)
	}
	if len(*acted) != 1 || len(*actedAfter) != 0 {
		t.Fatalf("acted %d times, then %d", len(*acted), len(*actedAfter))
	}
	raw, _ := os.ReadFile(filepath.Join(dir, "cursor.txt.acted"))
	if string(raw) != "d1\t2099-01-08T01:00:00Z\n" {
		t.Fatalf("acted file: %q", raw)
	}
}

func TestANewApprovalOfTheSameActionIsActedOnAgain(t *testing.T) {
	b, acted := actsOn(t.TempDir())
	b.act(approval("d1", "2099"), nil, Run{})
	if done, _ := b.act(approval("d2", "2099"), nil, Run{}); !done || len(*acted) != 2 {
		t.Fatalf("a new decision was not acted on: %v, %d", done, len(*acted))
	}
}

func TestWithoutADecisionIDAnActionIsKeyedByEntityVerbAndTarget(t *testing.T) {
	b, acted := actsOn(t.TempDir())
	b.act(approval("", "2099"), nil, Run{})
	b.act(approval("", "2099"), nil, Run{})
	other := approval("", "2099")
	other["action"].(map[string]any)["to"] = "r"
	b.act(other, nil, Run{})
	if len(*acted) != 2 {
		t.Fatalf("acted %d times, want 2", len(*acted))
	}
	none := Item{"entity": map[string]any{"id": "p"}, "action": map[string]any{"verb": "swap", "to": nil}}
	if got := actionKey(none); got != "p|swap|" {
		t.Errorf("a swap with no backup: got %q", got)
	}
}

func TestAnApprovalIsForgottenAWeekAfterItsActionIsOverAndALiveOneKept(t *testing.T) {
	dir := t.TempDir()
	stale := "old\t2020-01-08T00:00:00Z\nlive\t2099-01-08T00:00:00Z\nkept\t\n"
	if err := os.WriteFile(filepath.Join(dir, "cursor.txt.acted"), []byte(stale), 0o644); err != nil {
		t.Fatal(err)
	}
	b, _ := actsOn(dir)
	if _, ok := b.acted["old"]; ok || len(b.acted) != 2 {
		t.Fatalf("after load: %v", b.acted)
	}
	b.act(approval("d1", "2099"), nil, Run{})
	raw, _ := os.ReadFile(filepath.Join(dir, "cursor.txt.acted"))
	if string(raw) != "d1\t2099-01-08T01:00:00Z\nkept\t\nlive\t2099-01-08T00:00:00Z\n" {
		t.Fatalf("acted file: %q", raw)
	}
}

func TestTheLatestOfThePassEndAndTheCancelDeadlineSetsWhenAKeyIsForgotten(t *testing.T) {
	late := approval("d1", "2099")
	late["action"].(map[string]any)["deadline"] = "2099-02-01T00:00:00Z"
	if got := actedUntil(late); got != "2099-02-08T00:00:00Z" {
		t.Errorf("got %q", got)
	}
	if got := actedUntil(Item{"target_at": "soon"}); got != "" {
		t.Errorf("no time: got %q", got)
	}
}

func TestAnApprovalPastItsCancelDeadlineIsNeverActedOn(t *testing.T) {
	b, acted := actsOn(t.TempDir())
	if done, err := b.act(approval("d1", "2020"), nil, Run{}); done || err != nil || len(*acted) != 0 {
		t.Fatalf("acted on a closed action: %v %v %d", done, err, len(*acted))
	}
}

func TestAnApprovalOfALongPastPassIsActedOnAtMostOnce(t *testing.T) {
	b, acted := actsOn(t.TempDir())
	old := approval("d-old", "2020")
	delete(old["action"].(map[string]any), "deadline")
	for range 3 {
		if _, err := b.act(old, nil, Run{}); err != nil {
			t.Fatal(err)
		}
	}
	if len(*acted) != 1 {
		t.Fatalf("acted %d times, want 1", len(*acted))
	}
}

func feedEntry(cursor string) map[string]any {
	str := func(s string) map[string]any { return map[string]any{"stringValue": s} }
	kv := func(k string, v map[string]any) map[string]any { return map[string]any{"key": k, "value": v} }
	body := map[string]any{"kvlistValue": map[string]any{"values": []any{
		kv("entity", map[string]any{"kvlistValue": map[string]any{"values": []any{kv("id", str("p")), kv("type", str("pass"))}}}),
		kv("target_at", str("2030-01-01T00:00:00+00:00")),
	}}}
	return map[string]any{"cursor": cursor, "otlp": map[string]any{"resourceLogs": []any{map[string]any{
		"resource": map[string]any{"attributes": []any{kv("constellation.connection", str("b")),
			kv("constellation.run_id", str(strings.SplitN(cursor, "#", 2)[1]))}},
		"scopeLogs": []any{map[string]any{"logRecords": []any{map[string]any{
			"body": body, "attributes": []any{kv("constellation.schema", str("constellation.item/v2"))}}}}},
	}}}}
}

// feed serves `cursors` as the API does; answer may override the answer to a request.
func feed(t *testing.T, cursors []string, answer func(w http.ResponseWriter, r *http.Request) bool) (*Bridge, *int) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if answer != nil && answer(w, r) {
			return
		}
		if r.Method == "POST" {
			w.WriteHeader(204)
			return
		}
		var found []any
		for _, c := range cursors {
			if c > r.URL.Query().Get("after") && len(found) == 0 {
				found = append(found, feedEntry(c))
			}
		}
		json.NewEncoder(w).Encode(map[string]any{"entries": found, "next": ""})
	}))
	t.Cleanup(server.Close)
	applies := 0
	b := &Bridge{Manifest: map[string]any{"name": "b"}, Client: &Client{URL: server.URL, HTTP: server.Client()},
		CursorFile: filepath.Join(t.TempDir(), "cursor.txt"), Backfill: true, Log: func(string) {},
		Apply: func([]Item, map[string]any, Run) (map[string]int, error) {
			applies++
			return map[string]int{"passes": 1}, nil
		}}
	return b, &applies
}

func TestAFailedReportIsRetriedWithoutApplyingAgain(t *testing.T) {
	failed := false
	b, applies := feed(t, []string{"1#run_1"}, func(w http.ResponseWriter, r *http.Request) bool {
		if r.Method == "POST" && !failed {
			failed = true
			w.WriteHeader(400)
			return true
		}
		return false
	})
	if err := b.Poll(); err == nil {
		t.Fatal("a failed report did not fail the poll")
	}
	if err := b.Poll(); err != nil {
		t.Fatal(err)
	}
	if *applies != 1 || b.cursor != "1#run_1" {
		t.Fatalf("applied %d times, cursor %q", *applies, b.cursor)
	}
}

func TestARunAReplayingFeedDeliversAgainIsNotAppliedAgain(t *testing.T) {
	replayed := false
	b, applies := feed(t, []string{"1#run_1", "2#run_2"}, func(w http.ResponseWriter, r *http.Request) bool {
		if r.URL.Query().Get("after") == "2#run_2" && !replayed {
			replayed = true
			json.NewEncoder(w).Encode(map[string]any{"entries": []any{feedEntry("1#run_1")}, "next": ""})
			return true
		}
		return false
	})
	for range 2 {
		if err := b.Poll(); err != nil {
			t.Fatal(err)
		}
	}
	if *applies != 2 || !replayed {
		t.Fatalf("applied %d times (replayed %v)", *applies, replayed)
	}
}

func TestAPollReadsOnThroughAReplayToTheNewRuns(t *testing.T) {
	replayed := false
	b, applies := feed(t, []string{"1#run_1", "2#run_2", "3#run_3"}, func(w http.ResponseWriter, r *http.Request) bool {
		if r.URL.Query().Get("after") == "2#run_2" && !replayed {
			replayed = true
			json.NewEncoder(w).Encode(map[string]any{"entries": []any{feedEntry("1#run_1")}, "next": "1#run_1"})
			return true
		}
		return false
	})
	if err := os.WriteFile(b.CursorFile, []byte("2#run_2"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := b.Poll(); err != nil {
		t.Fatal(err)
	}
	if *applies != 1 || b.cursor != "3#run_3" {
		t.Fatalf("applied %d, cursor %q: one poll stopped at the replay", *applies, b.cursor)
	}
}

func TestAMalformedItemIsSkippedAndCountedAndItsRunGoesOn(t *testing.T) {
	var reports []map[string]any
	var handed []int
	b, _ := feed(t, []string{"1#run_1"}, func(w http.ResponseWriter, r *http.Request) bool {
		if r.Method == "POST" {
			var body map[string]any
			json.NewDecoder(r.Body).Decode(&body)
			reports = append(reports, body["applied"].(map[string]any))
			w.WriteHeader(204)
			return true
		}
		if r.URL.Query().Get("after") == "" {
			e := feedEntry("1#run_1")
			logs := e["otlp"].(map[string]any)["resourceLogs"].([]any)[0].(map[string]any)
			scope := logs["scopeLogs"].([]any)[0].(map[string]any)
			good := scope["logRecords"].([]any)[0].(map[string]any)
			bad := map[string]any{"body": map[string]any{"stringValue": "not an item"}, "attributes": good["attributes"]}
			scope["logRecords"] = []any{bad, good}
			json.NewEncoder(w).Encode(map[string]any{"entries": []any{e}, "next": "1#run_1"})
			return true
		}
		return false
	})
	b.Apply = func(items []Item, _ map[string]any, _ Run) (map[string]int, error) {
		handed = append(handed, len(items))
		return map[string]int{"passes": len(items)}, nil
	}
	if err := b.Poll(); err != nil {
		t.Fatal(err)
	}
	if len(handed) != 1 || handed[0] != 1 || len(reports) != 1 || reports[0]["malformed"] != float64(1) || b.cursor != "1#run_1" {
		t.Fatalf("handed %v, reported %v, cursor %q", handed, reports, b.cursor)
	}
}

func TestMalformedNamesWhatIsWrong(t *testing.T) {
	good := Item{"entity": map[string]any{"id": "p", "type": "pass"}, "target_at": "2030-01-01T00:00:00Z"}
	if why := Malformed(good); why != "" {
		t.Fatal(why)
	}
	for want, item := range map[string]Item{
		"entity":          {"target_at": "2030-01-01T00:00:00Z"},
		"target_at":       {"entity": map[string]any{"id": "p", "type": "pass"}, "target_at": "soon"},
		"action.deadline": {"entity": map[string]any{"id": "p", "type": "pass"}, "target_at": "2030-01-01T00:00:00Z", "action": map[string]any{"deadline": 5}},
	} {
		if why := Malformed(item); !strings.Contains(why, want) {
			t.Errorf("%s: %q", want, why)
		}
	}
}

func TestRunForeverChecksInAgainEveryCheckInEvery(t *testing.T) {
	checkins := 0
	b, _ := feed(t, nil, func(w http.ResponseWriter, r *http.Request) bool {
		if r.URL.Path != "/predictions/feed/checkin" {
			return false
		}
		if checkins++; checkins == 3 {
			w.WriteHeader(401)
		}
		return true
	})
	defer func(every time.Duration) { CheckInEvery = every }(CheckInEvery)
	CheckInEvery, b.PollEvery = time.Millisecond, time.Millisecond
	done := make(chan error)
	go func() { done <- b.RunForever() }()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("it never checked in a third time")
	}
}

func TestAnApprovalCarriedOutIsReportedActed(t *testing.T) {
	var reports []map[string]any
	b, _ := feed(t, []string{"1#run_1"}, func(w http.ResponseWriter, r *http.Request) bool {
		if r.Method == "POST" && r.URL.Path == "/predictions/feed/deliveries" {
			var body map[string]any
			json.NewDecoder(r.Body).Decode(&body)
			reports = append(reports, body["applied"].(map[string]any))
			w.WriteHeader(204)
			return true
		}
		return false
	})
	b.Act = func(Item, map[string]any, Run) (bool, error) { return true, nil }
	b.load()
	if _, err := b.act(approval("d0", "2099"), nil, Run{}); err != nil {
		t.Fatal(err)
	}
	b.Apply = func([]Item, map[string]any, Run) (map[string]int, error) { return map[string]int{}, nil }
	b.unreported = map[string][]written{}
	if err := b.applyPart("1#run_1", 0, Part{Items: []Item{approval("d1", "2099"), approval("d0", "2099")}}); err != nil {
		t.Fatal(err)
	}
	if len(reports) != 1 || reports[0]["acted"] != float64(1) {
		t.Fatalf("reported %v", reports)
	}
}

func TestABridgeThatCannotKeepItsStateStopsInsteadOfRepeatingItsWork(t *testing.T) {
	b, applies := feed(t, []string{"1#run_1"}, nil)
	b.CursorFile = filepath.Join(t.TempDir(), "missing", "cursor.txt")
	b.PollEvery = time.Millisecond
	done := make(chan error)
	go func() { done <- b.RunForever() }()
	select {
	case err := <-done:
		if !errors.Is(err, ErrState) || *applies != 1 {
			t.Fatalf("%v, applied %d times", err, *applies)
		}
	case <-time.After(5 * time.Second):
		t.Fatalf("it went on polling, applied %d times", *applies)
	}
	b.Act = func(Item, map[string]any, Run) (bool, error) { return true, nil }
	if _, err := b.act(approval("d1", "2099"), nil, Run{}); !errors.Is(err, ErrState) {
		t.Fatalf("the approvals carried out were not kept and it went on: %v", err)
	}
}

func TestACursorReadFromAnotherAPIIsNeverResumed(t *testing.T) {
	mock, _ := feed(t, []string{"1#run_1"}, nil)
	if err := mock.Poll(); err != nil {
		t.Fatal(err)
	}
	prod := &Bridge{Manifest: mock.Manifest, Client: &Client{URL: "https://api.constellation.space", Key: "k"}, CursorFile: mock.CursorFile, Log: func(string) {}}
	err := prod.RunForever()
	if err == nil || !strings.Contains(err.Error(), "where this bridge read "+Origin(mock.Client.URL)+" up to, and it now reads https://api.constellation.space") {
		t.Fatalf("%v", err)
	}
	same := &Bridge{Client: &Client{URL: strings.ToUpper(mock.Client.URL) + "/"}, CursorFile: mock.CursorFile}
	if err := same.load(); err != nil || same.cursor != "1#run_1" {
		t.Fatalf("the same API spelled another way: %v %q", err, same.cursor)
	}
}

func TestACursorOlderThanTheFeedHoldsIsLogged(t *testing.T) {
	b, _ := feed(t, nil, func(w http.ResponseWriter, r *http.Request) bool {
		w.Write([]byte(`{"entries": [], "next": "", "oldest": "5#e"}`))
		return true
	})
	var logs []string
	b.Log = func(m string) { logs = append(logs, m) }
	for _, cursor := range []string{"1#a", "6#f"} {
		b.cursor, b.started, b.acted = cursor, true, map[string]string{}
		if err := b.Poll(); err != nil {
			t.Fatal(err)
		}
	}
	if len(logs) != 1 || !strings.Contains(logs[0], "no longer holds cursor 1#a") {
		t.Fatalf("%q", logs)
	}
}

func TestWithNoKeyRunForeverStopsAtOnceExceptAgainstTheMock(t *testing.T) {
	b := &Bridge{Manifest: map[string]any{"name": "b"}, Client: &Client{URL: "https://api.constellation.space"},
		CursorFile: filepath.Join(t.TempDir(), "cursor.txt"), Log: func(string) {}}
	if err := b.RunForever(); err == nil || !strings.Contains(err.Error(), "CONSTELLATION_API_KEY is not set") {
		t.Fatalf("%v", err)
	}
	mock, _ := feed(t, nil, func(w http.ResponseWriter, r *http.Request) bool {
		w.WriteHeader(401)
		return true
	})
	if err := mock.RunForever(); err == nil || strings.Contains(err.Error(), "is not set") {
		t.Fatalf("the mock was not polled: %v", err)
	}
}
