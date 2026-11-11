// Package results reads Constellation workflow results: a run's items as OTLP log records, paged and
// joined, with each item's action state. Bridge (bridge.go) runs a Go bridge on it.
package results

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"strconv"
	"strings"
	"time"
)

// Schema is the item shape this package reads; a run in another is skipped and logged.
const Schema = "constellation.item/v2"

// PageRecords is the most records asked for in one page of a run.
const PageRecords = 400

const (
	DefaultURL = "https://api.constellation.space"
	platform   = "https://platform.constellation.space"
)

// NamePattern is what a connection's name (a bridge's consumer) must match.
var NamePattern = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{0,62}$`)

// Item is one result: entity, target_at, until, value, explain, and the facets
// (series, severity, action, action_status).
type Item = map[string]any

// ActionState is what an item's action is to a bridge. "advice": suggested, never an
// instruction. "approved": a person approved it in Constellation, an instruction to carry out.
// "pending": held for approval (the action itself is not sent). "rejected": decided against.
// "": no action. Any status other than approved or rejected counts as pending, so an item a
// bridge cannot read is never taken for a plain forecast.
func ActionState(item Item) string {
	status := item["action_status"]
	if status != nil && status != "approved" {
		if status == "rejected" {
			return "rejected"
		}
		return "pending"
	}
	if action, _ := item["action"].(map[string]any); len(action) == 0 {
		if status == nil {
			return ""
		}
		return "pending"
	}
	if status == "approved" {
		return "approved"
	}
	return "advice"
}

type KeyValue struct {
	Key   string         `json:"key"`
	Value map[string]any `json:"value"`
}

type LogRecord struct {
	TimeUnixNano         string         `json:"timeUnixNano,omitempty"`
	ObservedTimeUnixNano string         `json:"observedTimeUnixNano,omitempty"`
	SeverityNumber       int            `json:"severityNumber,omitempty"`
	SeverityText         string         `json:"severityText,omitempty"`
	Body                 map[string]any `json:"body"`
	Attributes           []KeyValue     `json:"attributes"`
}

type ScopeLogs struct {
	Scope      map[string]any `json:"scope,omitempty"`
	LogRecords []LogRecord    `json:"logRecords"`
}

type ResourceLogs struct {
	Resource struct {
		Attributes []KeyValue `json:"attributes"`
	} `json:"resource"`
	ScopeLogs []ScopeLogs `json:"scopeLogs"`
}

// OTLP is an ExportLogsServiceRequest: one ResourceLogs per feed output of a run.
type OTLP struct {
	ResourceLogs []ResourceLogs `json:"resourceLogs"`
}

// Entry is one run's entry in the workflow results. NextPage is set while pages of it remain.
type Entry struct {
	Cursor   string `json:"cursor"`
	OTLP     OTLP   `json:"otlp"`
	NextPage int    `json:"next_page,omitempty"`
}

// Page is GET /predictions/feed: entries after a cursor, oldest first.
type Page struct {
	Entries []Entry `json:"entries"`
	Next    string  `json:"next"`
	Oldest  *string `json:"oldest"`
}

// Plain is an OTLP AnyValue as plain JSON values (an intValue as int64).
func Plain(v map[string]any) any {
	if kv, ok := v["kvlistValue"].(map[string]any); ok {
		out := map[string]any{}
		for _, e := range list(kv["values"]) {
			e, _ := e.(map[string]any)
			value, _ := e["value"].(map[string]any)
			out[fmt.Sprint(e["key"])] = Plain(value)
		}
		return out
	}
	if arr, ok := v["arrayValue"].(map[string]any); ok {
		out := []any{}
		for _, e := range list(arr["values"]) {
			value, _ := e.(map[string]any)
			out = append(out, Plain(value))
		}
		return out
	}
	if s, ok := v["intValue"]; ok {
		n, _ := strconv.ParseInt(fmt.Sprint(s), 10, 64)
		return n
	}
	for _, k := range []string{"doubleValue", "boolValue", "stringValue"} {
		if x, ok := v[k]; ok {
			return x
		}
	}
	return nil
}

func list(v any) []any {
	l, _ := v.([]any)
	return l
}

// Attributes are OTLP attributes as a plain map.
func Attributes(kvs []KeyValue) map[string]any {
	out := map[string]any{}
	for _, kv := range kvs {
		out[kv.Key] = Plain(kv.Value)
	}
	return out
}

// Part is one feed output's resource attributes and its items.
type Part struct {
	Resource map[string]any
	Items    []Item
}

// Unpack is an entry's parts for `consumer` (every part when it is ""). A part in an item
// shape this package does not read is skipped and logged.
func Unpack(otlp OTLP, consumer string, log func(string)) []Part {
	var parts []Part
	for _, logs := range otlp.ResourceLogs {
		resource := Attributes(logs.Resource.Attributes)
		if consumer != "" && resource["constellation.connection"] != consumer {
			continue
		}
		var records []LogRecord
		for _, scope := range logs.ScopeLogs {
			records = append(records, scope.LogRecords...)
		}
		var newer []string
		items := []Item{}
		for _, r := range records {
			if s := Attributes(r.Attributes)["constellation.schema"]; s != Schema {
				newer = append(newer, fmt.Sprint(s))
			}
			item, _ := Plain(r.Body).(map[string]any)
			items = append(items, item)
		}
		if len(newer) > 0 {
			log(fmt.Sprintf("skipped run %v: its items are %s and this bridge reads %s",
				resource["constellation.run_id"], strings.Join(newer, ", "), Schema))
			continue
		}
		parts = append(parts, Part{resource, items})
	}
	return parts
}

// Whole is `entry` with every page of its run joined in, each page's records added to their
// own resource. `fetch(cursor, page)` reads one page. A run is applied whole or not at all.
func Whole(entry Entry, fetch func(cursor string, page int) (Entry, error)) (Entry, error) {
	key := func(r ResourceLogs) string {
		b, _ := json.Marshal(r.Resource)
		return string(b)
	}
	logs := append([]ResourceLogs(nil), entry.OTLP.ResourceLogs...)
	for entry.NextPage != 0 {
		more, err := fetch(entry.Cursor, entry.NextPage)
		if err != nil {
			return entry, err
		}
		for _, m := range more.OTLP.ResourceLogs {
			found := false
			for i := range logs {
				if key(logs[i]) == key(m) {
					scope := logs[i].ScopeLogs[0]
					scope.LogRecords = append(append([]LogRecord(nil), scope.LogRecords...), m.ScopeLogs[0].LogRecords...)
					logs[i].ScopeLogs = append([]ScopeLogs{scope}, logs[i].ScopeLogs[1:]...)
					found = true
					break
				}
			}
			if !found {
				logs = append(logs, m)
			}
		}
		entry.NextPage = more.NextPage
	}
	entry.OTLP.ResourceLogs = logs
	return entry, nil
}

// Client calls the Constellation API with an API key.
type Client struct {
	URL  string
	Key  string
	HTTP *http.Client
}

// NewClient reads CONSTELLATION_API_URL (default DefaultURL) and CONSTELLATION_API_KEY.
func NewClient() *Client {
	u := os.Getenv("CONSTELLATION_API_URL")
	if u == "" {
		u = DefaultURL
	}
	return &Client{URL: strings.TrimRight(u, "/"), Key: strings.TrimSpace(os.Getenv("CONSTELLATION_API_KEY")),
		HTTP: &http.Client{Timeout: 60 * time.Second}}
}

// Error is an API answer outside 2xx, from the API at URL.
type Error struct {
	Status int
	Body   []byte
	URL    string
}

// Site is the Constellation site that serves `apiURL`, where its keys and billing pages are:
// api.<domain> is platform.<domain>, <env>.api.<domain> is <env>-platform.<domain>. Any other API
// is told the public site.
func Site(apiURL string) string {
	u, _ := url.Parse(apiURL)
	host := ""
	if u != nil {
		host = u.Hostname()
	}
	if rest, ok := strings.CutPrefix(host, "api."); ok {
		return "https://platform." + rest
	}
	if env, domain, ok := strings.Cut(host, ".api."); ok && env != "" {
		return "https://" + env + "-platform." + domain
	}
	return platform
}

func (e *Error) Error() string {
	if why := e.Refusal(); why != "" {
		return why
	}
	return fmt.Sprintf("Constellation answered %d: %.300s", e.Status, e.Body)
}

// Refusal is why Constellation refused the key or plan and the fix, for a 401 or 403; "" for
// any other status. Neither clears by asking again, so a bridge stops on one.
func (e *Error) Refusal() string {
	if e.Status != 401 && e.Status != 403 {
		return ""
	}
	fix := "make a key with predictions:read on " + Site(e.URL) + "/keys and set it as CONSTELLATION_API_KEY"
	if e.Status == 401 {
		return "Constellation does not accept CONSTELLATION_API_KEY (HTTP 401; wrong, revoked or expired): " + fix
	}
	var answer struct {
		Detail map[string]any `json:"detail"`
	}
	_ = json.Unmarshal(e.Body, &answer)
	d := answer.Detail
	switch d["error"] {
	case "plan_required":
		return fmt.Sprintf("this account's plan does not include %v (HTTP 403 plan_required); the %v plan does. Upgrade on %s/billing", d["feature"], d["plan"], Site(e.URL))
	case "insufficient_scope":
		return fmt.Sprintf("CONSTELLATION_API_KEY lacks %v (HTTP 403): %s", d["required"], fix)
	case "live_key_required":
		return "CONSTELLATION_API_KEY is a test key; results need a live one (HTTP 403): " + fix
	case nil:
		return "Constellation refused CONSTELLATION_API_KEY (HTTP 403 forbidden): " + fix
	}
	return fmt.Sprintf("Constellation refused CONSTELLATION_API_KEY (HTTP 403 %v): %s", d["error"], fix)
}

// Origin is a URL's scheme and host, lowercase, its port only when it is not the scheme's
// default, and any name for this machine (localhost, 127.0.0.1, ::1) as localhost: two URLs with
// one origin are the same API. "" for a URL that does not parse.
func Origin(raw string) string {
	u, err := url.Parse(strings.TrimSpace(raw))
	if err != nil || u.Scheme == "" || u.Host == "" {
		return ""
	}
	scheme, host, port := strings.ToLower(u.Scheme), strings.ToLower(u.Hostname()), u.Port()
	if Loopback(strings.TrimSpace(raw)) {
		host = "localhost"
	}
	if (scheme == "https" && port == "443") || (scheme == "http" && port == "80") {
		port = ""
	}
	if port != "" {
		host = net.JoinHostPort(host, port)
	}
	return scheme + "://" + host
}

// KeySafe says whether an API key may go to `raw`: over https, or plain http to this machine.
func KeySafe(raw string) bool {
	u, err := url.Parse(raw)
	if err != nil {
		return false
	}
	if strings.EqualFold(u.Scheme, "https") {
		return true
	}
	return strings.EqualFold(u.Scheme, "http") && Loopback(raw)
}

// Loopback says whether `raw` names this machine: localhost or a loopback address.
func Loopback(raw string) bool {
	u, err := url.Parse(raw)
	if err != nil {
		return false
	}
	ip := net.ParseIP(u.Hostname())
	return strings.EqualFold(u.Hostname(), "localhost") || (ip != nil && ip.IsLoopback())
}

// Call sends `body` as JSON (none when nil) and decodes the answer into `out` (when non-nil).
// It never sends the key over plain http to another machine.
func (c *Client) Call(method, path string, body, out any) error {
	if c.Key != "" && !KeySafe(c.URL) {
		return fmt.Errorf("not sending the API key to %s: it is not https (plain http only to localhost)", c.URL)
	}
	var reader io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			return err
		}
		reader = bytes.NewReader(b)
	}
	req, err := http.NewRequest(method, c.URL+path, reader)
	if err != nil {
		return err
	}
	req.Header.Set("x-api-key", c.Key)
	req.Header.Set("Content-Type", "application/json")
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(resp.Body)
	if err != nil {
		return err
	}
	if resp.StatusCode/100 != 2 {
		return &Error{resp.StatusCode, raw, c.URL}
	}
	if out == nil || len(raw) == 0 {
		return nil
	}
	return json.Unmarshal(raw, out)
}

// Results is the page of entries after cursor `after`. `consumer` names the connection reading
// it ("" reads every connection's outputs); `limit` is 1 to 20; `pageRecords` > 0 asks for
// runs in pages of that many records (join them with Whole).
func (c *Client) Results(after, consumer string, limit, pageRecords int) (Page, error) {
	q := url.Values{"after": {after}, "limit": {strconv.Itoa(limit)}}
	if consumer != "" {
		q.Set("consumer", consumer)
	}
	if pageRecords > 0 {
		q.Set("page_records", strconv.Itoa(pageRecords))
	}
	var page Page
	err := c.Call("GET", "/predictions/feed?"+q.Encode(), nil, &page)
	return page, err
}

// Start asks where a first start for `consumer` reads from (from=latest): no entries, and Next
// just before the newest run with something for it.
func (c *Client) Start(consumer string) (Page, error) {
	q := url.Values{"from": {"latest"}, "after": {""}, "limit": {"20"}, "page_records": {"1"}}
	if consumer != "" {
		q.Set("consumer", consumer)
	}
	var page Page
	err := c.Call("GET", "/predictions/feed?"+q.Encode(), nil, &page)
	return page, err
}

// RunPage is page `page` of the run at `cursor`.
func (c *Client) RunPage(cursor, consumer string, page, pageRecords int) (Entry, error) {
	q := url.Values{"cursor": {cursor}, "page": {strconv.Itoa(page)}, "page_records": {strconv.Itoa(pageRecords)}}
	if consumer != "" {
		q.Set("consumer", consumer)
	}
	var entry Entry
	err := c.Call("GET", "/predictions/feed?"+q.Encode(), nil, &entry)
	return entry, err
}
