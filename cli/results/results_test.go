package results

import (
	"strings"
	"testing"
)

func TestActionStateSaysWhetherAnItemIsAdviceAnInstructionOrHeld(t *testing.T) {
	swap := func(mode string) map[string]any {
		return map[string]any{"verb": "swap", "from": "p1", "to": "p2", "mode": mode}
	}
	for _, c := range []struct {
		facets map[string]any
		want   string
	}{
		{map[string]any{}, ""},
		{map[string]any{"action": swap("suggest")}, "advice"},
		{map[string]any{"action": swap("approve"), "action_status": "approved"}, "approved"},
		{map[string]any{"action_status": "pending"}, "pending"},
		{map[string]any{"action_status": "rejected"}, "rejected"},
		// A status a bridge does not know is held, never a plain forecast, even with an action.
		{map[string]any{"action_status": "expired"}, "pending"},
		{map[string]any{"action": swap("approve"), "action_status": "expired"}, "pending"},
		// Approved with nothing to carry out: held.
		{map[string]any{"action_status": "approved"}, "pending"},
	} {
		item := Item{"entity": map[string]any{"type": "pass", "id": "p1"}}
		for k, v := range c.facets {
			item[k] = v
		}
		if got := ActionState(item); got != c.want {
			t.Errorf("%v: %q, want %q", c.facets, got, c.want)
		}
	}
}

func TestOnly401And403AreRefusals(t *testing.T) {
	plan := []byte(`{"detail": {"error": "plan_required", "plan": "enterprise", "feature": "output:custom"}}`)
	if why := (&Error{Status: 403, Body: plan}).Refusal(); !strings.Contains(why, "plan does not include output:custom") {
		t.Errorf("403: %q", why)
	}
	if why := (&Error{Status: 401}).Refusal(); !strings.Contains(why, "HTTP 401") {
		t.Errorf("401: %q", why)
	}
	for _, status := range []int{400, 404, 429, 500, 503} {
		if why := (&Error{Status: status, Body: plan}).Refusal(); why != "" {
			t.Errorf("%d is a refusal: %q", status, why)
		}
	}
}

func TestPlainUndoesOTLPAnyValues(t *testing.T) {
	v := map[string]any{"kvlistValue": map[string]any{"values": []any{
		map[string]any{"key": "n", "value": map[string]any{"intValue": "3"}},
		map[string]any{"key": "f", "value": map[string]any{"doubleValue": 1.5}},
		map[string]any{"key": "l", "value": map[string]any{"arrayValue": map[string]any{"values": []any{
			map[string]any{"stringValue": "a"}, map[string]any{"boolValue": true}}}}},
	}}}
	got := Plain(v).(map[string]any)
	if got["n"] != int64(3) || got["f"] != 1.5 || got["l"].([]any)[0] != "a" || got["l"].([]any)[1] != true {
		t.Fatalf("%v", got)
	}
}

func TestARefusalLinksTheKeysPageOfTheAPIInUse(t *testing.T) {
	for api, keys := range map[string]string{
		"https://api.constellation.space":       "https://platform.constellation.space/keys",
		"https://dev.api.constellation.space/":  "https://dev-platform.constellation.space/keys",
		"https://stage.api.constellation.space": "https://stage-platform.constellation.space/keys",
		"http://127.0.0.1:7070":                 "https://platform.constellation.space/keys",
	} {
		if why := (&Error{Status: 401, URL: api}).Refusal(); !strings.Contains(why, keys) {
			t.Errorf("%s: %q", api, why)
		}
	}
}

func TestOriginCountsEveryNameForThisMachineAsOne(t *testing.T) {
	for _, raw := range []string{"http://localhost:7070", "http://127.0.0.1:7070/", "HTTP://[::1]:7070", " http://LOCALHOST:7070"} {
		if got := Origin(raw); got != "http://localhost:7070" {
			t.Errorf("%s: %s", raw, got)
		}
	}
	if Origin("http://127.0.0.1:7071") == Origin("http://localhost:7070") || Origin("https://localhost:7070") == Origin("http://localhost:7070") {
		t.Error("another port or scheme is another API")
	}
}
