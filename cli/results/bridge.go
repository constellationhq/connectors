package results

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"sort"
	"strconv"
	"strings"
	"time"
)

// Run is the run a part of an entry belongs to: the run is the whole answer for its window.
type Run struct {
	Workflow        string `json:"workflow"`
	WorkflowVersion int64  `json:"workflow_version"`
	RunID           string `json:"run_id"`
	WindowFrom      string `json:"window_from"`
	WindowTo        string `json:"window_to"`
}

// Bridge runs a custom connector: it checks in with its manifest, pulls its results from the
// cursor it last applied, hands each run's items to Apply, each approval to Act exactly once
// (also across restarts and when a later entry sends it again; a new approval of the same
// action is acted on again), reports what it applied, and keeps the cursor. Fill in Apply (and Act) and call RunForever.
type Bridge struct {
	// Manifest is connector.json: name (the consumer), title, description, version, settings.
	Manifest map[string]any
	// Apply writes one run's items into your system and returns what it wrote, e.g.
	// {"passes": 12}. An error retries the run.
	Apply func(items []Item, settings map[string]any, run Run) (map[string]int, error)
	// Act carries out an approved action (cancel the pass, book the backup) and returns true
	// once done. Called after Apply, never for any state but approved. Nil carries out nothing.
	Act func(item Item, settings map[string]any, run Run) (bool, error)

	Client     *Client
	CursorFile string
	PollEvery  time.Duration
	Log        func(string)
	// ActionLog, when set, gets one JSON line per item applied (LogLine), after the run is
	// reported and before the cursor moves.
	ActionLog string
	// Backfill, with no saved cursor, applies the whole history the feed holds instead of starting
	// at its newest run (BACKFILL=1).
	Backfill bool

	cursor  string
	started bool
	// unreported keeps, by cursor, what each part of a run wrote until the run is reported, so a
	// failed report is retried without writing or acting again.
	unreported map[string][]written
	// acted maps each approval carried out (actionKey) to when it may be forgotten (actedUntil).
	acted map[string]string
}

// NewBridge is a bridge for the manifest `manifestJSON`, configured from the environment:
// CONSTELLATION_API_URL, CONSTELLATION_API_KEY, CURSOR_FILE (cursor.txt), POLL_SECONDS (15),
// BACKFILL and CONSTELLATION_CONFORMANCE_LOG.
func NewBridge(manifestJSON []byte) (*Bridge, error) {
	var manifest map[string]any
	if err := json.Unmarshal(manifestJSON, &manifest); err != nil {
		return nil, fmt.Errorf("connector.json: %w", err)
	}
	if name, _ := manifest["name"].(string); name == "" {
		return nil, errors.New("connector.json has no name")
	}
	poll, err := strconv.ParseFloat(env("POLL_SECONDS", "15"), 64)
	if err != nil {
		return nil, fmt.Errorf("POLL_SECONDS: %w", err)
	}
	b := &Bridge{Manifest: manifest, Client: NewClient(), CursorFile: env("CURSOR_FILE", "cursor.txt"),
		PollEvery: time.Duration(poll * float64(time.Second)), ActionLog: os.Getenv("CONSTELLATION_CONFORMANCE_LOG"),
		Backfill: os.Getenv("BACKFILL") == "1"}
	b.Log = func(m string) { fmt.Printf("%s: %s\n", b.Consumer(), m) }
	return b, nil
}

func env(name, fallback string) string {
	if v := os.Getenv(name); v != "" {
		return v
	}
	return fallback
}

// Consumer is the connector's name, which it pulls results as.
func (b *Bridge) Consumer() string {
	name, _ := b.Manifest["name"].(string)
	return name
}

func (b *Bridge) title() string {
	if t, _ := b.Manifest["title"].(string); t != "" {
		return t
	}
	return b.Consumer()
}

func (b *Bridge) checkIn() error {
	manifest := map[string]any{}
	for _, k := range []string{"name", "title", "description", "settings"} {
		if v, ok := b.Manifest[k]; ok {
			manifest[k] = v
		}
	}
	version, _ := b.Manifest["version"].(string)
	if version == "" {
		version = "0.1.0"
	}
	return b.Client.Call("POST", "/predictions/feed/checkin", map[string]any{
		"consumer": b.Consumer(), "system": "custom", "manifest": manifest,
		"bridge_version": version, "target": b.title(),
	}, nil)
}

// load reads the cursor and the approvals already carried out, kept beside it (a line per key:
// the key, a tab, when it may be forgotten), forgetting those long over. The API they were read
// from is kept beside them too (CursorFile plus ".api"): state from another API (the mock) is
// refused, never resumed against this one.
func (b *Bridge) load() error {
	raw, _ := os.ReadFile(b.CursorFile + ".api")
	if there, here := strings.TrimSpace(string(raw)), Origin(b.Client.URL); there != "" && there != here {
		return fmt.Errorf("%s is where this bridge read %s up to, and it now reads %s. Delete %s* to start afresh on %s, or set CURSOR_FILE to another file",
			b.CursorFile, there, here, b.CursorFile, here)
	}
	raw, _ = os.ReadFile(b.CursorFile)
	b.cursor = strings.TrimSpace(string(raw))
	b.acted = map[string]string{}
	raw, _ = os.ReadFile(b.CursorFile + ".acted")
	for _, line := range strings.Split(string(raw), "\n") {
		if key, until, _ := strings.Cut(line, "\t"); key != "" {
			b.acted[key] = until
		}
	}
	b.prune()
	return nil
}

// KeepActed is how long an acted key is kept once its action is over: past it, no run sends the
// action again.
const KeepActed = 7 * 24 * time.Hour

// actedUntil is when an acted key may be forgotten: the latest of the item's time, its end (a
// pass's end), its action's cancel deadline and now, plus KeepActed, so a key is never dropped as
// soon as it is written; "" (kept for good) when none of the item's is a time.
func actedUntil(item Item) string {
	action, _ := item["action"].(map[string]any)
	var latest time.Time
	for _, v := range []any{item["target_at"], item["until"], action["deadline"]} {
		s, _ := v.(string)
		if t, err := time.Parse(time.RFC3339, s); err == nil && t.After(latest) {
			latest = t
		}
	}
	if latest.IsZero() {
		return ""
	}
	if now := time.Now(); now.After(latest) {
		latest = now
	}
	return latest.Add(KeepActed).UTC().Format(time.RFC3339)
}

// PastDeadline says whether the item's action is past its cancel deadline: closed, never to be
// carried out.
func PastDeadline(item Item) bool {
	action, _ := item["action"].(map[string]any)
	s, _ := action["deadline"].(string)
	t, err := time.Parse(time.RFC3339, s)
	return err == nil && !t.After(time.Now())
}

// StartCursor is where a first start reads from: just before the newest run with something for
// `consumer`, so it applies that run and none of the older history. The API answers it without
// reading a run.
func StartCursor(c *Client, consumer string) (string, error) {
	page, err := c.Start(consumer)
	if err != nil {
		return "", err
	}
	if len(page.Entries) > 0 {
		return "", errors.New("the API ignored GET /predictions/feed?from=latest, so a first start cannot find the newest run: this bridge needs an API serving contract 1.5.0 or later")
	}
	return page.Next, nil
}

func (b *Bridge) prune() {
	now := time.Now()
	for key, until := range b.acted {
		if t, err := time.Parse(time.RFC3339, until); err == nil && t.Before(now) {
			delete(b.acted, key)
		}
	}
}

func (b *Bridge) saveActed() error {
	keys := make([]string, 0, len(b.acted))
	for key := range b.acted {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	var text strings.Builder
	for _, key := range keys {
		text.WriteString(key + "\t" + b.acted[key] + "\n")
	}
	path := b.CursorFile + ".acted"
	if err := os.WriteFile(path+".tmp", []byte(text.String()), 0o644); err != nil {
		return err
	}
	return os.Rename(path+".tmp", path)
}

// actionKey names the decision to carry out an action: its decision_id, which a standing
// approval keeps in every run and a new approval of the same action changes. Without one, its
// content, as Constellation keys an action: the same verb on the same entity to the same target.
func actionKey(item Item) string {
	if decision, _ := item["decision_id"].(string); decision != "" {
		return decision
	}
	entity, _ := item["entity"].(map[string]any)
	action, _ := item["action"].(map[string]any)
	text := func(m map[string]any, k string) string { s, _ := m[k].(string); return s }
	return text(entity, "id") + "|" + text(action, "verb") + "|" + text(action, "to")
}

func appendLines(path string, lines []string) error {
	if len(lines) == 0 {
		return nil
	}
	f, err := os.OpenFile(path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		return err
	}
	if _, err := f.WriteString(strings.Join(lines, "\n") + "\n"); err != nil {
		f.Close()
		return err
	}
	return f.Close()
}

// act carries out an approval unless it was carried out before, and says whether it did now.
func (b *Bridge) act(item Item, settings map[string]any, run Run) (bool, error) {
	if PastDeadline(item) {
		b.Log(fmt.Sprintf("not acting on %v: its cancel deadline has passed", item["entity"]))
		return false, nil
	}
	key := actionKey(item)
	if _, done := b.acted[key]; b.Act == nil || done {
		return false, nil
	}
	done, err := b.Act(item, settings, run)
	if err != nil || !done {
		return false, err
	}
	b.acted[key] = actedUntil(item)
	b.prune()
	if err := b.saveActed(); err != nil {
		return true, fmt.Errorf("%w: %v", ErrState, err)
	}
	return true, nil
}

// ErrState is a failure to save the cursor or the approvals carried out: RunForever stops on it,
// as a bridge that cannot keep its state repeats its work after every poll or restart.
var ErrState = errors.New("cannot keep its state, so it would repeat its work (make CURSOR_FILE's folder writable)")

// LogLine is the conformance log's line for one item applied. State is "forecast" for an item
// with no action; Acted is true only when the bridge carried out an approved action just now.
type LogLine struct {
	ItemID string  `json:"item_id"`
	State  string  `json:"state"`
	Acted  bool    `json:"acted"`
	Verb   *string `json:"verb"`
}

// NewLogLine is the log line for `item`.
func NewLogLine(item Item, acted bool) LogLine {
	entity, _ := item["entity"].(map[string]any)
	id, _ := entity["id"].(string)
	state := ActionState(item)
	if state == "" {
		state = "forecast"
	}
	var verb *string
	if action, _ := item["action"].(map[string]any); action != nil {
		if v, ok := action["verb"].(string); ok {
			verb = &v
		}
	}
	return LogLine{id, state, acted, verb}
}

// Poll applies every run after the cursor, oldest first, moving the cursor past each.
func (b *Bridge) Poll() error {
	if b.acted == nil {
		if err := b.load(); err != nil {
			return err
		}
	}
	if !b.started && b.cursor == "" && !b.Backfill {
		start, err := StartCursor(b.Client, b.Consumer())
		if err != nil {
			return err
		}
		b.cursor = start
		b.Log(fmt.Sprintf("first start: reading after %q", start))
	}
	b.started = true
	after := b.cursor
	for {
		page, err := b.Client.Results(after, b.Consumer(), 1, PageRecords)
		if err != nil {
			return err
		}
		if after != "" && page.Oldest != nil && after < *page.Oldest {
			b.Log(fmt.Sprintf("the feed no longer holds cursor %s: any runs after it and before the oldest it holds, %s, expired unread; reading on from there", after, *page.Oldest))
		}
		if len(page.Entries) == 0 {
			return nil
		}
		for _, entry := range page.Entries {
			after = entry.Cursor
			if b.cursor != "" && entry.Cursor <= b.cursor {
				// A feed replaying from an older cursor (a failover): applied before, never again.
				b.Log(fmt.Sprintf("skipped %s: already applied", entry.Cursor))
				continue
			}
			entry, err := Whole(entry, func(c string, n int) (Entry, error) {
				return b.Client.RunPage(c, b.Consumer(), n, PageRecords)
			})
			if err != nil {
				return err
			}
			if b.unreported == nil {
				b.unreported = map[string][]written{}
			}
			for n, part := range Unpack(entry.OTLP, b.Consumer(), b.Log) {
				if err := b.applyPart(entry.Cursor, n, part); err != nil {
					return err
				}
			}
			delete(b.unreported, entry.Cursor)
			// Only a run applied and reported (or not for this connector) moves the cursor.
			b.cursor = entry.Cursor
			if err := os.WriteFile(b.CursorFile+".api", []byte(Origin(b.Client.URL)), 0o644); err != nil {
				return fmt.Errorf("%w: %v", ErrState, err)
			}
			if err := os.WriteFile(b.CursorFile, []byte(b.cursor), 0o644); err != nil {
				return fmt.Errorf("%w: %v", ErrState, err)
			}
		}
	}
}

type written struct {
	applied map[string]int
	acted   []bool
	items   []Item
}

// Malformed is why an item cannot be read (no entity with a string id and type, or a time that
// is not one), or "" for a well-formed one; as the Python and TypeScript bridges judge it.
func Malformed(item Item) string {
	entity, _ := item["entity"].(map[string]any)
	id, _ := entity["id"].(string)
	kind, _ := entity["type"].(string)
	if id == "" || kind == "" {
		return "no entity with an id and a type"
	}
	action, isObject := item["action"].(map[string]any)
	if item["action"] != nil && !isObject {
		return "its action is not an object"
	}
	times := map[string]any{"target_at": item["target_at"]}
	if item["until"] != nil {
		times["until"] = item["until"]
	}
	if action["deadline"] != nil {
		times["action.deadline"] = action["deadline"]
	}
	for name, v := range times {
		s, _ := v.(string)
		if _, err := time.Parse(time.RFC3339, s); err != nil {
			return name + " is not a time"
		}
	}
	return ""
}

func (b *Bridge) applyPart(cursor string, n int, part Part) error {
	r := part.Resource
	var output struct {
		Mapping struct {
			Settings map[string]any `json:"settings"`
		} `json:"mapping"`
	}
	if s, _ := r["constellation.output"].(string); s != "" {
		_ = json.Unmarshal([]byte(s), &output)
	}
	settings := output.Mapping.Settings
	if settings == nil {
		settings = map[string]any{}
	}
	str := func(k string) string { s, _ := r[k].(string); return s }
	version, _ := r["constellation.workflow_version"].(int64)
	run := Run{str("constellation.workflow"), version, str("constellation.run_id"),
		str("constellation.window.from"), str("constellation.window.to")}
	if n == len(b.unreported[cursor]) {
		// One malformed item is logged, skipped and counted: never worth its run and the runs after it.
		var items []Item
		for i, item := range part.Items {
			if why := Malformed(item); why != "" {
				b.Log(fmt.Sprintf("skipped item %d of run %s: it is malformed (%s)", i, run.RunID, why))
				continue
			}
			items = append(items, item)
		}
		applied, err := b.Apply(items, settings, run)
		if err != nil {
			return err
		}
		if applied == nil {
			applied = map[string]int{}
		}
		if bad := len(part.Items) - len(items); bad > 0 {
			applied["malformed"] = bad
		}
		acted := make([]bool, len(items))
		for i, item := range items {
			if ActionState(item) == "approved" {
				if acted[i], err = b.act(item, settings, run); err != nil {
					return err
				}
				// So the platform can show that an approval was carried out.
				if acted[i] {
					applied["acted"]++
				}
			}
		}
		b.unreported[cursor] = append(b.unreported[cursor], written{applied, acted, items})
	}
	done := b.unreported[cursor][n]
	applied, acted, items := done.applied, done.acted, done.items
	if err := b.Client.Call("POST", "/predictions/feed/deliveries", map[string]any{
		"consumer": b.Consumer(), "run_id": run.RunID, "cursor": cursor, "applied": applied, "target": b.title(),
	}, nil); err != nil {
		return err
	}
	b.Log(fmt.Sprintf("applied %v, %s", applied, cursor))
	if b.ActionLog == "" {
		return nil
	}
	var lines []string
	for i, item := range items {
		line, _ := json.Marshal(NewLogLine(item, acted[i]))
		lines = append(lines, string(line))
	}
	return appendLines(b.ActionLog, lines)
}

// CheckInEvery is how often a bridge checks in again: the platform reads a connection that
// stopped checking in as stopped. A check-in that failed is tried again at the next poll.
var CheckInEvery = 5 * time.Minute

// RunForever checks in (again every CheckInEvery), then polls every PollEvery. It returns only when Constellation refuses
// the key or plan, which asking again does not clear, on ErrState, or at once with no key (unless the API is the mock
// on this machine); any other failure is logged and retried.
func (b *Bridge) RunForever() error {
	if b.Client.Key == "" && !Loopback(b.Client.URL) {
		return fmt.Errorf("CONSTELLATION_API_KEY is not set: make a key with predictions:read on %s/keys and set it", Site(b.Client.URL))
	}
	if err := b.load(); err != nil {
		return err
	}
	from := b.cursor
	if from == "" && b.Backfill {
		from = "the start"
	} else if from == "" {
		from = "its newest run"
	}
	b.Log(fmt.Sprintf("%s as %s, from cursor %s", b.Client.URL, b.Consumer(), from))
	checkedIn, nextCheckIn := false, time.Time{}
	for {
		err := error(nil)
		if time.Now().After(nextCheckIn) {
			if err = b.checkIn(); err == nil {
				nextCheckIn = time.Now().Add(CheckInEvery)
				if !checkedIn {
					b.Log("checked in; approve it on Integrations if it is new or its manifest changed")
				}
				checkedIn = true
			}
		}
		if err == nil {
			err = b.Poll()
		}
		var apiErr *Error
		if errors.As(err, &apiErr) && apiErr.Refusal() != "" {
			return apiErr
		}
		if errors.Is(err, ErrState) {
			return err
		}
		if err != nil {
			b.Log(err.Error())
		}
		time.Sleep(b.PollEvery)
	}
}
