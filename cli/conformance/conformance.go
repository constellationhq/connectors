// Package conformance checks a bridge in any language from the outside: it runs the bridge
// against the mock, kills it, restarts it, and judges what it asked the API, what it
// reported, and what it logged to CONSTELLATION_CONFORMANCE_LOG: one JSON line
// {"item_id", "state", "acted", "verb"} per item applied, written after the run is reported and
// before the cursor moves. state is forecast, advice, approved, pending or rejected (an unknown
// action_status is pending); acted is true only for an approved action carried out just then;
// verb is the action's verb, or null.
package conformance

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"slices"
	"sort"
	"strings"
	"syscall"
	"time"

	"github.com/constellationhq/connectors/cli/mock"
	"github.com/constellationhq/connectors/cli/results"
)

const Key = "conformance-key"

type Options struct {
	Command []string
	// Port is the mock's port on 127.0.0.1; 0 picks a free one.
	Port int
	// Timeout is how long each of the two runs of the bridge has to catch up with the feed.
	Timeout time.Duration
	// Settings replace the manifest's defaults in the feed outputs the bridge gets.
	Settings map[string]any
	// Output gets the bridge's stdout and stderr.
	Output io.Writer
}

type Check struct {
	Name   string `json:"name"`
	OK     bool   `json:"ok"`
	Detail string `json:"detail,omitempty"`
}

type Result struct {
	Passed bool    `json:"passed"`
	Checks []Check `json:"checks"`
}

// Run checks the bridge `opts.Command`. The error is for what kept the check from running
// (the port is taken, the command does not start), never for a failed check.
func Run(opts Options) (Result, error) {
	if len(opts.Command) == 0 {
		return Result{}, errors.New("give the bridge's command after --")
	}
	if opts.Output == nil {
		opts.Output = io.Discard
	}
	scenario := withHistory(mock.Demo())
	m := mock.New(scenario)
	m.SetPhase(0)
	m.FailFirstPoll, m.MaxPageRecords, m.Settings, m.ReplayOnce = true, 1, opts.Settings, true
	listener, err := net.Listen("tcp", fmt.Sprintf("127.0.0.1:%d", opts.Port))
	if err != nil {
		return Result{}, fmt.Errorf("the mock cannot listen: %w", err)
	}
	server := &http.Server{Handler: m}
	go server.Serve(listener)
	defer server.Close()

	dir, err := os.MkdirTemp("", "constellation-conformance-")
	if err != nil {
		return Result{}, err
	}
	defer os.RemoveAll(dir)
	actionLog := filepath.Join(dir, "actions.jsonl")
	env := append(os.Environ(), "CONSTELLATION_API_URL=http://"+listener.Addr().String(),
		"CONSTELLATION_API_KEY="+Key, "CURSOR_FILE="+filepath.Join(dir, "cursor.txt"),
		"POLL_SECONDS=1", "CONSTELLATION_CONFORMANCE_LOG="+actionLog)
	last := func(phase int) string {
		cursor := ""
		for _, e := range scenario.Entries {
			if e.Phase <= phase {
				cursor = e.Cursor()
			}
		}
		return cursor
	}

	// The feed first holds only history; the runs after it are published once the bridge has
	// applied the newest of it (or a while has passed), as runs arrive after a bridge starts.
	published := make(chan struct{})
	go func() {
		defer m.SetPhase(1)
		waited := time.After(opts.Timeout / 4)
		for !reportedRun(m.Requests(), historyHead) {
			select {
			case <-published:
				return
			case <-waited:
				return
			case <-time.After(50 * time.Millisecond):
			}
		}
	}()
	exitedFirst, stuck, err := runUntil(opts, env, m, 0, last(1), 1)
	close(published)
	if err != nil {
		return Result{}, err
	}
	restartAt, actedBefore := len(m.Requests()), len(readLog(actionLog).lines)
	m.SetPhase(2)
	// The feed replays once from its start when the bridge has read it all: the bridge asks for
	// what follows the last run once more after it. A bridge that exited or got stuck is not
	// run again: the checks that need the restart fail.
	exitedSecond := false
	if !exitedFirst && !stuck {
		if exitedSecond, _, err = runUntil(opts, env, m, restartAt, last(2), 2); err != nil {
			return Result{}, err
		}
	}

	checks := judge(scenario, m.Requests(), restartAt, readLog(actionLog), actedBefore, last(1), last(2))
	running := Check{Name: "keeps running until it is stopped", OK: !exitedFirst && !exitedSecond}
	if !running.OK {
		running.Detail = "it exited on its own"
	}
	checks = append(checks, running)
	result := Result{Passed: true, Checks: checks}
	for _, c := range checks {
		result.Passed = result.Passed && c.OK
	}
	return result, nil
}

const (
	historyOld  = "run_20300114T055800_0a1b2c3d"
	historyHead = "run_20300114T065800_4e5f6a7b"
	expiredRun  = "run_20300115T064900_8c9d0e1f"
	expiredItem = "pass_expired_approval"
)

// withHistory adds what a bridge finds in the feed on its first start (phase 0): an older run
// with an approval in it, never to be applied, and the newest run, to be applied. And a run
// with an approval past its cancel deadline, never to be acted on.
func withHistory(s mock.Scenario) mock.Scenario {
	pass := func(id string, extra map[string]any) map[string]any {
		item := map[string]any{
			"entity":    map[string]any{"type": "pass", "id": id, "satellite_id": "norad-43013", "antenna_id": "SITE-0001__ant0"},
			"target_at": "2030-01-14T10:00:00+00:00", "until": "2030-01-14T10:10:00+00:00",
			"value": map[string]any{"p10": json.Number("1.5"), "p50": json.Number("2.5"), "p90": json.Number("3.5")},
		}
		for k, v := range extra {
			item[k] = v
		}
		return item
	}
	approved := func(deadline string) map[string]any {
		return map[string]any{"action_status": "approved", "decision_id": "history-" + deadline[:4],
			"action": map[string]any{"verb": "swap", "mode": "approve", "to": "pass_backup", "deadline": deadline}}
	}
	window := [2]string{"2030-01-14T06:00:00+00:00", "2030-01-15T06:00:00+00:00"}
	history := []mock.Entry{
		{Phase: 0, Published: "20300114T060000.000000", RunID: historyOld, Workflow: "ka-rain-fade-swap", Template: "rain-fade-swap",
			Window: window, Items: []map[string]any{pass("pass_history_approval", approved("2030-01-14T09:45:00+00:00"))}},
		{Phase: 0, Published: "20300114T070000.000000", RunID: historyHead, Workflow: "noaa-pass-yield", Template: "pass-yield",
			Window: window, Items: []map[string]any{pass("pass_history_head", nil)}},
		{Phase: 1, Published: "20300115T065000.000000", RunID: expiredRun, Workflow: "ka-rain-fade-swap", Template: "rain-fade-swap",
			Window: window, Items: []map[string]any{pass(expiredItem, approved("2020-01-01T00:00:00+00:00"))}},
	}
	s.Entries = append(history, s.Entries...)
	sort.SliceStable(s.Entries, func(i, j int) bool { return s.Entries[i].Cursor() < s.Entries[j].Cursor() })
	return s
}

func reportedRun(reqs []mock.Request, run string) bool {
	for _, r := range reqs {
		if r.Method == "POST" && r.Path == "/predictions/feed/deliveries" && str(r.Body, "run_id") == run {
			return true
		}
	}
	return false
}

// stuckAfter is how many times running a bridge may ask for the same results, or check in, without
// applying anything before it counts as stuck (every poll fails) and is stopped early.
const stuckAfter = 5

// runUntil runs the bridge until it asks for the feed after `cursor` `times` times (it has
// applied everything up to there), it exits, it is stuck, or the timeout passes, then kills it
// with its children. It reports whether the bridge exited on its own, and whether it was stuck.
func runUntil(opts Options, env []string, m *mock.Mock, from int, cursor string, times int) (exited, stuck bool, err error) {
	cmd := exec.Command(opts.Command[0], opts.Command[1:]...)
	cmd.Env, cmd.Stdout, cmd.Stderr = env, opts.Output, opts.Output
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	if err := cmd.Start(); err != nil {
		return false, false, fmt.Errorf("the bridge does not start: %w", err)
	}
	done := make(chan struct{})
	go func() { cmd.Wait(); close(done) }()
	deadline := time.After(opts.Timeout)
	for {
		select {
		case <-done:
			fmt.Fprintln(opts.Output, "conformance: the bridge exited")
			return true, false, nil
		case <-deadline:
			kill(cmd, done)
			return false, false, nil
		case <-time.After(50 * time.Millisecond):
		}
		asked, same, checkins, after := 0, 0, 0, "\x00"
		for _, r := range m.Requests()[from:] {
			switch {
			case r.Method == "POST" && r.Path == "/predictions/feed/deliveries":
				same, checkins = 0, 0
			case r.Method == "POST" && r.Path == "/predictions/feed/checkin":
				checkins++
			case isPull(r):
				checkins = 0
				if a := r.Query.Get("after"); a == after {
					same++
				} else {
					after, same = a, 1
				}
				if after == cursor {
					asked++
				}
			}
		}
		if asked >= times {
			kill(cmd, done)
			return false, false, nil
		}
		if (same >= stuckAfter && after != cursor) || checkins >= stuckAfter {
			kill(cmd, done)
			fmt.Fprintf(opts.Output, "conformance: stopped the bridge early: it asked for the results after %q or checked in %d times in a row without applying a run\n", after, stuckAfter)
			return false, true, nil
		}
	}
}

func kill(cmd *exec.Cmd, exited chan struct{}) {
	_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
	<-exited
}

// isPull is a read of the feed after a cursor, as against a read of one run's page.
func isPull(r mock.Request) bool {
	return r.Method == "GET" && r.Path == "/predictions/feed" && !r.Query.Has("cursor")
}

type logLine struct {
	itemID, state, verb string
	acted               bool
}

type actionLog struct {
	missing bool
	bad     []string
	lines   []logLine
}

var states = map[string]bool{"forecast": true, "advice": true, "approved": true, "pending": true, "rejected": true}

// parseLine reads one line of the conformance log,
// {"item_id", "state", "acted", "verb"}, or says which field is wrong.
func parseLine(text string) (logLine, string) {
	var raw map[string]any
	if err := json.Unmarshal([]byte(text), &raw); err != nil {
		return logLine{}, "not a JSON object"
	}
	var l logLine
	var ok bool
	if l.itemID, ok = raw["item_id"].(string); !ok || l.itemID == "" {
		return l, `"item_id" must be the item's entity.id`
	}
	if l.state, _ = raw["state"].(string); !states[l.state] {
		return l, `"state" must be forecast, advice, approved, pending or rejected (an unknown action_status is pending)`
	}
	if l.acted, ok = raw["acted"].(bool); !ok {
		return l, `"acted" must be true or false`
	}
	verb, has := raw["verb"]
	if l.verb, ok = verb.(string); !has || (verb != nil && !ok) {
		return l, `"verb" must be the action's verb, or null for an item with no action`
	}
	for k := range raw {
		if k != "item_id" && k != "state" && k != "acted" && k != "verb" {
			return l, fmt.Sprintf("unknown field %q", k)
		}
	}
	return l, ""
}

func readLog(path string) actionLog {
	f, err := os.Open(path)
	if err != nil {
		return actionLog{missing: true}
	}
	defer f.Close()
	var log actionLog
	scanner := bufio.NewScanner(f)
	scanner.Buffer(nil, 1<<20)
	for scanner.Scan() {
		text := strings.TrimSpace(scanner.Text())
		if text == "" {
			continue
		}
		line, problem := parseLine(text)
		if problem != "" {
			log.bad = append(log.bad, fmt.Sprintf("%s: %s", text, problem))
			continue
		}
		log.lines = append(log.lines, line)
	}
	return log
}

func str(m map[string]any, k string) string {
	s, _ := m[k].(string)
	return s
}

func judge(scenario mock.Scenario, reqs []mock.Request, restartAt int, log actionLog, actedBefore int, lastFirst, lastAll string) []Check {
	var checkins, deliveries []map[string]any
	var pulls []mock.Request
	for _, r := range reqs {
		switch {
		case r.Method == "POST" && r.Path == "/predictions/feed/checkin":
			checkins = append(checkins, r.Body)
		case r.Method == "POST" && r.Path == "/predictions/feed/deliveries":
			deliveries = append(deliveries, r.Body)
		case r.Method == "GET" && r.Path == "/predictions/feed":
			pulls = append(pulls, r)
		}
	}
	var checks []Check
	add := func(ok bool, name, detail string) {
		if ok {
			detail = ""
		}
		checks = append(checks, Check{name, ok, detail})
	}
	// What a bridge must never do passes only once it has applied a run, never by doing nothing.
	never := func(ok bool, name, detail string) {
		if len(deliveries) == 0 {
			ok, detail = false, "it applied no run, so there is nothing to judge"
		}
		add(ok, name, detail)
	}

	checkin := map[string]any{}
	if len(checkins) > 0 {
		checkin = checkins[0]
	}
	system, name := str(checkin, "system"), str(checkin, "consumer")
	if system == "custom" || system == "" {
		manifest, _ := checkin["manifest"].(map[string]any)
		problems := manifestProblems(manifest)
		if len(checkins) == 0 {
			problems = []string{"it never checked in"}
		}
		add(system == "custom" && len(problems) == 0, "checks in as a custom connector with a valid manifest", strings.Join(problems, "; "))
		add(name != "" && name == str(manifest, "name"), "checks in with consumer equal to the manifest's name",
			fmt.Sprintf("consumer %q, manifest name %q", name, str(manifest, "name")))
	} else {
		add(name != "", "checks in as its system ("+system+")", "it checked in with no consumer")
	}

	consumers := map[string]bool{}
	for _, p := range pulls {
		consumers[p.Query.Get("consumer")] = true
	}
	add(len(pulls) > 0 && len(consumers) == 1 && consumers[name], "pulls its results as its own connection",
		fmt.Sprintf("pulled as %v", sortedKeys(consumers)))
	add(len(pulls) > 1, "keeps polling after the first poll is answered 503", fmt.Sprintf("%d poll(s)", len(pulls)))

	reported := map[string]bool{}
	var reportedList, twice []string
	byCursor := map[string]int{}
	good := len(deliveries) > 0
	for _, d := range deliveries {
		run := str(d, "run_id")
		if byCursor[str(d, "cursor")]++; byCursor[str(d, "cursor")] == 2 {
			twice = append(twice, str(d, "cursor"))
		}
		reported[run] = true
		reportedList = append(reportedList, run)
		applied, ok := d["applied"].(map[string]any)
		ok = ok && len(applied) <= 20
		for _, n := range applied {
			num, isNum := n.(json.Number)
			_, err := num.Int64()
			ok = ok && isNum && err == nil
		}
		good = good && ok && str(d, "consumer") == name && run != "" && strings.HasSuffix(str(d, "cursor"), "#"+run)
	}
	var missed, newer []string
	for _, e := range scenario.Entries {
		if e.Phase > 0 && e.Schema == "" && !reported[e.RunID] && !slices.Contains(missed, e.RunID) {
			missed = append(missed, e.RunID)
		}
		if e.Schema != "" && reported[e.RunID] {
			newer = append(newer, e.RunID)
		}
	}
	add(len(missed) == 0, "consumes every forecast run and reports it applied",
		fmt.Sprintf("never reported %v (reported %v)", missed, reportedList))
	add(good, "names its consumer, the run, the cursor and what it applied (up to 20 whole-number counts) in each report",
		fmt.Sprintf("reports: %v", deliveries))
	never(len(newer) == 0, "never reports a run in an item shape it was not built for", fmt.Sprintf("reported %v", newer))
	never(len(twice) == 0, "applies each run once: one the feed delivers again (a replay from an older cursor) is skipped",
		fmt.Sprintf("reported %v more than once", twice))
	// The mock replays from its start the first time the bridge asks for what follows the last run.
	var asked []string
	for _, p := range pulls {
		if isPull(p) {
			asked = append(asked, p.Query.Get("after"))
		}
	}
	replay := slices.Index(asked, lastAll)
	add(replay >= 0 && replay+1 < len(asked) && asked[replay+1] != lastAll,
		"reads on through a replay: it follows the replayed runs' cursors, never stops at the first",
		fmt.Sprintf("after the replay it asked from %v", asked[min(replay+1, len(asked)):]))
	add(reported[historyHead] && !reported[historyOld], "starts at the newest run on a first start, never the older history",
		fmt.Sprintf("reported %v: the history was %s then %s (the newest)", reportedList, historyOld, historyHead))

	if anyPaged(pulls) {
		pages := map[string]bool{}
		for _, p := range pulls {
			pages[p.Query.Get("cursor")+" "+p.Query.Get("page")] = true
		}
		var unread []string
		for _, e := range scenario.Entries {
			if reported[e.RunID] && e.Schema == "" && len(e.Items) > 1 && !pages[e.Cursor()+" 1"] {
				unread = append(unread, e.Cursor())
			}
		}
		add(len(unread) == 0, "reads every page of a paged run before it reports the run applied",
			fmt.Sprintf("reported without reading page 1 of %v", unread))
	}
	movedOn := false
	for _, p := range pulls {
		movedOn = movedOn || (isPull(p) && p.Query.Get("after") == lastAll)
	}
	add(movedOn, "moves past the last run it applied", "it never asked for results after "+lastAll)

	approvals, seen := expectations(scenario)
	switch {
	case log.missing:
		add(false, "writes the conformance log", `no file at CONSTELLATION_CONFORMANCE_LOG: a bridge appends one JSON line {"item_id", "state", "acted", "verb"} per item it applies (the bridge templates do)`)
	default:
		add(len(log.bad) == 0 && len(log.lines) > 0, "writes the conformance log as JSON lines {item_id, state, acted, verb}",
			fmt.Sprintf("%d line(s); unreadable: %s", len(log.lines), strings.Join(log.bad, "; ")))
	}
	counts := map[string]int{}
	var misread, wrong []string
	for _, l := range log.lines {
		if !seen[l.itemID][l.state] {
			misread = append(misread, fmt.Sprintf("%s as %s", l.itemID, l.state))
		}
		if l.acted {
			counts[l.itemID+" "+l.verb]++
			if l.state != "approved" {
				wrong = append(wrong, fmt.Sprintf("%s %s (%s)", l.itemID, l.verb, l.state))
			}
		}
	}
	never(len(misread) == 0, "reads each item's action state as the API gives it (an unknown status is pending)",
		"logged "+strings.Join(misread, ", "))
	var offCount, decisions []string
	for _, k := range sortedKeys(approvals) {
		a := approvals[k]
		if counts[k] != len(a.decisions) {
			offCount = append(offCount, fmt.Sprintf("%s acted on %d times for %d approval(s)", k, counts[k], len(a.decisions)))
		}
		switch {
		case len(a.decisions) > 1 && counts[k] < len(a.decisions):
			decisions = append(decisions, fmt.Sprintf("%s not acted on again when approved again (a new decision_id)", k))
		case len(a.decisions) == 1 && a.sent > 1 && counts[k] > 1:
			decisions = append(decisions, fmt.Sprintf("%s acted on %d times for one standing approval (the same decision_id)", k, counts[k]))
		}
	}
	add(len(offCount) == 0, "acts on each approved action exactly once", strings.Join(offCount, "; "))
	never(len(decisions) == 0, "acts on a new approval of an action it acted on before, never again on a standing one",
		strings.Join(decisions, "; ")+`: key what was acted on by the result's decision_id, or entity.id|verb|to without one`)
	never(len(wrong) == 0, "never acts on advice, pending, rejected or unknown-status actions, nor on plain forecasts",
		"acted on "+strings.Join(wrong, ", "))
	never(counts[expiredItem+" swap"] == 0, "never acts on an action past its cancel deadline",
		fmt.Sprintf("acted %d time(s) on %s, whose deadline passed in 2020", counts[expiredItem+" swap"], expiredItem))

	var resumed *mock.Request
	for i := restartAt; i < len(reqs); i++ {
		if isPull(reqs[i]) {
			resumed = &reqs[i]
			break
		}
	}
	from := "nothing"
	if resumed != nil {
		from = fmt.Sprintf("%q", resumed.Query.Get("after"))
	}
	add(resumed != nil && resumed.Query.Get("after") == lastFirst, "resumes from its cursor after a restart",
		fmt.Sprintf("after the restart it first asked for results after %s, not %q", from, lastFirst))
	before, after := map[string]bool{}, map[string]int{}
	var again []string
	for i, l := range log.lines {
		if !l.acted {
			continue
		}
		k := l.itemID + " " + l.verb
		if i < actedBefore {
			before[k] = true
			continue
		}
		// Approvals first sent after the restart are new; any other act on k is a repeat.
		if after[k]++; before[k] && (approvals[k] == nil || after[k] > approvals[k].afterRestart) {
			again = append(again, k)
		}
	}
	never(len(again) == 0, "does not act again after a restart on what it acted on before", "acted again on "+strings.Join(again, ", "))
	return checks
}

// approval is what the scenario sends of one approved action: its decisions (decision_id; ""
// for results without one, whose content is their one decision), how many of them are first
// sent after the restart, and in how many entries it is sent.
type approval struct {
	decisions    []string
	afterRestart int
	sent         int
}

// expectations are the approved actions in the scenario, by "item_id verb", and, by item id,
// the states the feed gives it, as a bridge logs them.
func expectations(s mock.Scenario) (map[string]*approval, map[string]map[string]bool) {
	approvals := map[string]*approval{}
	seen := map[string]map[string]bool{}
	for _, e := range s.Entries {
		for _, item := range e.Items {
			line := results.NewLogLine(item, false)
			if seen[line.ItemID] == nil {
				seen[line.ItemID] = map[string]bool{}
			}
			seen[line.ItemID][line.State] = true
			// History before the first start is never applied; a closed action is never acted on.
			if line.State != "approved" || e.Phase == 0 || results.PastDeadline(item) {
				continue
			}
			k := line.ItemID + " " + *line.Verb
			if approvals[k] == nil {
				approvals[k] = &approval{}
			}
			a := approvals[k]
			a.sent++
			if decision := str(item, "decision_id"); !slices.Contains(a.decisions, decision) {
				a.decisions = append(a.decisions, decision)
				if e.Phase > 1 {
					a.afterRestart++
				}
			}
		}
	}
	return approvals, seen
}

func anyPaged(pulls []mock.Request) bool {
	for _, p := range pulls {
		if p.Query.Get("page_records") != "" {
			return true
		}
	}
	return false
}

var settingPattern = regexp.MustCompile(`^[a-z][a-z0-9_]{0,39}$`)

// manifestProblems is why the API would refuse this manifest, nil when it would take it.
func manifestProblems(m map[string]any) []string {
	if m == nil {
		return []string{"no manifest"}
	}
	var out []string
	if !results.NamePattern.MatchString(str(m, "name")) {
		out = append(out, "name must be a lowercase slug (a-z, 0-9, -), at most 63 characters")
	}
	if t, ok := m["title"].(string); !ok || len(t) < 1 || len(t) > 80 {
		out = append(out, "title must be 1 to 80 characters")
	}
	if len(str(m, "description")) > 300 {
		out = append(out, "description is at most 300 characters")
	}
	if extra := unknown(m, "name", "title", "description", "settings"); len(extra) > 0 {
		out = append(out, fmt.Sprintf("the API refuses unknown manifest fields: %v", extra))
	}
	settings, _ := m["settings"].(map[string]any)
	if len(settings) > 20 {
		out = append(out, "at most 20 settings")
	}
	for _, key := range sortedKeys(settings) {
		s, _ := settings[key].(map[string]any)
		if !settingPattern.MatchString(key) {
			out = append(out, fmt.Sprintf("setting %s: keys are lowercase snake_case", key))
		}
		if !slices.Contains([]string{"number", "integer", "boolean", "string", "enum"}, str(s, "type")) {
			out = append(out, fmt.Sprintf("setting %s: type is number, integer, boolean, string or enum", key))
		}
		if _, ok := s["default"]; !ok || str(s, "title") == "" {
			out = append(out, fmt.Sprintf("setting %s: needs a title and a default", key))
		}
		if extra := unknown(s, "type", "title", "description", "default", "minimum", "maximum", "max_length", "options"); len(extra) > 0 {
			out = append(out, fmt.Sprintf("setting %s: the API refuses unknown fields %v", key, extra))
		}
		if len(str(s, "title")) > 80 || len(str(s, "description")) > 300 {
			out = append(out, fmt.Sprintf("setting %s: title at most 80 characters, description at most 300", key))
		}
		if problem := valueProblem(s, s["default"]); problem != "" {
			out = append(out, fmt.Sprintf("setting %s: its default %s", key, problem))
		}
	}
	return out
}

func unknown(m map[string]any, allowed ...string) []string {
	var out []string
	for _, k := range sortedKeys(m) {
		if !slices.Contains(allowed, k) {
			out = append(out, k)
		}
	}
	return out
}

func sortedKeys[V any](m map[string]V) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

func number(v any) (float64, bool) {
	n, ok := v.(json.Number)
	if !ok {
		return 0, false
	}
	f, err := n.Float64()
	return f, err == nil
}

// valueProblem is why `v` is not a valid value of setting `s`, as the API judges it, or "".
func valueProblem(s map[string]any, v any) string {
	switch kind := str(s, "type"); kind {
	case "boolean":
		if _, ok := v.(bool); ok {
			return ""
		}
		return "must be true or false"
	case "number", "integer":
		f, ok := number(v)
		if !ok {
			return "must be a number"
		}
		if kind == "integer" && f != float64(int64(f)) {
			return "must be a whole number"
		}
		if lo, ok := number(s["minimum"]); ok && f < lo {
			return fmt.Sprintf("must be at least %v", s["minimum"])
		}
		if hi, ok := number(s["maximum"]); ok && f > hi {
			return fmt.Sprintf("must be at most %v", s["maximum"])
		}
		return ""
	}
	text, ok := v.(string)
	if !ok {
		return "must be text"
	}
	if str(s, "type") == "enum" {
		options, _ := s["options"].([]any)
		valid, found := len(options) >= 1 && len(options) <= 20, false
		for _, o := range options {
			o, _ := o.(string)
			valid = valid && len(o) >= 1 && len(o) <= 64
			found = found || o == text
		}
		if !valid {
			return "comes from 1 to 20 options of 1 to 64 characters"
		}
		if !found {
			return fmt.Sprintf("must be one of %v", options)
		}
		return ""
	}
	limit := 200
	if n, ok := number(s["max_length"]); ok && n != 0 {
		limit = int(n)
	}
	if limit < 1 || limit > 2000 {
		return "has a max_length from 1 to 2000"
	}
	if len(text) > limit {
		return fmt.Sprintf("must be at most %d characters", limit)
	}
	return ""
}
