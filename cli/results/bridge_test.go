package results

import (
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
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
	if out, err := restarted.act(approval("d1", "2099"), nil, Run{}); out.Outcome != "" || err != nil {
		t.Fatalf("acted again after a restart: %v %v", out, err)
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
	if out, _ := b.act(approval("d2", "2099"), nil, Run{}); out.Outcome != "acted" || len(*acted) != 2 {
		t.Fatalf("a new decision was not acted on: %v, %d", out, len(*acted))
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

func TestTheLatestOfThePassEndAndTheDecideByTimeSetsWhenAKeyIsForgotten(t *testing.T) {
	late := approval("d1", "2099")
	late["action"].(map[string]any)["deadline"] = "2099-02-01T00:00:00Z"
	if got := actedUntil(late); got != "2099-02-08T00:00:00Z" {
		t.Errorf("got %q", got)
	}
	if got := actedUntil(Item{"target_at": "soon"}); got != "" {
		t.Errorf("no time: got %q", got)
	}
}

func TestAnApprovalPastItsDecideByTimeIsNeverActedOn(t *testing.T) {
	b, acted := actsOn(t.TempDir())
	if out, err := b.act(approval("d1", "2020"), nil, Run{}); out.Outcome != "refused" || err != nil || len(*acted) != 0 {
		t.Fatalf("acted on a closed action: %v %v %d", out, err, len(*acted))
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

func TestAManifestsDeprecatedMinP10GbIsReadAsMinGbAndSaidOnce(t *testing.T) {
	b, _ := feed(t, nil, nil)
	var got []map[string]any
	var said []string
	b.Apply = func(_ []Item, settings map[string]any, _ Run) (map[string]int, error) {
		got = append(got, settings)
		return nil, nil
	}
	b.Log = func(m string) {
		if strings.Contains(m, "deprecated") {
			said = append(said, m)
		}
	}
	b.unreported = map[string][]written{}
	for i, output := range []string{`{"min_p10_gb": 5}`, `{"min_p10_gb": 5}`, `{"min_p10_gb": 5, "min_gb": 7}`} {
		part := Part{Resource: map[string]any{"constellation.output": `{"mapping": {"settings": ` + output + `}}`}}
		if err := b.applyPart(string(rune('1'+i))+"#run", 0, part); err != nil {
			t.Fatal(err)
		}
	}
	if got[0]["min_gb"] != 5.0 || got[1]["min_gb"] != 5.0 || got[2]["min_gb"] != 7.0 {
		t.Fatalf("settings %v", got)
	}
	if len(said) != 1 {
		t.Fatalf("said %v", said)
	}
}

// decided is an approval (or another decision) the API sent, with its action and decision ids.
func decided(id, decision, status string) Item {
	item := approval(decision, "2099")
	item["entity"] = map[string]any{"type": "pass", "id": id}
	item["action_id"], item["action_status"] = "a-"+id, status
	return item
}

// reporting is a bridge whose Act answers with answers[entity id] (an error is returned as one),
// and the delivery reports it posted, in order. refuse answers the first report with outcomes.
func reporting(t *testing.T, answers map[string]any, refuse func(w http.ResponseWriter)) (*Bridge, *[]map[string]any) {
	var reports []map[string]any
	b, _ := feed(t, nil, func(w http.ResponseWriter, r *http.Request) bool {
		if r.Method != "POST" || r.URL.Path != "/predictions/feed/deliveries" {
			return false
		}
		var body map[string]any
		json.NewDecoder(r.Body).Decode(&body)
		reports = append(reports, body)
		if _, ok := body["actions"]; ok && refuse != nil {
			refuse(w)
			refuse = nil
			return true
		}
		w.WriteHeader(204)
		return true
	})
	b.Act = func(item Item, _ map[string]any, _ Run) (bool, error) {
		switch a := answers[item["entity"].(map[string]any)["id"].(string)].(type) {
		case error:
			return false, a
		case bool:
			return a, nil
		}
		t.Fatalf("Act asked about %v", item["entity"])
		return false, nil
	}
	b.Apply = func(items []Item, _ map[string]any, _ Run) (map[string]int, error) {
		return map[string]int{"passes": len(items)}, nil
	}
	b.load()
	b.unreported = map[string][]written{}
	return b, &reports
}

func outcomeOf(id, decision, outcome, reason string) map[string]any {
	return map[string]any{"action_id": "a-" + id, "decision_id": decision, "outcome": outcome, "reason": reason}
}

func TestEachApprovalIsReportedActedOrRefusedInOneDeliveryForItsRun(t *testing.T) {
	answers := map[string]any{"ok": true, "no": false, "boom": errors.New("backup antenna is down")}
	b, reports := reporting(t, answers, nil)
	items := []Item{decided("ok", "d1", "approved"), decided("no", "d2", "approved"), decided("boom", "d3", "approved")}
	if err := b.applyPart("1#run_1", 0, Part{Items: items}); err != nil {
		t.Fatal(err)
	}
	want := []any{outcomeOf("ok", "d1", "acted", ""), outcomeOf("no", "d2", "refused", "the bridge did not carry it out"),
		outcomeOf("boom", "d3", "refused", "backup antenna is down")}
	if len(*reports) != 1 || !reflect.DeepEqual((*reports)[0]["actions"], want) {
		t.Fatalf("reported %v", *reports)
	}
	if (*reports)[0]["applied"].(map[string]any)["acted"] != float64(1) {
		t.Fatalf("applied %v", (*reports)[0]["applied"])
	}
}

func TestDeclinedAndPendingActionsAndABridgeWithNoActReportNoOutcome(t *testing.T) {
	b, reports := reporting(t, nil, nil)
	if err := b.applyPart("1#run_1", 0, Part{Items: []Item{decided("x", "d1", "rejected"), decided("y", "d2", "pending")}}); err != nil {
		t.Fatal(err)
	}
	b.Act = nil
	if err := b.applyPart("2#run_2", 0, Part{Items: []Item{decided("z", "d3", "approved")}}); err != nil {
		t.Fatal(err)
	}
	for _, r := range *reports {
		if _, ok := r["actions"]; ok {
			t.Fatalf("reported outcomes: %v", r)
		}
	}
}

func TestAnApprovalSeenAgainIsNeitherActedOnNorReportedAgain(t *testing.T) {
	b, reports := reporting(t, map[string]any{"ok": true, "no": false}, nil)
	for _, cursor := range []string{"1#run_1", "2#run_2"} {
		if err := b.applyPart(cursor, 0, Part{Items: []Item{decided("ok", "d1", "approved"), decided("no", "d2", "approved")}}); err != nil {
			t.Fatal(err)
		}
	}
	b.Act = func(Item, map[string]any, Run) (bool, error) {
		t.Fatal("acted again after a restart")
		return false, nil
	}
	b.load()
	if err := b.applyPart("3#run_3", 0, Part{Items: []Item{decided("ok", "d1", "approved")}}); err != nil {
		t.Fatal(err)
	}
	var counts []int
	for _, r := range *reports {
		list, _ := r["actions"].([]any)
		counts = append(counts, len(list))
	}
	if !reflect.DeepEqual(counts, []int{2, 0, 0}) {
		t.Fatalf("outcomes per report: %v", counts)
	}
}

func TestOutcomesOnADeclinedOrDemoDecisionAreDroppedOnceAndTheDeliveryStillSent(t *testing.T) {
	var logged []string
	b, reports := reporting(t, map[string]any{"ok": true}, func(w http.ResponseWriter) {
		w.WriteHeader(409)
		w.Write([]byte(`{"detail": {"error": "action_not_sent", "action_id": "a-ok"}}`))
	})
	b.Log = func(m string) { logged = append(logged, m) }
	if err := b.applyPart("1#run_1", 0, Part{Items: []Item{decided("ok", "d1", "approved")}}); err != nil {
		t.Fatal(err)
	}
	if len(*reports) != 2 || (*reports)[1]["actions"] != nil {
		t.Fatalf("reports %v", *reports)
	}
	if logged[len(logged)-2] != "Constellation did not take the outcomes of 1 approved action(s) of 1#run_1: one was declined or approved on the demo fleet, so never sent (HTTP 409 action_not_sent). Reported the delivery without them; they are not sent again" {
		t.Fatalf("logged %q", logged)
	}
}

func TestAReportThatFailsIsRetriedWithBackoffAndTheSameOutcomes(t *testing.T) {
	defer func(d time.Duration) { RetryDelay = d }(RetryDelay)
	RetryDelay = time.Millisecond
	fails := 2
	b, reports := reporting(t, map[string]any{"ok": true}, nil)
	inner := b.Client.HTTP.Transport
	b.Client.HTTP.Transport = roundTrip(func(r *http.Request) (*http.Response, error) {
		if r.Method == "POST" && fails > 0 {
			fails--
			return nil, errors.New("connection reset")
		}
		return inner.RoundTrip(r)
	})
	if err := b.applyPart("1#run_1", 0, Part{Items: []Item{decided("ok", "d1", "approved")}}); err != nil {
		t.Fatal(err)
	}
	if len(*reports) != 1 || len((*reports)[0]["actions"].([]any)) != 1 {
		t.Fatalf("reports %v", *reports)
	}
}

type roundTrip func(*http.Request) (*http.Response, error)

func (f roundTrip) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }
