// Command constellation is the Constellation developer CLI: log in, tail workflow results, run
// the mock API, check a bridge's conformance, scaffold and run bridges, verify a release.
package main

import (
	"bufio"
	"bytes"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"github.com/constellationhq/connectors/cli/conformance"
	"github.com/constellationhq/connectors/cli/mock"
	"github.com/constellationhq/connectors/cli/results"
	"github.com/constellationhq/connectors/cli/templates"
)

// version is the release this source becomes (tests/test_versions.py keeps it with the rest);
// the release build stamps the tag over it: -ldflags "-X main.version=vX.Y.Z". Scaffolds pin
// the Go results package and the TypeScript client to it.
var version = "v0.7.1"

const usage = `constellation: the Constellation developer CLI

  login [--key KEY] [--url URL]     store an API key (prompted when not given) and check it
  whoami                            show the account the stored key belongs to
  results tail [--json] [--once] [--from-start]
                                    print workflow results as they arrive, from the newest run
  mock [--port 7070]                serve the workflow results endpoints with the demo scenario
  conformance [--json] -- CMD...    check a bridge against the mock
  bridge new NAME --lang python|ts|go   scaffold a bridge
  bridge run yamcs|lattice          run an official bridge (the constellation-space package)
  verify DIR                        verify a release folder's SHA256SUMS and its signature (cosign)
  version                           print the version

CONSTELLATION_API_URL and CONSTELLATION_API_KEY override what login stored. A stored key goes only
to the API it was stored for: pointed at another, set CONSTELLATION_API_KEY for it. No key is sent
over plain http, except to localhost.
`

func main() {
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr))
}

// errUsage is a mistake in the command line, already explained.
var errUsage = errors.New("usage")

func run(args []string, stdout, stderr io.Writer) int {
	if len(args) == 0 {
		fmt.Fprint(stderr, usage)
		return 2
	}
	var err error
	switch args[0] {
	case "login":
		err = login(args[1:], stdout)
	case "whoami":
		var c *results.Client
		if c, err = client(); err == nil {
			err = whoami(c, stdout)
		}
	case "results":
		if len(args) < 2 || args[1] != "tail" {
			fmt.Fprintln(stderr, "usage: constellation results tail [--json] [--once] [--from-start | --after CURSOR] [--consumer NAME]")
			return 2
		}
		err = tail(args[2:], stdout, stderr)
	case "mock":
		err = serveMock(args[1:], stdout)
	case "conformance":
		return runConformance(args[1:], stdout, stderr)
	case "bridge":
		err = bridge(args[1:], stdout, stderr)
	case "verify":
		err = verify(args[1:], stdout, stderr)
	case "version", "--version":
		fmt.Fprintln(stdout, version)
	case "help", "-h", "--help":
		fmt.Fprint(stdout, usage)
	default:
		fmt.Fprintf(stderr, "unknown command %q\n\n%s", args[0], usage)
		return 2
	}
	var exit exitCode
	switch {
	case errors.As(err, &exit):
		return int(exit)
	case errors.Is(err, errUsage), errors.Is(err, flag.ErrHelp):
		return 2
	case err != nil:
		fmt.Fprintf(stderr, "constellation: %v\n", err)
		return 1
	}
	return 0
}

// exitCode is a child's exit status, passed on as ours.
type exitCode int

func (e exitCode) Error() string { return fmt.Sprintf("exit status %d", int(e)) }

// parse parses flags that may come before or after positional arguments.
// A flag it does not know is reported by the flag package, once, as a usage error.
func parse(fs *flag.FlagSet, args []string) ([]string, error) {
	var positional []string
	for {
		if err := fs.Parse(args); errors.Is(err, flag.ErrHelp) {
			return nil, err
		} else if err != nil {
			return nil, errUsage
		}
		if fs.NArg() == 0 {
			return positional, nil
		}
		positional = append(positional, fs.Arg(0))
		args = fs.Args()[1:]
	}
}

type config struct {
	URL string `json:"url"`
	Key string `json:"key"`
}

func configPath() (string, error) {
	dir, err := os.UserConfigDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(dir, "constellation", "config.json"), nil
}

func loadConfig() config {
	var c config
	if p, err := configPath(); err == nil {
		raw, _ := os.ReadFile(p)
		_ = json.Unmarshal(raw, &c)
	}
	return c
}

// client is an API client from CONSTELLATION_API_URL and CONSTELLATION_API_KEY, else what
// login stored. The stored key is bound to the API it was stored for: it is never sent to
// another origin, which takes a key given for it (CONSTELLATION_API_KEY).
func client() (*results.Client, error) {
	c := results.NewClient()
	stored := loadConfig()
	if os.Getenv("CONSTELLATION_API_URL") == "" && stored.URL != "" {
		c.URL = strings.TrimRight(stored.URL, "/")
	}
	if c.Key == "" && stored.Key != "" {
		if results.Origin(c.URL) != results.Origin(stored.URL) {
			return nil, fmt.Errorf("the key constellation login stored is for %s, and CONSTELLATION_API_URL points at %s:"+
				" it is never sent anywhere else. Set CONSTELLATION_API_KEY to a key for %s, or run constellation login --url %s",
				stored.URL, c.URL, c.URL, c.URL)
		}
		c.Key = stored.Key
	}
	return c, nil
}

func login(args []string, stdout io.Writer) error {
	fs := flag.NewFlagSet("login", flag.ContinueOnError)
	key := fs.String("key", "", "the API key (prompted for when not given)")
	url := fs.String("url", results.DefaultURL, "the Constellation API")
	if _, err := parse(fs, args); err != nil {
		return err
	}
	if *key == "" {
		fmt.Fprint(stdout, "API key (from https://platform.constellation.space/keys): ")
		line, _ := bufio.NewReader(os.Stdin).ReadString('\n')
		*key = strings.TrimSpace(line)
	}
	if *key == "" {
		return errors.New("no key given")
	}
	c := &results.Client{URL: strings.TrimRight(*url, "/"), Key: *key, HTTP: &http.Client{Timeout: 30 * time.Second}}
	if err := whoami(c, stdout); err != nil {
		return fmt.Errorf("not stored: %w", err)
	}
	p, err := configPath()
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(p), 0o700); err != nil {
		return err
	}
	raw, _ := json.MarshalIndent(config{c.URL, c.Key}, "", "  ")
	if err := os.WriteFile(p, raw, 0o600); err != nil {
		return err
	}
	// WriteFile keeps the mode of a file that already exists.
	if err := os.Chmod(p, 0o600); err != nil {
		return err
	}
	fmt.Fprintf(stdout, "stored in %s\n", p)
	return nil
}

func whoami(c *results.Client, stdout io.Writer) error {
	if c.Key == "" {
		return errors.New("no API key: run constellation login, or set CONSTELLATION_API_KEY")
	}
	var state struct {
		AccountID   string `json:"account_id"`
		AccountType string `json:"account_type"`
		Plan        string `json:"plan"`
	}
	if err := c.Call("GET", "/account/state", nil, &state); err != nil {
		return err
	}
	fmt.Fprintf(stdout, "%s: account %s (%s, plan %s)\n", c.URL, state.AccountID, state.AccountType, state.Plan)
	return nil
}

func tail(args []string, stdout, stderr io.Writer) error {
	fs := flag.NewFlagSet("results tail", flag.ContinueOnError)
	asJSON := fs.Bool("json", false, "one JSON object per item: cursor, run_id, workflow, item")
	once := fs.Bool("once", false, "stop once caught up; any error ends it (exit 1)")
	after := fs.String("after", "", "start after this cursor (default: the newest run)")
	fromStart := fs.Bool("from-start", false, "start at the oldest run the feed holds")
	consumer := fs.String("consumer", "", "read as this connection (only its outputs; the API lists it as a reader)")
	every := fs.Duration("every", 15*time.Second, "how often to poll once caught up")
	if _, err := parse(fs, args); err != nil {
		return err
	}
	c, err := client()
	if err != nil {
		return err
	}
	if c.Key == "" && !results.Loopback(c.URL) {
		return errors.New("no API key: run constellation login, or set CONSTELLATION_API_KEY")
	}
	cursor := *after
	if cursor == "" && !*fromStart {
		if cursor, err = results.StartCursor(c, *consumer); err != nil {
			return fmt.Errorf("reading the results from %s: %w", c.URL, err)
		}
	}
	if !*asJSON {
		fmt.Fprintf(stdout, "%-30s %-24s %-18s %-9s %-26s %s\n", "RUN", "WORKFLOW", "ITEM", "STATE", "TARGET_AT", "ACTION")
	}
	for {
		var page results.Page
		page, err = c.Results(cursor, *consumer, 1, results.PageRecords)
		for _, entry := range page.Entries {
			if entry, err = results.Whole(entry, func(at string, n int) (results.Entry, error) {
				return c.RunPage(at, *consumer, n, results.PageRecords)
			}); err != nil {
				break
			}
			for _, part := range results.Unpack(entry.OTLP, *consumer, func(m string) { fmt.Fprintln(stderr, m) }) {
				for _, item := range part.Items {
					printItem(stdout, *asJSON, entry.Cursor, part.Resource, item)
				}
			}
			cursor = entry.Cursor
		}
		var apiErr *results.Error
		if err != nil && (*once || errors.As(err, &apiErr) && apiErr.Refusal() != "") {
			return fmt.Errorf("reading the results from %s: %w", c.URL, err)
		}
		if err != nil {
			fmt.Fprintf(stderr, "constellation: %v; retrying\n", err)
		} else if len(page.Entries) > 0 {
			continue
		} else if *once {
			return nil
		}
		time.Sleep(*every)
	}
}

func printItem(w io.Writer, asJSON bool, cursor string, resource map[string]any, item results.Item) {
	if asJSON {
		line, _ := json.Marshal(map[string]any{"cursor": cursor, "run_id": resource["constellation.run_id"],
			"workflow": resource["constellation.workflow"], "item": item})
		fmt.Fprintln(w, string(line))
		return
	}
	entity, _ := item["entity"].(map[string]any)
	state := results.ActionState(item)
	if state == "" {
		state = "forecast"
	}
	action := ""
	if a, ok := item["action"].(map[string]any); ok {
		action = fmt.Sprintf("%v -> %v", a["verb"], a["to"])
	}
	fmt.Fprintf(w, "%-30v %-24v %-18v %-9s %-26v %s\n", resource["constellation.run_id"], resource["constellation.workflow"],
		entity["id"], state, item["target_at"], action)
}

func serveMock(args []string, stdout io.Writer) error {
	fs := flag.NewFlagSet("mock", flag.ContinueOnError)
	port := fs.Int("port", 7070, "port on 127.0.0.1")
	scenario := fs.String("scenario", "demo", "the scenario to replay (demo)")
	if _, err := parse(fs, args); err != nil {
		return err
	}
	if *scenario != "demo" {
		return fmt.Errorf("unknown scenario %q: the mock has demo", *scenario)
	}
	addr := fmt.Sprintf("127.0.0.1:%d", *port)
	s := mock.Demo()
	fmt.Fprintf(stdout, `mock Constellation API on http://%s (scenario %s, %d entries; x-api-key accepted, not required)
  GET  /predictions/feed            ?after= &limit= &consumer= &page_records=  |  ?cursor= &page=  |  ?from=latest &consumer=
  POST /predictions/feed/checkin
  POST /predictions/feed/deliveries
  GET  /account/state
point a bridge at it: CONSTELLATION_API_URL=http://%s
`, addr, s.Name, len(s.Entries), addr)
	return http.ListenAndServe(addr, mock.New(s))
}

func runConformance(args []string, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("conformance", flag.ContinueOnError)
	fs.SetOutput(stderr)
	asJSON := fs.Bool("json", false, "print the result as JSON")
	port := fs.Int("port", 0, "the mock's port on 127.0.0.1 (default: a free one)")
	timeout := fs.Duration("timeout", 60*time.Second, "how long each run of the bridge has to catch up with the mock")
	settings := fs.String("settings", "", `JSON settings to send instead of the manifest's defaults, e.g. '{"min_p10_gb": 5}'`)
	if err := fs.Parse(args); err != nil {
		return 2
	}
	opts := conformance.Options{Command: fs.Args(), Port: *port, Timeout: *timeout, Output: stderr}
	if *settings != "" {
		d := json.NewDecoder(strings.NewReader(*settings))
		d.UseNumber()
		if err := d.Decode(&opts.Settings); err != nil {
			fmt.Fprintf(stderr, "constellation: --settings is not a JSON object: %v\n", err)
			return 2
		}
	}
	if len(opts.Command) == 0 {
		fmt.Fprintln(stderr, "usage: constellation conformance [--json] [--port N] [--timeout 60s] [--settings JSON] -- <bridge command...>")
		return 2
	}
	if !*asJSON {
		fmt.Fprintf(stderr, "running %s against the mock, then again after killing it\n", strings.Join(opts.Command, " "))
	}
	result, err := conformance.Run(opts)
	if err != nil {
		fmt.Fprintf(stderr, "constellation: %v\n", err)
		return 2
	}
	if *asJSON {
		raw, _ := json.MarshalIndent(result, "", "  ")
		fmt.Fprintln(stdout, string(raw))
	} else {
		failed := 0
		for _, c := range result.Checks {
			if c.OK {
				fmt.Fprintf(stdout, "PASS  %s\n", c.Name)
			} else {
				failed++
				fmt.Fprintf(stdout, "FAIL  %s: %s\n", c.Name, c.Detail)
			}
		}
		if failed == 0 {
			fmt.Fprintln(stdout, "conformant")
		} else {
			fmt.Fprintf(stdout, "%d of %d checks failed\n", failed, len(result.Checks))
		}
	}
	if !result.Passed {
		return 1
	}
	return 0
}

// officialBridges are the bridges `bridge run` runs, each with what pip installs it from.
var officialBridges = map[string]string{"yamcs": "constellation-space", "lattice": "'constellation-space[lattice]'"}

func bridge(args []string, stdout, stderr io.Writer) error {
	if len(args) == 0 {
		fmt.Fprintln(stderr, "usage: constellation bridge new NAME --lang python|ts|go | bridge run yamcs|lattice")
		return errUsage
	}
	switch args[0] {
	case "new":
		fs := flag.NewFlagSet("bridge new", flag.ContinueOnError)
		lang := fs.String("lang", "", "python, ts or go")
		dir := fs.String("dir", "", "the folder to write (default: ./NAME)")
		rest, err := parse(fs, args[1:])
		if err != nil {
			return err
		}
		if len(rest) != 1 || *lang == "" {
			fmt.Fprintln(stderr, "usage: constellation bridge new NAME --lang python|ts|go [--dir DIR]")
			return errUsage
		}
		if *dir == "" {
			*dir = rest[0]
		}
		if err := templates.Scaffold(*dir, rest[0], *lang, version); err != nil {
			return err
		}
		fmt.Fprintf(stdout, "wrote %s: see its README.md, then check it with constellation conformance\n", *dir)
		return nil
	case "run":
		if len(args) != 2 || officialBridges[args[1]] == "" {
			fmt.Fprintln(stderr, "usage: constellation bridge run yamcs|lattice")
			return errUsage
		}
		return runOfficial(args[1], stdout, stderr)
	}
	fmt.Fprintf(stderr, "unknown bridge command %q: new or run\n", args[0])
	return errUsage
}

func runOfficial(name string, stdout, stderr io.Writer) error {
	python := os.Getenv("PYTHON")
	if python == "" {
		python = "python3"
	}
	module := "constellation_space.bridges." + name
	var why bytes.Buffer
	check := exec.Command(python, "-c", "import "+module)
	check.Stderr = &why
	if err := check.Run(); err != nil {
		return fmt.Errorf("the %s bridge is not installed for %s (%s): pip install %s",
			name, python, strings.TrimSpace(lastLine(why.String(), err)), officialBridges[name])
	}
	c, err := client()
	if err != nil {
		return err
	}
	cmd := exec.Command(python, "-m", module)
	cmd.Stdin, cmd.Stdout, cmd.Stderr = os.Stdin, stdout, stderr
	if c.Key != "" && !results.KeySafe(c.URL) {
		return fmt.Errorf("not handing the API key to a bridge for %s: it is not https (plain http only to localhost)", c.URL)
	}
	cmd.Env = append(os.Environ(), "CONSTELLATION_API_URL="+c.URL, "CONSTELLATION_API_KEY="+c.Key)
	err = cmd.Run()
	var exit *exec.ExitError
	if errors.As(err, &exit) {
		return exitCode(exit.ExitCode())
	}
	return err
}

func lastLine(s string, err error) string {
	lines := strings.Split(strings.TrimSpace(s), "\n")
	if l := lines[len(lines)-1]; l != "" {
		return l
	}
	return err.Error()
}
