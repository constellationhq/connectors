package conformance

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/constellationhq/connectors/cli/results"
)

const manifest = `{"name": "fake-bridge", "title": "Fake", "version": "0.1.0",
  "settings": {"min_p10_gb": {"type": "number", "title": "Min", "default": 0, "minimum": 0}}}`

// TestMain runs this test binary as a fake bridge when FAKE_BRIDGE names how it behaves.
func TestMain(m *testing.M) {
	if mode := os.Getenv("FAKE_BRIDGE"); mode != "" {
		fakeBridge(mode)
		return
	}
	os.Exit(m.Run())
}

func fakeBridge(mode string) {
	if mode == "crash" {
		os.Exit(1)
	}
	if mode == "forgetful" {
		// A cursor nobody keeps: every start begins again from the start of the feed.
		os.Setenv("CURSOR_FILE", filepath.Join(os.TempDir(), fmt.Sprintf("forgetful-%d", time.Now().UnixNano())))
		// And so reads the whole history again.
		os.Setenv("BACKFILL", "1")
	}
	b, err := results.NewBridge([]byte(manifest))
	if err != nil {
		panic(err)
	}
	b.Apply = func(items []results.Item, _ map[string]any, _ results.Run) (map[string]int, error) {
		if mode == "broken" {
			return nil, fmt.Errorf("the system is down")
		}
		if mode == "content" {
			// Remembers what it acted on by the action's content alone, never by its decision.
			for _, item := range items {
				delete(item, "decision_id")
			}
		}
		if mode == "naive" {
			// Acts on every item that carries an action, whatever its state, and logs it so.
			f, _ := os.OpenFile(os.Getenv("CONSTELLATION_CONFORMANCE_LOG"), os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
			defer f.Close()
			for _, item := range items {
				line := results.NewLogLine(item, item["action"] != nil)
				out, _ := json.Marshal(line)
				f.Write(append(out, '\n'))
			}
		}
		return map[string]int{"passes": len(items)}, nil
	}
	if mode != "naive" {
		b.Act = func(results.Item, map[string]any, results.Run) (bool, error) { return true, nil }
	}
	b.Log = func(string) {}
	if err := b.RunForever(); err != nil {
		os.Exit(2)
	}
}

func run(t *testing.T, mode string) map[string]Check {
	t.Helper()
	t.Setenv("FAKE_BRIDGE", mode)
	result, err := Run(Options{Command: []string{os.Args[0]}, Timeout: 20 * time.Second})
	if err != nil {
		t.Fatal(err)
	}
	byName := map[string]Check{}
	for _, c := range result.Checks {
		byName[c.Name] = c
	}
	passed := true
	for _, c := range result.Checks {
		passed = passed && c.OK
	}
	if passed != result.Passed {
		t.Fatalf("Passed is %v while the checks say %v", result.Passed, passed)
	}
	return byName
}

func failing(checks map[string]Check) []string {
	var out []string
	for name, c := range checks {
		if !c.OK {
			out = append(out, name+": "+c.Detail)
		}
	}
	return out
}

func TestAWellBehavedBridgePasses(t *testing.T) {
	checks := run(t, "good")
	if failed := failing(checks); len(failed) > 0 {
		t.Fatalf("failed:\n%s", strings.Join(failed, "\n"))
	}
	if len(checks) != 21 {
		t.Errorf("%d checks, want 21", len(checks))
	}
}

func TestABridgeActingOnEveryActionFails(t *testing.T) {
	checks := run(t, "naive")
	for _, name := range []string{
		"never acts on advice, pending, rejected or unknown-status actions, nor on plain forecasts",
		"acts on each approved action exactly once",
	} {
		if checks[name].OK {
			t.Errorf("%q passed", name)
		}
	}
	if !checks["moves past the last run it applied"].OK {
		t.Error("the feed itself should still pass")
	}
}

func TestABridgeThatIgnoresDecisionsMissesAReapproval(t *testing.T) {
	checks := run(t, "content")
	for _, name := range []string{
		"acts on a new approval of an action it acted on before, never again on a standing one",
		"acts on each approved action exactly once",
	} {
		if checks[name].OK {
			t.Errorf("%q passed", name)
		}
	}
	if !checks["does not act again after a restart on what it acted on before"].OK {
		t.Error("it acts on nothing twice, so the restart check should pass")
	}
}

func TestABridgeThatForgetsItsCursorFailsTheRestart(t *testing.T) {
	checks := run(t, "forgetful")
	for _, name := range []string{"resumes from its cursor after a restart", "does not act again after a restart on what it acted on before"} {
		if checks[name].OK {
			t.Errorf("%q passed", name)
		}
	}
}

func TestABridgeThatExitsFails(t *testing.T) {
	checks := run(t, "crash")
	for _, name := range []string{"keeps running until it is stopped", "writes the conformance log", "checks in as a custom connector with a valid manifest",
		// Nothing it must never do passes by its doing nothing at all.
		"never reports a run in an item shape it was not built for",
		"never acts on an action past its cancel deadline",
		"does not act again after a restart on what it acted on before"} {
		if checks[name].OK {
			t.Errorf("%q passed", name)
		}
	}
}

func TestABridgeWhosePollsAllFailIsStoppedEarly(t *testing.T) {
	start := time.Now()
	checks := run(t, "broken")
	if took := time.Since(start); took > 15*time.Second {
		t.Errorf("took %v, as long as the timeout (20s)", took)
	}
	for _, name := range []string{"consumes every forecast run and reports it applied", "never acts on advice, pending, rejected or unknown-status actions, nor on plain forecasts"} {
		if checks[name].OK {
			t.Errorf("%q passed", name)
		}
	}
}

func TestTheLogNamesTheFieldABridgeGotWrong(t *testing.T) {
	for line, want := range map[string]string{
		`{"item_id": "p", "action": "swap", "state": "approved"}`:                     `"acted"`,
		`{"item_id": "p", "state": "held", "acted": false, "verb": null}`:             `"state"`,
		`{"state": "forecast", "acted": false, "verb": null}`:                         `"item_id"`,
		`{"item_id": "p", "state": "forecast", "acted": false}`:                       `"verb"`,
		`{"item_id": "p", "state": "forecast", "acted": false, "verb": null, "x": 1}`: `"x"`,
		`not json`: "JSON",
	} {
		if _, problem := parseLine(line); !strings.Contains(problem, want) {
			t.Errorf("%s: %q, want it to name %s", line, problem, want)
		}
	}
	if l, problem := parseLine(`{"item_id": "p", "state": "approved", "acted": true, "verb": "swap"}`); problem != "" || !l.acted || l.verb != "swap" {
		t.Errorf("a good line: %+v %q", l, problem)
	}
}

func TestManifestProblemsAreTheAPIs(t *testing.T) {
	var m map[string]any
	d := json.NewDecoder(strings.NewReader(`{"name": "Bad Name", "title": "", "colour": "red",
	  "settings": {"Min": {"type": "number", "title": "m", "default": -1, "minimum": 0},
	               "mode": {"type": "enum", "title": "Mode", "default": "c", "options": ["a", "b"]}}}`))
	d.UseNumber()
	if err := d.Decode(&m); err != nil {
		t.Fatal(err)
	}
	got := strings.Join(manifestProblems(m), "\n")
	for _, want := range []string{"lowercase slug", "title must be 1 to 80", "unknown manifest fields: [colour]",
		"setting Min: keys are lowercase", "setting Min: its default must be at least 0", "setting mode: its default must be one of"} {
		if !strings.Contains(got, want) {
			t.Errorf("missing %q in\n%s", want, got)
		}
	}
	m = nil
	d = json.NewDecoder(strings.NewReader(manifest))
	d.UseNumber()
	_ = d.Decode(&m)
	delete(m, "version") // a bridge checks in without it
	if p := manifestProblems(m); len(p) > 0 {
		t.Errorf("a valid manifest has problems: %v", p)
	}
}
