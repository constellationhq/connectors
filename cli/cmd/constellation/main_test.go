package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"

	"github.com/constellationhq/connectors/cli/mock"
	"github.com/constellationhq/connectors/cli/results"
)

func cli(t *testing.T, args ...string) (int, string, string) {
	t.Helper()
	var out, errs bytes.Buffer
	code := run(args, &out, &errs)
	return code, out.String(), errs.String()
}

func mockAPI(t *testing.T) string {
	server := httptest.NewServer(mock.New(mock.Demo()))
	t.Cleanup(server.Close)
	return server.URL
}

func TestLoginChecksTheKeyAndStoresItForOwnerOnly(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("XDG_CONFIG_HOME", filepath.Join(home, ".config"))
	t.Setenv("CONSTELLATION_API_URL", "")
	t.Setenv("CONSTELLATION_API_KEY", "")
	url := mockAPI(t)
	code, out, errs := cli(t, "login", "--key", "k_live_1", "--url", url)
	if code != 0 || !strings.Contains(out, "account acct_demo") {
		t.Fatalf("%d %s %s", code, out, errs)
	}
	p, _ := configPath()
	info, err := os.Stat(p)
	if err != nil || info.Mode().Perm() != 0o600 {
		t.Fatalf("%v %v", info, err)
	}
	if c, err := client(); err != nil || c.URL != url || c.Key != "k_live_1" {
		t.Fatalf("stored %+v %v", c, err)
	}
	t.Setenv("CONSTELLATION_API_KEY", "from-env")
	if c, _ := client(); c.Key != "from-env" {
		t.Fatal("the environment does not win over the stored key")
	}
	if code, _, _ := cli(t, "login", "--key", "k", "--url", "http://127.0.0.1:1"); code == 0 {
		t.Fatal("an unreachable API was stored")
	}
}

func TestAStoredKeyIsNeverSentToAnotherAPI(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("XDG_CONFIG_HOME", filepath.Join(home, ".config"))
	t.Setenv("CONSTELLATION_API_URL", "")
	t.Setenv("CONSTELLATION_API_KEY", "")
	stored := mockAPI(t)
	if code, _, errs := cli(t, "login", "--key", "k_live_prod", "--url", stored); code != 0 {
		t.Fatal(errs)
	}
	var asked []string
	evil := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		asked = append(asked, r.Header.Get("x-api-key"))
		w.Write([]byte(`{"entries": [], "next": ""}`))
	}))
	t.Cleanup(evil.Close)
	t.Setenv("CONSTELLATION_API_URL", evil.URL)
	for _, args := range [][]string{{"whoami"}, {"results", "tail", "--once"}} {
		code, _, errs := cli(t, args...)
		if code == 0 || !strings.Contains(errs, "it is never sent anywhere else") {
			t.Errorf("%v: %d %q", args, code, errs)
		}
	}
	if len(asked) != 0 {
		t.Fatalf("the other API was called with keys %q", asked)
	}
	// A key given for that API, for this run, goes to it.
	t.Setenv("CONSTELLATION_API_KEY", "k_for_it")
	if code, _, errs := cli(t, "results", "tail", "--once"); code != 0 || len(asked) == 0 || asked[0] != "k_for_it" {
		t.Fatalf("%d %q %q", code, errs, asked)
	}
	// The same API spelled with its default port is the same origin.
	t.Setenv("CONSTELLATION_API_KEY", "")
	// This machine by another name is the same API (the mock prints 127.0.0.1, the README says localhost).
	t.Setenv("CONSTELLATION_API_URL", strings.Replace(stored, "127.0.0.1", "localhost", 1))
	if c, err := client(); err != nil || c.Key != "k_live_prod" {
		t.Fatalf("localhost for 127.0.0.1: %+v %v", c, err)
	}
	t.Setenv("CONSTELLATION_API_URL", strings.TrimRight(stored, "/")+"/")
	if c, err := client(); err != nil || c.Key != "k_live_prod" {
		t.Fatalf("%+v %v", c, err)
	}
}

func TestNoKeyGoesOverPlainHTTPExceptToLocalhost(t *testing.T) {
	for raw, safe := range map[string]bool{
		"https://api.constellation.space": true, "http://127.0.0.1:7070": true, "http://localhost:7070": true,
		"http://[::1]:7070": true, "http://api.constellation.space": false, "http://10.0.0.5:7070": false, "ftp://x": false,
	} {
		if got := results.KeySafe(raw); got != safe {
			t.Errorf("%s: %v, want %v", raw, got, safe)
		}
	}
	c := &results.Client{URL: "http://api.example.com", Key: "k", HTTP: http.DefaultClient}
	if err := c.Call("GET", "/x", nil, nil); err == nil || !strings.Contains(err.Error(), "not https") {
		t.Fatalf("%v", err)
	}
	if code, _, errs := cli(t, "login", "--key", "k", "--url", "http://api.example.com"); code == 0 || !strings.Contains(errs, "not https") {
		t.Fatalf("login over plain http: %d %q", code, errs)
	}
	if results.Origin("HTTPS://API.constellation.space:443/v1") != "https://api.constellation.space" {
		t.Error("origin")
	}
}

func TestResultsTailPrintsEveryItemOnce(t *testing.T) {
	t.Setenv("CONSTELLATION_API_URL", mockAPI(t))
	t.Setenv("CONSTELLATION_API_KEY", "")
	code, out, errs := cli(t, "results", "tail", "--json", "--once", "--from-start")
	if code != 0 {
		t.Fatalf("%d %s", code, errs)
	}
	lines := strings.Split(strings.TrimSpace(out), "\n")
	total := 0
	for _, e := range mock.Demo().Entries {
		if e.Schema == "" {
			total += len(e.Items)
		}
	}
	if len(lines) != total {
		t.Fatalf("%d lines for %d items", len(lines), total)
	}
	var first map[string]any
	if err := json.Unmarshal([]byte(lines[0]), &first); err != nil || first["run_id"] == nil || first["item"] == nil {
		t.Fatalf("%v %v", first, err)
	}
	if !strings.Contains(errs, "constellation.item/v9") {
		t.Error("the run in a newer shape was skipped silently")
	}
	_, table, _ := cli(t, "results", "tail", "--once", "--from-start")
	for _, state := range []string{"forecast", "advice", "approved", "pending", "rejected"} {
		if !strings.Contains(table, " "+state+" ") {
			t.Errorf("no %s row in\n%s", state, table)
		}
	}
}

func TestResultsTailStartsAtTheNewestRun(t *testing.T) {
	t.Setenv("CONSTELLATION_API_URL", mockAPI(t))
	t.Setenv("CONSTELLATION_API_KEY", "")
	_, all, _ := cli(t, "results", "tail", "--json", "--once", "--from-start")
	code, out, errs := cli(t, "results", "tail", "--json", "--once")
	lines := strings.Split(strings.TrimSpace(out), "\n")
	if code != 0 || out == "" || !strings.HasSuffix(all, out) || len(lines) >= len(strings.Split(all, "\n")) {
		t.Fatalf("%d %q %s", code, out, errs)
	}
	var cursors = map[string]bool{}
	for _, line := range lines {
		var l map[string]any
		json.Unmarshal([]byte(line), &l)
		cursors[l["cursor"].(string)] = true
	}
	if len(cursors) != 1 {
		t.Fatalf("not one run: %v", cursors)
	}
}

func TestResultsTailOnceExitsOnAnyError(t *testing.T) {
	t.Setenv("CONSTELLATION_API_KEY", "k")
	for _, status := range []int{404, 500} {
		api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(status)
			w.Write([]byte(`{"message":"Not Found"}`))
		}))
		t.Cleanup(api.Close)
		t.Setenv("CONSTELLATION_API_URL", api.URL)
		code, _, errs := cli(t, "results", "tail", "--once", "--every", "1h")
		if code != 1 || !strings.Contains(errs, "reading the results from "+api.URL) || !strings.Contains(errs, "answered "+strconv.Itoa(status)) {
			t.Errorf("%d: %d %q", status, code, errs)
		}
	}
	t.Setenv("CONSTELLATION_API_URL", "http://127.0.0.1:1")
	if code, _, errs := cli(t, "results", "tail", "--once", "--from-start", "--every", "1h"); code != 1 || !strings.Contains(errs, "connection refused") {
		t.Errorf("offline: %d %q", code, errs)
	}
	t.Setenv("CONSTELLATION_API_KEY", "")
	t.Setenv("CONSTELLATION_API_URL", "https://api.example.invalid")
	if code, _, errs := cli(t, "results", "tail", "--once"); code != 1 || !strings.Contains(errs, "no API key") {
		t.Errorf("no key: %d %q", code, errs)
	}
}

func TestConformanceReportsAFailingBridgeAsJSONAndExitsNonzero(t *testing.T) {
	code, out, _ := cli(t, "conformance", "--json", "--", "false")
	var result struct {
		Passed bool
		Checks []map[string]any
	}
	if code != 1 || json.Unmarshal([]byte(out), &result) != nil || result.Passed || len(result.Checks) == 0 {
		t.Fatalf("%d %s", code, out)
	}
	if code, _, _ := cli(t, "conformance"); code != 2 {
		t.Fatal("no command is a usage error")
	}
}

func TestBridgeNewAndRunExplainThemselves(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "b")
	if code, _, errs := cli(t, "bridge", "new", "my-bridge", "--lang", "go", "--dir", dir); code != 0 {
		t.Fatal(errs)
	}
	if _, err := os.Stat(filepath.Join(dir, "main.go")); err != nil {
		t.Fatal(err)
	}
	if code, _, _ := cli(t, "bridge", "run", "cosmos"); code != 2 {
		t.Fatal("an unknown official bridge is a usage error")
	}
	// A Python without the package, whatever this machine has installed.
	python := filepath.Join(t.TempDir(), "python3")
	script := "#!/bin/sh\necho \"ModuleNotFoundError: No module named 'constellation_space'\" >&2\nexit 1\n"
	if err := os.WriteFile(python, []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PYTHON", python)
	code, _, errs := cli(t, "bridge", "run", "yamcs")
	if code == 0 || !strings.HasSuffix(strings.TrimSpace(errs), "pip install constellation-space") {
		t.Fatalf("%d %s", code, errs)
	}
	if code, _, errs := cli(t, "bridge", "run", "lattice"); code == 0 || !strings.Contains(errs, "pip install 'constellation-space[lattice]'") {
		t.Fatalf("%d %s", code, errs)
	}
}

func TestVerifyHelpSaysWhatItChecks(t *testing.T) {
	code, _, errs := cli(t, "verify", "--help")
	if code != 2 || !strings.HasPrefix(errs, "usage: constellation verify DIR\n\nChecks a downloaded release") {
		t.Fatalf("%d %q", code, errs)
	}
}

func TestAnUnknownFlagIsReportedOnce(t *testing.T) {
	for _, args := range [][]string{{"results", "tail", "--bogus"}, {"verify", "--bogus"}, {"mock", "--bogus"}} {
		if code, _, errs := cli(t, args...); code != 2 || strings.Contains(errs, "constellation: flag provided") {
			t.Errorf("%v: %d %q", args, code, errs)
		}
	}
}

func TestVersion(t *testing.T) {
	if _, out, _ := cli(t, "version"); strings.TrimSpace(out) != version {
		t.Fatal(out)
	}
}

// release writes a release folder with a SHA256SUMS bundle, and points verify at a fake cosign
// that passes a bundle reading "good" signed as the release workflow, and fails any other.
func release(t *testing.T) string {
	dir := t.TempDir()
	artifact := []byte("the binary")
	os.WriteFile(filepath.Join(dir, "constellation_linux_amd64"), artifact, 0o644)
	h := sha256.Sum256(artifact)
	sums := []byte(hex.EncodeToString(h[:]) + "  constellation_linux_amd64\n" + strings.Repeat("0", 64) + "  constellation_windows.zip\n")
	os.WriteFile(filepath.Join(dir, "SHA256SUMS"), sums, 0o644)
	os.WriteFile(filepath.Join(dir, "SHA256SUMS.sigstore.json"), []byte("good"), 0o644)
	cosign := filepath.Join(t.TempDir(), "cosign")
	script := `#!/bin/sh
case "$*" in
  *"--certificate-identity-regexp ` + releaseIdentity + ` --certificate-oidc-issuer ` + releaseIssuer + ` "*) ;;
  *) echo "unexpected arguments: $*"; exit 2 ;;
esac
[ "$(cat "$3")" = good ] || { echo "none of the expected identities matched"; exit 1; }
echo Verified OK
`
	os.WriteFile(cosign, []byte(script), 0o755)
	t.Setenv("COSIGN", cosign)
	return dir
}

func TestVerifyChecksTheSignatureAndEveryDownloadedFile(t *testing.T) {
	dir := release(t)
	code, out, errs := cli(t, "verify", dir)
	if code != 0 || !strings.Contains(out, "ok  SHA256SUMS is signed") || !strings.Contains(out, "ok  constellation_linux_amd64") || !strings.Contains(out, "not downloaded") {
		t.Fatalf("%d %s %s", code, out, errs)
	}
	os.WriteFile(filepath.Join(dir, "constellation_linux_amd64"), []byte("tampered"), 0o644)
	if code, out, _ := cli(t, "verify", dir); code == 0 || !strings.Contains(out, "BAD constellation_linux_amd64") {
		t.Fatalf("a tampered file passed: %s", out)
	}
	dir = release(t)
	os.WriteFile(filepath.Join(dir, "SHA256SUMS.sigstore.json"), []byte("someone else"), 0o644)
	if code, out, errs := cli(t, "verify", dir); code == 0 || !strings.Contains(errs, "not signed by the constellationhq/connectors release workflow") || strings.Contains(out, "ok") {
		t.Fatalf("another signer passed: %d %q %q", code, out, errs)
	}
}

func TestVerifyRefusesAnUnsignedReleaseOrNoCosign(t *testing.T) {
	dir := release(t)
	os.Remove(filepath.Join(dir, "SHA256SUMS.sigstore.json"))
	if code, out, errs := cli(t, "verify", dir); code == 0 || !strings.Contains(errs, "SHA256SUMS is not signed") || strings.Contains(out, "ok") {
		t.Fatalf("an unsigned release: %d %q %q", code, out, errs)
	}
	dir = release(t)
	t.Setenv("COSIGN", filepath.Join(t.TempDir(), "no-cosign"))
	if code, out, errs := cli(t, "verify", dir); code == 0 || !strings.Contains(errs, "cosign, which is not installed") || strings.Contains(out, "ok") {
		t.Fatalf("no cosign: %d %q %q", code, out, errs)
	}
}
